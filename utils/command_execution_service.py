import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional, Callable

from jarvis_log_client import JarvisLogger

from clients.jarvis_command_center_client import JarvisCommandCenterClient
from clients.responses.jarvis_command_center import ToolCallingResponse, ToolCall, ValidationRequest
from core.command_response import CommandResponse
from core.helpers import get_tts_provider
from core.platform_audio import platform_audio
from core.request_information import RequestInformation
from utils.command_discovery_service import get_command_discovery_service
from utils.config_service import Config
from utils.service_discovery import get_command_center_url
from utils.tool_result_formatter import format_tool_result, format_tool_error

logger = JarvisLogger(service="jarvis-node")


@dataclass
class ToolExecutionResult:
    """Carries both API-formatted results and command-level signals from tool execution."""
    api_results: List[Dict[str, Any]] = field(default_factory=list)
    wait_for_input: bool = False
    clear_history: bool = False
    all_failed: bool = False
    first_error: str | None = None
    tool_message: str | None = None


@dataclass
class ParseResult:
    """Result of classifying a voice command without executing it."""
    conversation_id: str
    pre_routed: bool
    tool_name: str | None
    tool_arguments: Dict[str, Any]
    raw_response: ToolCallingResponse | None  # None if pre-routed
    success: bool
    validation_request: ValidationRequest | None = None
    assistant_message: str | None = None


