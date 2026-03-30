"""Tests that command discovery picks up newly-installed commands without restart.

Validates the importlib.invalidate_caches() + sys.modules cleanup fix in
CommandDiscoveryService._discover_commands().

Uses real filesystem operations (creates/removes temp command packages) to prove
that Python's import caches are properly invalidated between discovery cycles.
"""

import importlib
import pkgutil
import shutil
import sys
from pathlib import Path

import pytest

# Absolute path to the custom_commands directory
_CUSTOM_COMMANDS_DIR = Path(__file__).resolve().parent.parent / "commands" / "custom_commands"

# Unique name unlikely to collide with real commands
_TEST_PKG_NAME = "_hot_reload_test_pkg"
_TEST_COMMAND_NAME = "hot_reload_test"


def _install_fake_command() -> Path:
    """Create a temporary command package in custom_commands/.

    Writes a minimal IJarvisCommand subclass that can be imported and instantiated.
    Uses the SDK base class if available, falls back to core.
    """
    pkg_dir = _CUSTOM_COMMANDS_DIR / _TEST_PKG_NAME
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / "__init__.py").write_text("")
    (pkg_dir / "command.py").write_text(
        "from core.ijarvis_command import IJarvisCommand\n"
        "from core.command_response import CommandResponse\n"
        "\n"
        "\n"
        "class HotReloadTestCommand(IJarvisCommand):\n"
        "    @property\n"
        "    def command_name(self) -> str:\n"
        f'        return "{_TEST_COMMAND_NAME}"\n'
        "\n"
        "    @property\n"
        "    def description(self) -> str:\n"
        '        return "Temporary command for hot-reload test"\n'
        "\n"
        "    @property\n"
        "    def parameters(self):\n"
        "        return []\n"
        "\n"
        "    @property\n"
        "    def required_secrets(self):\n"
        "        return []\n"
        "\n"
        "    @property\n"
        "    def keywords(self):\n"
        '        return ["hotreload"]\n'
        "\n"
        "    def generate_prompt_examples(self):\n"
        "        return []\n"
        "\n"
        "    def generate_adapter_examples(self):\n"
        "        return []\n"
        "\n"
        "    def run(self, request_info, **kwargs):\n"
        '        return CommandResponse.success_response({"source": "hot_reload"})\n'
    )
    return pkg_dir


def _remove_fake_command() -> None:
    """Remove the temporary command package and clean up sys.modules."""
    pkg_dir = _CUSTOM_COMMANDS_DIR / _TEST_PKG_NAME
    if pkg_dir.exists():
        shutil.rmtree(pkg_dir)
    # Also clear any cached imports for the test package
    for key in list(sys.modules.keys()):
        if _TEST_PKG_NAME in key:
            del sys.modules[key]


@pytest.fixture(autouse=True)
def cleanup():
    """Ensure the temporary command is always cleaned up."""
    _remove_fake_command()
    yield
    _remove_fake_command()


class TestHotReloadImportCacheInvalidation:
    """Test that importlib.invalidate_caches() + sys.modules cleanup
    allows pkgutil.iter_modules() to see new directories on disk.

    This is a focused unit test of the import-caching fix, isolated from
    the full _discover_commands() method and its dependencies.
    """

    def test_pkgutil_sees_new_directory_after_invalidate_caches(self):
        """pkgutil.iter_modules() misses new dirs without invalidate_caches()."""
        import commands.custom_commands as custom_pkg
        custom_path = custom_pkg.__path__

        # Baseline scan
        before = {name for _, name, is_pkg in pkgutil.iter_modules(custom_path) if is_pkg}
        assert _TEST_PKG_NAME not in before

        # Install on disk
        _install_fake_command()

        # Without invalidation, the stale FileFinder may miss the new dir.
        # (This depends on Python version; some do see it, some don't.)
        # After invalidation, it MUST be visible.
        importlib.invalidate_caches()
        # Clear module cache so the package is re-importable
        for key in list(sys.modules.keys()):
            if key.startswith("commands.custom_commands"):
                del sys.modules[key]
        import commands.custom_commands as fresh_pkg

        after = {name for _, name, is_pkg in pkgutil.iter_modules(fresh_pkg.__path__) if is_pkg}
        assert _TEST_PKG_NAME in after, (
            f"Expected {_TEST_PKG_NAME} in iter_modules after invalidate_caches, got: {after}"
        )

    def test_removed_directory_disappears_after_invalidate_caches(self):
        """A removed directory disappears from iter_modules after invalidation."""
        # Install first
        _install_fake_command()
        importlib.invalidate_caches()
        for key in list(sys.modules.keys()):
            if key.startswith("commands.custom_commands"):
                del sys.modules[key]
        import commands.custom_commands as pkg1

        found = {name for _, name, is_pkg in pkgutil.iter_modules(pkg1.__path__) if is_pkg}
        assert _TEST_PKG_NAME in found

        # Remove
        _remove_fake_command()
        importlib.invalidate_caches()
        for key in list(sys.modules.keys()):
            if key.startswith("commands.custom_commands"):
                del sys.modules[key]
        import commands.custom_commands as pkg2

        gone = {name for _, name, is_pkg in pkgutil.iter_modules(pkg2.__path__) if is_pkg}
        assert _TEST_PKG_NAME not in gone

    def test_full_cycle_install_remove_reinstall(self):
        """Full cycle: absent -> install -> remove -> reinstall."""
        def scan():
            importlib.invalidate_caches()
            for key in list(sys.modules.keys()):
                if key.startswith("commands.custom_commands"):
                    del sys.modules[key]
            import commands.custom_commands as pkg
            return {name for _, name, is_pkg in pkgutil.iter_modules(pkg.__path__) if is_pkg}

        # Absent
        assert _TEST_PKG_NAME not in scan()

        # Install
        _install_fake_command()
        assert _TEST_PKG_NAME in scan()

        # Remove
        _remove_fake_command()
        assert _TEST_PKG_NAME not in scan()

        # Reinstall
        _install_fake_command()
        assert _TEST_PKG_NAME in scan()
