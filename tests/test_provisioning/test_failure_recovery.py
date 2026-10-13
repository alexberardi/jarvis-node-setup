"""A failed provisioning attempt must not strand the node.

2026-10-08 dev Pi: the node joined the home WiFi, CC answered the register
call with 401 "Invalid or expired provisioning token" (the app had held the
token >10 min), and the node stayed on WiFi — unregistered, no hotspot, the
app spinning forever. Now any failure after the node leaves its hotspot
rolls back the WiFi profile it created, restores the hotspot, and reports
state=ERROR with a machine-readable error_code on GET /api/v1/status.
"""

import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from provisioning.api import _run_provisioning, create_provisioning_app
from provisioning.models import ProvisioningErrorCode, ProvisioningState
from provisioning.registration import RegistrationResult
from provisioning.state_machine import ProvisioningStateMachine
from provisioning.wifi_manager import SimulatedWiFi

AP_SSID = "jarvis-abcd1234"


class RecordingWiFi(SimulatedWiFi):
    """SimulatedWiFi that records the recovery-relevant calls in order."""

    def __init__(self, connect_ok: bool = True, ap_ok: bool = True) -> None:
        super().__init__()
        self.events: list[str] = []
        self._connect_ok = connect_ok
        self._ap_ok = ap_ok

    def connect(self, ssid: str, password: str) -> bool:
        self.events.append("connect")
        return self._connect_ok

    def rollback_connection(self) -> None:
        self.events.append("rollback")

    def commit_connection(self) -> None:
        self.events.append("commit")

    def start_ap_mode(self, ssid: str) -> bool:
        self.events.append(f"start_ap:{ssid}")
        return self._ap_ok


TOKEN_401 = RegistrationResult(
    ok=False,
    error_kind="token_rejected",
    status_code=401,
    detail="Invalid or expired provisioning token",
)
OK = RegistrationResult(ok=True, node_id="node-uuid-123", node_key="key-abc")


@pytest.fixture
def patched():
    with patch("provisioning.api._update_config", return_value=True) as upd, \
         patch("provisioning.api.save_wifi_credentials") as save_wifi, \
         patch("provisioning.api.register_node") as reg, \
         patch("provisioning.api._save_node_credentials", return_value=True) as save_creds, \
         patch("provisioning.api.mark_provisioned") as mark:
        yield MagicMock(update=upd, save_wifi=save_wifi, register=reg,
                        save_creds=save_creds, mark=mark)


def _run(wifi, sm, ap_ssid: str | None = AP_SSID) -> None:
    _run_provisioning(
        wifi_manager=wifi,
        state_machine=sm,
        ssid="HomeNet",
        password="pw",
        room="kitchen",
        command_center_url="http://cc:7703",
        config_service_url=None,
        household_id="hh",
        node_id="node-uuid-123",
        provisioning_token="tok",
        ap_ssid=ap_ssid,
    )


class TestRegistrationFailure:
    def test_401_rolls_back_wifi_and_restores_hotspot(self, patched):
        patched.register.return_value = TOKEN_401
        wifi, sm = RecordingWiFi(), ProvisioningStateMachine()
        _run(wifi, sm)
        assert wifi.events == ["connect", "rollback", f"start_ap:{AP_SSID}"]

    def test_401_reports_registration_failed_with_reason(self, patched):
        patched.register.return_value = TOKEN_401
        sm = ProvisioningStateMachine()
        _run(RecordingWiFi(), sm)
        status = sm.get_status()
        assert status["state"] == ProvisioningState.ERROR
        assert status["error_code"] == ProvisioningErrorCode.REGISTRATION_FAILED
        assert status["registration_status"] == 401
        assert "Invalid or expired provisioning token" in status["error"]
        assert status["hotspot_restored"] is True
        assert status["retryable"] is True

    def test_failure_does_not_mark_provisioned_or_save_creds(self, patched):
        patched.register.return_value = TOKEN_401
        _run(RecordingWiFi(), ProvisioningStateMachine())
        patched.mark.assert_not_called()
        patched.save_creds.assert_not_called()

    def test_network_failure_reported_without_http_status(self, patched):
        patched.register.return_value = RegistrationResult(
            ok=False, error_kind="network_error", detail="connection refused"
        )
        sm = ProvisioningStateMachine()
        _run(RecordingWiFi(), sm)
        status = sm.get_status()
        assert status["error_code"] == ProvisioningErrorCode.REGISTRATION_FAILED
        assert status["registration_status"] is None
        assert "Could not reach command center" in status["error"]

    def test_hotspot_restart_failure_is_reported_not_retryable(self, patched):
        patched.register.return_value = TOKEN_401
        sm = ProvisioningStateMachine()
        _run(RecordingWiFi(ap_ok=False), sm)
        status = sm.get_status()
        assert status["state"] == ProvisioningState.ERROR
        assert status["hotspot_restored"] is False
        assert status["retryable"] is False

    def test_without_ap_ssid_rolls_back_but_cannot_restore(self, patched):
        patched.register.return_value = TOKEN_401
        wifi, sm = RecordingWiFi(), ProvisioningStateMachine()
        _run(wifi, sm, ap_ssid=None)
        assert wifi.events == ["connect", "rollback"]
        assert sm.get_status()["hotspot_restored"] is False

    def test_rollback_exception_still_restores_hotspot(self, patched):
        patched.register.return_value = TOKEN_401
        wifi = RecordingWiFi()
        wifi.rollback_connection = MagicMock(side_effect=RuntimeError("nmcli gone"))
        sm = ProvisioningStateMachine()
        _run(wifi, sm)
        assert f"start_ap:{AP_SSID}" in wifi.events
        assert sm.get_status()["hotspot_restored"] is True

    def test_state_stays_busy_while_restoring(self, patched):
        """The AP↔STA watcher must not grab the radio mid-recovery."""
        patched.register.return_value = TOKEN_401
        sm = ProvisioningStateMachine()
        seen: list[bool] = []
        wifi = RecordingWiFi()
        orig = wifi.start_ap_mode

        def spy(ssid: str) -> bool:
            seen.append(sm.is_busy())
            return orig(ssid)

        wifi.start_ap_mode = spy
        _run(wifi, sm)
        assert seen == [True]
        assert sm.is_busy() is False


