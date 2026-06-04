"""Tests for the wake-threshold auto-calibrator.

Each legitimate wake score is one data point telling the calibrator
"this is what 'hey jarvis' sounds like in this room." After enough
samples, the threshold is set just under the lowest-scoring real wake
— meaning real wakes always fire on the first attempt without enlarging
the false-positive window for ambient noise.

State is module-level (the calibrator is a singleton per process), so
each test resets the deque and the load-once flag via an autouse fixture.
The persistence file is redirected to a tmp_path to keep the suite
hermetic.
"""

import json

import pytest

from core import wake_calibration


@pytest.fixture(autouse=True)
def _isolate_calibration_state(monkeypatch, tmp_path):
    """Per-test: empty deque, unloaded flag, file redirected to tmp_path."""
    monkeypatch.setattr(
        wake_calibration,
        "_WAKE_SCORE_HISTORY_FILE",
        tmp_path / "wake_scores.json",
    )
    wake_calibration._wake_score_history.clear()
    wake_calibration._wake_score_history_loaded = False
    yield


# ---------------------------------------------------------------------------
# load_wake_score_history — idempotent disk read with validation.
# ---------------------------------------------------------------------------


class TestLoadWakeScoreHistory:

    def test_missing_file_is_noop(self):
        # No file → no error, deque stays empty, loaded flag flips.
        wake_calibration.load_wake_score_history()
        assert list(wake_calibration._wake_score_history) == []
        assert wake_calibration._wake_score_history_loaded is True

    def test_valid_list_loaded(self):
        wake_calibration._WAKE_SCORE_HISTORY_FILE.write_text(
            json.dumps([0.2, 0.35, 0.42])
        )
        wake_calibration.load_wake_score_history()
        assert list(wake_calibration._wake_score_history) == [0.2, 0.35, 0.42]

    def test_idempotent(self):
        wake_calibration._WAKE_SCORE_HISTORY_FILE.write_text(json.dumps([0.5]))
        wake_calibration.load_wake_score_history()
        # Tampering with the file after the first load must not re-import —
        # caller relies on the in-memory deque being authoritative once loaded.
        wake_calibration._WAKE_SCORE_HISTORY_FILE.write_text(
            json.dumps([0.9, 0.95])
        )
        wake_calibration.load_wake_score_history()
        assert list(wake_calibration._wake_score_history) == [0.5]

    def test_invalid_json_is_silently_caught(self):
        wake_calibration._WAKE_SCORE_HISTORY_FILE.write_text("not valid json{")
        # Must not raise — calibration is best-effort.
        wake_calibration.load_wake_score_history()
        assert list(wake_calibration._wake_score_history) == []
        assert wake_calibration._wake_score_history_loaded is True

    def test_non_list_payload_ignored(self):
        wake_calibration._WAKE_SCORE_HISTORY_FILE.write_text(
            json.dumps({"history": [0.5]})  # dict, not list
        )
        wake_calibration.load_wake_score_history()
        assert list(wake_calibration._wake_score_history) == []

    def test_out_of_range_values_filtered(self):
        # Only [0.0, 1.0] is a legal OWW score range.
        wake_calibration._WAKE_SCORE_HISTORY_FILE.write_text(
            json.dumps([-0.1, 0.0, 0.5, 1.0, 1.5, 2.0])
        )
        wake_calibration.load_wake_score_history()
        assert list(wake_calibration._wake_score_history) == [0.0, 0.5, 1.0]

    def test_non_numeric_values_filtered(self):
        wake_calibration._WAKE_SCORE_HISTORY_FILE.write_text(
            json.dumps([0.3, "bogus", None, 0.5])
        )
        wake_calibration.load_wake_score_history()
        assert list(wake_calibration._wake_score_history) == [0.3, 0.5]

    def test_oversize_history_truncated_to_max(self):
        # _WAKE_SCORE_HISTORY_MAX = 20 — older entries dropped on load.
        big = [round(i / 100, 2) for i in range(50)]  # 50 entries
        wake_calibration._WAKE_SCORE_HISTORY_FILE.write_text(json.dumps(big))
        wake_calibration.load_wake_score_history()
        loaded = list(wake_calibration._wake_score_history)
        assert len(loaded) == wake_calibration._WAKE_SCORE_HISTORY_MAX
        # Most recent 20 = entries 30..49
        assert loaded == big[-wake_calibration._WAKE_SCORE_HISTORY_MAX:]


# ---------------------------------------------------------------------------
# record_legitimate_wake_score — append + persist with range guard.
# ---------------------------------------------------------------------------


