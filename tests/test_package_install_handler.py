"""Tests for package_install_handler — restart-after-install semantics.

Phase 2 of resilient package installs: every successful install/uninstall
defers its result to a marker file and restarts the node (in-process reload
corrupts long-lived state); failures post immediately and never restart.
The boot-time flush verifies the installed components actually loaded
before reporting success to CC.
"""

import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Callable
from unittest.mock import MagicMock, patch

import pytest

from services import package_install_handler
from services.package_install_handler import (
    _defer_result_and_restart,
    _post_restarting_status,
    _verify_installed_components_loaded,
    flush_post_restart_install_result,
    has_pending_install_result,
    run_install_and_upload,
    run_revert_and_upload,
    run_uninstall_and_upload,
)


# ── Helpers ─────────────────────────────────────────────────────────────────


def _fake_manifest(name: str = "pkg", version: str = "1.0.0", components: int = 2):
    manifest = MagicMock()
    manifest.name = name
    manifest.version = version
    manifest.components = [object()] * components
    return manifest


class _FakeCommandDiscovery:
    def __init__(self, commands: dict, refresh_adds: dict | None = None):
        self._commands = dict(commands)
        self._refresh_adds = refresh_adds or {}

    def get_all_commands(self, include_disabled: bool = False) -> dict:
        return dict(self._commands)

    def refresh_now(self) -> None:
        self._commands.update(self._refresh_adds)


class _FakeCommandDiscoveryWithRegistries(_FakeCommandDiscovery):
    def __init__(self, commands: dict, failed_modules: dict | None = None,
                 refresh_adds: dict | None = None):
        super().__init__(commands, refresh_adds)
        self._failed_modules = failed_modules or {}

    def get_failed_modules(self) -> dict:
        return dict(self._failed_modules)


class _FakeAgentDiscovery:
    """Plain agent discovery — deliberately has NO Phase-3 registries."""

    def __init__(self, agents: dict):
        self._agents = dict(agents)

    def get_all_agents(self) -> dict:
        return dict(self._agents)


class _FakeAgentDiscoveryWithRegistries(_FakeAgentDiscovery):
    def __init__(self, agents: dict, unconfigured: dict | None = None,
                 failed_modules: dict | None = None):
        super().__init__(agents)
        self._unconfigured = unconfigured or {}
        self._failed_modules = failed_modules or {}

    def get_unconfigured_agents(self) -> dict:
        return dict(self._unconfigured)

    def get_failed_modules(self) -> dict:
        return dict(self._failed_modules)


def _write_pkg_meta(packages_dir: Path, name: str, components: list[dict]) -> None:
    packages_dir.mkdir(parents=True, exist_ok=True)
    (packages_dir / f"{name}.json").write_text(json.dumps({
        "package_name": name,
        "version": "1.0.0",
        "components": components,
    }))


def _fake_discovery_module(module_name: str, accessor_name: str,
                           accessor: Callable[[], Any]) -> ModuleType:
    """Build a stand-in discovery module for sys.modules injection.

    The handler imports discovery accessors lazily (``from utils.x import
    get_x``), so injecting a fake module via patch.dict(sys.modules, ...)
    avoids importing the real module — which other suites poison by mocking
    ``db``/``repositories`` in sys.modules (see tests/integration/conftest).
    """
    mod = ModuleType(module_name)
    setattr(mod, accessor_name, accessor)
    return mod


def _patched_command_discovery(service_or_accessor: Any):
    accessor = (service_or_accessor if callable(service_or_accessor)
                else lambda: service_or_accessor)
    return patch.dict(sys.modules, {
        "utils.command_discovery_service": _fake_discovery_module(
            "utils.command_discovery_service",
            "get_command_discovery_service",
            accessor,
        ),
    })


def _patched_agent_discovery(service: Any):
    return patch.dict(sys.modules, {
        "utils.agent_discovery_service": _fake_discovery_module(
            "utils.agent_discovery_service",
            "get_agent_discovery_service",
            lambda: service,
        ),
    })


# ── run_install_and_upload ──────────────────────────────────────────────────


# Authoritative install details as returned by CC's verify endpoint. The node
# installs from THIS, never from the (untrusted) MQTT nudge payload.
_VERIFY_OK = {
    "confirmed": True,
    "command_name": "pkg",
    "github_repo_url": "https://github.com/x/y",
    "git_tag": None,
}


