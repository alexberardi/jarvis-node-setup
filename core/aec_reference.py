"""Reference-signal reader for the inline Speex AEC.

Spawns ``parec`` on PulseAudio's monitor source for the playback sink,
reads raw int16 LE samples in a background thread, and ring-buffers
them so the AEC stage can pull a time-aligned reference frame per mic
chunk.

Public surface::

    reader = ReferenceReader(rate=16000)
    reader.start()
    try:
        ref = reader.pull(n_samples=160, delay_samples=1280)
        if ref is None:
            # No fresh reference data — bypass AEC for this frame.
            ...
        else:
            cleaned = aec.process(mic, ref)
    finally:
        reader.stop()

If ``parec`` is missing, the monitor source can't be resolved, or
``parec`` dies and won't restart, the reader stays inert: ``pull()``
returns ``None`` so the AEC stage gracefully bypasses.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import time

import numpy as np

from jarvis_log_client import JarvisLogger

logger = JarvisLogger(service="jarvis-node")


def resolve_default_monitor_source() -> str | None:
    """Return the ``.monitor`` source for the current default PA sink.

    Returns ``None`` if ``pactl`` isn't installed, PA isn't running, or
    no default sink is configured. Caller can fall back to a configured
    source name or skip AEC entirely.
    """
    if not shutil.which("pactl"):
        return None
    try:
        result = subprocess.run(
            ["pactl", "info"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.warning("pactl info failed", error=str(exc))
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        if line.startswith("Default Sink:"):
            return f"{line.split(':', 1)[1].strip()}.monitor"
    return None


class ReferenceReader:
    """Continuously reads PA monitor-source samples for the AEC reference.

    The ring buffer is sized to ``buffer_secs`` of audio so the AEC can
    pull frames with significant negative offset (i.e. samples from
    ``delay_samples`` in the past relative to the most recent write).
    This is how we time-align playback-as-heard-by-mic with the
    playback-as-sent-to-DAC: the speaker→air→mic round-trip is ~50-150
    ms of delay, so we pull reference frames slightly older than "now".
    """

    def __init__(
        self,
        *,
        monitor_source: str | None = None,
        rate: int = 16000,
        buffer_secs: float = 2.0,
        stale_threshold_ms: float = 200.0,
        process_time_ms: int = 10,
        latency_ms: int = 20,
        restart_backoff_secs: float = 1.0,
    ):
        if rate <= 0:
            raise ValueError(f"rate must be positive, got {rate}")
        if buffer_secs <= 0:
            raise ValueError(f"buffer_secs must be positive, got {buffer_secs}")

        self.rate = rate
        self.stale_threshold_secs = stale_threshold_ms / 1000.0
        self._monitor_source = monitor_source
        self._process_time_ms = process_time_ms
        self._latency_ms = latency_ms
        self._restart_backoff_secs = restart_backoff_secs

        self._buffer = np.zeros(int(rate * buffer_secs), dtype=np.int16)
        self._buffer_lock = threading.Lock()
        self._write_idx = 0
        self._last_write_ts = 0.0

        self._proc: subprocess.Popen[bytes] | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._started_event = threading.Event()

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def buffer_capacity_samples(self) -> int:
        return len(self._buffer)

    def start(self) -> None:
        """Resolve the monitor source (if not already given) and spawn the reader thread.

        Idempotent only across stop(); raises if called while already running.
        """
        if self._thread is not None:
            raise RuntimeError("ReferenceReader already started")
        if self._monitor_source is None:
            self._monitor_source = resolve_default_monitor_source()
        if self._monitor_source is None:
            logger.warning(
                "AEC reference: no monitor source available; reader inert (pull() will return None)"
            )
            return
        self._stop_event.clear()
        self._started_event.clear()
        self._thread = threading.Thread(
            target=self._reader_loop, daemon=True, name="AECReferenceReader"
        )
        self._thread.start()
        self._started_event.wait(timeout=2.0)
        logger.info("AEC reference reader started", monitor_source=self._monitor_source)

    def stop(self) -> None:
        """Signal the reader to stop, kill parec, join the thread. Idempotent."""
        self._stop_event.set()
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._proc = None

    def pull(
        self,
        n_samples: int,
        delay_samples: int = 0,
        max_stale_secs: float | None = None,
    ) -> np.ndarray | None:
        """Return ``n_samples`` of reference ending ``delay_samples`` before the most recent write.

        ``delay_samples`` shifts the read window into the past — set this
        to the speaker→mic acoustic delay so AEC sees the reference
        sample that physically arrived at the mic at the same time as
        the mic sample it's paired with.

        Returns ``None`` if the buffer is stale (no samples written in
        the last ``max_stale_secs``), which is the AEC stage's cue to
        bypass for this frame.
        """
        if n_samples <= 0:
            raise ValueError(f"n_samples must be positive, got {n_samples}")
        if n_samples + delay_samples > len(self._buffer):
            raise ValueError(
                f"n_samples ({n_samples}) + delay_samples ({delay_samples}) "
                f"exceeds buffer capacity {len(self._buffer)}"
            )
        if max_stale_secs is None:
            max_stale_secs = self.stale_threshold_secs

        with self._buffer_lock:
            if self._last_write_ts == 0.0:
                return None
            if time.monotonic() - self._last_write_ts > max_stale_secs:
                return None
            buf_size = len(self._buffer)
            end_idx = (self._write_idx - delay_samples) % buf_size
            start_idx = (end_idx - n_samples) % buf_size
            if start_idx < end_idx:
                return self._buffer[start_idx:end_idx].copy()
            return np.concatenate(
                [self._buffer[start_idx:], self._buffer[:end_idx]]
            )

    def push_for_test(self, samples: np.ndarray) -> None:
        """Test-only hook: inject samples as if parec had produced them.

        Lets tests exercise pull() / wrap-around logic without spawning
        an actual parec subprocess.
        """
        with self._buffer_lock:
            self._scatter(samples.astype(np.int16))
            self._last_write_ts = time.monotonic()

    def _reader_loop(self) -> None:
        self._started_event.set()
        while not self._stop_event.is_set():
            proc = self._spawn_parec()
            if proc is None:
                if self._stop_event.wait(self._restart_backoff_secs):
                    return
                continue
            self._proc = proc
            try:
                self._consume_proc_stdout(proc)
            finally:
                if proc.poll() is None:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                self._proc = None
            if self._stop_event.is_set():
                return
            logger.warning(
                "AEC reference: parec exited, will restart",
                returncode=proc.returncode,
            )
            if self._stop_event.wait(self._restart_backoff_secs):
                return

    def _spawn_parec(self) -> subprocess.Popen[bytes] | None:
        try:
            return subprocess.Popen(
                [
                    "parec",
                    "--device", self._monitor_source or "",
                    "--rate", str(self.rate),
                    "--channels", "1",
                    "--format", "s16le",
                    "--raw",
                    f"--process-time-msec={self._process_time_ms}",
                    f"--latency-msec={self._latency_ms}",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except FileNotFoundError:
            logger.error("AEC reference: parec not installed; reader inert")
            return None
        except OSError as exc:
            logger.error("AEC reference: failed to spawn parec", error=str(exc))
            return None

    def _consume_proc_stdout(self, proc: subprocess.Popen[bytes]) -> None:
        chunk_samples = self.rate * self._process_time_ms // 1000
        chunk_bytes = chunk_samples * 2
        assert proc.stdout is not None

        while not self._stop_event.is_set():
            data = proc.stdout.read(chunk_bytes)
            if not data:
                return
            if len(data) % 2 != 0:
                continue
            arr = np.frombuffer(data, dtype=np.int16)
            with self._buffer_lock:
                self._scatter(arr)
                self._last_write_ts = time.monotonic()

    def _scatter(self, arr: np.ndarray) -> None:
        n = len(arr)
        if n == 0:
            return
        buf_size = len(self._buffer)
        if n > buf_size:
            arr = arr[-buf_size:]
            n = buf_size
        end_idx = self._write_idx + n
        if end_idx <= buf_size:
            self._buffer[self._write_idx:end_idx] = arr
        else:
            split = buf_size - self._write_idx
            self._buffer[self._write_idx:] = arr[:split]
            self._buffer[:n - split] = arr[split:]
        self._write_idx = end_idx % buf_size
