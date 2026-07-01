"""Tests for the ``allow_updates`` egress policy gate in
``services/update_service.py``.

The node is fully local by default: self-update reaches out to GitHub (and
the privileged self-update wrapper), so ``maybe_apply_update`` must be
short-circuited unless the operator has explicitly opted in via the
``allow_updates`` config key (env ``JARVIS_ALLOW_UPDATES``). The gate sits
at the very top of the function — before the ``_in_flight`` check, the
state write, and the installer spawn — so a disabled node performs zero
egress and leaves no state behind.

These tests patch the collaborators so nothing real is spawned or written.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

import services.update_service as update_service


def _version_info(version: str = "0.1.0", install_mode: str = "tarball"):
    """Minimal stand-in for ``core.version.version_info()``."""
    return SimpleNamespace(
        version=version,
        install_mode=install_mode,
        git_sha=None,
        install_dir=None,
        release_track="stable",
    )


@pytest.fixture(autouse=True)
def _clear_in_flight():
    """Reset the module-global in-flight latch around every test."""
    update_service._in_flight.clear()
    yield
    update_service._in_flight.clear()


class TestAllowUpdatesGate:
    def test_disabled_short_circuits_no_spawn_no_state(self):
        """Policy off: no installer spawn, no state write, latch stays clear."""
        pending = {"task_id": "t-1", "target_version": "0.2.0"}

        with patch.object(update_service.Config, "get_bool", return_value=False) as get_bool, \
                patch.object(update_service, "_spawn_upgrade") as spawn, \
                patch.object(update_service, "_write_state") as write_state:
            update_service.maybe_apply_update(pending)

        get_bool.assert_called_once_with("allow_updates", False)
        spawn.assert_not_called()
        write_state.assert_not_called()
        assert not update_service._in_flight.is_set()

    def test_disabled_does_not_consult_other_collaborators(self):
        """The gate fires before is_busy / version_info are ever touched."""
        pending = {"task_id": "t-2", "target_version": "0.2.0"}

        with patch.object(update_service.Config, "get_bool", return_value=False), \
                patch.object(update_service, "is_busy") as is_busy, \
                patch.object(update_service, "version_info") as version_info, \
                patch.object(update_service, "_spawn_upgrade") as spawn, \
                patch.object(update_service, "_write_state"):
            update_service.maybe_apply_update(pending)

        is_busy.assert_not_called()
        version_info.assert_not_called()
        spawn.assert_not_called()


class TestAllowUpdatesEnabledHappyPath:
    def test_enabled_tarball_new_version_spawns_upgrade(self):
        """Policy on + tarball install + differing version → spawn the upgrade."""
        pending = {"task_id": "t-3", "target_version": "0.2.0"}

        with patch.object(update_service.Config, "get_bool", return_value=True) as get_bool, \
                patch.object(update_service, "is_busy", return_value=False), \
                patch.object(
                    update_service,
                    "version_info",
                    return_value=_version_info(version="0.1.0", install_mode="tarball"),
                ), \
                patch.object(update_service, "_spawn_upgrade") as spawn, \
                patch.object(update_service, "_write_state") as write_state:
            update_service.maybe_apply_update(pending)

        get_bool.assert_called_once_with("allow_updates", False)
        spawn.assert_called_once_with("0.2.0")
        write_state.assert_called_once()
        assert update_service._in_flight.is_set()
