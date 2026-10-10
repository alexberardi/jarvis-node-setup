"""Integer-factor decimation for the wake path, numpy only.

The node captures 48 kHz and openWakeWord wants 16 kHz. This used to be
``scipy.signal.resample_poly(x, up=1, down=3)``, which cost the always-on
wake process ~60 MB RSS just to import scipy (measured on a Pi 4; see
jarvis-host-agent docs/node/go-pi-node.md §5.3).

:func:`decimate_int16` reproduces resample_poly's ``up=1`` path exactly:
the same filter (``firwin(20*down + 1, 1/down, window=("kaiser", 5.0))``),
the same centring, and the same zero-padded edges on every call, so each
chunk is processed independently, just like before. Results match scipy
to within 1 LSB after the int16 conversion (float64 rounding only).
"""

from functools import lru_cache

import numpy as np

# resample_poly's defaults: half-length 10 * max(up, down), Kaiser beta 5.
_HALF_LEN_PER_FACTOR = 10
_KAISER_BETA = 5.0


@lru_cache(maxsize=8)
def decimation_filter(down: int) -> np.ndarray:
    """Low-pass FIR that resample_poly uses for ``up=1, down=down``.

    Windowed sinc with cutoff at the new Nyquist (``1/down`` of the old
    one), normalised to unit DC gain, as ``scipy.signal.firwin`` does.
    Cached and read-only: callers share one array per factor.
    """
    if down < 1:
        raise ValueError(f"decimation factor must be >= 1, got {down}")
    half_len = _HALF_LEN_PER_FACTOR * down
    m = np.arange(-half_len, half_len + 1, dtype=np.float64)
    h = np.sinc(m / down) * np.kaiser(2 * half_len + 1, _KAISER_BETA)
    h /= h.sum()
    h.setflags(write=False)
    return h


def decimate_int16(samples: np.ndarray, down: int) -> np.ndarray:
    """Low-pass and keep every ``down``-th sample; int16 in, int16 out.

    Equivalent to ``np.clip(resample_poly(samples, 1, down), -32768,
    32767).astype(np.int16)``. Output length is ``ceil(len / down)``.
    """
    if down == 1:
        return np.asarray(samples, dtype=np.int16)
    x = np.asarray(samples, dtype=np.float64)
    n = x.shape[0]
    if n == 0:
        return np.zeros(0, dtype=np.int16)
    h = decimation_filter(down)
    half_len = (h.shape[0] - 1) // 2
    # Full convolution, then the slice centred on the input: identical to
    # a zero-padded, zero-phase filter, independent of which of x and h
    # is longer (np.convolve's "same" mode is not, for short chunks).
    y = np.convolve(x, h)[half_len : half_len + n : down]
    return np.clip(y, -32768, 32767).astype(np.int16)
