"""Behavioural tests for scripts/jarvis-net-watchdog.

These drive the real shell script with stubbed `ip`/`ping`/`nmcli`/`iw`/
`systemctl` on PATH, so we assert the actual escalation logic that recovers a
node which has fallen off the network — the exact failure (alive-but-offline
node) that had ZERO test coverage before the 2026-06 kitchen-node incident.

The stubs make `systemctl reboot` a no-op that only records the call, so the
"reboot is the last resort" path is exercised without ever touching the host.
"""

import os
import subprocess
import textwrap
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "jarvis-net-watchdog"


def _write_stub(path: Path, body: str) -> None:
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(0o755)


def _make_stubs(bindir: Path, calllog: Path, ping_file: Path) -> None:
    log = f'echo "$(basename "$0") $*" >> "{calllog}"'
    # `ip route show default ...` emits a gateway; everything else just logs.
    _write_stub(bindir / "ip", textwrap.dedent(f"""\
        {log}
        case "$*" in
          "route show default"*) echo "default via 10.0.0.1 dev wlan0 proto dhcp" ;;
        esac
        exit 0
    """))
    # ping exit code is whatever the test put in ping_file (0 up / 1 down).
    _write_stub(bindir / "ping", textwrap.dedent(f"""\
        {log}
        exit "$(cat "{ping_file}" 2>/dev/null || echo 1)"
    """))
    # nmcli: log every call, and answer the two queries the watchdog uses to
    # find the preferred (highest autoconnect-priority) wifi profile, so the
    # duplicate-profile recovery path (`nmcli connection up <pref>`) runs.
    _write_stub(bindir / "nmcli", textwrap.dedent(f"""\
        {log}
        case "$*" in
          "-t -f NAME,TYPE connection show") echo "wlan-good:802-11-wireless" ;;
          "-t -g connection.autoconnect-priority connection show wlan-good") echo "999" ;;
        esac
        exit 0
    """))
    for name in ("iw", "logger", "systemctl", "reboot"):
        _write_stub(bindir / name, f"{log}\nexit 0\n")


class Harness:
    def __init__(self, tmp_path: Path):
        self.bindir = tmp_path / "bin"
        self.bindir.mkdir()
        self.calllog = tmp_path / "calls.log"
        self.calllog.write_text("")
        self.ping_file = tmp_path / "ping_result"
        self.ping_file.write_text("1")
        sysfs = tmp_path / "sysfs"
        sysfs.mkdir()
        (sysfs / "wlan0").write_text("")  # interface "exists"
        uptime = tmp_path / "uptime"
        uptime.write_text("9999.00 1.00\n")  # well past NETWD_MIN_UPTIME
        _make_stubs(self.bindir, self.calllog, self.ping_file)

        self.reboot_log = tmp_path / "reboots"
        self.env = dict(os.environ)
        self.env["PATH"] = f"{self.bindir}:{self.env['PATH']}"
        self.env["NETWD_STATE"] = str(tmp_path / "state")
        self.env["NETWD_BREADCRUMB"] = str(tmp_path / "breadcrumb.json")
        self.env["NETWD_SYSFS_NET"] = str(sysfs)
        self.env["NETWD_UPTIME_FILE"] = str(uptime)
        self.env["NETWD_REBOOT_LOG"] = str(self.reboot_log)  # never touch /var/lib in tests
        self.breadcrumb = tmp_path / "breadcrumb.json"
        self._mark = 0

    def tick(self, *, online: bool) -> str:
        """Run one watchdog cycle; return only the stub calls it made this run."""
        self.ping_file.write_text("0" if online else "1")
        r = subprocess.run(
            ["bash", str(SCRIPT)], env=self.env, capture_output=True, text=True
        )
        assert r.returncode == 0, f"watchdog exited {r.returncode}: {r.stderr}"
        full = self.calllog.read_text()
        new = full[self._mark:]
        self._mark = len(full)
        return new


def test_healthy_tick_only_enforces_powersave(tmp_path):
    h = Harness(tmp_path)
    calls = h.tick(online=True)
    # Power-save is forced off every run...
    assert "iw dev wlan0 set power_save off" in calls
    # ...and a healthy node is left alone — no recovery actions.
    assert "device reapply" not in calls
    assert "link set wlan0 down" not in calls
    assert "systemctl reboot" not in calls


def test_escalation_reapply_then_bounce_then_reboot(tmp_path):
    h = Harness(tmp_path)
    h.tick(online=True)  # establish "was online this boot" (arms the reboot guard)

    assert "device reapply" not in h.tick(online=False)   # fail 1
    assert "device reapply" not in h.tick(online=False)   # fail 2

    reapply = h.tick(online=False)  # fail 3 → reapply + force preferred profile
    assert "nmcli device reapply wlan0" in reapply
    assert "nmcli connection up wlan-good" in reapply  # recovers the duplicate-profile race

    bounce = h.tick(online=False)  # fail 4 → bounce
    assert "ip link set wlan0 down" in bounce
    assert "ip link set wlan0 up" in bounce
    assert "nmcli connection up wlan-good" in bounce

    # fail 5 keeps bouncing (gives DHCP/assoc ~2 min after the bounce) — NOT reboot.
    five = h.tick(online=False)
    assert "ip link set wlan0 down" in five
    assert "systemctl reboot" not in five

    reboot = h.tick(online=False)  # fail 6 → reboot (last resort)
    assert "systemctl reboot" in reboot
    assert h.breadcrumb.exists()
    assert "lan_down_after_online" in h.breadcrumb.read_text()


def test_reboot_is_rate_limited_across_reboots(tmp_path):
    """The persistent guard: if the node has already rebooted REBOOT_MAX times
    in the window, the watchdog must STOP rebooting (and keep bouncing) even
    though it was online this boot — otherwise a flapping link reboot-loops."""
    h = Harness(tmp_path)
    now = int(time.time())
    # Pre-seed the persistent log with REBOOT_MAX(=3) recent reboots.
    h.reboot_log.write_text(f"{now - 30}\n{now - 60}\n{now - 90}\n")

    h.tick(online=True)  # arm online guard
    for _ in range(5):
        h.tick(online=False)
    capped = h.tick(online=False)  # fail >= 6, but rate limit is hit
    assert "systemctl reboot" not in capped
    assert "ip link set wlan0 down" in capped  # still bouncing


def test_recovery_resets_the_counter(tmp_path):
    h = Harness(tmp_path)
    h.tick(online=True)
    h.tick(online=False)
    h.tick(online=False)
    h.tick(online=True)  # recovered before reaching reapply
    # After recovery, the next failure starts the ladder over at 1 (no reapply).
    assert "device reapply" not in h.tick(online=False)


def test_never_online_never_reboots(tmp_path):
    """Boot-loop guard: a node that never reached the network must keep trying
    reapply/bounce but must NEVER reboot itself."""
    h = Harness(tmp_path)
    saw_reapply = saw_bounce = False
    for _ in range(8):  # well past FAIL_REBOOT=5
        calls = h.tick(online=False)
        assert "systemctl reboot" not in calls
        saw_reapply = saw_reapply or "nmcli device reapply wlan0" in calls
        saw_bounce = saw_bounce or "ip link set wlan0 down" in calls
    assert saw_reapply and saw_bounce


def test_skips_when_interface_absent(tmp_path):
    h = Harness(tmp_path)
    h.env["NETWD_IFACE"] = "wlan_missing"
    calls = h.tick(online=False)
    assert calls.strip() == ""  # no ip/ping/iw — clean no-op
