#!/usr/bin/env python3
"""
Full Pipeline Integration Test: TTS -> Whisper (via CC proxy) -> LLM -> Tool Execution

Tests the complete voice path that a real Pi Zero node takes:
1. Generate synthetic audio via TTS
2. Transcribe through Whisper via Command Center proxy (same path as Pi)
3. Send through command center for LLM inference
4. Execute tools on the node side
5. Validate correctness + timing at each step

Unlike test_multi_turn_conversation.py --full which hits TTS/Whisper directly,
this test uses the CC proxy endpoint that the real Pi uses.
"""

import argparse
import json
import os
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests as http_requests
from dotenv import load_dotenv

from clients.jarvis_command_center_client import JarvisCommandCenterClient
from clients.rest_client import RestClient
from clients.responses.jarvis_command_center import DateContext
from core.request_information import RequestInformation
from utils.audio_pipeline_client import AudioPipelineClient
from utils.command_discovery_service import get_command_discovery_service
from utils.config_loader import Config
from utils.tool_result_formatter import format_tool_error, format_tool_result

load_dotenv()


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class ValidationTurn:
    """A user response during a validation/clarification flow."""
    response_text: str
    expected_tool_after: str | None = None


@dataclass
class PipelineTestCase:
    """Single end-to-end pipeline test case."""
    voice_phrase: str
    expected_tool: str | None
    description: str = ""
    category: str = "general"
    expected_params: dict[str, Any] | None = None
    verify_response_contains: str | None = None
    min_transcription_overlap: float = 0.6
    validation_turns: list[ValidationTurn] | None = None
    max_total_time_seconds: float | None = None


@dataclass
class StepTiming:
    """Timing for a single pipeline step."""
    step_name: str
    duration_seconds: float


@dataclass
class PipelineTestResult:
    """Result of a single pipeline test execution."""
    test: PipelineTestCase
    passed: bool
    failure_reason: str | None = None
    step_timings: list[StepTiming] = field(default_factory=list)
    total_time_seconds: float = 0.0
    transcribed_text: str = ""
    transcription_overlap: float = 0.0
    actual_tool: str | None = None
    actual_params: dict[str, Any] | None = None
    final_response: str | None = None
    conversation_id: str = ""


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