class TestRunInstallSuccessAlwaysRestarts:
    @patch("services.package_install_handler._verify_with_cc", return_value=_VERIFY_OK)
    @patch("services.package_install_handler._upload_result")
    @patch("services.package_install_handler._defer_result_and_restart")
    @patch("services.command_store_service.install_from_github")
    def test_success_defers_even_without_pip_deps(self, mock_install, mock_defer, mock_upload, mock_verify):
        mock_install.return_value = _fake_manifest("pkg")

        run_install_and_upload("req-1")

        mock_verify.assert_called_once_with("req-1")
        mock_defer.assert_called_once()
        kwargs = mock_defer.call_args.kwargs
        assert kwargs["success"] is True
        assert kwargs["action"] == "install"
        assert kwargs["package_name"] == "pkg"
        assert kwargs["pip_deps_installed"] == []
        assert kwargs["details"]["package_name"] == "pkg"
        mock_upload.assert_not_called()

    @patch("services.package_install_handler._verify_with_cc",
           return_value={"confirmed": True, "command_name": "pkg",
                         "github_repo_url": "https://github.com/x/y", "git_tag": "v1.0.0"})
    @patch("services.package_install_handler._upload_result")
    @patch("services.package_install_handler._defer_result_and_restart")
    @patch("services.command_store_service.install_from_github")
    def test_success_with_pip_deps_passes_them_through(self, mock_install, mock_defer, mock_upload, mock_verify):
        def fake_install(url, version_tag=None, state=None):
            if state is not None:
                state["pip_deps_installed"] = ["httpx"]
            return _fake_manifest("pkg")

        mock_install.side_effect = fake_install

        run_install_and_upload("req-2")

        assert mock_defer.call_args.kwargs["pip_deps_installed"] == ["httpx"]
        # git_tag from the CC verify response reaches the installer.
        assert mock_install.call_args.kwargs["version_tag"] == "v1.0.0"
        mock_upload.assert_not_called()

    @patch("services.package_install_handler._verify_with_cc", return_value=_VERIFY_OK)
    @patch("services.package_install_handler._upload_result")
    @patch("services.package_install_handler._defer_result_and_restart")
    @patch("services.command_store_service.install_from_github")
    def test_install_never_skips_preflight_gate(self, mock_install, mock_defer, mock_upload, mock_verify):
        """The MQTT path must never set skip_tests."""
        mock_install.return_value = _fake_manifest("pkg")

        run_install_and_upload("req-3")

        assert "skip_tests" not in mock_install.call_args.kwargs

    @patch("services.package_install_handler._verify_with_cc")
    @patch("services.package_install_handler._upload_result")
    @patch("services.package_install_handler._defer_result_and_restart")
    @patch("services.command_store_service.install_from_github")
    def test_installs_repo_url_from_cc_not_payload(self, mock_install, mock_defer, mock_upload, mock_verify):
        """Zero-trust: the installed repo URL is the one CC returns from verify,
        so a spoofed MQTT payload URL can never reach the installer."""
        mock_verify.return_value = {
            "confirmed": True, "command_name": "pkg",
            "github_repo_url": "https://github.com/real/repo", "git_tag": None,
        }
        mock_install.return_value = _fake_manifest("pkg")

        run_install_and_upload("req-3b")

        assert mock_install.call_args.args[0] == "https://github.com/real/repo"


class TestRunInstallVerificationGate:
    @patch("services.package_install_handler._verify_with_cc", return_value=None)
    @patch("services.package_install_handler._upload_result")
    @patch("services.package_install_handler._defer_result_and_restart")
    @patch("services.command_store_service.install_from_github")
    def test_failed_verification_installs_nothing(self, mock_install, mock_defer, mock_upload, mock_verify):
        """A spoofed or stale nudge (verify returns None) installs nothing,
        defers nothing, and posts nothing — it is dropped."""
        run_install_and_upload("req-spoof")

        mock_install.assert_not_called()
        mock_defer.assert_not_called()
        mock_upload.assert_not_called()

    @patch("services.package_install_handler._verify_with_cc",
           return_value={"confirmed": True, "command_name": "pkg", "github_repo_url": "", "git_tag": None})
    @patch("services.package_install_handler._upload_result")
    @patch("services.command_store_service.install_from_github")
    def test_verify_without_repo_url_reports_failure(self, mock_install, mock_upload, mock_verify):
        run_install_and_upload("req-nourl")

        mock_install.assert_not_called()
        mock_upload.assert_called_once()
        assert mock_upload.call_args.kwargs["success"] is False


class TestRunInstallFailureNeverRestarts:
    @patch("services.package_install_handler._verify_with_cc", return_value=_VERIFY_OK)
    @patch("services.package_install_handler._upload_result")
    @patch("services.package_install_handler._defer_result_and_restart")
    @patch("services.command_store_service.install_from_github")
    def test_failure_posts_immediately(self, mock_install, mock_defer, mock_upload, mock_verify):
        mock_install.side_effect = Exception("Package failed validation: boom")

        run_install_and_upload("req-4")

        mock_defer.assert_not_called()
        mock_upload.assert_called_once()
        args, kwargs = mock_upload.call_args
        assert args[0] == "req-4"
        assert kwargs["success"] is False
        assert "boom" in kwargs["error"]

    @patch("services.package_install_handler._verify_with_cc", return_value=_VERIFY_OK)
    @patch("services.package_install_handler._upload_result")
    @patch("services.package_install_handler._defer_result_and_restart")
    @patch("services.command_store_service.install_from_github")
    def test_failure_with_pip_deps_installed_still_no_restart(
        self, mock_install, mock_defer, mock_upload, mock_verify
    ):
        """Pip deps from a failed attempt are idempotent/harmless — the
        pre-flight gate means nothing was scattered, so no restart."""
        def fake_install(url, version_tag=None, state=None):
            if state is not None:
                state["pip_deps_installed"] = ["httpx"]
            raise Exception("Package failed validation: import error")

        mock_install.side_effect = fake_install

        run_install_and_upload("req-5")

        mock_defer.assert_not_called()
        mock_upload.assert_called_once()
        assert mock_upload.call_args.kwargs["success"] is False


