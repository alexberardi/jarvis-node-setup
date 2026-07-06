"""Tests for the AP↔STA recovery cycle.

REGRESSION GUARD (2026-07-05 prod-kitchen stranding): AP mode used to be
a one-way door — the old recovery watcher probed the saved CC URL
*through the AP's own captive DNS*, so it could never observe recovery
and never fired. These tests pin the replacement behavior: the watcher
physically drops the AP, lets the known WiFi retry, reboots on success,
and restores the AP on failure — pausing while a pairing session is
active.
"""

import threading

from provisioning.recovery_watcher import _watcher_loop


class FakeWifiManager:
    """Records AP/STA transitions; SSID join is scripted per STA attempt."""

    def __init__(self, join_results):
        # join_results: list of bools — whether the known WiFi joins
        # during the Nth STA window.
        self._join_results = list(join_results)
        self._attempt = -1
        self.calls: list[str] = []
        self.ap_active = True

    def stop_ap_mode(self) -> bool:
        self.calls.append("stop_ap")
        self.ap_active = False
        self._attempt += 1
        return True

    def start_ap_mode(self, ssid: str) -> bool:
        self.calls.append(f"start_ap:{ssid}")
        self.ap_active = True
        return True

    def get_current_ssid(self):
        if 0 <= self._attempt < len(self._join_results) and self._join_results[self._attempt]:
            return "home-wifi"
        return None


def run_loop(wifi, *, pairing_active=False, max_cycles=1):
    """Drive the watcher loop with instant timings and a cycle budget."""
    shutdown = threading.Event()
    reboots: list[bool] = []
    cycles = {"n": 0}

    def reboot_fn():
        reboots.append(True)
        shutdown.set()

    def is_pairing_active():
        cycles["n"] += 1
        if cycles["n"] > max_cycles:
            shutdown.set()
        return pairing_active

    _watcher_loop(
        wifi_manager=wifi,
        ap_ssid="jarvis-test1234",
        is_pairing_active=is_pairing_active,
        shutdown_event=shutdown,
        retry_interval=0.01,
        sta_window=0.05,
        poll_seconds=0.01,
        reboot_fn=reboot_fn,
    )
    return reboots


class TestRecoveryCycle:

    def test_known_wifi_rejoins_triggers_reboot(self):
        """Transient outage: the STA retry joins → reboot to normal mode."""
        wifi = FakeWifiManager(join_results=[True])
        reboots = run_loop(wifi)
        assert reboots == [True]
        assert "stop_ap" in wifi.calls
        # No AP restore after a successful join — we're rebooting instead.
        assert not any(c.startswith("start_ap") for c in wifi.calls)

    def test_wifi_still_gone_restores_ap(self):
        """Genuine WiFi change: retry fails → AP comes back for pairing."""
        wifi = FakeWifiManager(join_results=[False])
        reboots = run_loop(wifi)
        assert reboots == []
        assert wifi.calls.index("stop_ap") < wifi.calls.index("start_ap:jarvis-test1234")
        assert wifi.ap_active is True

    def test_pairing_session_pauses_the_cycle(self):
        """Never yank the AP out from under an active pairing session."""
        wifi = FakeWifiManager(join_results=[True])
        reboots = run_loop(wifi, pairing_active=True, max_cycles=3)
        assert reboots == []
        assert wifi.calls == []
        assert wifi.ap_active is True

    def test_retries_forever_until_shutdown(self):
        """Repeated failed windows keep cycling (AP restored each time)."""
        wifi = FakeWifiManager(join_results=[False, False, True])
        reboots = run_loop(wifi, max_cycles=5)
        assert reboots == [True]
        assert wifi.calls.count("stop_ap") == 3
        assert wifi.calls.count("start_ap:jarvis-test1234") == 2
