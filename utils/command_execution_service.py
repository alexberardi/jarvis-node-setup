import queue
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Callable

from jarvis_log_client import JarvisLogger

from clients.jarvis_command_center_client import JarvisCommandCenterClient
from clients.responses.jarvis_command_center import ToolCallingResponse, ToolCall, ValidationRequest
from core.command_response import CommandResponse
from core.helpers import get_tts_provider
from core.music_control import _resolve_takeover_binary, stop_other_music_players
from core.platform_audio import platform_audio
from core.request_information import RequestInformation
from utils.command_discovery_service import get_command_discovery_service
from utils.config_service import Config
from utils.service_discovery import get_command_center_url
from utils.tool_result_formatter import format_tool_result, format_tool_error


def _maybe_take_over_music(command, arguments: Dict[str, Any]) -> None:
    """Stop other music players if this command is a music-takeover "play".

    Called immediately before ``command.execute()`` on both routing paths
    (pre-route and LLM tool-call), so a successful play takes over the
    speaker before any audio actually starts. Only fires for ``action="play"``
    — non-takeover actions (skip, volume, now_playing, stop) leave sibling
    players alone.

    The binary the command itself controls is resolved via the SDK property
    ``takes_over_playback_binary`` (preferred) or a legacy hardcoded map
    in :mod:`core.music_control`. ``None`` means "not a music command" and
    the helper short-circuits — no-op for weather, calendar, etc.
    """
    if arguments.get("action") != "play":
        return
    binary = _resolve_takeover_binary(command)
    if binary is None:
        return
    try:
        stop_other_music_players(except_binary=binary)
    except Exception as e:
        # Never let takeover failures abort the user's actual command —
        # they'd hear "I couldn't stop Spotify" instead of their song.
        logger.warning(
            "music takeover failed (continuing with execute)",
            command=getattr(command, "command_name", "?"),
            error=str(e),
        )


def _build_secrets(command) -> Dict[str, str]:
    """Build the secrets dict a command needs from the node's encrypted store.

    Returns key → value for every required secret that's present. Absent
    required secrets surface via MissingSecretsError when execute() runs.

    User-scoped secrets are resolved via the per-request user_id ContextVar
    set by the caller before command.execute(); without it, user-scope rows
    would always resolve as missing.

    Secret service is imported lazily so test environments without the
    encrypted SQLite driver (sqlcipher3) can still import this module.
    """
    from services.secret_service import get_secret_value  # lazy — avoids sqlcipher at import time
    from jarvis_command_sdk.context import get_current_user_id

    user_id = get_current_user_id()
    secrets: Dict[str, str] = {}
    for s in command.required_secrets:
        scoped_user_id = user_id if s.scope == "user" else None
        value = get_secret_value(s.key, s.scope, user_id=scoped_user_id)
        if value is not None:
            secrets[s.key] = value
    return secrets

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
    # Set when any executed CommandResponse provided one. Voice pipeline
    # invokes it after TTS + duck-release so media commands can defer their
    # actual play() call until the sink-input is back on the real ALSA sink.
    on_response_complete: Optional[Callable[[], None]] = None


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


_MAX_CONVERSATION_USERS = 100
# How long the node remembers the most-recently-surfaced list for a voice
# follow-up ("mark those as read"). Memory is node-level (not conversation-
# keyed) so it resolves whether the list was surfaced via the LLM tool path, a
# node-side pre-route (which never touches CC), or a previous wake cycle. The
# TTL bounds staleness; it also gates whether the node ships the list to CC in
# node_context at the next /conversation/start.
_RECENT_ITEMS_TTL_SECONDS = 600
# Actions that change or send data — these require an explicit spoken
# confirmation turn before act_on_items will dispatch them (mark_read and other
# read-only/idempotent verbs run immediately).
_DESTRUCTIVE_ACTIONS = frozenset({"delete", "trash", "archive", "send", "send_reply"})


