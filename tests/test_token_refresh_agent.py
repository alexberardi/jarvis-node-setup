"""Tests for TokenRefreshAgent."""

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import httpx
import pytest
from jarvis_command_sdk import IJarvisCommand, TokenBundle

from agents.token_refresh_agent import TokenRefreshAgent
from core.ijarvis_authentication import AuthenticationConfig


@pytest.fixture(autouse=True)
def mock_deferred_modules():
    """Pre-populate sys.modules so deferred imports work in test env."""
    mock_secret = MagicMock()
    mock_secret.get_secret_value = MagicMock(return_value=None)
    mock_secret.set_secret = MagicMock()

    mock_cmd_discovery = MagicMock()
    mock_auth_service = MagicMock()
    mock_family_discovery = MagicMock()
    mock_family_discovery.get_device_family_discovery_service.return_value.get_all_families.return_value = {}

    modules = {
        "services.secret_service": mock_secret,
        "db": MagicMock(),
        "services.command_auth_service": mock_auth_service,
        "utils.command_discovery_service": mock_cmd_discovery,
        "utils.device_family_discovery_service": mock_family_discovery,
    }
    with patch.dict(sys.modules, modules):
        yield {
            "secret_service": mock_secret,
            "cmd_discovery": mock_cmd_discovery,
            "family_discovery": mock_family_discovery,
            "auth_service": mock_auth_service,
        }


@pytest.fixture
def mock_secret_service(mock_deferred_modules):
    return mock_deferred_modules["secret_service"]


@pytest.fixture
def mock_auth_service(mock_deferred_modules):
    return mock_deferred_modules["auth_service"]


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

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_in": 3600,
        }
        mock_client = MagicMock()
        mock_client.post.return_value = mock_resp

        with patch.object(agent, "_http_client", return_value=mock_client):
            assert agent._do_refresh(mock_auth_config, source) is True

        source.store_auth_values.assert_called_once_with({
            "access_token": "new-access",
            "refresh_token": "new-refresh",
        })
        mock_secret_service.set_secret.assert_called_once()

    def test_refresh_without_new_refresh_token(self, agent: TokenRefreshAgent, mock_auth_config, mock_secret_service):
        source = MagicMock()
        mock_secret_service.get_secret_value.return_value = "old-refresh"

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "access_token": "new-access",
            "expires_in": 3600,
        }
        mock_client = MagicMock()
        mock_client.post.return_value = mock_resp

        with patch.object(agent, "_http_client", return_value=mock_client):
            assert agent._do_refresh(mock_auth_config, source) is True

        source.store_auth_values.assert_called_once_with({"access_token": "new-access"})

    def test_http_error_returns_false(self, agent: TokenRefreshAgent, mock_auth_config, mock_secret_service):
        mock_secret_service.get_secret_value.return_value = "old-refresh"
        mock_client = MagicMock()
        mock_client.post.side_effect = Exception("timeout")
        with patch.object(agent, "_http_client", return_value=mock_client):
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


