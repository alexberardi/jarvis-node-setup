"""The WiFi PSK must never reach a command line.

sudo journals every command line (``COMMAND=/usr/bin/nmcli ... password
<psk>``) and argv is world-readable via /proc while the process runs. The
2026-10-08 dev-Pi provisioning run leaked the home WiFi PSK into the journal
through both ``nmcli dev wifi connect ... password <psk>`` and
``nmcli connection add ... wifi-sec.psk <psk>``. These tests pin the
replacement: the profile is created without a secret and the PSK goes to
``nmcli connection up`` through a 0600 passwd-file that is deleted afterwards.

They also pin the profile bookkeeping the failure-recovery path relies on:
rollback removes only what this attempt created; commit finalises it.
"""

import stat
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from provisioning.models import NetworkInfo
from provisioning.wifi_manager import (
    HostapdWiFiManager,
    NetworkManagerWiFi,
    clear_network_cache,
    set_cached_networks,
)

PSK = "s3cret-Home:Psk!"
SSID = "HomeNet"


class FakeNmcli:
    """Records nmcli argvs and answers like NetworkManager would."""

    def __init__(self, existing: set[str] | None = None, up_rc: int = 0,
                 add_rc: int = 0, up_raises: Exception | None = None) -> None:
        self.calls: list[list[str]] = []
        self.existing = set(existing or ())
        self.up_rc = up_rc
        self.add_rc = add_rc
        self.up_raises = up_raises
        self.psk_file_seen: dict | None = None

    def __call__(self, cmd: list[str], **kwargs) -> MagicMock:
        self.calls.append(list(cmd))
        args = cmd[cmd.index("nmcli") + 1:] if "nmcli" in cmd else cmd
        rc = 0
        if args[:3] == ["connection", "show", "id"]:
            rc = 0 if args[3] in self.existing else 10
        elif args[:2] == ["connection", "add"]:
            rc = self.add_rc
            if rc == 0:
                self.existing.add(args[args.index("con-name") + 1])
        elif args[:3] == ["connection", "delete", "id"]:
            rc = 0 if args[3] in self.existing else 10
            self.existing.discard(args[3])
        elif args[:2] == ["connection", "up"]:
            if "passwd-file" in args:
                path = Path(args[args.index("passwd-file") + 1])
                self.psk_file_seen = {
                    "path": path,
                    "content": path.read_text(),
                    "mode": stat.S_IMODE(path.stat().st_mode),
                }
            if self.up_raises:
                raise self.up_raises
            rc = self.up_rc
        return MagicMock(returncode=rc, stdout="", stderr="")

    def nm_args(self) -> list[list[str]]:
        return [c[c.index("nmcli") + 1:] for c in self.calls if "nmcli" in c]

    def find(self, *prefix: str) -> list[list[str]]:
        return [a for a in self.nm_args() if a[:len(prefix)] == list(prefix)]


@pytest.fixture(autouse=True)
def _isolate(tmp_path):
    clear_network_cache()
    with patch("provisioning.wifi_manager.get_secret_dir", return_value=tmp_path):
        yield
    clear_network_cache()


def _managers() -> list:
    hostapd = HostapdWiFiManager()
    hostapd._wait_for_ssid = MagicMock(return_value=True)  # skip the 60s scan wait
    return [NetworkManagerWiFi(), hostapd]


@pytest.mark.parametrize("wifi", _managers(), ids=["networkmanager", "hostapd"])
class TestPskNeverOnCommandLine:
    def test_psk_absent_from_every_argv(self, wifi, tmp_path):
        fake = FakeNmcli()
        with patch("subprocess.run", side_effect=fake), patch("os.geteuid", return_value=1000):
            assert wifi.connect(SSID, PSK) is True
        assert fake.calls, "expected nmcli to be invoked"
        for argv in fake.calls:
            assert not any(PSK in part for part in argv), f"PSK leaked into argv: {argv}"

    def test_psk_delivered_via_0600_passwd_file(self, wifi, tmp_path):
        fake = FakeNmcli()
        with patch("subprocess.run", side_effect=fake), patch("os.geteuid", return_value=1000):
            wifi.connect(SSID, PSK)
        seen = fake.psk_file_seen
        assert seen is not None, "connection up must use passwd-file"
        assert seen["content"] == f"802-11-wireless-security.psk:{PSK}\n"
        assert seen["mode"] == 0o600

    def test_passwd_file_deleted_after_success(self, wifi, tmp_path):
        fake = FakeNmcli()
        with patch("subprocess.run", side_effect=fake), patch("os.geteuid", return_value=1000):
            wifi.connect(SSID, PSK)
        assert not fake.psk_file_seen["path"].exists()
        assert list(tmp_path.glob(".nm-psk-*")) == []

    def test_passwd_file_deleted_when_up_fails(self, wifi, tmp_path):
        fake = FakeNmcli(up_rc=4)
        with patch("subprocess.run", side_effect=fake), patch("os.geteuid", return_value=1000):
            assert wifi.connect(SSID, PSK) is False
        assert not fake.psk_file_seen["path"].exists()

    def test_passwd_file_deleted_when_up_times_out(self, wifi, tmp_path):
        fake = FakeNmcli(up_raises=subprocess.TimeoutExpired("nmcli", 45))
        with patch("subprocess.run", side_effect=fake), patch("os.geteuid", return_value=1000):
            assert wifi.connect(SSID, PSK) is False
        assert list(tmp_path.glob(".nm-psk-*")) == []

    def test_profile_created_without_secret_and_autoconnect_off(self, wifi):
        fake = FakeNmcli()
        with patch("subprocess.run", side_effect=fake), patch("os.geteuid", return_value=1000):
            wifi.connect(SSID, PSK)
        (add,) = fake.find("connection", "add")
        assert "wifi-sec.psk" not in add
        assert add[add.index("wifi-sec.key-mgmt") + 1] == "wpa-psk"
        assert add[add.index("wifi-sec.psk-flags") + 1] == "0"
        assert add[add.index("connection.autoconnect") + 1] == "no"
        assert add[add.index("ssid") + 1] == SSID

    def test_open_network_has_no_security_and_no_passwd_file(self, wifi):
        fake = FakeNmcli()
        with patch("subprocess.run", side_effect=fake), patch("os.geteuid", return_value=1000):
            assert wifi.connect(SSID, "") is True
        (add,) = fake.find("connection", "add")
        assert "wifi-sec.key-mgmt" not in add
        (up,) = fake.find("connection", "up")
        assert "passwd-file" not in up

    def test_wpa3_only_network_uses_sae(self, wifi):
        set_cached_networks([NetworkInfo(ssid=SSID, signal_strength=-50, security="WPA3")])
        fake = FakeNmcli()
        with patch("subprocess.run", side_effect=fake), patch("os.geteuid", return_value=1000):
            wifi.connect(SSID, PSK)
        (add,) = fake.find("connection", "add")
        assert add[add.index("wifi-sec.key-mgmt") + 1] == "sae"

    def test_stale_passwd_files_are_swept(self, wifi, tmp_path):
        stale = tmp_path / ".nm-psk-leftover"
        stale.write_text("802-11-wireless-security.psk:old\n")
        fake = FakeNmcli()
        with patch("subprocess.run", side_effect=fake), patch("os.geteuid", return_value=1000):
            wifi.connect(SSID, PSK)
        assert not stale.exists()