# ── run_uninstall_and_upload ────────────────────────────────────────────────


class TestRunUninstall:
    @patch("services.package_install_handler._upload_result")
    @patch("services.package_install_handler._defer_result_and_restart")
    @patch("services.command_store_service.remove")
    def test_success_defers_with_uninstall_action(self, mock_remove, mock_defer, mock_upload):
        run_uninstall_and_upload("req-6", "pkg", None)

        mock_remove.assert_called_once_with("pkg", component_type=None)
        mock_defer.assert_called_once()
        kwargs = mock_defer.call_args.kwargs
        assert kwargs["action"] == "uninstall"
        assert kwargs["package_name"] == "pkg"
        assert kwargs["success"] is True
        mock_upload.assert_not_called()

    @patch("services.package_install_handler._upload_result")
    @patch("services.package_install_handler._defer_result_and_restart")
    @patch("services.command_store_service.remove")
    def test_failure_posts_immediately_no_restart(self, mock_remove, mock_defer, mock_upload):
        mock_remove.side_effect = Exception("not installed")

        run_uninstall_and_upload("req-7", "pkg", None)

        mock_defer.assert_not_called()
        mock_upload.assert_called_once()
        args, kwargs = mock_upload.call_args
        assert kwargs["success"] is False
        assert kwargs["action"] == "uninstall"


# ── run_revert_and_upload ───────────────────────────────────────────────────


class TestRunRevert:
    @patch("services.package_install_handler._upload_result")
    @patch("services.package_install_handler._defer_result_and_restart")
    @patch("services.command_store_service.revert_package")
    def test_success_defers_with_revert_action(self, mock_revert, mock_defer, mock_upload):
        mock_revert.return_value = "1.0.0"

        run_revert_and_upload("req-15", "pkg")

        mock_revert.assert_called_once_with("pkg")
        mock_defer.assert_called_once()
        kwargs = mock_defer.call_args.kwargs
        assert kwargs["action"] == "revert"
        assert kwargs["package_name"] == "pkg"
        assert kwargs["success"] is True
        assert kwargs["details"] == {"package_name": "pkg", "version": "1.0.0"}
        mock_upload.assert_not_called()

    @patch("services.package_install_handler._upload_result")
    @patch("services.package_install_handler._defer_result_and_restart")
    @patch("services.command_store_service.revert_package")
    def test_failure_posts_immediately_no_restart(self, mock_revert, mock_defer, mock_upload):
        mock_revert.side_effect = Exception("No previous version of 'pkg' to revert to")

        run_revert_and_upload("req-16", "pkg")

        mock_defer.assert_not_called()
        mock_upload.assert_called_once()
        args, kwargs = mock_upload.call_args
        assert args[0] == "req-16"
        assert kwargs["success"] is False
        assert kwargs["action"] == "revert"
        assert "No previous version" in kwargs["error"]


# ── _defer_result_and_restart ───────────────────────────────────────────────


