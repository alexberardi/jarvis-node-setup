"""Tests for JarvisDirectDeviceManager.

Tests property values, device collection via protocol adapters,
deduplication logic, empty families, and error handling.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.ijarvis_device_manager import DeviceManagerDevice
from device_families.base import DiscoveredDevice
from device_managers.jarvis_direct_manager import JarvisDirectDeviceManager, _to_manager_device


# =============================================================================
# Helpers
# =============================================================================


def _make_discovered(
    name: str = "Test Light",
    domain: str = "light",
    entity_id: str = "light.test",
    protocol: str = "lifx",
    local_ip: str | None = "192.168.1.10",
    mac_address: str | None = "AA:BB:CC:DD:EE:FF",
    cloud_id: str | None = None,
    is_controllable: bool = True,
) -> DiscoveredDevice:
    return DiscoveredDevice(
        name=name,
        domain=domain,
        manufacturer="TestMfg",
        model="TestModel",
        protocol=protocol,
        entity_id=entity_id,
        local_ip=local_ip,
        mac_address=mac_address,
        cloud_id=cloud_id,
        is_controllable=is_controllable,
    )


def _make_mock_protocol(
    name: str,
    devices: list[DiscoveredDevice] | Exception,
) -> MagicMock:
    """Create a mock IJarvisDeviceProtocol that returns the given devices or raises."""
    proto = MagicMock()
    proto.protocol_name = name
    if isinstance(devices, Exception):
        proto.discover = AsyncMock(side_effect=devices)
    else:
        proto.discover = AsyncMock(return_value=devices)
    return proto


# =============================================================================
# Property tests
# =============================================================================


class TestJarvisDirectManagerProperties:
    def test_name(self) -> None:
        mgr = JarvisDirectDeviceManager()
        assert mgr.name == "jarvis_direct"

    def test_friendly_name(self) -> None:
        mgr = JarvisDirectDeviceManager()
        assert mgr.friendly_name == "Jarvis Direct"

    def test_can_edit_devices(self) -> None:
        mgr = JarvisDirectDeviceManager()
        assert mgr.can_edit_devices is True

    def test_description(self) -> None:
        mgr = JarvisDirectDeviceManager()
        assert mgr.description != ""

    def test_required_secrets_empty(self) -> None:
        """Jarvis Direct has no required secrets (uses per-protocol secrets)."""
        mgr = JarvisDirectDeviceManager()
        assert mgr.required_secrets == []

    def test_authentication_none(self) -> None:
        mgr = JarvisDirectDeviceManager()
        assert mgr.authentication is None


# =============================================================================
# _to_manager_device tests
# =============================================================================


class TestToManagerDevice:
    def test_converts_discovered_to_manager_device(self) -> None:
        discovered = _make_discovered(
            name="Desk Lamp",
            domain="light",
            entity_id="light.desk_lamp",
            protocol="lifx",
            local_ip="192.168.1.50",
            mac_address="11:22:33:44:55:66",
        )
        result = _to_manager_device(discovered)

        assert isinstance(result, DeviceManagerDevice)
        assert result.name == "Desk Lamp"
        assert result.domain == "light"
        assert result.entity_id == "light.desk_lamp"
        assert result.protocol == "lifx"
        assert result.local_ip == "192.168.1.50"
        assert result.mac_address == "11:22:33:44:55:66"
        assert result.source == "direct"
        assert result.is_controllable is True

    def test_preserves_cloud_id(self) -> None:
        discovered = _make_discovered(cloud_id="cloud-abc", local_ip=None, mac_address=None)
        result = _to_manager_device(discovered)
        assert result.cloud_id == "cloud-abc"

    def test_preserves_device_class(self) -> None:
        discovered = _make_discovered()
        discovered.device_class = "dimmer"
        result = _to_manager_device(discovered)
        assert result.device_class == "dimmer"


# =============================================================================
# collect_devices tests
# =============================================================================


class TestCollectDevices:
    @pytest.mark.asyncio
    async def test_empty_families_returns_empty(self) -> None:
        """No configured device families returns empty list."""
        mgr = JarvisDirectDeviceManager()
        mock_discovery = MagicMock()
        mock_discovery.get_all_families.return_value = {}

        with patch(
            "device_managers.jarvis_direct_manager.get_device_family_discovery_service",
            return_value=mock_discovery,
        ):
            result = await mgr.collect_devices()
            assert result == []

    @pytest.mark.asyncio
    async def test_single_protocol_returns_devices(self) -> None:
        """Single protocol adapter returns its discovered devices."""
        devices = [
            _make_discovered(name="Light 1", entity_id="light.one", mac_address="AA:BB:CC:01:02:03"),
            _make_discovered(name="Light 2", entity_id="light.two", mac_address="AA:BB:CC:04:05:06"),
        ]
        proto = _make_mock_protocol("lifx", devices)

        mock_discovery = MagicMock()
        mock_discovery.get_all_families.return_value = {"lifx": proto}

        mgr = JarvisDirectDeviceManager()
        with patch(
            "device_managers.jarvis_direct_manager.get_device_family_discovery_service",
            return_value=mock_discovery,
        ):
            result = await mgr.collect_devices()
            assert len(result) == 2
            assert all(isinstance(d, DeviceManagerDevice) for d in result)
            assert result[0].name == "Light 1"
            assert result[1].name == "Light 2"

    @pytest.mark.asyncio
    async def test_multiple_protocols_combined(self) -> None:
        """Devices from multiple protocols are combined."""
        lifx_devices = [_make_discovered(name="LIFX Bulb", entity_id="light.lifx", mac_address="AA:01")]
        kasa_devices = [_make_discovered(name="Kasa Plug", entity_id="switch.kasa", mac_address="BB:02", domain="switch")]

        lifx_proto = _make_mock_protocol("lifx", lifx_devices)
        kasa_proto = _make_mock_protocol("kasa", kasa_devices)

        mock_discovery = MagicMock()
        mock_discovery.get_all_families.return_value = {"lifx": lifx_proto, "kasa": kasa_proto}

        mgr = JarvisDirectDeviceManager()
        with patch(
            "device_managers.jarvis_direct_manager.get_device_family_discovery_service",
            return_value=mock_discovery,
        ):
            result = await mgr.collect_devices()
            assert len(result) == 2
            names = {d.name for d in result}
            assert "LIFX Bulb" in names
            assert "Kasa Plug" in names

    @pytest.mark.asyncio
    async def test_protocol_error_handled_gracefully(self) -> None:
        """Protocol errors are logged but don't raise; other protocols still work."""
        good_devices = [_make_discovered(name="Good Light", entity_id="light.good", mac_address="CC:03")]
        good_proto = _make_mock_protocol("lifx", good_devices)
        bad_proto = _make_mock_protocol("broken", RuntimeError("Connection refused"))

        mock_discovery = MagicMock()
        mock_discovery.get_all_families.return_value = {"lifx": good_proto, "broken": bad_proto}

        mgr = JarvisDirectDeviceManager()
        with patch(
            "device_managers.jarvis_direct_manager.get_device_family_discovery_service",
            return_value=mock_discovery,
        ):
            result = await mgr.collect_devices()
            # Only good protocol's devices survive
            assert len(result) == 1
            assert result[0].name == "Good Light"

    @pytest.mark.asyncio
    async def test_all_protocols_fail_returns_empty(self) -> None:
        """When all protocols fail, an empty list is returned (no raise)."""
        bad_proto = _make_mock_protocol("broken", RuntimeError("fail"))

        mock_discovery = MagicMock()
        mock_discovery.get_all_families.return_value = {"broken": bad_proto}

        mgr = JarvisDirectDeviceManager()
        with patch(
            "device_managers.jarvis_direct_manager.get_device_family_discovery_service",
            return_value=mock_discovery,
        ):
            result = await mgr.collect_devices()
            assert result == []


