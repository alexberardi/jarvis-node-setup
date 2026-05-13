"""Handle Bluetooth scan/pair/disconnect/discoverable requests from CC via MQTT.

Flow:
1. CC publishes to jarvis/nodes/{node_id}/bluetooth-scan with {request_id, role}
2. This handler runs bluetoothctl scan via the platform BluetoothProvider
3. Results are POSTed back to CC for mobile to poll

Similarly for pair, disconnect, and discoverable actions.
"""

from typing import Any

from jarvis_log_client import JarvisLogger

from clients.rest_client import RestClient
from core.platform_abstraction import BluetoothDevice, get_bluetooth_provider
from utils.service_discovery import get_command_center_url

logger = JarvisLogger(service="jarvis-node")

SCAN_TIMEOUT: float = 12.0


# =============================================================================
# Scan
# =============================================================================


def run_bluetooth_scan_and_upload(request_id: str, role: str) -> None:
    """Run Bluetooth scan and upload results to CC. Runs in background thread."""
    try:
        provider = get_bluetooth_provider()

        if not provider.is_available():
            _upload_scan_error(request_id, "Bluetooth hardware not available")
            return

        devices = provider.scan(timeout=SCAN_TIMEOUT)
        logger.info(
            "Bluetooth scan complete",
            request_id=request_id[:8],
            device_count=len(devices),
        )

        _upload_scan_results(request_id, devices)

    except Exception as e:
        logger.error("Bluetooth scan handler failed", request_id=request_id[:8], error=str(e))
        _upload_scan_error(request_id, str(e))


def _upload_scan_results(request_id: str, devices: list[BluetoothDevice]) -> None:
    """POST scan results to CC."""
    cc_url = get_command_center_url()
    if not cc_url:
        logger.error("Cannot upload BT scan results: CC URL not resolved")
        return

    from utils.config_service import Config
    node_id: str = Config.get_str("node_id", "") or ""

    url = f"{cc_url.rstrip('/')}/api/v0/nodes/{node_id}/bluetooth-scan/{request_id}/results"

    device_dicts: list[dict[str, Any]] = [
        {
            "name": d.name,
            "mac_address": d.mac_address,
            "device_type": d.device_type,
            "paired": d.paired,
            "connected": d.connected,
        }
        for d in devices
    ]

    result = RestClient.post(url, data={"devices": device_dicts}, timeout=15)
    if result:
        logger.info("BT scan results uploaded", request_id=request_id[:8], device_count=len(devices))
    else:
        logger.error("Failed to upload BT scan results", request_id=request_id[:8])


def _upload_scan_error(request_id: str, error_message: str) -> None:
    """POST error to CC for a failed scan."""
    cc_url = get_command_center_url()
    if not cc_url:
        return

    from utils.config_service import Config
    node_id: str = Config.get_str("node_id", "") or ""

    url = f"{cc_url.rstrip('/')}/api/v0/nodes/{node_id}/bluetooth-scan/{request_id}/results"
    RestClient.post(url, data={"devices": [], "error": error_message}, timeout=10)


# =============================================================================
# Pair
# =============================================================================


