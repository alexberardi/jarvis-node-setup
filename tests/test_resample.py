"""Tests for core.resample — the numpy-only decimator that replaced
scipy.signal.resample_poly in the wake/barge-in path.

The equivalence tests compare against scipy, which stays a dev/test
dependency. They are skipped when scipy isn't installed so the module's
own behaviour is still covered on a scipy-free node venv.
"""

import numpy as np
import pytest

from core.resample import decimate_int16, decimation_filter


def _scipy_reference(x: np.ndarray, down: int) -> np.ndarray:
    """What the node did before: resample_poly → clip → int16."""
    signal = pytest.importorskip("scipy.signal")
    y = signal.resample_poly(x, up=1, down=down)
    return np.clip(y, -32768, 32767).astype(np.int16)


def _mic_like(n: int, seed: int = 0) -> np.ndarray:
    """Wideband int16 test signal: speech-band tones + full-band noise.

    The noise reaches 24 kHz, so a filter that aliased differently
    from resample_poly would show up here.
    """
    rng = np.random.default_rng(seed)
    t = np.arange(n) / 48000
    x = (
        6000 * np.sin(2 * np.pi * 220 * t)
        + 3000 * np.sin(2 * np.pi * 3100 * t)
        + 2000 * np.sin(2 * np.pi * 11000 * t)  # above the 8 kHz cutoff
        + rng.normal(0, 1500, n)
    )
    return np.clip(x, -32768, 32767).astype(np.int16)


class TestDecimationFilter:
    def test_down3_is_61_taps_with_unit_dc_gain(self) -> None:
        h = decimation_filter(3)
        assert h.shape == (61,)
        assert h.sum() == pytest.approx(1.0, abs=1e-12)
        # Symmetric (linear phase), like firwin's output.
        np.testing.assert_allclose(h, h[::-1], atol=1e-15)

    @pytest.mark.parametrize("down", [2, 3, 4, 6])
    def test_matches_scipy_firwin(self, down: int) -> None:
        signal = pytest.importorskip("scipy.signal")
        expected = signal.firwin(20 * down + 1, 1.0 / down, window=("kaiser", 5.0))
        np.testing.assert_allclose(decimation_filter(down), expected, rtol=0, atol=1e-15)

    def test_is_cached_and_read_only(self) -> None:
        assert decimation_filter(3) is decimation_filter(3)
        with pytest.raises(ValueError):
            decimation_filter(3)[0] = 0.0

    def test_rejects_bad_factor(self) -> None:
        with pytest.raises(ValueError):
            decimation_filter(0)


class TestDecimateInt16:
    def test_node_chunk_shape(self) -> None:
        """The wake loop's 3840-sample 48 kHz chunk becomes oww's 1280."""
        y = decimate_int16(_mic_like(3840), 3)
        assert y.dtype == np.int16
        assert y.shape == (1280,)

    @pytest.mark.parametrize("n", [3840, 3841, 3839, 1000, 60, 7, 1])
    def test_output_length_matches_resample_poly(self, n: int) -> None:
        x = _mic_like(n, seed=n)
        assert decimate_int16(x, 3).shape == _scipy_reference(x, 3).shape

    @pytest.mark.parametrize("down", [2, 3])
    @pytest.mark.parametrize("seed", range(5))
    def test_within_one_lsb_of_resample_poly(self, down: int, seed: int) -> None:
        x = _mic_like(3840, seed=seed)
        diff = decimate_int16(x, down).astype(np.int32) - _scipy_reference(x, down).astype(np.int32)
        assert np.abs(diff).max() <= 1

    def test_short_chunk_within_one_lsb(self) -> None:
        """Shorter than the filter: edges are zero-padded like scipy."""
        x = _mic_like(40, seed=9)
        diff = decimate_int16(x, 3).astype(np.int32) - _scipy_reference(x, 3).astype(np.int32)
        assert np.abs(diff).max() <= 1

    def test_full_scale_is_clipped_not_wrapped(self) -> None:
        """Kaiser ringing overshoots on a full-scale square wave; the result
        must saturate at the int16 rails rather than wrap around."""
        x = np.tile(np.r_[np.full(48, 32767), np.full(48, -32768)], 40).astype(np.int16)
        y = decimate_int16(x, 3)
        assert y.max() == 32767
        assert y.min() == -32768
        np.testing.assert_array_equal(y, _scipy_reference(x, 3))

    def test_down_one_is_identity(self) -> None:
        x = _mic_like(100)
        np.testing.assert_array_equal(decimate_int16(x, 1), x)

    def test_empty_input(self) -> None:
        assert decimate_int16(np.zeros(0, dtype=np.int16), 3).shape == (0,)

    def test_attenuates_above_new_nyquist(self) -> None:
        """An 11 kHz tone would alias to 5 kHz without the low-pass."""
        t = np.arange(48000) / 48000
        x = (10000 * np.sin(2 * np.pi * 11000 * t)).astype(np.int16)
        y = decimate_int16(x, 3).astype(np.float64)
        assert np.sqrt(np.mean(y[100:-100] ** 2)) < 10  # ~ -60 dB vs ~7071 RMS in
