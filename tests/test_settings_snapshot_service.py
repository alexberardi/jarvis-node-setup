"""Tests for settings_snapshot_service."""

import json
import os
from unittest.mock import MagicMock, patch

import pytest

from services.settings_snapshot_service import (
    build_snapshot,
    encrypt_snapshot,
    handle_snapshot_request,
    upload_snapshot,
)


def _make_mock_secret(key: str, scope: str, description: str, value_type: str, required: bool) -> MagicMock:
    """Create a mock IJarvisSecret."""
    secret = MagicMock()
    secret.key = key
    secret.scope = scope
    secret.description = description
    secret.value_type = value_type
    secret.required = required
    return secret


def _make_mock_parameter(
    name: str,
    param_type: str = "str",
    description: str | None = None,
    required: bool = False,
    default_value: str | None = None,
    enum_values: list[str] | None = None,
) -> MagicMock:
    """Create a mock IJarvisParameter."""
    param = MagicMock()
    param.name = name
    param.param_type = param_type
    param.description = description
    param.required = required
    param.default_value = default_value
    param.enum_values = enum_values
    param.to_dict.return_value = {
        "name": name,
        "type": param_type,
        "description": description,
        "required": required,
        "default_value": default_value,
        "enum_values": enum_values,
    }
    return param


def _make_mock_command(
    name: str,
    description: str,
    secrets: list[MagicMock],
    parameters: list[MagicMock] | None = None,
) -> MagicMock:
    """Create a mock IJarvisCommand."""
    cmd = MagicMock()
    cmd.command_name = name
    cmd.description = description
    cmd.required_secrets = secrets
    cmd.parameters = parameters or []
    cmd.associated_service = None
    cmd.authentication = None
    return cmd


def _make_mock_button(text: str, action: str, btype: str, icon: str | None = None) -> MagicMock:
    """Create a mock IJarvisButton."""
    btn = MagicMock()
    btn.button_text = text
    btn.button_action = action
    btn.button_type = btype
    btn.button_icon = icon
    d: dict[str, str] = {"button_text": text, "button_action": action, "button_type": btype}
    if icon:
        d["button_icon"] = icon
    btn.to_dict.return_value = d
    return btn


def _make_mock_family(
    protocol_name: str,
    friendly_name: str,
    description: str,
    connection_type: str = "lan",
    supported_domains: list[str] | None = None,
    secrets: list[MagicMock] | None = None,
    authentication: MagicMock | None = None,
    missing_secrets: list[str] | None = None,
    supported_actions: list[MagicMock] | None = None,
) -> MagicMock:
    """Create a mock IJarvisDeviceProtocol for snapshot tests."""
    family = MagicMock()
    family.protocol_name = protocol_name
    family.friendly_name = friendly_name
    family.description = description
    family.connection_type = connection_type
    family.supported_domains = supported_domains or []
    family.required_secrets = secrets or []
    family.authentication = authentication
    family.validate_secrets.return_value = missing_secrets or []
    if supported_actions is not None:
        family.supported_actions = supported_actions
    else:
        family.supported_actions = [
            _make_mock_button("Turn On", "turn_on", "primary", "power"),
            _make_mock_button("Turn Off", "turn_off", "secondary", "power-off"),
        ]
    return family


