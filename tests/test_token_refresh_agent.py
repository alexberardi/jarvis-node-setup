"""Tests for TokenRefreshAgent."""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from agents.token_refresh_agent import TokenRefreshAgent
from core.ijarvis_authentication import AuthenticationConfig


@pytest.fixture
def agent():
    return TokenRefreshAgent()


@pytest.fixture
def mock_auth_config():
    return AuthenticationConfig(
        type="oauth",
        provider="test_provider",
        friendly_name="Test Provider",
        client_id="test-client-id",
        keys=["access_token", "refresh_token"],
        exchange_url="https://example.com/token",
        requires_background_refresh=True,
        refresh_interval_seconds=3540,
        refresh_token_secret_key="TEST_REFRESH_TOKEN",
    )


def _make_command(auth_config: AuthenticationConfig | None = None) -> MagicMock:
    cmd = MagicMock()
    cmd.authentication = auth_config
    cmd.refresh_token.return_value = True
    return cmd


class TestTokenRefreshAgentProperties:
    def test_name(self, agent: TokenRefreshAgent):
        assert agent.name == "token_refresh"

    def test_schedule(self, agent: TokenRefreshAgent):
        assert agent.schedule.interval_seconds == 300
        assert agent.schedule.run_on_startup is True

    def test_include_in_context(self, agent: TokenRefreshAgent):
        assert agent.include_in_context is False

    def test_validate_secrets_empty(self, agent: TokenRefreshAgent):
        assert agent.validate_secrets() == []

    def test_get_context_data_empty(self, agent: TokenRefreshAgent):
        assert agent.get_context_data() == {}


class TestNeedsRefresh:
    def test_missing_expires_at_returns_true(self, agent: TokenRefreshAgent):
        with patch("services.secret_service.get_secret_value", return_value=None):
            assert agent._needs_refresh("test", 3540) is True

    def test_expired_token_returns_true(self, agent: TokenRefreshAgent):
        expired = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        with patch("services.secret_service.get_secret_value", return_value=expired):
            assert agent._needs_refresh("test", 3540) is True

    def test_token_within_refresh_window_returns_true(self, agent: TokenRefreshAgent):
        # Expires in 30 min, refresh window is 59 min
        almost_expired = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        with patch("services.secret_service.get_secret_value", return_value=almost_expired):
            assert agent._needs_refresh("test", 3540) is True

    def test_fresh_token_returns_false(self, agent: TokenRefreshAgent):
        # Expires in 2 hours, refresh window is 59 min
        fresh = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        with patch("services.secret_service.get_secret_value", return_value=fresh):
            assert agent._needs_refresh("test", 3540) is False

    def test_invalid_expires_at_returns_true(self, agent: TokenRefreshAgent):
        with patch("services.secret_service.get_secret_value", return_value="not-a-date"):
            assert agent._needs_refresh("test", 3540) is True


