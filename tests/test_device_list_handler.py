"""Tests for device_list_handler.

Tests run_collect_and_upload, _upload_results, _upload_error,
and handling of unknown manager names.
"""

from dataclasses import asdict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.ijarvis_device_manager import DeviceManagerDevice
from services.device_list_handler import (
    _async_collect_and_upload,
    _upload_error,
    _upload_results,
    run_collect_and_upload,
)


# =============================================================================
# Helpers
# =============================================================================


def _make_device(
    name: str = "Test Light",
    domain: str = "light",
    entity_id: str = "light.test",
) -> DeviceManagerDevice:
    return DeviceManagerDevice(name=name, domain=domain, entity_id=entity_id)


def _make_mock_manager(
    name: str = "jarvis_direct",
    can_edit: bool = True,
    devices: list[DeviceManagerDevice] | None = None,
) -> MagicMock:
    mgr = MagicMock()
    mgr.name = name
    mgr.can_edit_devices = can_edit
    mgr.collect_devices = AsyncMock(return_value=devices or [])
    return mgr


# =============================================================================
# _async_collect_and_upload tests
# =============================================================================


class TestAsyncCollectAndUpload:
    @pytest.mark.asyncio
    async def test_unknown_manager_uploads_error(self) -> None:
        """Unknown manager_name results in an error upload to CC."""
        mock_discovery = MagicMock()
        mock_discovery.get_manager.return_value = None

        with patch(
            "services.device_list_handler.get_device_manager_discovery_service",
            return_value=mock_discovery,
        ):
            with patch("services.device_list_handler._upload_error") as mock_upload_err:
                await _async_collect_and_upload("req-123", "nonexistent_manager")

                mock_upload_err.assert_called_once()
                args = mock_upload_err.call_args
                assert args[0][0] == "req-123"
                assert "nonexistent_manager" in args[0][1]

    @pytest.mark.asyncio
    async def test_calls_manager_collect_devices(self) -> None:
        """Calls the selected manager's collect_devices and uploads results."""
        devices = [_make_device(name="Lamp")]
        mgr = _make_mock_manager(devices=devices)

        mock_discovery = MagicMock()
        mock_discovery.get_manager.return_value = mgr

        with patch(
            "services.device_list_handler.get_device_manager_discovery_service",
            return_value=mock_discovery,
        ):
            with patch("services.device_list_handler._upload_results") as mock_upload:
                await _async_collect_and_upload("req-456", "jarvis_direct")

                mgr.collect_devices.assert_awaited_once()
                mock_upload.assert_called_once_with("req-456", mgr, devices)

    @pytest.mark.asyncio
    async def test_uses_correct_manager_name(self) -> None:
        """get_manager is called with the correct manager_name."""
        mock_discovery = MagicMock()
        mock_discovery.get_manager.return_value = None

        with patch(
            "services.device_list_handler.get_device_manager_discovery_service",
            return_value=mock_discovery,
        ):
            with patch("services.device_list_handler._upload_error"):
                await _async_collect_and_upload("req-789", "home_assistant")

                mock_discovery.get_manager.assert_called_once_with("home_assistant")


# =============================================================================
# _upload_results tests
# =============================================================================


