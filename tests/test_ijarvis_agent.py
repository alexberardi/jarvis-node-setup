"""
Unit tests for IJarvisAgent interface.

Tests the abstract base class, AgentSchedule dataclass, and validate_secrets method.
"""

from typing import Any, Dict, List
from unittest.mock import patch

import pytest

from core.ijarvis_agent import AgentSchedule, IJarvisAgent
from core.ijarvis_secret import JarvisSecret


class TestAgentSchedule:
    """Test AgentSchedule dataclass"""

    def test_create_with_interval(self):
        """AgentSchedule can be created with interval"""
        schedule = AgentSchedule(interval_seconds=300)
        assert schedule.interval_seconds == 300
        assert schedule.run_on_startup is True  # default

    def test_create_with_run_on_startup_false(self):
        """AgentSchedule respects run_on_startup=False"""
        schedule = AgentSchedule(interval_seconds=60, run_on_startup=False)
        assert schedule.interval_seconds == 60
        assert schedule.run_on_startup is False


class MockAgent(IJarvisAgent):
    """Concrete implementation for testing"""

    def __init__(self, name: str = "test_agent", secrets: List[JarvisSecret] = None):
        self._name = name
        self._secrets = secrets or []
        self._context_data: Dict[str, Any] = {}
        self._run_called = False

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "Test agent for unit tests"

    @property
    def schedule(self) -> AgentSchedule:
        return AgentSchedule(interval_seconds=60, run_on_startup=True)

    @property
    def required_secrets(self) -> List[JarvisSecret]:
        return self._secrets

    async def run(self) -> None:
        self._run_called = True

    def get_context_data(self) -> Dict[str, Any]:
        return self._context_data


class TestIJarvisAgentInterface:
    """Test IJarvisAgent abstract class"""

    def test_agent_has_name(self):
        """Agent must implement name property"""
        agent = MockAgent(name="my_agent")
        assert agent.name == "my_agent"

    def test_agent_has_description(self):
        """Agent must implement description property"""
        agent = MockAgent()
        assert "test" in agent.description.lower()

    def test_agent_has_schedule(self):
        """Agent must implement schedule property"""
        agent = MockAgent()
        schedule = agent.schedule
        assert schedule.interval_seconds == 60
        assert schedule.run_on_startup is True

    def test_agent_has_required_secrets(self):
        """Agent must implement required_secrets property"""
        agent = MockAgent()
        assert agent.required_secrets == []

    def test_include_in_context_default_true(self):
        """include_in_context defaults to True"""
        agent = MockAgent()
        assert agent.include_in_context is True


class TestValidateSecrets:
    """Test validate_secrets method"""

    def test_validate_secrets_empty(self):
        """No secrets required returns empty list"""
        agent = MockAgent(secrets=[])

        missing = agent.validate_secrets()

        assert missing == []

    def test_validate_secrets_all_present(self):
        """All secrets present returns empty list"""
        secrets = [
            JarvisSecret("API_KEY", "API key", "integration", "string"),
            JarvisSecret("API_URL", "API URL", "integration", "string"),
        ]
        agent = MockAgent(secrets=secrets)

        with patch("services.secret_service.get_secret_value") as mock_get:
            mock_get.side_effect = lambda key, scope: f"value_{key}"

            missing = agent.validate_secrets()

            assert missing == []
            assert mock_get.call_count == 2

    def test_validate_secrets_some_missing(self):
        """Missing secrets are returned"""
        secrets = [
            JarvisSecret("API_KEY", "API key", "integration", "string"),
            JarvisSecret("API_URL", "API URL", "integration", "string"),
        ]
        agent = MockAgent(secrets=secrets)

        with patch("services.secret_service.get_secret_value") as mock_get:
            # Only API_KEY is set
            mock_get.side_effect = lambda key, scope: "value" if key == "API_KEY" else None

            missing = agent.validate_secrets()

            assert missing == ["API_URL"]

    def test_validate_secrets_optional_not_missing(self):
        """Optional (required=False) secrets don't count as missing"""
        secrets = [
            JarvisSecret("REQUIRED_KEY", "Required key", "integration", "string", required=True),
            JarvisSecret("OPTIONAL_KEY", "Optional key", "integration", "string", required=False),
        ]
        agent = MockAgent(secrets=secrets)

        with patch("services.secret_service.get_secret_value") as mock_get:
            # Only required is set
            mock_get.side_effect = lambda key, scope: "value" if key == "REQUIRED_KEY" else None

            missing = agent.validate_secrets()

            # Optional key not in missing list
            assert missing == []

    def test_validate_secrets_required_missing(self):
        """Required secrets that are missing are returned"""
        secrets = [
            JarvisSecret("REQUIRED_KEY", "Required key", "integration", "string", required=True),
        ]
        agent = MockAgent(secrets=secrets)

        with patch("services.secret_service.get_secret_value") as mock_get:
            mock_get.return_value = None

            missing = agent.validate_secrets()

            assert missing == ["REQUIRED_KEY"]
