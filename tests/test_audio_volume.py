"""Unit tests for utils.audio_volume.

We mock the underlying ``_run`` helper so these run on any platform —
the production code shells out to ``pactl`` and ``aplay``, which aren't
present on dev machines. The tests verify the right shell calls happen,
in the right order, with the right arguments.
"""

import json
from unittest.mock import MagicMock

import pytest

from utils import audio_volume


def _ok(stdout: str = "") -> MagicMock:
    """Mock CompletedProcess for a successful command."""
    return MagicMock(returncode=0, stdout=stdout, stderr="")


def _fail(stderr: str = "boom") -> MagicMock:
    return MagicMock(returncode=1, stdout="", stderr=stderr)


# Sample `pactl list short sinks` output with one local + one BT sink.
PACTL_SHORT_SINKS = (
    "0\talsa_output.platform-soc_sound.stereo-fallback\tmodule-alsa-card.c\ts16le 2ch 48000Hz\tSUSPENDED\n"
    "1\tbluez_sink.AA_BB_CC_DD_EE_FF.a2dp_sink\tmodule-bluez5-device.c\ts16le 2ch 44100Hz\tIDLE\n"
)

PACTL_GET_VOLUME_50 = (
    "Volume: front-left: 32768 /  50% / -18.00 dB,"
    "   front-right: 32768 /  50% / -18.00 dB\n"
    "        balance 0.00\n"
)

PACTL_GET_MUTE_NO = "Mute: no\n"
PACTL_GET_MUTE_YES = "Mute: yes\n"

# `aplay -l` output on a Pi with the ReSpeaker HAT installed.
APLAY_L_OUTPUT = (
    "**** List of PLAYBACK Hardware Devices ****\n"
    "card 0: vc4hdmi [vc4-hdmi], device 0: MAI PCM i2s-hifi-0 [MAI PCM i2s-hifi-0]\n"
    "  Subdevices: 1/1\n"
    "  Subdevice #0: subdevice #0\n"
    "card 1: seeed2micvoicec [seeed2micvoicec], device 0: bcm2835-i2s-tlv320aic3x-hifi tlv320aic3x-hifi-0\n"
    "  Subdevices: 1/1\n"
    "  Subdevice #0: subdevice #0\n"
)