def create_test_cases() -> list[PipelineTestCase]:
    """Create the full set of pipeline test cases."""
    return [
        # --- tool_execution ---
        PipelineTestCase(
            voice_phrase="What's 25 plus 37?",
            expected_tool="calculate",
            description="Calculator addition via audio",
            category="tool_execution",
            verify_response_contains="62",
        ),
        PipelineTestCase(
            voice_phrase="What's the weather in Miami?",
            expected_tool="get_weather",
            description="Weather query via audio",
            category="tool_execution",
            expected_params={"city": "Miami"},
        ),
        PipelineTestCase(
            voice_phrase="Tell me a joke",
            expected_tool="tell_joke",
            description="Joke request via audio",
            category="tool_execution",
        ),
        PipelineTestCase(
            voice_phrase="Set a timer for 5 minutes",
            expected_tool="set_timer",
            description="Timer request via audio",
            category="tool_execution",
        ),
        PipelineTestCase(
            voice_phrase="How did the Yankees do?",
            expected_tool="get_sports_scores",
            description="Sports score query via audio",
            category="tool_execution",
            expected_params={"team_name": "Yankees"},
        ),
        PipelineTestCase(
            voice_phrase="How many feet in a mile?",
            expected_tool="convert_measurement",
            description="Unit conversion via audio",
            category="tool_execution",
            verify_response_contains="5280",
        ),

        # --- validation ---
        PipelineTestCase(
            voice_phrase="Set a timer",
            expected_tool=None,
            description="Incomplete timer triggers validation",
            category="validation",
            validation_turns=[
                ValidationTurn(
                    response_text="5 minutes",
                    expected_tool_after="set_timer",
                ),
            ],
        ),

        # --- conversational ---
        PipelineTestCase(
            voice_phrase="What is the capital of France?",
            expected_tool=None,  # server-side tool — handled internally
            description="Knowledge question via audio",
            category="conversational",
            # Note: Qwen 2.5 7B 8-bit cannot recall factual knowledge;
            # test validates the flow completes without looping.
        ),

        # --- accuracy ---
        PipelineTestCase(
            voice_phrase="What's the weather in Albuquerque?",
            expected_tool="get_weather",
            description="Tricky city name transcription accuracy",
            category="accuracy",
            min_transcription_overlap=0.5,
        ),
        PipelineTestCase(
            voice_phrase="Calculate 1234 divided by 56",
            expected_tool="calculate",
            description="Tricky numbers via audio",
            category="accuracy",
        ),

        # --- performance ---
        PipelineTestCase(
            voice_phrase="What's 10 plus 5?",
            expected_tool="calculate",
            description="Simple calculation performance check",
            category="performance",
            max_total_time_seconds=20.0,
            verify_response_contains="15",
        ),
    ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _compute_word_overlap(original: str, transcribed: str) -> float:
    """Compute word overlap ratio between original and transcribed text.

    Mirrors AudioPipelineClient.verify_transcription_accuracy logic but
    returns the ratio instead of a boolean.
    """
    if not original or not transcribed:
        return 0.0

    stop_words = {"a", "an", "the", "is", "are", "what", "how", "please"}
    original_words = set(original.lower().split()) - stop_words
    transcribed_words = set(transcribed.lower().split()) - stop_words

    if not original_words:
        return 1.0

    overlap = len(original_words & transcribed_words)
    return overlap / len(original_words)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

class FullPipelineTestRunner:
    """Executes full pipeline tests: TTS -> Whisper (CC proxy) -> LLM -> Tools."""

    def __init__(
        self,
        jcc_client: JarvisCommandCenterClient,
        commands: dict,
        date_context: DateContext,
        audio_client: AudioPipelineClient,
        tts_url: str,
        app_auth_headers: dict[str, str],
        save_audio_dir: str | None = None,
        timing_threshold: float | None = None,
    ):
        self.jcc_client = jcc_client
        self.commands = commands
        self.date_context = date_context
        self.audio_client = audio_client
        self.cc_url = jcc_client.base_url
        self.tts_url = tts_url
        self.app_auth_headers = app_auth_headers
        self.save_audio_dir = save_audio_dir
        self.timing_threshold = timing_threshold

    # -- public --

    def run_test(self, test: PipelineTestCase) -> PipelineTestResult:
        """Execute a single full-pipeline test."""
        conversation_id = str(uuid.uuid4())
        result = PipelineTestResult(
            test=test, passed=False, conversation_id=conversation_id
        )

        print(f"\n{'─'*60}")
        print(f"  Testing: {test.description}")
        print(f"  Phrase:  \"{test.voice_phrase}\"")
        print(f"  Category: {test.category}  |  Conv: {conversation_id[:8]}...")

        pipeline_start = time.time()

        try:
            # Production flow: wake word fires warmup FIRST, then user
            # speaks while the KV cache warms up.  TTS + Whisper simulate
            # the user speaking — they run in parallel with warmup.

            # Step 1: Fire conversation warmup in background (mirrors wake-word trigger)
            warmup_t0 = time.time()
            warmup_result: dict[str, Any] = {"success": False, "error": None}

            def _warmup() -> None:
                try:
                    warmup_result["success"] = self.jcc_client.start_conversation(
                        conversation_id, self.commands, self.date_context
                    )
                except Exception as exc:
                    warmup_result["error"] = str(exc)

            warmup_thread = threading.Thread(target=_warmup, daemon=True)
            warmup_thread.start()

            # Step 2: TTS (runs while warmup is in progress)
            t0 = time.time()
            save_name = f"{conversation_id[:8]}_{test.category}" if self.save_audio_dir else None
            audio_bytes = self._generate_tts(test.voice_phrase, save_name=save_name)
            tts_dur = time.time() - t0
            result.step_timings.append(StepTiming("tts", tts_dur))

            if not audio_bytes:
                result.failure_reason = "TTS generation failed"
                self._print_step("TTS", tts_dur, False, result.failure_reason)
                return result
            self._print_step("TTS", tts_dur, True, f"{len(audio_bytes)} bytes")

            # Step 3: Whisper via CC proxy (still overlapping with warmup)
            t0 = time.time()
            transcribed_text = self._transcribe_via_cc_proxy(audio_bytes)
            whisper_dur = time.time() - t0
            result.step_timings.append(StepTiming("whisper", whisper_dur))

            if not transcribed_text:
                result.failure_reason = "Whisper transcription failed (CC proxy)"
                self._print_step("Whisper", whisper_dur, False, result.failure_reason)
                return result

            result.transcribed_text = transcribed_text
            overlap = _compute_word_overlap(test.voice_phrase, transcribed_text)
            result.transcription_overlap = overlap
            self._print_step(
                "Whisper", whisper_dur, True,
                f"\"{transcribed_text}\" (overlap: {overlap:.0%})"
            )

            # Warn on low transcription accuracy but don't fail
            if overlap < test.min_transcription_overlap:
                print(f"    WARN  Transcription overlap low: {overlap:.0%} < {test.min_transcription_overlap:.0%}")

            # Step 4: Wait for warmup to finish (should already be done)
            warmup_thread.join(timeout=15)
            conv_dur = time.time() - warmup_t0
            result.step_timings.append(StepTiming("conversation_start", conv_dur))

            if warmup_result["error"]:
                result.failure_reason = f"Warmup failed: {warmup_result['error']}"
                self._print_step("Conv Start", conv_dur, False, result.failure_reason)
                return result
            if not warmup_result["success"]:
                result.failure_reason = "Failed to start conversation"
                self._print_step("Conv Start", conv_dur, False, result.failure_reason)
                return result

            # How much of the warmup was hidden behind TTS+Whisper?
            user_speech_dur = tts_dur + whisper_dur
            warmup_wait = max(0, conv_dur - user_speech_dur)
            if warmup_wait < 0.1:
                self._print_step("Conv Start", conv_dur, True, f"fully overlapped with audio ({conv_dur:.1f}s)")
            else:
                self._print_step("Conv Start", conv_dur, True, f"{warmup_wait:.1f}s waited after audio")

            # Step 5: LLM inference
            t0 = time.time()
            response = self.jcc_client.send_command(transcribed_text, conversation_id)
            llm_dur = time.time() - t0
            result.step_timings.append(StepTiming("llm_inference", llm_dur))

            if not response:
                result.failure_reason = "No response from LLM"
                self._print_step("LLM", llm_dur, False, result.failure_reason)
                return result
            self._print_step(
                "LLM", llm_dur, True, f"stop_reason={response.stop_reason}"
            )

            # Step 5+6: Tool execution loop
            max_iterations = 5
            iteration = 0
            while response.requires_tool_execution() and response.tool_calls and iteration < max_iterations:
                iteration += 1
                calls = response.tool_calls  # narrowed to non-None by while guard

                # Execute tools
                t0 = time.time()
                tool_results = self._execute_tools(calls, conversation_id, result)
                tool_dur = time.time() - t0
                result.step_timings.append(StepTiming("tool_execution", tool_dur))
                self._print_step("Tool Exec", tool_dur, True, f"iteration {iteration}")

                # Send results back
                t0 = time.time()
                response = self.jcc_client.send_tool_results(conversation_id, tool_results)
                submit_dur = time.time() - t0
                result.step_timings.append(StepTiming("tool_result_submission", submit_dur))

                if not response:
                    result.failure_reason = "No response after tool result submission"
                    self._print_step("Submit", submit_dur, False, result.failure_reason)
                    return result
                self._print_step(
                    "Submit", submit_dur, True,
                    f"stop_reason={response.stop_reason}"
                )

            # Step 7: Validation loop
            if response.requires_validation() and response.validation_request and test.validation_turns:
                validation_req = response.validation_request
                for v_idx, v_turn in enumerate(test.validation_turns):
                    print(f"    ...   Validation turn {v_idx}: \"{v_turn.response_text}\"")

                    t0 = time.time()
                    response = self.jcc_client.send_validation_response(
                        conversation_id, validation_req, v_turn.response_text
                    )
                    val_dur = time.time() - t0
                    result.step_timings.append(StepTiming("validation", val_dur))

                    if not response:
                        result.failure_reason = "No response after validation"
                        self._print_step("Validation", val_dur, False, result.failure_reason)
                        return result
                    self._print_step("Validation", val_dur, True, f"stop_reason={response.stop_reason}")

                    # Tool execution after validation
                    tool_iter = 0
                    while response.requires_tool_execution() and response.tool_calls and tool_iter < max_iterations:
                        tool_iter += 1
                        v_calls = response.tool_calls
                        t0 = time.time()
                        tool_results = self._execute_tools(v_calls, conversation_id, result)
                        tool_dur = time.time() - t0
                        result.step_timings.append(StepTiming("tool_execution", tool_dur))

                        t0 = time.time()
                        response = self.jcc_client.send_tool_results(conversation_id, tool_results)
                        submit_dur = time.time() - t0
                        result.step_timings.append(StepTiming("tool_result_submission", submit_dur))

                        if not response:
                            result.failure_reason = "No response after post-validation tool submission"
                            return result

                    # Check expected tool after validation
                    if v_turn.expected_tool_after and result.actual_tool != v_turn.expected_tool_after:
                        result.failure_reason = (
                            f"After validation expected tool '{v_turn.expected_tool_after}', "
                            f"got '{result.actual_tool}'"
                        )
                        print(f"    FAIL  {result.failure_reason}")
                        return result

            # Capture final response
            if response and response.assistant_message:
                result.final_response = response.assistant_message

            # Step 8: Validate result
            result.total_time_seconds = time.time() - pipeline_start
            self._validate_result(test, result)

        except Exception as e:
            result.failure_reason = f"Exception: {e}"
            result.total_time_seconds = time.time() - pipeline_start
            import traceback
            traceback.print_exc()

        # Print final timing summary for this test
        status = "PASS" if result.passed else "FAIL"
        print(f"  [{status}] Total: {result.total_time_seconds:.2f}s")
        if result.failure_reason:
            print(f"         Reason: {result.failure_reason}")

        return result

    # -- private --

    def _generate_tts(self, text: str, save_name: str | None = None) -> bytes | None:
        """Generate TTS audio using app-to-app auth.

        TTS requires X-Jarvis-App-Id/Key, not node X-API-Key.
        """
        try:
            resp = http_requests.post(
                f"{self.tts_url}/speak",
                json={"text": text},
                headers=self.app_auth_headers,
                timeout=30,
            )
            resp.raise_for_status()
            audio_bytes = resp.content

            if self.save_audio_dir and save_name:
                audio_path = Path(self.save_audio_dir) / f"{save_name}.wav"
                audio_path.write_bytes(audio_bytes)

            return audio_bytes
        except http_requests.RequestException as e:
            print(f"    ...   TTS error: {e}")
            return None

    def _transcribe_via_cc_proxy(self, audio_bytes: bytes) -> str | None:
        """Transcribe audio via Command Center's whisper proxy endpoint.

        This mirrors the real Pi path: node -> CC /api/v0/media/whisper/transcribe.
        """
        url = f"{self.cc_url}/api/v0/media/whisper/transcribe"

        tmp_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                tmp.write(audio_bytes)
                tmp_path = tmp.name

            with open(tmp_path, "rb") as f:
                files: dict[str, Any] = {"file": ("audio.wav", f, "audio/wav")}
                response = RestClient.post(url, files=files, timeout=60)

            if response:
                return (response.get("text") or "").strip()
            return None

        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)

    def _execute_tools(
        self,
        tool_calls: list,
        conversation_id: str,
        result: PipelineTestResult,
    ) -> list[dict[str, Any]]:
        """Execute client-side tools and return formatted results."""
        results: list[dict[str, Any]] = []

        for tc in tool_calls:
            tool_name = tc.function.name
            # Track first tool called (most relevant for validation)
            if result.actual_tool is None:
                result.actual_tool = tool_name

            command = self.commands.get(tool_name)
            if not command:
                print(f"    ...   Unknown tool: {tool_name}")
                results.append(format_tool_error(tc.id, f"Unknown tool: {tool_name}"))
                continue

            try:
                arguments = tc.function.get_arguments_dict()
                if result.actual_params is None:
                    result.actual_params = arguments
                print(f"    ...   Executing: {tool_name}({arguments})")

                request_info = RequestInformation(
                    voice_command=f"Tool call: {tool_name}",
                    conversation_id=conversation_id,
                    is_validation_response=False,
                )
                command_response = command.execute(request_info, **arguments)
                results.append(format_tool_result(tc.id, command_response))
            except Exception as e:
                print(f"    ...   Tool error: {e}")
                results.append(format_tool_error(tc.id, str(e)))

        return results

    def _validate_result(
        self,
        test: PipelineTestCase,
        result: PipelineTestResult,
    ) -> None:
        """Run final validation checks and set result.passed."""

        # Check expected tool
        if test.expected_tool and result.actual_tool != test.expected_tool:
            # Allow if no tool was expected (conversational) or tool matched
            result.failure_reason = (
                f"Expected tool '{test.expected_tool}', got '{result.actual_tool}'"
            )
            print(f"    FAIL  {result.failure_reason}")
            return

        # Check expected params (subset match)
        if test.expected_params and result.actual_params:
            for key, expected_val in test.expected_params.items():
                actual_val = result.actual_params.get(key)
                if isinstance(expected_val, str) and isinstance(actual_val, str):
                    if expected_val.lower() not in actual_val.lower():
                        result.failure_reason = (
                            f"Param '{key}': expected '{expected_val}' in '{actual_val}'"
                        )
                        print(f"    FAIL  {result.failure_reason}")
                        return
                elif expected_val != actual_val:
                    result.failure_reason = (
                        f"Param '{key}': expected {expected_val}, got {actual_val}"
                    )
                    print(f"    FAIL  {result.failure_reason}")
                    return

        # Check verify_response_contains (strip commas for number formatting)
        if test.verify_response_contains and result.final_response:
            expected = test.verify_response_contains.lower().replace(",", "")
            actual = result.final_response.lower().replace(",", "")
            if expected not in actual:
                result.failure_reason = (
                    f"Response missing '{test.verify_response_contains}'"
                )
                print(f"    FAIL  {result.failure_reason}")
                print(f"           Response: {result.final_response}")
                return

        # Check performance threshold
        max_time = self.timing_threshold or test.max_total_time_seconds
        if max_time and result.total_time_seconds > max_time:
            result.failure_reason = (
                f"Too slow: {result.total_time_seconds:.2f}s > {max_time:.1f}s limit"
            )
            print(f"    FAIL  {result.failure_reason}")
            return

        result.passed = True

    @staticmethod
    def _print_step(
        name: str, duration: float, ok: bool, detail: str = ""
    ) -> None:
        """Print a single step's timing result."""
        icon = "  OK " if ok else " FAIL"
        detail_str = f"  {detail}" if detail else ""
        print(f"    {icon}  {name:<14s} {duration:6.2f}s{detail_str}")


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def write_results(results: list[PipelineTestResult], output_file: str) -> None:
    """Write results to JSON file with summary, timing, and accuracy sections."""
    passed = sum(1 for r in results if r.passed)

    # Aggregate timing by step name
    step_totals: dict[str, list[float]] = {}
    for r in results:
        for st in r.step_timings:
            step_totals.setdefault(st.step_name, []).append(st.duration_seconds)

    timing_summary: dict[str, Any] = {}
    for step_name, durations in step_totals.items():
        timing_summary[step_name] = {
            "avg_seconds": sum(durations) / len(durations),
            "min_seconds": min(durations),
            "max_seconds": max(durations),
            "count": len(durations),
        }

    all_totals = [r.total_time_seconds for r in results if r.total_time_seconds > 0]
    if all_totals:
        sorted_totals = sorted(all_totals)
        p95_idx = min(int(len(sorted_totals) * 0.95), len(sorted_totals) - 1)
        timing_summary["_total"] = {
            "avg_seconds": sum(all_totals) / len(all_totals),
            "p95_seconds": sorted_totals[p95_idx],
            "slowest_test": max(results, key=lambda r: r.total_time_seconds).test.description,
        }

    # Find slowest step
    if step_totals:
        slowest_step = max(step_totals.items(), key=lambda kv: sum(kv[1]) / len(kv[1]))
        timing_summary["_slowest_step"] = slowest_step[0]

    # Transcription accuracy stats
    overlaps = [r.transcription_overlap for r in results if r.transcription_overlap > 0]
    transcription_accuracy: dict[str, Any] = {}
    if overlaps:
        transcription_accuracy = {
            "avg_overlap": sum(overlaps) / len(overlaps),
            "min_overlap": min(overlaps),
            "max_overlap": max(overlaps),
            "tests_measured": len(overlaps),
        }

    output = {
        "summary": {
            "total": len(results),
            "passed": passed,
            "failed": len(results) - passed,
            "success_rate": f"{(passed / len(results) * 100):.1f}%" if results else "N/A",
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        },
        "timing_summary": timing_summary,
        "transcription_accuracy": transcription_accuracy,
        "results": [
            {
                "description": r.test.description,
                "category": r.test.category,
                "voice_phrase": r.test.voice_phrase,
                "passed": r.passed,
                "failure_reason": r.failure_reason,
                "total_time_seconds": round(r.total_time_seconds, 3),
                "step_timings": [
                    {"step": st.step_name, "seconds": round(st.duration_seconds, 3)}
                    for st in r.step_timings
                ],
                "transcribed_text": r.transcribed_text,
                "transcription_overlap": round(r.transcription_overlap, 3),
                "actual_tool": r.actual_tool,
                "actual_params": r.actual_params,
                "final_response": r.final_response,
                "conversation_id": r.conversation_id,
            }
            for r in results
        ],
    }

    with open(output_file, "w") as f:
        json.dump(output, f, indent=2, default=str)

    print(f"\nResults written to: {output_file}")


