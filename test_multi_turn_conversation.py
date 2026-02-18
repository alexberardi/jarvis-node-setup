#!/usr/bin/env python3
"""
E2E Test Suite for Multi-Turn Conversation Flow

Tests the "back half" of Jarvis voice processing:
- Tool execution flow (voice → LLM → tool_call → execute → result → complete)
- Validation/clarification flow (ambiguous input → clarify → complete)
- Multi-tool execution
- Context preservation across turns

Two test modes:
- Fast mode (default): Text-based, no TTS/Whisper - for quick iteration
- Full mode: TTS → Whisper → Command Center - complete audio pipeline verification
"""

import argparse
import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

from dotenv import load_dotenv

from clients.jarvis_command_center_client import JarvisCommandCenterClient
from clients.responses.jarvis_command_center import (
    DateContext,
    ToolCallingResponse,
    ValidationRequest,
)
from utils.config_loader import Config
from utils.command_discovery_service import get_command_discovery_service

# Load environment variables
load_dotenv()


class StopReason(str, Enum):
    """Expected stop reasons from the LLM"""

    TOOL_CALLS = "tool_calls"
    VALIDATION_REQUIRED = "validation_required"
    COMPLETE = "complete"


@dataclass
class Turn:
    """Single turn in a multi-turn conversation"""

    voice_command: Optional[str]
    """What the user says. None if this turn is a continuation from validation."""

    expected_stop_reason: StopReason
    """Expected stop_reason from the API response"""

    expected_tool: Optional[str] = None
    """Expected tool to be called (if stop_reason is tool_calls)"""

    expected_params: Optional[dict[str, Any]] = None
    """Expected parameters for the tool (subset match)"""

    validation_response: Optional[str] = None
    """If stop_reason is validation_required, what to respond"""


@dataclass
class MultiTurnTest:
    """Test case for multi-turn conversation scenarios"""

    description: str
    """Human-readable test description"""

    turns: list[Turn]
    """Sequence of conversation turns"""

    category: str = "general"
    """Test category for filtering"""

    verify_response_contains: Optional[str] = None
    """Optional: verify final response contains this text (case-insensitive)"""

    verify_response_not_contains: Optional[str] = None
    """Optional: verify final response does NOT contain this text"""


@dataclass
class TestResult:
    """Result of a single test execution"""

    test: MultiTurnTest
    passed: bool
    failure_reason: Optional[str] = None
    response_times: list[float] = field(default_factory=list)
    turn_results: list[dict[str, Any]] = field(default_factory=list)
    final_response: Optional[str] = None
    conversation_id: Optional[str] = None
    audio_transcriptions: list[tuple[str, str]] = field(default_factory=list)
    """List of (original, transcribed) pairs for full mode"""


