"""Tests for DeviceFamilyDiscoveryService.

Tests family discovery, secret validation, ImportError handling,
and singleton pattern — mirrors test_agent_discovery_service.py.
"""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from device_families.base import DeviceControlResult, IJarvisDeviceProtocol, DiscoveredDevice
from utils.device_family_discovery_service import (
    DeviceFamilyDiscoveryService,
    get_device_family_discovery_service,
)


class MockProtocol(IJarvisDeviceProtocol):
    """Test protocol implementation (LAN, no secrets)."""

    @property
    def protocol_name(self) -> str:
        return "mock"

    @property
    def supported_domains(self) -> list[str]:
        return ["light", "switch"]

    async def discover(self, timeout: float = 5.0) -> list[DiscoveredDevice]:
        return []

    async def control(
        self, ip: str, action: str, data: dict[str, Any] | None = None, **kwargs: Any
    ) -> DeviceControlResult:
        return DeviceControlResult(success=True, entity_id="light.mock", action=action)

    async def get_state(self, ip: str, **kwargs: Any) -> dict[str, Any] | None:
        return {"state": "on"}


class MockCloudProtocol(IJarvisDeviceProtocol):
    """Test cloud protocol with required secrets."""

    @property
    def protocol_name(self) -> str:
        return "mock_cloud"

    @property
    def supported_domains(self) -> list[str]:
        return ["lock"]

    @property
    def connection_type(self) -> str:
        return "cloud"

    @property
    def required_secrets(self) -> list:
        from core.ijarvis_secret import JarvisSecret
        return [
            JarvisSecret("MOCK_API_KEY", "Mock API key", "integration", "string"),
        ]

    async def discover(self, timeout: float = 5.0) -> list[DiscoveredDevice]:
        return []

    async def control(
        self, ip: str, action: str, data: dict[str, Any] | None = None, **kwargs: Any
    ) -> DeviceControlResult:
        return DeviceControlResult(success=True, entity_id="lock.mock", action=action)

    async def get_state(self, ip: str, **kwargs: Any) -> dict[str, Any] | None:
        return {"state": "locked"}


@pytest.fixture
def fresh_service() -> DeviceFamilyDiscoveryService:
    """Create a fresh DeviceFamilyDiscoveryService for each test."""
    return DeviceFamilyDiscoveryService()


# =============================================================================
# Service creation tests
# =============================================================================


class TestDeviceFamilyDiscoveryServiceCreation:
    def test_create_service(self, fresh_service: DeviceFamilyDiscoveryService) -> None:
        assert fresh_service is not None
        assert fresh_service._discovered is False

    def test_cache_initially_empty(self, fresh_service: DeviceFamilyDiscoveryService) -> None:
        assert fresh_service._families_cache == {}


# =============================================================================
# Discovery tests
# =============================================================================


