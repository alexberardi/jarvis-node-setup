"""Tests for the node camera stream-source handler.

The handler resolves the device-protocol plugin for a camera, asks it for a
go2rtc source string, and POSTs {stream_source} (or {error}) back to CC. It
owns no protocol specifics itself.
"""

import pytest

import services.camera_credentials_handler as handler


class _Adapter:
    """Fake protocol adapter with an async get_stream_source."""

    def __init__(self, result):
        self._result = result

    async def get_stream_source(self, device):
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class _AdapterNoMethod:
    """An old-SDK adapter without the get_stream_source hook."""


class _Discovery:
    def __init__(self, adapter):
        self._adapter = adapter

    def get_family(self, name):
        return self._adapter


@pytest.fixture
def captured(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(handler, "_upload_result", lambda rid, data: calls.append(data))
    return calls


def _wire(monkeypatch, adapter):
    monkeypatch.setattr(
        "utils.device_family_discovery_service.get_device_family_discovery_service",
        lambda: _Discovery(adapter),
    )


_DETAILS = {
    "protocol": "nest",
    "cloud_id": "enterprises/p/devices/XYZ",
    "entity_id": "nest_doorbell",
    "domain": "camera",
}


def test_uploads_stream_source_from_adapter(monkeypatch, captured):
    _wire(monkeypatch, _Adapter("nest:?client_id=cid&device_id=XYZ"))
    handler.run_credentials_lookup_and_upload("req-1", dict(_DETAILS))
    assert captured == [{"stream_source": "nest:?client_id=cid&device_id=XYZ"}]


def test_missing_protocol_errors(monkeypatch, captured):
    _wire(monkeypatch, _Adapter("x"))
    handler.run_credentials_lookup_and_upload("req-2", {"cloud_id": "c"})
    assert "error" in captured[0] and "protocol" in captured[0]["error"]


def test_unknown_protocol_errors(monkeypatch, captured):
    _wire(monkeypatch, None)  # get_family returns None
    handler.run_credentials_lookup_and_upload("req-3", dict(_DETAILS))
    assert "error" in captured[0]
    assert "not available" in captured[0]["error"]


def test_adapter_without_hook_errors(monkeypatch, captured):
    _wire(monkeypatch, _AdapterNoMethod())
    handler.run_credentials_lookup_and_upload("req-4", dict(_DETAILS))
    assert "error" in captured[0]
    assert "not supported" in captured[0]["error"]


def test_adapter_returns_none_errors(monkeypatch, captured):
    _wire(monkeypatch, _Adapter(None))
    handler.run_credentials_lookup_and_upload("req-5", dict(_DETAILS))
    assert "error" in captured[0]  # e.g. "complete OAuth setup"


def test_adapter_exception_surfaces_error(monkeypatch, captured):
    _wire(monkeypatch, _Adapter(RuntimeError("boom")))
    handler.run_credentials_lookup_and_upload("req-6", dict(_DETAILS))
    assert captured[0]["error"] == "boom"
