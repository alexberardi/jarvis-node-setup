import importlib
import pkgutil
import sys
import threading
import time
from typing import Dict, List, Optional

from jarvis_log_client import JarvisLogger

from jarvis_command_sdk import IJarvisCommand
from db import SessionLocal
from repositories.command_registry_repository import CommandRegistryRepository

logger = JarvisLogger(service="jarvis-node")

# Community packages (Pantry) import from jarvis_command_sdk, not jarvis_command_sdk.
# Both define IJarvisCommand but they're different classes, so issubclass() fails.
# We check against both so custom commands are discovered properly.
try:
    from jarvis_command_sdk import IJarvisCommand as SDKIJarvisCommand
    _COMMAND_BASES: tuple[type, ...] = (IJarvisCommand, SDKIJarvisCommand)
except ImportError:
    _COMMAND_BASES = (IJarvisCommand,)


def _get_overridable_builtin_names() -> set[str]:
    """Built-in command names marked ``overridable = True``.

    Lazy import — command_store_service imports this module's accessor at
    install time, so a top-level import here would be circular.
    """
    try:
        from services.command_store_service import _get_overridable_builtin_command_names
        return _get_overridable_builtin_command_names()
    except Exception as e:
        logger.warning("Could not resolve overridable built-ins", error=str(e))
        return set()


