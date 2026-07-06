"""Tests for NestProtocol.get_stream_source — the node-owned go2rtc source builder.

Exercises the runtime copy the node actually loads
(device_families/custom_families/nest/protocol.py). The canonical source repo
(jarvis-device-nest) is kept byte-identical.
"""

import asyncio
from urllib.parse import parse_qs, urlparse

import pytest

import jarvis_command_sdk.storage as storage_mod
from jarvis_command_sdk import DiscoveredDevice
from jarvis_command_sdk.storage import StorageBackend

from device_families.custom_families.nest.protocol import NestProtocol

CLOUD_ID = "enterprises/6810ba1e/devices/AVPHwXYZ"

_WORKING_SECRETS = {
    "NEST_CAMERA_SUPPORT": "on",
    "NEST_WEB_CLIENT_ID": "cid",
    "NEST_WEB_CLIENT_SECRET": "csec",
    "NEST_REFRESH_TOKEN": "rt",
    "NEST_PROJECT_ID": "6810ba1e",
    "NEST_ACCESS_TOKEN": "at",
}


class _FakeBackend(StorageBackend):
    def __init__(self, secrets: dict[str, str]) -> None:
        self._secrets = secrets

    def get_secret(self, key, scope, user_id=None):
        return self._secrets.get(key)

    # unused data/secret methods
    def save(self, *a, **k): ...
    def get(self, *a, **k): return None
    def get_all(self, *a, **k): return []
    def delete(self, *a, **k): return False
    def delete_all(self, *a, **k): return 0
    def set_secret(self, *a, **k): ...
    def delete_secret(self, *a, **k): ...


class _FakeResp:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _FakeAsyncClient:
    """Stands in for httpx.AsyncClient; records whether it was used."""

    calls: list[str] = []
    _resp: "_FakeResp | None" = None

    def __init__(self, *a, **k) -> None: ...
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False

    async def get(self, url, headers=None):
        _FakeAsyncClient.calls.append(url)
        return _FakeAsyncClient._resp


def _install_httpx(monkeypatch, resp: _FakeResp | None):
    import httpx
    _FakeAsyncClient.calls = []
    _FakeAsyncClient._resp = resp
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)


@pytest.fixture
def seed():
    prev = storage_mod.get_backend()

    def _seed(secrets: dict[str, str]):
        storage_mod.set_backend(_FakeBackend(secrets))

    yield _seed
    storage_mod._backend = prev


def _device(cloud_id: str = CLOUD_ID) -> DiscoveredDevice:
    return DiscoveredDevice(
        name="Front Door", domain="camera", manufacturer="Google",
        model="Nest Doorbell", protocol="nest", entity_id="nest_doorbell",
        cloud_id=cloud_id,
    )


def _run(device=None):
    return asyncio.run(NestProtocol().get_stream_source(device or _device()))


def _params(src: str) -> dict:
    assert src.startswith("nest:?")
    return {k: v[0] for k, v in parse_qs(urlparse("nest://?" + src[len("nest:?"):]).query).items()}


def test_webrtc_device_omits_protocols(seed, monkeypatch):
    seed(_WORKING_SECRETS)
    _install_httpx(monkeypatch, _FakeResp(200, {
        "traits": {"sdm.devices.traits.CameraLiveStream": {"supportedProtocols": ["WEB_RTC", "RTSP"]}}
    }))
    src = _run()
    p = _params(src)
    assert "protocols" not in p          # WebRTC = omit the param
    assert p["device_id"] == "AVPHwXYZ"  # cloud_id suffix strip
    assert p["client_id"] == "cid"
    assert p["client_secret"] == "csec"
    assert p["refresh_token"] == "rt"
    assert p["project_id"] == "6810ba1e"


def test_rtsp_only_device_sets_protocols_rtsp(seed, monkeypatch):
    seed(_WORKING_SECRETS)
    _install_httpx(monkeypatch, _FakeResp(200, {
        "traits": {"sdm.devices.traits.CameraLiveStream": {"supportedProtocols": ["RTSP"]}}
    }))
    src = _run()
    assert _params(src)["protocols"] == "RTSP"


def test_missing_camera_support_returns_none_without_http(seed, monkeypatch):
    seed({k: v for k, v in _WORKING_SECRETS.items() if k != "NEST_CAMERA_SUPPORT"})
    _install_httpx(monkeypatch, _FakeResp(200, {}))
    assert _run() is None
    assert _FakeAsyncClient.calls == []   # gated before any SDM call


def test_missing_creds_returns_none(seed, monkeypatch):
    seed({k: v for k, v in _WORKING_SECRETS.items() if k != "NEST_WEB_CLIENT_SECRET"})
    _install_httpx(monkeypatch, _FakeResp(200, {}))
    assert _run() is None


def test_sdm_lookup_failure_falls_back_to_webrtc(seed, monkeypatch):
    seed(_WORKING_SECRETS)
    _install_httpx(monkeypatch, _FakeResp(401, {}))
    src = _run()
    assert src is not None and "protocols" not in _params(src)   # NOT RTSP fallback