def run_bluetooth_pair_and_upload(request_id: str, mac_address: str, role: str) -> None:
    """Pair + connect + configure audio for a BT device. Runs in background thread.

    Always treats this as an explicit user pair intent — they put the
    buds in pairing mode and tapped "Pair" from the Hardware tab. So:

      remove → pair (auto-rescans cache if needed) → trust → connect

    Why not try a connect first when the bond looks intact? Because
    the connect attempt itself (even when it fails) burns the peer's
    pairing-mode window. Many earbuds (Arctis included) interpret a
    fleeting ACL link as "I'm now connected, exit pairing mode" — by
    the time we'd fall back to fresh pair, they've gone dark.

    The "remove first" cost is real but acceptable: a working bond
    that the user explicitly chose to re-pair gets rebuilt cleanly,
    and a stale bond gets replaced. Reconnect of a paired-and-quiet
    device is the auto-reconnect loop's job, not this handler's.
    """
    try:
        provider = get_bluetooth_provider()

        if not provider.is_available():
            _upload_pair_error(request_id, "Bluetooth hardware not available")
            return

        # Only clear the bond if there IS one. For fresh devices, the
        # scan just populated bluez's cache with the peer — calling
        # remove() here discards that cache entry and forces pair() to
        # do another scan to repopulate, which often misses the peer's
        # pairing-mode window (typically ~60s on most earbuds). This
        # was the dominant cause of "device not discoverable" failures
        # right after the user tapped Pair on a fresh scan result.
        # When a stale bond does exist (re-pair flow), we still need
        # the remove + rescan dance to clear it.
        already_paired = any(
            d.mac_address.upper() == mac_address.upper()
            for d in provider.get_paired_devices()
        )
        if already_paired:
            provider.remove(mac_address)

        # pair() auto-rescans bluez's cache if the device isn't known.
        # The peer must still be in pairing mode (LED flashing) — if
        # they've timed out, the rescan returns "not discoverable" and
        # we surface a clear error.
        if not provider.pair(mac_address):
            _upload_pair_error(
                request_id,
                "Failed to pair — make sure the device is in pairing mode",
            )
            return

        provider.trust(mac_address)  # post-pair trust now that bluez knows the device

        if not provider.connect(mac_address):
            _upload_pair_error(request_id, "Paired but failed to connect")
            return

        # Configure audio routing via BluetoothService if available
        _configure_audio(mac_address, role)

        # Get device name. get_paired_devices() can miss devices whose
        # cached metadata hasn't refreshed since pairing; fall back to a
        # direct bluetoothctl info lookup before resorting to the MAC.
        paired = provider.get_paired_devices()
        device = next((d for d in paired if d.mac_address == mac_address), None)
        if device:
            device_name = device.name
        else:
            device_name = _resolve_device_name(provider, mac_address) or mac_address

        # Persist device for auto-reconnect
        _persist_device(mac_address, device_name, role)

        logger.info("Bluetooth pair complete", request_id=request_id[:8], device=device_name)
        _upload_pair_result(request_id, success=True, device_name=device_name)

    except Exception as e:
        logger.error("Bluetooth pair handler failed", request_id=request_id[:8], error=str(e))
        _upload_pair_error(request_id, str(e))


def _configure_audio(mac_address: str, role: str) -> None:
    """Log the audio routing intent. Does NOT change the default sink.

    PulseAudio's bluez module automatically creates a sink for connected
    BT audio devices. Music commands route to it explicitly; TTS stays
    on the default (local) sink so Jarvis voice always comes from the
    room speaker.
    """
    mac_underscored = mac_address.replace(":", "_")

    if role == "source":
        sink_name = f"bluez_sink.{mac_underscored}.a2dp_sink"
        logger.info("BT audio sink available for music routing", sink=sink_name)
    elif role == "sink":
        logger.info("BT audio source available (phone → Pi)", source=mac_underscored)


def _persist_device(mac_address: str, device_name: str, role: str) -> None:
    """Save device for auto-reconnect on boot.

    Preserves the existing ``auto_connect`` flag if the user previously
    toggled it off (via the bluetooth-set-auto-connect MQTT command);
    otherwise defaults to True so freshly paired devices reconnect on
    boot like AirPods do.
    """
    try:
        from jarvis_command_sdk import JarvisStorage
        from datetime import datetime, timezone

        storage = JarvisStorage("bluetooth")
        existing = storage.get(mac_address) or {}
        # Honor the user's prior auto_connect choice. New device → True
        # (parity with consumer BT stacks); existing device → keep prior
        # setting so toggling off in the mobile UI persists across re-pairs.
        auto_connect = existing.get("auto_connect", True)

        storage.save(
            mac_address,
            {
                "mac_address": mac_address,
                "name": device_name,
                "role": role,
                "auto_connect": auto_connect,
                "last_connected": datetime.now(timezone.utc).isoformat(),
            },
        )
    except Exception as e:
        logger.warning("Failed to persist BT device", error=str(e))


def _upload_pair_result(request_id: str, success: bool, device_name: str | None = None) -> None:
    """POST pair result to CC."""
    cc_url = get_command_center_url()
    if not cc_url:
        return

    from utils.config_service import Config
    node_id: str = Config.get_str("node_id", "") or ""

    url = f"{cc_url.rstrip('/')}/api/v0/nodes/{node_id}/bluetooth/pair/{request_id}/results"
    RestClient.post(url, data={"success": success, "device_name": device_name}, timeout=10)


def _upload_pair_error(request_id: str, error_message: str) -> None:
    """POST error to CC for a failed pair."""
    cc_url = get_command_center_url()
    if not cc_url:
        return

    from utils.config_service import Config
    node_id: str = Config.get_str("node_id", "") or ""

    url = f"{cc_url.rstrip('/')}/api/v0/nodes/{node_id}/bluetooth/pair/{request_id}/results"
    RestClient.post(url, data={"success": False, "error": error_message}, timeout=10)