class CommandDiscoveryService:
    def __init__(self):
        self._commands_cache: Dict[str, IJarvisCommand] = {}
        # Custom command modules whose import/instantiation raised:
        # module name -> error string. Surfaced as synthetic import_failed
        # snapshot entries and in report_tools package health, so a broken
        # Pantry install shows a "failed to load" badge instead of the
        # command silently vanishing from every list.
        self._failed_modules: Dict[str, str] = {}
        self._last_refresh = 0
        self._lock = threading.Lock()
        # No background poll. Discovery runs on demand: CommandExecutionService
        # forces an initial refresh in its __init__, and every legitimate
        # disk-change path (Pantry install / uninstall / test_install / config
        # push / mqtt-driven settings snapshot) calls refresh_now() explicitly.
        # The old 600s polling thread popped commands.custom_commands.* from
        # sys.modules and re-imported them every cycle, retaining ~17
        # module objects + their function/type/dict tables per refresh —
        # measured ~970 KB / 35 min growth in main's heap census. The polling
        # was redundant with the install-driven refresh path. Dropped.

    def _discover_commands(self):
        """Discover all IJarvisCommand implementations from built-in and custom commands."""
        # Invalidate Python's import system caches so pkgutil.iter_modules()
        # sees newly-installed package directories on disk.
        importlib.invalidate_caches()

        # Remove cached custom_commands modules so importlib.import_module()
        # re-executes new module files instead of returning stale cache hits.
        # `pop(key, None)` instead of `del` so a concurrent caller (background
        # refresh thread + a mobile-triggered settings snapshot can both land
        # here at once) doesn't crash the second one with KeyError, which then
        # surfaces as "settings snapshot error: 'commands.custom_commands'" on
        # the mobile app.
        for key in list(sys.modules.keys()):
            if key.startswith("commands.custom_commands"):
                sys.modules.pop(key, None)

        from services.command_store_service import register_package_lib_paths
        register_package_lib_paths()

        import commands

        new_commands: Dict[str, IJarvisCommand] = {}
        new_failed: Dict[str, str] = {}

        # Fetch registry once so custom commands can override disabled built-ins
        registry: Dict[str, bool] = {}
        try:
            db = SessionLocal()
            try:
                repo = CommandRegistryRepository(db)
                registry = repo.get_all()
            finally:
                db.close()
        except Exception:
            pass  # Registry unavailable — all commands default to enabled

        # 1. Scan built-in commands (commands/*.py)
        self._scan_package(commands, "commands", new_commands)

        # 2. Scan custom commands (commands/custom_commands/*/)
        try:
            import commands.custom_commands as custom_pkg
            for _, subpkg_name, is_pkg in pkgutil.iter_modules(custom_pkg.__path__):
                if not is_pkg:
                    continue  # Custom commands must be packages (directories)
                try:
                    module = importlib.import_module(f"commands.custom_commands.{subpkg_name}.command")
                    for attr in dir(module):
                        cls = getattr(module, attr)
                        if (isinstance(cls, type)
                                and issubclass(cls, _COMMAND_BASES)
                                and cls not in _COMMAND_BASES):
                            instance = cls()
                            name = instance.command_name
                            if name in new_commands:
                                if self._custom_wins_conflict(name, registry, subpkg_name):
                                    new_commands[name] = instance
                                continue
                            new_commands[name] = instance
                except Exception as e:
                    new_failed[subpkg_name] = str(e)
                    logger.error("Error loading custom command", module=subpkg_name, error=str(e))
        except ImportError:
            pass  # custom_commands package doesn't exist yet

        # 3. Scan test commands (commands/test_commands/*/)
        try:
            import commands.test_commands as test_pkg
            for _, subpkg_name, is_pkg in pkgutil.iter_modules(test_pkg.__path__):
                if not is_pkg:
                    continue
                try:
                    module = importlib.import_module(f"commands.test_commands.{subpkg_name}.command")
                    for attr in dir(module):
                        cls = getattr(module, attr)
                        if (isinstance(cls, type)
                                and issubclass(cls, _COMMAND_BASES)
                                and cls not in _COMMAND_BASES):
                            instance = cls()
                            name = instance.command_name
                            if name in new_commands:
                                logger.warning(
                                    "Test command name conflicts, skipping",
                                    test_command=name,
                                    test_module=subpkg_name,
                                )
                                continue
                            new_commands[name] = instance
                except Exception as e:
                    logger.error("Error loading test command", module=subpkg_name, error=str(e))
        except ImportError:
            pass  # test_commands package doesn't exist yet

        with self._lock:
            self._commands_cache = new_commands
            self._failed_modules = new_failed
            self._last_refresh = time.time()

    def _custom_wins_conflict(
        self, name: str, registry: Dict[str, bool], subpkg_name: str
    ) -> bool:
        """Decide whether a custom command replaces a same-name built-in.

        An *overridable* built-in always yields to an installed custom command,
        regardless of registry state — the registry's enabled flag can't gate
        the takeover because CC command_registry config pushes re-enable the
        name (CC doesn't know about node-local overrides), and a disabled name
        is filtered from advertised tools entirely. The flag governs whether
        the (winning) command is advertised, nothing more.
        """
        if name in _get_overridable_builtin_names():
            logger.info(
                "Custom command overriding overridable built-in",
                custom_command=name,
                custom_module=subpkg_name,
            )
            return True
        # Allow custom command to override a DISABLED built-in
        if not registry.get(name, True):
            logger.info(
                "Custom command overriding disabled built-in",
                custom_command=name,
                custom_module=subpkg_name,
            )
            return True
        logger.warning(
            "Custom command name conflicts with built-in, skipping",
            custom_command=name,
            custom_module=subpkg_name,
        )
        return False

    def _scan_package(self, package, package_path: str, commands_dict: Dict[str, IJarvisCommand]) -> None:
        """Scan a package for IJarvisCommand implementations."""
        for _, module_name, _ in pkgutil.iter_modules(package.__path__):
            try:
                module = importlib.import_module(f"{package_path}.{module_name}")
                for attr in dir(module):
                    cls = getattr(module, attr)
                    if (isinstance(cls, type)
                            and issubclass(cls, IJarvisCommand)
                            and cls is not IJarvisCommand):
                        instance = cls()
                        commands_dict[instance.command_name] = instance
            except Exception as e:
                logger.error("Error loading command module", module=module_name, error=str(e))

    def get_command(self, command_name: str) -> Optional[IJarvisCommand]:
        """Get a specific command by name"""
        with self._lock:
            return self._commands_cache.get(command_name)

    def get_all_commands(self, include_disabled: bool = False) -> Dict[str, IJarvisCommand]:
        """Get all discovered commands.

        Args:
            include_disabled: If True, return all commands including disabled ones.
                            If False (default), filter out disabled commands.
        """
        with self._lock:
            if include_disabled:
                return self._commands_cache.copy()
            return self._filter_enabled(self._commands_cache)

    def get_failed_modules(self) -> Dict[str, str]:
        """Get custom command modules whose import/instantiation raised.

        Returns:
            Dict mapping module name to error string (from the last
            discovery pass).
        """
        with self._lock:
            return self._failed_modules.copy()

    def _filter_enabled(self, commands: Dict[str, IJarvisCommand]) -> Dict[str, IJarvisCommand]:
        """Filter out disabled commands using the command_registry table."""
        try:
            db = SessionLocal()
            try:
                repo = CommandRegistryRepository(db)
                registry = repo.get_all()
            finally:
                db.close()
        except Exception as e:
            logger.warning("Failed to read command registry, returning all commands", error=str(e))
            return commands.copy()

        # Commands not in registry default to enabled
        return {
            name: cmd for name, cmd in commands.items()
            if registry.get(name, True)
        }

    def get_available_commands_schema(self) -> List[IJarvisCommand]:
        """Get all available (enabled) commands as objects (for LLM)"""
        return list(self.get_all_commands(include_disabled=False).values())

    def refresh_now(self):
        """Force an immediate refresh of commands"""
        self._discover_commands()


# Global instance
_command_discovery_service: Optional[CommandDiscoveryService] = None
_init_lock = threading.Lock()


def get_command_discovery_service() -> CommandDiscoveryService:
    """Get the global command discovery service instance (thread-safe)."""
    global _command_discovery_service
    if _command_discovery_service is None:
        with _init_lock:
            if _command_discovery_service is None:
                _command_discovery_service = CommandDiscoveryService()
    return _command_discovery_service