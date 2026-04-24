"""Single-producer, many-consumer audio fan-out.

One long-running thread reads fixed-size chunks from the mic, appends
them to a rolling ring buffer (last ``history_secs`` seconds), and
pushes each chunk to every registered consumer queue. Consumers are
identified by name and can optionally prime their queue with audio
from the ring buffer at subscribe time — this closes the TTS-end →
listen-start gap that drops the first word of user commands.

Design constraints:
  - Exactly one PyAudio stream open for the lifetime of the node.
    Concurrent PyAudio opens on dsnoop are the race surface we are
    specifically eliminating.
  - Slow consumers must not block the producer. If a consumer's queue
    is full we drop its oldest chunk, log, and keep the producer live
    so wake detection stays responsive.
  - Source-agnostic bus + pyaudio_factory injection = tests without
    hardware.
"""

from __future__ import annotations

import queue
import threading
import time
from collections import deque
from typing import Callable, Optional

import pyaudio

from jarvis_log_client import JarvisLogger
from utils.mic_device import resolve_input_device_index

logger = JarvisLogger(service="jarvis-node")


class AudioBus:
    """Single mic capture → many consumer queues.

    Typical wiring::

        bus = AudioBus(rate=48000, chunk_samples=1536)
        bus.start()
        try:
            wake_q = bus.subscribe("wake")
            while not stopping:
                chunk = wake_q.get()
                score_wake_word(chunk)
        finally:
            bus.unsubscribe("wake")
            bus.stop()

    ``history_secs`` parameter on subscribe replays the tail of the ring
    buffer into the new queue, so a consumer can "start" slightly after
    the audio actually arrived and still see it.
    """

    def __init__(
        self,
        *,
        rate: int = 48000,
        chunk_samples: int = 1536,
        sample_format: int = pyaudio.paInt16,
        channels: int = 1,
        history_secs: float = 2.0,
        device_index_resolver: Callable[[], Optional[int]] = resolve_input_device_index,
        pyaudio_factory: Callable[[], pyaudio.PyAudio] = pyaudio.PyAudio,
        read_retry_sleep_secs: float = 0.05,
    ):
        self.rate = rate
        self.chunk_samples = chunk_samples
        self.sample_format = sample_format
        self.channels = channels

        ring_capacity = max(1, int(history_secs * rate / chunk_samples))
        self._ring: deque[bytes] = deque(maxlen=ring_capacity)
        self._ring_lock = threading.Lock()

        self._subscribers: dict[str, queue.Queue[bytes]] = {}
        self._subs_lock = threading.Lock()

        self._device_index_resolver = device_index_resolver
        self._pyaudio_factory = pyaudio_factory
        self._read_retry_sleep_secs = read_retry_sleep_secs

        self._pa: Optional[pyaudio.PyAudio] = None
        self._stream = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._started_event = threading.Event()

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """Open the mic and begin the producer thread.

        Blocks until the producer thread has entered its read loop (or
        failed). Raises RuntimeError if already started.
        """
        if self._thread is not None:
            raise RuntimeError("AudioBus already started")

        self._pa = self._pyaudio_factory()
        device_index = self._device_index_resolver()
        open_kwargs: dict = dict(
            format=self.sample_format,
            channels=self.channels,
            rate=self.rate,
            input=True,
            frames_per_buffer=self.chunk_samples,
        )
        if device_index is not None:
            open_kwargs["input_device_index"] = device_index

        self._stream = self._pa.open(**open_kwargs)
        self._stop_event.clear()
        self._started_event.clear()
        self._thread = threading.Thread(
            target=self._producer_loop, daemon=True, name="AudioBusProducer"
        )
        self._thread.start()
        # Wait for the producer to at least enter its loop, so callers
        # can subscribe immediately with a guarantee the first chunk is
        # on its way.
        self._started_event.wait(timeout=2.0)
        logger.info(
            "AudioBus started",
            rate=self.rate,
            chunk_samples=self.chunk_samples,
            device_index=device_index,
        )

    def stop(self) -> None:
        """Signal producer to stop, close mic. Idempotent."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None
        if self._stream is not None:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except Exception as e:
                logger.debug("AudioBus stream close error (non-fatal)", error=str(e))
            self._stream = None
        if self._pa is not None:
            try:
                self._pa.terminate()
            except Exception as e:
                logger.debug("AudioBus PyAudio terminate error (non-fatal)", error=str(e))
            self._pa = None

    def subscribe(
        self,
        name: str,
        *,
        history_secs: float = 0.0,
        maxsize: int = 128,
    ) -> "queue.Queue[bytes]":
        """Register a consumer queue and return it.

        If ``history_secs > 0``, the queue is primed with the last N
        seconds of audio from the ring buffer before the producer's next
        push arrives.
        """
        q: queue.Queue[bytes] = queue.Queue(maxsize=maxsize)

        if history_secs > 0:
            history_chunks = int(history_secs * self.rate / self.chunk_samples)
            with self._ring_lock:
                snapshot = list(self._ring)[-history_chunks:] if history_chunks > 0 else []
            for chunk in snapshot:
                try:
                    q.put_nowait(chunk)
                except queue.Full:
                    break

        with self._subs_lock:
            if name in self._subscribers:
                raise ValueError(f"AudioBus subscriber {name!r} already registered")
            self._subscribers[name] = q

        logger.debug(
            "AudioBus subscribed",
            name=name,
            history_secs=history_secs,
            primed_chunks=q.qsize(),
        )
        return q

    def unsubscribe(self, name: str) -> None:
        """Remove a consumer. No-op if name isn't registered."""
        with self._subs_lock:
            removed = self._subscribers.pop(name, None)
        if removed is not None:
            logger.debug("AudioBus unsubscribed", name=name)

    def subscribers(self) -> list[str]:
        """Snapshot of current subscriber names (for diagnostics)."""
        with self._subs_lock:
            return list(self._subscribers.keys())

    def push(self, data: bytes) -> None:
        """Inject a chunk directly (test/alternate-source hook).

        Production code uses ``start()`` + the internal producer. This
        method lets tests drive the bus deterministically without
        touching PyAudio.
        """
        self._distribute(data)

    def _producer_loop(self) -> None:
        assert self._stream is not None
        self._started_event.set()
        while not self._stop_event.is_set():
            try:
                data = self._stream.read(
                    self.chunk_samples, exception_on_overflow=False
                )
            except OSError as e:
                logger.warning("AudioBus read error, retrying", error=str(e))
                time.sleep(self._read_retry_sleep_secs)
                continue
            self._distribute(data)

    def _distribute(self, data: bytes) -> None:
        with self._ring_lock:
            self._ring.append(data)

        with self._subs_lock:
            subs = list(self._subscribers.items())

        for name, q in subs:
            try:
                q.put_nowait(data)
            except queue.Full:
                try:
                    q.get_nowait()
                    q.put_nowait(data)
                except (queue.Empty, queue.Full):
                    pass
                logger.debug("AudioBus subscriber slow, dropped chunk", name=name)