class CommandExecutionService:
    def __init__(self):
        self.command_center_url = get_command_center_url()
        self.node_id = Config.get_str("node_id")
        self.room = Config.get_str("room")
        self.command_discovery = get_command_discovery_service()
        self.client = JarvisCommandCenterClient(self.command_center_url)
        self._conversation_users: Dict[str, int | None] = {}
        # Force initial discovery
        self.command_discovery.refresh_now()

    def register_tools_for_conversation(
        self,
        conversation_id: str,
        speaker_user_id: Optional[int] = None,
        agents: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Register available client-side tools with the Command Center for a conversation

        Args:
            conversation_id: The conversation identifier
            speaker_user_id: Optional speaker identity from voice recognition
            agents: Optional agent context to inject (e.g., Home Assistant data)

        Returns:
            True if successful, False otherwise
        """
        self._conversation_users[conversation_id] = speaker_user_id

        commands = self.command_discovery.get_all_commands()

        if not commands:
            logger.warning("No commands available to register")
            return False

        logger.info("Registering tools for conversation", count=len(commands), conversation_id=conversation_id)

        try:
            # Get date context
            date_context = self.client.get_date_context()

            # Start conversation with available commands
            success = self.client.start_conversation(
                conversation_id, commands, date_context,
                speaker_user_id=speaker_user_id, agents=agents,
            )

            if success:
                logger.info("Successfully registered tools", count=len(commands))
            else:
                logger.error("Failed to register tools")

            return success

        except Exception as e:
            logger.error("Error registering tools", error=str(e))
            return False

    def parse_voice_command(
        self,
        voice_command: str,
        speaker_user_id: int | None = None,
        agents: dict | None = None,
        warmup_delay: float = 0,
    ) -> ParseResult:
        """Classify a voice command through the production code path without executing tools.

        Runs pre-routing, tool registration, LLM inference, and post-processing,
        but stops before tool execution or audio playback.

        Args:
            voice_command: The transcribed voice command
            speaker_user_id: Optional speaker identity from voice recognition
            agents: Optional agent context (e.g., Home Assistant device data)
            warmup_delay: Seconds to wait between tool registration and sending
                          the command (for KV cache warmup on GGUF models)

        Returns:
            ParseResult with classification details
        """
        conversation_id = self._generate_conversation_id()

        # Step 1: Try pre-routing (classification only — no execution)
        commands = self.command_discovery.get_all_commands()
        for command in commands.values():
            pre = command.pre_route(voice_command)
            if pre is not None:
                logger.info(
                    "Pre-routed to command (parse only)",
                    command=command.command_name,
                    voice_command=voice_command,
                )
                return ParseResult(
                    conversation_id=conversation_id,
                    pre_routed=True,
                    tool_name=command.command_name,
                    tool_arguments=pre.arguments,
                    raw_response=None,
                    success=True,
                    assistant_message=pre.spoken_response,
                )

        # Step 2: Register tools
        if not self.register_tools_for_conversation(
            conversation_id, speaker_user_id=speaker_user_id, agents=agents,
        ):
            return ParseResult(
                conversation_id=conversation_id,
                pre_routed=False,
                tool_name=None,
                tool_arguments={},
                raw_response=None,
                success=False,
                assistant_message="Failed to register tools",
            )

        # Step 3: Warmup delay (KV cache population for GGUF models)
        if warmup_delay > 0:
            time.sleep(warmup_delay)

        # Step 4: Send command to CC
        response = self.client.send_command(voice_command, conversation_id)
        if not response:
            return ParseResult(
                conversation_id=conversation_id,
                pre_routed=False,
                tool_name=None,
                tool_arguments={},
                raw_response=None,
                success=False,
                assistant_message="No response from command center",
            )

        # Step 5: Handle server-side validation (auto-select first option)
        while response.requires_validation() and response.validation_request:
            vr = response.validation_request
            chosen = None
            if vr.options:
                chosen = vr.options[0]
            elif vr.question:
                chosen = "yes"
            if not chosen:
                break
            logger.info("Auto-answering validation", question=vr.question, answer=chosen)
            response = self.client.send_validation_response(conversation_id, vr, chosen)
            if not response:
                return ParseResult(
                    conversation_id=conversation_id,
                    pre_routed=False,
                    tool_name=None,
                    tool_arguments={},
                    raw_response=None,
                    success=False,
                    assistant_message="No response after validation",
                )

        # Step 6: Extract tool call and apply post-processing
        if response.tool_calls:
            tool_call = response.tool_calls[0]
            tool_name = tool_call.function.name
            tool_arguments = tool_call.function.get_arguments_dict()

            # Apply post-processing (e.g., MusicCommand fills missing query)
            command = self.command_discovery.get_command(tool_name)
            if command:
                tool_arguments = command.post_process_tool_call(tool_arguments, voice_command)

            return ParseResult(
                conversation_id=conversation_id,
                pre_routed=False,
                tool_name=tool_name,
                tool_arguments=tool_arguments,
                raw_response=response,
                success=True,
                assistant_message=response.assistant_message,
            )

        # Step 7: No tool call — direct completion or error
        return ParseResult(
            conversation_id=conversation_id,
            pre_routed=False,
            tool_name=None,
            tool_arguments={},
            raw_response=response,
            success=response.stop_reason == "complete",
            assistant_message=response.assistant_message,
            validation_request=response.validation_request,
        )

    def _play_streaming_audio(
        self,
        response: Any,
        audio_meta: Dict[str, str],
    ) -> bool:
        """Play streamed PCM audio from an HTTP response.

        Uses a queue + thread to decouple network I/O from audio playback.

        Args:
            response: Streaming HTTP response (requests.Response) to iterate over.
            audio_meta: Dict with sample_rate, channels, sample_width strings.

        Returns:
            True if any audio was played, False otherwise.
        """
        sample_rate = int(audio_meta.get("sample_rate", "22050"))
        channels = int(audio_meta.get("channels", "1"))
        sample_width = int(audio_meta.get("sample_width", "2"))

        audio_queue: queue.Queue[bytes | None] = queue.Queue(maxsize=50)

        def audio_player() -> None:
            platform_audio.play_pcm_stream(
                iter(audio_queue.get, None),  # sentinel = None
                sample_rate=sample_rate,
                channels=channels,
                sample_width=sample_width,
            )

        player_thread = threading.Thread(target=audio_player, daemon=True)
        player_thread.start()

        has_audio = False
        try:
            for chunk in response.iter_content(chunk_size=4096):
                if chunk:
                    has_audio = True
                    audio_queue.put(chunk)
        finally:
            audio_queue.put(None)
            player_thread.join(timeout=30)
            response.close()

        return has_audio

    def process_voice_command(
        self,
        voice_command: str,
        validation_handler: Optional[Callable[[ValidationRequest], str]] = None,
        register_tools: bool = True,
        speaker_user_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Process a voice command through the unified streaming endpoint.

        Makes a single request to /voice/command/stream which handles all
        cases: conversational responses (streamed audio), tool calls, and
        validation. No fallback to a second endpoint.

        Args:
            voice_command: The transcribed voice command
            validation_handler: Optional callback to handle validation requests
                               If None, default behavior is used
            register_tools: Whether to register available tools before processing (default: True)
            speaker_user_id: Optional speaker identity from voice recognition

        Returns:
            Execution result dictionary with success, message, conversation_id,
            wait_for_input, and clear_history
        """
        conversation_id = self._generate_conversation_id()

        # Try node-side pre-routing (skip CC entirely)
        pre_result = self.try_pre_route(voice_command, conversation_id, speaker_user_id=speaker_user_id)
        if pre_result is not None:
            return pre_result

        logger.info("Starting conversation", conversation_id=conversation_id, command=voice_command)

        try:
            # Fire acknowledgment in background — speak while pipeline processes.
            # If the main pipeline returns before the ack finishes, join() will
            # wait briefly so we don't overlap TTS.
            ack_thread = threading.Thread(
                target=self._speak_acknowledgment,
                args=(voice_command,),
                daemon=True,
            )
            ack_thread.start()

            # Register available tools if requested
            if register_tools:
                self.register_tools_for_conversation(conversation_id, speaker_user_id=speaker_user_id)

            # Single unified request — handles audio, tool calls, and validation
            tag, payload = self.client.send_command_unified(voice_command, conversation_id)

            # Wait for acknowledgment TTS to finish before speaking main response
            ack_thread.join(timeout=5)

            if tag == "audio":
                # Streamed PCM audio — play it directly
                response, audio_meta = payload
                text = audio_meta.get("assistant_message", "")
                played = self._play_streaming_audio(response, audio_meta)
                if played:
                    logger.info("Streaming audio response played successfully")
                    return {
                        "success": True,
                        "message": text or "(streamed audio)",
                        "conversation_id": conversation_id,
                        "wait_for_input": False,
                        "clear_history": False,
                        "audio_played": True,
                    }
                # No audio bytes — fall back to the text we got from the header
                if text:
                    return {
                        "success": True,
                        "message": text,
                        "conversation_id": conversation_id,
                        "wait_for_input": False,
                        "clear_history": False,
                    }
                return self._handle_error("Empty audio response from server", conversation_id)

            if tag == "control":
                # JSON response (tool calls, validation, complete, error)
                return self._run_conversation_loop(payload, conversation_id, validation_handler, voice_command)

            # tag == "error"
            return self._handle_error(f"Command failed: {payload}", conversation_id)

        except Exception as e:
            logger.error("Error processing command", error=str(e))
            return self._handle_error(f"Error processing command: {str(e)}", conversation_id)

    def continue_conversation(
        self,
        conversation_id: str,
        message: str,
        validation_handler: Optional[Callable[[ValidationRequest], str]] = None,
    ) -> Dict[str, Any]:
        """
        Send a follow-up message to an existing conversation.

        Tools are already registered server-side (cached), so we skip registration
        and just send the message to continue the conversation.

        Args:
            conversation_id: Existing conversation ID to continue
            message: The follow-up message from the user
            validation_handler: Optional callback to handle validation requests

        Returns:
            Execution result dictionary with success, message, conversation_id,
            wait_for_input, and clear_history
        """
        logger.info("Continuing conversation", conversation_id=conversation_id, follow_up=message)

        try:
            response = self.client.send_command(message, conversation_id)

            if not response:
                return self._handle_error("Failed to communicate with Command Center", conversation_id)

            return self._run_conversation_loop(response, conversation_id, validation_handler, message)

        except Exception as e:
            logger.error("Error continuing conversation", error=str(e))
            return self._handle_error(f"Error continuing conversation: {str(e)}", conversation_id)

    def _run_conversation_loop(
        self,
        response: ToolCallingResponse,
        conversation_id: str,
        validation_handler: Optional[Callable[[ValidationRequest], str]] = None,
        voice_command: str = "",
    ) -> Dict[str, Any]:
        """
        Shared conversation loop that processes tool calls and validations until
        the conversation reaches a final state.

        Args:
            response: Initial response from the command center
            conversation_id: Current conversation ID
            validation_handler: Optional callback for validation requests
            voice_command: Original voice command for entity resolution

        Returns:
            Execution result dictionary
        """
        max_iterations = 10
        iteration = 0
        last_tool_result: Optional[ToolExecutionResult] = None
        prev_tool_signature: Optional[tuple] = None

        while not response.is_final() and iteration < max_iterations:
            iteration += 1
            logger.debug("Processing iteration", iteration=iteration, stop_reason=response.stop_reason)

            if response.requires_tool_execution():
                # Detect retry loop: if the LLM is calling the exact same
                # tool with the exact same arguments, it's stuck.
                current_signature = self._get_tool_signature(response.tool_calls)
                if prev_tool_signature is not None and current_signature == prev_tool_signature:
                    logger.warning("Detected repeated tool call, breaking retry loop",
                                   iteration=iteration)
                    error_detail = self._extract_tool_error(last_tool_result)
                    return {
                        "success": False,
                        "message": error_detail or "Sorry, I wasn't able to complete that request.",
                        "conversation_id": conversation_id,
                        "wait_for_input": False,
                        "clear_history": False,
                    }
                prev_tool_signature = current_signature

                logger.debug("Executing tools", count=len(response.tool_calls))
                last_tool_result = self._execute_tools(response.tool_calls, conversation_id, voice_command)

                response = self.client.send_tool_results(conversation_id, last_tool_result.api_results)

                if not response:
                    return self._handle_error("Failed to send tool results", conversation_id)

                # If any tool wants follow-up input, break AFTER sending results.
                # This gives the LLM one chance to generate a text response from
                # the tool result, while preventing infinite tool-call loops.
                if last_tool_result.wait_for_input:
                    logger.info("Tool requested follow-up input, pausing conversation loop")
                    break

            elif response.requires_validation():
                # Reset tool tracking after a validation step — the user
                # provided new input, so the same tool call is legitimate.
                prev_tool_signature = None
                logger.debug("Validation required", question=response.validation_request.question)

                if validation_handler:
                    user_response = validation_handler(response.validation_request)
                else:
                    user_response = self._default_validation_handler(response.validation_request)

                response = self.client.send_validation_response(
                    conversation_id,
                    response.validation_request,
                    user_response
                )

                if not response:
                    return self._handle_error("Failed to send validation response", conversation_id)

            else:
                return self._handle_error(f"Unknown stop_reason: {response.stop_reason}", conversation_id)

        if iteration >= max_iterations:
            return self._handle_error("Conversation exceeded maximum iterations", conversation_id)

        # Surface command-level signals from the last tool execution
        wait_for_input = last_tool_result.wait_for_input if last_tool_result else False
        clear_history = last_tool_result.clear_history if last_tool_result else False

        final_message = response.assistant_message

        # Adapter models may place conversational responses inside tool call
        # arguments (e.g. chat(message="I'm doing well!")) rather than in the
        # top-level message field. Extract it when the message is empty.
        if not final_message and wait_for_input and response.tool_calls:
            for tc in response.tool_calls:
                args = tc.function.get_arguments_dict()
                candidate = args.get("message", "")
                if candidate:
                    final_message = candidate
                    break

        if not final_message:
            final_message = "Go ahead, I'm listening." if wait_for_input else "Task completed."

        # If every tool call failed, don't trust the LLM's response — it may
        # hallucinate success.  Surface the actual error instead.
        all_failed = last_tool_result.all_failed if last_tool_result else False
        if all_failed and last_tool_result and last_tool_result.first_error:
            final_message = f"Sorry, that didn't work: {last_tool_result.first_error}"

        # If the tool provided a pre-formatted message (e.g., device status),
        # use it directly. Small LLMs misinterpret state data (e.g., "unlocked"
        # → "locked"), so the command's own message is more reliable.
        # Only applies to informational queries (wait_for_input=True), not
        # action commands where the LLM's natural confirmation is better.
        if (last_tool_result and last_tool_result.tool_message
                and not all_failed and wait_for_input):
            final_message = last_tool_result.tool_message

        logger.info("Conversation complete", response=final_message)

        return {
            "success": not all_failed,
            "message": final_message,
            "conversation_id": conversation_id,
            "wait_for_input": wait_for_input,
            "clear_history": clear_history,
        }

    def _execute_tools(
        self, tool_calls: List[ToolCall], conversation_id: str, voice_command: str = ""
    ) -> ToolExecutionResult:
        """
        Execute client-side tools and return results with aggregated signals.

        Args:
            tool_calls: List of tool calls to execute
            conversation_id: Current conversation ID
            voice_command: Original voice command for entity resolution

        Returns:
            ToolExecutionResult with API-formatted results and aggregated
            wait_for_input/clear_history signals from all executed commands
        """
        result = ToolExecutionResult()

        for tool_call in tool_calls:
            tool_name = tool_call.function.name
            logger.debug("Executing tool", tool=tool_name, tool_call_id=tool_call.id)

            try:
                command = self.command_discovery.get_command(tool_name)

                if not command:
                    error_msg = f"Unknown tool: {tool_name}"
                    logger.error("Unknown tool", tool=tool_name)
                    result.api_results.append(format_tool_error(tool_call.id, error_msg))
                    continue

                arguments = tool_call.function.get_arguments_dict()
                arguments = command.post_process_tool_call(arguments, voice_command)

                user_id = self._conversation_users.get(conversation_id)
                request_info = RequestInformation(
                    voice_command=voice_command or f"Tool call: {tool_name}",
                    conversation_id=conversation_id,
                    is_validation_response=False,
                    user_id=user_id,
                )

                from jarvis_command_sdk.context import set_current_user_id
                set_current_user_id(user_id)
                try:
                    command_response: CommandResponse = command.execute(request_info, **arguments)
                finally:
                    set_current_user_id(None)

                # Aggregate signals: OR across all tool responses
                if command_response.wait_for_input:
                    result.wait_for_input = True
                if command_response.clear_history:
                    result.clear_history = True

                result.api_results.append(format_tool_result(tool_call.id, command_response))

                if not command_response.success and result.first_error is None:
                    result.first_error = tool_call.failure_message or command_response.error_details

                # Capture pre-formatted message from the command so the
                # conversation loop can use it directly instead of relying
                # on the LLM to interpret raw state data.
                if command_response.success and result.tool_message is None:
                    ctx = command_response.context_data or {}
                    msg = ctx.get("message")
                    if msg and isinstance(msg, str):
                        result.tool_message = msg

                logger.debug("Tool executed successfully", tool=tool_name)

            except Exception as e:
                error_msg = str(e)
                logger.error("Tool execution error", tool=tool_name, error=error_msg)
                result.api_results.append(format_tool_error(tool_call.id, error_msg))
                if result.first_error is None:
                    result.first_error = error_msg

        # Check if ALL tool results were failures
        if result.api_results:
            result.all_failed = all(
                not r.get("output", {}).get("success", False) for r in result.api_results
            )

        return result

    @staticmethod
    def _get_tool_signature(tool_calls: List[ToolCall]) -> tuple:
        """Build a hashable signature from a list of tool calls."""
        return tuple(
            (tc.function.name, tc.function.arguments) for tc in tool_calls
        )

    @staticmethod
    def _extract_tool_error(last_result: Optional[ToolExecutionResult]) -> str:
        """Pull a readable error message from the most recent tool result."""
        if not last_result:
            return ""
        for item in last_result.api_results:
            output = item.get("output", {})
            if not output.get("success") and output.get("error"):
                return f"Sorry, that didn't work: {output['error']}"
        return ""

    def try_pre_route(self, voice_command: str, conversation_id: str, speaker_user_id: int | None = None) -> Dict[str, Any] | None:
        """Try node-side pre-routing across all discovered commands.

        Iterates commands, calls pre_route() on each.  First match wins.
        If matched, executes the command directly and returns the result
        dict — no CC contact at all.

        Returns:
            Result dict (same shape as process_voice_command), or None to
            fall through to the normal LLM path.
        """
        commands = self.command_discovery.get_all_commands()
        for command in commands.values():
            pre = command.pre_route(voice_command)
            if pre is None:
                continue

            logger.info(
                "Pre-routed to command",
                command=command.command_name,
                voice_command=voice_command,
            )

            try:
                request_info = RequestInformation(
                    voice_command=voice_command,
                    conversation_id=conversation_id,
                    is_validation_response=False,
                    user_id=speaker_user_id,
                )

                from jarvis_command_sdk.context import set_current_user_id
                set_current_user_id(speaker_user_id)
                try:
                    command_response: CommandResponse = command.execute(request_info, **pre.arguments)
                finally:
                    set_current_user_id(None)

                message = pre.spoken_response
                if not message:
                    ctx = command_response.context_data or {}
                    message = ctx.get("message", "Done.")

                return {
                    "success": command_response.success,
                    "message": message,
                    "conversation_id": conversation_id,
                    "wait_for_input": False,
                    "clear_history": False,
                }
            except Exception as e:
                logger.error(
                    "Pre-route execution failed, falling through to LLM",
                    command=command.command_name,
                    error=str(e),
                )
                return None

        return None

    def _default_validation_handler(self, validation: ValidationRequest) -> str:
        """
        Default validation handler - placeholder for now

        In practice, this should prompt the user via TTS and listen for their response.
        For now, we'll return a simple error message.

        Args:
            validation: The validation request

        Returns:
            User's response (or error message)
        """
        logger.warning("Default validation handler called - should be overridden",
                       question=validation.question,
                       options=validation.options)

        return "I'm not sure - please try rephrasing your request."

    def _generate_conversation_id(self) -> str:
        """Generate unique conversation ID for each voice interaction"""
        return str(uuid.uuid4())

    def _handle_error(self, message: str, conversation_id: str) -> Dict[str, Any]:
        """Handle general errors"""
        return {
            "success": False,
            "message": message,
            "conversation_id": conversation_id,
            "wait_for_input": False,
            "clear_history": False,
        }

    def _speak_acknowledgment(self, voice_command: str) -> None:
        """Fetch and speak a fast LLM-generated acknowledgment (background thread).

        Called in parallel with the main pipeline so the user hears something
        like "Let me look into that." within ~2s instead of 20s of silence.
        """
        try:
            text = self.client.get_acknowledgment(voice_command)
            if text:
                tts_provider = get_tts_provider()
                tts_provider.speak(False, text)
        except Exception as e:
            logger.debug("Acknowledgment TTS failed (non-fatal)", error=str(e))

    def speak_result(self, result: Dict[str, Any]) -> None:
        """Speak the result of command execution.

        Skips TTS when streaming audio was already played by
        ``process_voice_command`` to avoid double-speaking.

        Uses streaming for long responses (> 200 chars) to avoid
        buffering the entire WAV and hitting playback timeouts.
        """
        if result.get("audio_played"):
            return
        tts_provider = get_tts_provider()
        message = result.get("message", "An error occurred")

        # Use streaming for long responses (briefings, stories, etc.)
        if len(message) > 200 and hasattr(tts_provider, "speak_stream"):
            if tts_provider.speak_stream(message):
                return
            # Fall through to blocking speak if streaming fails

        tts_provider.speak(False, message)
