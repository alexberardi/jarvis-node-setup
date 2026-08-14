import json
from typing import Any, Type, TypeVar, Optional, List, Dict
from urllib.parse import unquote

from jarvis_log_client import JarvisLogger
from pydantic import BaseModel

from jarvis_command_sdk import IJarvisCommand
from .responses.jarvis_command_center import DateContext, ToolCallingResponse, ValidationRequest
from .rest_client import RestClient
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

    def send_command(
        self,
        voice_command: str,
        conversation_id: str,
        turn_source: str | None = None,
        follow_up_iteration: int | None = None,
    ) -> Optional[ToolCallingResponse]:
        """
        Send a voice command to the Command Center for processing

        Args:
            voice_command: The transcribed voice command
            conversation_id: Unique conversation identifier
            turn_source: How the mic came to be open ("wake"/"follow_up").
                        CC uses it to pick the not_for_me posture.
            follow_up_iteration: 1-based follow-up window iteration.

        Returns:
            ToolCallingResponse with stop_reason, tool_calls, validation, or final message
        """
        payload: dict[str, Any] = {
            "voice_command": voice_command,
            "conversation_id": conversation_id
        }
        if turn_source is not None:
            payload["turn_source"] = turn_source
        if follow_up_iteration is not None:
            payload["follow_up_iteration"] = follow_up_iteration

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
        speaker_user_id: int | None = None,
        pre_wake_speech_seconds: float | None = None,
        affect: dict[str, Any] | None = None,
        turn_source: str | None = None,
        wake_confidence: float | None = None,
        follow_up_iteration: int | None = None,
    ) -> tuple[str, Any]:
        """Send a voice command to the unified streaming endpoint.

        The endpoint returns either streamed audio (200) or JSON control
        data (202). This method inspects the status code and returns a
        tagged result so the caller knows which path to take.

        Args:
            voice_command: The transcribed voice command
            conversation_id: Unique conversation identifier
            speaker_user_id: Actual speaker from STT (for mismatch detection
                            when warmup used a cached speaker ID)
            pre_wake_speech_seconds: Optional seconds of speech-like audio
                            detected just before the wake word fired
                            (from the node's pre-wake VAD ring buffer).
                            CC surfaces this to the LLM as a direction hint.
            affect: Optional acoustic-affect read from whisper
                            (``{read, arousal, confidence}``). Opaque to the
                            node; CC turns it into a per-turn tone hint.
            turn_source: How the mic came to be open — "wake" (wake-word
                            fire) or "follow_up" (window kept open after
                            TTS). Selects CC's not_for_me posture.
            wake_confidence: OWW detection score for the wake fire (0-1).
            follow_up_iteration: 1-based follow-up window iteration.

        Returns:
            A tuple of (tag, payload):
            - ("audio", (response, audio_meta)) — caller streams PCM
            - ("control", ToolCallingResponse) — caller enters tool loop
            - ("error", message) — something went wrong
        """
        payload: dict[str, Any] = {
            "voice_command": voice_command,
            "conversation_id": conversation_id,
        }
        if speaker_user_id is not None:
            payload["speaker_user_id"] = speaker_user_id
        if pre_wake_speech_seconds is not None:
            payload["pre_wake_speech_seconds"] = pre_wake_speech_seconds
        if affect is not None:
            payload["affect"] = affect
        if turn_source is not None:
            payload["turn_source"] = turn_source
        if wake_confidence is not None:
            payload["wake_confidence"] = wake_confidence
        if follow_up_iteration is not None:
            payload["follow_up_iteration"] = follow_up_iteration

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

    def send_tool_results_stream(
        self,
        conversation_id: str,
        tool_results: List[Dict[str, Any]],
    ) -> tuple[Any, Dict[str, str]]:
        """Streaming variant of send_tool_results.

        Calls /voice/command/continue/stream which either streams PCM audio
        back (200 audio/raw) when the post-tool LLM call produces a natural
        text response, or returns 202 JSON to signal "fall back to blocking".

        Returns:
            (response, audio_meta) when streaming succeeded — caller plays
            the PCM directly via response.iter_content().
            (None, {}) when the server signaled fallback or the request
            failed — caller should call send_tool_results() instead.
        """
        payload = {
            "conversation_id": conversation_id,
            "tool_results": tool_results,
        }

        try:
            response = RestClient.post_stream(
                f"{self.base_url}/api/v0/voice/command/continue/stream",
                timeout=60,
                data=payload,
            )

            if not response:
                return (None, {})

            if response.status_code == 200:
                audio_meta = {
                    "sample_rate": response.headers.get("X-Audio-Sample-Rate", "22050"),
                    "channels": response.headers.get("X-Audio-Channels", "1"),
                    "sample_width": response.headers.get("X-Audio-Sample-Width", "2"),
                    "assistant_message": "",
                }
                return (response, audio_meta)

            # 202 (or other non-200) — server signaled fallback
            try:
                response.close()
            except Exception:
                pass
            return (None, {})

        except Exception as e:
            logger.error("Failed streaming tool results", error=str(e))
            return (None, {})

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
        speaker_confidence: Optional[float] = None,
        agents: Optional[dict] = None,
        adapter_settings: Optional[dict] = None,
        recently_shown_items: Optional[list] = None,
    ) -> bool:
        """
        Start a conversation session and register available client-side tools.

        Args:
            conversation_id: UUID for the conversation session
            commands: Dictionary of available commands to send to the command center
            date_context: Optional date context to use for tool schemas
            speaker_user_id: Optional speaker identity from voice recognition
            speaker_confidence: Optional confidence score (0-1) for the identification.
                                CC uses this to gate the per-node speaker stickiness
                                cache — only high-confidence IDs are remembered for
                                short follow-up inheritance.
            agents: Optional agent context to inject (e.g., Home Assistant data).
                    If provided, overrides auto-discovered agent context.
            adapter_settings: Optional dict with {hash, scale, enabled} to override
                              the server-side adapter for eval/test runs. Server
                              honors this only when JARVIS_TEST_MODE=1 is set.

        Returns:
            True if successful, False otherwise
        """
        # Get date context if not provided
        if date_context is None:
            date_context = self.get_date_context()

        node_context = {
            "timezone": get_user_timezone()
        }

        # Add speaker identity from voice recognition
        if speaker_user_id is not None:
            node_context["speaker_user_id"] = speaker_user_id
        if speaker_confidence is not None:
            node_context["speaker_confidence"] = float(speaker_confidence)

        # Carry the most-recently-surfaced list so CC can re-inject the
        # RECENTLY SHOWN block on this (possibly fresh) conversation — the
        # follow-up "mark those as read" path after a pre-route / re-wake.
        if recently_shown_items:
            node_context["recently_shown_items"] = recently_shown_items

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

        # Build both the server-side command metadata (prompt schema) and the
        # OpenAI-format client tool schema for each command — together, per
        # command, and fault-isolated. If EITHER schema fails to build for a
        # command, that command is dropped from BOTH lists (kept aligned) with a
        # loud warning, instead of the exception taking down the whole tool set
        # and failing the entire voice turn. A third-party command with any
        # schema-time bug degrades to "that one command is unavailable this turn",
        # never "no response from command center".
        available_commands = []
        client_tools = []
        skipped: list[str] = []
        for name, cmd in commands.items():
            try:
                schema = cmd.get_command_schema(date_context)
                tool_schema = cmd.to_openai_tool_schema(date_context)
                # A command that returns a non-dict schema WITHOUT raising is just
                # as broken as one that raises — handle it uniformly (skip), so the
                # malformed schema isn't advertised and the log reads below can't
                # throw past the isolation point.
                if not isinstance(schema, dict) or not isinstance(tool_schema, dict):
                    raise TypeError(
                        f"schema/tool_schema must be dicts, got "
                        f"{type(schema).__name__}/{type(tool_schema).__name__}"
                    )
                cmd_name = schema.get('command_name')
                keywords = schema.get('keywords', [])
            except Exception as e:
                skipped.append(name)
                logger.error(
                    "Skipping command with invalid schema — unavailable this turn",
                    command=name, error=str(e),
                )
                continue
            # Both schemas built cleanly — append together so the two lists stay
            # aligned (never a tool in one list but missing from the other).
            logger.debug("Warmup keywords", command=cmd_name, keywords=keywords)
            available_commands.append(schema)
            logger.debug("Registering tool", tool=name)
            client_tools.append(tool_schema)

        if skipped:
            logger.warning(
                "Some commands were skipped during registration",
                skipped=skipped, registered=len(client_tools),
            )
        # Total-failure blind spot: commands existed but every one was dropped.
        # That warms a zero-tool conversation (LLM can call nothing) — a silent
        # capability blackout, so surface it at error level for alerting rather
        # than returning a quiet "success".
        if commands and not client_tools:
            logger.error(
                "All commands failed schema-building — registering an EMPTY toolset",
                command_count=len(commands), skipped=skipped,
            )

        payload = {
            "conversation_id": conversation_id,
            "node_context": node_context,
            "available_commands": available_commands,
            "client_tools": client_tools,
        }
        if adapter_settings:
            payload["adapter_settings"] = adapter_settings

        try:
            response = RestClient.post(f"{self.base_url}/api/v0/conversation/start", timeout=30, data=payload)
            if response and response.get("status") == "success":
                logger.info("Successfully registered tools", count=len(client_tools))
                self._persist_home_context(response.get("home_context"))
                return True
            return False
        except Exception as e:
            logger.error("Failed to start conversation", error=str(e))
            return False

    def _persist_home_context(self, home_context: Optional[dict]) -> None:
        """Persist the household home_context command-center injected (location, ...)
        into config.json — ONE node-local source that both commands (via
        RequestInformation.home_context) and background agents (via the SDK
        ContextVar / Config.get_dict) read, so neither needs a duplicated location
        secret nor a round-trip back to CC. Diff-guarded to avoid disk churn; every
        conversation/start reasserts the value (self-healing). Best-effort — a
        failure here never fails conversation start."""
        try:
            from utils.config_service import Config
            from utils.config_file import update_config_file

            new = home_context or None
            if new == (Config.get_dict("home_context") or None):
                return  # unchanged
            def _mutate(cfg: dict) -> None:
                if new:
                    cfg["home_context"] = new
                else:
                    cfg.pop("home_context", None)
            update_config_file(_mutate)
            logger.info("Updated home_context", location=(new or {}).get("location"))
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to persist home_context", error=str(e))

    def end_conversation(self, conversation_id: str) -> bool:
        """Notify CC that this wake-cycle is complete.

        Best-effort fire-and-forget. Currently the only server-side effect
        is clearing this node's speaker stickiness so a later wake event by
        a different speaker doesn't inherit the previous speaker. Failures
        log a warning but don't surface to the caller — the wake-cycle has
        already finished, there's nothing to fall back to.
        """
        try:
            response = RestClient.post(
                f"{self.base_url}/api/v0/conversation/end",
                timeout=5,
                data={"conversation_id": conversation_id},
            )
            if not response:
                logger.warning("conversation/end returned no body", conversation_id=conversation_id)
                return False
            return True
        except Exception as e:
            logger.warning("Failed to end conversation", error=str(e), conversation_id=conversation_id)
            return False


