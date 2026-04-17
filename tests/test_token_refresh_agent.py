"""Tests for TokenRefreshAgent."""

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from agents.token_refresh_agent import TokenRefreshAgent
from core.ijarvis_authentication import AuthenticationConfig


@pytest.fixture(autouse=True)
def mock_deferred_modules():
    """Pre-populate sys.modules so deferred imports work in test env."""
    mock_secret = MagicMock()
    mock_secret.get_secret_value = MagicMock(return_value=None)
    mock_secret.set_secret = MagicMock()

    mock_cmd_discovery = MagicMock()
    mock_family_discovery = MagicMock()
    mock_family_discovery.get_device_family_discovery_service.return_value.get_all_families.return_value = {}

    modules = {
        "services.secret_service": mock_secret,
        "db": MagicMock(),
        "utils.command_discovery_service": mock_cmd_discovery,
        "utils.device_family_discovery_service": mock_family_discovery,
    }
    with patch.dict(sys.modules, modules):
        yield {
            "secret_service": mock_secret,
            "cmd_discovery": mock_cmd_discovery,
            "family_discovery": mock_family_discovery,
        }


@pytest.fixture
def mock_secret_service(mock_deferred_modules):
    return mock_deferred_modules["secret_service"]


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
    return cmd


def _make_protocol(auth_config: AuthenticationConfig | None = None) -> MagicMock:
    protocol = MagicMock()
    protocol.authentication = auth_config
    return protocol


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
    def test_missing_expires_at_returns_true(self, agent: TokenRefreshAgent, mock_secret_service):
        mock_secret_service.get_secret_value.return_value = None
        assert agent._needs_refresh("test", 3540) is True

    def test_expired_token_returns_true(self, agent: TokenRefreshAgent, mock_secret_service):
        expired = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        mock_secret_service.get_secret_value.return_value = expired
        assert agent._needs_refresh("test", 3540) is True

    def test_token_within_refresh_window_returns_true(self, agent: TokenRefreshAgent, mock_secret_service):
        almost_expired = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        mock_secret_service.get_secret_value.return_value = almost_expired
        assert agent._needs_refresh("test", 3540) is True

    def test_fresh_token_returns_false(self, agent: TokenRefreshAgent, mock_secret_service):
        fresh = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        mock_secret_service.get_secret_value.return_value = fresh
        assert agent._needs_refresh("test", 3540) is False

    def test_invalid_expires_at_returns_true(self, agent: TokenRefreshAgent, mock_secret_service):
        mock_secret_service.get_secret_value.return_value = "not-a-date"
        assert agent._needs_refresh("test", 3540) is True


class TestDoRefresh:
    def test_missing_refresh_token_secret_key(self, agent: TokenRefreshAgent):
        auth = AuthenticationConfig(
            type="oauth", provider="p", friendly_name="P", client_id="c",
            keys=["access_token"], exchange_url="https://example.com/token",
            requires_background_refresh=True, refresh_token_secret_key=None,
        )
        assert agent._do_refresh(auth, MagicMock()) is False

    def test_missing_exchange_url(self, agent: TokenRefreshAgent):
        auth = AuthenticationConfig(
            type="oauth", provider="p", friendly_name="P", client_id="c",
            keys=["access_token"], exchange_url=None,
            requires_background_refresh=True, refresh_token_secret_key="REFRESH",
        )
        assert agent._do_refresh(auth, MagicMock()) is False

    def test_no_stored_refresh_token(self, agent: TokenRefreshAgent, mock_auth_config, mock_secret_service):
        mock_secret_service.get_secret_value.return_value = None
        assert agent._do_refresh(mock_auth_config, MagicMock()) is False

    def test_successful_refresh(self, agent: TokenRefreshAgent, mock_auth_config, mock_secret_service):
        source = MagicMock()
        mock_secret_service.get_secret_value.return_value = "old-refresh"

        response_data = json.dumps({
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_in": 3600,
        }).encode()

        mock_resp = MagicMock()
        mock_resp.read.return_value = response_data
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("agents.token_refresh_agent.urlopen", return_value=mock_resp):
            assert agent._do_refresh(mock_auth_config, source) is True

        source.store_auth_values.assert_called_once_with({
            "access_token": "new-access",
            "refresh_token": "new-refresh",
        })
        mock_secret_service.set_secret.assert_called_once()

    def test_refresh_without_new_refresh_token(self, agent: TokenRefreshAgent, mock_auth_config, mock_secret_service):
        source = MagicMock()
        mock_secret_service.get_secret_value.return_value = "old-refresh"

        response_data = json.dumps({
            "access_token": "new-access",
            "expires_in": 3600,
        }).encode()

        mock_resp = MagicMock()
        mock_resp.read.return_value = response_data
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("agents.token_refresh_agent.urlopen", return_value=mock_resp):
            assert agent._do_refresh(mock_auth_config, source) is True

        source.store_auth_values.assert_called_once_with({"access_token": "new-access"})

    def test_http_error_returns_false(self, agent: TokenRefreshAgent, mock_auth_config, mock_secret_service):
        mock_secret_service.get_secret_value.return_value = "old-refresh"
        with patch("agents.token_refresh_agent.urlopen", side_effect=Exception("timeout")):
            assert agent._do_refresh(mock_auth_config, MagicMock()) is False