class TestFamilyDiscovery:
    def test_discover_no_package(self, fresh_service: DeviceFamilyDiscoveryService) -> None:
        """No device_families package returns empty dict."""
        with patch.dict("sys.modules", {"device_families": None}):
            with patch("utils.device_family_discovery_service.importlib") as mock_import:
                mock_import.import_module.side_effect = ImportError("No module")
                result = fresh_service.discover_families()
                assert result == {}

    def test_discover_finds_families(self, fresh_service: DeviceFamilyDiscoveryService) -> None:
        """Discovery finds IJarvisDeviceProtocol implementations."""
        mock_module = MagicMock()
        mock_module.MockProtocol = MockProtocol

        with patch("pkgutil.iter_modules") as mock_iter:
            mock_iter.return_value = [(None, "mock_adapter", None)]

            with patch("importlib.import_module") as mock_import:
                mock_import.return_value = mock_module

                with patch.object(MockProtocol, "validate_secrets", return_value=[]):
                    result = fresh_service.discover_families()

                    assert "mock" in result
                    assert isinstance(result["mock"], MockProtocol)

    def test_discover_skips_missing_secrets(
        self, fresh_service: DeviceFamilyDiscoveryService
    ) -> None:
        """Families with missing secrets are skipped."""
        mock_module = MagicMock()
        mock_module.MockCloudProtocol = MockCloudProtocol

        with patch("pkgutil.iter_modules") as mock_iter:
            mock_iter.return_value = [(None, "mock_cloud_adapter", None)]

            with patch("importlib.import_module") as mock_import:
                mock_import.return_value = mock_module

                with patch.object(
                    MockCloudProtocol,
                    "validate_secrets",
                    return_value=["MOCK_API_KEY"],
                ):
                    result = fresh_service.discover_families()
                    assert len(result) == 0

    def test_discover_skips_import_error(
        self, fresh_service: DeviceFamilyDiscoveryService
    ) -> None:
        """Families with missing pip packages are skipped (not errored)."""
        mock_device_families = MagicMock()
        mock_device_families.__path__ = ["/fake/path"]

        with patch.dict("sys.modules", {"device_families": mock_device_families}):
            with patch("pkgutil.iter_modules") as mock_iter:
                mock_iter.return_value = [(None, "missing_dep_adapter", None)]

                with patch("importlib.import_module") as mock_import:
                    mock_import.side_effect = ImportError("No module named 'some_lib'")

                    result = fresh_service.discover_families()
                    assert result == {}

    def test_discover_skips_base_module(
        self, fresh_service: DeviceFamilyDiscoveryService
    ) -> None:
        """The 'base' module is skipped during discovery."""
        mock_device_families = MagicMock()
        mock_device_families.__path__ = ["/fake/path"]

        with patch.dict("sys.modules", {"device_families": mock_device_families}):
            with patch("pkgutil.iter_modules") as mock_iter:
                mock_iter.return_value = [(None, "base", None)]

                with patch("importlib.import_module") as mock_import:
                    result = fresh_service.discover_families()
                    # importlib.import_module should NOT be called for "base"
                    mock_import.assert_not_called()
                    assert result == {}


# =============================================================================
# Getter tests
# =============================================================================


class TestGetFamily:
    def test_get_family_found(self, fresh_service: DeviceFamilyDiscoveryService) -> None:
        protocol = MockProtocol()
        fresh_service._families_cache = {"mock": protocol}
        fresh_service._discovered = True

        result = fresh_service.get_family("mock")
        assert result is protocol

    def test_get_family_not_found(self, fresh_service: DeviceFamilyDiscoveryService) -> None:
        fresh_service._families_cache = {}
        fresh_service._discovered = True

        result = fresh_service.get_family("nonexistent")
        assert result is None

    def test_get_family_triggers_discovery(
        self, fresh_service: DeviceFamilyDiscoveryService
    ) -> None:
        assert fresh_service._discovered is False

        with patch.object(fresh_service, "_do_discover_families") as mock_discover:
            mock_discover.return_value = {}
            fresh_service.get_family("any")
            mock_discover.assert_called_once()


class TestGetAllFamilies:
    def test_get_all_families(self, fresh_service: DeviceFamilyDiscoveryService) -> None:
        proto1 = MockProtocol()
        proto2 = MockCloudProtocol()
        fresh_service._families_cache = {"mock": proto1, "mock_cloud": proto2}
        fresh_service._discovered = True

        result = fresh_service.get_all_families()

        assert len(result) == 2
        assert result["mock"] is proto1
        assert result["mock_cloud"] is proto2

        # Modifying result doesn't affect cache
        result["extra"] = MagicMock()
        assert "extra" not in fresh_service._families_cache

    def test_get_all_triggers_discovery(
        self, fresh_service: DeviceFamilyDiscoveryService
    ) -> None:
        assert fresh_service._discovered is False

        with patch.object(fresh_service, "_do_discover_families") as mock_discover:
            mock_discover.return_value = {}
            fresh_service.get_all_families()
            mock_discover.assert_called_once()


