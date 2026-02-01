"""
Unit tests for the provisioning state machine.
"""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from provisioning.models import ProvisioningState
from provisioning.state_machine import ProvisioningStateMachine


@pytest.fixture
def state_machine():
    """Create a fresh state machine for each test."""
    return ProvisioningStateMachine()


class TestInitialState:
    """Test initial state machine state."""

    def test_initial_state_is_ap_mode(self, state_machine):
        assert state_machine.state == ProvisioningState.AP_MODE

    def test_initial_message(self, state_machine):
        assert "Waiting" in state_machine.message

    def test_initial_progress_is_zero(self, state_machine):
        assert state_machine.progress == 0

    def test_initial_error_is_none(self, state_machine):
        assert state_machine.error is None


class TestTransitions:
    """Test state transitions."""

    def test_transition_updates_state(self, state_machine):
        state_machine.transition_to(
            ProvisioningState.CONNECTING,
            "Connecting..."
        )
        assert state_machine.state == ProvisioningState.CONNECTING

    def test_transition_updates_message(self, state_machine):
        state_machine.transition_to(
            ProvisioningState.CONNECTING,
            "Connecting to HomeNetwork..."
        )
        assert state_machine.message == "Connecting to HomeNetwork..."

    def test_transition_updates_progress(self, state_machine):
        state_machine.transition_to(
            ProvisioningState.CONNECTING,
            "Connecting...",
            progress=50
        )
        assert state_machine.progress == 50

    def test_transition_clamps_progress_min(self, state_machine):
        state_machine.transition_to(
            ProvisioningState.CONNECTING,
            "Test",
            progress=-10
        )
        assert state_machine.progress == 0

    def test_transition_clamps_progress_max(self, state_machine):
        state_machine.transition_to(
            ProvisioningState.CONNECTING,
            "Test",
            progress=150
        )
        assert state_machine.progress == 100

    def test_transition_clears_error(self, state_machine):
        # First set an error
        state_machine.set_error("Something went wrong")
        assert state_machine.error is not None

        # Transition to non-error state
        state_machine.transition_to(
            ProvisioningState.CONNECTING,
            "Retrying..."
        )
        assert state_machine.error is None


class TestErrorState:
    """Test error state handling."""

    def test_set_error_updates_state(self, state_machine):
        state_machine.set_error("Connection failed")
        assert state_machine.state == ProvisioningState.ERROR

    def test_set_error_updates_message(self, state_machine):
        state_machine.set_error("Connection failed")
        assert state_machine.message == "Provisioning failed"

    def test_set_error_stores_error(self, state_machine):
        state_machine.set_error("Connection failed")
        assert state_machine.error == "Connection failed"


class TestGetStatus:
    """Test getting status as dictionary."""

    def test_get_status_returns_dict(self, state_machine):
        status = state_machine.get_status()
        assert isinstance(status, dict)

    def test_get_status_contains_all_fields(self, state_machine):
        status = state_machine.get_status()
        assert "state" in status
        assert "message" in status
        assert "progress_percent" in status
        assert "error" in status

    def test_get_status_reflects_current_state(self, state_machine):
        state_machine.transition_to(
            ProvisioningState.REGISTERING,
            "Registering with server...",
            progress=75
        )

        status = state_machine.get_status()
        assert status["state"] == ProvisioningState.REGISTERING
        assert status["message"] == "Registering with server..."
        assert status["progress_percent"] == 75


class TestReset:
    """Test state machine reset."""

    def test_reset_returns_to_initial_state(self, state_machine):
        state_machine.transition_to(
            ProvisioningState.PROVISIONED,
            "Done",
            progress=100
        )

        state_machine.reset()

        assert state_machine.state == ProvisioningState.AP_MODE
        assert state_machine.progress == 0
        assert state_machine.error is None


class TestThreadSafety:
    """Test thread safety of state machine."""

    def test_concurrent_transitions(self, state_machine):
        """Test that concurrent transitions don't corrupt state."""
        states_to_try = [
            (ProvisioningState.CONNECTING, "Msg1", 10),
            (ProvisioningState.REGISTERING, "Msg2", 50),
            (ProvisioningState.PROVISIONED, "Msg3", 100),
        ]

        errors = []

        def transition_worker(state, msg, progress):
            try:
                for _ in range(100):
                    state_machine.transition_to(state, msg, progress)
                    # Read state to verify consistency
                    status = state_machine.get_status()
                    assert "state" in status
                    assert "message" in status
            except Exception as e:
                errors.append(e)

        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = [
                executor.submit(transition_worker, s, m, p)
                for s, m, p in states_to_try
            ]
            for f in futures:
                f.result()

        assert len(errors) == 0, f"Thread safety errors: {errors}"

    def test_concurrent_reads_and_writes(self, state_machine):
        """Test reading while writing doesn't cause issues."""
        errors = []
        stop_event = threading.Event()

        def writer():
            try:
                i = 0
                while not stop_event.is_set():
                    state_machine.transition_to(
                        ProvisioningState.CONNECTING,
                        f"Message {i}",
                        progress=i % 100
                    )
                    i += 1
            except Exception as e:
                errors.append(e)

        def reader():
            try:
                while not stop_event.is_set():
                    _ = state_machine.get_status()
                    _ = state_machine.state
                    _ = state_machine.message
                    _ = state_machine.progress
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=writer),
            threading.Thread(target=reader),
            threading.Thread(target=reader),
        ]

        for t in threads:
            t.start()

        # Let them run briefly
        import time
        time.sleep(0.1)
        stop_event.set()

        for t in threads:
            t.join()

        assert len(errors) == 0, f"Concurrency errors: {errors}"
