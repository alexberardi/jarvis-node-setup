import faulthandler
import os
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

# Reduce default thread stack size from 8 MB to 2 MB.
# 25 threads × 8 MB = 200 MB virtual address space — too much for Pi Zero
# (512 MB RAM). 2 MB accommodates native C++ extensions (onnxruntime,
# openwakeword) that need more stack than pure Python threads.
threading.stack_size(2 * 1024 * 1024)

# Cap glibc malloc arenas BEFORE any worker thread spawns. A multi-threaded
# process on glibc opens up to 8×CPU arenas (32 on the 4-core Pi Zero 2),
# which fragment independently under steady transient churn. Belt-and-suspenders
# alongside the per-wake-cycle malloc_trim (core/wake_loop.py) that actually
# reclaims each voice command's freed audio buffers; applied via mallopt so it
# ships with a code update (the systemd unit also sets MALLOC_ARENA_MAX for
# fresh installs; setting both is harmless). No-op off glibc (macOS dev).
try:
    import ctypes as _ctypes

    _libc_malloc = _ctypes.CDLL("libc.so.6")
    _M_TRIM_THRESHOLD, _M_ARENA_MAX = -1, -8  # from <malloc.h>
    _libc_malloc.mallopt(_M_ARENA_MAX, 2)
    _libc_malloc.mallopt(_M_TRIM_THRESHOLD, 128 * 1024)
except Exception as _malloc_err:  # non-glibc (e.g. macOS dev) — harmless no-op
    print(f"glibc malloc tuning skipped: {_malloc_err}", file=sys.stderr)

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

# Pick the config-URL resolution style from the vantage the config-service host
# reveals. The Pi runs `python -m scripts.main` directly and never touches
# entrypoint.py, so this MUST happen here too — otherwise the node uses whatever
# the systemd unit hardcodes (historically 'remote', which can't reach
# container-name HTTP rows off-box). An explicit dockerized/external override
# wins; an unset OR stale-'remote' style is (re)computed, so a node whose unit
# a code-only update didn't regenerate still self-heals.
_cfg_url = os.environ.get("JARVIS_CONFIG_URL", "")
if _cfg_url:
    from utils.config_env import apply_config_url_style
    apply_config_url_style(_cfg_url)

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


def _start_tracemalloc_diagnostic() -> None:
    """Opt-in allocation tracer for finding memory leaks in long-running paths.

    Enabled when ``JARVIS_TRACEMALLOC=1`` is set in the unit environment.
    Cheap when off (env check + return). When on:

    - ``tracemalloc.start(25)`` traces every allocation with a 25-frame stack
    - A background thread takes a snapshot every 5 min and appends to
      ``/tmp/jarvis-tracemalloc.log``:
        * Top 20 allocators by current size
        * Diff vs. the previous snapshot (positive deltas = something grew)

    Overhead is real (~10-15% CPU + 50-80 MB extra working-set for the
    tracer itself), so don't leave it on in production. The point is to
    tail the log during a known-leaky workload and read off which
    file:line keeps appearing in the "growth since previous" section.

    Must be called BEFORE any other code so tracemalloc sees the full
    picture from process start.
    """
    if os.environ.get("JARVIS_TRACEMALLOC") != "1":
        return
    import tracemalloc
    tracemalloc.start(25)
    logger.info("tracemalloc enabled — top allocators will land in /tmp/jarvis-tracemalloc.log")

    from pathlib import Path
    out_path = Path("/tmp/jarvis-tracemalloc.log")
    interval_secs = int(os.environ.get("JARVIS_TRACEMALLOC_INTERVAL", "300"))

    def _snapshot_loop() -> None:
        prev_snap = None
        while True:
            time.sleep(interval_secs)
            try:
                import datetime as _dt
                snap = tracemalloc.take_snapshot()
                # Filter out tracemalloc's own bookkeeping and unimportant noise
                snap = snap.filter_traces((
                    tracemalloc.Filter(False, tracemalloc.__file__),
                    tracemalloc.Filter(False, "<frozen importlib._bootstrap>"),
                    tracemalloc.Filter(False, "<frozen importlib._bootstrap_external>"),
                ))
                ts = _dt.datetime.now(_dt.timezone.utc).isoformat()
                with out_path.open("a") as f:
                    f.write(f"\n=== {ts} ===\n")
                    f.write("Top 20 allocators (by current size):\n")
                    for i, stat in enumerate(snap.statistics("lineno")[:20], 1):
                        frame = stat.traceback[0]
                        f.write(
                            f"  {i:2}. {stat.size/1024:>9.1f} KB "
                            f"({stat.count:>6d} blocks)  "
                            f"{frame.filename}:{frame.lineno}\n"
                        )
                    if prev_snap is not None:
                        diffs = snap.compare_to(prev_snap, "lineno")
                        growth = [s for s in diffs if s.size_diff > 1024][:15]
                        if growth:
                            f.write("\nGrowth since previous snapshot:\n")
                            for stat in growth:
                                frame = stat.traceback[0]
                                f.write(
                                    f"  +{stat.size_diff/1024:>9.1f} KB "
                                    f"({stat.count_diff:+d} blocks)  "
                                    f"{frame.filename}:{frame.lineno}\n"
                                )
                prev_snap = snap
            except Exception:
                # Diagnostic must never take down the node.
                pass

    threading.Thread(
        target=_snapshot_loop, daemon=True, name="tracemalloc-dump",
    ).start()


