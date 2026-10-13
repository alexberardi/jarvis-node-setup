"""Tests for the PulseAudio steps of install.sh.

The node captures from the HAT through ALSA dsnoop and plays through
PulseAudio. Two failures shipped silent nodes:

  * a per-user mask on pulseaudio.{socket,service} (left by a hand
    ``systemctl --user mask``) beat the system-wide enable, so PulseAudio
    never started and TTS went nowhere (jarvis-dev, 2026-09-19 → 10-08);
  * module-udev-detect gave the HAT to whichever side opened it first at
    boot: PulseAudio first took the mic, the node first left PulseAudio with
    no sink. And a sink at 44.1 kHz makes the node's 48 kHz capture fail.

Pins:
  - switch_audio_to_pulseaudio removes a per-user /dev/null mask (and only
    that: a real unit file is left alone)
  - configure_pulseaudio_hat_sink writes the PULSE_IGNORE udev rule and a
    playback-only sink at rate=48000 named jarvis_output, set as default
  - both are idempotent (second run changes nothing, no reboot flagged)
  - SKIP_AUDIO=1 writes nothing
  - main() runs the HAT step
"""

import os
import re
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
INSTALL_SH = _REPO_ROOT / "install.sh"


def _extract(name: str) -> str:
    text = INSTALL_SH.read_text()
    match = re.search(rf"^{name}\(\)\s*\{{.*?^\}}", text, re.MULTILINE | re.DOTALL)
    assert match, f"{name}() not found in install.sh"
    return match.group(0)


def _hat_card_id() -> str:
    match = re.search(r'^HAT_CARD_ID="([^"]+)"', INSTALL_SH.read_text(), re.MULTILINE)
    assert match
    return match.group(1)


def _run(root: Path, body: str, skip_audio: int = 0) -> subprocess.CompletedProcess:
    """Run a function with /etc and /usr/lib redirected under root; prints
    NEEDS_REBOOT last."""
    body = body.replace("/etc/", f"{root}/etc/").replace("/usr/lib/", f"{root}/usr/lib/")
    stub_bin = root / "bin"
    stub_bin.mkdir(exist_ok=True)
    udevadm = stub_bin / "udevadm"
    udevadm.write_text("#!/bin/sh\nexit 0\n")
    udevadm.chmod(0o755)
    harness = "\n".join([
        "set -euo pipefail",
        'info() { echo "INFO: $*"; }',
        'warn() { echo "WARN: $*"; }',
        f"SKIP_AUDIO={skip_audio}",
        f'SERVICE_HOME="{root}/home/pi"',
        f'HAT_CARD_ID="{_hat_card_id()}"',
        "NEEDS_REBOOT=0",
        body,
        'echo "NEEDS_REBOOT=$NEEDS_REBOOT"',
    ])
    env = {**os.environ, "PATH": f"{stub_bin}:{os.environ['PATH']}"}
    return subprocess.run(["bash", "-c", harness], capture_output=True, text=True, timeout=30, env=env)


def test_hat_sink_files(tmp_path):
    r = _run(tmp_path, _extract("configure_pulseaudio_hat_sink") + "\nconfigure_pulseaudio_hat_sink")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().endswith("NEEDS_REBOOT=1")

    rule = (tmp_path / "etc/udev/rules.d/89-jarvis-hat-pulse-ignore.rules").read_text()
    assert rule == (
        'SUBSYSTEM=="sound", KERNEL=="card*", ATTR{id}=="seeed2micvoicec", ENV{PULSE_IGNORE}="1"\n'
    )
    pa = (tmp_path / "etc/pulse/default.pa.d/97-jarvis-hat-sink.pa").read_text()
    assert pa == (
        ".nofail\n"
        "load-module module-alsa-sink device=hw:CARD=seeed2micvoicec,DEV=0 "
        "sink_name=jarvis_output rate=48000 tsched=0\n"
        "set-default-sink jarvis_output\n"
    )


def test_hat_sink_idempotent(tmp_path):
    body = _extract("configure_pulseaudio_hat_sink") + "\nconfigure_pulseaudio_hat_sink"
    assert _run(tmp_path, body).returncode == 0
    second = _run(tmp_path, body)
    assert second.returncode == 0, second.stderr
    assert second.stdout.strip() == "NEEDS_REBOOT=0"


def test_hat_sink_skip_audio(tmp_path):
    r = _run(tmp_path, _extract("configure_pulseaudio_hat_sink") + "\nconfigure_pulseaudio_hat_sink", skip_audio=1)
    assert r.returncode == 0, r.stderr
    assert not (tmp_path / "etc").exists()


def _prepare_pulse_install(root: Path) -> None:
    lib = root / "usr/lib/systemd/user"
    lib.mkdir(parents=True)
    for unit in ("pulseaudio.socket", "pulseaudio.service"):
        (lib / unit).write_text("[Unit]\n")


def test_switch_removes_per_user_mask(tmp_path):
    _prepare_pulse_install(tmp_path)
    user_units = tmp_path / "home/pi/.config/systemd/user"
    user_units.mkdir(parents=True)
    for unit in ("pulseaudio.socket", "pulseaudio.service"):
        (user_units / unit).symlink_to("/dev/null")

    r = _run(tmp_path, _extract("switch_audio_to_pulseaudio") + "\nswitch_audio_to_pulseaudio")
    assert r.returncode == 0, r.stderr
    assert not (user_units / "pulseaudio.socket").is_symlink()
    assert not (user_units / "pulseaudio.service").is_symlink()
    assert "Removed a per-user mask" in r.stdout

    again = _run(tmp_path, _extract("switch_audio_to_pulseaudio") + "\nswitch_audio_to_pulseaudio")
    assert again.stdout.strip() == "NEEDS_REBOOT=0"


def test_switch_keeps_real_user_unit(tmp_path):
    _prepare_pulse_install(tmp_path)
    user_units = tmp_path / "home/pi/.config/systemd/user"
    user_units.mkdir(parents=True)
    (user_units / "pulseaudio.service").write_text("[Service]\n# a user's own override\n")

    r = _run(tmp_path, _extract("switch_audio_to_pulseaudio") + "\nswitch_audio_to_pulseaudio")
    assert r.returncode == 0, r.stderr
    assert (user_units / "pulseaudio.service").read_text().startswith("[Service]")


def test_main_runs_hat_step():
    main = _extract("main")
    assert "configure_pulseaudio_hat_sink" in main
    assert main.index("switch_audio_to_pulseaudio") < main.index("configure_pulseaudio_hat_sink")