class TestDeferResultAndRestart:
    def test_marker_gains_action_package_and_rollback_fields(self, tmp_path):
        marker = tmp_path / ".pending_install_result.json"

        with patch("services.package_install_handler._PENDING_RESULT_FILE", marker), \
             patch("services.package_install_handler._post_restarting_status"), \
             patch.object(package_install_handler.os, "_exit", side_effect=SystemExit) as mock_exit:
            with pytest.raises(SystemExit):
                _defer_result_and_restart(
                    request_id="req-8",
                    success=True,
                    error=None,
                    details={"package_name": "pkg", "version": "1.0.0"},
                    pip_deps_installed=["httpx"],
                    action="install",
                    package_name="pkg",
                )

        mock_exit.assert_called_once_with(1)
        data = json.loads(marker.read_text())
        assert data["request_id"] == "req-8"
        assert data["success"] is True
        assert data["action"] == "install"
        assert data["package_name"] == "pkg"
        assert data["rollback_attempted"] is False
        assert data["pip_deps_installed"] == ["httpx"]

    def test_uninstall_marker_records_action(self, tmp_path):
        marker = tmp_path / ".pending_install_result.json"

        with patch("services.package_install_handler._PENDING_RESULT_FILE", marker), \
             patch("services.package_install_handler._post_restarting_status"), \
             patch.object(package_install_handler.os, "_exit", side_effect=SystemExit):
            with pytest.raises(SystemExit):
                _defer_result_and_restart(
                    request_id="req-9",
                    success=True,
                    error=None,
                    details={"package_name": "pkg"},
                    pip_deps_installed=[],
                    action="uninstall",
                    package_name="pkg",
                )

        assert json.loads(marker.read_text())["action"] == "uninstall"

    def test_restarting_status_posted_before_exit(self, tmp_path):
        marker = tmp_path / ".pending_install_result.json"
        order: list[str] = []

        with patch("services.package_install_handler._PENDING_RESULT_FILE", marker), \
             patch("services.package_install_handler._post_restarting_status",
                   side_effect=lambda *a, **kw: order.append("post")), \
             patch.object(package_install_handler.os, "_exit",
                          side_effect=lambda code: order.append("exit") or (_ for _ in ()).throw(SystemExit)):
            with pytest.raises(SystemExit):
                _defer_result_and_restart(
                    request_id="req-10",
                    success=True,
                    error=None,
                    details=None,
                    pip_deps_installed=[],
                    action="install",
                    package_name="pkg",
                )

        assert order == ["post", "exit"]

    def test_marker_write_failure_uploads_terminal_result_and_still_restarts(self, tmp_path):
        """The fallback must still restart — the stale-state bug lives in
        THIS process. The terminal result is uploaded directly instead of
        the marker, and the 'restarting' POST is skipped (a restarting post
        after a terminal result would strand CC in 'restarting')."""
        # Parent of the marker path is a FILE, so mkdir(parents=True) fails.
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory")
        marker = blocker / ".pending_install_result.json"

        with patch("services.package_install_handler._PENDING_RESULT_FILE", marker), \
             patch("services.package_install_handler._upload_result") as mock_upload, \
             patch("services.package_install_handler._post_restarting_status") as mock_post, \
             patch.object(package_install_handler.os, "_exit",
                          side_effect=SystemExit) as mock_exit:
            with pytest.raises(SystemExit):
                _defer_result_and_restart(
                    request_id="req-11",
                    success=True,
                    error=None,
                    details={"package_name": "pkg"},
                    pip_deps_installed=[],
                    action="uninstall",
                    package_name="pkg",
                )

        # Terminal result uploaded exactly once...
        mock_upload.assert_called_once()
        args, kwargs = mock_upload.call_args
        assert args[0] == "req-11"
        assert kwargs["success"] is True
        assert kwargs["error"] is None
        assert kwargs["details"] == {"package_name": "pkg"}
        assert kwargs["action"] == "uninstall"
        # ...NO restarting POST...
        mock_post.assert_not_called()
        # ...and the restart still happens.
        mock_exit.assert_called_once_with(1)

    def test_marker_write_and_upload_failure_still_restarts(self, tmp_path):
        """Even when the direct upload also fails, os._exit must be reached."""
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory")
        marker = blocker / ".pending_install_result.json"

        with patch("services.package_install_handler._PENDING_RESULT_FILE", marker), \
             patch("services.package_install_handler._upload_result",
                   side_effect=Exception("cc unreachable")), \
             patch("services.package_install_handler._post_restarting_status") as mock_post, \
             patch.object(package_install_handler.os, "_exit",
                          side_effect=SystemExit) as mock_exit:
            with pytest.raises(SystemExit):
                _defer_result_and_restart(
                    request_id="req-11b",
                    success=True,
                    error=None,
                    details=None,
                    pip_deps_installed=[],
                    action="install",
                    package_name="pkg",
                )

        mock_post.assert_not_called()
        mock_exit.assert_called_once_with(1)


class TestPostRestartingStatus:
    @patch("services.package_install_handler.RestClient")
    @patch("services.package_install_handler.get_command_center_url",
           return_value="http://cc:7703")
    def test_posts_restarting_payload(self, mock_cc, mock_rest):
        with patch("utils.config_service.Config.get_str", return_value="node-1"):
            _post_restarting_status("req-12", action="install",
                                    details={"package_name": "pkg"})

        mock_rest.post.assert_called_once()
        args, kwargs = mock_rest.post.call_args
        assert args[0] == "http://cc:7703/api/v0/nodes/node-1/package-install/req-12/results"
        assert kwargs["data"] == {
            "success": True,
            "restarting": True,
            "details": {"package_name": "pkg"},
        }
        assert kwargs["timeout"] == 3

    @patch("services.package_install_handler.RestClient")
    @patch("services.package_install_handler.get_command_center_url",
           return_value="http://cc:7703")
    def test_uses_action_in_results_url(self, mock_cc, mock_rest):
        with patch("utils.config_service.Config.get_str", return_value="node-1"):
            _post_restarting_status("req-13", action="uninstall", details=None)

        assert "/package-uninstall/req-13/results" in mock_rest.post.call_args[0][0]

    @patch("services.package_install_handler.get_command_center_url",
           side_effect=Exception("network down"))
    def test_swallows_all_errors(self, mock_cc):
        _post_restarting_status("req-14", action="install", details=None)  # no raise


# ── flush_post_restart_install_result ───────────────────────────────────────


