import faulthandler
import os
import signal
import sys
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

# Reduce default thread stack size from 8 MB to 2 MB.
# 25 threads × 8 MB = 200 MB virtual address space — too much for Pi Zero
# (512 MB RAM). 2 MB accommodates native C++ extensions (onnxruntime,
# openwakeword) that need more stack than pure Python threads.
threading.stack_size(2 * 1024 * 1024)

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

# Initialize service discovery (jarvis-config-client) BEFORE importing
# any jarvis_log_client consumers. JarvisLogger resolves its server URL
# at __init__ time via ``_get_logs_url()`` — which queries
# ``jarvis_config_client.get_service_url("logs")``. If config-client
# isn't initialised, the call returns None and the URL falls back to
# the hard-coded ``http://localhost:7702`` default. Nothing listens on
# 7702 on a Pi, so every log batch silently fails and the client
# falls back to console-only mode.
#
# Subtle race: ``from scripts.voice_listener import start_voice_listener``
# below transitively imports jarvis_log_client AND instantiates
# ``logger = JarvisLogger(service="jarvis-node")`` at module level —
# so by the time main.py's own ``logger = JarvisLogger(...)`` runs the
# cached instance already has the wrong URL. Doing the init here, before
# any of the dependent imports, fixes the entire process tree.
#
# Failure mode for this discovery call is graceful: it logs a warning
# via stdlib logging and leaves config-client uninitialised, in which
# case JarvisLogger still falls through to its env-var → default
# fallback. Network outage at boot is the only realistic cause and
# systemd ``After=network-online.target`` keeps that window small.
from utils.service_discovery import init as init_service_discovery
init_service_discovery()

from jarvis_log_client import init as init_logging, init_node as init_logging_node, JarvisLogger

from scripts.mqtt_tts_listener import start_mqtt_listener
from scripts.voice_listener import start_voice_listener
from services.bluetooth_pair_agent import start_agent as start_bt_pair_agent
from services.agent_scheduler_service import initialize_agent_scheduler
from services.timer_service import initialize_timer_service
from utils.config_service import Config
from utils.music_assistant_service import DummyMusicAssistantService, MusicAssistantService

# Initialize logging.
# Prefer node-mode auth using the node credentials we already have in
# config.json — the node has no separate app credential registered with
# jarvis-auth, so app-mode auth would 401 every batch and trigger the
# fallback-to-console replay (entries appear duplicated in the journal).
# Fall back to app-mode if node credentials aren't present, so non-node
# environments (e.g. CLI invocations) still get console logging.
_node_id = Config.get_str("node_id") or os.getenv("JARVIS_NODE_ID", "")
_node_key = Config.get_str("api_key") or os.getenv("JARVIS_NODE_KEY", "")
if _node_id and _node_key:
    init_logging_node(node_id=_node_id, node_key=_node_key)
