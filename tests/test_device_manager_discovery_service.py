"""Tests for DeviceManagerDiscoveryService.

Tests manager discovery, secret validation, import error handling,
get_manager, get_all_managers_for_snapshot, and singleton pattern.
"""

import threading
from unittest.mock import MagicMock, patch

import pytest

from core.ijarvis_device_manager import DeviceManagerDevice, IJarvisDeviceManager
from utils.device_manager_discovery_service import (
    DeviceManagerDiscoveryService,
    get_device_manager_discovery_service,
)


# =============================================================================
# Stub managers for testing
# =============================================================================


class StubManagerA(IJarvisDeviceManager):
    @property
    def name(self) -> str:
        return "stub_a"

    @property
    def friendly_name(self) -> str:
        return "Stub A"

    @property
    def can_edit_devices(self) -> bool:
        return True

    async def collect_devices(self) -> list[DeviceManagerDevice]:
        return []


class StubManagerB(IJarvisDeviceManager):
    @property
    def name(self) -> str:
        return "stub_b"

    @property
    def friendly_name(self) -> str:
        return "Stub B"

    @property
    def can_edit_devices(self) -> bool:
        return False

    async def collect_devices(self) -> list[DeviceManagerDevice]:
        return []


@pytest.fixture
def fresh_service() -> DeviceManagerDiscoveryService:
    """Create a fresh service for each test."""
    return DeviceManagerDiscoveryService()


# =============================================================================
# Service creation tests
# =============================================================================


class TestServiceCreation:
    def test_create_service(self, fresh_service: DeviceManagerDiscoveryService) -> None:
        assert fresh_service is not None
        assert fresh_service._discovered is False

    def test_cache_initially_empty(self, fresh_service: DeviceManagerDiscoveryService) -> None:
        assert fresh_service._managers_cache == {}


# =============================================================================
# discover_managers tests
# =============================================================================


class TestDiscoverManagers:
    def test_discover_no_package(self, fresh_service: DeviceManagerDiscoveryService) -> None:
        """No device_managers package returns empty dict."""
        with patch.dict("sys.modules", {"device_managers": None}):
            with patch("utils.device_manager_discovery_service.importlib") as mock_importlib:
                mock_importlib.import_module.side_effect = ImportError("No module")
                result = fresh_service.discover_managers()
                assert result == {}

    def test_discover_finds_managers(self, fresh_service: DeviceManagerDiscoveryService) -> None:
        """Discovery finds IJarvisDeviceManager implementations."""
        mock_module = MagicMock()
        mock_module.StubManagerA = StubManagerA

        mock_device_managers = MagicMock()
        mock_device_managers.__path__ = ["/fake/path"]

        with patch.dict("sys.modules", {"device_managers": mock_device_managers}):
            with patch("pkgutil.iter_modules") as mock_iter:
                mock_iter.return_value = [(None, "stub_a_module", None)]

                with patch("importlib.import_module") as mock_import:
                    mock_import.return_value = mock_module

                    with patch.object(StubManagerA, "validate_secrets", return_value=[]):
                        result = fresh_service.discover_managers()

                        assert "stub_a" in result
                        assert isinstance(result["stub_a"], StubManagerA)

    def test_discover_skips_missing_secrets(
        self, fresh_service: DeviceManagerDiscoveryService
    ) -> None:
        """Managers with missing secrets are skipped."""
        mock_module = MagicMock()
        mock_module.StubManagerA = StubManagerA

        mock_device_managers = MagicMock()
        mock_device_managers.__path__ = ["/fake/path"]

        with patch.dict("sys.modules", {"device_managers": mock_device_managers}):
            with patch("pkgutil.iter_modules") as mock_iter:
                mock_iter.return_value = [(None, "stub_module", None)]

                with patch("importlib.import_module") as mock_import:
                    mock_import.return_value = mock_module

                    with patch.object(
                        StubManagerA,
                        "validate_secrets",
                        return_value=["MISSING_KEY"],
                    ):
                        result = fresh_service.discover_managers()
                        assert len(result) == 0

    def test_discover_skips_import_error(
        self, fresh_service: DeviceManagerDiscoveryService
    ) -> None:
        """Modules with missing pip packages are skipped (not errored)."""
        mock_device_managers = MagicMock()
        mock_device_managers.__path__ = ["/fake/path"]

        with patch.dict("sys.modules", {"device_managers": mock_device_managers}):
            with patch("pkgutil.iter_modules") as mock_iter:
                mock_iter.return_value = [(None, "missing_dep_module", None)]

                with patch("importlib.import_module") as mock_import:
                    mock_import.side_effect = ImportError("No module named 'some_lib'")

                    result = fresh_service.discover_managers()
                    assert result == {}

    def test_discover_sets_discovered_flag(
        self, fresh_service: DeviceManagerDiscoveryService
    ) -> None:
        """After discovery, _discovered flag is set."""
        mock_device_managers = MagicMock()
        mock_device_managers.__path__ = ["/fake/path"]

        with patch.dict("sys.modules", {"device_managers": mock_device_managers}):
            with patch("pkgutil.iter_modules", return_value=[]):
                fresh_service.discover_managers()
                assert fresh_service._discovered is True

    def test_discover_handles_generic_exception(
        self, fresh_service: DeviceManagerDiscoveryService
    ) -> None:
        """Generic exceptions during module loading are caught and logged."""
        mock_device_managers = MagicMock()
        mock_device_managers.__path__ = ["/fake/path"]

        with patch.dict("sys.modules", {"device_managers": mock_device_managers}):
            with patch("pkgutil.iter_modules") as mock_iter:
                mock_iter.return_value = [(None, "bad_module", None)]

                with patch("importlib.import_module") as mock_import:
                    mock_import.side_effect = RuntimeError("Unexpected error")

                    result = fresh_service.discover_managers()
                    assert result == {}