def _lock_pages_in_ram() -> None:
    """Pin pages-on-fault so accessed pages never get swapped to SD.

    Calls ``mlockall(MCL_CURRENT | MCL_FUTURE | MCL_ONFAULT)``:

    - ``MCL_CURRENT``: lock all pages currently resident in RAM
    - ``MCL_FUTURE``:  lock pages newly faulted in (libraries loaded
      later, lazy module imports, etc.)
    - ``MCL_ONFAULT``: don't pre-fault — only lock pages once they're
      actually accessed, so cold code paths never enter RAM at all

    Net effect on Pi Zero: peak RAM caps at our actual working set
    (~120-150 MiB) instead of the committed VM size (~280 MiB), and
    the oww/AEC/audio path stays in RAM regardless of overall memory
    pressure — eliminating the wake-detection-lag-from-swap-stall
    pattern. Non-Linux hosts and dev macOS no-op cleanly.

    The systemd unit must set ``LimitMEMLOCK=infinity`` for this to
    work as a non-root user; otherwise the default 64 KiB cap blocks
    mlockall and we log a warning and continue (no swap-locking, but
    process keeps running).
    """
    if sys.platform != "linux":
        return  # mlockall is Linux-specific; mac dev runs degrade cleanly

    try:
        import ctypes
        import ctypes.util

        libc_path = ctypes.util.find_library("c")
        if not libc_path:
            logger.warning("Could not find libc; skipping mlock")
            return
        libc = ctypes.CDLL(libc_path, use_errno=True)
    except Exception as e:
        logger.warning("mlock setup failed (libc load)", error=str(e))
        return

    MCL_CURRENT = 1
    MCL_FUTURE = 2
    MCL_ONFAULT = 4

    # First try with ONFAULT (Linux 4.4+). On older kernels this returns
    # EINVAL — fall back to the classic MCL_CURRENT | MCL_FUTURE which
    # pre-faults everything (larger RAM footprint but still avoids swap).
    for flags, label in (
        (MCL_CURRENT | MCL_FUTURE | MCL_ONFAULT, "current+future+onfault"),
        (MCL_CURRENT | MCL_FUTURE, "current+future"),
    ):
        if libc.mlockall(flags) == 0:
            logger.info("Memory locked into RAM", mode=label)
            return
        err = ctypes.get_errno()
        # EINVAL on the onfault attempt → try the fallback. Other errors
        # (EPERM, ENOMEM) are terminal — log and move on without locking.
        if err == 22:  # EINVAL
            continue
        logger.warning(
            "mlockall failed — wake detection may lag under memory pressure",
            errno=err,
            error=os.strerror(err),
            hint="check systemd LimitMEMLOCK=infinity",
        )
        return

    logger.warning("mlockall not supported on this kernel; skipping")


