"""Tests for VAD / barge-in threshold helpers.

These are thin Config wrappers on top of one piece of real math
(``adaptive_silence_threshold``). The wrapper tests pin the default
values so a renamed Config key fails loud; the adaptive-silence tests
cover the bounded-clamp logic that protects against runaway thresholds
in either direction (too low → recorder never stops, too high → clips
multi-syllable commands).
"""

import pytest

from core import vad_thresholds


# ---------------------------------------------------------------------------
# Config-wrapper helpers — defaults are part of the contract; production
# rooms are tuned via settings, but a fresh node has to fire correctly out
# of the box.
# ---------------------------------------------------------------------------


class TestBargeInEnabled:
    def test_default_is_enabled(self, monkeypatch):
        # No setting → enabled. Barge-in is opt-out, not opt-in.
        monkeypatch.setattr(
            vad_thresholds.Config, "get_str",
            lambda key, default: default,
        )
        assert vad_thresholds.barge_in_enabled() is True

    @pytest.mark.parametrize("value,expected", [
        ("true",   True),
        ("True",   True),
        ("TRUE",   True),
        ("1",      True),
        ("yes",    True),
        ("Yes",    True),
        ("false",  False),
        ("False",  False),
        ("0",      False),
        ("no",     False),
        ("",       True),    # falls back to "true" via the `or "true"` guard
    ])
    def test_string_to_bool_conversion(self, monkeypatch, value, expected):
        monkeypatch.setattr(
            vad_thresholds.Config, "get_str",
            lambda key, default: value,
        )
        assert vad_thresholds.barge_in_enabled() is expected

    def test_none_falls_back_to_true(self, monkeypatch):
        # Config returning None — the `or "true"` guard kicks in.
        monkeypatch.setattr(
            vad_thresholds.Config, "get_str",
            lambda key, default: None,
        )
        assert vad_thresholds.barge_in_enabled() is True


class TestBargeInThreshold:
    def test_default(self, monkeypatch):
        # 0.04 was tuned in beta when wake-over-TTS scores peaked at
        # 0.05-0.10. Lower default = more sensitive barge-in.
        monkeypatch.setattr(
            vad_thresholds.Config, "get_float",
            lambda key, default: default,
        )
        assert vad_thresholds.barge_in_threshold() == 0.04

    def test_override_applies(self, monkeypatch):
        monkeypatch.setattr(
            vad_thresholds.Config, "get_float",
            lambda key, default: 0.10,
        )
        assert vad_thresholds.barge_in_threshold() == 0.10


class TestBargeInEnergyThreshold:
    def test_default(self, monkeypatch):
        monkeypatch.setattr(
            vad_thresholds.Config, "get_float",
            lambda key, default: default,
        )
        assert vad_thresholds.barge_in_energy_threshold() == 500.0


class TestPreWakeVadThreshold:
    def test_default(self, monkeypatch):
        # 2500 separates ambient floor (high-hundreds RMS) from real
        # speech. Per-room tuning is via settings.
        monkeypatch.setattr(
            vad_thresholds.Config, "get_float",
            lambda key, default: default,
        )
        assert vad_thresholds.pre_wake_vad_threshold() == 2500.0


# ---------------------------------------------------------------------------
# adaptive_silence_threshold — the actual math.  Median × multiplier,
# clamped to [floor, ceiling]. Returns None for "fall back to static".
# ---------------------------------------------------------------------------


class _ConfigStub:
    """Tiny stand-in for Config that returns canned values per key."""

    def __init__(
        self, *,
        auto: bool = True,
        multiplier: float = 2.0,
        floor: int = 200,
        ceiling: int = 1000,
        vad_auto: bool = True,
        vad_multiplier: float = 3.0,
        vad_min: int = 300,
        vad_max: int = 2500,
    ):
        self._bool = {
            "silence_threshold_auto": auto,
            "pre_wake_vad_auto": vad_auto,
        }
        self._float = {
            "silence_threshold_auto_multiplier": multiplier,
            "pre_wake_vad_auto_multiplier": vad_multiplier,
        }
        self._int = {
            "silence_threshold_auto_floor": floor,
            "silence_threshold_auto_ceiling": ceiling,
            "pre_wake_vad_auto_min": vad_min,
            "pre_wake_vad_auto_max": vad_max,
        }

    def get_bool(self, key, default):
        return self._bool.get(key, default)

    def get_float(self, key, default):
        return self._float.get(key, default)

    def get_int(self, key, default):
        return self._int.get(key, default)


