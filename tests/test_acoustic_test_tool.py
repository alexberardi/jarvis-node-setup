"""Log parsing for tools/acoustic_test (pure functions, no network or ssh)."""

from __future__ import annotations

import pytest

from tools.acoustic_test.log_parser import (
    classify,
    estimate_clock_offset,
    line_timestamp,
    parse_node_log,
    parse_server_log,
)
from tools.acoustic_test.mic_level import parse_levels

# Lines as the node logs them for one answered turn.
ANSWERED = """\
2026-10-12 19:17:52,032 [INFO] Wake fired | {'score': 0.613, 'oww_score': 0.613, 'pre_wake_speech_seconds': 0.08}
2026-10-12 19:17:53,392 [INFO] Recording complete | {'duration': '1.68s', 'hit_max': False}
2026-10-12 19:17:53,904 [INFO] Transcription received | {'text': "Jarvis, what's the time?"}
2026-10-12 19:17:53,934 [INFO] Sending unified voice command | {'command': "Jarvis, what's the time?"}
2026-10-12 19:17:55,282 [INFO] Speaking 'It's 7:17 PM.' via command-center TTS proxy
2026-10-12 19:18:01,876 [INFO] wake-cycle COMPLETE | total=9845ms from wake_fired to return-to-idle
"""


def test_answered_turn():
    r = parse_node_log(ANSWERED)
    assert r["woke"] is True
    assert r["score"] == pytest.approx(0.613)
    assert r["rec"] == pytest.approx(1.68) and r["hit_max"] is False
    assert r["heard"] == "Jarvis, what's the time?"
    assert r["said"] == "It's 7:17 PM."
    assert r["path"] == "local"
    assert r["rec_to_audio"] == pytest.approx(1.89, abs=0.01)   # 53,392 -> 55,282
    assert r["send_to_audio"] == pytest.approx(1.35, abs=0.01)
    assert classify(r) == "answered"


def test_no_wake_is_a_miss():
    r = parse_node_log("2026-10-12 19:20:00,000 [INFO] Memory watchdog | {}\n")
    assert r == {"woke": False}
    assert classify(r) == "miss"


def test_not_for_me_is_dropped():
    log = ANSWERED.split("Speaking")[0].rsplit("\n", 1)[0] + (
        "\n2026-10-12 19:17:55,000 [INFO] not-for-me | silent abort\n"
    )
    r = parse_node_log(log)
    assert r["path"] == "not_for_me"
    assert classify(r) == "dropped"


def test_not_for_me_after_a_reply_is_the_follow_up_window_not_a_drop():
    log = ANSWERED.replace(
        "2026-10-12 19:18:01,876 [INFO] wake-cycle COMPLETE",
        "2026-10-12 19:18:01,000 [INFO] not-for-me | silent abort\n2026-10-12 19:18:01,876 [INFO] wake-cycle COMPLETE",
    )
    r = parse_node_log(log)
    assert r["said"] == "It's 7:17 PM." and r["path"] == "local"
    assert classify(r) == "answered"


def test_streamed_audio_path_and_speech_length():
    log = ANSWERED.replace(
        "Speaking 'It's 7:17 PM.' via command-center TTS proxy",
        "PCM stream starting | {}",
    ) + "2026-10-12 19:18:01,000 [INFO] PCM stream complete | {'duration_s': 3.4}\n"
    r = parse_node_log(log)
    assert r["path"] == "stream" and r["speech_s"] == pytest.approx(3.4)


def test_errors_are_collected():
    log = ANSWERED + "2026-10-12 19:18:02,000 [ERROR] something failed badly\n"
    assert parse_node_log(log)["errors"] == ["[ERROR] something failed badly"]


def test_error_result_classifies_as_error():
    assert classify({"error": "speaker: down", "woke": False}) == "error"


def test_timestamp():
    assert line_timestamp("not a log line") is None
    a = line_timestamp("2026-10-12 19:17:52,032 x")
    b = line_timestamp("2026-10-12 19:17:53,032 x")
    assert b - a == pytest.approx(1.0)


def test_server_log():
    text = (
        "INFO Router predicted tool=answer_question score=0.97\n"
        "INFO 🔎 wake-verify | verdict=verified node_id=n conversation_id=abc transcript='hey jarvis' phrase='jarvis' x\n"
        "INFO llm_stream_first_token=412ms\n"
        "INFO Returning 1 messages (stop_reason=end_turn)\n"
    )
    r = parse_server_log(text)
    assert (r["route"], r["rscore"]) == ("answer_question", 0.97)
    assert (r["verify"], r["clip"]) == ("verified", "hey jarvis")
    assert r["first_token_ms"] == 412 and r["stop"] == "end_turn"


def test_mic_levels():
    text = "[Parsed_volumedetect_0 @ 0x1] mean_volume: -32.4 dB\n[Parsed_volumedetect_0 @ 0x1] max_volume: -19.4 dB\n"
    assert parse_levels(text) == ("-32.4", "-19.4")
    assert parse_levels("") == ("?", "?")


class TestExpectedSilence:
    """Chatter (expect_answer false) must be reported by what the node did with it."""

    def test_answered_chatter_is_a_false_answer(self):
        r = parse_node_log(ANSWERED)
        r["expect_answer"] = False
        assert classify(r) == "false_answer"

    def test_unwoken_chatter_is_ignored(self):
        assert classify({"woke": False, "expect_answer": False}) == "ignored"

    def test_woken_but_dropped_chatter_is_ignored(self):
        assert classify({"woke": True, "path": "not_for_me", "expect_answer": False}) == "ignored"

    def test_errors_stay_errors(self):
        assert classify({"error": "speaker: down", "expect_answer": False}) == "error"

    def test_missing_expect_answer_defaults_to_expecting_one(self):
        assert classify({"woke": False}) == "miss"
        assert classify({"woke": True, "path": "not_for_me"}) == "dropped"


class TestClockOffset:
    def test_remote_ahead(self):
        assert estimate_clock_offset(1010.0, 99.0, 101.0) == pytest.approx(910.0)

    def test_remote_behind(self):
        assert estimate_clock_offset(95.0, 100.0, 102.0) == pytest.approx(-6.0)

    def test_same_clock(self):
        assert estimate_clock_offset(100.5, 100.0, 101.0) == pytest.approx(0.0)