else:
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

    # The main thread is blocked inside start_voice_listener and the
    # voice loop doesn't actively poll _shutdown_event, so without a
    # forced exit systemd waits the full TimeoutStopSec=30 seconds and
    # SIGKILLs us — every `systemctl stop jarvis-node` (including the
    # one install.sh issues during upgrades) stalls for half a minute.
    # Give scheduler + log batches a brief grace period to flush, then
    # exit hard. systemd sees a clean stop and install.sh's stop step
    # returns quickly.
    def _force_exit() -> None:
        time.sleep(3.0)
        logger.info("Shutdown grace period elapsed — forcing exit")
        os._exit(0)
    threading.Thread(target=_force_exit, daemon=True).start()


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
    finally:
        # Alembic's fileConfig sets sqlalchemy.engine to the level in
        # alembic.ini and that level persists for the rest of the process.
        # On a 416 MB Pi, INFO-level SQL logging floods journald and burns
        # CPU formatting strings every secret read. Force WARNING regardless
        # of what alembic.ini said.
        import logging as _stdlib_logging
        _stdlib_logging.getLogger("sqlalchemy.engine").setLevel(_stdlib_logging.WARNING)


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

    # `kill -USR1 <pid>` dumps Python frames for every live thread to
    # stderr (→ journalctl). Hook for diagnosing voice-loop deadlocks
    # without rebuilding with py-spy on the Pi.
    faulthandler.register(signal.SIGUSR1, all_threads=True)

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

    # Apply persisted audio volume to PulseAudio. config.json is the
    # source of truth for the user-facing volume; re-apply on every
    # startup so a reboot doesn't reset the slider to PA's default.
    try:
        from utils.audio_volume import set_volume_percent
        vol = Config.get_int("volume_percent", -1)
        if 0 <= vol <= 100:
            set_volume_percent(vol)
    except Exception as e:
        logger.warning("Audio volume apply failed", error=str(e))

    # Codec self-heal: ensure the TLV320AIC3104's ADC HPF is enabled so
    # captured audio doesn't carry a DC pedestal that buries voice
    # signal under a constant bias and tricks Whisper into transcribing
    # everything as music ("*sad music*" — see May-2026 beta blocker).
    # install.sh sets this at install time and alsactl-stores it, but
    # ENUMERATED controls don't always round-trip cleanly through
    # alsactl restore — re-asserting at startup is cheap insurance.
    try:
        from utils.audio_volume import ensure_adc_hpf_enabled
        ensure_adc_hpf_enabled()
    except Exception as e:
        logger.warning("ADC HPF self-heal failed (non-fatal)", error=str(e))

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
    # Service discovery was already initialised at module-import time
    # (above the jarvis_log_client import so the log-client picks up
    # the correct logs URL on first instantiation — see comment block
    # at the top of this file). Just report the status here for the
    # remote log so operators can confirm at a glance.
    from utils.service_discovery import is_initialized as _service_discovery_initialized
    if _service_discovery_initialized():
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
    led_service = None
    try:
        from services.alert_queue_service import get_alert_queue_service
        from services.led_service import get_led_service

        led_service = get_led_service()
        # Apply persisted LED preferences from config.json before anything
        # else can drive the LEDs so the user's chosen brightness / off
        # state is honored from the first frame (vs. flickering at full
        # brightness for one tick before update_node_config dials it back).
        if hasattr(led_service, "set_enabled"):
            led_service.set_enabled(Config.get_bool("led_enabled", True))
        if hasattr(led_service, "set_brightness_scale"):
            led_service.set_brightness_scale(Config.get_int("led_brightness_percent", 100))
        alert_queue = get_alert_queue_service()
        alert_queue.on_change = lambda count: led_service.set_pattern("alert" if count > 0 else "normal")
    except Exception as e:
        logger.warning("Alert/LED service init failed (non-fatal)", error=str(e))

    # Initialize ReSpeaker button (GPIO17) — short-press speaks queued alerts
    # via local TTS and flushes the queue; long-hold (>=3s) powers the node
    # off cleanly. Both events also publish on MQTT for CC observability.
    # No-op on hardware without the HAT or with gpiozero unavailable.
    try:
        from services.button_service import get_button_service

        def _mqtt_client_provider():
            from scripts.mqtt_tts_listener import _mqtt_client as client
            return client

        if led_service is not None:
            button_service = get_button_service(led_service, _mqtt_client_provider)
            if alert_queue is not None:
                button_service.on_short_press = lambda: alert_queue.announce_pending_and_flush(led_service)
    except Exception as e:
        logger.warning("Button service init failed (non-fatal)", error=str(e))

    # Initialize agent scheduler (Home Assistant, etc.)
    agent_scheduler = initialize_agent_scheduler()
    if alert_queue is not None:
        agent_scheduler.set_alert_queue(alert_queue)
    logger.info("Agent scheduler initialized")

    # MusicAssistantService (utils/music_assistant_service.py) is currently broken
    # against websockets >= 14 (uses removed `ws.closed` attr) and recursively
    # retries forever, pegging CPU on Pi Zeros. Nothing actually calls its
    # methods today — voice_listener handles pause/resume via PulseAudio, and
    # the jarvis-cmd-music-assistant Pantry package is the real music control
    # path. Always use the no-op stub until the shim is rewritten.
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

    # Start a persistent BlueZ pair agent so incoming pair requests
    # (phone → Pi) and A2DP profile authorization complete without user
    # interaction. Without this, bluez rejects auth with "Authentication
    # attempt without agent" and the phone sees "pairing unsuccessful".
    start_bt_pair_agent()

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

    # NOTE (v0.1.69): the v0.1.64-introduced ``connectivity_watchdog``
    # was removed here. It exited the process when CC was unreachable
    # for 5+ minutes, triggering a systemd restart — but the restarted
    # process then re-ran ``is_provisioned()``, which conflates "CC
    # unreachable" with "not provisioned" and drops the node into AP
    # mode. So on every transient CC blip the node tore down its WiFi
    # client and broadcast as ``jarvis-XXXX``, stuck offline until a
    # physical reboot. paho-mqtt's auto-reconnect already rides
    # transient outages correctly without any of this; the watchdog
    # was a misguided fix that actively created the very failure mode
    # it claimed to repair.
    #
    # The legitimate "CC unreachable so re-provisioning is needed
    # (WiFi changed)" case is still handled by the existing
    # is_provisioned() retry-then-AP-mode flow. AP mode itself runs
    # a recovery watcher (provisioning.recovery_watcher) that polls
    # the saved CC URL and reboots the node if CC comes back — so a
    # transient outage that DID happen to land during boot self-heals
    # without manual intervention.

    # Warm up the LLM by sending a throwaway request through the full
    # pipeline (tool registration → system prompt → KV cache).  This
    # primes llama.cpp's prefix cache so the first real voice command is fast.
    # SKIP on install-triggered restart: `process_voice_command` streams a
    # TTS reply back through the speaker, which is fine to hear once at
    # boot but annoying on every package install. The first voice command
    # after install will be a beat slower; acceptable trade-off.
    from services.package_install_handler import (
        flush_post_restart_install_result,
        has_pending_install_result,
    )
    _install_restart = has_pending_install_result()
    if _install_restart:
        logger.info("Skipping LLM warmup — install-triggered restart, staying silent")
    else:
        try:
            from utils.command_execution_service import CommandExecutionService
            warmup_service = CommandExecutionService()
            logger.info("Warming up LLM pipeline")
            warmup_service.process_voice_command("hello")
            logger.info("LLM warmup complete")
        except Exception as e:
            logger.warning("LLM warmup failed (non-fatal)", error=str(e))

    # Flush any install result the previous process deferred when it
    # restarted to load new pip imports. Done after MQTT + warmup so the
    # node is fully ready, and BEFORE the voice listener starts so the
    # wake-word path doesn't engage during the install→restart window.
    # Mobile only sees "Done" once this POST lands.
    try:
        flush_post_restart_install_result()
    except Exception as e:
        logger.warning("Post-restart install result flush failed (non-fatal)", error=str(e))

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
            import traceback as _tb
            logger.error(
                "Voice listener failed",
                error=str(e),
                error_type=type(e).__name__,
                traceback=_tb.format_exc(),
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