def create_test_scenarios() -> list[MultiTurnTest]:
    """Create comprehensive test scenarios for multi-turn conversations"""

    tests = []

    # ===== CATEGORY 1: Single-Turn Tool Execution (Happy Path) =====
    single_turn_tests = [
        MultiTurnTest(
            description="Calculator returns correct result",
            category="tool_execution",
            turns=[
                Turn(
                    voice_command="What's 25 plus 37?",
                    expected_stop_reason=StopReason.COMPLETE,
                    expected_tool="calculate",
                ),
            ],
            verify_response_contains="62",
        ),
        MultiTurnTest(
            description="Weather query executes correctly",
            category="tool_execution",
            turns=[
                Turn(
                    voice_command="What's the weather in Miami?",
                    expected_stop_reason=StopReason.TOOL_CALLS,
                    expected_tool="get_weather",
                    expected_params={"city": "Miami"},
                ),
            ],
        ),
        MultiTurnTest(
            description="Joke request works end-to-end",
            category="tool_execution",
            turns=[
                Turn(
                    voice_command="Tell me a joke about programming",
                    expected_stop_reason=StopReason.COMPLETE,
                    expected_tool="tell_joke",
                    expected_params={"topic": "programming"},
                ),
            ],
        ),
        MultiTurnTest(
            description="Timer request with duration",
            category="tool_execution",
            turns=[
                Turn(
                    voice_command="Set a timer for 5 minutes",
                    expected_stop_reason=StopReason.TOOL_CALLS,
                    expected_tool="set_timer",
                    expected_params={"duration_seconds": 300},
                ),
            ],
        ),
        MultiTurnTest(
            description="Sports score query",
            category="tool_execution",
            turns=[
                Turn(
                    voice_command="How did the Yankees do?",
                    expected_stop_reason=StopReason.TOOL_CALLS,
                    expected_tool="get_sports_scores",
                    expected_params={"team_name": "Yankees"},
                ),
            ],
        ),
    ]
    tests.extend(single_turn_tests)

    # ===== CATEGORY 2: Validation Flow =====
    validation_tests = [
        MultiTurnTest(
            description="Ambiguous sports query triggers validation (Giants)",
            category="validation",
            turns=[
                Turn(
                    voice_command="How did the Giants do?",
                    expected_stop_reason=StopReason.VALIDATION_REQUIRED,
                    validation_response="New York Giants",
                ),
                Turn(
                    voice_command=None,  # Continuation from validation
                    expected_stop_reason=StopReason.TOOL_CALLS,
                    expected_tool="get_sports_scores",
                ),
            ],
        ),
        MultiTurnTest(
            description="Incomplete timer request triggers validation",
            category="validation",
            turns=[
                Turn(
                    voice_command="Set a timer",
                    expected_stop_reason=StopReason.VALIDATION_REQUIRED,
                    validation_response="5 minutes",
                ),
                Turn(
                    voice_command=None,
                    expected_stop_reason=StopReason.TOOL_CALLS,
                    expected_tool="set_timer",
                ),
            ],
        ),
    ]
    tests.extend(validation_tests)

    # ===== CATEGORY 3: Tool Result Incorporation =====
    result_tests = [
        MultiTurnTest(
            description="Calculator result is incorporated in response",
            category="result_incorporation",
            turns=[
                Turn(
                    voice_command="What's 15 times 8?",
                    expected_stop_reason=StopReason.COMPLETE,
                    expected_tool="calculate",
                ),
            ],
            verify_response_contains="120",
        ),
        MultiTurnTest(
            description="Division result is correct",
            category="result_incorporation",
            turns=[
                Turn(
                    voice_command="What's 100 divided by 4?",
                    expected_stop_reason=StopReason.COMPLETE,
                    expected_tool="calculate",
                ),
            ],
            verify_response_contains="25",
        ),
    ]
    tests.extend(result_tests)

    # ===== CATEGORY 4: Context Preservation =====
    context_tests = [
        MultiTurnTest(
            description="Follow-up weather query uses context",
            category="context",
            turns=[
                Turn(
                    voice_command="What's the weather in Seattle?",
                    expected_stop_reason=StopReason.TOOL_CALLS,
                    expected_tool="get_weather",
                    expected_params={"city": "Seattle"},
                ),
                Turn(
                    voice_command="What about tomorrow?",
                    expected_stop_reason=StopReason.TOOL_CALLS,
                    expected_tool="get_weather",
                    # Context should preserve Seattle
                ),
            ],
        ),
    ]
    tests.extend(context_tests)

    # ===== CATEGORY 5: Error Handling =====
    error_tests = [
        MultiTurnTest(
            description="Unknown request gets handled gracefully",
            category="error_handling",
            turns=[
                Turn(
                    voice_command="Flibbertigibbet the woozlewazzle",
                    expected_stop_reason=StopReason.COMPLETE,
                    # Should complete without error, possibly with answer_question
                ),
            ],
        ),
    ]
    tests.extend(error_tests)

    # ===== CATEGORY 6: Complex Queries =====
    complex_tests = [
        MultiTurnTest(
            description="Knowledge question answered correctly",
            category="complex",
            turns=[
                Turn(
                    voice_command="What is the capital of France?",
                    expected_stop_reason=StopReason.COMPLETE,
                    expected_tool="answer_question",
                ),
            ],
            verify_response_contains="Paris",
        ),
        MultiTurnTest(
            description="Unit conversion works",
            category="complex",
            turns=[
                Turn(
                    voice_command="How many feet in a mile?",
                    expected_stop_reason=StopReason.COMPLETE,
                    expected_tool="convert_measurement",
                ),
            ],
            verify_response_contains="5280",
        ),
    ]
    tests.extend(complex_tests)

    return tests


