"""
IJarvisAgent - Abstract base class for background agents.

Agents are background tasks that collect data proactively and inject it into
voice request context. Unlike commands (triggered by voice), agents run on
a schedule and cache their results for later use.

Example use cases:
- Home Assistant: Pre-fetch device/area data for voice control
- Calendar: Sync upcoming events for proactive reminders
- Package tracking: Watch for delivery updates
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List

from core.ijarvis_secret import IJarvisSecret


@dataclass
class AgentSchedule:
    """Schedule configuration for an agent.

    Attributes:
        interval_seconds: Minimum interval between runs
        run_on_startup: Whether to run immediately when scheduler starts
    """
    interval_seconds: int
    run_on_startup: bool = True


class IJarvisAgent(ABC):
    """Abstract base class for background agents.

    Agents run in the background on a schedule, collecting data and caching it
    for injection into voice request context. This allows commands to have
    access to pre-fetched data without blocking on API calls.

    Lifecycle:
        1. Agent is discovered and instantiated by AgentDiscoveryService
        2. AgentSchedulerService validates secrets and starts scheduling
        3. run() is called on schedule, caching results internally
        4. get_context_data() is called at request time to inject into context

    Implementation notes:
        - run() should be idempotent and handle connection failures gracefully
        - get_context_data() should be fast (just return cached data)
        - Cache last_error for debugging but don't let errors crash the scheduler
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique identifier for this agent (e.g., 'home_assistant').

        Used as the key in aggregated context and for logging.
        """
        pass

    @property
    @abstractmethod
    def description(self) -> str:
        """Human-readable description of what this agent does."""
        pass

    @property
    @abstractmethod
    def schedule(self) -> AgentSchedule:
        """When this agent should run.

        Returns:
            AgentSchedule with interval and startup behavior
        """
        pass

    @property
    @abstractmethod
    def required_secrets(self) -> List[IJarvisSecret]:
        """Secrets this agent needs to function.

        Returns:
            List of IJarvisSecret definitions
        """
        pass

    @abstractmethod
    async def run(self) -> None:
        """Execute the background task.

        This method should:
        - Fetch data from external sources
        - Cache results internally for get_context_data()
        - Handle errors gracefully (log, set last_error, don't raise)
        - Be idempotent (safe to call multiple times)

        Raises:
            Should not raise exceptions - catch and cache as last_error
        """
        pass

    @property
    def include_in_context(self) -> bool:
        """Whether to include this agent's data in voice request context.

        Override to return False for agents that collect data for internal
        use only (e.g., triggering proactive notifications).

        Returns:
            True if get_context_data() should be included in node_context
        """
        return True

    @abstractmethod
    def get_context_data(self) -> Dict[str, Any]:
        """Return cached data for voice request context.

        This is called synchronously at request time, so it should just
        return the cached data from the last run() invocation.

        Returns:
            Dict with agent-specific data to include in context.
            Should include 'last_error' key if the last run failed.
        """
        pass

    def validate_secrets(self) -> List[str]:
        """Check which required secrets are missing.

        Returns:
            List of missing secret keys (empty if all present)
        """
        from services.secret_service import get_secret_value

        missing = []
        for secret in self.required_secrets:
            if secret.required and not get_secret_value(secret.key, secret.scope):
                missing.append(secret.key)
        return missing
