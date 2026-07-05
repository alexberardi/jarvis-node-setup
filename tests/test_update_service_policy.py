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
                patch.object(update_service, "_report_task_refused"), \
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
                patch.object(update_service, "_report_task_refused"), \
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


class TestMonotonicDowngradeGuard:
    """A pending_update must never roll the node back to an older version —
    the audit's 'no downgrade guard' gap. Only a strictly-newer target spawns."""

    def _run(self, current: str, target: str):
        pending = {"task_id": "t-dg", "target_version": target}
        with patch.object(update_service.Config, "get_bool", return_value=True), \
                patch.object(update_service, "is_busy", return_value=False), \
                patch.object(
                    update_service, "version_info",
                    return_value=_version_info(version=current, install_mode="tarball"),
                ), \
                patch.object(update_service, "_spawn_upgrade") as spawn, \
                patch.object(update_service, "_write_state"):
            update_service.maybe_apply_update(pending)
        return spawn

    def test_refuses_a_downgrade(self):
        spawn = self._run(current="0.1.133", target="0.1.100")
        spawn.assert_not_called()

    def test_refuses_a_major_downgrade(self):
        spawn = self._run(current="1.2.0", target="0.9.9")
        spawn.assert_not_called()

    def test_refuses_a_replay_of_the_current_version(self):
        spawn = self._run(current="0.1.133", target="0.1.133")
        spawn.assert_not_called()

    def test_allows_a_strictly_newer_target(self):
        spawn = self._run(current="0.1.133", target="0.1.134")
        spawn.assert_called_once_with("0.1.134")

    def test_ignores_a_leading_v_and_dev_suffix_when_comparing(self):
        # v-prefixed / -dev tagged versions still compare numerically.
        spawn = self._run(current="0.1.133", target="v0.1.100-dev")
        spawn.assert_not_called()

    def test_unparseable_current_falls_back_to_equality(self):
        # A weird/dev current version must not block a legitimate real update.
        spawn = self._run(current="garbage", target="0.1.134")
        spawn.assert_called_once_with("0.1.134")


class TestParseVersion:
    def test_parses_variants(self):
        assert update_service._parse_version("0.1.5") == (0, 1, 5)
        assert update_service._parse_version("v0.1.5") == (0, 1, 5)
        assert update_service._parse_version("0.1.5-dev") == (0, 1, 5)
        assert update_service._parse_version("0.1.5+build.7") == (0, 1, 5)

    def test_returns_none_for_unparseable(self):
        assert update_service._parse_version("") is None
        assert update_service._parse_version(None) is None
        assert update_service._parse_version("latest") is None


class TestPolicyRefusalReporting:
    """A refused update must be reported to CC as a terminal failure with
    the policy reason — otherwise the task dies ~15 minutes later as a
    misleading sweeper 'Timeout' and the operator never learns why
    (Jul-2026 prod kitchen incident)."""

    def test_disabled_with_task_id_reports_failed(self):
        pending = {"task_id": "t-9", "target_version": "0.2.0"}
        with patch.object(update_service.Config, "get_bool", return_value=False), \
                patch.object(update_service, "get_command_center_url",
                             return_value="http://cc:7703"), \
                patch.object(update_service.RestClient, "post",
                             return_value={"ok": True}) as post:
            update_service.maybe_apply_update(pending)

        post.assert_called_once()
        assert post.call_args.args[0] == "http://cc:7703/api/v0/nodes/tasks/t-9/status"
        payload = post.call_args.kwargs["data"]
        assert payload["state"] == "failed"
        assert "disabled" in payload["error_message"].lower()

    def test_disabled_without_task_id_does_not_post(self):
        with patch.object(update_service.Config, "get_bool", return_value=False), \
                patch.object(update_service.RestClient, "post") as post:
            update_service.maybe_apply_update({"target_version": "0.2.0"})
        post.assert_not_called()

    def test_report_errors_never_propagate(self):
        pending = {"task_id": "t-10", "target_version": "0.2.0"}
        with patch.object(update_service.Config, "get_bool", return_value=False), \
                patch.object(update_service, "get_command_center_url",
                             side_effect=RuntimeError("boom")):
            update_service.maybe_apply_update(pending)  # must not raise