class MultiTurnTestRunner:
    """Runner for multi-turn conversation tests"""

    def __init__(
        self,
        jcc_client: JarvisCommandCenterClient,
        commands: dict,
        date_context: DateContext,
        full_mode: bool = False,
        save_audio_dir: Optional[str] = None,
    ):
        self.jcc_client = jcc_client
        self.commands = commands
        self.date_context = date_context
        self.full_mode = full_mode
        self.save_audio_dir = save_audio_dir
        self.audio_client = None

        if full_mode:
            from utils.audio_pipeline_client import AudioPipelineClient

            self.audio_client = AudioPipelineClient(save_audio_dir=save_audio_dir)

    def run_test(self, test: MultiTurnTest) -> TestResult:
        """Execute a single multi-turn test scenario"""

        conversation_id = str(uuid.uuid4())
        result = TestResult(
            test=test,
            passed=False,
            conversation_id=conversation_id,
        )

        print(f"\n🧪 Testing: {test.description}")
        print(f"   Category: {test.category}")
        print(f"   Conversation ID: {conversation_id}")

        try:
            # Start conversation with registered tools
            success = self.jcc_client.start_conversation(
                conversation_id, self.commands, self.date_context
            )
            if not success:
                result.failure_reason = "Failed to start conversation"
                print(f"   ❌ {result.failure_reason}")
                return result

            print(f"   ✅ Conversation started")

            # Process each turn
            response: Optional[ToolCallingResponse] = None
            for turn_idx, turn in enumerate(test.turns):
                turn_result = self._process_turn(
                    conversation_id, turn, turn_idx, response, result
                )
                result.turn_results.append(turn_result)

                if not turn_result.get("success"):
                    result.failure_reason = turn_result.get("failure_reason", "Turn failed")
                    return result

                response = turn_result.get("response")

            # Verify final response if needed
            if response and response.assistant_message:
                result.final_response = response.assistant_message

                if test.verify_response_contains:
                    if test.verify_response_contains.lower() not in response.assistant_message.lower():
                        result.failure_reason = (
                            f"Final response missing expected text: '{test.verify_response_contains}'"
                        )
                        print(f"   ❌ {result.failure_reason}")
                        print(f"      Actual response: {response.assistant_message}")
                        return result

                if test.verify_response_not_contains:
                    if test.verify_response_not_contains.lower() in response.assistant_message.lower():
                        result.failure_reason = (
                            f"Final response contains forbidden text: '{test.verify_response_not_contains}'"
                        )
                        print(f"   ❌ {result.failure_reason}")
                        return result

            result.passed = True
            print(f"   ✅ Test PASSED")

        except Exception as e:
            result.failure_reason = f"Exception: {str(e)}"
            print(f"   ❌ {result.failure_reason}")
            import traceback

            traceback.print_exc()

        return result

    def _process_turn(
        self,
        conversation_id: str,
        turn: Turn,
        turn_idx: int,
        prev_response: Optional[ToolCallingResponse],
        result: TestResult,
    ) -> dict[str, Any]:
        """Process a single conversation turn"""

        turn_result: dict[str, Any] = {
            "turn_index": turn_idx,
            "success": False,
            "voice_command": turn.voice_command,
        }

        start_time = time.time()

        try:
            # Determine the voice command to send
            voice_command = turn.voice_command

            # Full mode: convert text to speech and back
            if self.full_mode and voice_command and self.audio_client:
                print(f"   🎤 Turn {turn_idx}: Running audio pipeline...")
                transcribed, audio_bytes = self.audio_client.full_pipeline(
                    voice_command,
                    save_name=f"{conversation_id}_turn{turn_idx}",
                )
                if transcribed:
                    result.audio_transcriptions.append((voice_command, transcribed))
                    print(f"      Original: {voice_command}")
                    print(f"      Transcribed: {transcribed}")
                    voice_command = transcribed
                else:
                    print(f"      ⚠️ Audio pipeline failed, using original text")

            # Send command or handle validation continuation
            if voice_command:
                print(f"   📤 Turn {turn_idx}: Sending command: '{voice_command}'")
                response = self.jcc_client.send_command(voice_command, conversation_id)
            elif prev_response and prev_response.requires_validation():
                # This is a validation response continuation
                print(f"   📤 Turn {turn_idx}: Sending validation response: '{turn.validation_response}'")
                response = self.jcc_client.send_validation_response(
                    conversation_id,
                    prev_response.validation_request,
                    turn.validation_response or "",
                )
            else:
                turn_result["failure_reason"] = "No voice command and no validation to respond to"
                return turn_result

            end_time = time.time()
            response_time = end_time - start_time
            result.response_times.append(response_time)
            turn_result["response_time"] = response_time

            if not response:
                turn_result["failure_reason"] = "No response from server"
                return turn_result

            turn_result["response"] = response
            turn_result["stop_reason"] = response.stop_reason

            print(f"   📥 Turn {turn_idx}: stop_reason = {response.stop_reason} (⏱️ {response_time:.2f}s)")

            # Handle tool execution loop
            max_tool_iterations = 5
            tool_iteration = 0

            while response.requires_tool_execution() and tool_iteration < max_tool_iterations:
                tool_iteration += 1
                print(f"   🔧 Turn {turn_idx}: Executing {len(response.tool_calls)} tool(s) (iteration {tool_iteration})")

                # Execute tools and send results
                tool_results = self._execute_tools(response.tool_calls, conversation_id)
                turn_result["tool_results"] = tool_results

                response = self.jcc_client.send_tool_results(conversation_id, tool_results)

                if not response:
                    turn_result["failure_reason"] = "No response after tool execution"
                    return turn_result

                print(f"   📥 Turn {turn_idx}: After tool execution, stop_reason = {response.stop_reason}")
                turn_result["response"] = response
                turn_result["stop_reason"] = response.stop_reason

            # Validate the response matches expectations
            validation_result = self._validate_turn_response(turn, response, turn_idx)
            if not validation_result["success"]:
                turn_result["failure_reason"] = validation_result["failure_reason"]
                return turn_result

            # Handle validation request if this turn expects it
            if response.requires_validation() and turn.validation_response:
                # The next turn will handle the validation response
                print(f"   ❓ Turn {turn_idx}: Validation requested: {response.validation_request.question}")

            turn_result["success"] = True

        except Exception as e:
            turn_result["failure_reason"] = f"Exception: {str(e)}"

        return turn_result

    def _execute_tools(
        self, tool_calls: list, conversation_id: str
    ) -> list[dict[str, Any]]:
        """Execute client-side tools and return formatted results"""
        from core.request_information import RequestInformation
        from utils.tool_result_formatter import format_tool_error, format_tool_result

        results = []

        for tool_call in tool_calls:
            tool_name = tool_call.function.name
            print(f"      🔨 Executing: {tool_name}")

            try:
                command = self.commands.get(tool_name)
                if not command:
                    results.append(format_tool_error(tool_call.id, f"Unknown tool: {tool_name}"))
                    continue

                arguments = tool_call.function.get_arguments_dict()
                request_info = RequestInformation(
                    voice_command=f"Tool call: {tool_name}",
                    conversation_id=conversation_id,
                    is_validation_response=False,
                )

                command_response = command.execute(request_info, **arguments)
                results.append(format_tool_result(tool_call.id, command_response))
                print(f"      ✅ Tool executed successfully")

            except Exception as e:
                print(f"      ❌ Tool error: {e}")
                results.append(format_tool_error(tool_call.id, str(e)))

        return results

    def _validate_turn_response(
        self, turn: Turn, response: ToolCallingResponse, turn_idx: int
    ) -> dict[str, Any]:
        """Validate that the response matches turn expectations"""

        result: dict[str, Any] = {"success": True}

        # Check stop_reason - be flexible about complete vs tool_calls for some cases
        actual_stop = response.stop_reason
        expected_stop = turn.expected_stop_reason.value

        # Allow 'complete' when we expected 'tool_calls' if tools were already executed
        stop_ok = (
            actual_stop == expected_stop
            or (expected_stop == "tool_calls" and actual_stop == "complete")
        )

        if not stop_ok:
            result["success"] = False
            result["failure_reason"] = (
                f"Turn {turn_idx}: Expected stop_reason '{expected_stop}', got '{actual_stop}'"
            )
            print(f"   ❌ {result['failure_reason']}")
            return result

        # Check expected tool if specified and we got tool_calls
        if turn.expected_tool and response.tool_calls:
            tool_names = [tc.function.name for tc in response.tool_calls]
            if turn.expected_tool not in tool_names:
                result["success"] = False
                result["failure_reason"] = (
                    f"Turn {turn_idx}: Expected tool '{turn.expected_tool}', got {tool_names}"
                )
                print(f"   ❌ {result['failure_reason']}")
                return result
            print(f"   ✅ Turn {turn_idx}: Correct tool called: {turn.expected_tool}")

        # Check expected parameters if specified
        if turn.expected_params and response.tool_calls:
            for tc in response.tool_calls:
                if tc.function.name == turn.expected_tool:
                    actual_params = tc.function.get_arguments_dict()
                    for key, expected_value in turn.expected_params.items():
                        actual_value = actual_params.get(key)
                        # Flexible matching for strings
                        if isinstance(expected_value, str) and isinstance(actual_value, str):
                            if expected_value.lower() not in actual_value.lower():
                                result["success"] = False
                                result["failure_reason"] = (
                                    f"Turn {turn_idx}: Parameter '{key}' expected '{expected_value}', got '{actual_value}'"
                                )
                                print(f"   ❌ {result['failure_reason']}")
                                return result
                        elif expected_value != actual_value:
                            result["success"] = False
                            result["failure_reason"] = (
                                f"Turn {turn_idx}: Parameter '{key}' expected {expected_value}, got {actual_value}"
                            )
                            print(f"   ❌ {result['failure_reason']}")
                            return result
                    print(f"   ✅ Turn {turn_idx}: Parameters match")

        return result


