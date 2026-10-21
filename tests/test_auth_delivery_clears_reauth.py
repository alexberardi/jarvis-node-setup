"""Fresh credentials clear a provider's re-auth flag (#404).

Both delivery paths (config push ``auth:<provider>`` and the OAuth
auth-ready pull) store through ``store_auth_values()`` and then clear
``command_auth.needs_auth`` so the app stops asking to reconnect."""

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

_mock_db = MagicMock()
_mock_db.SessionLocal = MagicMock
_mock_db.engine = MagicMock()
if "sqlcipher3" not in sys.modules:
    sys.modules["sqlcipher3"] = MagicMock()
    sys.modules["sqlcipher3.dbapi2"] = MagicMock()
if "db" not in sys.modules:
    sys.modules["db"] = _mock_db


@pytest.fixture
def delivery():
    """The two delivery functions, imported against the real ``repositories``.

    tests/integration/conftest.py replaces ``repositories`` with a MagicMock
    during collection of a full run, which breaks importing these modules
    (the same error test_config_push_service.py hits). Import past it, and
    restore sys.modules afterwards so other tests see what they did before."""
    with patch.dict(sys.modules):
        for k in list(sys.modules):
            if (k == "repositories" or k.startswith("repositories.")) and isinstance(
                sys.modules[k], MagicMock
            ):
                del sys.modules[k]
        from scripts.mqtt_tts_listener import _pull_auth_credentials
        from services.config_push_service import _dispatch_config

        yield SimpleNamespace(dispatch=_dispatch_config, pull=_pull_auth_credentials)


def _command(provider: str) -> MagicMock:
    cmd = MagicMock()
    cmd.authentication.provider = provider
    cmd.command_name = "cmd"
    return cmd


@patch("services.command_auth_service.clear_auth_flag")
@patch("utils.command_discovery_service.get_command_discovery_service")
def test_config_push_auth_clears_flag(mock_discovery, mock_clear, delivery):
    cmd = _command("spotify")
    mock_discovery.return_value.get_all_commands.return_value = {"cmd": cmd}

    delivery.dispatch("auth:spotify", {"access_token": "a"})

    cmd.store_auth_values.assert_called_once()
    mock_clear.assert_called_once_with("spotify")


@patch("services.command_auth_service.clear_auth_flag", side_effect=RuntimeError("db locked"))
@patch("utils.command_discovery_service.get_command_discovery_service")
def test_clear_failure_does_not_fail_the_store(mock_discovery, mock_clear, delivery):
    cmd = _command("spotify")
    mock_discovery.return_value.get_all_commands.return_value = {"cmd": cmd}

    delivery.dispatch("auth:spotify", {"access_token": "a"})  # must not raise

    cmd.store_auth_values.assert_called_once()


@patch("services.command_auth_service.clear_auth_flag")
@patch("utils.command_discovery_service.get_command_discovery_service")
@patch("utils.service_discovery.get_command_center_url", return_value="http://cc")
@patch("clients.rest_client.RestClient.get", return_value={"access_token": "a", "user_id": 3})
def test_auth_ready_pull_clears_flag(_get, _url, mock_discovery, mock_clear, delivery):
    cmd = _command("google_calendar")
    mock_discovery.return_value.get_all_commands.return_value = {"cmd": cmd}

    delivery.pull("google_calendar")

    cmd.store_auth_values.assert_called_once_with({"access_token": "a"})
    mock_clear.assert_called_once_with("google_calendar")
