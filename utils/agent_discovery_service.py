"""
Agent discovery service for finding and instantiating IJarvisAgent implementations.

Mirrors the CommandDiscoveryService pattern but adapted for agents:
- Scans agents/ package for IJarvisAgent implementations
- Validates secrets before registering agents
- Provides singleton accessor for use throughout the application
"""

import importlib
import pkgutil
import threading
from typing import Dict, List, Optional

from jarvis_log_client import JarvisLogger

from core.ijarvis_agent import IJarvisAgent

logger = JarvisLogger(service="jarvis-node")


class AgentDiscoveryService:
    """Discovers and manages IJarvisAgent implementations.

    Unlike CommandDiscoveryService, this does not run a background refresh thread.
    Agents are discovered once at startup, and their lifecycle is managed by
    the AgentSchedulerService.

    Thread safety:
        - Uses RLock for reentrant acquisition during discovery
        - All public methods acquire the lock before accessing state
    """

    def __init__(self):
        self._agents_cache: Dict[str, IJarvisAgent] = {}
        self._lock = threading.RLock()  # Use RLock for reentrant acquisition
        self._discovered = False

    def discover_agents(self) -> Dict[str, IJarvisAgent]:
        """Discover all IJarvisAgent implementations in the agents package.

        Agents with missing required secrets are logged but skipped.
        Thread-safe wrapper around _do_discover_agents.

        Returns:
            Dict mapping agent name to agent instance
        """
        with self._lock:
            return self._do_discover_agents()

    def _do_discover_agents(self) -> Dict[str, IJarvisAgent]:
        """Internal discovery implementation. Caller must hold _lock.

        Returns:
            Dict mapping agent name to agent instance
        """
        try:
            import agents
        except ImportError:
            logger.warning("No agents package found, skipping agent discovery")
            return {}

        new_agents: Dict[str, IJarvisAgent] = {}

        for _, module_name, _ in pkgutil.iter_modules(agents.__path__):
            try:
                module = importlib.import_module(f"agents.{module_name}")

                for attr in dir(module):
                    cls = getattr(module, attr)

                    if (
                        isinstance(cls, type)
                        and issubclass(cls, IJarvisAgent)
                        and cls is not IJarvisAgent
                    ):
                        instance = cls()

                        # Validate secrets before registering
                        missing_secrets = instance.validate_secrets()
                        if missing_secrets:
                            logger.warning(
                                "Agent skipped due to missing secrets",
                                agent=instance.name,
                                missing=missing_secrets,
                            )
                            continue

                        new_agents[instance.name] = instance
                        logger.debug("Discovered agent", agent=instance.name)

            except Exception as e:
                logger.error(
                    "Error loading agent module", module=module_name, error=str(e)
                )

        self._agents_cache = new_agents
        self._discovered = True

        logger.info("Agent discovery complete", count=len(new_agents))
        return new_agents

    def get_agent(self, name: str) -> Optional[IJarvisAgent]:
        """Get a specific agent by name.

        Args:
            name: Agent name (e.g., 'home_assistant')

        Returns:
            Agent instance if found, None otherwise
        """
        with self._lock:
            if not self._discovered:
                self._do_discover_agents()
            return self._agents_cache.get(name)

    def get_all_agents(self) -> Dict[str, IJarvisAgent]:
        """Get all discovered agents.

        Returns:
            Dict mapping agent name to agent instance
        """
        with self._lock:
            if not self._discovered:
                self._do_discover_agents()
            return self._agents_cache.copy()

    def get_context_contributing_agents(self) -> List[IJarvisAgent]:
        """Get agents that should inject data into voice request context.

        Filters by include_in_context property.

        Returns:
            List of agents with include_in_context=True
        """
        with self._lock:
            if not self._discovered:
                self._do_discover_agents()
            return [
                agent
                for agent in self._agents_cache.values()
                if agent.include_in_context
            ]

    def refresh(self) -> None:
        """Force a fresh discovery of agents.

        Useful for testing or when new agents are installed.
        """
        self._discovered = False
        self.discover_agents()


# Global singleton instance
_agent_discovery_service: Optional[AgentDiscoveryService] = None


def get_agent_discovery_service() -> AgentDiscoveryService:
    """Get the global AgentDiscoveryService instance.

    Returns:
        Singleton AgentDiscoveryService instance
    """
    global _agent_discovery_service
    if _agent_discovery_service is None:
        _agent_discovery_service = AgentDiscoveryService()
    return _agent_discovery_service
