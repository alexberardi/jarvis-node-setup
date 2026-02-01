"""
Unit tests for WiFi credential storage.
"""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from provisioning.wifi_credentials import (
    clear_wifi_credentials,
    load_wifi_credentials,
    save_wifi_credentials,
)


@pytest.fixture
def temp_secret_dir(tmp_path):
    """Create a temporary secret directory for testing."""
    secret_dir = tmp_path / ".jarvis"
    secret_dir.mkdir()
    with patch("provisioning.wifi_credentials.get_secret_dir", return_value=secret_dir):
        with patch("utils.encryption_utils.get_secret_dir", return_value=secret_dir):
            yield secret_dir


class TestSaveWifiCredentials:
    """Test saving WiFi credentials."""

    def test_save_creates_encrypted_file(self, temp_secret_dir):
        save_wifi_credentials("TestNetwork", "testpass123")

        cred_file = temp_secret_dir / "wifi_credentials.enc"
        assert cred_file.exists()

        # Verify it's encrypted (not plaintext)
        content = cred_file.read_bytes()
        assert b"TestNetwork" not in content
        assert b"testpass123" not in content

    def test_save_overwrites_existing(self, temp_secret_dir):
        save_wifi_credentials("Network1", "pass1")
        save_wifi_credentials("Network2", "pass2")

        # Should be able to load the new credentials
        result = load_wifi_credentials()
        assert result is not None
        assert result[0] == "Network2"
        assert result[1] == "pass2"


class TestLoadWifiCredentials:
    """Test loading WiFi credentials."""

    def test_load_returns_none_if_not_exists(self, temp_secret_dir):
        result = load_wifi_credentials()
        assert result is None

    def test_load_returns_saved_credentials(self, temp_secret_dir):
        save_wifi_credentials("MyNetwork", "mypassword")

        result = load_wifi_credentials()
        assert result is not None
        ssid, password = result
        assert ssid == "MyNetwork"
        assert password == "mypassword"

    def test_load_handles_corrupted_file(self, temp_secret_dir):
        # Write invalid data
        cred_file = temp_secret_dir / "wifi_credentials.enc"
        cred_file.write_bytes(b"not valid encrypted data")

        result = load_wifi_credentials()
        # Should return None on decryption failure, not raise
        assert result is None


class TestClearWifiCredentials:
    """Test clearing WiFi credentials."""

    def test_clear_removes_file(self, temp_secret_dir):
        save_wifi_credentials("TestNetwork", "testpass")

        cred_file = temp_secret_dir / "wifi_credentials.enc"
        assert cred_file.exists()

        clear_wifi_credentials()
        assert not cred_file.exists()

    def test_clear_no_error_if_not_exists(self, temp_secret_dir):
        # Should not raise if file doesn't exist
        clear_wifi_credentials()


class TestCredentialRoundTrip:
    """Test full round-trip of save/load/clear."""

    def test_full_flow(self, temp_secret_dir):
        # Initially empty
        assert load_wifi_credentials() is None

        # Save
        save_wifi_credentials("HomeWiFi", "supersecret")

        # Load
        result = load_wifi_credentials()
        assert result == ("HomeWiFi", "supersecret")

        # Clear
        clear_wifi_credentials()
        assert load_wifi_credentials() is None

    def test_special_characters_in_credentials(self, temp_secret_dir):
        # Test with special characters that might cause JSON issues
        ssid = "My Café's WiFi 🌐"
        password = 'p@ss"word\\with$pecial'

        save_wifi_credentials(ssid, password)
        result = load_wifi_credentials()

        assert result is not None
        assert result[0] == ssid
        assert result[1] == password
