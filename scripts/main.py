import os
import signal
import sys
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

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
    except FileNotFoundError:
        print(f"WARNING: Config file not found: {_config_path}", file=sys.stderr)
    except (json.JSONDecodeError, KeyError) as _e:
        print(f"WARNING: Config parse error: {_e}", file=sys.stderr)

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

# Module-level shutdown event for graceful shutdown
_shutdown_event = threading.Event()


def _handle_shutdown(signum: int, frame: Any) -> None:
    """Handle SIGTERM/SIGINT for graceful shutdown."""
    sig_name: str = signal.Signals(signum).name
    logger.info("Received shutdown signal", signal=sig_name)
    _shutdown_event.set()
    # Stop agent scheduler if running
    try:
        from services.agent_scheduler_service import get_agent_scheduler_service
        get_agent_scheduler_service().stop()
    except Exception:
        pass


def _supervisor_loop(
    threads: Dict[str, Tuple[threading.Thread, Callable[[], threading.Thread]]],
    shutdown_event: threading.Event,
    heartbeat_threads: Optional[Dict[str, Any]] = None,
) -> None:
    """Monitor tracked threads and restart them if they die.

    Args:
        threads: Dict mapping thread name to (thread, factory_fn) tuples.
                 factory_fn returns a new started Thread when called.
        shutdown_event: Event to signal clean exit.
        heartbeat_threads: Optional dict to update when threads are restarted,
                          so heartbeat reports reflect the new thread.
    """
    while not shutdown_event.is_set():
        shutdown_event.wait(timeout=30)
        if shutdown_event.is_set():
            break
        for name, (thread, factory_fn) in list(threads.items()):
            if not thread.is_alive():
                logger.warning("Supervised thread died, restarting", thread_name=name)
                try:
                    new_thread = factory_fn()
                    threads[name] = (new_thread, factory_fn)
                    # Update heartbeat reference so CC sees the new thread status
                    if heartbeat_threads is not None and name in heartbeat_threads:
                        heartbeat_threads[name] = (new_thread, None)
                    logger.info("Supervised thread restarted", thread_name=name)
                except Exception as e:
                    logger.error("Failed to restart supervised thread", thread_name=name, error=str(e))


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


def _validate_config() -> None:
    """Log warnings for missing required config keys."""
    required: list[str] = ["node_id", "api_key", "jarvis_command_center_api_url"]
    missing: list[str] = [k for k in required if not Config.get_str(k)]
    if missing:
        logger.warning("Missing required config keys (provisioning may be needed)",
                       keys=missing,
                       config_path=os.environ.get("CONFIG_PATH", "config.json"))