def test_no_dev_wifi_connect_fallback():
    """`nmcli dev wifi connect ... password <psk>` is gone entirely."""
    fake = FakeNmcli()
    wifi = HostapdWiFiManager()
    wifi._wait_for_ssid = MagicMock(return_value=True)
    with patch("subprocess.run", side_effect=fake), patch("os.geteuid", return_value=1000):
        wifi.connect(SSID, PSK)
    assert fake.find("dev", "wifi", "connect") == []
    assert all("password" not in a for a in fake.nm_args())


def test_hostapd_manager_uses_sudo_for_nmcli():
    fake = FakeNmcli()
    wifi = HostapdWiFiManager()
    wifi._wait_for_ssid = MagicMock(return_value=True)
    with patch("subprocess.run", side_effect=fake), patch("os.geteuid", return_value=1000):
        wifi.connect(SSID, PSK)
    up = [c for c in fake.calls if "up" in c and "nmcli" in c]
    assert up and up[0][:3] == ["sudo", "-n", "nmcli"]


class TestProfileBookkeeping:
    @pytest.fixture
    def wifi(self):
        w = HostapdWiFiManager()
        w._wait_for_ssid = MagicMock(return_value=True)
        return w

    def _run(self, wifi, fake, fn, *args):
        with patch("subprocess.run", side_effect=fake), patch("os.geteuid", return_value=0):
            return fn(*args)

    def test_new_profile_is_deleted_on_rollback(self, wifi):
        fake = FakeNmcli()
        self._run(wifi, fake, wifi.connect, SSID, PSK)
        self._run(wifi, fake, wifi.rollback_connection)
        assert fake.find("connection", "delete", "id", f"jarvis-{SSID}")
        assert f"jarvis-{SSID}" not in fake.existing

    def test_preexisting_profile_is_kept_on_rollback(self, wifi):
        fake = FakeNmcli(existing={f"jarvis-{SSID}"})
        self._run(wifi, fake, wifi.connect, SSID, PSK)
        (add,) = fake.find("connection", "add")
        assert add[add.index("con-name") + 1] == f"jarvis-{SSID}-new"

        self._run(wifi, fake, wifi.rollback_connection)
        assert f"jarvis-{SSID}" in fake.existing, "pre-existing profile must survive"
        assert f"jarvis-{SSID}-new" not in fake.existing
        assert not fake.find("connection", "delete", "id", f"jarvis-{SSID}")

    def test_rollback_after_failed_add_deletes_nothing(self, wifi):
        fake = FakeNmcli(add_rc=1)
        assert self._run(wifi, fake, wifi.connect, SSID, PSK) is False
        self._run(wifi, fake, wifi.rollback_connection)
        assert fake.find("connection", "delete") == []

    def test_commit_enables_autoconnect(self, wifi):
        fake = FakeNmcli()
        self._run(wifi, fake, wifi.connect, SSID, PSK)
        self._run(wifi, fake, wifi.commit_connection)
        (modify,) = fake.find("connection", "modify")
        assert modify[3] == f"jarvis-{SSID}"
        assert modify[modify.index("connection.autoconnect") + 1] == "yes"
        assert modify[modify.index("connection.autoconnect-priority") + 1] == "999"
        assert "connection.id" not in modify

    def test_commit_replaces_preexisting_profile(self, wifi):
        fake = FakeNmcli(existing={f"jarvis-{SSID}"})
        self._run(wifi, fake, wifi.connect, SSID, PSK)
        self._run(wifi, fake, wifi.commit_connection)
        assert fake.find("connection", "delete", "id", f"jarvis-{SSID}")
        (modify,) = fake.find("connection", "modify")
        assert modify[3] == f"jarvis-{SSID}-new"
        assert modify[modify.index("connection.id") + 1] == f"jarvis-{SSID}"

    def test_rollback_after_commit_is_noop(self, wifi):
        fake = FakeNmcli()
        self._run(wifi, fake, wifi.connect, SSID, PSK)
        self._run(wifi, fake, wifi.commit_connection)
        before = len(fake.calls)
        self._run(wifi, fake, wifi.rollback_connection)
        assert len(fake.calls) == before