def print_summary(results: list[PipelineTestResult]) -> None:
    """Print a summary table with timing breakdown."""
    passed = sum(1 for r in results if r.passed)
    failed = len(results) - passed

    print(f"\n{'='*60}")
    print(f"  FULL PIPELINE TEST SUMMARY")
    print(f"{'='*60}")
    print(f"  Total: {len(results)}  |  Passed: {passed}  |  Failed: {failed}")
    if results:
        print(f"  Success Rate: {(passed / len(results) * 100):.1f}%")

    # Timing breakdown by step
    step_totals: dict[str, list[float]] = {}
    for r in results:
        for st in r.step_timings:
            step_totals.setdefault(st.step_name, []).append(st.duration_seconds)

    if step_totals:
        print(f"\n  Step Timing Averages:")
        print(f"  {'Step':<24s} {'Avg':>7s}  {'Min':>7s}  {'Max':>7s}  {'Count':>5s}")
        print(f"  {'─'*24} {'─'*7}  {'─'*7}  {'─'*7}  {'─'*5}")
        for step_name, durations in sorted(step_totals.items()):
            avg = sum(durations) / len(durations)
            print(
                f"  {step_name:<24s} {avg:6.2f}s  {min(durations):6.2f}s  "
                f"{max(durations):6.2f}s  {len(durations):>5d}"
            )

    # Transcription accuracy
    overlaps = [r.transcription_overlap for r in results if r.transcription_overlap > 0]
    if overlaps:
        print(f"\n  Transcription Accuracy (word overlap):")
        print(f"    Avg: {sum(overlaps)/len(overlaps):.0%}  "
              f"Min: {min(overlaps):.0%}  Max: {max(overlaps):.0%}  "
              f"({len(overlaps)} tests)")

    # Total times
    totals = [r.total_time_seconds for r in results if r.total_time_seconds > 0]
    if totals:
        print(f"\n  Total Pipeline Time (wall clock):")
        print(f"    Avg: {sum(totals)/len(totals):.2f}s  "
              f"Min: {min(totals):.2f}s  Max: {max(totals):.2f}s")

    # User-perceived latency: time from "user stops speaking" to response.
    # In production, warmup runs during user speech.  The user only waits
    # for: max(0, warmup - speech - whisper) + LLM + tool execution.
    perceived: list[float] = []
    for r in results:
        timings = {st.step_name: st.duration_seconds for st in r.step_timings}
        speech_overlap = timings.get("tts", 0) + timings.get("whisper", 0)
        warmup = timings.get("conversation_start", 0)
        warmup_wait = max(0.0, warmup - speech_overlap)
        llm = timings.get("llm_inference", 0)
        tool = timings.get("tool_execution", 0)
        submit = timings.get("tool_result_submission", 0)
        perceived.append(warmup_wait + llm + tool + submit)
    if perceived:
        print(f"\n  User-Perceived Latency (after speaking):")
        print(f"    Avg: {sum(perceived)/len(perceived):.2f}s  "
              f"Min: {min(perceived):.2f}s  Max: {max(perceived):.2f}s")
        print(f"    (warmup_wait + llm + tool_exec + tool_submit)")

    # Failed tests
    if failed > 0:
        print(f"\n  FAILED TESTS:")
        for r in results:
            if not r.passed:
                print(f"    - {r.test.description}")
                print(f"      {r.failure_reason}")

    # Category breakdown
    categories: dict[str, dict[str, int]] = {}
    for r in results:
        cat = r.test.category
        if cat not in categories:
            categories[cat] = {"passed": 0, "failed": 0}
        if r.passed:
            categories[cat]["passed"] += 1
        else:
            categories[cat]["failed"] += 1

    print(f"\n  By Category:")
    for cat, stats in sorted(categories.items()):
        total = stats["passed"] + stats["failed"]
        rate = (stats["passed"] / total * 100) if total else 0
        print(f"    {cat:<20s} {stats['passed']}/{total} ({rate:.0f}%)")

    print(f"{'='*60}")