class TestBuildSnapshot:
    """Tests for build_snapshot()."""

    @patch("services.settings_snapshot_service.get_device_family_discovery_service")
    @patch("services.settings_snapshot_service.get_secret_value")
    @patch("services.settings_snapshot_service.get_command_discovery_service")
    def test_builds_snapshot_with_set_and_unset_secrets(
        self, mock_discovery, mock_get_secret, mock_family_discovery
    ):
        mock_family_service = MagicMock()
        mock_family_service.get_all_families_for_snapshot.return_value = {}
        mock_family_discovery.return_value = mock_family_service

        api_key_secret = _make_mock_secret(
            "API_KEY", "integration", "API key", "string", True
        )
        location_secret = _make_mock_secret(
            "LOCATION", "node", "Location", "string", False
        )
        cmd = _make_mock_command("get_weather", "Weather", [api_key_secret, location_secret])

        mock_service = MagicMock()
        mock_service.get_all_commands.return_value = {"get_weather": cmd}
        mock_discovery.return_value = mock_service

        mock_get_secret.side_effect = lambda key, scope: "abc123" if key == "API_KEY" else None

        snapshot = build_snapshot()

        assert snapshot["schema_version"] == 1
        assert snapshot["commands_schema_version"] == 2
        assert len(snapshot["commands"]) == 1

        weather = snapshot["commands"][0]
        assert weather["command_name"] == "get_weather"
        assert weather["description"] == "Weather"
        assert len(weather["secrets"]) == 2

        api_entry = weather["secrets"][0]
        assert api_entry["key"] == "API_KEY"
        assert api_entry["is_set"] is True
        assert "value" not in api_entry  # never include actual values

        loc_entry = weather["secrets"][1]
        assert loc_entry["key"] == "LOCATION"
        assert loc_entry["is_set"] is False

    @patch("services.settings_snapshot_service.get_device_family_discovery_service")
    @patch("services.settings_snapshot_service.get_secret_value")
    @patch("services.settings_snapshot_service.get_command_discovery_service")
    def test_includes_commands_without_secrets(self, mock_discovery, mock_get_secret, mock_family_discovery):
        mock_family_service = MagicMock()
        mock_family_service.get_all_families_for_snapshot.return_value = {}
        mock_family_discovery.return_value = mock_family_service

        cmd_no_secrets = _make_mock_command("jokes", "Tell a joke", [])
        cmd_with_secrets = _make_mock_command(
            "weather",
            "Weather",
            [_make_mock_secret("KEY", "integration", "Key", "string", True)],
        )

        mock_service = MagicMock()
        mock_service.get_all_commands.return_value = {
            "jokes": cmd_no_secrets,
            "weather": cmd_with_secrets,
        }
        mock_discovery.return_value = mock_service
        mock_get_secret.return_value = None

        snapshot = build_snapshot()
        assert len(snapshot["commands"]) == 2
        names = [c["command_name"] for c in snapshot["commands"]]
        assert "jokes" in names
        assert "weather" in names
        jokes = next(c for c in snapshot["commands"] if c["command_name"] == "jokes")
        assert jokes["secrets"] == []

    @patch("services.settings_snapshot_service.get_device_family_discovery_service")
    @patch("services.settings_snapshot_service.get_secret_value")
    @patch("services.settings_snapshot_service.get_command_discovery_service")
    def test_includes_parameters_in_snapshot(self, mock_discovery, mock_get_secret, mock_family_discovery):
        """Commands with parameters include them in the snapshot."""
        mock_family_service = MagicMock()
        mock_family_service.get_all_families_for_snapshot.return_value = {}
        mock_family_discovery.return_value = mock_family_service
        location_param = _make_mock_parameter(
            "location", param_type="str", description="City name", required=True,
        )
        unit_param = _make_mock_parameter(
            "units", param_type="str", description="Temperature units",
            required=False, default_value="fahrenheit",
            enum_values=["fahrenheit", "celsius"],
        )
        secret = _make_mock_secret("API_KEY", "integration", "API key", "string", True)
        cmd = _make_mock_command(
            "get_weather", "Weather", [secret], parameters=[location_param, unit_param],
        )

        mock_service = MagicMock()
        mock_service.get_all_commands.return_value = {"get_weather": cmd}
        mock_discovery.return_value = mock_service
        mock_get_secret.return_value = "key123"

        snapshot = build_snapshot()
        weather = snapshot["commands"][0]
        assert "parameters" in weather
        assert len(weather["parameters"]) == 2

        loc = weather["parameters"][0]
        assert loc["name"] == "location"
        assert loc["type"] == "str"
        assert loc["required"] is True

        units = weather["parameters"][1]
        assert units["name"] == "units"
        assert units["enum_values"] == ["fahrenheit", "celsius"]
        assert units["default_value"] == "fahrenheit"

    @patch("services.settings_snapshot_service.get_device_family_discovery_service")
    @patch("services.settings_snapshot_service.get_secret_value")
    @patch("services.settings_snapshot_service.get_command_discovery_service")
    def test_omits_parameters_when_empty(self, mock_discovery, mock_get_secret, mock_family_discovery):
        """Commands with no parameters should not include a parameters key."""
        mock_family_service = MagicMock()
        mock_family_service.get_all_families_for_snapshot.return_value = {}
        mock_family_discovery.return_value = mock_family_service
        secret = _make_mock_secret("KEY", "integration", "Key", "string", True)
        cmd = _make_mock_command("simple", "Simple cmd", [secret])

        mock_service = MagicMock()
        mock_service.get_all_commands.return_value = {"simple": cmd}
        mock_discovery.return_value = mock_service
        mock_get_secret.return_value = None

        snapshot = build_snapshot()
        assert "parameters" not in snapshot["commands"][0]

    @patch("services.settings_snapshot_service.get_device_family_discovery_service")
    @patch("services.settings_snapshot_service.get_secret_value")
    @patch("services.settings_snapshot_service.get_command_discovery_service")
    def test_empty_commands_returns_empty_list(self, mock_discovery, mock_get_secret, mock_family_discovery):
        mock_family_service = MagicMock()
        mock_family_service.get_all_families_for_snapshot.return_value = {}
        mock_family_discovery.return_value = mock_family_service

        mock_service = MagicMock()
        mock_service.get_all_commands.return_value = {}
        mock_discovery.return_value = mock_service

        snapshot = build_snapshot()
        assert snapshot["commands"] == []