class TestOtherFailures:
    def test_wifi_connect_failure_restores_hotspot(self, patched):
        wifi, sm = RecordingWiFi(connect_ok=False), ProvisioningStateMachine()
        _run(wifi, sm)
        assert wifi.events == ["connect", "rollback", f"start_ap:{AP_SSID}"]
        status = sm.get_status()
        assert status["error_code"] == ProvisioningErrorCode.WIFI_CONNECT_FAILED
        assert status["retryable"] is True
        patched.register.assert_not_called()

    def test_config_write_failure_never_touches_wifi(self, patched):
        patched.update.return_value = False
        wifi, sm = RecordingWiFi(), ProvisioningStateMachine()
        _run(wifi, sm)
        assert wifi.events == []
        status = sm.get_status()
        assert status["error_code"] == ProvisioningErrorCode.CONFIG_WRITE_FAILED
        assert status["retryable"] is True  # still on the hotspot

    def test_credentials_save_failure_restores_hotspot(self, patched):
        patched.register.return_value = OK
        patched.save_creds.return_value = False
        wifi, sm = RecordingWiFi(), ProvisioningStateMachine()
        _run(wifi, sm)
        assert wifi.events[-2:] == ["rollback", f"start_ap:{AP_SSID}"]
        assert sm.get_status()["error_code"] == ProvisioningErrorCode.CREDENTIALS_SAVE_FAILED
        patched.mark.assert_not_called()

    def test_unexpected_exception_restores_hotspot(self, patched):
        patched.register.side_effect = RuntimeError("boom")
        wifi, sm = RecordingWiFi(), ProvisioningStateMachine()
        _run(wifi, sm)
        assert wifi.events[-1] == f"start_ap:{AP_SSID}"
        status = sm.get_status()
        assert status["error_code"] == ProvisioningErrorCode.INTERNAL_ERROR
        assert status["error"] == "boom"


class TestSuccess:
    def test_success_commits_profile_and_marks_provisioned(self, patched):
        patched.register.return_value = OK
        wifi, sm = RecordingWiFi(), ProvisioningStateMachine()
        _run(wifi, sm)
        assert wifi.events == ["connect", "commit"]
        patched.mark.assert_called_once()
        status = sm.get_status()
        assert status["state"] == ProvisioningState.PROVISIONED
        assert status["error_code"] is None


class TestStatusEndpointContract:
    """End to end over HTTP: what the app sees, and that it can retry."""

    PROVISION_BODY = {
        "wifi_ssid": "HomeNetwork",
        "wifi_password": "pass",
        "room": "kitchen",
        "command_center_url": "http://localhost:7703",
        "household_id": "hh-uuid",
        "node_id": "node-uuid-123",
        "provisioning_token": "tok_old",
    }

    def _wait_for(self, client: TestClient, pred, timeout: float = 3.0) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            status = client.get("/api/v1/status").json()
            if pred(status) or time.monotonic() > deadline:
                return status
            time.sleep(0.02)

    def test_status_reports_registration_failed_then_retry_succeeds(self, patched):
        wifi = RecordingWiFi()
        client = TestClient(create_provisioning_app(wifi, ap_ssid=AP_SSID))

        patched.register.return_value = TOKEN_401
        assert client.post("/api/v1/provision", json=self.PROVISION_BODY).json()["success"]
        status = self._wait_for(client, lambda s: s["state"] == "ERROR")
        assert status == {
            "state": "ERROR",
            "message": "Provisioning failed — reconnect to the node's setup network and try again",
            "progress_percent": status["progress_percent"],
            "error": "Command center rejected registration (HTTP 401): "
                     "Invalid or expired provisioning token",
            "error_code": "registration_failed",
            "registration_status": 401,
            "retryable": True,
            "hotspot_restored": True,
        }

        # The app resends with a fresh token; the node accepts it.
        patched.register.return_value = OK
        body = {**self.PROVISION_BODY, "provisioning_token": "tok_fresh"}
        assert client.post("/api/v1/provision", json=body).json()["success"]
        status = self._wait_for(client, lambda s: s["state"] == "PROVISIONED")
        assert status["state"] == "PROVISIONED"
        assert status["error_code"] is None
        assert status["retryable"] is False
        assert patched.register.call_args[1]["provisioning_token"] == "tok_fresh"