class TestFlushPostRestart:
    def _write_marker(self, marker: Path, **overrides) -> None:
        payload = {
            "request_id": "req-20",
            "success": True,
            "scheduled_at": 0,
            "pip_deps_installed": [],
            "action": "install",
            "package_name": "pkg",
            "rollback_attempted": False,
            "details": {"package_name": "pkg", "version": "1.0.0"},
        }
        payload.update(overrides)
        marker.write_text(json.dumps(payload))

    def test_no_marker_is_noop(self, tmp_path):
        marker = tmp_path / "missing.json"
        with patch("services.package_install_handler._PENDING_RESULT_FILE", marker), \
             patch("services.package_install_handler._upload_result") as mock_upload:
            flush_post_restart_install_result()
        mock_upload.assert_not_called()

    def test_all_components_loaded_posts_success(self, tmp_path):
        marker = tmp_path / "marker.json"
        self._write_marker(marker)

        with patch("services.package_install_handler._PENDING_RESULT_FILE", marker), \
             patch("services.package_install_handler._verify_installed_components_loaded",
                   return_value=[]) as mock_verify, \
             patch("services.package_install_handler._upload_result") as mock_upload:
            flush_post_restart_install_result()

        mock_verify.assert_called_once_with("pkg")
        mock_upload.assert_called_once()
        kwargs = mock_upload.call_args.kwargs
        assert kwargs["success"] is True
        assert kwargs["action"] == "install"
        assert not marker.exists(), "marker cleared after flush"

    def test_failed_components_post_failure(self, tmp_path):
        marker = tmp_path / "marker.json"
        self._write_marker(marker)

        with patch("services.package_install_handler._PENDING_RESULT_FILE", marker), \
             patch("services.package_install_handler._verify_installed_components_loaded",
                   return_value=["pkg (cannot import name 'JarvisInbox')"]), \
             patch("services.package_install_handler._upload_result") as mock_upload:
            flush_post_restart_install_result()

        kwargs = mock_upload.call_args.kwargs
        assert kwargs["success"] is False
        assert kwargs["error"] == (
            "Installed but components failed to load: "
            "pkg (cannot import name 'JarvisInbox')"
        )
        assert not marker.exists()

    def test_legacy_marker_without_action_defaults_to_install(self, tmp_path):
        """Back-compat for a marker written by the previous node version
        mid-upgrade: no action/package_name fields."""
        marker = tmp_path / "marker.json"
        marker.write_text(json.dumps({
            "request_id": "req-21",
            "success": True,
            "scheduled_at": 0,
            "pip_deps_installed": ["httpx"],
            "details": {"package_name": "pkg"},
        }))

        with patch("services.package_install_handler._PENDING_RESULT_FILE", marker), \
             patch("services.package_install_handler._verify_installed_components_loaded",
                   return_value=[]) as mock_verify, \
             patch("services.package_install_handler._upload_result") as mock_upload:
            flush_post_restart_install_result()

        mock_verify.assert_called_once_with("pkg")  # package_name from details
        assert mock_upload.call_args.kwargs["action"] == "install"

    def test_uninstall_marker_skips_health_check(self, tmp_path):
        marker = tmp_path / "marker.json"
        self._write_marker(marker, action="uninstall",
                           details={"package_name": "pkg"})

        with patch("services.package_install_handler._PENDING_RESULT_FILE", marker), \
             patch("services.package_install_handler._verify_installed_components_loaded") as mock_verify, \
             patch("services.package_install_handler._upload_result") as mock_upload:
            flush_post_restart_install_result()

        mock_verify.assert_not_called()
        assert mock_upload.call_args.kwargs["action"] == "uninstall"
        assert mock_upload.call_args.kwargs["success"] is True

    def test_failed_install_marker_skips_health_check(self, tmp_path):
        marker = tmp_path / "marker.json"
        self._write_marker(marker, success=False, error="pip install failed")

        with patch("services.package_install_handler._PENDING_RESULT_FILE", marker), \
             patch("services.package_install_handler._verify_installed_components_loaded") as mock_verify, \
             patch("services.package_install_handler._upload_result") as mock_upload:
            flush_post_restart_install_result()

        mock_verify.assert_not_called()
        kwargs = mock_upload.call_args.kwargs
        assert kwargs["success"] is False
        assert kwargs["error"] == "pip install failed"

    def test_unreadable_marker_cleared_without_post(self, tmp_path):
        marker = tmp_path / "marker.json"
        marker.write_text("{not json")

        with patch("services.package_install_handler._PENDING_RESULT_FILE", marker), \
             patch("services.package_install_handler._upload_result") as mock_upload:
            flush_post_restart_install_result()

        mock_upload.assert_not_called()
        assert not marker.exists()

    def test_has_pending_install_result(self, tmp_path):
        marker = tmp_path / "marker.json"
        with patch("services.package_install_handler._PENDING_RESULT_FILE", marker):
            assert has_pending_install_result() is False
            marker.write_text("{}")
            assert has_pending_install_result() is True


# ── auto-rollback at the boot flush (Phase 4) ───────────────────────────────


