"""Device manager discovery service for finding IJarvisDeviceManager implementations.

Mirrors DeviceFamilyDiscoveryService:
- Scans device_managers/ package for IJarvisDeviceManager subclasses
- Validates secrets before registering managers
- Gracefully skips managers with missing pip packages (ImportError)
- Provides singleton accessor
"""

import importlib
import pkgutil
import threading
from pathlib import Path

from jarvis_log_client import JarvisLogger

from core.ijarvis_device_manager import IJarvisDeviceManager

# Community packages (Pantry) import from jarvis_command_sdk, not core.
# Both define IJarvisDeviceManager but they're different classes.
try:
    from jarvis_command_sdk import IJarvisDeviceManager as SDKIJarvisDeviceManager
    _MANAGER_BASES: tuple[type, ...] = (IJarvisDeviceManager, SDKIJarvisDeviceManager)
except ImportError:
    _MANAGER_BASES = (IJarvisDeviceManager,)

logger = JarvisLogger(service="jarvis-node")


class DeviceManagerDiscoveryService:
    """Discovers and manages IJarvisDeviceManager implementations.

    Managers are discovered once at startup.  Managers with missing secrets
    or missing pip dependencies are logged but skipped (not errored).

    Thread safety:
        Uses RLock for reentrant acquisition during discovery.
        All public methods acquire the lock before accessing state.
    """

    def __init__(self) -> None:
        self._managers_cache: dict[str, IJarvisDeviceManager] = {}
        self._lock = threading.RLock()
        self._discovered = False

    def discover_managers(self) -> dict[str, IJarvisDeviceManager]:
        """Discover all IJarvisDeviceManager implementations in the device_managers package.

        Managers with missing required secrets or missing pip packages are
        logged but skipped.

        Returns:
            Dict mapping manager name to manager instance.
        """
        with self._lock:
            return self._do_discover_managers()

    def _do_discover_managers(self) -> dict[str, IJarvisDeviceManager]:
        """Internal discovery implementation.  Caller must hold _lock."""
        from services.command_store_service import register_package_lib_paths
        register_package_lib_paths()

        try:
            import device_managers
        except ImportError:
            logger.warning("No device_managers package found, skipping manager discovery")
            return {}

        new_managers: dict[str, IJarvisDeviceManager] = {}

        # Scan built-in managers
        for _, module_name, _ in pkgutil.iter_modules(device_managers.__path__):
            self._try_load_manager(f"device_managers.{module_name}", module_name, new_managers)

        # Scan custom managers (installed by Pantry)
        custom_managers_dir = Path(device_managers.__path__[0]).parent / "device_managers" / "custom_managers"
        if custom_managers_dir.exists():
            for mgr_dir in custom_managers_dir.iterdir():
                if mgr_dir.is_dir() and not mgr_dir.name.startswith("_"):
                    # Look for any .py file that might contain the manager
                    for py_file in mgr_dir.glob("*.py"):
                        if py_file.name.startswith("_"):
                            continue
                        import sys
                        if str(mgr_dir) not in sys.path:
                            sys.path.insert(0, str(mgr_dir))
                        self._try_load_manager(
                            f"device_managers.custom_managers.{mgr_dir.name}.{py_file.stem}",
                            mgr_dir.name,
                            new_managers,
                        )

        self._managers_cache = new_managers
        self._discovered = True

        logger.info("Device manager discovery complete", count=len(new_managers))
        return new_managers

    def _try_load_manager(
        self, module_path: str, module_name: str, managers_dict: dict[str, IJarvisDeviceManager]
    ) -> None:
        """Try to load a device manager from a module path."""
        try:
            module = importlib.import_module(module_path)

            for attr in dir(module):
                cls = getattr(module, attr)

                if (
                    isinstance(cls, type)
                    and issubclass(cls, _MANAGER_BASES)
                    and cls not in _MANAGER_BASES
                ):
                    instance = cls()

                    if hasattr(instance, "validate_secrets"):
                        missing_secrets = instance.validate_secrets()
                        if missing_secrets:
                            logger.warning(
                                "Device manager skipped due to missing secrets",
                                manager=instance.name,
                                missing=missing_secrets,
                            )
                            continue

                    managers_dict[instance.name] = instance
                    logger.debug(
                        "Discovered device manager",
                        manager=instance.name,
                        can_edit=instance.can_edit_devices,
                    )

        except ImportError as e:
            logger.debug(
                "Device manager module skipped (missing dependency)",
                module=module_name,
                error=str(e),
            )
        except Exception as e:
            logger.error(
                "Error loading device manager module",
                module=module_name,
                error=str(e),
            )

    def get_manager(self, name: str) -> IJarvisDeviceManager | None:
        """Get a specific device manager by name.

        Args:
            name: Manager name (e.g., 'jarvis_direct', 'home_assistant').

        Returns:
            IJarvisDeviceManager instance if found, None otherwise.
        """
        with self._lock:
            if not self._discovered:
                self._do_discover_managers()
            return self._managers_cache.get(name)

    def get_all_managers(self) -> dict[str, IJarvisDeviceManager]:
        """Get all discovered device managers (only those with secrets configured).

        Returns:
            Dict mapping manager name to manager instance.
        """
        with self._lock:
            if not self._discovered:
                self._do_discover_managers()
            return self._managers_cache.copy()

    def get_all_managers_for_snapshot(self) -> dict[str, IJarvisDeviceManager]:
        """Get all device managers for settings snapshot (no secret filtering).

        Unlike get_all_managers(), this does NOT skip managers with missing
        secrets.  It still skips ImportError (missing pip packages).

        No caching — this is called infrequently (on-demand snapshot requests).

        Returns:
            Dict mapping manager name to manager instance.
        """
        try:
            import device_managers
        except ImportError:
            logger.warning("No device_managers package found, skipping snapshot scan")
            return {}

        managers: dict[str, IJarvisDeviceManager] = {}

        for _, module_name, _ in pkgutil.iter_modules(device_managers.__path__):
            try:
                module = importlib.import_module(f"device_managers.{module_name}")

                for attr in dir(module):
                    cls = getattr(module, attr)

                    if (
                        isinstance(cls, type)
                        and issubclass(cls, IJarvisDeviceManager)
                        and cls is not IJarvisDeviceManager
                    ):
                        instance = cls()
                        managers[instance.name] = instance
                        logger.debug(
                            "Found device manager for snapshot",
                            manager=instance.name,
                        )

            except ImportError as e:
                logger.debug(
                    "Device manager module skipped for snapshot (missing dependency)",
                    module=module_name,
                    error=str(e),
                )
            except Exception as e:
                logger.error(
                    "Error loading device manager module for snapshot",
                    module=module_name,
                    error=str(e),
                )

        return managers

    def refresh(self) -> None:
        """Force a fresh discovery of device managers."""
        self._discovered = False
        self.discover_managers()


# Global singleton instance
_device_manager_discovery_service: DeviceManagerDiscoveryService | None = None


def get_device_manager_discovery_service() -> DeviceManagerDiscoveryService:
    """Get the global DeviceManagerDiscoveryService instance."""
    global _device_manager_discovery_service
    if _device_manager_discovery_service is None:
        _device_manager_discovery_service = DeviceManagerDiscoveryService()
    return _device_manager_discovery_service