def run_tests(
    tests: list[MultiTurnTest],
    full_mode: bool = False,
    save_audio_dir: Optional[str] = None,
) -> list[TestResult]:
    """Run all test scenarios"""

    # Initialize clients
    jcc_url = Config.get("jarvis_command_center_api_url")
    if not jcc_url:
        print("❌ Could not find jarvis_command_center_api_url in configuration")
        return []

    jcc_client = JarvisCommandCenterClient(jcc_url)
    print(f"✅ Connected to JCC at: {jcc_url}")

    # Get date context
    date_context = jcc_client.get_date_context()
    if not date_context:
        print("❌ Could not get date context from server")
        return []
    print(f"✅ Got date context for timezone: {date_context.timezone.user_timezone}")

    # Get available commands
    command_service = get_command_discovery_service()
    command_service.refresh_now()
    commands = command_service.get_all_commands()

    if not commands:
        print("❌ No commands found")
        return []
    print(f"✅ Found {len(commands)} commands")

    # Check audio services if full mode
    if full_mode:
        from utils.audio_pipeline_client import AudioPipelineClient

        audio_client = AudioPipelineClient()
        service_status = audio_client.check_services()

        if not service_status["tts"]:
            print("❌ TTS service not available (required for full mode)")
            print("   Start jarvis-tts: cd jarvis-tts && uvicorn app.main:app --port 7707")
            return []

        if not service_status["whisper"]:
            print("❌ Whisper service not available (required for full mode)")
            print("   Start jarvis-whisper-api: cd jarvis-whisper-api && ./run-dev.sh")
            return []

        print("✅ Audio services available (TTS + Whisper)")

    # Create test runner
    runner = MultiTurnTestRunner(
        jcc_client=jcc_client,
        commands=commands,
        date_context=date_context,
        full_mode=full_mode,
        save_audio_dir=save_audio_dir,
    )

    # Run tests
    results = []
    for test in tests:
        result = runner.run_test(test)
        results.append(result)
        time.sleep(0.5)  # Small delay between tests

    return results