class TestPerUserRefresh:
    """User-scoped refresh tokens (e.g. per-user Google calendars) are
    refreshed once per owning user, with the SDK ContextVar set so
    store_auth_values resolves that user's rows. Integration-scoped
    providers (the default; e.g. household Spotify) keep the legacy
    single-refresh path."""

    def _user_scoped_command(self, auth):
        from types import SimpleNamespace

        cmd = MagicMock()
        cmd.authentication = auth
        cmd.all_possible_secrets = [
            SimpleNamespace(key="TEST_REFRESH_TOKEN", scope="user"),
        ]
        return cmd

    def test_scope_from_declared_secrets(self, agent: TokenRefreshAgent, mock_auth_config):
        assert agent._refresh_token_scope(mock_auth_config, self._user_scoped_command(mock_auth_config)) == "user"
        # Plain MagicMock iterates empty -> integration default (legacy sources).
        assert agent._refresh_token_scope(mock_auth_config, MagicMock()) == "integration"

    def test_run_refreshes_each_token_owner(self, agent: TokenRefreshAgent, mock_auth_config, mock_secret_service):
        from types import SimpleNamespace

        from jarvis_command_sdk import get_current_user_id

        cmd = self._user_scoped_command(mock_auth_config)
        seen: list[tuple[int | None, dict]] = []
        cmd.store_auth_values.side_effect = lambda values: seen.append((get_current_user_id(), values))

        mock_secret_service.get_all_secrets = MagicMock(return_value=[
            SimpleNamespace(key="TEST_REFRESH_TOKEN", user_id=1, value="rt1"),
            SimpleNamespace(key="TEST_REFRESH_TOKEN", user_id=2, value="rt2"),
            SimpleNamespace(key="OTHER", user_id=3, value="x"),
        ])

        def get_value(key, scope, user_id=None):
            if key == "TEST_REFRESH_TOKEN" and scope == "user":
                return {1: "rt1", 2: "rt2"}.get(user_id)
            return None  # no expiry recorded -> needs refresh

        mock_secret_service.get_secret_value.side_effect = get_value

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"access_token": "new-access", "expires_in": 3600}
        mock_client = MagicMock()
        mock_client.post.return_value = mock_resp

        with (
            patch("utils.command_discovery_service.get_command_discovery_service") as mock_discovery,
            patch("utils.device_family_discovery_service.get_device_family_discovery_service") as mock_families,
            patch.object(agent, "_http_client", return_value=mock_client),
        ):
            mock_discovery.return_value.get_all_commands.return_value = {"cmd": cmd}
            mock_families.return_value.get_all_families.return_value = {}
            asyncio.get_event_loop().run_until_complete(agent.run())

        # Both owners refreshed, each with THEIR identity ambient during the store.
        assert seen == [
            (1, {"access_token": "new-access"}),
            (2, {"access_token": "new-access"}),
        ]
        assert get_current_user_id() is None  # cleared afterwards
        assert agent._last_results["test_provider"] == "u1:ok, u2:ok"

        # Expiry recorded per user, user-scoped.
        expiry_calls = mock_secret_service.set_secret.call_args_list
        assert [(c.args[2], c.kwargs.get("user_id")) for c in expiry_calls] == [
            ("user", 1), ("user", 2),
        ]

    def test_run_user_scoped_without_user_rows_falls_back_to_legacy(
        self, agent: TokenRefreshAgent, mock_auth_config, mock_secret_service
    ):
        """Pre-threading installs hold tokens in one integration row — those
        must keep refreshing until the user re-runs OAuth."""
        from jarvis_command_sdk import get_current_user_id

        cmd = self._user_scoped_command(mock_auth_config)
        seen: list = []
        cmd.store_auth_values.side_effect = lambda values: seen.append(get_current_user_id())
        mock_secret_service.get_all_secrets = MagicMock(return_value=[])

        def get_value(key, scope, user_id=None):
            if key == "TEST_REFRESH_TOKEN" and scope == "integration":
                return "legacy-rt"
            return None  # no expiry recorded -> needs refresh

        mock_secret_service.get_secret_value.side_effect = get_value

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"access_token": "new-access", "expires_in": 3600}
        mock_client = MagicMock()
        mock_client.post.return_value = mock_resp

        with (
            patch("utils.command_discovery_service.get_command_discovery_service") as mock_discovery,
            patch("utils.device_family_discovery_service.get_device_family_discovery_service") as mock_families,
            patch.object(agent, "_http_client", return_value=mock_client),
        ):
            mock_discovery.return_value.get_all_commands.return_value = {"cmd": cmd}
            mock_families.return_value.get_all_families.return_value = {}
            asyncio.get_event_loop().run_until_complete(agent.run())

        assert agent._last_results["test_provider"] == "legacy:ok"
        assert seen == [None]  # legacy refresh runs with no user context

    def test_one_owner_failure_does_not_block_others(self, agent: TokenRefreshAgent, mock_auth_config, mock_secret_service):
        from types import SimpleNamespace

        cmd = self._user_scoped_command(mock_auth_config)
        mock_secret_service.get_all_secrets = MagicMock(return_value=[
            SimpleNamespace(key="TEST_REFRESH_TOKEN", user_id=1, value="rt1"),
            SimpleNamespace(key="TEST_REFRESH_TOKEN", user_id=2, value="rt2"),
        ])

        def do_refresh(auth, source, user_id=None):
            if user_id == 1:
                raise RuntimeError("provider 500")
            return True

        with (
            patch("utils.command_discovery_service.get_command_discovery_service") as mock_discovery,
            patch("utils.device_family_discovery_service.get_device_family_discovery_service") as mock_families,
            patch.object(agent, "_needs_refresh", return_value=True),
            patch.object(agent, "_do_refresh", side_effect=do_refresh),
        ):
            mock_discovery.return_value.get_all_commands.return_value = {"cmd": cmd}
            mock_families.return_value.get_all_families.return_value = {}
            asyncio.get_event_loop().run_until_complete(agent.run())

        assert agent._last_results["test_provider"] == "u1:error, u2:ok"


