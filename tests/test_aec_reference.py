"""Tests for core.aec_reference.ReferenceReader.

The pull()/ring-buffer logic is the load-bearing part to test —
spawning parec is exercised separately as a dev-host smoke test
(see /home/pi/aec-test/ on the dev node). Tests here use
``push_for_test()`` to inject samples deterministically.
"""

from __future__ import annotations

import time

import numpy as np
import pytest

from core.aec_reference import ReferenceReader


def _reader(**kw) -> ReferenceReader:
    defaults = dict(rate=16000, buffer_secs=1.0, stale_threshold_ms=200.0)
    defaults.update(kw)
    return ReferenceReader(**defaults)


# --- pull() before any data --------------------------------------------


class TestNoData:
    def test_pull_returns_none_before_any_writes(self) -> None:
        r = _reader()
        assert r.pull(n_samples=160) is None

    def test_pull_returns_none_when_stale(self) -> None:
        r = _reader(stale_threshold_ms=10.0)
        r.push_for_test(np.ones(160, dtype=np.int16) * 1234)
        # Wait beyond the stale threshold.
        time.sleep(0.03)
        assert r.pull(n_samples=160) is None


# --- Basic round-trip --------------------------------------------------


class TestRoundTrip:
    def test_push_then_pull_returns_recent_samples(self) -> None:
        r = _reader()
        data = np.arange(160, dtype=np.int16) + 1
        r.push_for_test(data)
        out = r.pull(n_samples=160)
        assert out is not None
        np.testing.assert_array_equal(out, data)

    def test_pull_returns_most_recent_when_smaller_than_written(self) -> None:
        r = _reader()
        chunk1 = np.full(160, 11, dtype=np.int16)
        chunk2 = np.full(160, 22, dtype=np.int16)
        r.push_for_test(chunk1)
        r.push_for_test(chunk2)
        out = r.pull(n_samples=160)
        assert out is not None
        np.testing.assert_array_equal(out, chunk2)

    def test_dtype_is_int16(self) -> None:
        r = _reader()
        r.push_for_test(np.ones(160, dtype=np.int16) * 7)
        out = r.pull(n_samples=160)
        assert out is not None and out.dtype == np.int16


# --- Wrap-around -------------------------------------------------------


class TestWrapAround:
    def test_writes_exceeding_capacity_wrap(self) -> None:
        """Push 1.5x buffer capacity; the most recent buffer-worth of
        samples should be intact and pullable.
        """
        r = _reader(buffer_secs=0.01)  # 160 samples at 16k
        capacity = r.buffer_capacity_samples
        big = np.arange(capacity * 3 // 2, dtype=np.int16)
        r.push_for_test(big)
        out = r.pull(n_samples=capacity)
        assert out is not None
        np.testing.assert_array_equal(out, big[-capacity:])

    def test_pull_with_delay_into_pre_wrap_region(self) -> None:
        r = _reader(buffer_secs=0.02)  # 320 samples at 16k
        marker = np.arange(320, dtype=np.int16) + 1000
        r.push_for_test(marker)
        more = np.arange(160, dtype=np.int16) + 5000
        r.push_for_test(more)
        # Now write_idx is at 160 (wrapped). Pull a frame from
        # delay_samples=160 in the past — should be the *end* of marker.
        out = r.pull(n_samples=160, delay_samples=160)
        assert out is not None
        np.testing.assert_array_equal(out, marker[-160:])


# --- Delay argument ----------------------------------------------------


class TestDelay:
    def test_pull_with_delay_returns_older_samples(self) -> None:
        r = _reader()
        chunk1 = np.full(160, 11, dtype=np.int16)
        chunk2 = np.full(160, 22, dtype=np.int16)
        r.push_for_test(chunk1)
        r.push_for_test(chunk2)
        # No delay → newest (chunk2). delay=160 → previous (chunk1).
        np.testing.assert_array_equal(r.pull(160), chunk2)
        np.testing.assert_array_equal(r.pull(160, delay_samples=160), chunk1)


# --- Input validation --------------------------------------------------


class TestValidation:
    def test_invalid_rate_raises(self) -> None:
        with pytest.raises(ValueError, match="rate"):
            ReferenceReader(rate=0)

    def test_invalid_buffer_secs_raises(self) -> None:
        with pytest.raises(ValueError, match="buffer_secs"):
            ReferenceReader(buffer_secs=0)

    def test_pull_zero_samples_raises(self) -> None:
        r = _reader()
        with pytest.raises(ValueError, match="n_samples"):
            r.pull(n_samples=0)

    def test_pull_beyond_capacity_raises(self) -> None:
        r = _reader(buffer_secs=0.01)  # 160 samples
        r.push_for_test(np.ones(160, dtype=np.int16))
        with pytest.raises(ValueError, match="capacity"):
            r.pull(n_samples=200)


# --- Lifecycle ---------------------------------------------------------


class TestLifecycle:
    def test_stop_without_start_is_safe(self) -> None:
        r = _reader()
        r.stop()  # must not raise

    def test_double_start_raises(self) -> None:
        # Use an invalid monitor source so the thread doesn't actually
        # spin up parec — but the reader still considers itself
        # started until stop().
        r = _reader(monitor_source="nonexistent_source")
        r.start()
        try:
            with pytest.raises(RuntimeError, match="already started"):
                r.start()
        finally:
            r.stop()