class TestAutoRollbackAtBootFlush:
    """Failed post-restart health check + a .previous snapshot ⇒ revert once,
    rewrite the marker (rollback_attempted guard), restart one extra time.
    """

    def _write_marker(self, marker: Path, **overrides) -> None:
        payload = {
            "request_id": "req-50",
            "success": True,
            "scheduled_at": 0,
            "pip_deps_installed": [],
            "action": "install",
            "package_name": "pkg",
            "rollback_attempted": False,
            "details": {"package_name": "pkg", "version": "2.0.0"},
        }
        payload.update(overrides)
        marker.write_text(json.dumps(payload))

    def test_failed_components_with_previous_roll_back_once(self, tmp_path):
        marker = tmp_path / "marker.json"
        self._write_marker(marker)

        with patch("services.package_install_handler._PENDING_RESULT_FILE", marker), \
             patch("services.package_install_handler._verify_installed_components_loaded",
                   return_value=["foo (ImportError: nope)"]), \
             patch("services.command_store_service.has_previous_version",
                   return_value=True), \
             patch("services.command_store_service.revert_package",
                   return_value="1.0.0") as mock_revert, \
             patch("services.package_install_handler._post_restarting_status"), \
             patch("services.package_install_handler._upload_result") as mock_upload, \
             patch.object(package_install_handler.os, "_exit",
                          side_effect=SystemExit) as mock_exit:
            with pytest.raises(SystemExit):
                flush_post_restart_install_result()

        mock_revert.assert_called_once_with("pkg")
        mock_exit.assert_called_once_with(1)
        # Result is deferred to the NEXT boot — nothing posted yet.
        mock_upload.assert_not_called()
        # Marker rewritten in place: failed + flagged so no rollback loop.
        data = json.loads(marker.read_text())
        assert data["request_id"] == "req-50"
        assert data["success"] is False
        assert data["rollback_attempted"] is True
        assert data["error"].startswith("Install failed to load — rolled back to 1.0.0.")
        assert "foo (ImportError: nope)" in data["error"]

    def test_rollback_posts_restarting_status_before_second_exit(self, tmp_path):
        """The rollback's second boot needs its own +120s expiry extension
        on CC — post 'restarting' (best-effort) before the second exit."""
        marker = tmp_path / "marker.json"
        self._write_marker(marker)
        order: list[str] = []

        with patch("services.package_install_handler._PENDING_RESULT_FILE", marker), \
             patch("services.package_install_handler._verify_installed_components_loaded",
                   return_value=["foo (ImportError: nope)"]), \
             patch("services.command_store_service.has_previous_version",
                   return_value=True), \
             patch("services.command_store_service.revert_package",
                   return_value="1.0.0"), \
             patch("services.package_install_handler._post_restarting_status",
                   side_effect=lambda *a, **kw: order.append("post")) as mock_post, \
             patch("services.package_install_handler._upload_result") as mock_upload, \
             patch.object(package_install_handler.os, "_exit",
                          side_effect=lambda code: order.append("exit") or (_ for _ in ()).throw(SystemExit)):
            with pytest.raises(SystemExit):
                flush_post_restart_install_result()

        assert order == ["post", "exit"]
        args, kwargs = mock_post.call_args
        assert args[0] == "req-50"
        assert kwargs["action"] == "install"
        assert kwargs["details"] == {"package_name": "pkg", "version": "2.0.0"}
        # Result still deferred to the next boot — nothing terminal posted.
        mock_upload.assert_not_called()

    def test_rewritten_marker_posts_failure_on_next_boot(self, tmp_path):
        """The boot after the rollback restart: marker is success=False, so
        the flush skips the health check AND the rollback and just posts."""
        marker = tmp_path / "marker.json"
        rolled_back_error = (
            "Install failed to load — rolled back to 1.0.0. foo (ImportError: nope)"
        )
        self._write_marker(
            marker, success=False, rollback_attempted=True, error=rolled_back_error,
        )

        with patch("services.package_install_handler._PENDING_RESULT_FILE", marker), \
             patch("services.package_install_handler._verify_installed_components_loaded") as mock_verify, \
             patch("services.command_store_service.revert_package") as mock_revert, \
             patch("services.package_install_handler._upload_result") as mock_upload, \
             patch.object(package_install_handler.os, "_exit") as mock_exit:
            flush_post_restart_install_result()

        mock_verify.assert_not_called()
        mock_revert.assert_not_called()
        mock_exit.assert_not_called()
        kwargs = mock_upload.call_args.kwargs
        assert kwargs["success"] is False
        assert kwargs["error"] == rolled_back_error
        assert not marker.exists()

    def test_rollback_attempted_flag_prevents_second_rollback(self, tmp_path):
        """Belt and braces: even a (theoretically impossible) success=True
        marker with the flag set must never revert twice."""
        marker = tmp_path / "marker.json"
        self._write_marker(marker, rollback_attempted=True)

        with patch("services.package_install_handler._PENDING_RESULT_FILE", marker), \
             patch("services.package_install_handler._verify_installed_components_loaded",
                   return_value=["foo (ImportError: nope)"]), \
             patch("services.command_store_service.revert_package") as mock_revert, \
             patch("services.package_install_handler._upload_result") as mock_upload, \
             patch.object(package_install_handler.os, "_exit") as mock_exit:
            flush_post_restart_install_result()

        mock_revert.assert_not_called()
        mock_exit.assert_not_called()
        kwargs = mock_upload.call_args.kwargs
        assert kwargs["success"] is False
        assert kwargs["error"] == (
            "Installed but components failed to load: foo (ImportError: nope)"
        )
        assert not marker.exists()

    def test_failed_components_without_previous_post_failure(self, tmp_path):
        marker = tmp_path / "marker.json"
        self._write_marker(marker)

        with patch("services.package_install_handler._PENDING_RESULT_FILE", marker), \
             patch("services.package_install_handler._verify_installed_components_loaded",
                   return_value=["foo (not discovered)"]), \
             patch("services.command_store_service.has_previous_version",
                   return_value=False), \
             patch("services.command_store_service.revert_package") as mock_revert, \
             patch("services.package_install_handler._upload_result") as mock_upload, \
             patch.object(package_install_handler.os, "_exit") as mock_exit:
            flush_post_restart_install_result()

        mock_revert.assert_not_called()
        mock_exit.assert_not_called()
        kwargs = mock_upload.call_args.kwargs
        assert kwargs["success"] is False
        assert kwargs["error"] == (
            "Installed but components failed to load: foo (not discovered)"
        )
        assert not marker.exists()

    def test_revert_exception_falls_back_to_failure_post(self, tmp_path):
        marker = tmp_path / "marker.json"
        self._write_marker(marker)

        with patch("services.package_install_handler._PENDING_RESULT_FILE", marker), \
             patch("services.package_install_handler._verify_installed_components_loaded",
                   return_value=["foo (not discovered)"]), \
             patch("services.command_store_service.has_previous_version",
                   return_value=True), \
             patch("services.command_store_service.revert_package",
                   side_effect=Exception("restore blew up")), \
             patch("services.package_install_handler._upload_result") as mock_upload, \
             patch.object(package_install_handler.os, "_exit") as mock_exit:
            flush_post_restart_install_result()

        mock_exit.assert_not_called()
        kwargs = mock_upload.call_args.kwargs
        assert kwargs["success"] is False
        assert kwargs["error"] == (
            "Installed but components failed to load: foo (not discovered)"
        )
        assert not marker.exists()

    def test_marker_rewrite_failure_posts_rolled_back_error_without_restart(
        self, tmp_path
    ):
        marker = tmp_path / "marker.json"
        self._write_marker(marker)

        with patch("services.package_install_handler._PENDING_RESULT_FILE", marker), \
             patch("services.package_install_handler._verify_installed_components_loaded",
                   return_value=["foo (ImportError: nope)"]), \
             patch("services.command_store_service.has_previous_version",
                   return_value=True), \
             patch("services.command_store_service.revert_package",
                   return_value="1.0.0"), \
             patch("services.package_install_handler._write_marker_file",
                   side_effect=OSError("read-only fs")), \
             patch("services.package_install_handler._upload_result") as mock_upload, \
             patch.object(package_install_handler.os, "_exit") as mock_exit:
            flush_post_restart_install_result()

        mock_exit.assert_not_called()
        kwargs = mock_upload.call_args.kwargs
        assert kwargs["success"] is False
        assert kwargs["error"].startswith("Install failed to load — rolled back to 1.0.0.")