class TestRecordLegitimateWakeScore:

    def test_valid_score_appended_and_persisted(self):
        wake_calibration.record_legitimate_wake_score(0.42)
        assert list(wake_calibration._wake_score_history) == [0.42]
        # Persisted to disk.
        data = json.loads(
            wake_calibration._WAKE_SCORE_HISTORY_FILE.read_text()
        )
        assert data == [0.42]

    @pytest.mark.parametrize("bad_score", [-0.01, -1.0, 1.01, 2.0])
    def test_out_of_range_score_is_noop(self, bad_score):
        wake_calibration.record_legitimate_wake_score(bad_score)
        assert list(wake_calibration._wake_score_history) == []
        # No file written either.
        assert not wake_calibration._WAKE_SCORE_HISTORY_FILE.exists()

    def test_boundary_values_accepted(self):
        # Exactly 0.0 and 1.0 are valid scores.
        wake_calibration.record_legitimate_wake_score(0.0)
        wake_calibration.record_legitimate_wake_score(1.0)
        assert list(wake_calibration._wake_score_history) == [0.0, 1.0]

    def test_persistence_failure_does_not_crash(self, monkeypatch):
        # Simulate OSError on write_text. The score should still land in
        # the deque (in-memory record is independent of disk).
        original_write = wake_calibration._WAKE_SCORE_HISTORY_FILE.write_text

        def explode(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(
            type(wake_calibration._WAKE_SCORE_HISTORY_FILE),
            "write_text", explode,
        )
        wake_calibration.record_legitimate_wake_score(0.5)
        assert list(wake_calibration._wake_score_history) == [0.5]
        # Restore (the monkeypatch fixture handles teardown anyway).
        _ = original_write

    def test_deque_maxlen_bounded(self):
        # Add more than max — oldest drop off the left.
        for i in range(wake_calibration._WAKE_SCORE_HISTORY_MAX + 5):
            wake_calibration.record_legitimate_wake_score(round(i / 100, 2))
        assert len(wake_calibration._wake_score_history) == wake_calibration._WAKE_SCORE_HISTORY_MAX


# ---------------------------------------------------------------------------
# auto_calibrated_wake_threshold — p20 × 0.85, clamped to [0.10, 0.50].
# ---------------------------------------------------------------------------


class TestAutoCalibratedWakeThreshold:

    def test_below_min_samples_returns_fallback(self):
        # 4 samples, MIN_SAMPLES = 5 → fallback.
        for s in [0.40, 0.42, 0.45, 0.48]:
            wake_calibration._wake_score_history.append(s)
        # Mark loaded so the load call doesn't blow away the deque.
        wake_calibration._wake_score_history_loaded = True
        assert wake_calibration.auto_calibrated_wake_threshold(0.4) == 0.4

    def test_empty_returns_fallback(self):
        wake_calibration._wake_score_history_loaded = True
        assert wake_calibration.auto_calibrated_wake_threshold(0.4) == 0.4

    def test_calibrated_p20_with_5_samples(self):
        # 5 samples sorted: idx = int(5 * 0.2) = 1, so p20 = samples[1].
        scores = [0.30, 0.40, 0.50, 0.60, 0.70]
        for s in scores:
            wake_calibration._wake_score_history.append(s)
        wake_calibration._wake_score_history_loaded = True
        # p20 = 0.40, calibrated = 0.40 * 0.85 = 0.34 (within clamp)
        result = wake_calibration.auto_calibrated_wake_threshold(0.9)
        assert result == pytest.approx(0.34)

    def test_calibrated_value_clamped_to_floor(self):
        # All low scores → calibrated below 0.10 floor.
        for s in [0.05] * 10:
            wake_calibration._wake_score_history.append(s)
        wake_calibration._wake_score_history_loaded = True
        # p20 = 0.05, calibrated = 0.0425 → clamps to 0.10
        result = wake_calibration.auto_calibrated_wake_threshold(0.4)
        assert result == 0.10

    def test_calibrated_value_clamped_to_ceiling(self):
        # All high scores → calibrated above 0.50 ceiling.
        for s in [0.95] * 10:
            wake_calibration._wake_score_history.append(s)
        wake_calibration._wake_score_history_loaded = True
        # p20 = 0.95, calibrated = 0.8075 → clamps to 0.50
        result = wake_calibration.auto_calibrated_wake_threshold(0.4)
        assert result == 0.50

    def test_load_called_on_first_invocation(self, tmp_path):
        # File on disk; auto_calibrated must trigger the load.
        wake_calibration._WAKE_SCORE_HISTORY_FILE.write_text(
            json.dumps([0.30, 0.40, 0.50, 0.60, 0.70])
        )
        # Loaded flag is False (the fixture cleared it).
        assert wake_calibration._wake_score_history_loaded is False
        result = wake_calibration.auto_calibrated_wake_threshold(0.9)
        assert wake_calibration._wake_score_history_loaded is True
        # p20 = sorted[1] = 0.40, * 0.85 = 0.34
        assert result == pytest.approx(0.34)

    def test_calibrated_threshold_returns_within_bounds(self):
        # Property check: regardless of input, output is in [0.10, 0.50].
        for s in [0.01, 0.10, 0.25, 0.50, 0.75, 0.99]:
            wake_calibration._wake_score_history.append(s)
        wake_calibration._wake_score_history_loaded = True
        result = wake_calibration.auto_calibrated_wake_threshold(0.4)
        assert 0.10 <= result <= 0.50