class TestDeviceFamiliesInSnapshot:
    """Tests for device_families in build_snapshot()."""

    @patch("services.settings_snapshot_service.get_device_family_discovery_service")
    @patch("services.settings_snapshot_service.get_secret_value")
    @patch("services.settings_snapshot_service.get_command_discovery_service")
    def test_snapshot_includes_device_families(
        self, mock_discovery, mock_get_secret, mock_family_discovery
    ):
        # Set up empty commands
        mock_cmd_service = MagicMock()
        mock_cmd_service.get_all_commands.return_value = {}
        mock_discovery.return_value = mock_cmd_service

        # Set up a device family with a secret
        api_key_secret = _make_mock_secret(
            "GOVEE_API_KEY", "integration", "Govee API key", "string", True
        )
        api_key_secret.is_sensitive = True
        api_key_secret.friendly_name = "Govee API Key"

        govee = _make_mock_family(
            protocol_name="govee",
            friendly_name="Govee",
            description="Govee smart devices (LAN + cloud control)",
            connection_type="hybrid",
            supported_domains=["switch", "light"],
            secrets=[api_key_secret],
            missing_secrets=["GOVEE_API_KEY"],
        )

        mock_family_service = MagicMock()
        mock_family_service.get_all_families_for_snapshot.return_value = {"govee": govee}
        mock_family_discovery.return_value = mock_family_service

        mock_get_secret.return_value = None  # GOVEE_API_KEY not set

        snapshot = build_snapshot()

        assert "device_families" in snapshot
        assert len(snapshot["device_families"]) == 1

        family = snapshot["device_families"][0]
        assert family["family_name"] == "govee"
        assert family["friendly_name"] == "Govee"
        assert family["description"] == "Govee smart devices (LAN + cloud control)"
        assert family["connection_type"] == "hybrid"
        assert family["supported_domains"] == ["switch", "light"]
        assert family["is_configured"] is False
        assert len(family["secrets"]) == 1
        assert family["secrets"][0]["key"] == "GOVEE_API_KEY"
        assert family["secrets"][0]["is_set"] is False
        # Verify supported_actions are serialized
        assert "supported_actions" in family
        assert len(family["supported_actions"]) == 2
        assert family["supported_actions"][0]["button_action"] == "turn_on"
        assert family["supported_actions"][1]["button_action"] == "turn_off"

    @patch("services.settings_snapshot_service.get_device_family_discovery_service")
    @patch("services.settings_snapshot_service.get_secret_value")
    @patch("services.settings_snapshot_service.get_command_discovery_service")
    def test_lan_family_with_no_secrets_is_configured(
        self, mock_discovery, mock_get_secret, mock_family_discovery
    ):
        mock_cmd_service = MagicMock()
        mock_cmd_service.get_all_commands.return_value = {}
        mock_discovery.return_value = mock_cmd_service

        lifx = _make_mock_family(
            protocol_name="lifx",
            friendly_name="LIFX",
            description="LIFX smart lights (LAN control)",
            connection_type="lan",
            supported_domains=["light"],
            secrets=[],
            missing_secrets=[],  # no secrets = configured
        )

        mock_family_service = MagicMock()
        mock_family_service.get_all_families_for_snapshot.return_value = {"lifx": lifx}
        mock_family_discovery.return_value = mock_family_service

        snapshot = build_snapshot()

        family = snapshot["device_families"][0]
        assert family["family_name"] == "lifx"
        assert family["is_configured"] is True
        assert family["secrets"] == []

    @patch("services.settings_snapshot_service.get_device_family_discovery_service")
    @patch("services.settings_snapshot_service.get_secret_value")
    @patch("services.settings_snapshot_service.get_command_discovery_service")
    def test_snapshot_includes_family_authentication(
        self, mock_discovery, mock_get_secret, mock_family_discovery
    ):
        mock_cmd_service = MagicMock()
        mock_cmd_service.get_all_commands.return_value = {}
        mock_discovery.return_value = mock_cmd_service

        mock_auth = MagicMock()
        mock_auth.to_dict.return_value = {
            "type": "oauth",
            "provider": "nest",
            "friendly_name": "Google Nest",
            "client_id": "nest-client-id",
            "keys": ["access_token"],
        }

        nest = _make_mock_family(
            protocol_name="nest",
            friendly_name="Google Nest",
            description="Nest thermostat (cloud)",
            connection_type="cloud",
            supported_domains=["climate"],
            authentication=mock_auth,
        )

        mock_family_service = MagicMock()
        mock_family_service.get_all_families_for_snapshot.return_value = {"nest": nest}
        mock_family_discovery.return_value = mock_family_service

        snapshot = build_snapshot()

        family = snapshot["device_families"][0]
        assert "authentication" in family
        assert family["authentication"]["provider"] == "nest"


