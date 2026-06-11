"""High-priority alert announcer.

The main wake loop calls :func:`drain_alert_announcements` during quiet
moments (no wake fired, ring buffer otherwise idle) to speak queued
alerts via TTS, listen briefly for a snooze/dismiss response, route
that response through CC, and flush the queue.

Three thresholds keep this from being annoying:

  * **Priority filter** — only ``priority >= ALERT_ANNOUNCE_PRIORITY``
    (3 — reminders, urgent emails) get spoken. News at priority 1 lives
    in the queue silently for the "what's up" command (and does not
    light the LED — the queue's on_change only counts announceable
    alerts).
  * **Inline listen timeout** — after speaking, we wait
    ``INLINE_LISTEN_TIMEOUT`` seconds (default 8) for a response. If
    nothing, we move on without crashing the wake cycle.
  * **Per-alert isolation** — TTS failure on one alert continues with
    the next; inline-listen failure is swallowed; queue-removal failure
    is swallowed. The wake loop must not crash because the queue had a
    bad entry.

Only the alerts actually announced are removed from the queue —
unspoken low-priority alerts stay retrievable via "what's up" / the
button, per the contract above.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from jarvis_log_client import JarvisLogger

from clients.responses.jarvis_command_center import ValidationRequest
from core.audio_bus import AudioBus
from core.helpers import get_tts_provider
from scripts.speech_to_text import listen_for_follow_up
from services.alert_queue_service import (
    ALERT_ANNOUNCE_PRIORITY,  # single source of truth, shared with the LED gating
    get_alert_queue_service,
)

if TYPE_CHECKING:
    from utils.command_execution_service import CommandExecutionService


logger = JarvisLogger(service="jarvis-node")


INLINE_LISTEN_TIMEOUT = 8.0  # Seconds to wait for snooze/dismiss after announcement


def has_pending_high_priority_alerts() -> bool:
    """True if the alert queue has at least one pending alert at
    ``priority >= ALERT_ANNOUNCE_PRIORITY``. Safe against queue
    failures — returns False if the queue can't be reached.

    Called from the wake loop's inner-iteration alert check; keeping
    the queue lookup behind one helper keeps the queue concern in one
    module (rather than the wake loop reaching directly into
    ``services.alert_queue_service``).
    """
    try:
        aq = get_alert_queue_service()
        return any(
            a.priority >= ALERT_ANNOUNCE_PRIORITY for a in aq.get_pending()
        )
    except Exception:
        return False


def drain_alert_announcements(
    bus: AudioBus,
    command_service: "CommandExecutionService",
    stt_provider,
    validation_handler: Callable[[ValidationRequest], str],
) -> bool:
    """Check for high-priority alerts and announce them via TTS.

    Returns True if any announcements were made (caller should reopen stream).
    """
    try:
        alert_queue = get_alert_queue_service()
        pending = alert_queue.get_pending()
    except Exception:
        return False

    # Filter to only high-priority alerts (reminders, urgent emails)
    announcements = [a for a in pending if a.priority >= ALERT_ANNOUNCE_PRIORITY]
    if not announcements:
        return False

    tts_provider = get_tts_provider()

    for alert in announcements:
        logger.info("Announcing alert", title=alert.title, priority=alert.priority)

        # Speak the alert
        try:
            tts_provider.speak(True, alert.summary)
        except Exception as e:
            logger.warning("Alert TTS failed", error=str(e))
            continue

        # Inline listen for response (snooze/dismiss/silence)
        try:
            audio_file = listen_for_follow_up(bus, timeout_seconds=INLINE_LISTEN_TIMEOUT)
            if audio_file is None:
                logger.debug("No response to alert announcement (silence)")
                continue

            transcription_result = stt_provider.transcribe_with_speaker(audio_file)
            if not transcription_result.text:
                continue

            text = transcription_result.text
            speaker_user_id = transcription_result.speaker_user_id
            logger.info("Alert response received", text=text)

            # Process the response (e.g., "snooze", "snooze for 20 minutes", "got it")
            result = command_service.process_voice_command(
                text, validation_handler, speaker_user_id=speaker_user_id,
            )
            command_service.speak_result(result)

        except Exception as e:
            logger.warning("Inline listen after alert failed", error=str(e))

    # Remove exactly what was announced. flush() would also destroy the
    # unspoken low-priority alerts, breaking the "news lives in the queue
    # for what's-up" contract.
    try:
        alert_queue.remove_ids([a.id for a in announcements])
    except Exception:
        pass

    return True
