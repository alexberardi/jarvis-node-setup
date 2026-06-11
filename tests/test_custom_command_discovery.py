"""Tests for custom command discovery in CommandDiscoveryService."""

import sys
import threading
import types
from unittest.mock import patch, MagicMock

from jarvis_command_sdk import IJarvisCommand, CommandExample
from core.ijarvis_parameter import JarvisParameter
from core.command_response import CommandResponse
from core.request_information import RequestInformation

# Pre-import so _discover_commands' lazy `from services.command_store_service
# import register_package_lib_paths` hits sys.modules instead of triggering a
# fresh import (whose pydantic lazy-loading would be intercepted by the
# importlib.import_module patches below when this file runs standalone).
import services.command_store_service  # noqa: F401


# ── Sample commands for testing ────────────────────────────────────────────


class FakeBuiltinCommand(IJarvisCommand):
    @property
    def command_name(self) -> str:
        return "test_builtin"

    @property
    def description(self) -> str:
        return "A built-in test command"

    @property
    def parameters(self):
        return []

    @property
    def required_secrets(self):
        return []

    @property
    def keywords(self):
        return ["test"]

    def generate_prompt_examples(self):
        return [CommandExample("test", {}, is_primary=True)]

    def generate_adapter_examples(self):
        return self.generate_prompt_examples()

    def run(self, request_info, **kwargs):
        return CommandResponse.success_response({"source": "builtin"})


class FakeCustomCommand(IJarvisCommand):
    @property
    def command_name(self) -> str:
        return "test_custom"

    @property
    def description(self) -> str:
        return "A custom test command"

    @property
    def parameters(self):
        return [JarvisParameter("query", "string", required=True)]

    @property
    def required_secrets(self):
        return []

    @property
    def keywords(self):
        return ["custom"]

    def generate_prompt_examples(self):
        return [CommandExample("custom test", {"query": "hello"}, is_primary=True)]

    def generate_adapter_examples(self):
        return self.generate_prompt_examples()

    def run(self, request_info, **kwargs):
        return CommandResponse.success_response({"source": "custom"})


class ConflictingCustomCommand(IJarvisCommand):
    """Custom command that shares a name with the built-in."""

    @property
    def command_name(self) -> str:
        return "test_builtin"  # Same as FakeBuiltinCommand

    @property
    def description(self) -> str:
        return "Conflicting custom command"

    @property
    def parameters(self):
        return []

    @property
    def required_secrets(self):
        return []

    @property
    def keywords(self):
        return ["conflict"]

    def generate_prompt_examples(self):
        return [CommandExample("conflict", {}, is_primary=True)]

    def generate_adapter_examples(self):
        return self.generate_prompt_examples()

    def run(self, request_info, **kwargs):
        return CommandResponse.success_response({"source": "custom_conflict"})


# ── Helpers ─────────────────────────────────────────────────────────────────


def _create_service():
    """Create a CommandDiscoveryService without starting the background thread."""
    from utils.command_discovery_service import CommandDiscoveryService
    svc = object.__new__(CommandDiscoveryService)
    svc.refresh_interval = 600
    svc._commands_cache = {}
    svc._failed_modules = {}
    svc._last_refresh = 0
    svc._lock = threading.Lock()
    return svc


def _make_module_with_class(cls):
    """Create a module containing the given command class."""
    mod = types.ModuleType("fake_module")
    setattr(mod, cls.__name__, cls)
    return mod


# ── Tests ───────────────────────────────────────────────────────────────────


class TestScanPackage:
    """Test _scan_package helper."""

    def test_discovers_commands(self):
        svc = _create_service()
        mod = _make_module_with_class(FakeBuiltinCommand)

        mock_pkg = types.ModuleType("commands")
        mock_pkg.__path__ = ["/fake/path"]

        commands_dict = {}
        with patch("utils.command_discovery_service.pkgutil.iter_modules", return_value=[("", "test_mod", False)]):
            with patch("utils.command_discovery_service.importlib.import_module", return_value=mod):
                svc._scan_package(mock_pkg, "commands", commands_dict)

        assert "test_builtin" in commands_dict
        assert isinstance(commands_dict["test_builtin"], FakeBuiltinCommand)

    def test_import_error_doesnt_crash(self):
        svc = _create_service()

        mock_pkg = types.ModuleType("commands")
        mock_pkg.__path__ = ["/fake/path"]

        commands_dict = {}
        with patch("utils.command_discovery_service.pkgutil.iter_modules", return_value=[("", "broken_mod", False)]):
            with patch("utils.command_discovery_service.importlib.import_module", side_effect=ImportError("nope")):
                svc._scan_package(mock_pkg, "commands", commands_dict)

        assert len(commands_dict) == 0