def _status_error(status: int) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "https://example.com/token")
    return httpx.HTTPStatusError(
        f"{status}", request=req, response=httpx.Response(status, request=req)
    )


def _client_raising(exc: Exception) -> MagicMock:
    client = MagicMock()
    client.post.return_value.raise_for_status.side_effect = exc
    return client


class _OverridingSource:
    """A component with a non-standard refresh flow (overrides the SDK hook)."""

    def __init__(self, bundle: TokenBundle | None) -> None:
        self.bundle = bundle
        self.calls: list[dict] = []
        self.stored: list[dict] = []
        self.user_seen: list[int | None] = []

    def refresh_token(self, *, refresh_token: str, client_secret: str | None = None,
                      timeout_seconds: float = 15.0) -> TokenBundle | None:
        from jarvis_command_sdk.context import get_current_user_id

        self.user_seen.append(get_current_user_id())
        self.calls.append({"refresh_token": refresh_token, "client_secret": client_secret})
        return self.bundle

    def store_auth_values(self, values: dict) -> None:
        self.stored.append(values)


class _InheritingSource:
    """A component that inherits the SDK's default refresh_token()."""

    refresh_token = IJarvisCommand.refresh_token

    def __init__(self) -> None:
        self.store_auth_values = MagicMock()


class TestRefreshTokenOverride:
    def test_override_is_called_instead_of_standard_grant(
        self, agent: TokenRefreshAgent, mock_auth_config, mock_secret_service
    ):
        mock_secret_service.get_secret_value.return_value = "old-refresh"
        source = _OverridingSource(TokenBundle("new-access", "new-refresh", 3600, {}))
        client = MagicMock()

        with patch.object(agent, "_http_client", return_value=client):
            assert agent._do_refresh(mock_auth_config, source) is True

        assert source.calls == [{"refresh_token": "old-refresh", "client_secret": None}]
        client.post.assert_not_called()
        assert source.stored == [{"access_token": "new-access", "refresh_token": "new-refresh"}]
        key, _value, scope = mock_secret_service.set_secret.call_args.args
        assert (key, scope) == ("TOKEN_EXPIRES_AT_TEST_PROVIDER", "integration")

    def test_override_runs_under_token_owner(
        self, agent: TokenRefreshAgent, mock_auth_config, mock_secret_service
    ):
        mock_secret_service.get_secret_value.return_value = "old-refresh"
        source = _OverridingSource(TokenBundle("new-access", None, None, {}))

        assert agent._do_refresh(mock_auth_config, source, user_id=7) is True

        assert source.user_seen == [7]
        assert source.stored == [{"access_token": "new-access"}]
        mock_secret_service.set_secret.assert_not_called()

    def test_override_works_without_exchange_url(
        self, agent: TokenRefreshAgent, mock_auth_config, mock_secret_service
    ):
        mock_auth_config.exchange_url = None
        mock_secret_service.get_secret_value.return_value = "old-refresh"
        source = _OverridingSource(TokenBundle("new-access", None, 60, {}))

        assert agent._do_refresh(mock_auth_config, source) is True

    def test_inherited_default_uses_shared_http_client(
        self, agent: TokenRefreshAgent, mock_auth_config, mock_secret_service
    ):
        mock_secret_service.get_secret_value.return_value = "old-refresh"
        source = _InheritingSource()
        client = MagicMock()
        client.post.return_value.json.return_value = {"access_token": "a", "expires_in": 60}

        with patch.object(agent, "_http_client", return_value=client):
            assert agent._do_refresh(mock_auth_config, source) is True

        client.post.assert_called_once()
        source.store_auth_values.assert_called_once_with({"access_token": "a"})

    def test_override_returning_none_flags_reauth(
        self, agent: TokenRefreshAgent, mock_auth_config, mock_secret_service, mock_auth_service
    ):
        mock_secret_service.get_secret_value.return_value = "old-refresh"
        source = _OverridingSource(None)

        assert agent._do_refresh(mock_auth_config, source) is False

        mock_auth_service.set_needs_auth.assert_called_once()
        assert mock_auth_service.set_needs_auth.call_args.args[0] == "test_provider"
        assert source.stored == []

    def test_override_raising_is_not_flagged(
        self, agent: TokenRefreshAgent, mock_auth_config, mock_secret_service, mock_auth_service
    ):
        mock_secret_service.get_secret_value.return_value = "old-refresh"
        with patch.object(_OverridingSource, "refresh_token", side_effect=RuntimeError("boom")):
            assert agent._do_refresh(mock_auth_config, _OverridingSource(None)) is False

        mock_auth_service.set_needs_auth.assert_not_called()