# ── _verify_installed_components_loaded (Phase-2 stub) ──────────────────────


class TestVerifyInstalledComponentsLoaded:
    def test_all_loaded_returns_empty(self, tmp_path):
        _write_pkg_meta(tmp_path, "pkg", [
            {"type": "command", "name": "foo", "path": "commands/foo/command.py"},
            {"type": "agent", "name": "bar", "path": "agents/bar/agent.py"},
        ])
        cmd_svc = _FakeCommandDiscovery({"foo": object()})
        agent_svc = _FakeAgentDiscovery({"bar": object()})

        with patch("services.command_store_service.PACKAGES_DIR", tmp_path), \
             _patched_command_discovery(cmd_svc), \
             _patched_agent_discovery(agent_svc):
            assert _verify_installed_components_loaded("pkg") == []

    def test_missing_command_with_import_error_reported(self, tmp_path):
        _write_pkg_meta(tmp_path, "pkg", [
            {"type": "command", "name": "foo", "path": "commands/foo/command.py"},
        ])
        cmd_svc = _FakeCommandDiscoveryWithRegistries(
            {"other": object()},
            failed_modules={"foo": "ImportError: cannot import name 'JarvisInbox'"},
        )

        with patch("services.command_store_service.PACKAGES_DIR", tmp_path), \
             _patched_command_discovery(cmd_svc):
            assert _verify_installed_components_loaded("pkg") == [
                "foo (ImportError: cannot import name 'JarvisInbox')"
            ]

    def test_missing_command_without_import_error_passes(self, tmp_path):
        """Presence miss with no recorded import error: the manifest name may
        legitimately differ from the runtime command_name — treat as loaded
        rather than roll back a healthy install."""
        _write_pkg_meta(tmp_path, "pkg", [
            {"type": "command", "name": "foo", "path": "commands/foo/command.py"},
        ])
        cmd_svc = _FakeCommandDiscoveryWithRegistries({"other": object()})

        with patch("services.command_store_service.PACKAGES_DIR", tmp_path), \
             _patched_command_discovery(cmd_svc):
            assert _verify_installed_components_loaded("pkg") == []

    def test_missing_command_without_phase3_registries_passes(self, tmp_path):
        """No failed-modules registry at all ⇒ no recorded error ⇒ a
        presence miss cannot fail the health check."""
        _write_pkg_meta(tmp_path, "pkg", [
            {"type": "command", "name": "foo", "path": "commands/foo/command.py"},
        ])
        cmd_svc = _FakeCommandDiscovery({"other": object()})

        with patch("services.command_store_service.PACKAGES_DIR", tmp_path), \
             _patched_command_discovery(cmd_svc):
            assert _verify_installed_components_loaded("pkg") == []

    def test_empty_cache_forces_a_discovery_pass(self, tmp_path):
        """Boot paths initialize command discovery lazily — the check must
        not judge an empty cache without refreshing first."""
        _write_pkg_meta(tmp_path, "pkg", [
            {"type": "command", "name": "foo", "path": "commands/foo/command.py"},
        ])
        cmd_svc = _FakeCommandDiscovery({}, refresh_adds={"foo": object()})

        with patch("services.command_store_service.PACKAGES_DIR", tmp_path), \
             _patched_command_discovery(cmd_svc):
            assert _verify_installed_components_loaded("pkg") == []

    def test_unconfigured_agent_counts_as_loaded(self, tmp_path):
        """Phase-3 registry, read via getattr: unconfigured = loaded."""
        _write_pkg_meta(tmp_path, "pkg", [
            {"type": "agent", "name": "bar", "path": "agents/bar/agent.py"},
        ])
        agent_svc = _FakeAgentDiscoveryWithRegistries(
            {}, unconfigured={"bar": (object(), ["API_KEY"])},
        )

        with patch("services.command_store_service.PACKAGES_DIR", tmp_path), \
             _patched_agent_discovery(agent_svc):
            assert _verify_installed_components_loaded("pkg") == []

    def test_missing_agent_without_phase3_registries_passes(self, tmp_path):
        """Tolerates discovery services that don't have the Phase-3
        registries yet (getattr/hasattr, no AttributeError) — and without a
        recorded import error, a presence miss counts as loaded."""
        _write_pkg_meta(tmp_path, "pkg", [
            {"type": "agent", "name": "bar", "path": "agents/bar/agent.py"},
        ])
        agent_svc = _FakeAgentDiscovery({})  # no get_unconfigured_agents

        with patch("services.command_store_service.PACKAGES_DIR", tmp_path), \
             _patched_agent_discovery(agent_svc):
            assert _verify_installed_components_loaded("pkg") == []

    def test_missing_agent_without_import_error_passes(self, tmp_path):
        """Registries exist but record no error for the missing agent —
        the manifest name may differ from the runtime agent name."""
        _write_pkg_meta(tmp_path, "pkg", [
            {"type": "agent", "name": "bar", "path": "agents/bar/agent.py"},
        ])
        agent_svc = _FakeAgentDiscoveryWithRegistries({}, failed_modules={})

        with patch("services.command_store_service.PACKAGES_DIR", tmp_path), \
             _patched_agent_discovery(agent_svc):
            assert _verify_installed_components_loaded("pkg") == []

    def test_failed_module_registry_error_included(self, tmp_path):
        _write_pkg_meta(tmp_path, "pkg", [
            {"type": "agent", "name": "bar", "path": "agents/bar/agent.py"},
        ])
        agent_svc = _FakeAgentDiscoveryWithRegistries(
            {}, failed_modules={"bar": "ImportError: cannot import name 'JarvisInbox'"},
        )

        with patch("services.command_store_service.PACKAGES_DIR", tmp_path), \
             _patched_agent_discovery(agent_svc):
            assert _verify_installed_components_loaded("pkg") == [
                "bar (ImportError: cannot import name 'JarvisInbox')"
            ]

    def test_other_component_types_not_checked_by_stub(self, tmp_path):
        _write_pkg_meta(tmp_path, "pkg", [
            {"type": "device_protocol", "name": "proto", "path": "device_families/proto/protocol.py"},
            {"type": "routine", "name": "morning", "path": "routines/morning/routine.json"},
        ])
        with patch("services.command_store_service.PACKAGES_DIR", tmp_path):
            assert _verify_installed_components_loaded("pkg") == []

    def test_missing_metadata_returns_empty(self, tmp_path):
        with patch("services.command_store_service.PACKAGES_DIR", tmp_path):
            assert _verify_installed_components_loaded("ghost") == []

    def test_discovery_exception_treated_as_loaded(self, tmp_path):
        """Verification must never fail a good install because discovery
        itself hiccuped."""
        _write_pkg_meta(tmp_path, "pkg", [
            {"type": "command", "name": "foo", "path": "commands/foo/command.py"},
        ])

        def _boom():
            raise Exception("db locked")

        with patch("services.command_store_service.PACKAGES_DIR", tmp_path), \
             _patched_command_discovery(_boom):
            assert _verify_installed_components_loaded("pkg") == []