def print_summary(results: list[TestResult]) -> None:
    """Print test summary"""

    passed = sum(1 for r in results if r.passed)
    failed = len(results) - passed
    total_time = sum(sum(r.response_times) for r in results)

    print(f"\n{'='*60}")
    print(f"📊 TEST SUMMARY")
    print(f"   Total Tests: {len(results)}")
    print(f"   Passed: {passed}")
    print(f"   Failed: {failed}")
    print(f"   Success Rate: {(passed/len(results)*100):.1f}%" if results else "N/A")
    print(f"   Total Time: {total_time:.2f}s")

    if failed > 0:
        print(f"\n❌ FAILED TESTS:")
        for r in results:
            if not r.passed:
                print(f"   - {r.test.description}")
                print(f"     Reason: {r.failure_reason}")

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

    print(f"\n📈 BY CATEGORY:")
    for cat, stats in sorted(categories.items()):
        total = stats["passed"] + stats["failed"]
        rate = (stats["passed"] / total * 100) if total else 0
        print(f"   {cat}: {stats['passed']}/{total} ({rate:.0f}%)")


def write_results(results: list[TestResult], output_file: str) -> None:
    """Write results to JSON file"""

    output = {
        "summary": {
            "total": len(results),
            "passed": sum(1 for r in results if r.passed),
            "failed": sum(1 for r in results if not r.passed),
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        },
        "results": [
            {
                "description": r.test.description,
                "category": r.test.category,
                "passed": r.passed,
                "failure_reason": r.failure_reason,
                "response_times": r.response_times,
                "final_response": r.final_response,
                "conversation_id": r.conversation_id,
                "audio_transcriptions": r.audio_transcriptions,
            }
            for r in results
        ],
    }

    with open(output_file, "w") as f:
        json.dump(output, f, indent=2, default=str)

    print(f"\n📄 Results written to: {output_file}")


