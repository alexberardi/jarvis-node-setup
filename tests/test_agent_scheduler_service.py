"""
Unit tests for AgentSchedulerService.

Tests lifecycle management, scheduling, and context aggregation.
"""

import asyncio
import time
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.ijarvis_agent import AgentSchedule, IJarvisAgent
from core.ijarvis_secret import JarvisSecret
from services.agent_scheduler_service import (
    AgentSchedulerService,
    get_agent_scheduler_service,
    initialize_agent_scheduler,
)


class MockAgent(IJarvisAgent):
    """Test agent implementation"""

    def __init__(
        self,
        name: str = "mock_agent",
        interval: int = 60,
        run_on_startup: bool = True,
    ):
        self._name = name
        self._interval = interval
        self._run_on_startup = run_on_startup
        self._run_count = 0
        self._context_data: Dict[str, Any] = {"name": name}

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"Mock agent: {self._name}"

    @property
    def schedule(self) -> AgentSchedule:
        return AgentSchedule(
            interval_seconds=self._interval,
            run_on_startup=self._run_on_startup,
        )

    @property
    def required_secrets(self) -> List[JarvisSecret]:
        return []

    async def run(self) -> None:
        self._run_count += 1

    def get_context_data(self) -> Dict[str, Any]:
        return self._context_data


@pytest.fixture
def fresh_scheduler():
    """Create a fresh AgentSchedulerService for each test"""
    # Reset singleton
    AgentSchedulerService._instance = None
    service = AgentSchedulerService()
    yield service
    # Cleanup
    if service._running:
        service.stop()
    AgentSchedulerService._instance = None


class TestAgentSchedulerServiceCreation:
    """Test service instantiation"""

    def test_create_service(self, fresh_scheduler):
        """Service can be created"""
        assert fresh_scheduler is not None
        assert fresh_scheduler._running is False

    def test_singleton_pattern(self):
        """Service follows singleton pattern"""
        AgentSchedulerService._instance = None

        service1 = AgentSchedulerService()
        service2 = AgentSchedulerService()

        assert service1 is service2

        # Cleanup
        AgentSchedulerService._instance = None


class TestStart:
    """Test start() method"""

    def test_start_with_no_agents(self, fresh_scheduler):
        """start() does nothing if no agents discovered"""
        with patch(
            "services.agent_scheduler_service.get_agent_discovery_service"
        ) as mock_discovery:
            mock_discovery.return_value.get_all_agents.return_value = {}

            fresh_scheduler.start()

            # Should not start thread
            assert fresh_scheduler._running is False

    def test_start_with_agents(self, fresh_scheduler):
        """start() starts scheduler thread when agents exist"""
        agent = MockAgent(name="test_agent")

        with patch(
            "services.agent_scheduler_service.get_agent_discovery_service"
        ) as mock_discovery:
            mock_discovery.return_value.get_all_agents.return_value = {"test_agent": agent}

            fresh_scheduler.start()

            # Should start running
            assert fresh_scheduler._running is True
            assert fresh_scheduler._thread is not None
            assert fresh_scheduler._thread.is_alive()

    def test_start_idempotent(self, fresh_scheduler):
        """start() is idempotent - calling twice doesn't break anything"""
        agent = MockAgent(name="test_agent")

        with patch(
            "services.agent_scheduler_service.get_agent_discovery_service"
        ) as mock_discovery:
            mock_discovery.return_value.get_all_agents.return_value = {"test_agent": agent}

            fresh_scheduler.start()
            first_thread = fresh_scheduler._thread

            # Call start again
            fresh_scheduler.start()

            # Should still be same thread
            assert fresh_scheduler._thread is first_thread


class TestStop:
    """Test stop() method"""

    def test_stop_when_not_running(self, fresh_scheduler):
        """stop() does nothing when not running"""
        assert fresh_scheduler._running is False

        # Should not raise
        fresh_scheduler.stop()

        assert fresh_scheduler._running is False

    def test_stop_stops_thread(self, fresh_scheduler):
        """stop() stops the scheduler thread"""
        agent = MockAgent(name="test_agent")

        with patch(
            "services.agent_scheduler_service.get_agent_discovery_service"
        ) as mock_discovery:
            mock_discovery.return_value.get_all_agents.return_value = {"test_agent": agent}

            fresh_scheduler.start()
            assert fresh_scheduler._running is True

            fresh_scheduler.stop()

            assert fresh_scheduler._running is False
            assert fresh_scheduler._loop is None


