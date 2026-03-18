"""Tests for IJarvisDeviceManager interface and DeviceManagerDevice dataclass.

Tests the abstract interface, dataclass defaults, validate_secrets,
and is_available methods.
"""

from dataclasses import asdict
from typing import Any
from unittest.mock import patch

import pytest

from core.ijarvis_device_manager import DeviceManagerDevice, IJarvisDeviceManager
from core.ijarvis_secret import JarvisSecret


# =============================================================================
# Concrete test implementation
# =============================================================================


class StubDeviceManager(IJarvisDeviceManager):
    """Minimal concrete implementation for testing base class behavior."""

    def __init__(self, secrets: list | None = None) -> None:
        self._secrets = secrets or []

    @property
    def name(self) -> str:
        return "stub"

    @property
    def friendly_name(self) -> str:
        return "Stub Manager"

    @property
    def can_edit_devices(self) -> bool:
        return False

    @property
    def required_secrets(self) -> list:
        return self._secrets

    async def collect_devices(self) -> list[DeviceManagerDevice]:
        return []


# =============================================================================
# DeviceManagerDevice dataclass tests
# =============================================================================


class TestDeviceManagerDevice:
    def test_required_fields(self) -> None:
        """Device requires name, domain, entity_id."""
        dev = DeviceManagerDevice(
            name="Desk Lamp",
            domain="light",
            entity_id="light.desk_lamp",
        )
        assert dev.name == "Desk Lamp"
        assert dev.domain == "light"
        assert dev.entity_id == "light.desk_lamp"

    def test_defaults(self) -> None:
        """Default values are correct."""
        dev = DeviceManagerDevice(
            name="Lamp",
            domain="light",
            entity_id="light.lamp",
        )
        assert dev.is_controllable is True
        assert dev.source == "direct"
        assert dev.manufacturer is None
        assert dev.model is None
        assert dev.protocol is None
        assert dev.local_ip is None
        assert dev.mac_address is None
        assert dev.cloud_id is None
        assert dev.device_class is None
        assert dev.area is None
        assert dev.state is None
        assert dev.extra == {}

    def test_all_fields(self) -> None:
        """All fields are set correctly."""
        dev = DeviceManagerDevice(
            name="Living Room Light",
            domain="light",
            entity_id="light.living_room",
            is_controllable=True,
            manufacturer="LIFX",
            model="A19",
            protocol="lifx",
            local_ip="192.168.1.50",
            mac_address="aa:bb:cc:dd:ee:ff",
            cloud_id="cloud-123",
            device_class="light",
            source="direct",
            area="Living Room",
            state="on",
            extra={"brightness": 100},
        )
        assert dev.manufacturer == "LIFX"
        assert dev.model == "A19"
        assert dev.protocol == "lifx"
        assert dev.local_ip == "192.168.1.50"
        assert dev.mac_address == "aa:bb:cc:dd:ee:ff"
        assert dev.cloud_id == "cloud-123"
        assert dev.device_class == "light"
        assert dev.area == "Living Room"
        assert dev.state == "on"
        assert dev.extra == {"brightness": 100}

    def test_source_home_assistant(self) -> None:
        """Source can be set to home_assistant."""
        dev = DeviceManagerDevice(
            name="HA Light",
            domain="light",
            entity_id="light.ha_light",
            source="home_assistant",
        )
        assert dev.source == "home_assistant"

    def test_is_controllable_false(self) -> None:
        """is_controllable can be set to False."""
        dev = DeviceManagerDevice(
            name="Sensor",
            domain="sensor",
            entity_id="sensor.temp",
            is_controllable=False,
        )
        assert dev.is_controllable is False

    def test_asdict(self) -> None:
        """Device can be serialized to dict via dataclasses.asdict."""
        dev = DeviceManagerDevice(
            name="Lamp",
            domain="light",
            entity_id="light.lamp",
        )
        d = asdict(dev)
        assert d["name"] == "Lamp"
        assert d["domain"] == "light"
        assert d["entity_id"] == "light.lamp"
        assert d["source"] == "direct"
        assert d["extra"] == {}

    def test_extra_does_not_share_default(self) -> None:
        """Each device gets its own extra dict (no shared mutable default)."""
        dev1 = DeviceManagerDevice(name="A", domain="light", entity_id="light.a")
        dev2 = DeviceManagerDevice(name="B", domain="light", entity_id="light.b")
        dev1.extra["key"] = "value"
        assert "key" not in dev2.extra


# =============================================================================
# IJarvisDeviceManager interface tests
# =============================================================================