class TestFailedRefreshAsksForReauth:
    def test_rejected_refresh_token_flags_reauth(
        self, agent: TokenRefreshAgent, mock_auth_config, mock_secret_service, mock_auth_service
    ):
        mock_secret_service.get_secret_value.return_value = "old-refresh"
        client = _client_raising(_status_error(400))

        with patch.object(agent, "_http_client", return_value=client):
            assert agent._do_refresh(mock_auth_config, MagicMock()) is False

        mock_auth_service.set_needs_auth.assert_called_once()
        assert mock_auth_service.set_needs_auth.call_args.args[0] == "test_provider"
        mock_auth_service.clear_auth_flag.assert_not_called()

    def test_unauthorized_flags_reauth(
        self, agent: TokenRefreshAgent, mock_auth_config, mock_secret_service, mock_auth_service
    ):
        mock_secret_service.get_secret_value.return_value = "old-refresh"
        with patch.object(agent, "_http_client", return_value=_client_raising(_status_error(401))):
            assert agent._do_refresh(mock_auth_config, MagicMock()) is False
        mock_auth_service.set_needs_auth.assert_called_once()

    def test_server_error_is_not_flagged(
        self, agent: TokenRefreshAgent, mock_auth_config, mock_secret_service, mock_auth_service
    ):
        mock_secret_service.get_secret_value.return_value = "old-refresh"
        with patch.object(agent, "_http_client", return_value=_client_raising(_status_error(503))):
            assert agent._do_refresh(mock_auth_config, MagicMock()) is False
        mock_auth_service.set_needs_auth.assert_not_called()

    def test_connection_error_is_not_flagged(
        self, agent: TokenRefreshAgent, mock_auth_config, mock_secret_service, mock_auth_service
    ):
        mock_secret_service.get_secret_value.return_value = "old-refresh"
        client = MagicMock()
        client.post.side_effect = httpx.ConnectError("no route")
        with patch.object(agent, "_http_client", return_value=client):
            assert agent._do_refresh(mock_auth_config, MagicMock()) is False
        mock_auth_service.set_needs_auth.assert_not_called()

    def test_missing_access_token_flags_reauth(
        self, agent: TokenRefreshAgent, mock_auth_config, mock_secret_service, mock_auth_service
    ):
        mock_secret_service.get_secret_value.return_value = "old-refresh"
        client = MagicMock()
        client.post.return_value.json.return_value = {"error": "invalid_grant"}
        with patch.object(agent, "_http_client", return_value=client):
            assert agent._do_refresh(mock_auth_config, MagicMock()) is False
        mock_auth_service.set_needs_auth.assert_called_once()

    def test_no_stored_refresh_token_flags_reauth(
        self, agent: TokenRefreshAgent, mock_auth_config, mock_secret_service, mock_auth_service
    ):
        mock_secret_service.get_secret_value.return_value = None
        assert agent._do_refresh(mock_auth_config, MagicMock()) is False
        mock_auth_service.set_needs_auth.assert_called_once()

    def test_success_clears_reauth_flag(
        self, agent: TokenRefreshAgent, mock_auth_config, mock_secret_service, mock_auth_service
    ):
        mock_secret_service.get_secret_value.return_value = "old-refresh"
        client = MagicMock()
        client.post.return_value.json.return_value = {"access_token": "a"}
        with patch.object(agent, "_http_client", return_value=client):
            assert agent._do_refresh(mock_auth_config, MagicMock()) is True
        mock_auth_service.clear_auth_flag.assert_called_once_with("test_provider")
        mock_auth_service.set_needs_auth.assert_not_called()

    def test_flag_store_failure_does_not_break_refresh(
        self, agent: TokenRefreshAgent, mock_auth_config, mock_secret_service, mock_auth_service
    ):
        mock_secret_service.get_secret_value.return_value = "old-refresh"
        mock_auth_service.clear_auth_flag.side_effect = RuntimeError("db locked")
        client = MagicMock()
        client.post.return_value.json.return_value = {"access_token": "a"}
        with patch.object(agent, "_http_client", return_value=client):
            assert agent._do_refresh(mock_auth_config, MagicMock()) is True


