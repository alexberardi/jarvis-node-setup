"""Unit tests for config push service.

Tests AES-256-GCM decryption, dispatch routing, and end-to-end
process_pending_configs with mocked dependencies.
"""

import base64
import json
import os
from datetime import datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from services.config_push_service import (
    _b64url_decode,
    _decrypt_config,
    _dispatch_config,
    process_pending_configs,
)
from utils.encryption_utils import K2Data


# ── Helpers ──────────────────────────────────────────────────────────────


def _encrypt_config(
    config_data: dict[str, str],
    k2_raw: bytes,
    node_id: str,
    config_type: str,
) -> tuple[str, str, str]:
    """Encrypt config data the same way the mobile app does.

    Returns (ciphertext_b64url, nonce_b64url, tag_b64url).
    """
    # Mobile: base64url(JSON.stringify(configData)) → plaintext
    json_bytes = json.dumps(config_data).encode("utf-8")
    plaintext = base64.urlsafe_b64encode(json_bytes)  # bytes

    aad = f"{node_id}:{config_type}".encode("utf-8")
    nonce = os.urandom(12)
    aesgcm = AESGCM(k2_raw)
    ct_with_tag = aesgcm.encrypt(nonce, plaintext, aad)

    # AESGCM.encrypt returns ciphertext || tag (tag is last 16 bytes)
    ciphertext = ct_with_tag[:-16]
    tag = ct_with_tag[-16:]

    # Base64url-encode without padding (matching mobile)
    def b64url_no_pad(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

    return b64url_no_pad(ciphertext), b64url_no_pad(nonce), b64url_no_pad(tag)


# Fixed test key (32 bytes)
TEST_K2 = os.urandom(32)
TEST_NODE_ID = "node-abc-123"
TEST_CONFIG_TYPE = "auth:home_assistant"
TEST_K2_DATA = K2Data(k2=TEST_K2, kid="k2-test", created_at=datetime(2026, 1, 1))


# ── Tests ────────────────────────────────────────────────────────────────


class TestB64UrlDecode:
    """Test base64url decoding with missing padding."""

    def test_padded_input(self) -> None:
        original = b"hello world"
        encoded = base64.urlsafe_b64encode(original).decode("ascii")
        assert _b64url_decode(encoded) == original

    def test_unpadded_input(self) -> None:
        original = b"hello world"
        encoded = base64.urlsafe_b64encode(original).rstrip(b"=").decode("ascii")
        assert _b64url_decode(encoded) == original

    def test_url_safe_chars(self) -> None:
        """Verify +/ are replaced by -_ in urlsafe encoding."""
        data = bytes(range(256))
        encoded = base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")
        assert _b64url_decode(encoded) == data


class TestDecryptConfig:
    """Test AES-256-GCM decryption with known K2."""

    @patch("services.config_push_service.get_k2")
    def test_decrypt_roundtrip(self, mock_get_k2: MagicMock) -> None:
        mock_get_k2.return_value = TEST_K2_DATA
        config = {"access_token": "tok_12345", "base_url": "http://ha.local:8123"}

        ct, nonce, tag = _encrypt_config(config, TEST_K2, TEST_NODE_ID, TEST_CONFIG_TYPE)
        result = _decrypt_config(ct, nonce, tag, TEST_NODE_ID, TEST_CONFIG_TYPE)

        assert result == config

    @patch("services.config_push_service.get_k2")
    def test_decrypt_wrong_aad_fails(self, mock_get_k2: MagicMock) -> None:
        mock_get_k2.return_value = TEST_K2_DATA
        config = {"token": "abc"}

        ct, nonce, tag = _encrypt_config(config, TEST_K2, TEST_NODE_ID, TEST_CONFIG_TYPE)

        with pytest.raises(Exception):  # InvalidTag from cryptography
            _decrypt_config(ct, nonce, tag, "wrong-node", TEST_CONFIG_TYPE)

    @patch("services.config_push_service.get_k2")
    def test_decrypt_no_k2_raises(self, mock_get_k2: MagicMock) -> None:
        mock_get_k2.return_value = None

        with pytest.raises(ValueError, match="K2 key not available"):
            _decrypt_config("aaa", "bbb", "ccc", TEST_NODE_ID, TEST_CONFIG_TYPE)

    @patch("services.config_push_service.get_k2")
    def test_decrypt_wrong_key_fails(self, mock_get_k2: MagicMock) -> None:
        wrong_k2 = os.urandom(32)
        mock_get_k2.return_value = K2Data(k2=wrong_k2, kid="k2-wrong", created_at=datetime(2026, 1, 1))
        config = {"token": "secret"}

        ct, nonce, tag = _encrypt_config(config, TEST_K2, TEST_NODE_ID, TEST_CONFIG_TYPE)

        with pytest.raises(Exception):
            _decrypt_config(ct, nonce, tag, TEST_NODE_ID, TEST_CONFIG_TYPE)


class TestDispatchConfig:
    """Test config dispatch routing."""

    @patch("utils.command_discovery_service.get_command_discovery_service")
    def test_dispatch_auth_calls_store_auth_values(
        self, mock_get_service: MagicMock
    ) -> None:
        mock_cmd = MagicMock()
        mock_cmd.authentication = MagicMock()
        mock_cmd.authentication.provider = "home_assistant"
        mock_cmd.command_name = "control_device"

        mock_service = MagicMock()
        mock_service.get_all_commands.return_value = {"control_device": mock_cmd}
        mock_get_service.return_value = mock_service

        config_data = {"access_token": "tok_abc", "base_url": "http://ha.local:8123"}
        _dispatch_config("auth:home_assistant", config_data)

        mock_cmd.store_auth_values.assert_called_once_with(config_data)

    @patch("utils.command_discovery_service.get_command_discovery_service")
    @patch("utils.device_family_discovery_service.get_device_family_discovery_service")
    def test_dispatch_auth_no_matching_command_or_family(
        self, mock_family_service: MagicMock, mock_get_service: MagicMock
    ) -> None:
        """Should log warning but not raise when no command or family matches."""
        mock_service = MagicMock()
        mock_service.get_all_commands.return_value = {}
        mock_get_service.return_value = mock_service

        mock_fam_svc = MagicMock()
        mock_fam_svc.get_all_families_for_snapshot.return_value = {}
        mock_family_service.return_value = mock_fam_svc

        # Should not raise
        _dispatch_config("auth:spotify", {"token": "abc"})

    @patch("utils.command_discovery_service.get_command_discovery_service")
    @patch("utils.device_family_discovery_service.get_device_family_discovery_service")
    def test_dispatch_auth_falls_through_to_device_family(
        self, mock_family_service: MagicMock, mock_get_service: MagicMock
    ) -> None:
        """When no command matches, dispatch should search device families."""
        # No commands match
        mock_service = MagicMock()
        mock_service.get_all_commands.return_value = {}
        mock_get_service.return_value = mock_service

        # Device family matches
        mock_family = MagicMock()
        mock_family.authentication = MagicMock()
        mock_family.authentication.provider = "nest"
        mock_family.protocol_name = "nest"

        mock_fam_svc = MagicMock()
        mock_fam_svc.get_all_families_for_snapshot.return_value = {"nest": mock_family}
        mock_family_service.return_value = mock_fam_svc

        config_data = {"access_token": "tok_nest_123"}
        _dispatch_config("auth:nest", config_data)

        mock_family.store_auth_values.assert_called_once_with(config_data)

    @patch("services.secret_service.set_secret")
    def test_dispatch_non_auth_stores_secrets(
        self, mock_set_secret: MagicMock
    ) -> None:
        config_data = {"volume": "75", "led_brightness": "50"}
        _dispatch_config("settings:display", config_data)

        assert mock_set_secret.call_count == 2
        mock_set_secret.assert_any_call("volume", "75", "integration")
        mock_set_secret.assert_any_call("led_brightness", "50", "integration")


class TestProcessPendingConfigs:
    """Test end-to-end process_pending_configs with mocked dependencies."""

    @patch("services.config_push_service._ack_config")
    @patch("services.config_push_service._dispatch_config")
    @patch("services.config_push_service._decrypt_config")
    @patch("services.config_push_service._fetch_pending")
    def test_process_all_pending(
        self,
        mock_fetch: MagicMock,
        mock_decrypt: MagicMock,
        mock_dispatch: MagicMock,
        mock_ack: MagicMock,
    ) -> None:
        mock_fetch.return_value = [
            {
                "push_id": "push-1",
                "config_type": "auth:home_assistant",
                "node_id": TEST_NODE_ID,
                "ciphertext": "ct1",
                "nonce": "n1",
                "tag": "t1",
            },
            {
                "push_id": "push-2",
                "config_type": "settings:display",
                "node_id": TEST_NODE_ID,
                "ciphertext": "ct2",
                "nonce": "n2",
                "tag": "t2",
            },
        ]
        mock_decrypt.side_effect = [
            {"access_token": "tok"},
            {"brightness": "80"},
        ]

        count = process_pending_configs()

        assert count == 2
        assert mock_decrypt.call_count == 2
        assert mock_dispatch.call_count == 2
        assert mock_ack.call_count == 2
        mock_ack.assert_any_call("push-1")
        mock_ack.assert_any_call("push-2")

    @patch("services.config_push_service._fetch_pending")
    def test_process_empty_returns_zero(self, mock_fetch: MagicMock) -> None:
        mock_fetch.return_value = []
        assert process_pending_configs() == 0

    @patch("services.config_push_service._ack_config")
    @patch("services.config_push_service._dispatch_config")
    @patch("services.config_push_service._decrypt_config")
    @patch("services.config_push_service._fetch_pending")
    def test_process_continues_on_error(
        self,
        mock_fetch: MagicMock,
        mock_decrypt: MagicMock,
        mock_dispatch: MagicMock,
        mock_ack: MagicMock,
    ) -> None:
        """If one config fails to decrypt, the rest should still process."""
        mock_fetch.return_value = [
            {
                "push_id": "push-bad",
                "config_type": "auth:broken",
                "node_id": TEST_NODE_ID,
                "ciphertext": "bad",
                "nonce": "bad",
                "tag": "bad",
            },
            {
                "push_id": "push-good",
                "config_type": "auth:home_assistant",
                "node_id": TEST_NODE_ID,
                "ciphertext": "ct",
                "nonce": "n",
                "tag": "t",
            },
        ]
        mock_decrypt.side_effect = [
            ValueError("decrypt failed"),
            {"access_token": "tok"},
        ]

        count = process_pending_configs()

        assert count == 1
        mock_ack.assert_called_once_with("push-good")


class TestFetchPending:
    """Test _fetch_pending with mocked RestClient."""

    @patch("services.config_push_service.Config")
    @patch("services.config_push_service.get_command_center_url")
    @patch("services.config_push_service.RestClient")
    def test_fetch_dict_response(
        self,
        mock_rest: MagicMock,
        mock_cc_url: MagicMock,
        mock_config: MagicMock,
    ) -> None:
        from services.config_push_service import _fetch_pending

        mock_cc_url.return_value = "http://localhost:7703"
        mock_config.get_str.return_value = TEST_NODE_ID
        mock_rest.get.return_value = {"pending": [{"push_id": "p1"}]}

        result = _fetch_pending()

        assert len(result) == 1
        assert result[0]["push_id"] == "p1"

    @patch("services.config_push_service.Config")
    @patch("services.config_push_service.get_command_center_url")
    @patch("services.config_push_service.RestClient")
    def test_fetch_list_response(
        self,
        mock_rest: MagicMock,
        mock_cc_url: MagicMock,
        mock_config: MagicMock,
    ) -> None:
        from services.config_push_service import _fetch_pending

        mock_cc_url.return_value = "http://localhost:7703"
        mock_config.get_str.return_value = TEST_NODE_ID
        mock_rest.get.return_value = [{"push_id": "p1"}]

        result = _fetch_pending()

        assert len(result) == 1

    @patch("services.config_push_service.get_command_center_url")
    def test_fetch_no_cc_url(self, mock_cc_url: MagicMock) -> None:
        from services.config_push_service import _fetch_pending

        mock_cc_url.return_value = ""
        assert _fetch_pending() == []
