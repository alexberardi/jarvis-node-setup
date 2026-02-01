"""
Provisioning state machine for tracking provisioning progress.
"""

import threading
from typing import Optional

from provisioning.models import ProvisioningState


class ProvisioningStateMachine:
    """
    Thread-safe state machine for provisioning progress.

    Tracks the current state, message, and any errors during the provisioning process.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: ProvisioningState = ProvisioningState.AP_MODE
        self._message: str = "Waiting for mobile app connection..."
        self._error: Optional[str] = None
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
    def error(self) -> Optional[str]:
        with self._lock:
            return self._error

    @property
    def progress(self) -> int:
        with self._lock:
            return self._progress

    def transition_to(
        self,
        new_state: ProvisioningState,
        message: str,
        progress: Optional[int] = None
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
            # Clear error when transitioning to non-error state
            if new_state != ProvisioningState.ERROR:
                self._error = None

    def set_error(self, error: str) -> None:
        """
        Set an error state with error message.

        Args:
            error: Error description
        """
        with self._lock:
            self._state = ProvisioningState.ERROR
            self._message = "Provisioning failed"
            self._error = error

    def get_status(self) -> dict:
        """
        Get current status as a dictionary.

        Returns:
            Dictionary with state, message, progress, and error fields.
        """
        with self._lock:
            return {
                "state": self._state,
                "message": self._message,
                "progress_percent": self._progress,
                "error": self._error
            }

    def reset(self) -> None:
        """Reset state machine to initial state."""
        with self._lock:
            self._state = ProvisioningState.AP_MODE
            self._message = "Waiting for mobile app connection..."
            self._error = None
            self._progress = 0
