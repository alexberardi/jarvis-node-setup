import json
from typing import Any, Type, TypeVar, Optional, List, Dict
from urllib.parse import unquote

from jarvis_log_client import JarvisLogger
from pydantic import BaseModel

from core.ijarvis_command import IJarvisCommand
from .responses.jarvis_command_center import DateContext, ToolCallingResponse, ValidationRequest
from .rest_client import RestClient
from utils.config_loader import Config
from utils.timezone_util import get_user_timezone

logger = JarvisLogger(service="jarvis-node")

T = TypeVar("T", bound=BaseModel)

class JarvisCommandCenterClient:
    def __init__(self, base_url: str):
        self.base_url = base_url
    
    def get_date_context(self) -> Optional[DateContext]:
        """Get the current date context from the server"""
        try:
            timezone = get_user_timezone()
            response = RestClient.get(f"{self.base_url}/api/v0/generate/date-context?timezone={timezone}")

            if not response:
                logger.warning("No response from date-context endpoint")
                return None

            # Parse the response into a DateContext object
            try:
                date_context = DateContext.model_validate(response)
                logger.debug("Fetched date context", timezone=date_context.timezone.user_timezone)
                return date_context
            except Exception as parse_error:
                logger.error("Failed to parse date context response", error=str(parse_error), response=response)
                return None

        except Exception as e:
            logger.error("Failed to get date context", error=str(e))
            return None

    def send_command(self, voice_command: str, conversation_id: str) -> Optional[ToolCallingResponse]:
        """
        Send a voice command to the Command Center for processing
        
        Args:
            voice_command: The transcribed voice command
            conversation_id: Unique conversation identifier
            
        Returns:
            ToolCallingResponse with stop_reason, tool_calls, validation, or final message
        """
        payload = {
            "voice_command": voice_command,
            "conversation_id": conversation_id
        }
            
        logger.info("Sending voice command", command=voice_command, conversation_id=conversation_id)

        try:
            response = RestClient.post(f"{self.base_url}/api/v0/voice/command", timeout=30, data=payload)

            if not response:
                return None

            # Parse response into ToolCallingResponse
            return ToolCallingResponse.model_validate(response)
        except Exception as e:
            logger.error("Failed to send command", error=str(e))
            return None
    
    def send_command_stream(
        self,
        voice_command: str,
        conversation_id: str,
        chunk_size: int = 4096,
    ) -> tuple[Any | None, dict[str, str]]:
        """Send a voice command and get streaming PCM audio back.

        Args:
            voice_command: The transcribed voice command
            conversation_id: Unique conversation identifier
            chunk_size: Size of audio chunks to iterate

        Returns:
            Tuple of (response object for iteration, audio metadata dict).
            Returns (None, {}) if streaming fails.
        """
        payload = {
            "voice_command": voice_command,
            "conversation_id": conversation_id,
        }

        logger.info(
            "Sending streaming voice command",
            command=voice_command,
            conversation_id=conversation_id,
        )

        try:
            response = RestClient.post_stream(
                f"{self.base_url}/api/v0/voice/command/stream",
                timeout=30,
                data=payload,
                chunk_size=chunk_size,
            )

            if not response:
                return None, {}

            # Read audio format from response headers
            audio_meta = {
                "sample_rate": response.headers.get("X-Audio-Sample-Rate", "22050"),
                "channels": response.headers.get("X-Audio-Channels", "1"),
                "sample_width": response.headers.get("X-Audio-Sample-Width", "2"),
            }

            return response, audio_meta
        except Exception as e:
            logger.error("Failed to send streaming command", error=str(e))
            return None, {}

    def send_command_unified(
        self,
        voice_command: str,
        conversation_id: str,
    ) -> tuple[str, Any]:
        """Send a voice command to the unified streaming endpoint.

        The endpoint returns either streamed audio (200) or JSON control
        data (202). This method inspects the status code and returns a
        tagged result so the caller knows which path to take.

        Args:
            voice_command: The transcribed voice command
            conversation_id: Unique conversation identifier

        Returns:
            A tuple of (tag, payload):
            - ("audio", (response, audio_meta)) — caller streams PCM
            - ("control", ToolCallingResponse) — caller enters tool loop
            - ("error", message) — something went wrong
        """
        payload = {
            "voice_command": voice_command,
            "conversation_id": conversation_id,
        }

        logger.info(
            "Sending unified voice command",
            command=voice_command,
            conversation_id=conversation_id,
        )

        try:
            response = RestClient.post_stream(
                f"{self.base_url}/api/v0/voice/command/stream",
                timeout=30,
                data=payload,
            )

            if not response:
                return ("error", "No response from command center")

            if response.status_code == 200:
                # Streamed audio response
                raw_msg = response.headers.get("X-Assistant-Message", "")
                audio_meta = {
                    "sample_rate": response.headers.get("X-Audio-Sample-Rate", "22050"),
                    "channels": response.headers.get("X-Audio-Channels", "1"),
                    "sample_width": response.headers.get("X-Audio-Sample-Width", "2"),
                    "assistant_message": unquote(raw_msg) if raw_msg else "",
                }
                return ("audio", (response, audio_meta))

            if response.status_code == 202:
                # JSON control response (tool calls, validation, etc.)
                try:
                    body = json.loads(response.content)
                    tool_response = ToolCallingResponse.model_validate(body)
                    response.close()
                    return ("control", tool_response)
                except Exception as parse_err:
                    response.close()
                    return ("error", f"Failed to parse 202 response: {parse_err}")

            # Unexpected status code
            response.close()
            return ("error", f"Unexpected status {response.status_code}")

        except Exception as e:
            logger.error("Unified command failed", error=str(e))
            return ("error", str(e))

    def get_acknowledgment(self, voice_command: str) -> str | None:
        """Get a fast LLM-generated acknowledgment from CC (~1s).

        Called in parallel with the main command pipeline so the node can
        speak a quick acknowledgment while the full response is processing.

        Returns:
            Acknowledgment text (e.g., "Let me look into that."), or None on failure.
        """
        try:
            result = RestClient.post(
                f"{self.base_url}/api/v0/voice/acknowledge",
                data={"voice_command": voice_command},
                timeout=5,
            )
            return result.get("text") if result else None
        except Exception:
            return None

    def send_tool_results(
        self,
        conversation_id: str,
        tool_results: List[Dict[str, Any]],
    ) -> Optional[ToolCallingResponse]:
        """
        Send tool execution results back to continue the conversation
        
        Args:
            conversation_id: The conversation identifier
            tool_results: List of tool execution results in format:
                         [{"tool_call_id": "...", "output": {...}}]
            
        Returns:
            ToolCallingResponse with next action
        """
        payload = {
            "conversation_id": conversation_id,
            "tool_results": tool_results,
        }
        
        logger.debug("Sending tool results", count=len(tool_results), conversation_id=conversation_id)

        try:
            response = RestClient.post(f"{self.base_url}/api/v0/voice/command/continue", timeout=30, data=payload)

            if not response:
                return None

            return ToolCallingResponse.model_validate(response)
        except Exception as e:
            logger.error("Failed to send tool results", error=str(e))
            return None
    
    def send_validation_response(
        self,
        conversation_id: str,
        validation_request: ValidationRequest,
        user_response: str,
    ) -> Optional[ToolCallingResponse]:
        """
        Send user's validation/clarification response back to continue conversation
        
        Args:
            conversation_id: The conversation identifier
            validation_request: The validation/clarification prompt from the server
            user_response: The user's chosen answer
            
        Returns:
            ToolCallingResponse with next action
        """

        # Build ToolResultRequest payload expected by /voice/command/continue
        tool_call_id = getattr(validation_request, "tool_call_id", None)
        if not tool_call_id:
            # Fallback to a deterministic placeholder if missing
            tool_call_id = "validation-response"

        payload = {
            "conversation_id": conversation_id,
            "tool_results": [
                {
                    "tool_call_id": tool_call_id,
                    "output": {
                        "answer": user_response,
                        "parameter_name": validation_request.parameter_name,
                        "question": validation_request.question,
                        "options": validation_request.options,
                    },
                }
            ],
        }
        
        logger.debug("Sending validation response", conversation_id=conversation_id)

        try:
            response = RestClient.post(f"{self.base_url}/api/v0/voice/command/continue", timeout=30, data=payload)

            if not response:
                return None

            return ToolCallingResponse.model_validate(response)
        except Exception as e:
            logger.error("Failed to send validation response", error=str(e))
            return None

    def train_tool_router(self, payload: Dict[str, Any], timeout: int = 120) -> Optional[Dict[str, Any]]:
        """
        Train the tool router FastText model on the command center.

        Args:
            payload: ToolRouterTrainingRequest body.
            timeout: Request timeout in seconds.

        Returns:
            JSON response dict on success, or None on error.
        """
        try:
            response = RestClient.post(
                f"{self.base_url}/api/v0/tool-router/train",
                timeout=timeout,
                data=payload,
            )
            return response
        except Exception as e:
            logger.error("Failed to train tool router", error=str(e))
            return None

    def train_node_adapter(self, payload: Dict[str, Any], timeout: int = 120) -> Optional[Dict[str, Any]]:
        """
        Request node adapter training on the command center.

        Args:
            payload: Adapter training request body.
            timeout: Request timeout in seconds.

        Returns:
            JSON response dict on success, or None on error.
        """
        try:
            response = RestClient.post(
                f"{self.base_url}/api/v0/adapters/train",
                timeout=timeout,
                data=payload,
            )
            return response
        except Exception as e:
            logger.error("Failed to train node adapter", error=str(e))
            return None

    def get_adapter_job_status(self, job_id: str, timeout: int = 10) -> Optional[Dict[str, Any]]:
        """
        Fetch adapter training job status.
        """
        try:
            response = RestClient.get(
                f"{self.base_url}/api/v0/adapters/jobs/{job_id}",
                timeout=timeout,
            )
            return response
        except Exception as e:
            logger.error("Failed to fetch adapter job status", error=str(e), job_id=job_id)
            return None

    def chat_text(self, message: str) -> Optional[str]:
        """Send a chat message and return raw text content (no Pydantic parsing).

        Useful for open-ended questions where the response is plain text.
        """
        response = RestClient.post(f"{self.base_url}/api/v0/chat", {
            "messages": [
                {"role": "system", "content": message}
            ]
        })

        if not response:
            return None

        try:
            return response["choices"][0]["message"]["content"]
        except (KeyError, TypeError, IndexError) as e:
            logger.warning("Failed to extract chat text", error=str(e))
            return None

    def chat(self, message: str, model: Type[T]) -> Optional[T]:
        response = RestClient.post(f"{self.base_url}/api/v0/chat", {
            "messages": [
                {"role": "system", "content": message}
            ]
        })
        logger.debug("Chat response received", response=response)

        if not response:
            return None

        try:
            content = response["choices"][0]["message"]["content"]
            logger.debug("Chat content", content=content)
            return model.model_validate_json(content)
        except (KeyError, ValueError, TypeError) as e:
            logger.warning("Failed to parse LLM response", error=str(e))

            # Enhanced fallback: try to extract JSON from mixed content
            try:
                if isinstance(content, str):
                    # Try to find JSON in the content
                    json_start = content.find('{')
                    json_end = content.rfind('}')

                    if json_start != -1 and json_end != -1 and json_end > json_start:
                        # Extract just the JSON portion
                        json_content = content[json_start:json_end + 1]
                        logger.debug("Extracted JSON from mixed content", json_content=json_content)
                        return model.model_validate_json(json_content)
                    elif not content.strip().startswith('{'):
                        # If no JSON found, wrap plain text in the expected JSON format
                        wrapped_content = f'{{"response": "{content.strip()}"}}'
                        logger.debug("Attempting fallback JSON wrapping", wrapped_content=wrapped_content)
                        return model.model_validate_json(wrapped_content)
            except Exception as fallback_error:
                logger.warning("Enhanced fallback parsing also failed", error=str(fallback_error))

            return None

    def lightweight_chat(self, message: str, model: Type[T]) -> Optional[T]:
        response = RestClient.post(f"{self.base_url}/api/v0/lightweight/chat", {
            "messages": [
                {"role": "system", "content": message}
            ]
        })

        if not response:
            return None

        try:
            content = response["choices"][0]["message"]["content"]
            logger.debug("Lightweight chat content", content=content)
            return model.model_validate_json(content)
        except (KeyError, ValueError, TypeError) as e:
            logger.warning("Failed to parse lightweight LLM response", error=str(e))

            # Enhanced fallback: try to extract JSON from mixed content
            try:
                if isinstance(content, str):
                    # Try to find JSON in the content
                    json_start = content.find('{')
                    json_end = content.rfind('}')

                    if json_start != -1 and json_end != -1 and json_end > json_start:
                        # Extract just the JSON portion
                        json_content = content[json_start:json_end + 1]
                        logger.debug("Extracted JSON from mixed content", json_content=json_content)
                        return model.model_validate_json(json_content)
                    elif not content.strip().startswith('{'):
                        # If no JSON found, wrap plain text in the expected JSON format
                        wrapped_content = f'{{"response": "{content.strip()}"}}'
                        logger.debug("Attempting fallback JSON wrapping", wrapped_content=wrapped_content)
                        return model.model_validate_json(wrapped_content)
            except Exception as fallback_error:
                logger.warning("Enhanced fallback parsing also failed", error=str(fallback_error))

            return None

    def start_conversation(
        self,
        conversation_id: str,
        commands: dict[str, IJarvisCommand],
        date_context: Optional[DateContext] = None,
        speaker_user_id: Optional[int] = None,
        agents: Optional[dict] = None,
    ) -> bool:
        """
        Start a conversation session and register available client-side tools.

        Args:
            conversation_id: UUID for the conversation session
            commands: Dictionary of available commands to send to the command center
            date_context: Optional date context to use for tool schemas
            speaker_user_id: Optional speaker identity from voice recognition
            agents: Optional agent context to inject (e.g., Home Assistant data).
                    If provided, overrides auto-discovered agent context.

        Returns:
            True if successful, False otherwise
        """
        # Get date context if not provided
        if date_context is None:
            date_context = self.get_date_context()

        # Check if we should skip warmup inference (useful for vLLM which doesn't cache KV)
        skip_warmup_raw = Config.get("skip_warmup_inference", "false")
        skip_warmup = str(skip_warmup_raw).lower() in ("true", "1", "yes")

        node_context = {
            "timezone": get_user_timezone()
        }

        # Add speaker identity from voice recognition
        if speaker_user_id is not None:
            node_context["speaker_user_id"] = speaker_user_id

        # Add agent context (Home Assistant, etc.)
        if agents:
            node_context["agents"] = agents
            logger.debug("Injected caller-provided agent context", agents=list(agents.keys()))
        else:
            try:
                from services.agent_scheduler_service import get_agent_scheduler_service
                agent_context = get_agent_scheduler_service().get_aggregated_context()
                if agent_context:
                    node_context["agents"] = agent_context
                    logger.debug("Injected agent context", agents=list(agent_context.keys()))
            except Exception as e:
                logger.warning("Failed to get agent context", error=str(e))

        # Build available commands metadata for server-side tools
        available_commands = []
        for cmd in commands.values():
            schema = cmd.get_command_schema(date_context)
            logger.debug("Warmup keywords", command=schema.get('command_name'), keywords=schema.get('keywords', []))
            available_commands.append(schema)

        # Build client tools array in OpenAI format
        client_tools = []
        for cmd in commands.values():
            logger.debug("Registering tool", tool=cmd.command_name)
            tool_schema = cmd.to_openai_tool_schema(date_context)
            client_tools.append(tool_schema)

        payload = {
            "conversation_id": conversation_id,
            "node_context": node_context,
            "available_commands": available_commands,
            "client_tools": client_tools,
            "skip_warmup_inference": skip_warmup
        }

        try:
            response = RestClient.post(f"{self.base_url}/api/v0/conversation/start", timeout=30, data=payload)
            if response and response.get("status") == "success":
                logger.info("Successfully registered tools", count=len(client_tools))
                return True
            return False
        except Exception as e:
            logger.error("Failed to start conversation", error=str(e))
            return False