def list_tests(tests: list[MultiTurnTest]) -> None:
    """List all available tests"""

    print("📋 AVAILABLE TESTS:")
    print("=" * 80)

    categories: dict[str, list[tuple[int, MultiTurnTest]]] = {}
    for i, test in enumerate(tests):
        if test.category not in categories:
            categories[test.category] = []
        categories[test.category].append((i, test))

    for cat, cat_tests in sorted(categories.items()):
        print(f"\n{cat.upper()} ({len(cat_tests)} tests):")
        for idx, test in cat_tests:
            print(f"  #{idx:2d}: {test.description}")
            for j, turn in enumerate(test.turns):
                if turn.voice_command:
                    print(f"       Turn {j}: \"{turn.voice_command}\"")

    print(f"\nTotal: {len(tests)} tests")


def main():
    parser = argparse.ArgumentParser(
        description="E2E tests for multi-turn Jarvis conversations"
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Run in full mode (TTS → Whisper pipeline)",
    )
    parser.add_argument(
        "--list",
        "-l",
        action="store_true",
        help="List all available tests",
    )
    parser.add_argument(
        "--test",
        "-t",
        type=int,
        nargs="+",
        help="Run specific tests by index",
    )
    parser.add_argument(
        "--category",
        "-c",
        type=str,
        help="Run tests in a specific category",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=str,
        default="multi_turn_test_results.json",
        help="Output file for results",
    )
    parser.add_argument(
        "--save-audio",
        type=str,
        help="Directory to save audio files (full mode only)",
    )

    args = parser.parse_args()

    # Create all test scenarios
    all_tests = create_test_scenarios()

    # List tests if requested
    if args.list:
        list_tests(all_tests)
        return

    # Filter tests
    tests_to_run = all_tests

    if args.test:
        tests_to_run = [all_tests[i] for i in args.test if 0 <= i < len(all_tests)]
        print(f"🎯 Running {len(tests_to_run)} selected tests")

    if args.category:
        tests_to_run = [t for t in tests_to_run if t.category == args.category]
        print(f"🎯 Running {len(tests_to_run)} tests in category: {args.category}")

    if not tests_to_run:
        print("❌ No tests to run")
        return

    # Run tests
    print(f"\n{'='*60}")
    print(f"🧪 MULTI-TURN CONVERSATION E2E TESTS")
    print(f"   Mode: {'Full (TTS → Whisper)' if args.full else 'Fast (text-only)'}")
    print(f"   Tests: {len(tests_to_run)}")
    print(f"{'='*60}")

    results = run_tests(
        tests_to_run,
        full_mode=args.full,
        save_audio_dir=args.save_audio,
    )

    if results:
        print_summary(results)
        write_results(results, args.output)


if __name__ == "__main__":
    print("🧪 Jarvis Multi-Turn Conversation E2E Test Suite")
    print("=" * 50)
    print("Usage examples:")
    print("  python test_multi_turn_conversation.py              # Run all tests (fast mode)")
    print("  python test_multi_turn_conversation.py --full       # Run with audio pipeline")
    print("  python test_multi_turn_conversation.py -l           # List all tests")
    print("  python test_multi_turn_conversation.py -t 0 1 2     # Run specific tests")
    print("  python test_multi_turn_conversation.py -c validation  # Run category")
    print("  python test_multi_turn_conversation.py --full --save-audio ./audio/")
    print("=" * 50)
    print()

    main()
