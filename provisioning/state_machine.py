"""
Provisioning state machine for tracking provisioning progress.
"""

import threading

from provisioning.models import ProvisioningErrorCode, ProvisioningState


# States in which a provisioning attempt is actively running. Anything else
# (idle AP_MODE, a terminal ERROR with the hotspot back, PROVISIONED) is not.
_BUSY_STATES = frozenset({ProvisioningState.CONNECTING, ProvisioningState.REGISTERING})


class ProvisioningStateMachine:
    """
    Thread-safe state machine for provisioning progress.

    Tracks the current state, message, and any errors during the provisioning process.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: ProvisioningState = ProvisioningState.AP_MODE
        self._message: str = "Waiting for mobile app connection..."
        self._error: str | None = None
        self._error_code: ProvisioningErrorCode | None = None
        self._registration_status: int | None = None
        self._retryable: bool = False
        self._hotspot_restored: bool = False
        self._progress: int = 0

    @property
    def state(self) -> ProvisioningState:
        with self._lock:
            return self._state

    @property
    def message(self) -> str:
        with self._lock:
            return self._message

    @property
    def error(self) -> str | None:
        with self._lock:
            return self._error

    @property
    def error_code(self) -> ProvisioningErrorCode | None:
        with self._lock:
            return self._error_code

    @property
    def progress(self) -> int:
        with self._lock:
            return self._progress

    def is_busy(self) -> bool:
        """True while a provisioning attempt is in flight (CONNECTING/REGISTERING)."""
        with self._lock:
            return self._state in _BUSY_STATES

    def transition_to(
        self,
        new_state: ProvisioningState,
        message: str,
        progress: int | None = None
    ) -> None:
        """
        Transition to a new state with a status message.

        Args:
            new_state: The new provisioning state
            message: Human-readable status message
            progress: Optional progress percentage (0-100)
        """
        with self._lock:
            self._state = new_state
            self._message = message
            if progress is not None:
                self._progress = max(0, min(100, progress))
            # Clear error details when transitioning to a non-error state
            if new_state != ProvisioningState.ERROR:
                self._clear_error_locked()

    def set_error(
        self,
        error: str,
        code: ProvisioningErrorCode = ProvisioningErrorCode.INTERNAL_ERROR,
        *,
        registration_status: int | None = None,
        retryable: bool = False,
        hotspot_restored: bool = False,
        message: str = "Provisioning failed",
    ) -> None:
        """
        Set an error state with error message.

        Args:
            error: Human-readable error description
            code: Machine-readable failure reason
            registration_status: HTTP status from CC when registration was refused
            retryable: Whether the app may resend credentials right now
            hotspot_restored: Whether the setup hotspot is broadcasting again
            message: Status message shown alongside the error
        """
        with self._lock:
            self._state = ProvisioningState.ERROR
            self._message = message
            self._error = error
            self._error_code = code
            self._registration_status = registration_status
            self._retryable = retryable
            self._hotspot_restored = hotspot_restored

    def get_status(self) -> dict:
        """
        Get current status as a dictionary.

        Returns:
            Dictionary matching the ProvisionStatus model.
        """
        with self._lock:
            return {
                "state": self._state,
                "message": self._message,
                "progress_percent": self._progress,
                "error": self._error,
                "error_code": self._error_code,
                "registration_status": self._registration_status,
                "retryable": self._retryable,
                "hotspot_restored": self._hotspot_restored,
            }

    def reset(self) -> None:
        """Reset state machine to initial state."""
        with self._lock:
            self._state = ProvisioningState.AP_MODE
            self._message = "Waiting for mobile app connection..."
            self._progress = 0
            self._clear_error_locked()

    def _clear_error_locked(self) -> None:
        self._error = None
        self._error_code = None
        self._registration_status = None
        self._retryable = False
        self._hotspot_restored = False
