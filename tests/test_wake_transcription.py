"""Tests for the wake-transcription module — the audio→STT→CC pipeline.

After the wake-word fires, this module owns the round trip from
recorded audio to a final command result:

  1. Snapshot the wake-word audio from the bus ring buffer
     (``try_capture_wake_audio``) so the speaker pass can score the
     full "Hey Jarvis <command>" clip rather than the bare command.
  2. Concat that wake snapshot with the just-recorded command audio
     for the speaker pass (``try_build_speaker_audio``). ECAPA needs
     the longer clip; Whisper still transcribes the command-only file.
  3. Send everything to STT (``send_for_transcription``), then walk
     the result through non-speech filtering, false-wake detection,
     command-center round-trip, and TTS-out.
  4. ``speak_error`` is the shared error-path TTS that also flashes
     the LED red and falls back to a bundled error chime if TTS
     itself is unreachable.
  5. ``make_validation_handler`` builds a closure CC calls when it
     needs the user to disambiguate a parameter (e.g. "which timer?").

Module-level state ``_last_speaker_user_id`` / ``_last_speaker_confidence``
is written by a successful ``send_for_transcription`` and read by the
warmup-thread args in voice_listener. Exposed via ``get_last_speaker()``.

Tests mock every external surface (STT, CC, TTS, platform_audio, bus)
so the suite is hermetic.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from clients.responses.jarvis_command_center import ValidationRequest
from core import wake_transcription
from core.ijarvis_speech_to_text_provider import TranscriptionResult
from scripts.speech_to_text import RecordingResult


# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_wake_transcription(monkeypatch, tmp_path):
    """Per-test: redirect WAV path constants to tmp_path, scrub the
    module-level speaker state, point the errors dir at tmp_path."""
    monkeypatch.setattr(wake_transcription, "_WAKE_AUDIO_PATH", tmp_path / "wake.wav")
    monkeypatch.setattr(wake_transcription, "_SPEAKER_AUDIO_PATH", tmp_path / "speaker.wav")
    monkeypatch.setattr(wake_transcription, "_ERRORS_DIR", tmp_path / "errors")
    wake_transcription._last_speaker_user_id = None
    wake_transcription._last_speaker_confidence = None
    # Silence the LED — the function calls it from many sites and we
    # don't want a stray import attempt to spew warnings in tests.
    monkeypatch.setattr(wake_transcription, "set_led_transient", lambda p: None)


def _recording(path: str, *, duration: float = 1.2, hit_max: bool = False) -> RecordingResult:
    return RecordingResult(path, duration, hit_max)


def _validation(question: str, options: list[str] | None = None) -> ValidationRequest:
    return ValidationRequest(
        question=question,
        parameter_name="param",
        options=options,
        tool_call_id="tc-1",
    )


# ---------------------------------------------------------------------------
# try_capture_wake_audio — bus → WAV snapshot
# ---------------------------------------------------------------------------


class TestTryCaptureWakeAudio:

    def test_snapshot_success_returns_path(self, monkeypatch):
        bus = MagicMock()
        monkeypatch.setattr(
            wake_transcription, "snapshot_bus_to_wav",
            lambda bus, secs, out_path: True,
        )
        result = wake_transcription.try_capture_wake_audio(bus)
        assert result == str(wake_transcription._WAKE_AUDIO_PATH)

    def test_snapshot_returns_false_returns_none(self, monkeypatch):
        bus = MagicMock()
        monkeypatch.setattr(
            wake_transcription, "snapshot_bus_to_wav",
            lambda bus, secs, out_path: False,
        )
        assert wake_transcription.try_capture_wake_audio(bus) is None

    def test_snapshot_raises_returns_none(self, monkeypatch):
        bus = MagicMock()

        def boom(*a, **kw):
            raise RuntimeError("bus gone")
        monkeypatch.setattr(wake_transcription, "snapshot_bus_to_wav", boom)
        # Must not raise — failure is non-fatal, STT still runs without
        # the speaker-pass clip.
        assert wake_transcription.try_capture_wake_audio(bus) is None


# ---------------------------------------------------------------------------
# try_capture_wake_audio_from_frames — consumed-chunks → WAV (primary path)
# ---------------------------------------------------------------------------


class TestTryCaptureWakeAudioFromFrames:

    def test_frames_written_returns_path(self, monkeypatch):
        bus = MagicMock()
        written: dict = {}

        def _write(path, frames, b):
            written["path"] = path
            written["frames"] = frames

        monkeypatch.setattr(wake_transcription, "write_frames_to_wav", _write)
        result = wake_transcription.try_capture_wake_audio_from_frames(
            [b"aa", b"bb"], bus,
        )
        assert result == str(wake_transcription._WAKE_AUDIO_PATH)
        assert written["path"] == str(wake_transcription._WAKE_AUDIO_PATH)
        assert written["frames"] == [b"aa", b"bb"]

    def test_empty_frames_returns_none(self, monkeypatch):
        bus = MagicMock()
        write = MagicMock()
        monkeypatch.setattr(wake_transcription, "write_frames_to_wav", write)
        assert wake_transcription.try_capture_wake_audio_from_frames(
            [], bus,
        ) is None
        write.assert_not_called()

    def test_write_raises_returns_none(self, monkeypatch):
        # Must not raise — the caller falls back to the bus snapshot.
        bus = MagicMock()

        def boom(*a, **kw):
            raise RuntimeError("disk full")

        monkeypatch.setattr(wake_transcription, "write_frames_to_wav", boom)
        assert wake_transcription.try_capture_wake_audio_from_frames(
            [b"aa"], bus,
        ) is None


# ---------------------------------------------------------------------------
# try_build_speaker_audio — concat wake + command WAVs
# ---------------------------------------------------------------------------


class TestTryBuildSpeakerAudio:

    def test_no_wake_path_returns_none(self):
        # No wake snapshot to prepend → fall back to command-only.
        assert wake_transcription.try_build_speaker_audio(None, "/tmp/cmd.wav") is None

    def test_concat_success_returns_speaker_path(self, monkeypatch):
        called: list[tuple[str, str, str]] = []
        monkeypatch.setattr(
            wake_transcription, "concat_wav_files",
            lambda a, b, out: called.append((a, b, out)),
        )
        result = wake_transcription.try_build_speaker_audio(
            "/tmp/wake.wav", "/tmp/cmd.wav",
        )
        assert result == str(wake_transcription._SPEAKER_AUDIO_PATH)
        assert called == [("/tmp/wake.wav", "/tmp/cmd.wav",
                           str(wake_transcription._SPEAKER_AUDIO_PATH))]

    def test_concat_raises_returns_none(self, monkeypatch):
        def boom(*a, **kw):
            raise OSError("disk full")
        monkeypatch.setattr(wake_transcription, "concat_wav_files", boom)
        # Falls back to command-only STT.
        assert wake_transcription.try_build_speaker_audio(
            "/tmp/wake.wav", "/tmp/cmd.wav",
        ) is None


# ---------------------------------------------------------------------------
# speak_error — TTS with error LED + bundled-chime fallback
# ---------------------------------------------------------------------------


class TestSpeakError:

    def test_tts_speaks_message(self, monkeypatch):
        spoken: list[tuple[bool, str]] = []
        fake_tts = MagicMock()
        fake_tts.speak = lambda include_chime, text: spoken.append((include_chime, text))
        monkeypatch.setattr(wake_transcription, "get_tts_provider", lambda: fake_tts)
        led_calls: list[str | None] = []
        monkeypatch.setattr(
            wake_transcription, "set_led_transient",
            lambda p: led_calls.append(p),
        )

        wake_transcription.speak_error("I'm having trouble.")

        assert spoken == [(False, "I'm having trouble.")]
        # LED red then cleared, in order.
        assert led_calls == ["error", None]

    def test_tts_failure_plays_chime(self, monkeypatch):
        # TTS provider raises → fall back to the bundled chime.
        def boom():
            raise RuntimeError("tts unreachable")
        monkeypatch.setattr(wake_transcription, "get_tts_provider", boom)
        wake_transcription._ERRORS_DIR.mkdir()
        chime = wake_transcription._ERRORS_DIR / "error_generic.wav"
        chime.write_bytes(b"chime")
        played: list[str] = []
        monkeypatch.setattr(
            wake_transcription.platform_audio, "play_audio_file",
            lambda path, **kw: played.append(path),
        )

        wake_transcription.speak_error("oops")

        assert played == [str(chime)]

    def test_tts_failure_no_chime_is_silent(self, monkeypatch):
        # TTS unreachable AND no bundled chime present — must not crash.
        def boom():
            raise RuntimeError("tts unreachable")
        monkeypatch.setattr(wake_transcription, "get_tts_provider", boom)
        # _ERRORS_DIR doesn't exist (autouse fixture pointed it at tmp).
        wake_transcription.speak_error("nothing to say")


# ---------------------------------------------------------------------------
# make_validation_handler — closure over (bus, stt_provider)
# ---------------------------------------------------------------------------


class TestMakeValidationHandler:

    def test_speaks_question_and_returns_transcription(self, monkeypatch):
        bus = MagicMock()
        stt_provider = MagicMock()
        stt_provider.transcribe = MagicMock(return_value="five minutes")

        spoken: list[tuple[bool, str]] = []
        fake_tts = MagicMock()
        fake_tts.speak = lambda include_chime, text: spoken.append((include_chime, text))
        monkeypatch.setattr(wake_transcription, "get_tts_provider", lambda: fake_tts)
        monkeypatch.setattr(
            wake_transcription, "listen",
            lambda bus, history_secs: _recording("/tmp/v.wav"),
        )

        handler = wake_transcription.make_validation_handler(bus, stt_provider)
        result = handler(_validation("How long?"))

        assert spoken == [(False, "How long?")]
        stt_provider.transcribe.assert_called_once_with("/tmp/v.wav")
        assert result == "five minutes"

    def test_options_concatenated_into_question(self, monkeypatch):
        bus = MagicMock()
        stt_provider = MagicMock()
        stt_provider.transcribe = MagicMock(return_value="kitchen")

        spoken: list[tuple[bool, str]] = []
        fake_tts = MagicMock()
        fake_tts.speak = lambda include_chime, text: spoken.append((include_chime, text))
        monkeypatch.setattr(wake_transcription, "get_tts_provider", lambda: fake_tts)
        monkeypatch.setattr(
            wake_transcription, "listen",
            lambda bus, history_secs: _recording("/tmp/v.wav"),
        )

        handler = wake_transcription.make_validation_handler(bus, stt_provider)
        handler(_validation("Which room?", options=["kitchen", "office", "bedroom"]))

        assert spoken == [(False, "Which room? Your options are: kitchen, office, bedroom")]

    def test_empty_transcription_returns_fallback(self, monkeypatch):
        bus = MagicMock()
        stt_provider = MagicMock()
        stt_provider.transcribe = MagicMock(return_value=None)

        fake_tts = MagicMock()
        fake_tts.speak = MagicMock()
        monkeypatch.setattr(wake_transcription, "get_tts_provider", lambda: fake_tts)
        monkeypatch.setattr(
            wake_transcription, "listen",
            lambda bus, history_secs: _recording("/tmp/v.wav"),
        )

        handler = wake_transcription.make_validation_handler(bus, stt_provider)
        result = handler(_validation("Which?"))

        # User's input couldn't be transcribed → we speak a friendly
        # fallback so CC gets a string, not None.
        assert result == "I didn't catch that, sorry."


# ---------------------------------------------------------------------------
# send_for_transcription — STT + CC orchestration
# ---------------------------------------------------------------------------


def _stt_returning(result: TranscriptionResult) -> MagicMock:
    stt = MagicMock()
    stt.transcribe_with_speaker = MagicMock(return_value=result)
    return stt


def _stt_raising(exc: Exception) -> MagicMock:
    stt = MagicMock()
    stt.transcribe_with_speaker = MagicMock(side_effect=exc)
    return stt


def _command_service_returning(result):
    cs = MagicMock()
    cs.process_voice_command = MagicMock(return_value=result)
    cs.speak_result = MagicMock()
    return cs


@pytest.fixture
def _no_concat(monkeypatch):
    """Sidestep wake-audio concat; tests that don't care about the
    speaker pass should just see ``speaker_audio_path=None`` reach STT."""
    monkeypatch.setattr(
        wake_transcription, "try_build_speaker_audio",
        lambda wake, cmd: None,
    )


@pytest.fixture
def _stable_config(monkeypatch):
    """Default Config so tests don't depend on env DB state."""
    monkeypatch.setattr(
        wake_transcription.Config, "get_bool",
        lambda key, default: default,
    )


