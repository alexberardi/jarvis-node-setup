"""
Device family discovery service for finding and instantiating IJarvisDeviceProtocol implementations.

Mirrors the AgentDiscoveryService pattern:
- Scans device_families/ package for IJarvisDeviceProtocol implementations
- Validates secrets before registering families
- Gracefully skips families with missing pip packages (ImportError)
- Provides singleton accessor for use throughout the application
"""

import importlib
import pkgutil
import sys
import threading
from pathlib import Path

from jarvis_log_client import JarvisLogger

from jarvis_command_sdk import IJarvisDeviceProtocol

logger = JarvisLogger(service="jarvis-node")


class DeviceFamilyDiscoveryService:
    """Discovers and manages IJarvisDeviceProtocol implementations.

    Families are discovered once at startup. Families with missing secrets
    or missing pip dependencies are logged but skipped (not errored).

    Thread safety:
        - Uses RLock for reentrant acquisition during discovery
        - All public methods acquire the lock before accessing state
    """

    def __init__(self) -> None:
        self._families_cache: dict[str, IJarvisDeviceProtocol] = {}
        self._lock = threading.RLock()
        self._discovered = False

    def discover_families(self) -> dict[str, IJarvisDeviceProtocol]:
        """Discover all IJarvisDeviceProtocol implementations in the device_families package.

        Families with missing required secrets or missing pip packages are
        logged but skipped.

        Returns:
            Dict mapping protocol name to protocol instance.
        """
        with self._lock:
            return self._do_discover_families()

    def _do_discover_families(self) -> dict[str, IJarvisDeviceProtocol]:
        """Internal discovery implementation. Caller must hold _lock.

        Returns:
            Dict mapping protocol name to protocol instance.
        """
        try:
            import device_families
        except ImportError:
            logger.warning("No device_families package found, skipping family discovery")
            return {}

        # Invalidate Python's import caches so newly-installed or
        # reinstalled Pantry packages are picked up without a restart
        # (same pattern as command_discovery_service._discover_commands).
        importlib.invalidate_caches()
        for key in list(sys.modules.keys()):
            if key.startswith("device_families.custom_families"):
                del sys.modules[key]

        new_families: dict[str, IJarvisDeviceProtocol] = {}

        # Scan built-in families
        for _, module_name, _ in pkgutil.iter_modules(device_families.__path__):
            if module_name == "base":
                continue
            self._try_load_family(f"device_families.{module_name}", module_name, new_families)

        # Scan custom families (installed by Pantry)
        custom_families_dir = Path(device_families.__path__[0]).parent / "device_families" / "custom_families"
        if custom_families_dir.exists():
            for family_dir in custom_families_dir.iterdir():
                if family_dir.is_dir() and not family_dir.name.startswith("_"):
                    protocol_py = family_dir / "protocol.py"
                    if protocol_py.exists():
                        import sys
                        if str(family_dir) not in sys.path:
                            sys.path.insert(0, str(family_dir))
                        self._try_load_family(
                            f"device_families.custom_families.{family_dir.name}.protocol",
                            family_dir.name,
                            new_families,
                        )

        self._families_cache = new_families
        self._discovered = True

        logger.info("Device family discovery complete", count=len(new_families))
        return new_families

    def _try_load_family(
        self, module_path: str, module_name: str, families_dict: dict[str, IJarvisDeviceProtocol]
    ) -> None:
        """Try to load a device family from a module path."""
        try:
            module = importlib.import_module(module_path)

            for attr in dir(module):
                cls = getattr(module, attr)

                if (
                    isinstance(cls, type)
                    and issubclass(cls, IJarvisDeviceProtocol)
                    and cls is not IJarvisDeviceProtocol
                ):
                    instance = cls()

                    missing_secrets = instance.validate_secrets() if hasattr(instance, 'validate_secrets') else []
                    if missing_secrets:
                        logger.warning(
                            "Device family skipped due to missing secrets",
                            family=instance.protocol_name,
                            connection_type=instance.connection_type,
                            missing=missing_secrets,
                        )
                        continue

                    families_dict[instance.protocol_name] = instance
                    logger.debug(
                        "Discovered device family",
                        family=instance.protocol_name,
                        connection_type=instance.connection_type,
                        domains=instance.supported_domains,
                    )

        except ImportError as e:
            logger.debug(
                "Device family module skipped (missing dependency)",
                module=module_name,
                error=str(e),
            )
        except Exception as e:
            logger.error(
                "Error loading device family module",
                module=module_name,
                error=str(e),
            )

    def get_family(self, name: str) -> IJarvisDeviceProtocol | None:
        """Get a specific device family by protocol name.

        Args:
            name: Protocol name (e.g., 'lifx', 'kasa', 'govee').

        Returns:
            IJarvisDeviceProtocol instance if found, None otherwise.
        """
        with self._lock:
            if not self._discovered:
                self._do_discover_families()
            return self._families_cache.get(name)

    def get_all_families(self) -> dict[str, IJarvisDeviceProtocol]:
        """Get all discovered device families.

        Returns:
            Dict mapping protocol name to protocol instance.
        """
        with self._lock:
            if not self._discovered:
                self._do_discover_families()
            return self._families_cache.copy()

    def get_all_families_for_snapshot(self) -> dict[str, IJarvisDeviceProtocol]:
        """Get all device families for settings snapshot (no secret filtering).

        Unlike get_all_families() / discover_families(), this does NOT skip
        families with missing secrets. It still skips ImportError (missing pip
        packages) and the 'base' module.

        No caching — this is called infrequently (on-demand snapshot requests).

        Returns:
            Dict mapping protocol name to protocol instance.
        """
        try:
            import device_families
        except ImportError:
            logger.warning("No device_families package found, skipping snapshot scan")
            return {}

        families: dict[str, IJarvisDeviceProtocol] = {}

        for _, module_name, _ in pkgutil.iter_modules(device_families.__path__):
            if module_name == "base":
                continue

            try:
                module = importlib.import_module(f"device_families.{module_name}")

                for attr in dir(module):
                    cls = getattr(module, attr)

                    if (
                        isinstance(cls, type)
                        and issubclass(cls, IJarvisDeviceProtocol)
                        and cls is not IJarvisDeviceProtocol
                    ):
                        instance = cls()
                        families[instance.protocol_name] = instance
                        logger.debug(
                            "Found device family for snapshot",
                            family=instance.protocol_name,
                            connection_type=instance.connection_type,
                        )

            except ImportError as e:
                logger.debug(
                    "Device family module skipped for snapshot (missing dependency)",
                    module=module_name,
                    error=str(e),
                )
            except Exception as e:
                logger.error(
                    "Error loading device family module for snapshot",
                    module=module_name,
                    error=str(e),
                )

        # Scan custom families (installed by Pantry)
        custom_families_dir = Path(device_families.__path__[0]).parent / "device_families" / "custom_families"
        if custom_families_dir.exists():
            for family_dir in custom_families_dir.iterdir():
                if family_dir.is_dir() and not family_dir.name.startswith("_"):
                    protocol_py = family_dir / "protocol.py"
                    if protocol_py.exists():
                        import sys
                        if str(family_dir) not in sys.path:
                            sys.path.insert(0, str(family_dir))
                        try:
                            module = importlib.import_module(
                                f"device_families.custom_families.{family_dir.name}.protocol",
                            )
                            for attr in dir(module):
                                cls = getattr(module, attr)
                                if (
                                    isinstance(cls, type)
                                    and issubclass(cls, IJarvisDeviceProtocol)
                                    and cls is not IJarvisDeviceProtocol
                                ):
                                    instance = cls()
                                    families[instance.protocol_name] = instance
                        except Exception as e:
                            logger.debug(
                                "Custom device family skipped for snapshot",
                                family=family_dir.name,
                                error=str(e),
                            )

        return families

    def refresh(self) -> None:
        """Force a fresh discovery of device families."""
        self._discovered = False
        self.discover_families()


# Global singleton instance
_device_family_discovery_service: DeviceFamilyDiscoveryService | None = None


def get_device_family_discovery_service() -> DeviceFamilyDiscoveryService:
    """Get the global DeviceFamilyDiscoveryService instance.

    Returns:
        Singleton DeviceFamilyDiscoveryService instance.
    """
    global _device_family_discovery_service
    if _device_family_discovery_service is None:
        _device_family_discovery_service = DeviceFamilyDiscoveryService()
    return _device_family_discovery_service