class TestIJarvisDeviceManagerDefaults:
    def test_description_default(self) -> None:
        """Default description is empty string."""
        mgr = StubDeviceManager()
        assert mgr.description == ""

    def test_required_secrets_default(self) -> None:
        """Default required_secrets is empty list when not overridden."""
        # Use a minimal subclass that doesn't override required_secrets
        class BareManager(IJarvisDeviceManager):
            @property
            def name(self) -> str:
                return "bare"

            @property
            def friendly_name(self) -> str:
                return "Bare"

            @property
            def can_edit_devices(self) -> bool:
                return False

            async def collect_devices(self) -> list[DeviceManagerDevice]:
                return []

        mgr = BareManager()
        assert mgr.required_secrets == []

    def test_authentication_default(self) -> None:
        """Default authentication is None."""
        mgr = StubDeviceManager()
        assert mgr.authentication is None


# =============================================================================
# validate_secrets tests
# =============================================================================


class TestValidateSecrets:
    def test_no_secrets_required(self) -> None:
        """No secrets required returns empty missing list."""
        mgr = StubDeviceManager(secrets=[])
        assert mgr.validate_secrets() == []

    def test_all_secrets_present(self) -> None:
        """All secrets present returns empty missing list."""
        secrets = [
            JarvisSecret("API_KEY", "Test key", "integration", "string"),
        ]
        mgr = StubDeviceManager(secrets=secrets)

        with patch("services.secret_service.get_secret_value", return_value="some-value"):
            assert mgr.validate_secrets() == []

    def test_missing_secrets(self) -> None:
        """Missing secrets are returned."""
        secrets = [
            JarvisSecret("API_KEY", "Test key", "integration", "string"),
            JarvisSecret("API_URL", "Test URL", "integration", "string"),
        ]
        mgr = StubDeviceManager(secrets=secrets)

        with patch("services.secret_service.get_secret_value", return_value=None):
            missing = mgr.validate_secrets()
            assert "API_KEY" in missing
            assert "API_URL" in missing

    def test_partial_missing_secrets(self) -> None:
        """Only missing secrets are returned."""
        secrets = [
            JarvisSecret("API_KEY", "Key", "integration", "string"),
            JarvisSecret("API_URL", "URL", "integration", "string"),
        ]
        mgr = StubDeviceManager(secrets=secrets)

        def _mock_get(key: str, scope: str) -> str | None:
            if key == "API_KEY":
                return "present"
            return None

        with patch("services.secret_service.get_secret_value", side_effect=_mock_get):
            missing = mgr.validate_secrets()
            assert missing == ["API_URL"]

    def test_optional_secrets_not_reported(self) -> None:
        """Non-required secrets are not reported as missing."""
        secrets = [
            JarvisSecret("OPTIONAL_KEY", "Optional", "integration", "string", required=False),
        ]
        mgr = StubDeviceManager(secrets=secrets)

        with patch("services.secret_service.get_secret_value", return_value=None):
            missing = mgr.validate_secrets()
            assert missing == []


# =============================================================================
# is_available tests
# =============================================================================


class TestIsAvailable:
    def test_available_no_secrets(self) -> None:
        """Manager with no required secrets is always available."""
        mgr = StubDeviceManager(secrets=[])
        assert mgr.is_available() is True

    def test_available_all_secrets_present(self) -> None:
        """Manager is available when all required secrets are present."""
        secrets = [
            JarvisSecret("API_KEY", "Key", "integration", "string"),
        ]
        mgr = StubDeviceManager(secrets=secrets)

        with patch("services.secret_service.get_secret_value", return_value="value"):
            assert mgr.is_available() is True

    def test_unavailable_missing_secret(self) -> None:
        """Manager is unavailable when a required secret is missing."""
        secrets = [
            JarvisSecret("API_KEY", "Key", "integration", "string"),
        ]
        mgr = StubDeviceManager(secrets=secrets)

        with patch("services.secret_service.get_secret_value", return_value=None):
            assert mgr.is_available() is False

    def test_available_optional_secret_missing(self) -> None:
        """Manager is available even if optional secrets are missing."""
        secrets = [
            JarvisSecret("REQUIRED", "Required", "integration", "string", required=True),
            JarvisSecret("OPTIONAL", "Optional", "integration", "string", required=False),
        ]
        mgr = StubDeviceManager(secrets=secrets)

        def _mock_get(key: str, scope: str) -> str | None:
            if key == "REQUIRED":
                return "present"
            return None

        with patch("services.secret_service.get_secret_value", side_effect=_mock_get):
            assert mgr.is_available() is True