class TestEncryptSnapshot:
    """Tests for encrypt_snapshot()."""

    @patch("services.settings_snapshot_service.get_k2")
    def test_encrypts_and_returns_components(self, mock_get_k2):
        from utils.encryption_utils import K2Data
        from datetime import datetime

        k2_raw = os.urandom(32)
        mock_get_k2.return_value = K2Data(
            k2=k2_raw, kid="k2-dev-test", created_at=datetime.now()
        )

        snapshot = {"schema_version": 1, "commands": []}
        result = encrypt_snapshot(snapshot, "node-123")

        assert "ciphertext" in result
        assert "nonce" in result
        assert "tag" in result
        # All values should be non-empty base64url strings
        for key in ("ciphertext", "nonce", "tag"):
            assert len(result[key]) > 0
            assert "+" not in result[key]  # base64url uses - not +
            assert "/" not in result[key]  # base64url uses _ not /

    @patch("services.settings_snapshot_service.get_k2")
    def test_encrypt_raises_without_k2(self, mock_get_k2):
        mock_get_k2.return_value = None
        with pytest.raises(ValueError, match="K2 key not available"):
            encrypt_snapshot({}, "node-123")

    @patch("services.settings_snapshot_service.get_k2")
    def test_encrypted_snapshot_can_be_decrypted(self, mock_get_k2):
        """Round-trip: encrypt then decrypt to verify correctness."""
        import base64
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from datetime import datetime
        from utils.encryption_utils import K2Data

        k2_raw = os.urandom(32)
        mock_get_k2.return_value = K2Data(
            k2=k2_raw, kid="k2-dev-test", created_at=datetime.now()
        )

        snapshot = {"schema_version": 1, "commands": [{"command_name": "test"}]}
        node_id = "node-456"
        encrypted = encrypt_snapshot(snapshot, node_id)

        # Decrypt
        def _b64url_decode(s: str) -> bytes:
            padding = (4 - len(s) % 4) % 4
            return base64.urlsafe_b64decode(s + "=" * padding)

        ct = _b64url_decode(encrypted["ciphertext"])
        nonce = _b64url_decode(encrypted["nonce"])
        tag = _b64url_decode(encrypted["tag"])
        aad = f"{node_id}:settings:snapshot".encode("utf-8")

        aesgcm = AESGCM(k2_raw)
        plaintext_b64url = aesgcm.decrypt(nonce, ct + tag, aad).decode("utf-8")

        # Decode the base64url-encoded JSON
        plaintext_json = _b64url_decode(plaintext_b64url)
        decrypted = json.loads(plaintext_json)

        assert decrypted == snapshot