def _run_boot_warmup(timeout_s: float = 20.0) -> bool:
    """Prime the LLM KV cache at boot without ever blocking the wake listener.

    Runs a TEXT-ONLY warmup (``parse_voice_command`` — tool registration + an
    LLM prefill pass, but NO audio playback) on a daemon thread and waits at
    most ``timeout_s`` for it. Two failure modes this guards against, both of
    which previously left the node wake-dead:

    * a wedged ALSA sink making the old ``process_voice_command`` "hello" reply
      hang forever in aplay (eliminated outright by going text-only), and
    * a slow/unreachable command-center or LLM making the warmup network call
      hang (bounded here by the join timeout — we log and continue).

    Returns True if the warmup finished within the timeout, False otherwise.
    """
    logger.info("Warming up LLM pipeline")

    def _warm() -> None:
        try:
            from utils.command_execution_service import CommandExecutionService
            CommandExecutionService().parse_voice_command("hello")
        except Exception as e:
            logger.warning("LLM warmup failed (non-fatal)", error=str(e))

    warm_thread = threading.Thread(target=_warm, daemon=True, name="boot-warmup")
    warm_thread.start()
    warm_thread.join(timeout_s)
    if warm_thread.is_alive():
        logger.warning(
            "LLM warmup did not finish within timeout; continuing to voice listener",
            timeout_s=timeout_s,
        )
        return False
    logger.info("LLM warmup complete")
    return True