class TestRun:
    def test_skips_commands_without_auth(self, agent: TokenRefreshAgent):
        cmd_no_auth = _make_command(auth_config=None)
        commands = {"no_auth": cmd_no_auth}

        with patch(
            "utils.command_discovery_service.get_command_discovery_service"
        ) as mock_discovery:
            mock_discovery.return_value.get_all_commands.return_value = commands
            asyncio.get_event_loop().run_until_complete(agent.run())

        cmd_no_auth.refresh_token.assert_not_called()

    def test_skips_commands_without_background_refresh(self, agent: TokenRefreshAgent):
        auth = AuthenticationConfig(
            type="oauth",
            provider="no_refresh",
            friendly_name="No Refresh",
            client_id="id",
            keys=["access_token"],
            requires_background_refresh=False,
        )
        cmd = _make_command(auth_config=auth)
        commands = {"cmd": cmd}

        with patch(
            "utils.command_discovery_service.get_command_discovery_service"
        ) as mock_discovery:
            mock_discovery.return_value.get_all_commands.return_value = commands
            asyncio.get_event_loop().run_until_complete(agent.run())

        cmd.refresh_token.assert_not_called()

    def test_refreshes_expired_token(self, agent: TokenRefreshAgent, mock_auth_config):
        cmd = _make_command(auth_config=mock_auth_config)
        commands = {"cmd": cmd}

        with (
            patch(
                "utils.command_discovery_service.get_command_discovery_service"
            ) as mock_discovery,
            patch.object(agent, "_needs_refresh", return_value=True),
        ):
            mock_discovery.return_value.get_all_commands.return_value = commands
            asyncio.get_event_loop().run_until_complete(agent.run())

        cmd.refresh_token.assert_called_once()
        assert agent._last_results["test_provider"] == "ok"

    def test_skips_fresh_token(self, agent: TokenRefreshAgent, mock_auth_config):
        cmd = _make_command(auth_config=mock_auth_config)
        commands = {"cmd": cmd}

        with (
            patch(
                "utils.command_discovery_service.get_command_discovery_service"
            ) as mock_discovery,
            patch.object(agent, "_needs_refresh", return_value=False),
        ):
            mock_discovery.return_value.get_all_commands.return_value = commands
            asyncio.get_event_loop().run_until_complete(agent.run())

        cmd.refresh_token.assert_not_called()
        assert agent._last_results["test_provider"] == "fresh"

    def test_deduplicates_by_provider(self, agent: TokenRefreshAgent, mock_auth_config):
        cmd1 = _make_command(auth_config=mock_auth_config)
        cmd2 = _make_command(auth_config=mock_auth_config)
        commands = {"cmd1": cmd1, "cmd2": cmd2}

        with (
            patch(
                "utils.command_discovery_service.get_command_discovery_service"
            ) as mock_discovery,
            patch.object(agent, "_needs_refresh", return_value=True),
        ):
            mock_discovery.return_value.get_all_commands.return_value = commands
            asyncio.get_event_loop().run_until_complete(agent.run())

        # Only one of them should have been called
        total_calls = cmd1.refresh_token.call_count + cmd2.refresh_token.call_count
        assert total_calls == 1

    def test_error_isolation(self, agent: TokenRefreshAgent):
        """One provider failing should not block others."""
        auth_a = AuthenticationConfig(
            type="oauth",
            provider="provider_a",
            friendly_name="A",
            client_id="id",
            keys=["access_token"],
            requires_background_refresh=True,
            refresh_token_secret_key="A_REFRESH",
        )
        auth_b = AuthenticationConfig(
            type="oauth",
            provider="provider_b",
            friendly_name="B",
            client_id="id",
            keys=["access_token"],
            requires_background_refresh=True,
            refresh_token_secret_key="B_REFRESH",
        )

        cmd_a = _make_command(auth_config=auth_a)
        cmd_a.refresh_token.side_effect = RuntimeError("Network error")

        cmd_b = _make_command(auth_config=auth_b)
        cmd_b.refresh_token.return_value = True

        commands = {"a": cmd_a, "b": cmd_b}

        with (
            patch(
                "utils.command_discovery_service.get_command_discovery_service"
            ) as mock_discovery,
            patch.object(agent, "_needs_refresh", return_value=True),
        ):
            mock_discovery.return_value.get_all_commands.return_value = commands
            asyncio.get_event_loop().run_until_complete(agent.run())

        # Provider B should still have been refreshed despite A failing
        cmd_b.refresh_token.assert_called_once()
        assert "error" in agent._last_results["provider_a"]
        assert agent._last_results["provider_b"] == "ok"

    def test_failed_refresh_tracked(self, agent: TokenRefreshAgent, mock_auth_config):
        cmd = _make_command(auth_config=mock_auth_config)
        cmd.refresh_token.return_value = False
        commands = {"cmd": cmd}

        with (
            patch(
                "utils.command_discovery_service.get_command_discovery_service"
            ) as mock_discovery,
            patch.object(agent, "_needs_refresh", return_value=True),
        ):
            mock_discovery.return_value.get_all_commands.return_value = commands
            asyncio.get_event_loop().run_until_complete(agent.run())

        assert agent._last_results["test_provider"] == "failed"