class TestSendForTranscriptionStt:
    """STT round-trip branches."""

    def test_connection_error_speaks_friendly_and_returns_none(self, monkeypatch, _no_concat):
        stt = _stt_raising(ConnectionError("whisper down"))
        cs = MagicMock()
        spoken: list[str] = []
        monkeypatch.setattr(
            wake_transcription, "speak_error",
            lambda msg: spoken.append(msg),
        )
        result = wake_transcription.send_for_transcription(
            recording=_recording("/tmp/cmd.wav"),
            command_service=cs,
            stt_provider=stt,
            validation_handler=lambda v: "",
        )
        assert result is None
        assert spoken == ["I'm having trouble connecting right now."]

    def test_generic_stt_error_speaks_couldnt_understand(self, monkeypatch, _no_concat):
        stt = _stt_raising(RuntimeError("model crashed"))
        cs = MagicMock()
        spoken: list[str] = []
        monkeypatch.setattr(
            wake_transcription, "speak_error",
            lambda msg: spoken.append(msg),
        )
        result = wake_transcription.send_for_transcription(
            recording=_recording("/tmp/cmd.wav"),
            command_service=cs,
            stt_provider=stt,
            validation_handler=lambda v: "",
        )
        assert result is None
        assert spoken == ["I couldn't understand that, sorry."]

    def test_non_speech_silently_returns_none(self, monkeypatch, _no_concat):
        # Whisper noise marker → drop silently (no TTS).
        stt = _stt_returning(TranscriptionResult(text="[BLANK_AUDIO]"))
        cs = MagicMock()
        spoken: list[str] = []
        monkeypatch.setattr(
            wake_transcription, "speak_error",
            lambda msg: spoken.append(msg),
        )
        result = wake_transcription.send_for_transcription(
            recording=_recording("/tmp/cmd.wav"),
            command_service=cs,
            stt_provider=stt,
            validation_handler=lambda v: "",
        )
        assert result is None
        assert spoken == []
        cs.process_voice_command.assert_not_called()

    def test_false_wake_silently_returns_none(self, monkeypatch, _no_concat):
        stt = _stt_returning(
            TranscriptionResult(text="just some narration text here", segments=[])
        )
        cs = MagicMock()
        monkeypatch.setattr(
            wake_transcription, "is_false_wake",
            lambda text, recording, segments: True,
        )
        spoken: list[str] = []
        monkeypatch.setattr(
            wake_transcription, "speak_error",
            lambda msg: spoken.append(msg),
        )
        result = wake_transcription.send_for_transcription(
            recording=_recording("/tmp/cmd.wav"),
            command_service=cs,
            stt_provider=stt,
            validation_handler=lambda v: "",
        )
        assert result is None
        assert spoken == []
        cs.process_voice_command.assert_not_called()

    def test_empty_text_drops_silently_via_non_speech_path(self, monkeypatch, _no_concat):
        # Empty / whitespace text trips ``is_non_speech`` first, so it
        # exits silently via the non-speech branch — the later
        # ``else: speak_error(...)`` branch is dead. Characterizes the
        # current behavior so we'd notice if a future change rerouted
        # empty text through TTS.
        stt = _stt_returning(TranscriptionResult(text=""))
        cs = MagicMock()
        spoken: list[str] = []
        monkeypatch.setattr(
            wake_transcription, "speak_error",
            lambda msg: spoken.append(msg),
        )
        result = wake_transcription.send_for_transcription(
            recording=_recording("/tmp/cmd.wav"),
            command_service=cs,
            stt_provider=stt,
            validation_handler=lambda v: "",
        )
        assert result is None
        assert spoken == []
        cs.process_voice_command.assert_not_called()