# =============================================================================
# Singleton tests
# =============================================================================


class TestSingleton:
    def test_returns_same_instance(self) -> None:
        import utils.device_family_discovery_service as module
        module._device_family_discovery_service = None

        service1 = get_device_family_discovery_service()
        service2 = get_device_family_discovery_service()

        assert service1 is service2

        # Cleanup
        module._device_family_discovery_service = None


# =============================================================================
# Refresh tests
# =============================================================================


class TestRefresh:
    def test_refresh_resets_discovered_flag(
        self, fresh_service: DeviceFamilyDiscoveryService
    ) -> None:
        fresh_service._discovered = True

        with patch.object(fresh_service, "discover_families") as mock_discover:
            mock_discover.return_value = {}
            fresh_service.refresh()
            mock_discover.assert_called_once()


# =============================================================================
# Thread safety tests
# =============================================================================


class TestThreadSafety:
    def test_concurrent_discovery(self, fresh_service: DeviceFamilyDiscoveryService) -> None:
        """Multiple threads calling get_all_families should not cause issues."""
        import threading

        results: list[dict] = []
        errors: list[Exception] = []

        def _get_families() -> None:
            try:
                with patch.object(fresh_service, "_do_discover_families", return_value={}):
                    result = fresh_service.get_all_families()
                    results.append(result)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=_get_families) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert len(results) == 10


# =============================================================================
# Connection type / extended ABC tests
# =============================================================================


class TestIJarvisDeviceProtocolExtensions:
    def test_default_connection_type_is_lan(self) -> None:
        proto = MockProtocol()
        assert proto.connection_type == "lan"

    def test_cloud_connection_type(self) -> None:
        proto = MockCloudProtocol()
        assert proto.connection_type == "cloud"

    def test_default_required_secrets_empty(self) -> None:
        proto = MockProtocol()
        assert proto.required_secrets == []

    def test_cloud_protocol_has_secrets(self) -> None:
        proto = MockCloudProtocol()
        assert len(proto.required_secrets) == 1
        assert proto.required_secrets[0].key == "MOCK_API_KEY"

    def test_default_friendly_name(self) -> None:
        """Default friendly_name is title-cased protocol_name."""
        proto = MockProtocol()
        assert proto.friendly_name == "Mock"

    def test_default_friendly_name_with_underscores(self) -> None:
        proto = MockCloudProtocol()
        assert proto.friendly_name == "Mock Cloud"

    def test_default_description_is_empty(self) -> None:
        proto = MockProtocol()
        assert proto.description == ""

    def test_default_authentication_is_none(self) -> None:
        proto = MockProtocol()
        assert proto.authentication is None

    def test_store_auth_values_is_noop(self) -> None:
        """Default store_auth_values() does not raise."""
        proto = MockProtocol()
        proto.store_auth_values({"access_token": "tok_abc"})  # should not raise

    def test_discovered_device_optional_fields(self) -> None:
        """DiscoveredDevice works with only required fields (cloud device)."""
        dev = DiscoveredDevice(
            name="Smart Lock",
            domain="lock",
            manufacturer="Schlage",
            model="Encode Plus",
            protocol="schlage",
            entity_id="lock.front_door",
            cloud_id="device-abc-123",
        )
        assert dev.local_ip is None
        assert dev.mac_address is None
        assert dev.cloud_id == "device-abc-123"

    def test_discovered_device_all_fields(self) -> None:
        """DiscoveredDevice works with all fields (hybrid device)."""
        dev = DiscoveredDevice(
            name="Water Boiler",
            domain="switch",
            manufacturer="Govee",
            model="H7141",
            protocol="govee",
            entity_id="switch.water_boiler",
            local_ip="192.168.1.100",
            mac_address="aa:bb:cc:dd:ee:ff",
            cloud_id="govee-device-456",
        )
        assert dev.local_ip == "192.168.1.100"
        assert dev.mac_address == "aa:bb:cc:dd:ee:ff"
        assert dev.cloud_id == "govee-device-456"


