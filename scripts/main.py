import os
import sys
import threading

# Set config service URL from config.json before any library imports,
# so jarvis-config-client uses the right URL instead of localhost
if not os.environ.get("JARVIS_CONFIG_URL"):
    try:
        import json
        _config_path = os.environ.get("CONFIG_PATH", "config.json")
        with open(_config_path) as _f:
            _url = json.load(_f).get("jarvis_config_service_url")
        if _url:
            os.environ["JARVIS_CONFIG_URL"] = _url
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        pass

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


def _run_db_migrations() -> None:
    """Run Alembic migrations to ensure DB schema is up to date."""
    try:
        from alembic.config import Config as AlembicConfig
        from alembic import command as alembic_command

        alembic_cfg = AlembicConfig(
            os.path.join(os.path.dirname(os.path.dirname(__file__)), "alembic.ini")
        )
        alembic_command.upgrade(alembic_cfg, "head")
        logger.info("Database migrations complete")
    except Exception as e:
        logger.warning("Database migration failed (non-fatal)", error=str(e))


def main():
    # Run DB migrations before anything that needs the database
    _run_db_migrations()

    # Register SDK storage backend (must be after DB migrations)
    try:
        from services.storage_backend import init_storage_backend
        init_storage_backend()
    except Exception as e:
        logger.warning("Storage backend init failed, commands may lack persistence", error=str(e))

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

    # Initialize reminder service
    try:
        from services.reminder_service import initialize_reminder_service
        reminder_service = initialize_reminder_service()
        restored_reminders = reminder_service.restore_reminders()
        if restored_reminders > 0:
            logger.info("Restored reminders from previous session", count=restored_reminders)
    except Exception as e:
        logger.warning("Reminder service init failed, continuing without reminders", error=str(e))

    # Initialize alert queue + LED service for proactive notifications
    from services.alert_queue_service import get_alert_queue_service
    from services.led_service import get_led_service

    led_service = get_led_service()
    alert_queue = get_alert_queue_service()
    alert_queue.on_change = lambda count: led_service.set_pattern("alert" if count > 0 else "normal")

    # Initialize agent scheduler (Home Assistant, etc.)
    agent_scheduler = initialize_agent_scheduler()
    agent_scheduler.set_alert_queue(alert_queue)
    logger.info("Agent scheduler initialized")

    # Music Assistant: enabled when URL secret is configured
    from services.secret_service import get_secret_value
    if get_secret_value("MUSIC_ASSISTANT_URL", "integration"):
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

    # Device scanning is now user-driven via MQTT (mobile → CC → node).
    # See services/device_scan_handler.py and mqtt_tts_listener.py.

    # Auto-reconnect known Bluetooth devices in background
    def _bt_reconnect():
        import time
        time.sleep(30)  # Let BlueZ initialize
        try:
            from jarvis_command_sdk import JarvisStorage
            from core.platform_abstraction import get_bluetooth_provider

            storage = JarvisStorage("bluetooth")
            records = storage.get_all()
            if not records:
                return

            provider = get_bluetooth_provider()
            if not provider.is_available():
                return

            count = 0
            for record in records:
                if not record.get("auto_connect", True):
                    continue
                mac = record.get("mac_address")
                if mac and provider.connect(mac):
                    count += 1
                    logger.info("Auto-reconnected BT device", name=record.get("name", mac), mac=mac)
            if count > 0:
                logger.info("Bluetooth auto-reconnect complete", count=count)
        except Exception as e:
            logger.warning("Bluetooth auto-reconnect failed (non-fatal)", error=str(e))

    bt_thread = threading.Thread(target=_bt_reconnect, daemon=True)
    bt_thread.start()

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

    # Start voice listener (blocks until KeyboardInterrupt or audio failure)
    try:
        start_voice_listener(ma_service)
    except Exception as e:
        logger.error("Voice listener failed", error=str(e))

    # If voice listener exits (no mic, audio failure, etc.), keep the process
    # alive so MQTT, agents, and reminders continue to work. The node won't
    # respond to voice but can still receive commands from the mobile app.
    logger.warning("Voice listener exited — node running in headless mode (MQTT + agents only)")
    try:
        import signal
        signal.pause()  # Block forever until SIGTERM/SIGINT
    except (KeyboardInterrupt, SystemExit):
        logger.info("Node shutting down")


if __name__ == "__main__":
    main()