class TestSendForTranscriptionCc:
    """Command-center round-trip branches (assuming STT succeeded)."""

    def test_success_calls_process_and_speak_result(self, monkeypatch, _no_concat, _stable_config):
        stt = _stt_returning(TranscriptionResult(
            text="what time is it",
            speaker_user_id=42,
            speaker_confidence=0.93,
        ))
        cs = _command_service_returning({"reply": "It's 5pm."})
        monkeypatch.setattr(
            wake_transcription, "is_false_wake",
            lambda text, recording, segments: False,
        )
        result = wake_transcription.send_for_transcription(
            recording=_recording("/tmp/cmd.wav"),
            command_service=cs,
            stt_provider=stt,
            validation_handler=lambda v: "",
            conversation_id="conv-1",
        )
        assert result == {"reply": "It's 5pm."}
        cs.process_voice_command.assert_called_once()
        cs.speak_result.assert_called_once_with({"reply": "It's 5pm."})

    def test_success_updates_module_speaker_state(self, monkeypatch, _no_concat, _stable_config):
        stt = _stt_returning(TranscriptionResult(
            text="what time",
            speaker_user_id=42,
            speaker_confidence=0.93,
        ))
        cs = _command_service_returning({"reply": "ok"})
        monkeypatch.setattr(
            wake_transcription, "is_false_wake",
            lambda text, recording, segments: False,
        )
        # Before: no speaker known.
        assert wake_transcription.get_last_speaker() == (None, None)

        wake_transcription.send_for_transcription(
            recording=_recording("/tmp/cmd.wav"),
            command_service=cs,
            stt_provider=stt,
            validation_handler=lambda v: "",
        )

        assert wake_transcription.get_last_speaker() == (42, 0.93)

    def test_no_speaker_id_does_not_update_state(self, monkeypatch, _no_concat, _stable_config):
        # Successful transcription but speaker pass didn't ID anyone —
        # don't overwrite the previous speaker (a follow-up may still
        # benefit from the prior turn's identification).
        wake_transcription._last_speaker_user_id = 7
        wake_transcription._last_speaker_confidence = 0.5
        stt = _stt_returning(TranscriptionResult(text="hi", speaker_user_id=None))
        cs = _command_service_returning({})
        monkeypatch.setattr(
            wake_transcription, "is_false_wake",
            lambda text, recording, segments: False,
        )
        wake_transcription.send_for_transcription(
            recording=_recording("/tmp/cmd.wav"),
            command_service=cs,
            stt_provider=stt,
            validation_handler=lambda v: "",
        )
        # State preserved.
        assert wake_transcription.get_last_speaker() == (7, 0.5)

    def test_cc_connection_error_speaks_friendly_and_returns_none(self, monkeypatch, _no_concat, _stable_config):
        stt = _stt_returning(TranscriptionResult(text="set a timer"))
        cs = MagicMock()
        cs.process_voice_command = MagicMock(side_effect=ConnectionError("cc down"))
        monkeypatch.setattr(
            wake_transcription, "is_false_wake",
            lambda text, recording, segments: False,
        )
        spoken: list[str] = []
        monkeypatch.setattr(
            wake_transcription, "speak_error",
            lambda msg: spoken.append(msg),
        )
        result = wake_transcription.send_for_transcription(
            recording=_recording("/tmp/cmd.wav"),
            command_service=cs,
            stt_provider=stt,
            validation_handler=lambda v: "",
        )
        assert result is None
        assert spoken == ["I can't reach my server right now."]

    def test_cc_generic_error_speaks_something_went_wrong(self, monkeypatch, _no_concat, _stable_config):
        stt = _stt_returning(TranscriptionResult(text="set a timer"))
        cs = MagicMock()
        cs.process_voice_command = MagicMock(side_effect=RuntimeError("boom"))
        monkeypatch.setattr(
            wake_transcription, "is_false_wake",
            lambda text, recording, segments: False,
        )
        spoken: list[str] = []
        monkeypatch.setattr(
            wake_transcription, "speak_error",
            lambda msg: spoken.append(msg),
        )
        result = wake_transcription.send_for_transcription(
            recording=_recording("/tmp/cmd.wav"),
            command_service=cs,
            stt_provider=stt,
            validation_handler=lambda v: "",
        )
        assert result is None
        assert spoken == ["Something went wrong, sorry about that."]


