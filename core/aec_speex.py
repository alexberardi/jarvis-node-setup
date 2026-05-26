"""Speex DSP echo canceller — Python wrapper around libspeexdsp.so.1.

Why Speex over WebRTC for the first pass: pure C API (this file is ~150
lines vs ~400+ to wrap WebRTC APM's C++ vtables), already shipped via
``libspeexdsp1`` on Debian 13 (no compilation on the Pi Zero 2 W, which
has 512 MB and would OOM trying to build ``webrtc-audio-processing``).
Quality is below WebRTC AEC3 but adequate for the speaker-bleed regime
on the ReSpeaker HAT.

Public surface::

    EchoCanceller(frame_size, filter_length, sample_rate)
        .process(mic_chunk, reference_chunk) -> cleaned_chunk
        .reset()    # forget the adapted filter (e.g. after a reference gap)
        .close()    # free the underlying state; idempotent

Sample format is always int16 mono. The caller is responsible for
time-aligning ``reference`` to ``mic``; the wrapper is the math, not
the plumbing.

Speex's echo state is NOT thread-safe — keep a single ``EchoCanceller``
on the producer thread.
"""

from __future__ import annotations

import ctypes
import ctypes.util
from ctypes import POINTER, c_int, c_int16, c_void_p

import numpy as np


def _load_speexdsp() -> ctypes.CDLL:
    soname = ctypes.util.find_library("speexdsp") or "libspeexdsp.so.1"
    try:
        return ctypes.CDLL(soname)
    except OSError as exc:
        raise OSError(
            f"libspeexdsp not found (tried {soname}). "
            "On Debian-based systems install with: apt-get install libspeexdsp1"
        ) from exc


_lib = _load_speexdsp()

# Function signatures must be declared before first call — ctypes assumes
# int return on 64-bit otherwise, which truncates pointers and crashes.
_lib.speex_echo_state_init.argtypes = [c_int, c_int]
_lib.speex_echo_state_init.restype = c_void_p

_lib.speex_echo_state_destroy.argtypes = [c_void_p]
_lib.speex_echo_state_destroy.restype = None

_lib.speex_echo_cancellation.argtypes = [
    c_void_p,           # SpeexEchoState *
    POINTER(c_int16),   # rec   (mic input)
    POINTER(c_int16),   # play  (reference / speaker signal)
    POINTER(c_int16),   # out   (cleaned mic output)
]
_lib.speex_echo_cancellation.restype = None

_lib.speex_echo_state_reset.argtypes = [c_void_p]
_lib.speex_echo_state_reset.restype = None

_lib.speex_echo_ctl.argtypes = [c_void_p, c_int, c_void_p]
_lib.speex_echo_ctl.restype = c_int

# From speex/speex_echo.h. Stable across libspeexdsp versions.
_SPEEX_ECHO_SET_SAMPLING_RATE = 24


class EchoCanceller:
    """Single-channel acoustic echo canceller backed by libspeexdsp.

    Parameters
    ----------
    frame_size : int
        Samples per ``speex_echo_cancellation`` call. Speex is tuned for
        10-20 ms frames — 160 samples at 16 kHz is the canonical setting.
        Chunks passed to ``process`` must be a multiple of this.
    filter_length : int
        Echo tail length in samples. Typical rooms have 50-200 ms of
        reverb tail; at 16 kHz that's 800-3200 samples. Longer filters
        cancel more reverb but cost more CPU (roughly linear) and take
        longer to adapt.
    sample_rate : int
        Sample rate of mic/reference in Hz. Used only for Speex's
        internal HP filter and double-talk detector tuning.
    """

    def __init__(
        self,
        frame_size: int = 160,
        filter_length: int = 1600,
        sample_rate: int = 16000,
    ):
        if frame_size <= 0:
            raise ValueError(f"frame_size must be positive, got {frame_size}")
        if filter_length < frame_size:
            raise ValueError(
                f"filter_length ({filter_length}) must be >= frame_size ({frame_size})"
            )

        self.frame_size = frame_size
        self.filter_length = filter_length
        self.sample_rate = sample_rate

        state = _lib.speex_echo_state_init(frame_size, filter_length)
        if not state:
            raise OSError("speex_echo_state_init returned NULL")
        self._state: c_void_p | None = c_void_p(state)

        rate_arg = c_int(sample_rate)
        rc = _lib.speex_echo_ctl(
            self._state,
            _SPEEX_ECHO_SET_SAMPLING_RATE,
            ctypes.byref(rate_arg),
        )
        if rc != 0:
            self.close()
            raise OSError(f"speex_echo_ctl SET_SAMPLING_RATE failed (rc={rc})")

    def process(self, mic: np.ndarray, reference: np.ndarray) -> np.ndarray:
        """Cancel echo from ``mic`` using ``reference`` as the speaker signal.

        Both inputs must be 1-D int16 arrays of equal length, and the
        length must be a multiple of ``frame_size``. Returns a new int16
        array of the same length with the cleaned mic signal.

        For chunks larger than ``frame_size`` the call is split into
        sub-frames; the underlying Speex state carries adaptation across
        sub-frames within a single call and across separate calls.
        """
        if self._state is None or self._state.value is None:
            raise RuntimeError("EchoCanceller already closed")
        if mic.dtype != np.int16 or reference.dtype != np.int16:
            raise ValueError(
                f"mic and reference must be int16; got {mic.dtype}, {reference.dtype}"
            )
        if mic.ndim != 1 or reference.ndim != 1:
            raise ValueError("mic and reference must be 1-D arrays")
        if mic.shape[0] != reference.shape[0]:
            raise ValueError(
                f"mic length ({mic.shape[0]}) != reference length ({reference.shape[0]})"
            )
        n = mic.shape[0]
        if n % self.frame_size != 0:
            raise ValueError(
                f"chunk length {n} is not a multiple of frame_size {self.frame_size}"
            )

        mic_c = np.ascontiguousarray(mic, dtype=np.int16)
        ref_c = np.ascontiguousarray(reference, dtype=np.int16)
        out = np.empty(n, dtype=np.int16)

        end = self.frame_size
        for offset in range(0, n, self.frame_size):
            end = offset + self.frame_size
            _lib.speex_echo_cancellation(
                self._state,
                mic_c[offset:end].ctypes.data_as(POINTER(c_int16)),
                ref_c[offset:end].ctypes.data_as(POINTER(c_int16)),
                out[offset:end].ctypes.data_as(POINTER(c_int16)),
            )

        return out

    def reset(self) -> None:
        """Reset the adapted filter state.

        Useful after a gap in the reference stream — a filter that was
        adapted to one playback signal won't help against a new one and
        may briefly produce artifacts during readaptation.
        """
        if self._state is not None and self._state.value is not None:
            _lib.speex_echo_state_reset(self._state)

    def close(self) -> None:
        """Free the underlying Speex state. Idempotent."""
        if self._state is not None and self._state.value is not None:
            _lib.speex_echo_state_destroy(self._state)
            self._state.value = None
        self._state = None

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