class TestPerUserReauthFlag:
    """command_auth is per provider: a user-scoped provider's flag is cleared
    only when every owner refreshed, never by another owner's success."""

    def _run(self, agent, mock_auth_config, mock_secret_service, outcomes: dict[int, bool]):
        from types import SimpleNamespace

        cmd = MagicMock()
        cmd.authentication = mock_auth_config
        cmd.all_possible_secrets = [SimpleNamespace(key="TEST_REFRESH_TOKEN", scope="user")]
        mock_secret_service.get_all_secrets = MagicMock(return_value=[
            SimpleNamespace(key="TEST_REFRESH_TOKEN", user_id=uid, value=f"rt{uid}")
            for uid in outcomes
        ])
        with (
            patch("utils.command_discovery_service.get_command_discovery_service") as mock_discovery,
            patch("utils.device_family_discovery_service.get_device_family_discovery_service") as mock_families,
            patch.object(agent, "_needs_refresh", return_value=True),
            patch.object(agent, "_do_refresh", side_effect=lambda a, s, user_id=None: outcomes[user_id]),
        ):
            mock_discovery.return_value.get_all_commands.return_value = {"cmd": cmd}
            mock_families.return_value.get_all_families.return_value = {}
            asyncio.run(agent.run())

    def test_all_owners_ok_clears_flag(self, agent, mock_auth_config, mock_secret_service, mock_auth_service):
        self._run(agent, mock_auth_config, mock_secret_service, {1: True, 2: True})
        mock_auth_service.clear_auth_flag.assert_called_once_with("test_provider")

    def test_one_owner_failed_keeps_flag(self, agent, mock_auth_config, mock_secret_service, mock_auth_service):
        self._run(agent, mock_auth_config, mock_secret_service, {1: False, 2: True})
        mock_auth_service.clear_auth_flag.assert_not_called()

    def test_user_refresh_does_not_clear_flag_itself(
        self, agent, mock_auth_config, mock_secret_service, mock_auth_service
    ):
        mock_secret_service.get_secret_value.return_value = "rt1"
        client = MagicMock()
        client.post.return_value.json.return_value = {"access_token": "a"}
        with patch.object(agent, "_http_client", return_value=client):
            assert agent._do_refresh(mock_auth_config, MagicMock(), user_id=1) is True
        mock_auth_service.clear_auth_flag.assert_not_called()