def list_tests(tests: list[PipelineTestCase]) -> None:
    """List all available test cases."""
    print("AVAILABLE PIPELINE TESTS:")
    print("=" * 70)

    categories: dict[str, list[tuple[int, PipelineTestCase]]] = {}
    for i, test in enumerate(tests):
        categories.setdefault(test.category, []).append((i, test))

    for cat, cat_tests in sorted(categories.items()):
        print(f"\n{cat.upper()} ({len(cat_tests)} tests):")
        for idx, test in cat_tests:
            extras: list[str] = []
            if test.verify_response_contains:
                extras.append(f"expect=\"{test.verify_response_contains}\"")
            if test.max_total_time_seconds:
                extras.append(f"max={test.max_total_time_seconds}s")
            if test.validation_turns:
                extras.append("validation")
            extra_str = f"  [{', '.join(extras)}]" if extras else ""
            print(f"  #{idx:2d}: {test.description}{extra_str}")
            print(f"       \"{test.voice_phrase}\"")

    print(f"\nTotal: {len(tests)} tests")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_tests(
    tests: list[PipelineTestCase],
    save_audio_dir: str | None = None,
    timing_threshold: float | None = None,
) -> list[PipelineTestResult]:
    """Initialize clients, check services, and run all tests."""

    # Get command center URL
    jcc_url = Config.get("jarvis_command_center_api_url")
    if not jcc_url:
        print("ERROR: jarvis_command_center_api_url not found in config")
        return []
    jcc_client = JarvisCommandCenterClient(jcc_url)
    print(f"Command Center: {jcc_url}")

    # Date context
    date_context = jcc_client.get_date_context()
    if not date_context:
        print("ERROR: Could not get date context from command center")
        return []
    print(f"Timezone: {date_context.timezone.user_timezone}")

    # Commands
    command_service = get_command_discovery_service()
    command_service.refresh_now()
    commands = command_service.get_all_commands()
    if not commands:
        print("ERROR: No commands discovered")
        return []
    print(f"Commands: {len(commands)} available")

    # Audio services
    audio_client = AudioPipelineClient(save_audio_dir=save_audio_dir)
    tts_url = audio_client.tts_url

    # App-to-app auth for TTS (TTS requires X-Jarvis-App-Id/Key, not node X-API-Key)
    app_id = os.environ.get("JARVIS_APP_ID", "")
    app_key = os.environ.get("JARVIS_APP_KEY", "")
    if not app_id or not app_key:
        print("ERROR: JARVIS_APP_ID and JARVIS_APP_KEY env vars required for TTS auth")
        print("  Export them or add to .env:")
        print("    export JARVIS_APP_ID=jarvis-command-center")
        print("    export JARVIS_APP_KEY=<key from CC container>")
        return []

    app_auth_headers = {
        "X-Jarvis-App-Id": app_id,
        "X-Jarvis-App-Key": app_key,
    }

    # Check TTS is reachable with app auth
    try:
        tts_check = http_requests.get(f"{tts_url}/ping", timeout=5)
        if tts_check.status_code != 200:
            print(f"ERROR: TTS service not available at {tts_url}")
            print("  Start: cd jarvis-tts && ./run-docker-dev.sh")
            return []
    except http_requests.RequestException:
        print(f"ERROR: TTS service not available at {tts_url}")
        return []

    # Verify CC is reachable
    cc_health = RestClient.get(f"{jcc_url}/health")
    if not cc_health:
        print("ERROR: Command center health check failed")
        return []
    print(f"Services: TTS ({tts_url}) + CC Whisper proxy ready")

    # Run
    runner = FullPipelineTestRunner(
        jcc_client=jcc_client,
        commands=commands,
        date_context=date_context,
        audio_client=audio_client,
        tts_url=tts_url,
        app_auth_headers=app_auth_headers,
        save_audio_dir=save_audio_dir,
        timing_threshold=timing_threshold,
    )

    results: list[PipelineTestResult] = []
    for test in tests:
        result = runner.run_test(test)
        results.append(result)
        time.sleep(0.5)

    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Full pipeline integration test: TTS -> Whisper (CC proxy) -> LLM -> Tools"
    )
    parser.add_argument(
        "--list", "-l", action="store_true", help="List all available tests"
    )
    parser.add_argument(
        "--test", "-t", type=int, nargs="+", help="Run specific tests by index"
    )
    parser.add_argument(
        "--category", "-c", type=str, help="Run tests in a specific category"
    )
    parser.add_argument(
        "--output", "-o", type=str, default="pipeline_test_results.json",
        help="Output file for results (default: pipeline_test_results.json)"
    )
    parser.add_argument(
        "--save-audio", type=str, help="Directory to save TTS audio files"
    )
    parser.add_argument(
        "--timing-threshold", type=float,
        help="Override max total time (seconds) for all tests"
    )

    args = parser.parse_args()
    all_tests = create_test_cases()

    if args.list:
        list_tests(all_tests)
        return

    # Filter tests
    tests_to_run = all_tests
    if args.test:
        tests_to_run = [all_tests[i] for i in args.test if 0 <= i < len(all_tests)]
        print(f"Running {len(tests_to_run)} selected tests")
    if args.category:
        tests_to_run = [t for t in tests_to_run if t.category == args.category]
        print(f"Running {len(tests_to_run)} tests in category: {args.category}")

    if not tests_to_run:
        print("No tests to run")
        return

    print(f"\n{'='*60}")
    print(f"  FULL PIPELINE INTEGRATION TEST")
    print(f"  TTS -> Whisper (CC proxy) -> LLM -> Tool Execution")
    print(f"  Tests: {len(tests_to_run)}")
    print(f"{'='*60}")

    results = run_tests(
        tests_to_run,
        save_audio_dir=args.save_audio,
        timing_threshold=args.timing_threshold,
    )

    if results:
        print_summary(results)
        write_results(results, args.output)


if __name__ == "__main__":
    print("Jarvis Full Pipeline Integration Test")
    print("=" * 45)
    print("Usage:")
    print("  python test_full_pipeline.py              # Run all tests")
    print("  python test_full_pipeline.py -l            # List tests")
    print("  python test_full_pipeline.py -t 0 1 3      # Run specific tests")
    print("  python test_full_pipeline.py -c performance # Run category")
    print("  python test_full_pipeline.py -o results.json")
    print("  python test_full_pipeline.py --save-audio ./audio/")
    print("  python test_full_pipeline.py --timing-threshold 15")
    print("=" * 45)
    print()

    main()