def _patch_config(monkeypatch, stub: _ConfigStub) -> None:
    monkeypatch.setattr(vad_thresholds.Config, "get_bool", stub.get_bool)
    monkeypatch.setattr(vad_thresholds.Config, "get_float", stub.get_float)
    monkeypatch.setattr(vad_thresholds.Config, "get_int", stub.get_int)


class TestAdaptiveSilenceThreshold:

    def test_auto_disabled_returns_none(self, monkeypatch):
        _patch_config(monkeypatch, _ConfigStub(auto=False))
        # Even with perfectly valid stats, auto-off means "use static".
        assert vad_thresholds.adaptive_silence_threshold({"median": 500.0}) is None

    def test_non_dict_returns_none(self, monkeypatch):
        # Defensive — upstream callers pass whatever the bus snapshot was.
        _patch_config(monkeypatch, _ConfigStub())
        assert vad_thresholds.adaptive_silence_threshold(None) is None        # type: ignore[arg-type]
        assert vad_thresholds.adaptive_silence_threshold([]) is None          # type: ignore[arg-type]
        assert vad_thresholds.adaptive_silence_threshold("a") is None         # type: ignore[arg-type]

    def test_missing_median_returns_none(self, monkeypatch):
        _patch_config(monkeypatch, _ConfigStub())
        assert vad_thresholds.adaptive_silence_threshold({}) is None
        assert vad_thresholds.adaptive_silence_threshold({"max": 1000.0}) is None

    def test_non_numeric_median_returns_none(self, monkeypatch):
        _patch_config(monkeypatch, _ConfigStub())
        assert vad_thresholds.adaptive_silence_threshold({"median": "loud"}) is None
        assert vad_thresholds.adaptive_silence_threshold({"median": None}) is None

    def test_zero_median_returns_none(self, monkeypatch):
        # median <= 0 means the deque was effectively empty / silent.
        _patch_config(monkeypatch, _ConfigStub())
        assert vad_thresholds.adaptive_silence_threshold({"median": 0}) is None
        assert vad_thresholds.adaptive_silence_threshold({"median": -50.0}) is None

    def test_normal_case_returns_int(self, monkeypatch):
        # median 300 × 2.0 = 600; within [200, 1000] → 600 int.
        _patch_config(monkeypatch, _ConfigStub())
        result = vad_thresholds.adaptive_silence_threshold({"median": 300.0})
        assert result == 600
        assert isinstance(result, int)

    def test_below_floor_clamps_up(self, monkeypatch):
        # median 50 × 2.0 = 100, below floor 200 → return 200.
        _patch_config(monkeypatch, _ConfigStub())
        assert vad_thresholds.adaptive_silence_threshold({"median": 50.0}) == 200

    def test_above_ceiling_clamps_down(self, monkeypatch):
        # median 800 × 2.0 = 1600, above ceiling 1000 → return 1000.
        # Critical: this is the protection against clipping multi-syllable
        # commands mid-sentence in a loud room (kitchen with fan/TV).
        _patch_config(monkeypatch, _ConfigStub())
        assert vad_thresholds.adaptive_silence_threshold({"median": 800.0}) == 1000

    def test_exact_floor_value_returned(self, monkeypatch):
        # median × multiplier == floor exactly.
        _patch_config(monkeypatch, _ConfigStub())
        assert vad_thresholds.adaptive_silence_threshold({"median": 100.0}) == 200

    def test_exact_ceiling_value_returned(self, monkeypatch):
        _patch_config(monkeypatch, _ConfigStub())
        assert vad_thresholds.adaptive_silence_threshold({"median": 500.0}) == 1000

    def test_custom_multiplier(self, monkeypatch):
        # Operator widens the multiplier — 300 × 3.0 = 900, still under
        # default ceiling of 1000.
        _patch_config(monkeypatch, _ConfigStub(multiplier=3.0))
        assert vad_thresholds.adaptive_silence_threshold({"median": 300.0}) == 900

    def test_custom_floor_and_ceiling(self, monkeypatch):
        # Wider window for a tuned room: floor=100, ceiling=2000.
        _patch_config(monkeypatch, _ConfigStub(floor=100, ceiling=2000))
        assert vad_thresholds.adaptive_silence_threshold({"median": 30.0}) == 100
        assert vad_thresholds.adaptive_silence_threshold({"median": 1500.0}) == 2000

    def test_int_median_works(self, monkeypatch):
        # The signature is `dict[str, float]` but ints are valid floats.
        _patch_config(monkeypatch, _ConfigStub())
        assert vad_thresholds.adaptive_silence_threshold({"median": 300}) == 600


