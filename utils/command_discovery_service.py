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


class CommandDiscoveryService:
    def __init__(self, refresh_interval: int = 600):
        self.refresh_interval = refresh_interval
        self._commands_cache: Dict[str, IJarvisCommand] = {}
        self._last_refresh = 0
        self._lock = threading.Lock()

        # Start background refresh thread
        self._refresh_thread = threading.Thread(target=self._background_refresh, daemon=True)
        self._refresh_thread.start()

    def _background_refresh(self) -> None:
        """Background thread that refreshes commands every refresh_interval seconds."""
        while True:
            if _shutdown_event is not None:
                _shutdown_event.wait(timeout=self.refresh_interval)
                if _shutdown_event.is_set():
                    return
            else:
                time.sleep(self.refresh_interval)
            try:
                self._discover_commands()
                logger.debug("Refreshed commands", count=len(self._commands_cache))
            except Exception as e:
                logger.error("Error refreshing commands", error=str(e))

    def _discover_commands(self):
        """Discover all IJarvisCommand implementations from built-in and custom commands."""
        # Invalidate Python's import system caches so pkgutil.iter_modules()
        # sees newly-installed package directories on disk.
        importlib.invalidate_caches()

        # Remove cached custom_commands modules so importlib.import_module()
        # re-executes new module files instead of returning stale cache hits.
        for key in list(sys.modules.keys()):
            if key.startswith("commands.custom_commands"):
                del sys.modules[key]

        from services.command_store_service import register_package_lib_paths
        register_package_lib_paths()

        import commands

        new_commands: Dict[str, IJarvisCommand] = {}

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
                                # Allow custom command to override a DISABLED built-in
                                if not registry.get(name, True):
                                    logger.info(
                                        "Custom command overriding disabled built-in",
                                        custom_command=name,
                                        custom_module=subpkg_name,
                                    )
                                    new_commands[name] = instance
                                else:
                                    logger.warning(
                                        "Custom command name conflicts with built-in, skipping",
                                        custom_command=name,
                                        custom_module=subpkg_name,
                                    )
                                continue
                            new_commands[name] = instance
                except Exception as e:
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
            self._last_refresh = time.time()

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

# Shutdown event shared with main.py for graceful shutdown
_shutdown_event: Optional[threading.Event] = None


def set_shutdown_event(event: threading.Event) -> None:
    """Accept a shutdown event from main.py for graceful shutdown of background refresh."""
    global _shutdown_event
    _shutdown_event = event


def get_command_discovery_service() -> CommandDiscoveryService:
    """Get the global command discovery service instance (thread-safe)."""
    global _command_discovery_service
    if _command_discovery_service is None:
        with _init_lock:
            if _command_discovery_service is None:
                _command_discovery_service = CommandDiscoveryService()
    return _command_discovery_service