import os
import sys
import threading

from jarvis_log_client import init as init_logging, JarvisLogger

from scripts.mqtt_tts_listener import start_mqtt_listener
from scripts.voice_listener import start_voice_listener
from services.agent_scheduler_service import initialize_agent_scheduler
from services.timer_service import initialize_timer_service
from utils.config_service import Config
from utils.music_assistant_service import DummyMusicAssistantService, MusicAssistantService
from utils.service_discovery import init as init_service_discovery

# Initialize logging
init_logging(
    app_id=os.getenv("JARVIS_APP_ID", "jarvis-node"),
    app_key=os.getenv("JARVIS_APP_KEY", ""),
)
logger = JarvisLogger(service="jarvis-node")


def _run_provisioning_and_restart() -> None:
    """Run provisioning server and restart main.py after completion."""
    logger.info("Not provisioned - entering provisioning mode")
    logger.info("Connect to the node's WiFi AP and use the mobile app to provision")

    from scripts.run_provisioning import run_provisioning_server

    # Run provisioning with auto-shutdown enabled
    success = run_provisioning_server(auto_shutdown=True)

    if success:
        logger.info("Provisioning complete, restarting main service...")
        # Re-exec ourselves to start the main service
        os.execv(sys.executable, [sys.executable] + sys.argv)
    else:
        logger.error("Provisioning server stopped without completing")
        sys.exit(1)


def main():
    # Check if node is provisioned (skip in development mode)
    if not os.environ.get("JARVIS_SKIP_PROVISIONING_CHECK", "").lower() in ("true", "1", "yes"):
        from provisioning.startup import is_provisioned
        if not is_provisioned():
            logger.warning("Node not provisioned or cannot reach command center")
            _run_provisioning_and_restart()
            return  # Should not reach here due to os.execv
    # Initialize service discovery (config service → JSON config fallback)
    if init_service_discovery():
        logger.info("Service discovery initialized")
    else:
        logger.info("Using JSON config for service URLs")

    # Initialize timer service with TTS callback
    try:
        timer_service = initialize_timer_service()

        # Restore any persisted timers from previous session
        restored_count = timer_service.restore_timers()
        if restored_count > 0:
            logger.info("Restored timers from previous session", count=restored_count)
    except Exception as e:
        logger.warning("Timer service unavailable (pysqlcipher3 not installed?), continuing without timers", error=str(e))

    # Initialize agent scheduler (Home Assistant, etc.)
    agent_scheduler = initialize_agent_scheduler()
    logger.info("Agent scheduler initialized")

    if Config.get_bool("music_assistant_enabled", False):
        ma_service = MusicAssistantService()
    else:
        ma_service = DummyMusicAssistantService()

    # Start MQTT listener in thread (skip if disabled in config)
    mqtt_enabled: bool = Config.get_bool("mqtt_enabled", True) is not False
    if mqtt_enabled:
        mqtt_thread = threading.Thread(target=start_mqtt_listener, args=(ma_service,), daemon=True)
        mqtt_thread.start()
    else:
        logger.info("MQTT disabled in config, skipping MQTT listener")

    # Warm up the LLM by sending a throwaway request through the full
    # pipeline (tool registration → system prompt → KV cache).  This
    # primes llama.cpp's prefix cache so the first real voice command is fast.
    try:
        from utils.command_execution_service import CommandExecutionService
        warmup_service = CommandExecutionService()
        logger.info("Warming up LLM pipeline")
        warmup_service.process_voice_command("hello")
        logger.info("LLM warmup complete")
    except Exception as e:
        logger.warning("LLM warmup failed (non-fatal)", error=str(e))

    # Start voice listener in main thread
    start_voice_listener(ma_service)


if __name__ == "__main__":
    main()

