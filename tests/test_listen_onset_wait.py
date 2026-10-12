"""``listen_onset_wait_secs``: do not stop on silence before speech has started.

Someone who pauses after the wake word was cut off before they began speaking,
because the end-of-speech silence rule starts counting from the first chunk.
With the option on, the silence stop is held back until speech is heard in the
live frames (or the wait elapses). Off (the default) is the original behaviour.
"""

from __future__ import annotations

import queue
import struct

import pytest

from scripts import speech_to_text

RATE = 16000
CHUNK = 1600                  # 0.1 s
CHUNK_SECS = CHUNK / RATE
SILENCE_THRESHOLD = 300


def _chunk(amplitude: int) -> bytes:
    return struct.pack(f"<{CHUNK}h", *([amplitude] * CHUNK))


QUIET, LOUD = _chunk(10), _chunk(3000)


class _FakeBus:
    rate = RATE
    chunk_samples = CHUNK
    channels = 1
    sample_width = 2

    def __init__(self, chunks):
        self._q: queue.Queue = queue.Queue()
        for c in chunks:
            self._q.put(c)

    def subscribe(self, name, history_secs=0.0):
        return self._q

    def unsubscribe(self, name):
        pass


@pytest.fixture
def recorder(monkeypatch, tmp_path):
    """Run listen() over scripted chunks; returns the number of chunks recorded."""
    monkeypatch.setattr(speech_to_text, "_cache_dir", tmp_path)
    monkeypatch.setattr(speech_to_text, "_write_wav", lambda path, frames, bus: None)
    monkeypatch.setattr(speech_to_text, "_maybe_normalize", lambda frames: frames)

    def run(chunks, onset_wait=0.0):
        monkeypatch.setattr(
            speech_to_text.Config, "get_float",
            lambda key, default: onset_wait if key == "listen_onset_wait_secs" else default,
        )
        result = speech_to_text.listen(
            _FakeBus(chunks),
            silence_threshold=SILENCE_THRESHOLD,
            silence_duration=0.35,     # 3 chunks of silence end the recording (0.35 / 0.1 = 3.5, floored)
            min_record_secs=0.2,
            max_record_secs=5.0,
            quiet_wait_secs=0.0,
        )
        return round(result.duration / CHUNK_SECS)

    return run


def test_off_by_default_stops_on_leading_silence(recorder):
    # A pause of a second before speaking: the original rule gives up after 3 quiet chunks.
    chunks = [QUIET] * 10 + [LOUD] * 5 + [QUIET] * 5
    assert recorder(chunks) == 3


def test_onset_wait_keeps_recording_until_speech_starts(recorder):
    chunks = [QUIET] * 10 + [LOUD] * 5 + [QUIET] * 5
    # Waits through the 1 s pause, records the speech, then stops on 3 quiet chunks after it.
    assert recorder(chunks, onset_wait=2.0) == 18


def test_onset_wait_gives_up_when_no_speech_comes(recorder):
    chunks = [QUIET] * 40
    # No speech at all: stops once the wait (0.5 s = 5 chunks) has elapsed, not at max length.
    # Frame 5 is the first one allowed to end the recording, so 6 chunks are kept.
    assert recorder(chunks, onset_wait=0.5) == 6


def test_normal_speech_is_unaffected(recorder):
    chunks = [LOUD] * 5 + [QUIET] * 5
    assert recorder(chunks, onset_wait=2.0) == recorder(chunks, onset_wait=0.0) == 8