# =============================================================================
# Disconnect
# =============================================================================


def run_bluetooth_disconnect(mac_address: str) -> None:
    """Disconnect a Bluetooth device. Runs in background thread."""
    try:
        provider = get_bluetooth_provider()
        if provider.disconnect(mac_address):
            logger.info("Bluetooth device disconnected", mac=mac_address)
        else:
            logger.warning("Failed to disconnect BT device", mac=mac_address)
    except Exception as e:
        logger.error("Bluetooth disconnect failed", mac=mac_address, error=str(e))


# =============================================================================
# Release / Forget
# =============================================================================


def run_bluetooth_release(mac_address: str, forget: bool = False) -> None:
    """Release a device for use elsewhere (e.g., the user's phone).

    Default mode: disconnect + flip ``auto_connect`` to False so the
    10-min reconnect loop won't grab the device again. Pairing stays
    intact so the user can reconnect with one tap.

    With ``forget=True``: also call ``provider.remove()`` to fully
    unpair (deletes ``/var/lib/bluetooth/<adapter>/<mac>/``) and remove
    the JarvisStorage record. The user must re-pair from scratch.
    """
    try:
        provider = get_bluetooth_provider()

        # Disconnect first so we don't fight bluez during remove.
        provider.disconnect(mac_address)

        if forget:
            provider.remove(mac_address)
            try:
                from jarvis_command_sdk import JarvisStorage
                JarvisStorage("bluetooth").delete(mac_address)
            except Exception as e:
                logger.warning("Failed to delete BT storage record",
                               mac=mac_address, error=str(e))
            logger.info("Bluetooth device forgotten", mac=mac_address)
            return

        # Soft release: keep pairing, disable auto-reconnect.
        try:
            from jarvis_command_sdk import JarvisStorage
            storage = JarvisStorage("bluetooth")
            record = storage.get(mac_address)
            if record:
                record["auto_connect"] = False
                storage.save(mac_address, record)
        except Exception as e:
            logger.warning("Failed to clear auto_connect flag",
                           mac=mac_address, error=str(e))
        logger.info("Bluetooth device released (auto-reconnect off)", mac=mac_address)
    except Exception as e:
        logger.error("Bluetooth release failed", mac=mac_address, error=str(e))


# =============================================================================
# Auto-connect toggle
# =============================================================================


def run_bluetooth_set_auto_connect(mac_address: str, enabled: bool) -> None:
    """Toggle auto-reconnect for a saved device.

    Used by the mobile Hardware tab — a per-device switch the user can
    flip to opt out of the boot/10-min reconnect loop without
    forgetting the pairing.
    """
    try:
        from jarvis_command_sdk import JarvisStorage
        storage = JarvisStorage("bluetooth")
        record = storage.get(mac_address)
        if not record:
            logger.warning("auto_connect toggle: device not in storage",
                           mac=mac_address)
            return
        record["auto_connect"] = bool(enabled)
        storage.save(mac_address, record)
        logger.info("auto_connect set", mac=mac_address, enabled=bool(enabled))
    except Exception as e:
        logger.error("Bluetooth set auto_connect failed",
                     mac=mac_address, error=str(e))


def _resolve_device_name(provider: Any, mac_address: str) -> str | None:
    """Fall back to ``bluetoothctl info`` when get_paired_devices misses one."""
    try:
        # Only PiBluetoothProvider exposes the helper; gracefully no-op
        # otherwise so this still works on Mac/simulator.
        info_method = getattr(provider, "_run_bluetoothctl", None)
        if info_method is None:
            return None
        result = info_method("info", mac_address, timeout=5.0)
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith("Name:"):
                return line.split(":", 1)[1].strip() or None
        return None
    except Exception:
        return None


# =============================================================================
# Discoverable
# =============================================================================


def run_bluetooth_discoverable(timeout: int = 120) -> None:
    """Make the node discoverable for pairing. Runs in background thread."""
    try:
        provider = get_bluetooth_provider()
        if provider.set_discoverable(enabled=True, timeout=timeout):
            logger.info("Node now discoverable", timeout_seconds=timeout)
        else:
            logger.warning("Failed to set discoverable mode")
    except Exception as e:
        logger.error("Bluetooth discoverable failed", error=str(e))
