"""Unit tests for the SNR-mixing math + WAV plumbing in the wake pipeline.

Run with the node repo venv (numpy/scipy/pytest only — no soundfile,
no openwakeword needed):

    pytest tools/wake_model_training/test_augment_music.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from augment_music import parse_args, snr_bucket_name  # noqa: E402
from common import (  # noqa: E402
    SAMPLE_RATE,
    apply_rir,
    fit_noise_length,
    measure_snr_db,
    mix_at_snr,
    read_wav_mono_16k,
    rms,
    write_wav_mono_16k,
)


def tone(freq: float, seconds: float = 1.0, amp: float = 0.3) -> np.ndarray:
    t = np.arange(int(seconds * SAMPLE_RATE)) / SAMPLE_RATE
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def white_noise(seconds: float = 1.0, amp: float = 0.1,
                seed: int = 7) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return (amp * rng.standard_normal(int(seconds * SAMPLE_RATE))
            ).astype(np.float32)


# ---------------------------------------------------------------------------
# rms / measure_snr_db
# ---------------------------------------------------------------------------


class TestRms:
    def test_sine_rms_is_amp_over_sqrt2(self):
        amp = 0.5
        assert rms(tone(440, amp=amp)) == pytest.approx(amp / np.sqrt(2),
                                                        rel=1e-3)

    def test_silence_is_zero(self):
        assert rms(np.zeros(1000, dtype=np.float32)) == 0.0

    def test_empty_is_zero(self):
        assert rms(np.array([], dtype=np.float32)) == 0.0


class TestMeasureSnr:
    def test_equal_rms_is_zero_db(self):
        s = tone(440, amp=0.3)
        n = tone(1000, amp=0.3)
        assert measure_snr_db(s, n) == pytest.approx(0.0, abs=1e-6)

    def test_10x_amplitude_is_20_db(self):
        s = tone(440, amp=0.5)
        n = tone(1000, amp=0.05)
        assert measure_snr_db(s, n) == pytest.approx(20.0, abs=1e-6)

    def test_silent_noise_is_inf(self):
        assert measure_snr_db(tone(440), np.zeros(100)) == float("inf")

    def test_silent_signal_is_neg_inf(self):
        assert measure_snr_db(np.zeros(100), tone(440)) == float("-inf")


# ---------------------------------------------------------------------------
# mix_at_snr — the core contract
# ---------------------------------------------------------------------------


class TestMixAtSnr:
    @pytest.mark.parametrize("snr_db", [10.0, 5.0, 0.0, -5.0, -10.0])
    def test_achieved_snr_matches_request(self, snr_db):
        """Recover the noise term from the mix; its level must land on
        the requested SNR (small amp keeps clipping rescue out of play)."""
        signal = tone(440, amp=0.1)
        noise = white_noise(amp=0.1)
        mix, gain = mix_at_snr(signal, noise, snr_db)
        assert gain == 1.0  # no clipping rescue at these amplitudes
        recovered_noise = mix - signal
        assert measure_snr_db(signal, recovered_noise) == pytest.approx(
            snr_db, abs=0.01)

    def test_signal_untouched_when_no_clipping(self):
        """Only the NOISE is scaled to hit the SNR — the wake-phrase
        energy must match the source clip. The projection of the mix
        onto the signal has coefficient ~1 (white noise is uncorrelated
        with a tone)."""
        signal = tone(440, amp=0.1)
        noise = white_noise(amp=0.2)
        mix, _ = mix_at_snr(signal, noise, 10.0)
        coeff = float(np.dot(mix, signal) / np.dot(signal, signal))
        assert coeff == pytest.approx(1.0, abs=0.02)

    def test_negative_snr_makes_noise_louder_than_signal(self):
        signal = tone(440, amp=0.1)
        noise = white_noise(amp=0.1)
        mix, _ = mix_at_snr(signal, noise, -10.0)
        noise_part = mix - signal
        assert rms(noise_part) > rms(signal)

    def test_clipping_rescue_keeps_peak_bounded(self):
        signal = tone(440, amp=0.9)
        noise = tone(440, amp=0.9)  # in-phase worst case
        mix, gain = mix_at_snr(signal, noise, 0.0, peak=0.99)
        assert gain < 1.0
        assert float(np.max(np.abs(mix))) <= 0.99 + 1e-6

    def test_clipping_rescue_preserves_snr(self):
        """Post-sum gain scales signal and noise together — SNR intact."""
        signal = tone(440, amp=0.9)
        noise = tone(440, amp=0.9)
        mix, gain = mix_at_snr(signal, noise, 0.0)
        recovered_noise = mix - signal * gain
        assert measure_snr_db(signal * gain, recovered_noise) == pytest.approx(
            0.0, abs=0.01)

    def test_silent_signal_raises(self):
        noise = white_noise()
        with pytest.raises(ValueError, match="signal is silent"):
            mix_at_snr(np.zeros(noise.size, dtype=np.float32), noise, 0.0)

    def test_silent_noise_raises(self):
        with pytest.raises(ValueError, match="noise is silent"):
            mix_at_snr(tone(440), np.zeros(SAMPLE_RATE, dtype=np.float32), 0.0)

    def test_length_mismatch_raises(self):
        with pytest.raises(ValueError, match="length mismatch"):
            mix_at_snr(tone(440, seconds=1.0), white_noise(seconds=0.5), 0.0)

    def test_stereo_input_raises(self):
        stereo = np.zeros((100, 2), dtype=np.float32)
        with pytest.raises(ValueError, match="mono"):
            mix_at_snr(stereo, stereo, 0.0)

    def test_output_dtype_float32(self):
        mix, _ = mix_at_snr(tone(440, amp=0.1), white_noise(amp=0.1), 3.0)
        assert mix.dtype == np.float32


# ---------------------------------------------------------------------------
# fit_noise_length
# ---------------------------------------------------------------------------


class TestFitNoiseLength:
    def test_trims_long_noise(self):
        noise = white_noise(seconds=2.0)
        out = fit_noise_length(noise, SAMPLE_RATE)
        assert out.size == SAMPLE_RATE
        np.testing.assert_array_equal(out, noise[:SAMPLE_RATE])

    def test_offset_selects_window(self):
        noise = np.arange(100, dtype=np.float32)
        out = fit_noise_length(noise, 10, offset=25)
        np.testing.assert_array_equal(out, noise[25:35])

    def test_tiles_short_noise(self):
        noise = np.array([1.0, 2.0, 3.0], dtype=np.float32)
        out = fit_noise_length(noise, 7)
        np.testing.assert_array_equal(
            out, np.array([1, 2, 3, 1, 2, 3, 1], dtype=np.float32))

    def test_tiling_with_offset(self):
        noise = np.array([1.0, 2.0, 3.0], dtype=np.float32)
        out = fit_noise_length(noise, 4, offset=2)
        np.testing.assert_array_equal(
            out, np.array([3, 1, 2, 3], dtype=np.float32))

    def test_offset_beyond_length_wraps(self):
        noise = np.array([1.0, 2.0, 3.0], dtype=np.float32)
        out = fit_noise_length(noise, 2, offset=4)  # 4 % 3 == 1
        np.testing.assert_array_equal(out, np.array([2, 3], dtype=np.float32))

    def test_empty_noise_raises(self):
        with pytest.raises(ValueError, match="empty"):
            fit_noise_length(np.array([], dtype=np.float32), 10)


# ---------------------------------------------------------------------------
# apply_rir
# ---------------------------------------------------------------------------


class TestApplyRir:
    def test_identity_impulse_preserves_signal(self):
        signal = tone(440, amp=0.3)
        rir = np.zeros(64, dtype=np.float32)
        rir[0] = 1.0
        wet = apply_rir(signal, rir)
        np.testing.assert_allclose(wet, signal, atol=1e-4)

    def test_output_length_matches_input(self):
        signal = tone(440, seconds=0.5)
        rir = white_noise(seconds=0.1, amp=0.5)
        assert apply_rir(signal, rir).size == signal.size

    def test_rms_preserved(self):
        """Energy normalization: an RIR pass must not shift the SNR a
        later mix_at_snr computes."""
        signal = tone(440, amp=0.3)
        rir = np.abs(white_noise(seconds=0.05, amp=0.5)) * np.exp(
            -np.linspace(0, 8, int(0.05 * SAMPLE_RATE)))
        wet = apply_rir(signal, rir.astype(np.float32))
        assert rms(wet) == pytest.approx(rms(signal), rel=1e-3)

    def test_empty_rir_raises(self):
        with pytest.raises(ValueError, match="empty"):
            apply_rir(tone(440), np.array([], dtype=np.float32))


# ---------------------------------------------------------------------------
# WAV round-trip (stdlib wave I/O)
# ---------------------------------------------------------------------------


class TestWavRoundTrip:
    def test_mono_16k_round_trip(self, tmp_path):
        signal = tone(440, amp=0.3)
        path = tmp_path / "t.wav"
        write_wav_mono_16k(path, signal)
        back = read_wav_mono_16k(path)
        assert back.size == signal.size
        # int16 quantization: write scales by 32767, read divides by 32768
        # → worst-case error just under 2 LSB.
        np.testing.assert_allclose(back, signal, atol=2.0 / 32767)

    def test_write_clips_out_of_range(self, tmp_path):
        loud = np.array([2.0, -2.0, 0.5], dtype=np.float32)
        path = tmp_path / "loud.wav"
        write_wav_mono_16k(path, loud)
        back = read_wav_mono_16k(path)
        assert float(np.max(np.abs(back))) <= 1.0

    def test_stereo_48k_downmix_resample(self, tmp_path):
        """June-recording shape: 48 kHz stereo → 16 kHz mono."""
        import wave

        rate = 48000
        t = np.arange(rate) / rate  # 1 s
        left = (0.3 * np.sin(2 * np.pi * 440 * t) * 32767).astype(np.int16)
        right = left.copy()
        interleaved = np.column_stack([left, right]).ravel()
        path = tmp_path / "stereo48k.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(interleaved.tobytes())

        back = read_wav_mono_16k(path)
        assert back.size == SAMPLE_RATE  # 1 s at 16 kHz
        assert rms(back) == pytest.approx(0.3 / np.sqrt(2), rel=0.02)


# ---------------------------------------------------------------------------
# augment_music CLI plumbing
# ---------------------------------------------------------------------------


class TestBucketNames:
    @pytest.mark.parametrize("snr,expected", [
        (10.0, "snr_p10"),
        (5.0, "snr_p5"),
        (0.0, "snr_p0"),
        (-5.0, "snr_n5"),
        (-10.0, "snr_n10"),
        (2.5, "snr_p2.5"),
    ])
    def test_bucket_name(self, snr, expected):
        assert snr_bucket_name(snr) == expected


class TestArgParsing:
    def test_default_snr_grid(self):
        args = parse_args(["--positives-dir", "p", "--music-dir", "m",
                           "--out-dir", "o"])
        assert args.snr_grid == [10.0, 5.0, 0.0, -5.0, -10.0]

    def test_custom_snr_grid(self):
        args = parse_args(["--positives-dir", "p", "--music-dir", "m",
                           "--out-dir", "o", "--snr-grid", "6,-3"])
        assert args.snr_grid == [6.0, -3.0]

    def test_bad_snr_grid_rejected(self):
        with pytest.raises(SystemExit):
            parse_args(["--positives-dir", "p", "--music-dir", "m",
                        "--out-dir", "o", "--snr-grid", "loud,quiet"])

    def test_missing_required_args_rejected(self):
        with pytest.raises(SystemExit):
            parse_args(["--positives-dir", "p"])

    def test_dry_run_flag(self):
        args = parse_args(["--positives-dir", "p", "--music-dir", "m",
                           "--out-dir", "o", "--dry-run"])
        assert args.dry_run is True