# =============================================================================
# get_manager tests
# =============================================================================


class TestGetManager:
    def test_get_manager_found(self, fresh_service: DeviceManagerDiscoveryService) -> None:
        mgr = StubManagerA()
        fresh_service._managers_cache = {"stub_a": mgr}
        fresh_service._discovered = True

        result = fresh_service.get_manager("stub_a")
        assert result is mgr

    def test_get_manager_not_found(self, fresh_service: DeviceManagerDiscoveryService) -> None:
        fresh_service._managers_cache = {}
        fresh_service._discovered = True

        result = fresh_service.get_manager("nonexistent")
        assert result is None

    def test_get_manager_triggers_discovery(
        self, fresh_service: DeviceManagerDiscoveryService
    ) -> None:
        """get_manager triggers discovery if not yet discovered."""
        assert fresh_service._discovered is False

        with patch.object(fresh_service, "_do_discover_managers") as mock_discover:
            mock_discover.return_value = {}
            fresh_service.get_manager("any")
            mock_discover.assert_called_once()


# =============================================================================
# get_all_managers tests
# =============================================================================


class TestGetAllManagers:
    def test_get_all_managers(self, fresh_service: DeviceManagerDiscoveryService) -> None:
        mgr_a = StubManagerA()
        mgr_b = StubManagerB()
        fresh_service._managers_cache = {"stub_a": mgr_a, "stub_b": mgr_b}
        fresh_service._discovered = True

        result = fresh_service.get_all_managers()
        assert len(result) == 2
        assert result["stub_a"] is mgr_a
        assert result["stub_b"] is mgr_b

    def test_get_all_returns_copy(self, fresh_service: DeviceManagerDiscoveryService) -> None:
        """Modifying the result does not affect the internal cache."""
        fresh_service._managers_cache = {"stub_a": StubManagerA()}
        fresh_service._discovered = True

        result = fresh_service.get_all_managers()
        result["extra"] = MagicMock()
        assert "extra" not in fresh_service._managers_cache

    def test_get_all_triggers_discovery(
        self, fresh_service: DeviceManagerDiscoveryService
    ) -> None:
        assert fresh_service._discovered is False

        with patch.object(fresh_service, "_do_discover_managers") as mock_discover:
            mock_discover.return_value = {}
            fresh_service.get_all_managers()
            mock_discover.assert_called_once()


# =============================================================================
# get_all_managers_for_snapshot tests
# =============================================================================