class CommandExecutionService:
    def __init__(self):
        self.command_center_url = get_command_center_url()
        self.node_id = Config.get_str("node_id")
        self.room = Config.get_str("room")
        self.command_discovery = get_command_discovery_service()
        self.client = JarvisCommandCenterClient(self.command_center_url)
        self._conversation_users: OrderedDict[str, int | None] = OrderedDict()
        # Most-recently surfaced referenceable items (node-level, latest wins):
        # {ref_id: {"owner", "label", "attrs", "actions"}} + a monotonic stamp.
        self._recent_items: Dict[str, Dict[str, Any]] = {}
        self._recent_items_ts: float = 0.0
        # Force initial discovery
        self.command_discovery.refresh_now()

    def _track_conversation_user(self, conversation_id: str, user_id: int | None) -> None:
        """Store speaker identity for a conversation, evicting oldest entries when full."""
        self._conversation_users[conversation_id] = user_id
        while len(self._conversation_users) > _MAX_CONVERSATION_USERS:
            self._conversation_users.popitem(last=False)

    def _record_referenceable_items(self, owner: str, items: Any) -> None:
        """Remember the items a command/agent just surfaced (node-level, latest wins).

        The most-recent surfacing replaces the buffer ("those" == the last thing
        shown). Stored node-level — not per conversation_id — so a follow-up
        resolves whether the surfacing came from the LLM tool path, a node-side
        pre-route, or a prior wake cycle; the node ships this list to CC in
        node_context at the next /conversation/start. Stores only handles +
        facets, never the command's payload, keeping the node a thin edge.
        """
        if not items:
            return
        bucket: Dict[str, Dict[str, Any]] = {}
        for item in items:
            d = item.to_dict() if hasattr(item, "to_dict") else dict(item)
            ref_id = d.get("ref_id")
            if not ref_id:
                continue
            bucket[ref_id] = {
                "owner": owner,
                "label": d.get("label"),
                "attrs": d.get("attrs") or {},
                "actions": d.get("actions") or [],
            }
        if bucket:
            self._recent_items = bucket
            self._recent_items_ts = time.monotonic()

    def _maybe_record_items(self, owner: str, command_response: "CommandResponse") -> None:
        """Record any referenceable items a successful command surfaced this turn."""
        if not getattr(command_response, "success", True):
            return
        items = getattr(command_response, "referenceable_items", None)
        if items:
            self._record_referenceable_items(owner, items)

    def _recent_items_fresh(self) -> bool:
        """True if the remembered list is non-empty and within the TTL."""
        return bool(self._recent_items) and (
            time.monotonic() - self._recent_items_ts
        ) <= _RECENT_ITEMS_TTL_SECONDS

    def recently_shown_items_wire(self) -> list[dict] | None:
        """Wire form of the remembered items for node_context (None if stale/empty).

        Shipped to CC at /conversation/start so the command-center can re-inject
        the RECENTLY SHOWN block on a FRESH conversation (a pre-route or re-wake
        follow-up), where there is no tool result for CC to stash from.
        """
        if not self._recent_items_fresh():
            return None
        return [
            {"ref_id": rid, "label": m["label"], "attrs": m["attrs"], "actions": m["actions"]}
            for rid, m in self._recent_items.items()
        ]

    def register_tools_for_conversation(
        self,
        conversation_id: str,
        speaker_user_id: Optional[int] = None,
        speaker_confidence: Optional[float] = None,
        agents: Optional[Dict[str, Any]] = None,
        adapter_settings: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Register available client-side tools with the Command Center for a conversation

        Args:
            conversation_id: The conversation identifier
            speaker_user_id: Optional speaker identity from voice recognition
            speaker_confidence: Optional confidence (0-1) for the identification
            agents: Optional agent context to inject (e.g., Home Assistant data)
            adapter_settings: Optional test-mode override for the server-side
                              adapter (hash/scale/enabled). Honored only when
                              the server was started with JARVIS_TEST_MODE=1.

        Returns:
            True if successful, False otherwise
        """
        self._track_conversation_user(conversation_id, speaker_user_id)

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
                speaker_user_id=speaker_user_id,
                speaker_confidence=speaker_confidence,
                agents=agents,
                adapter_settings=adapter_settings,
                # Carry the most-recently-surfaced list to CC so a follow-up on a
                # FRESH conversation (pre-route / re-wake) can still resolve
                # "mark those as read" — there's no tool result for CC to stash from.
                recently_shown_items=self.recently_shown_items_wire(),
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
        adapter_settings: dict | None = None,
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
        disabled_by_cmd = self._load_disabled_fast_paths()
        commands = self.command_discovery.get_all_commands()
        for command in commands.values():
            disabled_ids = disabled_by_cmd.get(command.command_name, set())
            pre = self._safe_pre_route(command, voice_command, disabled_ids)
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
            adapter_settings=adapter_settings,
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

        # PCM iterator for the player thread. We use a generator with a max-
        # idle timeout instead of ``iter(audio_queue.get, None)`` so the
        # player can never block forever waiting for a sentinel that fails
        # to arrive. The original pattern leaked aplay processes whenever
        # the cleanup's ``put_nowait(None)`` lost its race against a full
        # queue (aplay slow + iter_content fast = queue at maxsize when
        # iter_content ends, sentinel dropped, queue.get() blocks forever
        # once the real chunks are drained, finally block never runs,
        # proc.stdin stays open, aplay sits on pipe_r forever).
        #
        # idle_timeout=8s is comfortably above any realistic gap between
        # chunks on a healthy stream (typical: ~50ms/chunk) and well below
        # the user's "did it crash?" threshold. If iter_content stalls
        # mid-response the player will close cleanly after 8s.
        _IDLE_TIMEOUT_S: float = 8.0

        def _audio_chunks() -> Any:
            while True:
                try:
                    item = audio_queue.get(timeout=_IDLE_TIMEOUT_S)
                except queue.Empty:
                    logger.warning(
                        "Streaming audio queue idle past timeout; ending stream",
                        idle_timeout_s=_IDLE_TIMEOUT_S,
                    )
                    return
                if item is None:
                    return
                yield item

        def audio_player() -> None:
            platform_audio.play_pcm_stream(
                _audio_chunks(),
                sample_rate=sample_rate,
                channels=channels,
                sample_width=sample_width,
            )

        player_thread = threading.Thread(target=audio_player, daemon=True)
        player_thread.start()

        has_audio = False
        # Register the streaming response so cancel_playback() can close
        # the socket from the barge-in monitor thread. Without this, the
        # ``is_cancelled`` check below only fires when the next chunk
        # arrives from the TTS server — and the server may keep sending
        # for the full duration of the response (minutes for a news
        # briefing). The wake-step then hangs waiting for iter_content
        # to yield, the LED stays stuck, and the next wake can't fire.
        platform_audio.register_cancel_closeable(response)
        try:
            for chunk in response.iter_content(chunk_size=4096):
                if not chunk:
                    continue
                # Stop fetching if barge-in cancelled playback
                if platform_audio.is_cancelled:
                    logger.info("Streaming audio cancelled (barge-in)")
                    break
                has_audio = True
                # Bounded put with cancel re-check: when barge-in fires,
                # play_pcm_stream exits as soon as aplay is killed and
                # stops draining audio_queue. A plain audio_queue.put()
                # would then block forever once the queue fills to
                # ``maxsize`` — that was the second source of the
                # post-barge-in hang (the first being iter_content
                # waiting on the next chunk). 200ms poll lets cancel
                # interrupt us at worst one tick after it's set.
                while True:
                    if platform_audio.is_cancelled:
                        break
                    try:
                        audio_queue.put(chunk, timeout=0.2)
                        break
                    except queue.Full:
                        continue
                if platform_audio.is_cancelled:
                    logger.info("Streaming audio cancelled while queue full (barge-in)")
                    break
        except Exception as e:
            # cancel_playback closed the response while we were blocked
            # on iter_content — requests / urllib3 surface that as a
            # ChunkedEncodingError or ProtocolError. Treat any exception
            # paired with the cancel flag as a clean barge-in cancel;
            # anything else is a real error and we re-raise.
            if platform_audio.is_cancelled:
                logger.info(
                    "Streaming audio iter_content raised after cancel",
                    error=str(e),
                )
            else:
                raise
        finally:
            platform_audio.unregister_cancel_closeable(response)
            # Send the sentinel reliably, not via put_nowait. On the normal
            # completion path the queue may be at maxsize when iter_content
            # ends — put with a short timeout waits for the player to drain
            # enough to accept it. If we still can't enqueue, the generator's
            # idle timeout (see _audio_chunks above) will end the stream
            # within _IDLE_TIMEOUT_S so aplay still exits cleanly.
            try:
                audio_queue.put(None, timeout=3.0)
            except queue.Full:
                logger.debug(
                    "Streaming audio sentinel deferred; consumer idle "
                    "timeout will handle stream end",
                )
            # Generous join — a maxsize=50 queue of 4KB chunks at 24 kHz
            # mono 16-bit drains in ~4s. 20s lets aplay fully play out the
            # buffered audio (the user's TTS) before we return. If the
            # consumer is genuinely stuck, the idle timeout above plus
            # play_pcm_stream's own 30s aplay drain timeout serve as the
            # final escape hatches.
            player_thread.join(timeout=20)
            if player_thread.is_alive():
                logger.warning(
                    "Streaming audio player thread did not exit within 20s; "
                    "playback may be partially lost (aplay drain timeout will "
                    "still free the process)",
                )
            response.close()

        return has_audio

    def process_voice_command(
        self,
        voice_command: str,
        validation_handler: Optional[Callable[[ValidationRequest], str]] = None,
        register_tools: bool = True,
        speaker_user_id: Optional[int] = None,
        conversation_id: Optional[str] = None,
        warmup_thread: Optional[threading.Thread] = None,
        warmup_result: Optional[Dict[str, Any]] = None,
        skip_ack: bool = False,
        pre_wake_speech_seconds: Optional[float] = None,
        affect: Optional[Dict[str, Any]] = None,
        on_llm_fallback: Optional[Callable[[], None]] = None,
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
            conversation_id: Optional pre-generated conversation ID (from parallel warmup)
            warmup_thread: Optional background warmup thread to join instead of
                          calling register_tools_for_conversation inline
            warmup_result: Optional dict with warmup outcome ({"success": bool})
            skip_ack: If True, suppress the ack timer (processing ack already played)

        Returns:
            Execution result dictionary with success, message, conversation_id,
            wait_for_input, and clear_history
        """
        if conversation_id is None:
            conversation_id = self._generate_conversation_id()

        # Try node-side pre-routing (skip CC entirely)
        pre_result = self.try_pre_route(voice_command, conversation_id, speaker_user_id=speaker_user_id)
        if pre_result is not None:
            return pre_result

        # Pre-route didn't claim this — we're committed to the LLM path now.
        # Fire the LLM-fallback hook (used by the caller to play a deferred
        # wake-ack audio cue, so the user gets feedback while the network
        # round-trip happens). Fast-path queries skip this entirely.
        if on_llm_fallback is not None:
            try:
                on_llm_fallback()
            except Exception as e:
                logger.warning("on_llm_fallback hook raised", error=str(e))

        logger.info("Starting conversation", conversation_id=conversation_id, command=voice_command)

        # Gate the ack on a timer so fast conversational responses aren't
        # preceded by a pointless "Let me look into that." For slow tool
        # calls (deep research, external APIs, multi-iteration LLM loops)
        # the timer fires and the ack plays to cover the wait.
        # Skip if a processing ack was already played (avoids double ack).
        main_response_ready = threading.Event()
        if skip_ack:
            main_response_ready.set()  # pre-signal so ack thread exits immediately

        try:
            ack_thread = threading.Thread(
                target=self._speak_acknowledgment,
                args=(voice_command, main_response_ready),
                daemon=True,
            )
            ack_thread.start()

            # Register available tools if requested
            if warmup_thread is not None and register_tools:
                # Parallel warmup was started during recording — wait for it
                warmup_thread.join(timeout=10)
                if warmup_result and not warmup_result.get("success"):
                    logger.warning("Parallel warmup failed, falling back to inline warmup")
                    self.register_tools_for_conversation(conversation_id, speaker_user_id=speaker_user_id)
                else:
                    # Warmup succeeded but used last_speaker_user_id — store
                    # the actual speaker for tool execution later
                    self._track_conversation_user(conversation_id, speaker_user_id)
            elif register_tools:
                self.register_tools_for_conversation(conversation_id, speaker_user_id=speaker_user_id)

            # Single unified request — handles audio, tool calls, and validation
            tag, payload = self.client.send_command_unified(
                voice_command, conversation_id, speaker_user_id=speaker_user_id,
                pre_wake_speech_seconds=pre_wake_speech_seconds,
            )

            # Signal the ack thread: the main response is here. If the ack
            # hasn't started speaking yet (still in its pre-speak wait), it
            # will exit quietly. If it has, join() waits for it to finish
            # so the main answer doesn't talk over it.
            main_response_ready.set()
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
        # Server detected the LLM's <not_for_me/> sentinel — the wake word
        # fired on ambient speech (TV, separate conversation, etc.). Skip
        # TTS, briefly flash the orange not_for_me LED so the user knows
        # the wake fired and was dismissed, and signal the follow-up loop
        # to exit immediately via the dedicated ``not_for_me`` flag.
        # Note: we deliberately do NOT set clear_history here, because
        # many normal one-shot commands (timers, lamp toggles) set it and
        # the follow-up loop must still run after those.
        if response.is_not_for_me():
            logger.info("Server signaled not-for-me — silent abort",
                        conversation_id=conversation_id)
            try:
                from services.led_service import get_led_service
                get_led_service().preview_pattern("not_for_me", duration_seconds=1.2)
            except Exception:
                pass
            return {
                "success": True,
                "message": "",
                "conversation_id": conversation_id,
                "wait_for_input": False,
                "clear_history": False,
                "audio_played": True,
                "not_for_me": True,
            }

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

                # Try the streaming continue endpoint first when we don't
                # need a text follow-up — server streams iter-2 PCM directly
                # to the speaker, saving 2-3s perceived latency on chatty
                # answers (news, weather, etc.). If the server signals
                # fallback (202 JSON) we fall through to the blocking path.
                if not last_tool_result.wait_for_input:
                    audio_resp, audio_meta = self.client.send_tool_results_stream(
                        conversation_id, last_tool_result.api_results,
                    )
                    if audio_resp is not None:
                        # Streaming TTS bypasses speak_result() (which is where
                        # the LED normally flips). Set it inline so the
                        # pinwheel doesn't persist through the response. Use
                        # red ("error") if every tool call failed — otherwise
                        # cyan ("speaking") for a normal response.
                        led_pattern = "error" if last_tool_result.all_failed else "speaking"
                        try:
                            from services.led_service import get_led_service
                            get_led_service().set_transient_pattern(led_pattern)
                        except Exception:
                            pass
                        try:
                            played = self._play_streaming_audio(audio_resp, audio_meta)
                        finally:
                            try:
                                from services.led_service import get_led_service
                                get_led_service().set_transient_pattern(None)
                            except Exception:
                                pass
                        if played:
                            logger.info("Streaming continue played successfully")
                            return {
                                "success": not last_tool_result.all_failed,
                                "message": "(streamed audio)",
                                "conversation_id": conversation_id,
                                "wait_for_input": False,
                                "clear_history": last_tool_result.clear_history,
                                "audio_played": True,
                                # Forward the deferred-play callback from the
                                # executed tool — without this, spotify/MA/
                                # pandora "play X" through the LLM+tool path
                                # would silently drop on_response_complete and
                                # no music would ever start.
                                "on_response_complete": (
                                    last_tool_result.on_response_complete
                                ),
                            }
                        logger.warning("Streaming continue playback failed, falling back")

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
            "on_response_complete": (
                last_tool_result.on_response_complete if last_tool_result else None
            ),
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
                if tool_name == "act_on_items":
                    # Generic "act on what I was just shown" tool. Dispatch is
                    # handled here (not in a command's run()) because only this
                    # service holds the per-conversation ref_id->owner map.
                    command_response = self._dispatch_act_on_items(
                        tool_call, conversation_id, voice_command
                    )
                else:
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
                    # Music-takeover hook: if this is a music command's "play",
                    # stop sibling players before execute() so the new playback
                    # claims the speaker cleanly. No-op for non-music commands.
                    _maybe_take_over_music(command, arguments)
                    try:
                        command_response: CommandResponse = command.execute(
                            request_info, secrets=_build_secrets(command), **arguments,
                        )
                    finally:
                        set_current_user_id(None)

                    # Remember any items this command surfaced so a follow-up
                    # act_on_items can resolve "those"/"#3" to a ref_id + owner.
                    self._maybe_record_items(tool_name, command_response)

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

                # Capture the deferred-play callback from the LAST tool that
                # set one — rare for multiple media tools to fire in a single
                # turn; "last wins" matches the user's perceived "thing that
                # was actually triggered" in the spoken response. getattr keeps
                # us compatible with older jarvis-command-sdk (<0.2.1) that
                # doesn't have the field.
                _on_complete = getattr(command_response, "on_response_complete", None)
                if _on_complete is not None:
                    result.on_response_complete = _on_complete

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

    def _dispatch_act_on_items(
        self, tool_call: ToolCall, conversation_id: str, voice_command: str
    ) -> CommandResponse:
        """Dispatch ``act_on_items(action, ref_ids)`` to the owning commands' @callbacks.

        Resolves each ref_id against this conversation's surfaced-items map,
        groups by owning command, and calls ``command.get_callbacks()[action]``
        with the same ``{action, selected, context}`` payload a mobile tap sends —
        so one @callback handler serves both surfaces. Fully fail-soft: an empty
        stash, unknown ids, or an unsupported action yields a spoken explanation,
        never a raise.
        """
        args = tool_call.function.get_arguments_dict()
        action = str(args.get("action") or "").strip()
        ref_ids = args.get("ref_ids") or []
        if isinstance(ref_ids, str):
            ref_ids = [ref_ids]

        bucket = self._recent_items if self._recent_items_fresh() else {}
        if not action or not ref_ids or not bucket:
            logger.info(
                "act_on_items: nothing to act on",
                action=action, ref_count=len(ref_ids), have_items=bool(bucket),
            )
            return CommandResponse.final_response(
                context_data={"message": "I don't have anything shown to act on right now."}
            )

        known = [(rid, bucket[rid]) for rid in ref_ids if rid in bucket]
        unknown = [rid for rid in ref_ids if rid not in bucket]
        if unknown:
            logger.warning("act_on_items: unknown ref_ids", unknown=unknown, action=action)
        if not known:
            return CommandResponse.error_response(
                error_details="None of those items are in the list I just showed you.",
                context_data={"message": "I couldn't find those items in what I just showed you."},
            )

        # Destructive verbs (delete/archive/send/...) require an explicit
        # confirmation round-trip, which isn't wired yet — v1 surfaces only
        # non-destructive actions (mark_read). Refuse rather than act without
        # confirmation. (See follow-on: spoken confirm turn for destructive verbs.)
        if action in _DESTRUCTIVE_ACTIONS:
            logger.info("act_on_items: refusing destructive action without confirmation", action=action)
            return CommandResponse.error_response(
                error_details=f"'{action}' needs confirmation and isn't available by voice yet.",
                context_data={
                    "message": (
                        f"I can't {action.replace('_', ' ')} those by voice yet — "
                        "you can do that from the app."
                    )
                },
            )

        # Group by owning command and dispatch each group to its @callback.
        by_owner: Dict[str, List[tuple]] = {}
        for rid, meta in known:
            by_owner.setdefault(meta["owner"], []).append((rid, meta))

        user_id = self._conversation_users.get(conversation_id)
        request_info = RequestInformation(
            voice_command=voice_command or f"act_on_items: {action}",
            conversation_id=conversation_id,
            is_validation_response=False,
            user_id=user_id,
        )

        from jarvis_command_sdk.context import set_current_user_id

        messages: List[str] = []
        all_ok = True
        set_current_user_id(user_id)
        try:
            for owner, entries in by_owner.items():
                command = self.command_discovery.get_command(owner)
                if command is None:
                    all_ok = False
                    logger.warning("act_on_items: owner command not found", owner=owner)
                    messages.append("I couldn't reach the right app for those.")
                    continue
                get_callbacks = getattr(command, "get_callbacks", None)
                callbacks = get_callbacks() if callable(get_callbacks) else {}
                handler = callbacks.get(action)
                if handler is None:
                    all_ok = False
                    logger.warning(
                        "act_on_items: action not supported",
                        owner=owner, action=action, available=list(callbacks.keys()),
                    )
                    messages.append(f"I can't {action.replace('_', ' ')} those.")
                    continue
                data = {
                    "action": action,
                    # attrs first, then key/action LAST so a stray attrs key can't
                    # clobber the message handle the callback reads.
                    "selected": [
                        {**(meta.get("attrs") or {}), "key": rid} for rid, meta in entries
                    ],
                    "context": {"source_tool": owner},
                }
                try:
                    resp = handler(data, request_info)
                except Exception as e:
                    all_ok = False
                    logger.error(
                        "act_on_items: callback raised", owner=owner, action=action, error=str(e)
                    )
                    messages.append("Sorry, that didn't work.")
                    continue
                if not resp.success:
                    all_ok = False
                msg = (resp.context_data or {}).get("message") or resp.error_details
                if msg:
                    messages.append(msg)
                # A follow-up callback may itself surface new items to chain on.
                self._maybe_record_items(owner, resp)
        finally:
            set_current_user_id(None)

        combined = " ".join(m for m in messages if m).strip()
        if all_ok:
            return CommandResponse.final_response(context_data={"message": combined or "Done."})
        return CommandResponse.error_response(
            error_details=combined or "Sorry, that didn't work.",
            context_data={"message": combined or "Sorry, that didn't work."},
        )

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

    def _load_disabled_fast_paths(self) -> dict[str, set[str]]:
        """Load the user's disabled fast-path pattern set in one DB pass.

        Returns command_name -> set of disabled pattern_ids. Empty dict on
        any DB error -- fail-open so a transient hiccup never blocks the
        fast path (the latency win is the whole point of this code path).
        """
        try:
            from db import SessionLocal
            from repositories.disabled_fast_path_repository import (
                DisabledFastPathRepository,
            )
            db = SessionLocal()
            try:
                return DisabledFastPathRepository(db).get_all_disabled()
            finally:
                db.close()
        except Exception as e:
            logger.warning("Failed to load disabled fast paths", error=str(e))
            return {}

    @staticmethod
    def _safe_pre_route(command, voice_command: str, disabled_ids: set[str]):
        """Call command.pre_route() honoring the disabled set, with a
        graceful fallback for any override that pre-dates the kwarg."""
        try:
            return command.pre_route(
                voice_command, disabled_pattern_ids=disabled_ids
            )
        except TypeError as e:
            if "disabled_pattern_ids" not in str(e):
                raise
            return command.pre_route(voice_command)

    def try_pre_route(self, voice_command: str, conversation_id: str, speaker_user_id: int | None = None) -> Dict[str, Any] | None:
        """Try node-side pre-routing across all discovered commands.

        Iterates commands, calls pre_route() on each.  First match wins.
        If matched, executes the command directly and returns the result
        dict — no CC contact at all.

        Per-pattern disable: loads the user's `disabled_fast_paths` set in
        one query, then passes the per-command subset into `pre_route()` so
        each command can skip patterns the user opted out of from the mobile
        inspect UI.

        Returns:
            Result dict (same shape as process_voice_command), or None to
            fall through to the normal LLM path.
        """
        disabled_by_cmd = self._load_disabled_fast_paths()

        commands = self.command_discovery.get_all_commands()
        for command in commands.values():
            disabled_ids = disabled_by_cmd.get(command.command_name, set())
            pre = self._safe_pre_route(command, voice_command, disabled_ids)
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
                    is_pre_routed=True,
                )

                from jarvis_command_sdk.context import set_current_user_id
                set_current_user_id(speaker_user_id)
                # Music-takeover hook (pre_route path) — see _maybe_take_over_music.
                _maybe_take_over_music(command, pre.arguments)
                try:
                    command_response: CommandResponse = command.execute(
                        request_info, secrets=_build_secrets(command), **pre.arguments,
                    )
                finally:
                    set_current_user_id(None)

                if not command_response.success:
                    # Silent fall-through: when the pre-routed command
                    # returns an error, don't speak that error. Return None
                    # so the caller hits the LLM path, which can either
                    # retry the command or compose a more natural reply.
                    # Otherwise the user would hear the error AND then the
                    # LLM's response — two failures stacked.
                    logger.info(
                        "Pre-routed command failed, falling through to LLM path",
                        command=command.command_name,
                        error_details=command_response.error_details,
                    )
                    return None

                message = pre.spoken_response
                if not message:
                    ctx = command_response.context_data or {}
                    message = ctx.get("message")
                if not message:
                    # The command didn't pre-compose a spoken response.
                    # Rather than emit a misleading "Done.", fall through
                    # to the LLM path so it can compose from context_data.
                    logger.info(
                        "Pre-routed command succeeded but produced no spoken message; falling through to LLM",
                        command=command.command_name,
                    )
                    return None

                # Remember any items the pre-routed command surfaced (e.g. an
                # email list) so a follow-up "mark those as read" resolves even
                # though this path never contacts CC.
                self._maybe_record_items(command.command_name, command_response)

                return {
                    "success": True,
                    "message": message,
                    "conversation_id": conversation_id,
                    # Pre-routed turns never registered this conversation with CC,
                    # so the follow-up loop must NOT continue it (that 400s).
                    # The flag makes the loop start a fresh CC conversation, which
                    # also carries recently_shown_items across in node_context.
                    "pre_routed": True,
                    "wait_for_input": False,
                    "clear_history": False,
                    "on_response_complete": getattr(
                        command_response, "on_response_complete", None,
                    ),
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

    ACK_TIMER_SECONDS = 3.0

    def _speak_acknowledgment(
        self,
        voice_command: str,
        main_response_ready: threading.Event,
    ) -> None:
        """Fetch and speak a fast LLM-generated acknowledgment (background thread).

        Waits ``ACK_TIMER_SECONDS`` on ``main_response_ready`` before
        speaking. If the main pipeline finishes within that window (fast
        conversational replies), the event is set and we skip the ack
        entirely — no "Let me look into that." in front of a 1-second
        answer. If the window expires, we proceed with the ack so the
        user hears something while a slow tool loop grinds.

        Honors ``wake_ack_audio_enabled``: when false, the LED already
        signals "I heard you" + "thinking" without spoken filler. Stays
        silent regardless of how slow the LLM is.
        """
        if not Config.get_bool("wake_ack_audio_enabled", True):
            return
        if main_response_ready.wait(timeout=self.ACK_TIMER_SECONDS):
            logger.debug("Main response arrived before ack timer, skipping ack")
            return

        try:
            text = self.client.get_acknowledgment(voice_command)
            if not text:
                return
            # One more check: the ack-text fetch itself took some time. If
            # main landed while we were fetching, still skip to avoid
            # talking over the answer.
            if main_response_ready.is_set():
                logger.debug("Main response arrived during ack fetch, skipping ack")
                return
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
        # Bail immediately if barge-in cancelled playback — avoids a
        # wasted TTS HTTP roundtrip that blocks the return to wake mode.
        if platform_audio.is_cancelled:
            return
        tts_provider = get_tts_provider()
        message = result.get("message", "An error occurred")

        # Show red (error) when the result is a failure; cyan (speaking) otherwise.
        # Both patterns are transient overlays and get cleared in the finally.
        led_pattern = "error" if result.get("success") is False else "speaking"
        try:
            from services.led_service import get_led_service
            get_led_service().set_transient_pattern(led_pattern)
        except Exception:
            pass
        try:
            # Use streaming for long responses (briefings, stories, etc.)
            if len(message) > 200 and hasattr(tts_provider, "speak_stream"):
                if tts_provider.speak_stream(message):
                    return
                # Fall through to blocking speak if streaming fails

            tts_provider.speak(False, message)
        finally:
            try:
                from services.led_service import get_led_service
                get_led_service().set_transient_pattern(None)
            except Exception:
                pass
