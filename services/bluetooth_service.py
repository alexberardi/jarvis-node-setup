"""
Bluetooth service for Jarvis Node.

Orchestrates Bluetooth pairing flows, audio routing via PulseAudio,
device persistence (via command_data table), and auto-reconnect on startup.
"""

import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from jarvis_log_client import JarvisLogger

from core.platform_abstraction import BluetoothDevice, BluetoothProvider
from db import SessionLocal
from repositories.command_data_repository import CommandDataRepository

logger = JarvisLogger(service="jarvis-node")

COMMAND_NAME = "bluetooth"


class BluetoothRole(Enum):
    SINK = "sink"       # Phone → Pi speaker
    SOURCE = "source"   # Pi → BT speaker
    BRIDGE = "bridge"   # Phone → Pi → BT speaker


@dataclass
class BluetoothPairResult:
    success: bool
    device: BluetoothDevice | None = None
    message: str = ""


class BluetoothService:
    """Orchestrates Bluetooth pairing, audio routing, and persistence."""

    def __init__(self, provider: BluetoothProvider) -> None:
        self._provider = provider

    def is_available(self) -> bool:
        """Check if Bluetooth hardware is available."""
        return self._provider.is_available()

    def scan_for_devices(self, timeout: float = 10.0) -> list[BluetoothDevice]:
        """Scan for nearby Bluetooth devices."""
        return self._provider.scan(timeout=timeout)

    def make_discoverable(self, timeout: int = 120) -> bool:
        """Make the Pi discoverable so phones can find it."""
        return self._provider.set_discoverable(enabled=True, timeout=timeout)

    def pair_and_connect(self, mac_address: str, role: BluetoothRole = BluetoothRole.SINK) -> BluetoothPairResult:
        """Pair, trust, connect to a device and configure audio routing."""
        # Trust first for headless auto-accept
        if not self._provider.trust(mac_address):
            return BluetoothPairResult(success=False, message="Failed to trust device")

        if not self._provider.pair(mac_address):
            return BluetoothPairResult(success=False, message="Failed to pair device")

        if not self._provider.connect(mac_address):
            return BluetoothPairResult(success=False, message="Paired but failed to connect")

        # Configure audio route
        self.configure_audio_route(mac_address, role)

        # Get device info for persistence
        paired = self._provider.get_paired_devices()
        device = next((d for d in paired if d.mac_address == mac_address), None)
        if device is None:
            device = BluetoothDevice(name=mac_address, mac_address=mac_address, paired=True, connected=True)

        # Persist to DB
        self._save_device(device, role)

        return BluetoothPairResult(
            success=True,
            device=device,
            message=f"Connected to {device.name}",
        )

    def connect_device(self, mac_address: str) -> bool:
        """Connect to an already-paired device."""
        return self._provider.connect(mac_address)

    def disconnect_device(self, mac_address: str) -> bool:
        """Disconnect a device."""
        return self._provider.disconnect(mac_address)

    def forget_device(self, mac_address: str) -> bool:
        """Remove a device from BlueZ and the local DB."""
        self._provider.remove(mac_address)
        self._delete_device(mac_address)
        return True

    def configure_audio_route(self, mac_address: str, role: BluetoothRole) -> bool:
        """Configure PulseAudio routing for the given role."""
        mac_underscored = mac_address.replace(":", "_")

        try:
            if role == BluetoothRole.SOURCE:
                # Pi → BT speaker: set as default sink
                sink_name = f"bluez_sink.{mac_underscored}.a2dp_sink"
                result = subprocess.run(
                    ["pactl", "set-default-sink", sink_name],
                    capture_output=True, text=True, timeout=5.0,
                )
                if result.returncode != 0:
                    logger.warning("Failed to set BT default sink", sink=sink_name, error=result.stderr)
                    return False
                logger.info("Audio routing: Pi → BT speaker", sink=sink_name)
                return True

            elif role == BluetoothRole.BRIDGE:
                # Phone → Pi → BT speaker: loopback module
                source_name = f"bluez_source.{mac_underscored}.a2dp_source"
                result = subprocess.run(
                    ["pactl", "load-module", "module-loopback",
                     f"source={source_name}"],
                    capture_output=True, text=True, timeout=5.0,
                )
                if result.returncode != 0:
                    logger.warning("Failed to set up loopback", error=result.stderr)
                    return False
                logger.info("Audio routing: bridge mode", source=source_name)
                return True

            else:
                # SINK (phone → Pi): PulseAudio handles A2DP Sink automatically
                logger.info("Audio routing: phone → Pi speaker (auto)")
                return True

        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            logger.error("Audio route configuration failed", error=str(e))
            return False

    def get_paired_devices(self) -> list[BluetoothDevice]:
        """Get paired devices from BlueZ."""
        return self._provider.get_paired_devices()

    def get_connected_devices(self) -> list[BluetoothDevice]:
        """Get currently connected devices."""
        return self._provider.get_connected_devices()

    def save_device(self, device: BluetoothDevice, role: BluetoothRole) -> None:
        """Public wrapper for persisting a device record."""
        self._save_device(device, role)

    def get_known_devices(self) -> list[dict[str, Any]]:
        """Get all persisted Bluetooth device records from command_data."""
        session = SessionLocal()
        try:
            repo = CommandDataRepository(session)
            return repo.get_all(COMMAND_NAME)
        finally:
            session.close()

    def get_status(self) -> dict:
        """Get current Bluetooth status: connected and paired devices."""
        connected = self._provider.get_connected_devices()
        paired = self._provider.get_paired_devices()
        return {
            "available": self._provider.is_available(),
            "connected": [
                {"name": d.name, "mac": d.mac_address, "type": d.device_type}
                for d in connected
            ],
            "paired": [
                {"name": d.name, "mac": d.mac_address, "type": d.device_type, "connected": d.connected}
                for d in paired
            ],
        }

    def reconnect_known_devices(self) -> int:
        """Reconnect all auto_connect devices. Returns count of successful reconnections."""
        records = self.get_known_devices()

        count = 0
        for record in records:
            if not record.get("auto_connect", True):
                continue
            mac = record["mac_address"]
            name = record.get("name", mac)
            role_str = record.get("role", "sink")
            if self._provider.connect(mac):
                self.configure_audio_route(mac, BluetoothRole(role_str))
                count += 1
                logger.info("Auto-reconnected BT device", name=name, mac=mac)
            else:
                logger.warning("Auto-reconnect failed", name=name, mac=mac)
        return count

    def _save_device(self, device: BluetoothDevice, role: BluetoothRole) -> None:
        """Persist or update a device record in command_data."""
        session = SessionLocal()
        try:
            repo = CommandDataRepository(session)
            repo.save(
                command_name=COMMAND_NAME,
                data_key=device.mac_address,
                data={
                    "mac_address": device.mac_address,
                    "name": device.name,
                    "device_type": device.device_type,
                    "role": role.value,
                    "auto_connect": True,
                    "last_connected": datetime.now(timezone.utc).isoformat(),
                },
            )
        except Exception as e:
            logger.error("Failed to save BT device record", error=str(e))
        finally:
            session.close()

    def _delete_device(self, mac_address: str) -> None:
        """Delete a device record from command_data."""
        session = SessionLocal()
        try:
            repo = CommandDataRepository(session)
            repo.delete(COMMAND_NAME, mac_address)
        except Exception as e:
            logger.error("Failed to delete BT device record", error=str(e))
        finally:
            session.close()