# =============================================================================
# get_all_families_for_snapshot tests
# =============================================================================


class TestGetAllFamiliesForSnapshot:
    def test_returns_families_with_missing_secrets(
        self, fresh_service: DeviceFamilyDiscoveryService
    ) -> None:
        """Snapshot method includes families even when secrets are missing."""
        mock_module = MagicMock()
        mock_module.MockCloudProtocol = MockCloudProtocol

        mock_device_families = MagicMock()
        mock_device_families.__path__ = ["/fake/path"]

        with patch.dict("sys.modules", {"device_families": mock_device_families}):
            with patch("pkgutil.iter_modules") as mock_iter:
                mock_iter.return_value = [(None, "mock_cloud_adapter", None)]

                with patch("importlib.import_module") as mock_import:
                    mock_import.return_value = mock_module

                    result = fresh_service.get_all_families_for_snapshot()

                    assert "mock_cloud" in result
                    assert isinstance(result["mock_cloud"], MockCloudProtocol)

    def test_returns_families_without_secrets(
        self, fresh_service: DeviceFamilyDiscoveryService
    ) -> None:
        """Snapshot method includes LAN families with no secrets."""
        mock_module = MagicMock()
        mock_module.MockProtocol = MockProtocol

        mock_device_families = MagicMock()
        mock_device_families.__path__ = ["/fake/path"]

        with patch.dict("sys.modules", {"device_families": mock_device_families}):
            with patch("pkgutil.iter_modules") as mock_iter:
                mock_iter.return_value = [(None, "mock_adapter", None)]

                with patch("importlib.import_module") as mock_import:
                    mock_import.return_value = mock_module

                    result = fresh_service.get_all_families_for_snapshot()

                    assert "mock" in result
                    assert isinstance(result["mock"], MockProtocol)

    def test_skips_import_error(
        self, fresh_service: DeviceFamilyDiscoveryService
    ) -> None:
        """Snapshot method skips families with missing pip packages."""
        mock_device_families = MagicMock()
        mock_device_families.__path__ = ["/fake/path"]

        with patch.dict("sys.modules", {"device_families": mock_device_families}):
            with patch("pkgutil.iter_modules") as mock_iter:
                mock_iter.return_value = [(None, "missing_dep", None)]

                with patch("importlib.import_module") as mock_import:
                    mock_import.side_effect = ImportError("No module named 'some_lib'")

                    result = fresh_service.get_all_families_for_snapshot()
                    assert result == {}

    def test_skips_base_module(
        self, fresh_service: DeviceFamilyDiscoveryService
    ) -> None:
        """Snapshot method skips the 'base' module."""
        mock_device_families = MagicMock()
        mock_device_families.__path__ = ["/fake/path"]

        with patch.dict("sys.modules", {"device_families": mock_device_families}):
            with patch("pkgutil.iter_modules") as mock_iter:
                mock_iter.return_value = [(None, "base", None)]

                with patch("importlib.import_module") as mock_import:
                    result = fresh_service.get_all_families_for_snapshot()
                    mock_import.assert_not_called()
                    assert result == {}

    def test_no_caching(
        self, fresh_service: DeviceFamilyDiscoveryService
    ) -> None:
        """Snapshot method does not use or update the internal cache."""
        mock_module = MagicMock()
        mock_module.MockProtocol = MockProtocol

        mock_device_families = MagicMock()
        mock_device_families.__path__ = ["/fake/path"]

        with patch.dict("sys.modules", {"device_families": mock_device_families}):
            with patch("pkgutil.iter_modules") as mock_iter:
                mock_iter.return_value = [(None, "mock_adapter", None)]

                with patch("importlib.import_module") as mock_import:
                    mock_import.return_value = mock_module

                    fresh_service.get_all_families_for_snapshot()

                    # Internal cache should NOT be updated
                    assert fresh_service._families_cache == {}
                    assert fresh_service._discovered is False
