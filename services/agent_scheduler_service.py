"""
Agent scheduler service for running background agents.

Manages the lifecycle of IJarvisAgent implementations:
- Discovers agents via AgentDiscoveryService
- Runs agents on their configured schedules
- Aggregates context data for voice request injection

Uses asyncio event loop in a daemon thread (Pi Zero compatible).
"""

import asyncio
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from jarvis_log_client import JarvisLogger

from core.ijarvis_agent import IJarvisAgent
from db import SessionLocal
from repositories.agent_registry_repository import AgentRegistryRepository
from services.alert_queue_service import AlertQueueService
from utils.agent_discovery_service import get_agent_discovery_service

logger = JarvisLogger(service="jarvis-node")

# Check interval for schedule evaluation (seconds)
SCHEDULER_CHECK_INTERVAL = 10


class AgentSchedulerService:
    """Singleton service for scheduling and running background agents.

    Creates a dedicated asyncio event loop in a daemon thread to run
    async agents without blocking the main thread.

    Thread safety:
        - Agent runs happen in the asyncio thread
        - Context access (get_aggregated_context) is thread-safe via lock
        - Lifecycle methods (start, stop) are thread-safe
        - Running state uses threading.Event for thread-safe flag access
    """

    _instance: Optional["AgentSchedulerService"] = None
    _lock: threading.RLock = threading.RLock()  # Use RLock for reentrant acquisition

    def __new__(cls) -> "AgentSchedulerService":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        with self._lock:
            if self._initialized:
                return

            self._agents: Dict[str, IJarvisAgent] = {}
            self._last_run: Dict[str, float] = {}  # agent_name -> timestamp
            self._context_cache: Dict[str, Dict[str, Any]] = {}
            self._context_lock = threading.Lock()

            # Consecutive failure tracking — when an agent throws three runs
            # in a row, the scheduler trips the registry's `enabled` flag to
            # 0 and records a reason so the snapshot can surface a clear UI
            # signal ("this agent was auto-disabled because of X"). The
            # reason map is in-memory; on node restart we lose the reason
            # but the disabled state persists (enabled=0 in the registry).
            # If the underlying issue is fixed and the user re-enables, the
            # counter starts fresh and the agent gets another chance.
            self._consecutive_failures: Dict[str, int] = {}
            self._auto_disabled_reasons: Dict[str, str] = {}

            self._loop: Optional[asyncio.AbstractEventLoop] = None
            self._thread: Optional[threading.Thread] = None
            self._running_event = threading.Event()  # Thread-safe running flag
            self._stop_event: Optional[asyncio.Event] = None
            self._alert_queue: Optional[AlertQueueService] = None

            # Cache the agent_registry enabled-map. _check_and_run_agents reads
            # it every 10s tick, and each read opened a fresh SQLCipher session
            # — and SQLCipher re-derives the key (PBKDF) on every new
            # connection, so this was real CPU + allocation churn on the Pi
            # Zero every 10s. The registry only changes on a mobile toggle or
            # an auto-disable, so a short TTL is plenty; _auto_disable_agent
            # invalidates explicitly so a tripped agent stops promptly.
            self._enabled_cache: Optional[Dict[str, bool]] = None
            self._enabled_cache_ts: float = 0.0

            self._initialized = True

    AUTO_DISABLE_AFTER_FAILURES: int = 3

    def set_alert_queue(self, queue: AlertQueueService) -> None:
        """Wire the alert queue so agent alerts are collected after each run."""
        self._alert_queue = queue

    @property
    def _running(self) -> bool:
        """Thread-safe access to running state."""
        return self._running_event.is_set()

    @_running.setter
    def _running(self, value: bool) -> None:
        """Thread-safe update of running state."""
        if value:
            self._running_event.set()
        else:
            self._running_event.clear()

    def start(self) -> None:
        """Start the agent scheduler.

        Discovers agents, creates the asyncio event loop, and starts
        the background scheduler thread.
        """
        if self._running:
            logger.warning("Agent scheduler already running")
            return

        # Discover agents
        discovery = get_agent_discovery_service()
        self._agents = discovery.get_all_agents()

        if not self._agents:
            logger.info("No agents discovered, scheduler not starting")
            return

        # Ensure every discovered agent has a registry row (default enabled).
        # New agents from a fresh package install land here too, so the mobile
        # app's Agents tab can toggle them right away.
        self._ensure_agents_registered(list(self._agents.keys()))

        logger.info("Starting agent scheduler", agent_count=len(self._agents))

        # Create and start the scheduler thread
        self._running = True
        self._thread = threading.Thread(target=self._run_event_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the agent scheduler gracefully."""
        if not self._running:
            return

        logger.info("Stopping agent scheduler")
        self._running = False

        # Signal the event loop to stop
        if self._loop and self._stop_event:
            self._loop.call_soon_threadsafe(self._stop_event.set)

        # Wait for thread to finish (with timeout)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)

        self._loop = None
        self._thread = None
        logger.info("Agent scheduler stopped")

    def _run_event_loop(self) -> None:
        """Run the asyncio event loop in the background thread."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._stop_event = asyncio.Event()

        try:
            self._loop.run_until_complete(self._scheduler_loop())
        except Exception as e:
            logger.error("Agent scheduler loop error", error=str(e))
        finally:
            self._loop.close()

    async def _scheduler_loop(self) -> None:
        """Main scheduler loop - runs agents on their schedules."""
        # Run startup agents immediately
        await self._run_startup_agents()

        # Main scheduling loop
        while self._running:
            try:
                # Check which agents need to run
                await self._check_and_run_agents()

                # Evict expired alerts and re-deliver the announceable count
                # to the LED every tick — reconciliation, not edge-trigger,
                # so a dropped or out-of-order on_change callback can't
                # strand a stale purple LED for more than one interval.
                if self._alert_queue is not None:
                    try:
                        self._alert_queue.sweep_expired()
                    except Exception as sweep_err:
                        logger.warning("Alert queue sweep failed", error=str(sweep_err))

                # Wait for check interval or stop signal
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=SCHEDULER_CHECK_INTERVAL
                    )
                    # If we get here, stop was signaled
                    break
                except asyncio.TimeoutError:
                    # Normal timeout, continue loop
                    pass

            except Exception as e:
                logger.error("Error in scheduler loop", error=str(e))
                await asyncio.sleep(SCHEDULER_CHECK_INTERVAL)

    async def _run_startup_agents(self) -> None:
        """Run all agents with run_on_startup=True."""
        enabled = self._enabled_map()
        startup_agents = [
            agent for agent in self._agents.values()
            if agent.schedule.run_on_startup and enabled.get(agent.name, True)
        ]

        if not startup_agents:
            return

        logger.info("Running startup agents", count=len(startup_agents))

        # Run startup agents concurrently
        tasks = [self._run_agent_safe(agent) for agent in startup_agents]
        await asyncio.gather(*tasks)

    async def _check_and_run_agents(self) -> None:
        """Check schedules and run any agents that are due."""
        now = time.time()
        enabled = self._enabled_map()

        for agent in self._agents.values():
            if not enabled.get(agent.name, True):
                continue

            last_run = self._last_run.get(agent.name, 0)
            interval = agent.schedule.interval_seconds

            if now - last_run >= interval:
                await self._run_agent_safe(agent)

    ENABLED_CACHE_TTL_SECONDS: float = 30.0

    def _enabled_map(self) -> Dict[str, bool]:
        """Return a name -> enabled snapshot from agent_registry, cached for
        ENABLED_CACHE_TTL_SECONDS so we don't open (and re-key) a SQLCipher
        session on every 10s scheduler tick.

        Falls back to the last good cache, then {} (all agents treated as
        enabled), if the DB read fails — so a transient DB issue doesn't
        silently stop every agent.
        """
        now = time.monotonic()
        if (
            self._enabled_cache is not None
            and now - self._enabled_cache_ts < self.ENABLED_CACHE_TTL_SECONDS
        ):
            return self._enabled_cache
        try:
            db = SessionLocal()
            try:
                enabled = AgentRegistryRepository(db).get_all()
                self._enabled_cache = enabled
                self._enabled_cache_ts = now
                return enabled
            finally:
                db.close()
        except Exception as e:
            logger.warning("Failed to read agent registry, treating all as enabled", error=str(e))
            return self._enabled_cache if self._enabled_cache is not None else {}

    def invalidate_enabled_cache(self) -> None:
        """Force the next _enabled_map() to re-read the registry. Call after
        any write to agent enabled-state so the change takes effect at the
        next tick instead of waiting out the TTL."""
        self._enabled_cache_ts = 0.0

    def _ensure_agents_registered(self, agent_names: list[str]) -> None:
        """Insert default-enabled rows for any agents not yet in the registry."""
        try:
            db = SessionLocal()
            try:
                AgentRegistryRepository(db).ensure_registered(agent_names)
            finally:
                db.close()
        except Exception as e:
            logger.warning("Failed to ensure agents registered", error=str(e))

    async def _run_agent_safe(self, agent: IJarvisAgent) -> None:
        """Run an agent with error handling and context caching."""
        try:
            logger.debug("Running agent", agent=agent.name)
            start_time = time.time()

            await agent.run()

            # Update last run time
            self._last_run[agent.name] = time.time()

            # Successful run resets the consecutive-failure counter so a
            # transient hiccup doesn't add up to auto-disable over hours.
            with self._context_lock:
                self._consecutive_failures.pop(agent.name, None)

            # Cache context data (thread-safe)
            if agent.include_in_context:
                context = agent.get_context_data()
                with self._context_lock:
                    self._context_cache[agent.name] = context

            # Collect alerts from the agent
            if self._alert_queue is not None:
                try:
                    alerts = agent.get_alerts()
                    for alert in alerts:
                        self._alert_queue.add_alert(alert)
                    if alerts:
                        logger.debug("Collected alerts from agent", agent=agent.name, count=len(alerts))
                except Exception as alert_err:
                    logger.warning("Failed to collect alerts", agent=agent.name, error=str(alert_err))

            elapsed = time.time() - start_time
            logger.debug("Agent run complete", agent=agent.name, elapsed_ms=int(elapsed * 1000))

        except Exception as e:
            logger.error("Agent run failed", agent=agent.name, error=str(e))

            # Cache error state
            with self._context_lock:
                self._context_cache[agent.name] = {
                    "last_error": str(e),
                    "error_time": datetime.now(timezone.utc).isoformat()
                }
                streak = self._consecutive_failures.get(agent.name, 0) + 1
                self._consecutive_failures[agent.name] = streak

            if streak >= self.AUTO_DISABLE_AFTER_FAILURES:
                self._auto_disable_agent(agent.name, last_error=str(e), streak=streak)

    def _auto_disable_agent(self, agent_name: str, *, last_error: str, streak: int) -> None:
        """Trip the registry's enabled flag to 0 after a failure streak.

        Records the reason in `_auto_disabled_reasons` so the snapshot
        service can render a "Auto-disabled after N failures" badge in the
        mobile UI. Counter is cleared so re-enabling restarts the streak
        from zero rather than insta-disabling on the next failure.
        """
        reason = f"Auto-disabled after {streak} consecutive failures: {last_error}"
        try:
            db = SessionLocal()
            try:
                repo = AgentRegistryRepository(db)
                if repo.is_enabled(agent_name):
                    repo.set_enabled(agent_name, False)
                    logger.error(
                        "Agent auto-disabled — repeated failures",
                        agent=agent_name,
                        streak=streak,
                        last_error=last_error,
                    )
                else:
                    # Already disabled (user-flipped). Don't double-log.
                    logger.warning(
                        "Auto-disable triggered on already-disabled agent",
                        agent=agent_name,
                        streak=streak,
                    )
            finally:
                db.close()
        except Exception as repo_err:
            logger.error(
                "Auto-disable DB write failed; agent remains enabled",
                agent=agent_name,
                error=str(repo_err),
            )
            return

        with self._context_lock:
            self._auto_disabled_reasons[agent_name] = reason
            self._consecutive_failures.pop(agent_name, None)

        # Drop the cached enabled-map so the very next tick sees this agent as
        # disabled instead of running it again for up to ENABLED_CACHE_TTL_SECONDS.
        self.invalidate_enabled_cache()

    def get_auto_disabled_reasons(self) -> Dict[str, str]:
        """Snapshot of agents that were auto-disabled this process lifetime.

        Maps agent_name → human-readable reason. Read by the settings
        snapshot service so the mobile UI can render a circuit-breaker
        badge separate from a user-toggled disable.
        """
        with self._context_lock:
            return dict(self._auto_disabled_reasons)

    def get_aggregated_context(self) -> Dict[str, Dict[str, Any]]:
        """Get aggregated context data from all agents.

        Thread-safe - can be called from the main thread.

        Returns:
            Dict mapping agent name to its context data
        """
        with self._context_lock:
            return self._context_cache.copy()

    def run_agent_now(self, agent_name: str) -> bool:
        """Trigger an immediate run of a specific agent.

        Args:
            agent_name: Name of the agent to run

        Returns:
            True if agent was found and run was scheduled, False otherwise
        """
        agent = self._agents.get(agent_name)
        if agent is None:
            logger.warning("Agent not found for immediate run", agent=agent_name)
            return False

        if not self._loop or not self._running:
            logger.warning("Scheduler not running, cannot trigger agent")
            return False

        # Schedule the agent run on the event loop
        asyncio.run_coroutine_threadsafe(
            self._run_agent_safe(agent),
            self._loop
        )
        return True

    def update_agents(self, new_agents: Dict[str, IJarvisAgent]) -> None:
        """Thread-safe replacement of the agent dict.

        Clears context cache entries for agents that were removed.
        Safe to call from any thread (e.g., package install handler).

        Args:
            new_agents: New agent dict to replace the current one
        """
        with self._context_lock:
            removed = set(self._agents.keys()) - set(new_agents.keys())
            for name in removed:
                self._context_cache.pop(name, None)
                logger.info("Removed stale agent context", agent=name)
            self._agents = new_agents

        # Newly installed agents (e.g., from a Pantry package) need registry
        # rows so the mobile snapshot shows them with default-enabled state.
        self._ensure_agents_registered(list(new_agents.keys()))

    def restart(self) -> None:
        """Stop and restart the scheduler for a clean agent reload."""
        self.stop()
        self.start()

    def get_agent_status(self) -> Dict[str, Dict[str, Any]]:
        """Get status information for all agents.

        Returns:
            Dict mapping agent name to status info
        """
        status = {}
        now = time.time()

        for name, agent in self._agents.items():
            last_run = self._last_run.get(name, 0)
            next_run = last_run + agent.schedule.interval_seconds if last_run else 0

            status[name] = {
                "name": name,
                "description": agent.description,
                "interval_seconds": agent.schedule.interval_seconds,
                "last_run": datetime.fromtimestamp(last_run, tz=timezone.utc).isoformat() if last_run else None,
                "next_run": datetime.fromtimestamp(next_run, tz=timezone.utc).isoformat() if next_run else "pending",
                "include_in_context": agent.include_in_context,
            }

        return status


# Singleton accessor
_scheduler_service: Optional[AgentSchedulerService] = None


def get_agent_scheduler_service() -> AgentSchedulerService:
    """Get the global AgentSchedulerService instance.

    Returns:
        Singleton AgentSchedulerService instance
    """
    global _scheduler_service
    if _scheduler_service is None:
        _scheduler_service = AgentSchedulerService()
    return _scheduler_service


def initialize_agent_scheduler() -> AgentSchedulerService:
    """Initialize and start the agent scheduler.

    Call this during application startup.

    Returns:
        The started AgentSchedulerService instance
    """
    service = get_agent_scheduler_service()
    service.start()
    return service