class _RunStub:
    """Records every command issued and returns canned responses."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str], timeout: float = 2.0) -> MagicMock | None:
        self.calls.append(list(cmd))
        if cmd[:2] == ["aplay", "-l"]:
            return _ok(APLAY_L_OUTPUT)
        if cmd[:2] == ["pactl", "list"]:
            return _ok(PACTL_SHORT_SINKS)
        if cmd[:2] == ["pactl", "get-sink-volume"]:
            return _ok(PACTL_GET_VOLUME_50)
        if cmd[:2] == ["pactl", "get-sink-mute"]:
            return _ok(PACTL_GET_MUTE_NO)
        if cmd[:2] in (["pactl", "set-sink-volume"], ["pactl", "set-sink-mute"]):
            return _ok()
        return _fail("unknown command in stub")


@pytest.fixture(autouse=True)
def isolate_config(tmp_path, monkeypatch):
    """Point CONFIG_PATH at a per-test tmp file so set/get don't touch the
    real working-tree config.json.
    """
    config_file = tmp_path / "config.json"
    monkeypatch.setenv("CONFIG_PATH", str(config_file))
    return config_file


@pytest.fixture
def stub(monkeypatch) -> _RunStub:
    s = _RunStub()
    monkeypatch.setattr(audio_volume, "_run", s)
    return s


class TestGetAudioCard:
    def test_skips_hdmi_returns_seeed(self, stub):
        # First non-HDMI card on a real HAT-equipped Pi is the seeed card.
        assert audio_volume.get_audio_card() == "seeed2micvoicec"

    def test_returns_none_when_only_hdmi(self, monkeypatch):
        only_hdmi = (
            "**** List of PLAYBACK Hardware Devices ****\n"
            "card 0: vc4hdmi [vc4-hdmi], device 0: MAI PCM i2s-hifi-0\n"
        )
        monkeypatch.setattr(audio_volume, "_run", lambda *a, **k: _ok(only_hdmi))
        assert audio_volume.get_audio_card() is None

    def test_returns_none_when_aplay_missing(self, monkeypatch):
        monkeypatch.setattr(audio_volume, "_run", lambda *a, **k: None)
        assert audio_volume.get_audio_card() is None

    def test_returns_none_when_aplay_fails(self, monkeypatch):
        monkeypatch.setattr(audio_volume, "_run", lambda *a, **k: _fail())
        assert audio_volume.get_audio_card() is None


class TestGetVolumePercent:
    def test_prefers_persisted_value(self, stub, isolate_config):
        # Persisted value is the source of truth — bypasses any drift
        # through pactl's integer percent rounding.
        isolate_config.write_text(json.dumps({"volume_percent": 70}))
        assert audio_volume.get_volume_percent() == 70

    def test_falls_back_to_pactl_when_no_persisted_value(self, stub):
        # No volume_percent in config.json yet (fresh install / first
        # boot before the user has touched the slider).
        assert audio_volume.get_volume_percent() == 50

    def test_returns_none_when_pactl_missing_and_no_persisted(self, monkeypatch):
        monkeypatch.setattr(audio_volume, "_run", lambda *a, **k: None)
        assert audio_volume.get_volume_percent() is None

    def test_returns_none_on_nonzero_exit_no_persisted(self, monkeypatch):
        monkeypatch.setattr(audio_volume, "_run", lambda *a, **k: _fail())
        assert audio_volume.get_volume_percent() is None

    def test_picks_max_channel_volume(self, monkeypatch, isolate_config):
        # Asymmetric channels — pick the louder one so the displayed
        # value reflects what the user actually hears.
        asym = (
            "Volume: front-left: 19660 /  30% / -27.06 dB,"
            "   front-right: 32768 /  50% / -18.00 dB\n"
        )

        def stub(cmd, timeout=2.0):
            if cmd[:2] == ["pactl", "get-sink-volume"]:
                return _ok(asym)
            return _fail()

        monkeypatch.setattr(audio_volume, "_run", stub)
        assert audio_volume.get_volume_percent() == 50


class TestIsMuted:
    def test_unmuted(self, stub):
        assert audio_volume.is_muted() is False

    def test_muted(self, monkeypatch):
        monkeypatch.setattr(
            audio_volume, "_run",
            lambda cmd, timeout=2.0: _ok(PACTL_GET_MUTE_YES),
        )
        assert audio_volume.is_muted() is True

    def test_unavailable(self, monkeypatch):
        monkeypatch.setattr(audio_volume, "_run", lambda *a, **k: None)
        assert audio_volume.is_muted() is None


class TestSetVolumePercent:
    def test_hits_default_sink_and_every_bluez_sink(self, stub, isolate_config):
        assert audio_volume.set_volume_percent(25) is True

        # Persisted so subsequent get_volume_percent returns 25 exactly.
        assert json.loads(isolate_config.read_text())["volume_percent"] == 25

        # PulseAudio default sink — pass-through, no taper
        assert any(
            c[:3] == ["pactl", "set-sink-volume", "@DEFAULT_SINK@"] and c[3] == "25%"
            for c in stub.calls
        ), f"missing default sink set 25%, calls={stub.calls}"

        # BT sink discovered from `pactl list short sinks`
        assert any(
            c[:2] == ["pactl", "set-sink-volume"]
            and c[2].startswith("bluez_sink.")
            and c[3] == "25%"
            for c in stub.calls
        ), f"missing bluez sink set 25%, calls={stub.calls}"

    def test_max(self, stub):
        audio_volume.set_volume_percent(100)
        assert any(
            c[:3] == ["pactl", "set-sink-volume", "@DEFAULT_SINK@"] and c[3] == "100%"
            for c in stub.calls
        )

    def test_zero(self, stub):
        audio_volume.set_volume_percent(0)
        assert any(
            c[:3] == ["pactl", "set-sink-volume", "@DEFAULT_SINK@"] and c[3] == "0%"
            for c in stub.calls
        )

    def test_clamps_above_100(self, stub):
        audio_volume.set_volume_percent(250)
        assert any(
            c[:3] == ["pactl", "set-sink-volume", "@DEFAULT_SINK@"] and c[3] == "100%"
            for c in stub.calls
        )

    def test_clamps_below_zero(self, stub):
        audio_volume.set_volume_percent(-50)
        assert any(
            c[:3] == ["pactl", "set-sink-volume", "@DEFAULT_SINK@"] and c[3] == "0%"
            for c in stub.calls
        )

    def test_falls_back_to_listed_sinks_when_default_fails(self, monkeypatch, isolate_config):
        # Fresh-install case (issue surfaced in beta May 2026): PA's
        # @DEFAULT_SINK@ resolves to HDMI but the user's speaker is on
        # another sink. We enumerate non-monitor sinks and apply to
        # each, returning True when any succeed.
        def stub(cmd, timeout=2.0):
            if cmd[:3] == ["pactl", "set-sink-volume", "@DEFAULT_SINK@"]:
                return _fail()
            if cmd[:2] == ["pactl", "list"]:
                return _ok(PACTL_SHORT_SINKS)
            return _ok()

        monkeypatch.setattr(audio_volume, "_run", stub)
        assert audio_volume.set_volume_percent(50) is True

    def test_returns_false_when_every_sink_fails(self, monkeypatch):
        # Every pactl set-sink-volume fails (PA daemon down). Volume is
        # still persisted to config.json — but the live apply reports
        # False so callers can log/diagnose.
        def stub(cmd, timeout=2.0):
            if cmd[:2] == ["pactl", "set-sink-volume"]:
                return _fail()
            if cmd[:2] == ["pactl", "list"]:
                return _ok(PACTL_SHORT_SINKS)
            return _ok()

        monkeypatch.setattr(audio_volume, "_run", stub)
        assert audio_volume.set_volume_percent(50) is False

    def test_persists_even_when_pactl_fails(self, monkeypatch, isolate_config):
        # Critical for the fresh-install / boot-time case: PA may not
        # have enumerated its sinks yet when the mobile-app push lands.
        # config.json must still record the user's wish so the next
        # boot picks it up.
        def stub(cmd, timeout=2.0):
            if cmd[:2] == ["pactl", "set-sink-volume"]:
                return _fail()
            return _ok(PACTL_SHORT_SINKS if cmd[:2] == ["pactl", "list"] else "")

        monkeypatch.setattr(audio_volume, "_run", stub)
        audio_volume.set_volume_percent(50)
        assert json.loads(isolate_config.read_text())["volume_percent"] == 50


class TestAdjustVolumePercent:
    def test_increment_uses_persisted_value(self, stub, isolate_config):
        isolate_config.write_text(json.dumps({"volume_percent": 70}))
        assert audio_volume.adjust_volume_percent(10) == 80

    def test_increment_falls_back_to_pactl(self, stub):
        # No persisted value; pactl reports 50% → +10 = 60.
        assert audio_volume.adjust_volume_percent(10) == 60

    def test_decrement_clamped(self, stub):
        assert audio_volume.adjust_volume_percent(-100) == 0

    def test_increment_clamped(self, stub):
        assert audio_volume.adjust_volume_percent(100) == 100

    def test_returns_none_when_unreadable(self, monkeypatch):
        monkeypatch.setattr(audio_volume, "_run", lambda *a, **k: None)
        assert audio_volume.adjust_volume_percent(10) is None


class TestSetMuted:
    def test_mute_calls_every_sink(self, stub):
        assert audio_volume.set_muted(True) is True

        assert any(
            c[:3] == ["pactl", "set-sink-mute", "@DEFAULT_SINK@"] and c[3] == "1"
            for c in stub.calls
        )
        assert any(
            c[:2] == ["pactl", "set-sink-mute"]
            and c[2].startswith("bluez_sink.")
            and c[3] == "1"
            for c in stub.calls
        )

    def test_unmute(self, stub):
        audio_volume.set_muted(False)
        assert any(
            c[:3] == ["pactl", "set-sink-mute", "@DEFAULT_SINK@"] and c[3] == "0"
            for c in stub.calls
        )

    def test_falls_back_to_listed_sinks_when_default_fails(self, monkeypatch):
        # Same fresh-install resilience as set_volume_percent.
        def stub(cmd, timeout=2.0):
            if cmd[:3] == ["pactl", "set-sink-mute", "@DEFAULT_SINK@"]:
                return _fail()
            if cmd[:2] == ["pactl", "list"]:
                return _ok(PACTL_SHORT_SINKS)
            return _ok()

        monkeypatch.setattr(audio_volume, "_run", stub)
        assert audio_volume.set_muted(True) is True

    def test_returns_false_when_every_sink_fails(self, monkeypatch):
        def stub(cmd, timeout=2.0):
            if cmd[:2] == ["pactl", "set-sink-mute"]:
                return _fail()
            if cmd[:2] == ["pactl", "list"]:
                return _ok(PACTL_SHORT_SINKS)
            return _ok()

        monkeypatch.setattr(audio_volume, "_run", stub)
        assert audio_volume.set_muted(True) is False


class TestPersistVolumePercent:
    def test_writes_to_config_path(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({"node_id": "test-node"}))
        monkeypatch.setenv("CONFIG_PATH", str(config_file))

        assert audio_volume.persist_volume_percent(45) is True

        written = json.loads(config_file.read_text())
        assert written["volume_percent"] == 45
        assert written["node_id"] == "test-node"  # other keys preserved

    def test_creates_config_when_missing(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.json"
        monkeypatch.setenv("CONFIG_PATH", str(config_file))
        assert audio_volume.persist_volume_percent(70) is True
        assert json.loads(config_file.read_text())["volume_percent"] == 70

    def test_clamps_value(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.json"
        monkeypatch.setenv("CONFIG_PATH", str(config_file))
        audio_volume.persist_volume_percent(150)
        assert json.loads(config_file.read_text())["volume_percent"] == 100


class TestListBluezSinks:
    def test_filters_only_bluez(self, stub):
        assert audio_volume._list_bluez_sinks() == [
            "bluez_sink.AA_BB_CC_DD_EE_FF.a2dp_sink"
        ]

    def test_empty_when_pactl_missing(self, monkeypatch):
        monkeypatch.setattr(audio_volume, "_run", lambda *a, **k: None)
        assert audio_volume._list_bluez_sinks() == []


# `amixer cget` output for the ADC HPF Cut-off control. Format mirrors
# what the seeed2micvoicec card returns; lines are intentionally indented
# the way `amixer` outputs them so the parser regex is exercised.
_HPF_CGET_TEMPLATE = (
    "numid=36,iface=MIXER,name='ADC HPF Cut-off'\n"
    "  ; type=ENUMERATED,access=rw------,values=2,items=4\n"
    "  ; Item #0 'Disabled'\n"
    "  ; Item #1 '0.0045xFs'\n"
    "  ; Item #2 '0.0125xFs'\n"
    "  ; Item #3 '0.025xFs'\n"
    "  : values={value},{value}\n"
)


class TestEnsureAdcHpfEnabled:
    """Codec self-heal for the TLV320AIC3104 ADC high-pass filter.

    Without this, every recording carries a DC pedestal that buries
    the AC voice signal and tricks Whisper into transcribing as music
    — see the May-2026 kitchen-node beta blocker.
    """

    def test_enables_when_disabled(self, monkeypatch):
        calls: list[list[str]] = []

        def stub(cmd, timeout=2.0):
            calls.append(list(cmd))
            if cmd[:2] == ["aplay", "-l"]:
                return _ok(APLAY_L_OUTPUT)
            if cmd[:3] == ["amixer", "-c", "seeed2micvoicec"] and cmd[3] == "cget":
                return _ok(_HPF_CGET_TEMPLATE.format(value=0))
            if cmd[:3] == ["amixer", "-c", "seeed2micvoicec"] and cmd[3] == "cset":
                return _ok()
            return _fail()

        monkeypatch.setattr(audio_volume, "_run", stub)
        audio_volume.ensure_adc_hpf_enabled()
        # The self-heal must have issued a cset to flip the HPF from
        # item 0 (Disabled) to item 1 (0.0045xFs).
        cset_calls = [c for c in calls if len(c) > 3 and c[3] == "cset"]
        assert len(cset_calls) == 1, f"expected one cset, got {cset_calls}"
        assert cset_calls[0][-1] == "1"
        assert "name=ADC HPF Cut-off" in cset_calls[0]

    def test_no_op_when_already_enabled(self, monkeypatch):
        calls: list[list[str]] = []

        def stub(cmd, timeout=2.0):
            calls.append(list(cmd))
            if cmd[:2] == ["aplay", "-l"]:
                return _ok(APLAY_L_OUTPUT)
            if cmd[:3] == ["amixer", "-c", "seeed2micvoicec"] and cmd[3] == "cget":
                return _ok(_HPF_CGET_TEMPLATE.format(value=1))
            return _fail()

        monkeypatch.setattr(audio_volume, "_run", stub)
        audio_volume.ensure_adc_hpf_enabled()
        cset_calls = [c for c in calls if len(c) > 3 and c[3] == "cset"]
        assert cset_calls == [], "must not downgrade an already-enabled HPF"

    def test_no_op_when_user_picked_higher_cutoff(self, monkeypatch):
        # A user (or future install.sh) might pick item 2 (0.0125xFs)
        # for a noisier room. We should not downgrade them.
        def stub(cmd, timeout=2.0):
            if cmd[:2] == ["aplay", "-l"]:
                return _ok(APLAY_L_OUTPUT)
            if cmd[:3] == ["amixer", "-c", "seeed2micvoicec"] and cmd[3] == "cget":
                return _ok(_HPF_CGET_TEMPLATE.format(value=2))
            return _fail()

        calls: list[list[str]] = []
        monkeypatch.setattr(
            audio_volume, "_run",
            lambda cmd, timeout=2.0: (calls.append(list(cmd)) or stub(cmd, timeout)),
        )
        audio_volume.ensure_adc_hpf_enabled()
        cset_calls = [c for c in calls if len(c) > 3 and c[3] == "cset"]
        assert cset_calls == []

    def test_no_op_when_no_seeed_card(self, monkeypatch):
        # Plain Pi without the ReSpeaker HAT (or macOS dev box) —
        # aplay only reports the HDMI card. self-heal should skip silently.
        only_hdmi = (
            "**** List of PLAYBACK Hardware Devices ****\n"
            "card 0: vc4hdmi [vc4-hdmi], device 0: MAI PCM\n"
        )
        calls: list[list[str]] = []

        def stub(cmd, timeout=2.0):
            calls.append(list(cmd))
            if cmd[:2] == ["aplay", "-l"]:
                return _ok(only_hdmi)
            return _fail()

        monkeypatch.setattr(audio_volume, "_run", stub)
        audio_volume.ensure_adc_hpf_enabled()
        assert not any(c[:1] == ["amixer"] for c in calls)

    def test_unreadable_cget_logs_and_returns(self, monkeypatch):
        # If `amixer cget` fails (codec busy, missing control on a
        # newer kernel), we log + return — don't crash, don't blindly
        # set a control we couldn't read.
        def stub(cmd, timeout=2.0):
            if cmd[:2] == ["aplay", "-l"]:
                return _ok(APLAY_L_OUTPUT)
            if cmd[:3] == ["amixer", "-c", "seeed2micvoicec"] and cmd[3] == "cget":
                return _fail("unknown control")
            return _ok()

        monkeypatch.setattr(audio_volume, "_run", stub)
        # Must not raise.
        audio_volume.ensure_adc_hpf_enabled()
