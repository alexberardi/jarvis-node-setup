import uuid
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional, Callable

from jarvis_log_client import JarvisLogger

from clients.jarvis_command_center_client import JarvisCommandCenterClient
from clients.responses.jarvis_command_center import ToolCallingResponse, ToolCall, ValidationRequest
from core.command_response import CommandResponse
from core.helpers import get_tts_provider
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


class CommandExecutionService:
    def __init__(self):
        self.command_center_url = get_command_center_url()
        self.node_id = Config.get_str("node_id")
        self.room = Config.get_str("room")
        self.command_discovery = get_command_discovery_service()
        self.client = JarvisCommandCenterClient(self.command_center_url)
        # Force initial discovery
        self.command_discovery.refresh_now()

    def register_tools_for_conversation(self, conversation_id: str) -> bool:
        """
        Register available client-side tools with the Command Center for a conversation

        Args:
            conversation_id: The conversation identifier

        Returns:
            True if successful, False otherwise
        """
        commands = self.command_discovery.get_all_commands()

        if not commands:
            logger.warning("No commands available to register")
            return False

        logger.info("Registering tools for conversation", count=len(commands), conversation_id=conversation_id)

        try:
            # Get date context
            date_context = self.client.get_date_context()

            # Start conversation with available commands
            success = self.client.start_conversation(conversation_id, commands, date_context)

            if success:
                logger.info("Successfully registered tools", count=len(commands))
            else:
                logger.error("Failed to register tools")

            return success

        except Exception as e:
            logger.error("Error registering tools", error=str(e))
            return False

    def process_voice_command(
        self,
        voice_command: str,
        validation_handler: Optional[Callable[[ValidationRequest], str]] = None,
        register_tools: bool = True
    ) -> Dict[str, Any]:
        """
        Process a voice command through the tool calling pipeline:
        1. Optionally register available tools with the server
        2. Send to Jarvis Command Center for LLM processing
        3. Loop on response stop_reason:
           - tool_call: Execute tools locally and send results back
           - validation: Ask user for clarification and send response back
           - stop/complete: Return final message

        Args:
            voice_command: The transcribed voice command
            validation_handler: Optional callback to handle validation requests
                               If None, default behavior is used
            register_tools: Whether to register available tools before processing (default: True)

        Returns:
            Execution result dictionary with success, message, conversation_id,
            wait_for_input, and clear_history
        """
        conversation_id = self._generate_conversation_id()

        logger.info("Starting conversation", conversation_id=conversation_id, command=voice_command)

        try:
            # Register available tools if requested
            if register_tools:
                self.register_tools_for_conversation(conversation_id)

            # Initial request to Command Center
            response = self.client.send_command(voice_command, conversation_id)

            if not response:
                return self._handle_error("Failed to communicate with Command Center", conversation_id)

            return self._run_conversation_loop(response, conversation_id, validation_handler, voice_command)

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

        while not response.is_final() and iteration < max_iterations:
            iteration += 1
            logger.debug("Processing iteration", iteration=iteration, stop_reason=response.stop_reason)

            if response.requires_tool_execution():
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

        logger.info("Conversation complete", response=final_message)

        return {
            "success": True,
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

                request_info = RequestInformation(
                    voice_command=voice_command or f"Tool call: {tool_name}",
                    conversation_id=conversation_id,
                    is_validation_response=False
                )

                command_response: CommandResponse = command.execute(request_info, **arguments)

                # Aggregate signals: OR across all tool responses
                if command_response.wait_for_input:
                    result.wait_for_input = True
                if command_response.clear_history:
                    result.clear_history = True

                result.api_results.append(format_tool_result(tool_call.id, command_response))

                logger.debug("Tool executed successfully", tool=tool_name)

            except Exception as e:
                error_msg = str(e)
                logger.error("Tool execution error", tool=tool_name, error=error_msg)
                result.api_results.append(format_tool_error(tool_call.id, error_msg))

        return result

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

    def speak_result(self, result: Dict[str, Any]):
        """Speak the result of command execution"""
        tts_provider = get_tts_provider()
        message = result.get("message", "An error occurred")
        tts_provider.speak(False, message)
