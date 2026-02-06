"""
Unit tests for AgentDiscoveryService.

Tests agent discovery, secret validation, and singleton pattern.
"""

from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

from core.ijarvis_agent import AgentSchedule, IJarvisAgent
from core.ijarvis_secret import JarvisSecret
from utils.agent_discovery_service import (
    AgentDiscoveryService,
    get_agent_discovery_service,
)


class MockAgent(IJarvisAgent):
    """Test agent implementation"""

    def __init__(self, name: str = "mock_agent", secrets: List[JarvisSecret] = None):
        self._name = name
        self._secrets = secrets or []

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"Mock agent: {self._name}"

    @property
    def schedule(self) -> AgentSchedule:
        return AgentSchedule(interval_seconds=60)

    @property
    def required_secrets(self) -> List[JarvisSecret]:
        return self._secrets

    async def run(self) -> None:
        pass

    def get_context_data(self) -> Dict[str, Any]:
        return {"name": self._name}


class MockAgentNoContext(MockAgent):
    """Agent that doesn't contribute to context"""

    @property
    def include_in_context(self) -> bool:
        return False


@pytest.fixture
def fresh_service():
    """Create a fresh AgentDiscoveryService for each test"""
    return AgentDiscoveryService()


class TestAgentDiscoveryServiceCreation:
    """Test service instantiation"""

    def test_create_service(self, fresh_service):
        """Service can be created"""
        assert fresh_service is not None
        assert fresh_service._discovered is False

    def test_cache_initially_empty(self, fresh_service):
        """Agents cache is empty before discovery"""
        assert fresh_service._agents_cache == {}


class TestAgentDiscovery:
    """Test agent discovery"""

    def test_discover_no_agents_package(self, fresh_service):
        """No agents package returns empty dict"""
        with patch.dict("sys.modules", {"agents": None}):
            with patch("utils.agent_discovery_service.importlib") as mock_import:
                # Make import fail for agents package
                mock_import.import_module.side_effect = ImportError("No module")

                result = fresh_service.discover_agents()

                assert result == {}

    def test_discover_finds_agents(self, fresh_service):
        """Discovery finds IJarvisAgent implementations"""
        mock_module = MagicMock()
        mock_module.MockAgent = MockAgent

        with patch("pkgutil.iter_modules") as mock_iter:
            mock_iter.return_value = [(None, "mock_agent_module", None)]

            with patch("importlib.import_module") as mock_import:
                mock_import.return_value = mock_module

                with patch.object(MockAgent, "validate_secrets", return_value=[]):
                    result = fresh_service.discover_agents()

                    assert "mock_agent" in result
                    assert isinstance(result["mock_agent"], MockAgent)

    def test_discover_skips_missing_secrets(self, fresh_service):
        """Agents with missing secrets are skipped"""
        secrets = [JarvisSecret("MISSING_KEY", "Key", "integration", "string")]
        agent_with_secrets = MockAgent(name="secret_agent", secrets=secrets)

        mock_module = MagicMock()
        mock_module.AgentWithSecrets = type(agent_with_secrets)

        with patch("pkgutil.iter_modules") as mock_iter:
            mock_iter.return_value = [(None, "secret_agent_module", None)]

            with patch("importlib.import_module") as mock_import:
                mock_import.return_value = mock_module

                # validate_secrets returns missing key
                with patch.object(
                    type(agent_with_secrets),
                    "validate_secrets",
                    return_value=["MISSING_KEY"],
                ):
                    result = fresh_service.discover_agents()

                    # Agent should be skipped
                    assert len(result) == 0


class TestGetAgent:
    """Test get_agent method"""

    def test_get_agent_found(self, fresh_service):
        """get_agent returns agent if found"""
        agent = MockAgent(name="test_agent")
        fresh_service._agents_cache = {"test_agent": agent}
        fresh_service._discovered = True

        result = fresh_service.get_agent("test_agent")

        assert result is agent

    def test_get_agent_not_found(self, fresh_service):
        """get_agent returns None if not found"""
        fresh_service._agents_cache = {}
        fresh_service._discovered = True

        result = fresh_service.get_agent("nonexistent")

        assert result is None

    def test_get_agent_triggers_discovery(self, fresh_service):
        """get_agent triggers discovery if not yet discovered"""
        assert fresh_service._discovered is False

        with patch.object(fresh_service, "_do_discover_agents") as mock_discover:
            mock_discover.return_value = {}
            fresh_service.get_agent("any")

            mock_discover.assert_called_once()


class TestGetAllAgents:
    """Test get_all_agents method"""

    def test_get_all_agents(self, fresh_service):
        """get_all_agents returns copy of cache"""
        agent1 = MockAgent(name="agent1")
        agent2 = MockAgent(name="agent2")
        fresh_service._agents_cache = {"agent1": agent1, "agent2": agent2}
        fresh_service._discovered = True

        result = fresh_service.get_all_agents()

        assert len(result) == 2
        assert result["agent1"] is agent1
        assert result["agent2"] is agent2

        # Modifying result doesn't affect cache
        result["agent3"] = MockAgent(name="agent3")
        assert "agent3" not in fresh_service._agents_cache


class TestGetContextContributingAgents:
    """Test get_context_contributing_agents method"""

    def test_filters_by_include_in_context(self, fresh_service):
        """Returns only agents with include_in_context=True"""
        agent_with_context = MockAgent(name="context_agent")
        agent_no_context = MockAgentNoContext(name="no_context_agent")

        fresh_service._agents_cache = {
            "context_agent": agent_with_context,
            "no_context_agent": agent_no_context,
        }
        fresh_service._discovered = True

        result = fresh_service.get_context_contributing_agents()

        assert len(result) == 1
        assert result[0].name == "context_agent"


class TestSingleton:
    """Test singleton pattern"""

    def test_get_agent_discovery_service_returns_same_instance(self):
        """get_agent_discovery_service returns singleton"""
        # Clear global
        import utils.agent_discovery_service as module
        module._agent_discovery_service = None

        service1 = get_agent_discovery_service()
        service2 = get_agent_discovery_service()

        assert service1 is service2

        # Cleanup
        module._agent_discovery_service = None


class TestRefresh:
    """Test refresh method"""

    def test_refresh_resets_discovered_flag(self, fresh_service):
        """refresh() resets discovered flag and rediscovers"""
        fresh_service._discovered = True

        with patch.object(fresh_service, "discover_agents") as mock_discover:
            mock_discover.return_value = {}
            fresh_service.refresh()

            mock_discover.assert_called_once()