class TestUploadSnapshot:
    """Tests for upload_snapshot()."""

    @patch("services.settings_snapshot_service.RestClient")
    @patch("services.settings_snapshot_service.get_command_center_url")
    def test_upload_success(self, mock_cc_url, mock_rest):
        mock_cc_url.return_value = "http://localhost:7703"
        mock_rest.put.return_value = {"status": "ok"}

        result = upload_snapshot("node-1", "req-1", {"ciphertext": "ct", "nonce": "n", "tag": "t"})
        assert result is True
        mock_rest.put.assert_called_once()

    @patch("services.settings_snapshot_service.RestClient")
    @patch("services.settings_snapshot_service.get_command_center_url")
    def test_upload_failure_returns_false(self, mock_cc_url, mock_rest):
        mock_cc_url.return_value = "http://localhost:7703"
        mock_rest.put.return_value = None

        result = upload_snapshot("node-1", "req-1", {"ciphertext": "ct", "nonce": "n", "tag": "t"})
        assert result is False

    @patch("services.settings_snapshot_service.get_command_center_url")
    def test_upload_no_cc_url_returns_false(self, mock_cc_url):
        mock_cc_url.return_value = ""
        result = upload_snapshot("node-1", "req-1", {})
        assert result is False


class TestHandleSnapshotRequest:
    """Tests for handle_snapshot_request() full flow."""

    @patch("services.settings_snapshot_service.upload_snapshot")
    @patch("services.settings_snapshot_service.encrypt_snapshot")
    @patch("services.settings_snapshot_service.build_snapshot")
    @patch("services.settings_snapshot_service.RestClient")
    @patch("services.settings_snapshot_service.get_command_center_url")
    @patch("services.settings_snapshot_service.Config")
    def test_full_flow_success(
        self, mock_config, mock_cc_url, mock_rest, mock_build, mock_encrypt, mock_upload
    ):
        mock_config.get_str.return_value = "node-1"
        mock_cc_url.return_value = "http://localhost:7703"
        mock_rest.get.return_value = {"status": "pending"}
        mock_build.return_value = {"schema_version": 1, "commands": []}
        mock_encrypt.return_value = {"ciphertext": "ct", "nonce": "n", "tag": "t"}
        mock_upload.return_value = True

        result = handle_snapshot_request("req-123")
        assert result is True
        mock_build.assert_called_once()
        mock_encrypt.assert_called_once()
        mock_upload.assert_called_once()

    @patch("services.settings_snapshot_service.get_command_center_url")
    @patch("services.settings_snapshot_service.Config")
    def test_missing_node_id_returns_false(self, mock_config, mock_cc_url):
        mock_config.get_str.return_value = ""
        result = handle_snapshot_request("req-123")
        assert result is False

    @patch("services.settings_snapshot_service.RestClient")
    @patch("services.settings_snapshot_service.get_command_center_url")
    @patch("services.settings_snapshot_service.Config")
    def test_confirmation_failure_returns_false(self, mock_config, mock_cc_url, mock_rest):
        mock_config.get_str.return_value = "node-1"
        mock_cc_url.return_value = "http://localhost:7703"
        mock_rest.get.return_value = None

        result = handle_snapshot_request("req-123")
        assert result is False

    @patch("services.settings_snapshot_service.encrypt_snapshot")
    @patch("services.settings_snapshot_service.build_snapshot")
    @patch("services.settings_snapshot_service.RestClient")
    @patch("services.settings_snapshot_service.get_command_center_url")
    @patch("services.settings_snapshot_service.Config")
    def test_encryption_failure_returns_false(
        self, mock_config, mock_cc_url, mock_rest, mock_build, mock_encrypt
    ):
        mock_config.get_str.return_value = "node-1"
        mock_cc_url.return_value = "http://localhost:7703"
        mock_rest.get.return_value = {"status": "pending"}
        mock_build.return_value = {"schema_version": 1, "commands": []}
        mock_encrypt.side_effect = ValueError("K2 key not available")

        result = handle_snapshot_request("req-123")
        assert result is False