class TestUploadResults:
    def test_posts_to_correct_url(self) -> None:
        """Results are POSTed to the correct CC endpoint."""
        devices = [_make_device()]
        mgr = _make_mock_manager(name="jarvis_direct", can_edit=True)

        with patch(
            "services.device_list_handler.get_command_center_url",
            return_value="http://localhost:7703",
        ):
            with patch("utils.config_service.Config") as mock_config:
                mock_config.get_str.return_value = "node-abc"

                with patch("services.device_list_handler.RestClient") as mock_rest:
                    mock_rest.post.return_value = {"ok": True}

                    _upload_results("req-123", mgr, devices)

                    call_args = mock_rest.post.call_args
                    url = call_args[0][0]
                    assert "node-abc" in url
                    assert "req-123" in url
                    assert url.endswith("/results")

    def test_payload_contains_devices_and_metadata(self) -> None:
        """Payload includes serialized devices, manager_name, and can_edit_devices."""
        devices = [
            _make_device(name="Light 1", entity_id="light.one"),
            _make_device(name="Light 2", entity_id="light.two"),
        ]
        mgr = _make_mock_manager(name="home_assistant", can_edit=False)

        with patch(
            "services.device_list_handler.get_command_center_url",
            return_value="http://localhost:7703",
        ):
            with patch("utils.config_service.Config") as mock_config:
                mock_config.get_str.return_value = "node-abc"

                with patch("services.device_list_handler.RestClient") as mock_rest:
                    mock_rest.post.return_value = {"ok": True}

                    _upload_results("req-123", mgr, devices)

                    call_args = mock_rest.post.call_args
                    payload = call_args[1]["data"] if "data" in call_args[1] else call_args[0][1]
                    assert payload["manager_name"] == "home_assistant"
                    assert payload["can_edit_devices"] is False
                    assert len(payload["devices"]) == 2

    def test_extra_field_stripped_from_devices(self) -> None:
        """The 'extra' field is removed from device dicts before upload."""
        dev = _make_device()
        dev.extra = {"brightness": 100}
        mgr = _make_mock_manager()

        with patch(
            "services.device_list_handler.get_command_center_url",
            return_value="http://localhost:7703",
        ):
            with patch("utils.config_service.Config") as mock_config:
                mock_config.get_str.return_value = "node-abc"

                with patch("services.device_list_handler.RestClient") as mock_rest:
                    mock_rest.post.return_value = {"ok": True}

                    _upload_results("req-123", mgr, [dev])

                    call_args = mock_rest.post.call_args
                    payload = call_args[1]["data"] if "data" in call_args[1] else call_args[0][1]
                    device_dict = payload["devices"][0]
                    assert "extra" not in device_dict

    def test_no_cc_url_does_not_post(self) -> None:
        """If CC URL cannot be resolved, no POST is made."""
        devices = [_make_device()]
        mgr = _make_mock_manager()

        with patch(
            "services.device_list_handler.get_command_center_url",
            return_value=None,
        ):
            with patch("services.device_list_handler.RestClient") as mock_rest:
                _upload_results("req-123", mgr, devices)
                mock_rest.post.assert_not_called()


# =============================================================================
# _upload_error tests
# =============================================================================


class TestUploadError:
    def test_posts_error_to_cc(self) -> None:
        """Error is POSTed to the CC results endpoint."""
        with patch(
            "services.device_list_handler.get_command_center_url",
            return_value="http://localhost:7703",
        ):
            with patch("utils.config_service.Config") as mock_config:
                mock_config.get_str.return_value = "node-xyz"

                with patch("services.device_list_handler.RestClient") as mock_rest:
                    _upload_error("req-err-1", "Something went wrong")

                    call_args = mock_rest.post.call_args
                    url = call_args[0][0]
                    assert "node-xyz" in url
                    assert "req-err-1" in url

                    payload = call_args[1]["data"] if "data" in call_args[1] else call_args[0][1]
                    assert payload["devices"] == []
                    assert payload["error"] == "Something went wrong"

    def test_no_cc_url_returns_silently(self) -> None:
        """If CC URL is not resolved, error upload is skipped silently."""
        with patch(
            "services.device_list_handler.get_command_center_url",
            return_value=None,
        ):
            with patch("services.device_list_handler.RestClient") as mock_rest:
                _upload_error("req-err-2", "fail")
                mock_rest.post.assert_not_called()


# =============================================================================
# run_collect_and_upload tests (sync wrapper)
# =============================================================================


class TestRunCollectAndUpload:
    def test_runs_async_collect(self) -> None:
        """Sync wrapper runs the async collect in a new event loop."""
        devices = [_make_device()]
        mgr = _make_mock_manager(devices=devices)

        mock_discovery = MagicMock()
        mock_discovery.get_manager.return_value = mgr

        with patch(
            "services.device_list_handler.get_device_manager_discovery_service",
            return_value=mock_discovery,
        ):
            with patch("services.device_list_handler._upload_results") as mock_upload:
                run_collect_and_upload("req-sync-1", "jarvis_direct")

                mock_upload.assert_called_once()

    def test_exception_uploads_error(self) -> None:
        """If collect_devices raises, error is uploaded to CC."""
        mgr = MagicMock()
        mgr.name = "jarvis_direct"
        mgr.collect_devices = AsyncMock(side_effect=RuntimeError("Scan failed"))

        mock_discovery = MagicMock()
        mock_discovery.get_manager.return_value = mgr

        with patch(
            "services.device_list_handler.get_device_manager_discovery_service",
            return_value=mock_discovery,
        ):
            with patch("services.device_list_handler._upload_error") as mock_err:
                run_collect_and_upload("req-fail-1", "jarvis_direct")

                mock_err.assert_called()
                args = mock_err.call_args[0]
                assert args[0] == "req-fail-1"
                assert "Scan failed" in args[1]