# ---------------------------------------------------------------------------
# auto_pre_wake_vad_threshold — ambient-floor calibration for the pre-wake
# speech signal. Median × multiplier, clamped to [min, max]. Returns None
# for "use the static threshold". Default ON: the static 2500 default sat
# above real speech RMS on the prod mic and reported 0.00 for 14 days.
# ---------------------------------------------------------------------------


class TestAutoPreWakeVadThreshold:

    def test_auto_disabled_returns_none(self, monkeypatch):
        _patch_config(monkeypatch, _ConfigStub(vad_auto=False))
        assert vad_thresholds.auto_pre_wake_vad_threshold(
            {"median": 500.0},
        ) is None

    def test_default_is_enabled(self, monkeypatch):
        # No setting → auto ON. Unlike silence auto-tune (which refines
        # a working static value) this replaces a provably dead default,
        # so opting OUT is the setting.
        stub = _ConfigStub()

        def get_bool(key, default):
            return default  # simulate unset settings DB

        monkeypatch.setattr(vad_thresholds.Config, "get_bool", get_bool)
        monkeypatch.setattr(vad_thresholds.Config, "get_float", stub.get_float)
        monkeypatch.setattr(vad_thresholds.Config, "get_int", stub.get_int)
        assert vad_thresholds.auto_pre_wake_vad_threshold(
            {"median": 400.0},
        ) == 1200.0

    def test_non_dict_returns_none(self, monkeypatch):
        _patch_config(monkeypatch, _ConfigStub())
        assert vad_thresholds.auto_pre_wake_vad_threshold(None) is None       # type: ignore[arg-type]
        assert vad_thresholds.auto_pre_wake_vad_threshold([]) is None         # type: ignore[arg-type]

    def test_missing_or_bad_median_returns_none(self, monkeypatch):
        _patch_config(monkeypatch, _ConfigStub())
        assert vad_thresholds.auto_pre_wake_vad_threshold({}) is None
        assert vad_thresholds.auto_pre_wake_vad_threshold(
            {"median": "loud"},
        ) is None
        assert vad_thresholds.auto_pre_wake_vad_threshold(
            {"median": 0},
        ) is None
        assert vad_thresholds.auto_pre_wake_vad_threshold(
            {"median": -10.0},
        ) is None

    def test_normal_case_median_times_multiplier(self, monkeypatch):
        # median 400 × 3.0 = 1200; within [300, 2500].
        _patch_config(monkeypatch, _ConfigStub())
        assert vad_thresholds.auto_pre_wake_vad_threshold(
            {"median": 400.0},
        ) == 1200.0

    def test_below_min_clamps_up(self, monkeypatch):
        # Dead-quiet room: median 50 × 3.0 = 150, below min 300 → 300.
        # Below the min, ordinary quiet-room flutter counts as speech.
        _patch_config(monkeypatch, _ConfigStub())
        assert vad_thresholds.auto_pre_wake_vad_threshold(
            {"median": 50.0},
        ) == 300.0

    def test_above_max_clamps_down(self, monkeypatch):
        # Loud room: median 2000 × 3.0 = 6000 → clamp to 2500 (the old
        # static default). Auto can never be MORE deaf than the value it
        # replaces.
        _patch_config(monkeypatch, _ConfigStub())
        assert vad_thresholds.auto_pre_wake_vad_threshold(
            {"median": 2000.0},
        ) == 2500.0

    def test_custom_multiplier(self, monkeypatch):
        _patch_config(monkeypatch, _ConfigStub(vad_multiplier=2.0))
        assert vad_thresholds.auto_pre_wake_vad_threshold(
            {"median": 400.0},
        ) == 800.0

    def test_custom_clamp_band(self, monkeypatch):
        _patch_config(monkeypatch, _ConfigStub(vad_min=100, vad_max=900))
        assert vad_thresholds.auto_pre_wake_vad_threshold(
            {"median": 20.0},
        ) == 100.0
        assert vad_thresholds.auto_pre_wake_vad_threshold(
            {"median": 5000.0},
        ) == 900.0