class TestCustomCommandDiscovery:
    """Test that _discover_commands scans custom_commands."""

    def test_custom_commands_discovered(self):
        """Custom commands in commands/custom_commands/*/command.py are found."""
        svc = _create_service()

        builtin_pkg = types.ModuleType("commands")
        builtin_pkg.__path__ = ["/fake/commands"]

        custom_pkg = types.ModuleType("commands.custom_commands")
        custom_pkg.__path__ = ["/fake/commands/custom_commands"]

        custom_mod = _make_module_with_class(FakeCustomCommand)

        def import_side_effect(name):
            if name == "commands.custom_commands.test_custom.command":
                return custom_mod
            raise ImportError(f"No module named '{name}'")

        def iter_modules_side_effect(path):
            if path == builtin_pkg.__path__:
                return []  # No built-in commands
            if path == custom_pkg.__path__:
                return [("", "test_custom", True)]
            return []

        with patch.dict(sys.modules, {"commands": builtin_pkg, "commands.custom_commands": custom_pkg}):
            with patch("utils.command_discovery_service.pkgutil.iter_modules", side_effect=iter_modules_side_effect):
                with patch("utils.command_discovery_service.importlib.import_module", side_effect=import_side_effect):
                    svc._discover_commands()

        assert "test_custom" in svc._commands_cache
        assert isinstance(svc._commands_cache["test_custom"], FakeCustomCommand)

    def test_builtin_wins_name_conflict(self):
        """When a custom command has the same name as built-in, built-in wins."""
        svc = _create_service()

        builtin_pkg = types.ModuleType("commands")
        builtin_pkg.__path__ = ["/fake/commands"]

        custom_pkg = types.ModuleType("commands.custom_commands")
        custom_pkg.__path__ = ["/fake/commands/custom_commands"]

        builtin_mod = _make_module_with_class(FakeBuiltinCommand)
        conflict_mod = _make_module_with_class(ConflictingCustomCommand)

        def import_side_effect(name):
            if name == "commands.test_builtin":
                return builtin_mod
            if name == "commands.custom_commands.conflicting.command":
                return conflict_mod
            raise ImportError(f"No module named '{name}'")

        def iter_modules_side_effect(path):
            if path == builtin_pkg.__path__:
                return [("", "test_builtin", False)]
            if path == custom_pkg.__path__:
                return [("", "conflicting", True)]
            return []

        with patch.dict(sys.modules, {"commands": builtin_pkg, "commands.custom_commands": custom_pkg}):
            with patch("utils.command_discovery_service.pkgutil.iter_modules", side_effect=iter_modules_side_effect):
                with patch("utils.command_discovery_service.importlib.import_module", side_effect=import_side_effect):
                    svc._discover_commands()

        # Built-in should win
        assert "test_builtin" in svc._commands_cache
        resp = svc._commands_cache["test_builtin"].run(
            RequestInformation("test", "conv-1")
        )
        assert resp.context_data["source"] == "builtin"

    def test_non_package_custom_entries_skipped(self):
        """Files (not directories) in custom_commands/ are skipped."""
        svc = _create_service()

        builtin_pkg = types.ModuleType("commands")
        builtin_pkg.__path__ = ["/fake/commands"]

        custom_pkg = types.ModuleType("commands.custom_commands")
        custom_pkg.__path__ = ["/fake/commands/custom_commands"]

        def iter_modules_side_effect(path):
            if path == builtin_pkg.__path__:
                return []
            if path == custom_pkg.__path__:
                return [("", "some_file", False)]  # is_pkg=False
            return []

        with patch.dict(sys.modules, {"commands": builtin_pkg, "commands.custom_commands": custom_pkg}):
            with patch("utils.command_discovery_service.pkgutil.iter_modules", side_effect=iter_modules_side_effect):
                with patch("utils.command_discovery_service.importlib.import_module", side_effect=ImportError):
                    svc._discover_commands()

        assert len(svc._commands_cache) == 0

    def test_broken_custom_command_logged_not_fatal(self):
        """A broken custom command doesn't crash discovery of other commands."""
        svc = _create_service()

        builtin_pkg = types.ModuleType("commands")
        builtin_pkg.__path__ = ["/fake/commands"]

        custom_pkg = types.ModuleType("commands.custom_commands")
        custom_pkg.__path__ = ["/fake/commands/custom_commands"]

        good_mod = _make_module_with_class(FakeCustomCommand)

        def import_side_effect(name):
            if name == "commands.custom_commands.broken.command":
                raise SyntaxError("broken command")
            if name == "commands.custom_commands.good.command":
                return good_mod
            raise ImportError(f"No module named '{name}'")

        def iter_modules_side_effect(path):
            if path == builtin_pkg.__path__:
                return []
            if path == custom_pkg.__path__:
                return [("", "broken", True), ("", "good", True)]
            return []

        with patch.dict(sys.modules, {"commands": builtin_pkg, "commands.custom_commands": custom_pkg}):
            with patch("utils.command_discovery_service.pkgutil.iter_modules", side_effect=iter_modules_side_effect):
                with patch("utils.command_discovery_service.importlib.import_module", side_effect=import_side_effect):
                    svc._discover_commands()

        # Good command should still be discovered
        assert "test_custom" in svc._commands_cache
        assert len(svc._commands_cache) == 1