def main():
    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    # Startup banner — visible in journalctl for debugging
    logger.info("Jarvis node starting",
                config_path=os.environ.get("CONFIG_PATH", "config.json"),
                node_id=Config.get_str("node_id", "unknown"),
                room=Config.get_str("room", "unknown"))

    # Validate config keys (warnings only — provisioning may resolve them)
    _validate_config()

    # Auto-initialize encryption key (K1) if it doesn't exist yet
    try:
        from utils.encryption_utils import initialize_encryption_key
        initialize_encryption_key()
    except Exception as e:
        logger.warning("Encryption key init failed", error=str(e))

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
    alert_queue = None
    try:
        from services.alert_queue_service import get_alert_queue_service
        from services.led_service import get_led_service

        led_service = get_led_service()
        alert_queue = get_alert_queue_service()
        alert_queue.on_change = lambda count: led_service.set_pattern("alert" if count > 0 else "normal")
    except Exception as e:
        logger.warning("Alert/LED service init failed (non-fatal)", error=str(e))

    # Initialize agent scheduler (Home Assistant, etc.)
    agent_scheduler = initialize_agent_scheduler()
    if alert_queue is not None:
        agent_scheduler.set_alert_queue(alert_queue)
    logger.info("Agent scheduler initialized")

    # Music Assistant: enabled when URL secret is configured
    from services.secret_service import get_secret_value
    if get_secret_value("MUSIC_ASSISTANT_URL", "integration"):
        ma_service = MusicAssistantService()
    else:
        ma_service = DummyMusicAssistantService()

    # Pass shutdown event to MQTT module for graceful shutdown of loops
    from scripts.mqtt_tts_listener import set_shutdown_event as mqtt_set_shutdown
    mqtt_set_shutdown(_shutdown_event)

    # Pass shutdown event to command discovery for graceful background refresh
    from utils.command_discovery_service import set_shutdown_event as cmd_set_shutdown
    cmd_set_shutdown(_shutdown_event)

    # Supervised threads: only mqtt and bluetooth are restarted if they die
    supervised_threads: Dict[str, Tuple[threading.Thread, Callable[[], threading.Thread]]] = {}

    # Heartbeat thread status: includes all key subsystems for CC reporting
    heartbeat_threads: Dict[str, Tuple[threading.Thread, None]] = {}

    # Start MQTT listener in thread (skip if disabled in config)
    mqtt_enabled: bool = Config.get_bool("mqtt_enabled", True) is not False
    if mqtt_enabled:
        def _make_mqtt_thread() -> threading.Thread:
            t = threading.Thread(target=start_mqtt_listener, args=(ma_service,), daemon=True)
            t.start()
            return t

        mqtt_thread = _make_mqtt_thread()
        supervised_threads["mqtt"] = (mqtt_thread, _make_mqtt_thread)
        heartbeat_threads["mqtt"] = (mqtt_thread, None)
    else:
        logger.info("MQTT disabled in config, skipping MQTT listener")

    # Device scanning is now user-driven via MQTT (mobile → CC → node).
    # See services/device_scan_handler.py and mqtt_tts_listener.py.

    # Auto-reconnect known Bluetooth devices in background
    # Re-try reconnect every 10 min so a device that appears after boot
    # (e.g. speaker powered on late) gets picked up. Long enough to avoid
    # log spam + BT radio churn on Pi Zero, short enough that the user
    # doesn't wait an hour for auto-connect to retry.
    BT_RECONNECT_INTERVAL_SECONDS = 600

    def _bt_reconnect() -> None:
        """Long-running reconnect loop for saved BT devices.

        Runs forever (until shutdown) so the thread-supervisor doesn't
        treat one-shot completion as a crash — previously this returned
        after the first pass and got re-launched every 30s by the
        supervisor, producing "Supervised thread died, restarting" log
        spam + constant CPU churn.
        """
        # Give BlueZ time to finish initializing at boot.
        if _shutdown_event.wait(timeout=30):
            return

        while not _shutdown_event.is_set():
            try:
                from jarvis_command_sdk import JarvisStorage
                from core.platform_abstraction import get_bluetooth_provider

                storage = JarvisStorage("bluetooth")
                records = storage.get_all()
                if records:
                    provider = get_bluetooth_provider()
                    if provider.is_available():
                        count = 0
                        for record in records:
                            if not record.get("auto_connect", True):
                                continue
                            mac = record.get("mac_address")
                            if mac and provider.connect(mac):
                                count += 1
                                logger.info(
                                    "Auto-reconnected BT device",
                                    name=record.get("name", mac),
                                    mac=mac,
                                )
                        if count > 0:
                            logger.info("Bluetooth auto-reconnect complete", count=count)
            except Exception as e:
                logger.warning("Bluetooth auto-reconnect failed (non-fatal)", error=str(e))

            # Sleep until next cycle (waking early on shutdown)
            if _shutdown_event.wait(timeout=BT_RECONNECT_INTERVAL_SECONDS):
                break

    def _make_bt_thread() -> threading.Thread:
        t = threading.Thread(target=_bt_reconnect, daemon=True)
        t.start()
        return t

    bt_thread = _make_bt_thread()
    supervised_threads["bluetooth"] = (bt_thread, _make_bt_thread)
    heartbeat_threads["bluetooth"] = (bt_thread, None)

    # Add agent scheduler to heartbeat reporting (not supervised — manages its own lifecycle)
    if agent_scheduler._thread is not None:
        heartbeat_threads["agents"] = (agent_scheduler._thread, None)

    # Pass heartbeat threads to MQTT for status reporting
    from scripts.mqtt_tts_listener import set_tracked_threads
    set_tracked_threads(heartbeat_threads)

    # Start supervisor thread to monitor and restart dead threads
    supervisor_thread = threading.Thread(
        target=_supervisor_loop,
        args=(supervised_threads, _shutdown_event, heartbeat_threads),
        daemon=True,
    )
    supervisor_thread.start()
    logger.info("Thread supervisor started")

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

    # Start voice listener with retry (blocks until KeyboardInterrupt or audio failure)
    max_voice_retries: int = 3
    for voice_attempt in range(1, max_voice_retries + 1):
        try:
            # Mark voice as active for heartbeat (main thread is the voice thread)
            heartbeat_threads["voice"] = (threading.current_thread(), None)
            set_tracked_threads(heartbeat_threads)

            start_voice_listener(ma_service)
            break  # Clean exit from voice listener
        except Exception as e:
            logger.error(
                "Voice listener failed",
                error=str(e),
                attempt=voice_attempt,
                max_attempts=max_voice_retries,
            )
            if voice_attempt < max_voice_retries:
                logger.info("Retrying voice listener", retry_in_seconds=10)
                time.sleep(10)

    # If voice listener exits (no mic, audio failure, etc.), keep the process
    # alive so MQTT, agents, and reminders continue to work. The node won't
    # respond to voice but can still receive commands from the mobile app.
    logger.warning("Voice listener exited — node running in headless mode (MQTT + agents only)")
    _shutdown_event.wait()
    logger.info("Node shutting down")


if __name__ == "__main__":
    main()