class TestGetAllManagersForSnapshot:
    def test_includes_managers_with_missing_secrets(
        self, fresh_service: DeviceManagerDiscoveryService
    ) -> None:
        """Snapshot method includes managers even when secrets are missing."""
        mock_module = MagicMock()
        mock_module.StubManagerA = StubManagerA

        mock_device_managers = MagicMock()
        mock_device_managers.__path__ = ["/fake/path"]

        with patch.dict("sys.modules", {"device_managers": mock_device_managers}):
            with patch("pkgutil.iter_modules") as mock_iter:
                mock_iter.return_value = [(None, "stub_module", None)]

                with patch("importlib.import_module") as mock_import:
                    mock_import.return_value = mock_module

                    # validate_secrets would return missing keys, but snapshot ignores that
                    result = fresh_service.get_all_managers_for_snapshot()

                    assert "stub_a" in result
                    assert isinstance(result["stub_a"], StubManagerA)

    def test_does_not_update_cache(
        self, fresh_service: DeviceManagerDiscoveryService
    ) -> None:
        """Snapshot method does NOT update the internal cache."""
        mock_module = MagicMock()
        mock_module.StubManagerA = StubManagerA

        mock_device_managers = MagicMock()
        mock_device_managers.__path__ = ["/fake/path"]

        with patch.dict("sys.modules", {"device_managers": mock_device_managers}):
            with patch("pkgutil.iter_modules") as mock_iter:
                mock_iter.return_value = [(None, "stub_module", None)]

                with patch("importlib.import_module") as mock_import:
                    mock_import.return_value = mock_module

                    fresh_service.get_all_managers_for_snapshot()

                    assert fresh_service._managers_cache == {}
                    assert fresh_service._discovered is False

    def test_skips_import_error(
        self, fresh_service: DeviceManagerDiscoveryService
    ) -> None:
        """Snapshot method skips modules with missing pip packages."""
        mock_device_managers = MagicMock()
        mock_device_managers.__path__ = ["/fake/path"]

        with patch.dict("sys.modules", {"device_managers": mock_device_managers}):
            with patch("pkgutil.iter_modules") as mock_iter:
                mock_iter.return_value = [(None, "missing_dep", None)]

                with patch("importlib.import_module") as mock_import:
                    mock_import.side_effect = ImportError("No module")

                    result = fresh_service.get_all_managers_for_snapshot()
                    assert result == {}

    def test_no_package_returns_empty(
        self, fresh_service: DeviceManagerDiscoveryService
    ) -> None:
        """No device_managers package returns empty dict."""
        with patch.dict("sys.modules", {"device_managers": None}):
            with patch("importlib.import_module", side_effect=ImportError):
                result = fresh_service.get_all_managers_for_snapshot()
                assert result == {}


# =============================================================================
# Singleton tests
# =============================================================================


class TestSingleton:
    def test_returns_same_instance(self) -> None:
        import utils.device_manager_discovery_service as module

        module._device_manager_discovery_service = None

        service1 = get_device_manager_discovery_service()
        service2 = get_device_manager_discovery_service()

        assert service1 is service2

        # Cleanup
        module._device_manager_discovery_service = None


# =============================================================================
# Refresh tests
# =============================================================================


class TestRefresh:
    def test_refresh_resets_and_rediscovers(
        self, fresh_service: DeviceManagerDiscoveryService
    ) -> None:
        fresh_service._discovered = True

        with patch.object(fresh_service, "discover_managers") as mock_discover:
            mock_discover.return_value = {}
            fresh_service.refresh()

            assert fresh_service._discovered is False
            mock_discover.assert_called_once()


# =============================================================================
# Thread safety tests
# =============================================================================


class TestThreadSafety:
    def test_concurrent_get_manager(self, fresh_service: DeviceManagerDiscoveryService) -> None:
        """Multiple threads calling get_manager should not raise."""
        results: list[IJarvisDeviceManager | None] = []
        errors: list[Exception] = []

        def _get() -> None:
            try:
                with patch.object(fresh_service, "_do_discover_managers", return_value={}):
                    result = fresh_service.get_manager("any")
                    results.append(result)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=_get) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert len(results) == 10
