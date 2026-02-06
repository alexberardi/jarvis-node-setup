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

            self._loop: Optional[asyncio.AbstractEventLoop] = None
            self._thread: Optional[threading.Thread] = None
            self._running_event = threading.Event()  # Thread-safe running flag
            self._stop_event: Optional[asyncio.Event] = None

            self._initialized = True

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
        startup_agents = [
            agent for agent in self._agents.values()
            if agent.schedule.run_on_startup
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

        for agent in self._agents.values():
            last_run = self._last_run.get(agent.name, 0)
            interval = agent.schedule.interval_seconds

            if now - last_run >= interval:
                await self._run_agent_safe(agent)

    async def _run_agent_safe(self, agent: IJarvisAgent) -> None:
        """Run an agent with error handling and context caching."""
        try:
            logger.debug("Running agent", agent=agent.name)
            start_time = time.time()

            await agent.run()

            # Update last run time
            self._last_run[agent.name] = time.time()

            # Cache context data (thread-safe)
            if agent.include_in_context:
                context = agent.get_context_data()
                with self._context_lock:
                    self._context_cache[agent.name] = context

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