class TestFailedModuleRegistry:
    """Phase 3 of resilient installs: custom command modules that fail to
    import are recorded in a registry so the settings snapshot can emit a
    synthetic import_failed entry instead of the command silently vanishing.

    Uses a real (empty) ``commands``/``commands.custom_commands`` package in
    a tmp dir because ``_discover_commands`` pops the custom_commands module
    from sys.modules and re-imports it via a plain import statement — a fake
    sys.modules entry doesn't survive that round-trip.
    """

    def _make_commands_pkg(self, tmp_path):
        (tmp_path / "commands" / "custom_commands").mkdir(parents=True, exist_ok=True)
        (tmp_path / "commands" / "__init__.py").write_text("")
        (tmp_path / "commands" / "custom_commands" / "__init__.py").write_text("")

    def _discover(self, svc, tmp_path, custom_modules, import_side_effect):
        """Run _discover_commands against the tmp commands package."""
        self._make_commands_pkg(tmp_path)

        def iter_modules_side_effect(path):
            # Only the custom_commands scan yields modules; the built-in
            # scan (commands/) is empty.
            if path and "custom_commands" in str(path[0]):
                return [("", name, True) for name in custom_modules]
            return []

        with patch.dict(sys.modules), patch.object(sys, "path", [str(tmp_path)] + sys.path):
            for key in list(sys.modules):
                if key == "commands" or key.startswith("commands."):
                    del sys.modules[key]
            with patch("utils.command_discovery_service.pkgutil.iter_modules", side_effect=iter_modules_side_effect):
                with patch("utils.command_discovery_service.importlib.import_module", side_effect=import_side_effect):
                    svc._discover_commands()

    def test_import_failure_recorded_in_failed_modules(self, tmp_path):
        svc = _create_service()

        def import_side_effect(name):
            raise ImportError("cannot import name 'JarvisInbox' from 'jarvis_command_sdk'")

        self._discover(svc, tmp_path, ["broken_cmd"], import_side_effect)

        assert "broken_cmd" not in svc._commands_cache
        failed = svc.get_failed_modules()
        assert "broken_cmd" in failed
        assert "JarvisInbox" in failed["broken_cmd"]

    def test_failed_modules_cleared_on_successful_rediscovery(self, tmp_path):
        svc = _create_service()
        good_mod = _make_module_with_class(FakeCustomCommand)

        # First pass: import raises — recorded.
        self._discover(svc, tmp_path, ["flaky_cmd"], ImportError("boom"))
        assert "flaky_cmd" in svc.get_failed_modules()

        # Second pass: import succeeds — the stale failure must clear.
        def import_side_effect(name):
            if name == "commands.custom_commands.flaky_cmd.command":
                return good_mod
            raise ImportError(f"No module named '{name}'")

        self._discover(svc, tmp_path, ["flaky_cmd"], import_side_effect)
        assert "flaky_cmd" not in svc.get_failed_modules()
        assert "test_custom" in svc._commands_cache

    def test_clean_discovery_has_empty_registry(self, tmp_path):
        svc = _create_service()
        good_mod = _make_module_with_class(FakeCustomCommand)

        def import_side_effect(name):
            if name == "commands.custom_commands.good_cmd.command":
                return good_mod
            raise ImportError(f"No module named '{name}'")

        self._discover(svc, tmp_path, ["good_cmd"], import_side_effect)

        assert "test_custom" in svc._commands_cache
        assert svc.get_failed_modules() == {}