class TestSendForTranscriptionAckPath:
    """Skip-ack threading: audio_acks_disabled OR caller skip_ack."""

    def test_audio_acks_disabled_forces_skip_ack(self, monkeypatch, _no_concat):
        # Config flag off → process_voice_command must receive
        # skip_ack=True even though the caller passed False.
        monkeypatch.setattr(
            wake_transcription.Config, "get_bool",
            lambda key, default: False if key == "wake_ack_audio_enabled" else default,
        )
        stt = _stt_returning(TranscriptionResult(text="hi"))
        captured: dict = {}

        def capture(*a, **kw):
            captured.update(kw)
            return {}
        cs = MagicMock()
        cs.process_voice_command = capture
        cs.speak_result = MagicMock()
        monkeypatch.setattr(
            wake_transcription, "is_false_wake",
            lambda text, recording, segments: False,
        )
        wake_transcription.send_for_transcription(
            recording=_recording("/tmp/cmd.wav"),
            command_service=cs,
            stt_provider=stt,
            validation_handler=lambda v: "",
            skip_ack=False,
        )
        assert captured["skip_ack"] is True

    def test_caller_skip_ack_is_honored(self, monkeypatch, _no_concat, _stable_config):
        stt = _stt_returning(TranscriptionResult(text="hi"))
        captured: dict = {}

        def capture(*a, **kw):
            captured.update(kw)
            return {}
        cs = MagicMock()
        cs.process_voice_command = capture
        cs.speak_result = MagicMock()
        monkeypatch.setattr(
            wake_transcription, "is_false_wake",
            lambda text, recording, segments: False,
        )
        wake_transcription.send_for_transcription(
            recording=_recording("/tmp/cmd.wav"),
            command_service=cs,
            stt_provider=stt,
            validation_handler=lambda v: "",
            skip_ack=True,
        )
        assert captured["skip_ack"] is True