# =============================================================================
# Deduplication tests
# =============================================================================


class TestDeduplication:
    @pytest.mark.asyncio
    async def test_dedup_by_mac_address(self) -> None:
        """Devices with the same MAC address are deduplicated."""
        dev1 = _make_discovered(name="LIFX Bulb", entity_id="light.lifx_bulb", mac_address="AA:BB:CC:DD:EE:FF", protocol="lifx")
        dev2 = _make_discovered(name="Same Bulb via Kasa", entity_id="light.kasa_bulb", mac_address="aa:bb:cc:dd:ee:ff", protocol="kasa")

        proto1 = _make_mock_protocol("lifx", [dev1])
        proto2 = _make_mock_protocol("kasa", [dev2])

        mock_discovery = MagicMock()
        mock_discovery.get_all_families.return_value = {"lifx": proto1, "kasa": proto2}

        mgr = JarvisDirectDeviceManager()
        with patch(
            "device_managers.jarvis_direct_manager.get_device_family_discovery_service",
            return_value=mock_discovery,
        ):
            result = await mgr.collect_devices()
            assert len(result) == 1
            # Last one wins (kasa overwrites lifx for same mac)
            assert result[0].name == "Same Bulb via Kasa"

    @pytest.mark.asyncio
    async def test_dedup_by_ip_when_no_mac(self) -> None:
        """Devices with no MAC but same IP are deduplicated."""
        dev1 = _make_discovered(name="Dev A", entity_id="light.a", mac_address=None, local_ip="192.168.1.10")
        dev2 = _make_discovered(name="Dev B", entity_id="light.b", mac_address=None, local_ip="192.168.1.10")

        proto = _make_mock_protocol("test", [dev1, dev2])

        mock_discovery = MagicMock()
        mock_discovery.get_all_families.return_value = {"test": proto}

        mgr = JarvisDirectDeviceManager()
        with patch(
            "device_managers.jarvis_direct_manager.get_device_family_discovery_service",
            return_value=mock_discovery,
        ):
            result = await mgr.collect_devices()
            assert len(result) == 1

    @pytest.mark.asyncio
    async def test_dedup_by_cloud_id_when_no_mac_or_ip(self) -> None:
        """Devices with no MAC/IP but same cloud_id are deduplicated."""
        dev1 = _make_discovered(name="Cloud A", entity_id="lock.a", mac_address=None, local_ip=None, cloud_id="cloud-xyz")
        dev2 = _make_discovered(name="Cloud B", entity_id="lock.b", mac_address=None, local_ip=None, cloud_id="cloud-xyz")

        proto = _make_mock_protocol("test", [dev1, dev2])

        mock_discovery = MagicMock()
        mock_discovery.get_all_families.return_value = {"test": proto}

        mgr = JarvisDirectDeviceManager()
        with patch(
            "device_managers.jarvis_direct_manager.get_device_family_discovery_service",
            return_value=mock_discovery,
        ):
            result = await mgr.collect_devices()
            assert len(result) == 1

    @pytest.mark.asyncio
    async def test_dedup_by_entity_id_fallback(self) -> None:
        """Devices with no MAC/IP/cloud_id deduplicate by entity_id."""
        dev1 = _make_discovered(name="Sensor A", entity_id="sensor.temp", mac_address=None, local_ip=None, cloud_id=None)
        dev2 = _make_discovered(name="Sensor B", entity_id="sensor.temp", mac_address=None, local_ip=None, cloud_id=None)

        proto = _make_mock_protocol("test", [dev1, dev2])

        mock_discovery = MagicMock()
        mock_discovery.get_all_families.return_value = {"test": proto}

        mgr = JarvisDirectDeviceManager()
        with patch(
            "device_managers.jarvis_direct_manager.get_device_family_discovery_service",
            return_value=mock_discovery,
        ):
            result = await mgr.collect_devices()
            assert len(result) == 1

    @pytest.mark.asyncio
    async def test_different_macs_not_deduped(self) -> None:
        """Devices with different MAC addresses are NOT deduplicated."""
        dev1 = _make_discovered(name="Light 1", entity_id="light.one", mac_address="AA:11")
        dev2 = _make_discovered(name="Light 2", entity_id="light.two", mac_address="BB:22")

        proto = _make_mock_protocol("test", [dev1, dev2])

        mock_discovery = MagicMock()
        mock_discovery.get_all_families.return_value = {"test": proto}

        mgr = JarvisDirectDeviceManager()
        with patch(
            "device_managers.jarvis_direct_manager.get_device_family_discovery_service",
            return_value=mock_discovery,
        ):
            result = await mgr.collect_devices()
            assert len(result) == 2