class TestRun:
    def test_skips_commands_without_auth(self, agent: TokenRefreshAgent):
        cmd_no_auth = _make_command(auth_config=None)

        with (
            patch("utils.command_discovery_service.get_command_discovery_service") as mock_discovery,
            patch("utils.device_family_discovery_service.get_device_family_discovery_service") as mock_families,
        ):
            mock_discovery.return_value.get_all_commands.return_value = {"no_auth": cmd_no_auth}
            mock_families.return_value.get_all_families.return_value = {}
            asyncio.get_event_loop().run_until_complete(agent.run())

        assert agent._last_results == {}

    def test_refreshes_expired_command_token(self, agent: TokenRefreshAgent, mock_auth_config):
        cmd = _make_command(auth_config=mock_auth_config)

        with (
            patch("utils.command_discovery_service.get_command_discovery_service") as mock_discovery,
            patch("utils.device_family_discovery_service.get_device_family_discovery_service") as mock_families,
            patch.object(agent, "_needs_refresh", return_value=True),
            patch.object(agent, "_do_refresh", return_value=True),
        ):
            mock_discovery.return_value.get_all_commands.return_value = {"cmd": cmd}
            mock_families.return_value.get_all_families.return_value = {}
            asyncio.get_event_loop().run_until_complete(agent.run())

        assert agent._last_results["test_provider"] == "ok"

    def test_refreshes_expired_protocol_token(self, agent: TokenRefreshAgent, mock_auth_config):
        """Device protocols (like Nest) should also get their tokens refreshed."""
        protocol = _make_protocol(auth_config=mock_auth_config)

        with (
            patch("utils.command_discovery_service.get_command_discovery_service") as mock_discovery,
            patch("utils.device_family_discovery_service.get_device_family_discovery_service") as mock_families,
            patch.object(agent, "_needs_refresh", return_value=True),
            patch.object(agent, "_do_refresh", return_value=True) as mock_refresh,
        ):
            mock_discovery.return_value.get_all_commands.return_value = {}
            mock_families.return_value.get_all_families.return_value = {"nest": protocol}
            asyncio.get_event_loop().run_until_complete(agent.run())

        mock_refresh.assert_called_once_with(mock_auth_config, protocol)
        assert agent._last_results["test_provider"] == "ok"

    def test_skips_fresh_token(self, agent: TokenRefreshAgent, mock_auth_config):
        cmd = _make_command(auth_config=mock_auth_config)

        with (
            patch("utils.command_discovery_service.get_command_discovery_service") as mock_discovery,
            patch("utils.device_family_discovery_service.get_device_family_discovery_service") as mock_families,
            patch.object(agent, "_needs_refresh", return_value=False),
        ):
            mock_discovery.return_value.get_all_commands.return_value = {"cmd": cmd}
            mock_families.return_value.get_all_families.return_value = {}
            asyncio.get_event_loop().run_until_complete(agent.run())

        assert agent._last_results["test_provider"] == "fresh"

    def test_deduplicates_by_provider(self, agent: TokenRefreshAgent, mock_auth_config):
        """Command and protocol sharing the same provider should only refresh once."""
        cmd = _make_command(auth_config=mock_auth_config)
        protocol = _make_protocol(auth_config=mock_auth_config)

        with (
            patch("utils.command_discovery_service.get_command_discovery_service") as mock_discovery,
            patch("utils.device_family_discovery_service.get_device_family_discovery_service") as mock_families,
            patch.object(agent, "_needs_refresh", return_value=True),
            patch.object(agent, "_do_refresh", return_value=True) as mock_refresh,
        ):
            mock_discovery.return_value.get_all_commands.return_value = {"cmd": cmd}
            mock_families.return_value.get_all_families.return_value = {"nest": protocol}
            asyncio.get_event_loop().run_until_complete(agent.run())

        assert mock_refresh.call_count == 1

    def test_error_isolation(self, agent: TokenRefreshAgent):
        """One provider failing should not block others."""
        auth_a = AuthenticationConfig(
            type="oauth", provider="provider_a", friendly_name="A",
            client_id="id", keys=["access_token"],
            requires_background_refresh=True, refresh_token_secret_key="A_REFRESH",
        )
        auth_b = AuthenticationConfig(
            type="oauth", provider="provider_b", friendly_name="B",
            client_id="id", keys=["access_token"],
            requires_background_refresh=True, refresh_token_secret_key="B_REFRESH",
        )

        cmd_a = _make_command(auth_config=auth_a)
        cmd_b = _make_command(auth_config=auth_b)

        def mock_do_refresh(auth, source):
            if auth.provider == "provider_a":
                raise RuntimeError("Network error")
            return True

        with (
            patch("utils.command_discovery_service.get_command_discovery_service") as mock_discovery,
            patch("utils.device_family_discovery_service.get_device_family_discovery_service") as mock_families,
            patch.object(agent, "_needs_refresh", return_value=True),
            patch.object(agent, "_do_refresh", side_effect=mock_do_refresh),
        ):
            mock_discovery.return_value.get_all_commands.return_value = {"a": cmd_a, "b": cmd_b}
            mock_families.return_value.get_all_families.return_value = {}
            asyncio.get_event_loop().run_until_complete(agent.run())

        assert "error" in agent._last_results["provider_a"]
        assert agent._last_results["provider_b"] == "ok"

    def test_failed_refresh_tracked(self, agent: TokenRefreshAgent, mock_auth_config):
        cmd = _make_command(auth_config=mock_auth_config)

        with (
            patch("utils.command_discovery_service.get_command_discovery_service") as mock_discovery,
            patch("utils.device_family_discovery_service.get_device_family_discovery_service") as mock_families,
            patch.object(agent, "_needs_refresh", return_value=True),
            patch.object(agent, "_do_refresh", return_value=False),
        ):
            mock_discovery.return_value.get_all_commands.return_value = {"cmd": cmd}
            mock_families.return_value.get_all_families.return_value = {}
            asyncio.get_event_loop().run_until_complete(agent.run())

        assert agent._last_results["test_provider"] == "failed"