def main():
    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    # `kill -USR1 <pid>` dumps Python frames for every live thread to
    # stderr (→ journalctl). Hook for diagnosing voice-loop deadlocks
    # without rebuilding with py-spy on the Pi.
    faulthandler.register(signal.SIGUSR1, all_threads=True)

    # `kill -USR2 <pid>` dumps a Python object-type census to
    # /tmp/jarvis_soak/heap_main_pid<PID>_<ts>.txt. Lower memory than
    # tracemalloc (which OOM'd on Pi Zero 2W under swap pressure). For
    # leak hunting — take two snapshots 60+ min apart and diff.
    from services.heap_census import register_sigusr2
    register_sigusr2("main")

    # Startup banner — visible in journalctl for debugging
    logger.info("Jarvis node starting",
                config_path=os.environ.get("CONFIG_PATH", "config.json"),
                node_id=Config.get_str("node_id", "unknown"),
                room=Config.get_str("room", "unknown"))

    # DISABLED 2026-05-28 (v0.1.81): mlockall(MCL_CURRENT|FUTURE|ONFAULT)
    # consistently OOM-killed jarvis-node (and sshd / wpa_supplicant in
    # the cascade) on Pi Zero 2W. MCL_FUTURE means every faulted page
    # gets permanently locked, so the working set grows past the
    # ~300 MiB usable RAM and the kernel can't reclaim. Reverting to
    # the swap-thrash trade-off until a more surgical mlock (specific
    # pages: oww model + AEC buffers only, not the whole process) lands.
    # The _lock_pages_in_ram helper is kept for that future work.
    # _lock_pages_in_ram()

    # Opt-in allocation tracing — set JARVIS_TRACEMALLOC=1 in the unit
    # environment to dump top allocators + growth deltas to
    # /tmp/jarvis-tracemalloc.log every 5 min. Used for leak hunting.
    # Started here (after startup banner, before everything else) so
    # tracemalloc sees the full allocation history from module imports
    # onward.
    _start_tracemalloc_diagnostic()

    # Validate config keys (warnings only — provisioning may resolve them)
    _validate_config()

    # Auto-initialize encryption key (K1) if it doesn't exist yet
    try:
        from utils.encryption_utils import initialize_encryption_key
        initialize_encryption_key()
    except Exception as e:
        logger.warning("Encryption key init failed", error=str(e))

    # Apply audio volume to PulseAudio on every startup. config.json is
    # the source of truth for the user-facing volume; re-apply on every
    # startup so a reboot doesn't reset the slider to PA's default. The
    # default 100% covers fresh installs where config.json has no
    # ``volume_percent`` yet — without this, the mobile slider showed
    # 100% but PA was at whatever the system default was, so users had
    # to move the slider and save just to make the displayed value real.
    try:
        from utils.audio_volume import set_volume_percent
        vol = Config.get_int("volume_percent", 100)
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

    # Output mixer self-heal: same alsactl-restore round-trip story as
    # the ADC HPF above — install.sh sets a calibrated baseline (Line at
    # +4 dB, Line DAC at -1.5 dB, HP/HPCOM muted) and stores it, but the
    # kernel TLV320 driver can leave the live mixer at quiet defaults
    # after boot. Without this, "100% volume" is ~26 dB below intended,
    # which is the prod-kitchen v0.1.81 fresh-install symptom that drove
    # this self-heal.
    try:
        from utils.audio_volume import ensure_output_baseline
        ensure_output_baseline()
    except Exception as e:
        logger.warning("Output baseline self-heal failed (non-fatal)", error=str(e))

    # Run DB migrations before anything that needs the database
    _run_db_migrations()

    # Register SDK storage backend (must be after DB migrations)
    try:
        from services.storage_backend import init_storage_backend
        init_storage_backend()
    except Exception as e:
        logger.warning("Storage backend init failed, commands may lack persistence", error=str(e))

    # Register SDK inbox backend (posts route through command-center; no DB dependency)
    try:
        from services.inbox_backend import init_inbox_backend
        init_inbox_backend()
    except Exception as e:
        logger.warning("Inbox backend init failed, commands cannot post inbox items", error=str(e))

    # Provisioning gate (skip in development mode). Three distinct cases —
    # conflating them stranded the prod kitchen node on 2026-07-05:
    #   1. No marker → fresh node → plain AP provisioning mode.
    #   2. Marker + WiFi won't join within grace → RECOVERABLE AP mode:
    #      the AP↔STA cycle (provisioning.recovery_watcher) keeps retrying
    #      the known WiFi and reboots back to normal when it returns, while
    #      still letting the user re-pair if the WiFi genuinely changed.
    #   3. Marker + WiFi up but CC unreachable → NEVER AP mode: that's a
    #      server/internet condition provisioning can't fix. Log, continue,
    #      and rely on runtime retries (MQTT reconnect, heartbeat, voice).
    if not os.environ.get("JARVIS_SKIP_PROVISIONING_CHECK", "").lower() in ("true", "1", "yes"):
        from provisioning.startup import (
            should_enter_provisioning,
            wait_for_command_center,
            wait_for_wifi,
        )
        if should_enter_provisioning():
            logger.warning("No provisioning marker — node needs setup")
            _run_provisioning_and_restart()
            return  # Should not reach here due to os.execv
        if not wait_for_wifi():
            logger.warning(
                "WiFi did not join within boot grace — entering recoverable "
                "provisioning mode (AP↔STA cycle keeps retrying the known network)"
            )
            _run_provisioning_and_restart()
            return  # Should not reach here due to os.execv
        if not wait_for_command_center():
            logger.warning(
                "WiFi is up but command center is unreachable — continuing "
                "startup; connectivity will be retried in normal operation"
            )
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

    # Pull the household routine set from CC (pull-on-nudge backstop for any
    # nudges missed while offline). Fail-soft: a missed pull just leaves the
    # local routine store as-is; voice still works off defaults.
    try:
        from services.routine_sync_service import pull_routines
        pulled = pull_routines()
        logger.info("Routine boot-pull complete", count=pulled)
    except Exception as e:
        logger.warning("Routine boot-pull failed, continuing with local routines", error=str(e))

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
        # count = announceable (priority>=3) alerts only — silent low-priority
        # alerts (news, calendar proximity) no longer light the LED. Level
        # signal: the same value repeats every scheduler tick; set_pattern
        # dedups, and the repetition self-heals any LED/queue divergence.
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

    # Memory watchdog: logs RSS+swap+thread slope every ~5 min and ERRORs if it
    # drifts past a threshold — so a future memory regression surfaces in the
    # logs instead of as a slow wake weeks later (this is the "catch leaks
    # before prod" instrument). Cheap; disable with JARVIS_MEMORY_WATCHDOG=0.
    if os.environ.get("JARVIS_MEMORY_WATCHDOG", "1") != "0":
        try:
            from utils.memory_hygiene import MemoryWatchdog

            MemoryWatchdog(
                interval_seconds=float(
                    os.environ.get("JARVIS_MEMORY_WATCHDOG_INTERVAL", "300")
                ),
            ).start()
        except Exception as e:
            logger.warning("Memory watchdog init failed (non-fatal)", error=str(e))

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

    # command_discovery_service no longer runs a background refresh thread
    # (the poll re-imported custom_commands every cycle and leaked module
    # objects). Discovery is now triggered only by install/uninstall code
    # paths, so it doesn't need a shutdown hook.

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

    # If the previous process restarted to apply a package change, we need
    # to know NOW (it suppresses the LLM warmup TTS reply — "hello" out the
    # speaker — later in boot). The deferred result itself is flushed AFTER
    # the warmup section below, once command + agent discovery have
    # initialized, so the flush can verify the installed components
    # actually loaded before telling CC "done".
    _install_restart: bool = False
    try:
        from services.package_install_handler import has_pending_install_result
        _install_restart = has_pending_install_result()
    except Exception as e:
        logger.warning("Pending install-result check failed (non-fatal)", error=str(e))

    # Same silent-on-boot treatment for maintenance-triggered restarts.
    # A scheduled 3 AM restart that loudly spoke the LLM warmup
    # "Hello! How can I assist you today?" through the speaker would
    # defeat the entire point of running a quiet maintenance window;
    # the RSS-ceiling emergency stop has the same audibility concern.
    # The flag rolls into ``_silent_restart`` below so the warmup gate
    # treats both cases identically.
    _maintenance_restart: bool = False
    try:
        from services.maintenance_restart_service import (
            clear_pending_maintenance_restart,
            has_pending_maintenance_restart,
        )
        _maintenance_restart = has_pending_maintenance_restart()
        if _maintenance_restart:
            clear_pending_maintenance_restart()
    except Exception as e:
        logger.warning(
            "Maintenance-restart marker check failed (non-fatal)",
            error=str(e),
        )

    # Either an install-triggered restart OR a maintenance-triggered
    # restart should keep boot silent.
    _silent_restart: bool = _install_restart or _maintenance_restart

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

    # Start the maintenance-restart scheduler. On the Pi Zero 2W, a daily
    # restart at a quiet hour clears accumulated allocator fragmentation
    # and library-internal caches that no in-process GC can reach. The
    # window is user-configurable via the ``maintenance.restart_at_time``
    # + ``maintenance.restart_enabled`` settings; an RSS-ceiling fallback
    # protects against a regression that drives the leak rate above what
    # the daily window bounds. See services/maintenance_restart_service.py.
    from services.maintenance_restart_service import (
        get_maintenance_restart_service,
    )
    get_maintenance_restart_service().start()

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
    # (2026-07-05 update): the remaining instance of that same conflation
    # — is_provisioned()'s retry-then-AP-mode flow at startup — stranded
    # the prod kitchen node after a net-watchdog reboot and is now also
    # removed. Provisioning is entered ONLY when the marker is absent;
    # re-provisioning a relocated node is an explicit factory reset. The
    # AP-mode recovery watcher could never rescue these cases anyway:
    # AP mode's captive dnsmasq resolves every hostname to the node
    # itself, so its CC reachability probe can never succeed.

    # Warm up the LLM to prime llama.cpp's prefix/KV cache so the first real
    # voice command is fast. _run_boot_warmup uses the TEXT-ONLY warmup path
    # (parse_voice_command — no audio playback) on a bounded daemon thread, so
    # neither a wedged ALSA sink nor a hung command-center/LLM can keep boot
    # from reaching the wake listener below. Still SKIP entirely on
    # install/maintenance restarts to stay quiet.
    if _silent_restart:
        reason = (
            "install-triggered" if _install_restart else "maintenance-triggered"
        )
        logger.info(
            "Skipping LLM warmup — silent restart, staying quiet",
            reason=reason,
        )
    else:
        _run_boot_warmup()

    # Flush any package install/uninstall result deferred by the previous
    # process. This deliberately runs AFTER agent scheduler init and the
    # warmup's command discovery so the flush can verify the installed
    # package's components actually loaded post-restart before reporting
    # "done" to CC (the health check forces a discovery pass itself when
    # none has happened yet — e.g. on silent restarts that skip warmup).
    # The voice listener still starts only after this point, so the
    # wake-word-during-restart invariant is preserved.
    if _install_restart:
        try:
            from services.package_install_handler import flush_post_restart_install_result
            flush_post_restart_install_result()
        except Exception as e:
            logger.warning("Install-result flush failed (non-fatal)", error=str(e))

    # Spawn a continuous silent stream on the default sink so the TLV320
    # ALSA driver never gets a chance to wedge into SUSPENDED. Without
    # an active sink-input pulse marks the sink IDLE and the driver
    # enters a state it can't reliably resume from ("Resume failed,
    # couldn't restore original sample settings" floods pulse's journal).
    # A 0-amplitude /dev/zero feed costs <0.2% CPU but holds the sink
    # permanently RUNNING.
    #
    # 44.1 kHz matches Spotify Connect's native rate. After paplay starts
    # and pulse has registered the sink-input, drop its volume to ~-170 dB
    # via pactl. Both knobs together produce the right behavior:
    #
    # - With volume at 100 % (default), pulse's mixer treats it as a
    #   real concurrent stream and applies headroom math when other
    #   sink-inputs (e.g. music) are also at high volume — this produced
    #   audible static at high speaker volume on 2026-06-03.
    # - With volume <near-zero> but non-zero, pulse still pulls samples
    #   and keeps the sink-input "active" (so the sink stays RUNNING and
    #   the TLV320 doesn't wedge into SUSPENDED), but the contribution
    #   to the mix is effectively zero and other streams play through
    #   without the headroom hit.
    _sink_keepalive_proc: subprocess.Popen | None = None
    # ReSpeaker-HAT-only workaround: the keepalive exists solely to dodge the
    # TLV320's broken resume-from-SUSPENDED path. It's pointless on other
    # hardware and wrong inside a container sharing a host PulseAudio sink, so
    # only run it when the HAT is actually present.
    from utils.audio_volume import has_respeaker_hat
    if not has_respeaker_hat():
        logger.info("Sink keepalive skipped (no ReSpeaker HAT detected)")
    else:
        try:
            _sink_keepalive_proc = subprocess.Popen(
                [
                    "paplay",
                    "--raw",
                    "--rate=44100",
                    "--channels=2",
                    "--format=s16le",
                    "/dev/zero",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            logger.info(
                "Sink keepalive started", pid=_sink_keepalive_proc.pid,
            )
            # Give pulse a moment to register the new sink-input, then
            # adjust its volume. Look up by application.process.id so we
            # find OUR paplay specifically (not any other pacat/paplay
            # streams a user might have running).
            time.sleep(2.0)
            try:
                import json as _json
                r = subprocess.run(
                    ["pactl", "-f", "json", "list", "sink-inputs"],
                    capture_output=True, text=True, timeout=2.0,
                )
                if r.returncode == 0:
                    target_id: Optional[str] = None
                    for item in _json.loads(r.stdout or "[]"):
                        pid_str = (item.get("properties") or {}).get(
                            "application.process.id",
                        )
                        if (
                            pid_str
                            and str(_sink_keepalive_proc.pid) == str(pid_str)
                        ):
                            target_id = str(item.get("index"))
                            break
                    if target_id is not None:
                        subprocess.run(
                            ["pactl", "set-sink-input-volume", target_id, "100"],
                            timeout=2.0, capture_output=True,
                        )
                        logger.info(
                            "Sink keepalive volume reduced",
                            sink_input_id=target_id,
                        )
                    else:
                        logger.warning(
                            "Sink keepalive sink-input not found; volume "
                            "left at default — static may be audible at high "
                            "output volume",
                        )
            except Exception as e:
                logger.warning(
                    "Sink keepalive volume adjust failed",
                    error=str(e),
                )
        except Exception as e:
            logger.warning(
                "Sink keepalive failed to start; sink-wedge fallback recovery "
                "will still fire on each TTS, but the gap window remains",
                error=str(e),
            )

    # Start voice listener with retry (blocks until KeyboardInterrupt or audio
    # failure). Two failure classes are handled DIFFERENTLY:
    #   - ServiceUnresolvedError: config-service is unreachable, so the voice
    #     path can't resolve command-center. Retry INDEFINITELY with backoff —
    #     config-service comes back (cold boot / full-stack restart), and going
    #     headless here would leave the node permanently voice-dead until a
    #     manual reboot (systemd won't restart a process that stays alive). This
    #     mirrors the MQTT connect loop, which already retries discovery forever.
    #   - Any other failure (no mic / audio device error): bounded retry, then
    #     fall through to headless — a broken mic won't self-heal.
    from utils.service_discovery import ServiceUnresolvedError

    max_voice_retries: int = 3
    audio_attempt: int = 0
    discovery_attempt: int = 0
    while _shutdown_event is None or not _shutdown_event.is_set():
        try:
            # Mark voice as active for heartbeat (main thread is the voice thread)
            heartbeat_threads["voice"] = (threading.current_thread(), None)
            set_tracked_threads(heartbeat_threads)

            start_voice_listener(ma_service)
            break  # Clean exit from voice listener
        except ServiceUnresolvedError as e:
            discovery_attempt += 1
            backoff: int = min(2 ** min(discovery_attempt, 6), 60)
            logger.warning(
                "Voice listener can't resolve services yet (config-service "
                "unreachable) — retrying, will self-heal when it returns",
                error=str(e),
                attempt=discovery_attempt,
                retry_in_seconds=backoff,
            )
            if _shutdown_event is not None:
                if _shutdown_event.wait(timeout=backoff):
                    break
            else:
                time.sleep(backoff)
        except Exception as e:
            audio_attempt += 1
            import traceback as _tb
            logger.error(
                "Voice listener failed",
                error=str(e),
                error_type=type(e).__name__,
                traceback=_tb.format_exc(),
                attempt=audio_attempt,
                max_attempts=max_voice_retries,
            )
            if audio_attempt >= max_voice_retries:
                break  # non-recoverable (audio) → headless
            logger.info("Retrying voice listener", retry_in_seconds=10)
            if _shutdown_event is not None:
                if _shutdown_event.wait(timeout=10):
                    break
            else:
                time.sleep(10)

    # If voice listener exits (no mic, audio failure, etc.), keep the process
    # alive so MQTT, agents, and reminders continue to work. The node won't
    # respond to voice but can still receive commands from the mobile app.
    logger.warning("Voice listener exited — node running in headless mode (MQTT + agents only)")
    _shutdown_event.wait()
    logger.info("Node shutting down")


if __name__ == "__main__":
    main()