class TestContextAggregation:
    """Test get_aggregated_context method"""

    def test_get_aggregated_context_empty(self, fresh_scheduler):
        """get_aggregated_context returns empty dict initially"""
        result = fresh_scheduler.get_aggregated_context()

        assert result == {}

    def test_get_aggregated_context_returns_copy(self, fresh_scheduler):
        """get_aggregated_context returns a copy"""
        fresh_scheduler._context_cache = {"agent1": {"key": "value"}}

        result = fresh_scheduler.get_aggregated_context()

        assert result == {"agent1": {"key": "value"}}

        # Modifying result doesn't affect cache
        result["agent2"] = {}
        assert "agent2" not in fresh_scheduler._context_cache

    def test_get_aggregated_context_thread_safe(self, fresh_scheduler):
        """get_aggregated_context is thread-safe"""
        # Pre-populate cache
        fresh_scheduler._context_cache = {
            "agent1": {"data": "test"},
            "agent2": {"data": "test2"},
        }

        # Should not raise even with concurrent access
        import threading

        results = []

        def read_context():
            for _ in range(100):
                result = fresh_scheduler.get_aggregated_context()
                results.append(len(result))

        threads = [threading.Thread(target=read_context) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All reads should see 2 agents
        assert all(r == 2 for r in results)


class TestRunAgentNow:
    """Test run_agent_now method"""

    def test_run_agent_now_not_found(self, fresh_scheduler):
        """run_agent_now returns False if agent not found"""
        fresh_scheduler._agents = {}

        result = fresh_scheduler.run_agent_now("nonexistent")

        assert result is False

    def test_run_agent_now_scheduler_not_running(self, fresh_scheduler):
        """run_agent_now returns False if scheduler not running"""
        agent = MockAgent(name="test_agent")
        fresh_scheduler._agents = {"test_agent": agent}
        fresh_scheduler._running = False

        result = fresh_scheduler.run_agent_now("test_agent")

        assert result is False


class TestGetAgentStatus:
    """Test get_agent_status method"""

    def test_get_agent_status_empty(self, fresh_scheduler):
        """get_agent_status returns empty dict with no agents"""
        fresh_scheduler._agents = {}

        result = fresh_scheduler.get_agent_status()

        assert result == {}

    def test_get_agent_status_with_agents(self, fresh_scheduler):
        """get_agent_status returns status for all agents"""
        agent = MockAgent(name="test_agent", interval=300)
        fresh_scheduler._agents = {"test_agent": agent}

        result = fresh_scheduler.get_agent_status()

        assert "test_agent" in result
        status = result["test_agent"]
        assert status["name"] == "test_agent"
        assert status["interval_seconds"] == 300
        assert status["include_in_context"] is True


class TestInitializeAgentScheduler:
    """Test initialize_agent_scheduler function"""

    def test_initialize_starts_scheduler(self):
        """initialize_agent_scheduler starts the service"""
        # Reset singleton
        AgentSchedulerService._instance = None

        with patch(
            "services.agent_scheduler_service.get_agent_discovery_service"
        ) as mock_discovery:
            mock_discovery.return_value.get_all_agents.return_value = {}

            service = initialize_agent_scheduler()

            assert service is not None

        # Cleanup
        if service._running:
            service.stop()
        AgentSchedulerService._instance = None


class TestGlobalAccessor:
    """Test get_agent_scheduler_service function"""

    def test_returns_singleton(self):
        """get_agent_scheduler_service returns singleton"""
        # Reset singleton
        AgentSchedulerService._instance = None
        import services.agent_scheduler_service as module
        module._scheduler_service = None

        service1 = get_agent_scheduler_service()
        service2 = get_agent_scheduler_service()

        assert service1 is service2

        # Cleanup
        AgentSchedulerService._instance = None
        module._scheduler_service = None
