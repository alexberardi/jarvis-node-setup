"""Tests for core.aec_speex.EchoCanceller.

The suppression test is the load-bearing one — it proves end-to-end
that the ctypes binding wires up correctly AND that Speex actually
cancels echo. Everything else is input validation and lifecycle.

These tests require ``libspeexdsp.so.1`` to be available at import
time. On Debian 13 / Pi Zero 2 W it's already present via the
``libspeexdsp1`` apt package (a transitive dep of pulseaudio).
"""

from __future__ import annotations

import numpy as np
import pytest

speex_module = pytest.importorskip(
    "core.aec_speex",
    reason="libspeexdsp not available — install libspeexdsp1 (apt) or speexdsp (brew)",
)
EchoCanceller = speex_module.EchoCanceller


# --- Suppression test -------------------------------------------------


class TestEchoSuppression:
    """The actual `does the AEC work?` test."""

    @staticmethod
    def _rms(arr: np.ndarray) -> float:
        return float(np.sqrt(np.mean(arr.astype(np.float64) ** 2)))

    def test_white_noise_echo_is_suppressed(self) -> None:
        """Feed a delayed+attenuated copy of the reference as the mic;
        after a few seconds of adaptation the cleaned output should
        have substantially less energy than the unprocessed mic.

        White noise is the easiest case for an LMS adaptive filter:
        wide spectrum means every frequency bin has signal, so the
        filter converges in well under a second. Real speaker bleed
        from music is harder, but if AEC can't suppress white noise
        the wrapper is broken.
        """
        sr = 16000
        frame_size = 160        # 10 ms
        filter_length = 1600    # 100 ms tail
        duration_s = 5
        n = duration_s * sr

        rng = np.random.default_rng(seed=42)
        reference = rng.integers(-8000, 8000, n, dtype=np.int16)

        # Echo: 10 ms delay, 0.5 attenuation — well within the filter's
        # 100 ms tail, so it should fully cancel.
        delay = int(0.010 * sr)
        mic_i32 = np.zeros(n, dtype=np.int32)
        mic_i32[delay:] = reference[:-delay].astype(np.int32) // 2
        mic = np.clip(mic_i32, -32768, 32767).astype(np.int16)

        aec = EchoCanceller(
            frame_size=frame_size,
            filter_length=filter_length,
            sample_rate=sr,
        )
        try:
            cleaned = aec.process(mic, reference)
        finally:
            aec.close()

        # Measure suppression on the LAST second (after adaptation).
        # The first few hundred ms include the filter warming up, so
        # they're not a fair measure.
        tail_start = n - sr
        raw_rms = self._rms(mic[tail_start:])
        cleaned_rms = self._rms(cleaned[tail_start:])

        assert raw_rms > 0, "test bug: mic signal has no energy"
        suppression_db = 20 * np.log10(raw_rms / max(cleaned_rms, 1.0))

        assert suppression_db >= 12, (
            f"Speex AEC only achieved {suppression_db:.1f} dB suppression "
            f"on white-noise echo (expected >= 12 dB). "
            f"raw_rms={raw_rms:.1f} cleaned_rms={cleaned_rms:.1f}"
        )

    def test_no_reference_no_change(self) -> None:
        """If the reference is silent, the AEC has nothing to subtract;
        the cleaned mic should be nearly identical to the input mic.
        """
        sr = 16000
        frame_size = 160
        n = 16000  # 1 s

        rng = np.random.default_rng(seed=7)
        mic = rng.integers(-4000, 4000, n, dtype=np.int16)
        reference = np.zeros(n, dtype=np.int16)

        aec = EchoCanceller(frame_size=frame_size, filter_length=1600, sample_rate=sr)
        try:
            cleaned = aec.process(mic, reference)
        finally:
            aec.close()

        # Speex has an internal HP filter, so the cleaned signal isn't
        # bit-identical to the input — but the RMS should be within
        # a few percent of the input.
        ratio = self._rms(cleaned) / self._rms(mic)
        assert 0.7 < ratio < 1.3, (
            f"With silent reference, cleaned RMS should ~match input RMS; "
            f"got ratio {ratio:.2f}"
        )


# --- Construction validation -----------------------------------------


class TestConstruction:
    def test_invalid_frame_size_raises(self) -> None:
        with pytest.raises(ValueError, match="frame_size must be positive"):
            EchoCanceller(frame_size=0)
        with pytest.raises(ValueError, match="frame_size must be positive"):
            EchoCanceller(frame_size=-1)

    def test_filter_smaller_than_frame_raises(self) -> None:
        with pytest.raises(ValueError, match="filter_length"):
            EchoCanceller(frame_size=320, filter_length=160)

    def test_filter_equal_to_frame_is_allowed(self) -> None:
        # Edge case: pathologically short filter is technically valid.
        aec = EchoCanceller(frame_size=160, filter_length=160, sample_rate=16000)
        aec.close()


# --- process() input validation --------------------------------------


class TestProcessValidation:
    @pytest.fixture
    def aec(self):
        a = EchoCanceller(frame_size=160, filter_length=1600, sample_rate=16000)
        yield a
        a.close()

    def test_dtype_mismatch_raises(self, aec) -> None:
        mic = np.zeros(160, dtype=np.float32)
        ref = np.zeros(160, dtype=np.int16)
        with pytest.raises(ValueError, match="int16"):
            aec.process(mic, ref)

    def test_dim_mismatch_raises(self, aec) -> None:
        mic = np.zeros((160, 1), dtype=np.int16)
        ref = np.zeros(160, dtype=np.int16)
        with pytest.raises(ValueError, match="1-D"):
            aec.process(mic, ref)

    def test_length_mismatch_raises(self, aec) -> None:
        mic = np.zeros(160, dtype=np.int16)
        ref = np.zeros(320, dtype=np.int16)
        with pytest.raises(ValueError, match="length"):
            aec.process(mic, ref)

    def test_non_multiple_of_frame_size_raises(self, aec) -> None:
        mic = np.zeros(200, dtype=np.int16)
        ref = np.zeros(200, dtype=np.int16)
        with pytest.raises(ValueError, match="multiple of frame_size"):
            aec.process(mic, ref)

    def test_after_close_raises(self) -> None:
        aec = EchoCanceller(frame_size=160, filter_length=1600, sample_rate=16000)
        aec.close()
        with pytest.raises(RuntimeError, match="already closed"):
            aec.process(np.zeros(160, dtype=np.int16), np.zeros(160, dtype=np.int16))

    def test_multi_frame_chunk_processed(self, aec) -> None:
        # 1280 samples = 8 frames at frame_size=160 — must not raise.
        mic = np.zeros(1280, dtype=np.int16)
        ref = np.zeros(1280, dtype=np.int16)
        out = aec.process(mic, ref)
        assert out.shape == (1280,) and out.dtype == np.int16


# --- Lifecycle -------------------------------------------------------


class TestLifecycle:
    def test_close_is_idempotent(self) -> None:
        aec = EchoCanceller(frame_size=160, filter_length=1600, sample_rate=16000)
        aec.close()
        aec.close()  # must not crash

    def test_reset_after_close_is_safe(self) -> None:
        aec = EchoCanceller(frame_size=160, filter_length=1600, sample_rate=16000)
        aec.close()
        aec.reset()  # must not crash

    def test_reset_before_process_works(self) -> None:
        aec = EchoCanceller(frame_size=160, filter_length=1600, sample_rate=16000)
        try:
            aec.reset()
            out = aec.process(
                np.zeros(160, dtype=np.int16),
                np.zeros(160, dtype=np.int16),
            )
            assert out.shape == (160,)
        finally:
            aec.close()
