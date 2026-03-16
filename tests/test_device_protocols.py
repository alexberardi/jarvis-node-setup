"""Tests for device protocol adapters and device scanner/control services."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.device_protocols.base import (
    DeviceControlResult,
    DeviceProtocol,
    DiscoveredDevice,
)
from services.device_scanner_service import DeviceScannerService
from services.direct_device_service import DeviceRecord, DirectDeviceService


# =============================================================================
# Base protocol tests
# =============================================================================


class TestDiscoveredDevice:
    def test_create_basic(self) -> None:
        dev = DiscoveredDevice(
            name="Kitchen Light",
            domain="light",
            manufacturer="LIFX",
            model="LIFX A19",
            protocol="lifx",
            local_ip="192.168.1.50",
            mac_address="aa:bb:cc:dd:ee:ff",
            entity_id="light.kitchen_light",
        )
        assert dev.name == "Kitchen Light"
        assert dev.domain == "light"
        assert dev.protocol == "lifx"
        assert dev.is_controllable is True
        assert dev.extra == {}

    def test_create_with_extras(self) -> None:
        dev = DiscoveredDevice(
            name="Smart Plug",
            domain="switch",
            manufacturer="TP-Link",
            model="HS100",
            protocol="kasa",
            local_ip="192.168.1.51",
            mac_address="11:22:33:44:55:66",
            entity_id="switch.smart_plug",
            device_class="outlet",
            extra={"device_type": "plug"},
        )
        assert dev.device_class == "outlet"
        assert dev.extra["device_type"] == "plug"


class TestDeviceControlResult:
    def test_success(self) -> None:
        result = DeviceControlResult(
            success=True, entity_id="light.bedroom", action="turn_on",
        )
        assert result.success is True
        assert result.error is None

    def test_failure(self) -> None:
        result = DeviceControlResult(
            success=False, entity_id="light.bedroom", action="turn_on",
            error="Connection refused",
        )
        assert result.success is False
        assert "Connection refused" in result.error


# =============================================================================
# Device scanner service tests
# =============================================================================


class TestDeviceScannerService:
    def _make_scanner(self) -> DeviceScannerService:
        return DeviceScannerService(
            cc_base_url="http://localhost:7703",
            node_id="test-node",
            api_key="test-key",
            household_id="test-household",
        )

    @pytest.mark.asyncio
    async def test_scan_no_protocols(self) -> None:
        scanner = self._make_scanner()
        scanner._protocols = []
        devices = await scanner.scan()
        assert devices == []

    @pytest.mark.asyncio
    async def test_scan_deduplicates_by_mac(self) -> None:
        scanner = self._make_scanner()

        mock_protocol = MagicMock(spec=DeviceProtocol)
        mock_protocol.protocol_name = "test"
        mock_protocol.discover = AsyncMock(return_value=[
            DiscoveredDevice(
                name="Light A", domain="light", manufacturer="Test",
                model="T1", protocol="test", local_ip="192.168.1.50",
                mac_address="aa:bb:cc:dd:ee:ff", entity_id="light.light_a",
            ),
            DiscoveredDevice(
                name="Light A Updated", domain="light", manufacturer="Test",
                model="T1", protocol="test", local_ip="192.168.1.50",
                mac_address="AA:BB:CC:DD:EE:FF", entity_id="light.light_a",
            ),
        ])
        scanner._protocols = [mock_protocol]

        devices = await scanner.scan()
        assert len(devices) == 1
        assert devices[0].name == "Light A Updated"  # Later wins

    @pytest.mark.asyncio
    async def test_scan_handles_protocol_error(self) -> None:
        scanner = self._make_scanner()

        mock_protocol = MagicMock(spec=DeviceProtocol)
        mock_protocol.protocol_name = "broken"
        mock_protocol.discover = AsyncMock(side_effect=RuntimeError("Network error"))
        scanner._protocols = [mock_protocol]

        devices = await scanner.scan()
        assert devices == []

    def test_resolve_entity_collisions(self) -> None:
        scanner = self._make_scanner()
        devices = [
            DiscoveredDevice(
                name="Light", domain="light", manufacturer="A", model="X",
                protocol="lifx", local_ip="1.1.1.1", mac_address="aa:aa:aa:aa:aa:aa",
                entity_id="light.living_room",
            ),
            DiscoveredDevice(
                name="Light", domain="light", manufacturer="B", model="Y",
                protocol="kasa", local_ip="1.1.1.2", mac_address="bb:bb:bb:bb:bb:bb",
                entity_id="light.living_room",
            ),
        ]
        resolved = scanner._resolve_entity_collisions(devices)
        entity_ids = [d.entity_id for d in resolved]
        assert "light.living_room" in entity_ids
        assert "light.living_room_kasa" in entity_ids

    @pytest.mark.asyncio
    async def test_report_to_cc_empty(self) -> None:
        scanner = self._make_scanner()
        result = await scanner.report_to_cc([])
        assert result == {"created": 0, "updated": 0}

    @pytest.mark.asyncio
    async def test_report_to_cc_success(self) -> None:
        scanner = self._make_scanner()
        devices = [
            DiscoveredDevice(
                name="Light", domain="light", manufacturer="LIFX", model="A19",
                protocol="lifx", local_ip="192.168.1.50",
                mac_address="aa:bb:cc:dd:ee:ff", entity_id="light.kitchen",
            ),
        ]

        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {"created": 1, "updated": 0}
        mock_response.raise_for_status = MagicMock()

        with patch("services.device_scanner_service.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            result = await scanner.report_to_cc(devices)
            assert result == {"created": 1, "updated": 0}

            # Verify the request payload
            call_args = mock_client.post.call_args
            payload = call_args[1]["json"]
            assert len(payload["devices"]) == 1
            assert payload["devices"][0]["source"] == "direct"
            assert payload["devices"][0]["protocol"] == "lifx"


# =============================================================================
# Direct device service tests
# =============================================================================


class TestDirectDeviceService:
    def _make_service(self) -> DirectDeviceService:
        svc = DirectDeviceService(
            cc_base_url="http://localhost:7703",
            node_id="test-node",
            api_key="test-key",
            household_id="test-household",
        )
        svc._protocols = {}  # Clear to avoid import issues
        return svc

    def test_register_and_lookup(self) -> None:
        svc = self._make_service()
        record = DeviceRecord(
            entity_id="light.kitchen",
            protocol="lifx",
            local_ip="192.168.1.50",
            mac_address="aa:bb:cc:dd:ee:ff",
            domain="light",
            name="Kitchen Light",
        )
        svc.register_device(record)

        assert svc.is_direct_device("light.kitchen") is True
        assert svc.is_direct_device("light.unknown") is False
        assert svc.get_device("light.kitchen") == record
        assert len(svc.list_devices()) == 1

    @pytest.mark.asyncio
    async def test_control_device_not_found(self) -> None:
        svc = self._make_service()
        svc._cc_base_url = ""  # Disable CC refresh

        result = await svc.control_device("light.nonexistent", "turn_on")
        assert result.success is False
        assert "not found" in result.error.lower()

    @pytest.mark.asyncio
    async def test_control_device_no_adapter(self) -> None:
        svc = self._make_service()
        svc.register_device(DeviceRecord(
            entity_id="light.kitchen", protocol="unknown_proto",
            local_ip="192.168.1.50", mac_address="aa:bb:cc:dd:ee:ff",
            domain="light", name="Kitchen Light",
        ))

        result = await svc.control_device("light.kitchen", "turn_on")
        assert result.success is False
        assert "No adapter" in result.error

    @pytest.mark.asyncio
    async def test_control_device_success(self) -> None:
        svc = self._make_service()
        svc.register_device(DeviceRecord(
            entity_id="light.kitchen", protocol="lifx",
            local_ip="192.168.1.50", mac_address="aa:bb:cc:dd:ee:ff",
            domain="light", name="Kitchen Light",
        ))

        mock_adapter = MagicMock(spec=DeviceProtocol)
        mock_adapter.control = AsyncMock(return_value=DeviceControlResult(
            success=True, entity_id="light.kitchen", action="turn_on",
        ))
        svc._protocols["lifx"] = mock_adapter

        result = await svc.control_device("light.kitchen", "turn_on")
        assert result.success is True
        mock_adapter.control.assert_awaited_once()

    def test_get_context_data(self) -> None:
        svc = self._make_service()
        svc.register_device(DeviceRecord(
            entity_id="light.kitchen", protocol="lifx",
            local_ip="192.168.1.50", mac_address="aa:bb:cc:dd:ee:ff",
            domain="light", name="Kitchen Light",
        ))
        svc.register_device(DeviceRecord(
            entity_id="switch.plug", protocol="kasa",
            local_ip="192.168.1.51", mac_address="11:22:33:44:55:66",
            domain="switch", name="Smart Plug",
        ))

        context = svc.get_context_data()
        assert "device_controls" in context
        assert len(context["device_controls"]["light"]) == 1
        assert len(context["device_controls"]["switch"]) == 1
        assert context["device_controls"]["light"][0]["source"] == "direct"

    @pytest.mark.asyncio
    async def test_refresh_from_cc(self) -> None:
        svc = self._make_service()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {
                "entity_id": "light.kitchen",
                "name": "Kitchen Light",
                "domain": "light",
                "source": "direct",
                "protocol": "lifx",
                "local_ip": "192.168.1.50",
                "mac_address": "aa:bb:cc:dd:ee:ff",
            },
            {
                "entity_id": "light.ha_light",
                "name": "HA Light",
                "domain": "light",
                "source": "home_assistant",
            },
        ]
        mock_response.raise_for_status = MagicMock()

        with patch("services.direct_device_service.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_client

            count = await svc.refresh_from_cc()
            assert count == 1  # Only direct devices
            assert svc.is_direct_device("light.kitchen") is True
            assert svc.is_direct_device("light.ha_light") is False
