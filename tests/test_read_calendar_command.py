"""Tests for ReadCalendarCommand dynamic secrets and authentication."""

from unittest.mock import patch

import pytest

from commands.read_calendar_command import ReadCalendarCommand


@pytest.fixture
def cmd():
    return ReadCalendarCommand()


class TestRequiredSecrets:
    """required_secrets varies based on CALENDAR_TYPE."""

    def test_icloud_default(self, cmd: ReadCalendarCommand):
        """Unset CALENDAR_TYPE defaults to iCloud secrets."""
        with patch("commands.read_calendar_command.get_secret_value", return_value=None):
            keys = {s.key for s in cmd.required_secrets}
        assert "CALENDAR_USERNAME" in keys
        assert "CALENDAR_PASSWORD" in keys
        assert "GOOGLE_CLIENT_ID" not in keys

    def test_icloud_explicit(self, cmd: ReadCalendarCommand):
        """CALENDAR_TYPE=icloud returns iCloud secrets."""
        with patch("commands.read_calendar_command.get_secret_value", return_value="icloud"):
            keys = {s.key for s in cmd.required_secrets}
        assert "CALENDAR_USERNAME" in keys
        assert "CALENDAR_PASSWORD" in keys
        assert "GOOGLE_CLIENT_ID" not in keys

    def test_google(self, cmd: ReadCalendarCommand):
        """CALENDAR_TYPE=google returns Google secrets."""
        with patch("commands.read_calendar_command.get_secret_value", return_value="google"):
            keys = {s.key for s in cmd.required_secrets}
        assert "GOOGLE_CLIENT_ID" in keys
        assert "CALENDAR_USERNAME" not in keys
        assert "CALENDAR_PASSWORD" not in keys

    def test_always_includes_common(self, cmd: ReadCalendarCommand):
        """Both variants include CALENDAR_TYPE and CALENDAR_DEFAULT_NAME."""
        for cal_type in [None, "icloud", "google"]:
            with patch("commands.read_calendar_command.get_secret_value", return_value=cal_type):
                keys = {s.key for s in cmd.required_secrets}
            assert "CALENDAR_TYPE" in keys
            assert "CALENDAR_DEFAULT_NAME" in keys

    def test_db_error_defaults_to_icloud(self, cmd: ReadCalendarCommand):
        """If DB is unavailable, defaults to iCloud secrets."""
        with patch("commands.read_calendar_command.get_secret_value", side_effect=Exception("DB down")):
            keys = {s.key for s in cmd.required_secrets}
        assert "CALENDAR_USERNAME" in keys
        assert "GOOGLE_CLIENT_ID" not in keys


class TestAllPossibleSecrets:
    """all_possible_secrets returns the superset of all variants."""

    def test_superset(self, cmd: ReadCalendarCommand):
        keys = {s.key for s in cmd.all_possible_secrets}
        assert keys == {
            "CALENDAR_TYPE",
            "CALENDAR_DEFAULT_NAME",
            "CALENDAR_USERNAME",
            "CALENDAR_PASSWORD",
            "GOOGLE_CLIENT_ID",
        }


class TestAuthentication:
    """authentication property returns Google OAuth config or None."""

    def test_icloud_returns_none(self, cmd: ReadCalendarCommand):
        with patch("commands.read_calendar_command.get_secret_value", return_value="icloud"):
            assert cmd.authentication is None

    def test_unset_returns_none(self, cmd: ReadCalendarCommand):
        with patch("commands.read_calendar_command.get_secret_value", return_value=None):
            assert cmd.authentication is None

    def test_google_returns_config(self, cmd: ReadCalendarCommand):
        def _mock_secret(key: str, scope: str):
            if key == "CALENDAR_TYPE":
                return "google"
            if key == "GOOGLE_CLIENT_ID":
                return "test-client-id.apps.googleusercontent.com"
            return None

        with patch("commands.read_calendar_command.get_secret_value", side_effect=_mock_secret):
            auth = cmd.authentication
        assert auth is not None
        assert auth.provider == "google_calendar"
        assert auth.type == "oauth"
        assert auth.client_id == "test-client-id.apps.googleusercontent.com"
        assert auth.supports_pkce is True
        assert "https://www.googleapis.com/auth/calendar.readonly" in auth.scopes
        assert auth.authorize_url == "https://accounts.google.com/o/oauth2/v2/auth"
        assert auth.exchange_url == "https://oauth2.googleapis.com/token"
        assert auth.keys == ["access_token", "refresh_token"]


class TestStoreAuthValues:
    """store_auth_values persists tokens and clears re-auth flag."""

    def test_stores_tokens(self, cmd: ReadCalendarCommand):
        with patch("services.secret_service.set_secret") as mock_set, \
             patch("services.command_auth_service.clear_auth_flag") as mock_clear:
            cmd.store_auth_values({
                "access_token": "ya29.test-access",
                "refresh_token": "1//test-refresh",
            })

        mock_set.assert_any_call("GOOGLE_ACCESS_TOKEN", "ya29.test-access", "integration")
        mock_set.assert_any_call("GOOGLE_REFRESH_TOKEN", "1//test-refresh", "integration")
        mock_clear.assert_called_once_with("google_calendar")

    def test_stores_access_token_only(self, cmd: ReadCalendarCommand):
        """If refresh_token is missing (e.g., re-auth), only access_token is stored."""
        with patch("services.secret_service.set_secret") as mock_set, \
             patch("services.command_auth_service.clear_auth_flag"):
            cmd.store_auth_values({"access_token": "ya29.test-access"})

        mock_set.assert_called_once_with("GOOGLE_ACCESS_TOKEN", "ya29.test-access", "integration")
