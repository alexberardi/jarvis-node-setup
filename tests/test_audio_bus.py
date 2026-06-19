"""Tests for core.audio_bus.AudioBus.

Tests drive the bus via ``push()`` and skip the PyAudio producer
thread entirely — lifecycle tests use a mock pyaudio_factory.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from core.audio_bus import AudioBus


def _bus(**kw) -> AudioBus:
    defaults = dict(rate=16000, chunk_samples=320, history_secs=1.0)
    defaults.update(kw)
    return AudioBus(**defaults)


class TestSubscribeDistribute:
    def test_two_subscribers_both_receive_all_chunks(self) -> None:
        bus = _bus()
        q1 = bus.subscribe("one")
        q2 = bus.subscribe("two")
        for i in range(3):
            bus.push(bytes([i]))
        assert [q1.get_nowait() for _ in range(3)] == [b"\x00", b"\x01", b"\x02"]
        assert [q2.get_nowait() for _ in range(3)] == [b"\x00", b"\x01", b"\x02"]

    def test_unsubscribed_consumer_stops_receiving(self) -> None:
        bus = _bus()
        q = bus.subscribe("one")
        bus.push(b"a")
        bus.unsubscribe("one")
        bus.push(b"b")
        assert q.get_nowait() == b"a"
        assert q.empty()

    def test_no_subscribers_push_is_noop(self) -> None:
        bus = _bus()
        bus.push(b"lost")  # should not raise

    def test_duplicate_subscribe_raises(self) -> None:
        bus = _bus()
        bus.subscribe("same")
        with pytest.raises(ValueError):
            bus.subscribe("same")

    def test_unsubscribe_unknown_is_noop(self) -> None:
        bus = _bus()
        bus.unsubscribe("nonexistent")

    def test_subscribers_list_reflects_state(self) -> None:
        bus = _bus()
        bus.subscribe("a")
        bus.subscribe("b")
        assert set(bus.subscribers()) == {"a", "b"}
        bus.unsubscribe("a")
        assert bus.subscribers() == ["b"]


class TestHistoryPriming:
    def test_subscribe_with_history_replays_ring_buffer(self) -> None:
        # Ring holds ~1s at 16000Hz / 320 samples = 50 chunks.
        bus = _bus()
        for i in range(10):
            bus.push(bytes([i]))
        q = bus.subscribe("late", history_secs=1.0)
        # Should have primed with all 10 chunks still in the ring.
        drained = []
        while not q.empty():
            drained.append(q.get_nowait())
        assert drained == [bytes([i]) for i in range(10)]

    def test_history_truncated_to_ring_capacity(self) -> None:
        # ring_capacity = int(0.1 * 16000 / 320) = 5 chunks
        bus = _bus(history_secs=0.1)
        for i in range(20):
            bus.push(bytes([i]))
        q = bus.subscribe("late", history_secs=10.0)  # asks for more than ring holds
        drained = []
        while not q.empty():
            drained.append(q.get_nowait())
        assert drained == [bytes([i]) for i in range(15, 20)]

    def test_history_zero_gives_no_prime(self) -> None:
        bus = _bus()
        bus.push(b"a")
        bus.push(b"b")
        q = bus.subscribe("fresh", history_secs=0.0)
        assert q.empty()
        bus.push(b"c")
        assert q.get_nowait() == b"c"

    def test_history_partial_keeps_suffix(self) -> None:
        # Ring holds 50 chunks at history_secs=1.0, rate=16000, chunk=320.
        # Ask for 0.2s = 10 chunks — should get the last 10.
        bus = _bus()
        for i in range(30):
            bus.push(bytes([i]))
        q = bus.subscribe("partial", history_secs=0.2)
        drained = []
        while not q.empty():
            drained.append(q.get_nowait())
        assert drained == [bytes([i]) for i in range(20, 30)]


class TestSlowConsumerIsolation:
    def test_slow_consumer_drops_chunks_without_blocking_others(self) -> None:
        bus = _bus()
        fast = bus.subscribe("fast", maxsize=1000)
        slow = bus.subscribe("slow", maxsize=3)  # deliberately tiny
        for i in range(100):
            bus.push(bytes([i % 256]))
        # Fast consumer got every chunk.
        assert fast.qsize() == 100
        # Slow consumer's queue capped at its maxsize and still has the
        # most-recent chunks (dropping is oldest-first per _distribute).
        assert slow.qsize() == 3
        drained = []
        while not slow.empty():
            drained.append(slow.get_nowait())
        # Last 3 chunks pushed → values 97, 98, 99
        assert drained == [bytes([97]), bytes([98]), bytes([99])]

    def test_producer_not_blocked_by_slow_consumer(self) -> None:
        # If push() ever blocked on a slow queue, pushing 1000 chunks
        # with a maxsize=1 consumer would stall. Assert it finishes fast.
        bus = _bus()
        bus.subscribe("slow", maxsize=1)
        start = time.perf_counter()
        for _ in range(1000):
            bus.push(b"x")
        elapsed = time.perf_counter() - start
        assert elapsed < 0.5  # should be milliseconds, half-second is loose guard


class TestWakeDrainToNewest:
    """The wake loop (core/wake_loop.py) subscribes with a shallow maxsize=4
    queue and drains to the newest chunk each iteration so detection stays
    current under a transient over-budget stall, instead of scoring up to
    ~10s of stale backlog from the old 128-deep FIFO."""

    @staticmethod
    def _drain_to_newest(q):
        """Mirror of the wake-loop consumer: blocking get, then drain any
        backlog and keep only the newest chunk, counting skips."""
        import queue as _q

        raw = q.get(timeout=0.5)
        skipped = 0
        while True:
            try:
                raw = q.get_nowait()
                skipped += 1
            except _q.Empty:
                break
        return raw, skipped

    def test_drain_keeps_consumer_on_newest_chunk(self) -> None:
        bus = _bus()
        wake = bus.subscribe("wake", maxsize=4)
        # Audio arrived far faster than the (stalled) consumer could score.
        for i in range(50):
            bus.push(bytes([i]))
        # Bus drop-oldest leaves the last 4 (46,47,48,49); drain-to-newest
        # then yields the FRESHEST chunk (49), not a ~10s-stale one.
        raw, skipped = self._drain_to_newest(wake)
        assert raw == bytes([49])
        assert skipped == 3  # got 46 via get(), then drained 47,48,49
        assert wake.empty()

    def test_no_skip_when_consumer_keeps_up(self) -> None:
        # When only one chunk is queued, the drain skips nothing and there
        # is no realign churn (so steady-state predict jitter is unaffected).
        bus = _bus()
        wake = bus.subscribe("wake", maxsize=4)
        bus.push(bytes([7]))
        raw, skipped = self._drain_to_newest(wake)
        assert raw == bytes([7])
        assert skipped == 0

    def test_shallow_queue_bounds_backlog(self) -> None:
        # The whole point: the wake queue can never hoard seconds of audio.
        bus = _bus()
        wake = bus.subscribe("wake", maxsize=4)
        for i in range(1000):
            bus.push(bytes([i % 256]))
        assert wake.qsize() <= 4  # bounded to ~320ms at 80ms/chunk, not ~10s


class TestLifecycle:
    def test_start_opens_stream_via_factory_and_resolver(self) -> None:
        mock_stream = MagicMock()
        # First few reads return canned chunks; then block (simulate mic).
        read_calls: list[bytes] = [b"\x00" * 640, b"\x01" * 640]

        def read_side_effect(*args, **kwargs):
            if read_calls:
                return read_calls.pop(0)
            time.sleep(0.01)
            return b"\x02" * 640

        mock_stream.read.side_effect = read_side_effect
        mock_pa = MagicMock()
        mock_pa.open.return_value = mock_stream

        bus = AudioBus(
            rate=16000,
            chunk_samples=320,
            history_secs=1.0,
            pyaudio_factory=lambda: mock_pa,
            device_index_resolver=lambda: 7,
        )
        try:
            bus.start()
            assert bus.is_running
            # PyAudio.open was called with our resolved device_index
            mock_pa.open.assert_called_once()
            kwargs = mock_pa.open.call_args.kwargs
            assert kwargs["input_device_index"] == 7
            assert kwargs["rate"] == 16000
            assert kwargs["frames_per_buffer"] == 320

            q = bus.subscribe("c")
            chunk = q.get(timeout=1)
            assert isinstance(chunk, bytes) and len(chunk) == 640
        finally:
            bus.stop()

        assert not bus.is_running
        mock_stream.stop_stream.assert_called()
        mock_stream.close.assert_called()
        mock_pa.terminate.assert_called()

    def test_start_twice_raises(self) -> None:
        mock_pa = MagicMock()
        mock_pa.open.return_value = MagicMock(read=MagicMock(return_value=b"\x00" * 640))
        bus = AudioBus(
            chunk_samples=320,
            rate=16000,
            pyaudio_factory=lambda: mock_pa,
            device_index_resolver=lambda: None,
        )
        try:
            bus.start()
            with pytest.raises(RuntimeError):
                bus.start()
        finally:
            bus.stop()

    def test_stop_without_start_is_safe(self) -> None:
        bus = _bus()
        bus.stop()  # should not raise

    def test_resolver_returning_none_omits_device_index(self) -> None:
        mock_stream = MagicMock(read=MagicMock(return_value=b"\x00" * 640))
        mock_pa = MagicMock()
        mock_pa.open.return_value = mock_stream
        bus = AudioBus(
            chunk_samples=320,
            rate=16000,
            pyaudio_factory=lambda: mock_pa,
            device_index_resolver=lambda: None,
        )
        try:
            bus.start()
            kwargs = mock_pa.open.call_args.kwargs
            assert "input_device_index" not in kwargs
        finally:
            bus.stop()


class TestSnapshotHistory:
    """Coverage for the wake-word concat path (Phase 2d) — lets callers
    grab the ring contents without registering a subscriber."""

    def test_returns_last_n_seconds(self) -> None:
        bus = _bus()
        for i in range(10):
            bus.push(bytes([i]))
        # rate=16000, chunk_samples=320 → 50 chunks/sec → 0.2s = 10 chunks
        snap = bus.snapshot_history(0.2)
        assert snap == [bytes([i]) for i in range(10)]

    def test_truncated_to_ring_capacity(self) -> None:
        bus = _bus(history_secs=0.1)  # 5 chunks
        for i in range(20):
            bus.push(bytes([i]))
        snap = bus.snapshot_history(10.0)  # ask for more than ring holds
        assert snap == [bytes([i]) for i in range(15, 20)]

    def test_zero_seconds_returns_empty(self) -> None:
        bus = _bus()
        bus.push(b"a")
        assert bus.snapshot_history(0.0) == []

    def test_negative_seconds_returns_empty(self) -> None:
        bus = _bus()
        bus.push(b"a")
        assert bus.snapshot_history(-1.0) == []

    def test_does_not_register_subscriber(self) -> None:
        bus = _bus()
        bus.push(b"a")
        bus.snapshot_history(1.0)
        assert bus.subscribers() == []
