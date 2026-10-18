"""NodeInboxBackend attachments against a fake jarvisd.

The fake implements the two node routes of the inbox-attachments contract
(jarvis-server docs/decisions/2026-10-17-inbox-attachments.md):
    POST /api/v0/node/inbox-attachment  raw bytes, Content-Type = mime -> 201
    POST /api/v0/node/inbox-item        JSON incl. attachment_ids
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock, patch

import pytest
import requests

import jarvis_command_sdk.inbox as sdk_inbox
from jarvis_command_sdk import JarvisInbox
from jarvis_command_sdk.inbox import Attachment

import services.inbox_backend as inbox_backend
from services.inbox_backend import NodeInboxBackend, init_inbox_backend

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 10
_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 12


class FakeJarvisd:
    """Records requests; replies from per-route queues of (status, body)."""

    def __init__(self) -> None:
        self.uploads: list[dict] = []
        self.items: list[dict] = []
        self.upload_replies: list[tuple[int, dict]] = []
        self.item_replies: list[tuple[int, dict]] = []
        self._next_id = 0
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep pytest output quiet
                pass

            def _reply(self, status: int, body: dict) -> None:
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                if self.path == "/api/v0/node/inbox-attachment":
                    fake.uploads.append({
                        "body": body,
                        "content_type": self.headers.get("Content-Type"),
                        "api_key": self.headers.get("X-API-Key"),
                    })
                    if fake.upload_replies:
                        status, reply = fake.upload_replies.pop(0)
                    else:
                        fake._next_id += 1
                        status, reply = 201, {
                            "id": f"att-{fake._next_id}",
                            "mime": self.headers.get("Content-Type"),
                            "size": len(body),
                        }
                    self._reply(status, reply)
                elif self.path == "/api/v0/node/inbox-item":
                    fake.items.append(json.loads(body))
                    if fake.item_replies:
                        self._reply(*fake.item_replies.pop(0))
                    else:
                        self._reply(201, {"id": "item-1", "sent": True})
                else:
                    self._reply(404, {"error": "not found"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self._thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )

    def __enter__(self) -> "FakeJarvisd":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture(autouse=True)
def _quiet_logger():
    with patch.object(inbox_backend, "logger", MagicMock()) as log:
        yield log


@pytest.fixture(autouse=True)
def _reset_sdk_backend():
    prior = sdk_inbox.get_inbox_backend()
    yield
    sdk_inbox._backend = prior


@pytest.fixture(autouse=True)
def _no_backoff():
    with patch.object(inbox_backend, "_UPLOAD_BACKOFF_S", 0):
        yield


@pytest.fixture
def jarvisd():
    with FakeJarvisd() as fake, patch(
        "utils.service_discovery.get_command_center_url", return_value=fake.url
    ), patch(
        "clients.rest_client.RestClient._build_auth_header",
        return_value={"X-API-Key": "node-1:key"},
    ):
        yield fake


@pytest.fixture
def png_path(tmp_path):
    p = tmp_path / "out.png"
    p.write_bytes(_PNG)
    return p


def _post(attachments, **overrides) -> str:
    kwargs = {"title": "Your image", **overrides}
    return NodeInboxBackend().post_inbox_item(
        "comfyui", attachments=attachments, **kwargs
    )


class TestUpload:
    def test_path_and_bytes_upload_in_order(self, jarvisd, png_path):
        result = _post([
            Attachment(path=png_path, mime="image/png"),
            Attachment(data=_JPEG, mime="image/jpeg", filename="b.jpg"),
        ])

        assert result == "ok"
        assert [u["body"] for u in jarvisd.uploads] == [_PNG, _JPEG]
        assert [u["content_type"] for u in jarvisd.uploads] == [
            "image/png", "image/jpeg",
        ]
        assert all(u["api_key"] == "node-1:key" for u in jarvisd.uploads)
        assert jarvisd.items[0]["attachment_ids"] == ["att-1", "att-2"]

    def test_path_is_streamed_not_read_into_memory(self, jarvisd, png_path):
        with patch.object(
            Attachment, "read_bytes", side_effect=AssertionError("read whole file")
        ):
            assert _post([Attachment(path=png_path, mime="image/png")]) == "ok"
        assert jarvisd.uploads[0]["body"] == _PNG

    def test_no_attachments_payload_has_no_ids(self, jarvisd):
        assert _post(None) == "ok"
        assert _post([]) == "ok"
        assert all("attachment_ids" not in item for item in jarvisd.items)


class TestRetries:
    def test_one_5xx_then_201_is_ok(self, jarvisd, png_path):
        jarvisd.upload_replies = [(500, {"error": "boom"})]
        assert _post([Attachment(path=png_path, mime="image/png")]) == "ok"
        assert len(jarvisd.uploads) == 2
        assert jarvisd.uploads[1]["body"] == _PNG  # file re-read from the start

    def test_three_5xx_is_http_error(self, jarvisd, png_path):
        jarvisd.upload_replies = [(502, {"error": "bad gateway"})] * 3
        assert _post([Attachment(path=png_path, mime="image/png")]) == "http_error"
        assert len(jarvisd.uploads) == 3
        assert jarvisd.items == []

    def test_503_is_final(self, jarvisd, png_path, _quiet_logger):
        jarvisd.upload_replies = [(503, {"error": "attachments disabled"})]
        assert _post([Attachment(path=png_path, mime="image/png")]) == "http_error"
        assert len(jarvisd.uploads) == 1
        _quiet_logger.error.assert_called()

    def test_connection_error_is_retried_then_http_error(self, jarvisd, png_path):
        with patch("clients.rest_client.RestClient._get_session") as get_session:
            get_session.return_value.post.side_effect = requests.ConnectionError("x")
            result = _post([Attachment(path=png_path, mime="image/png")])
        assert result == "http_error"
        assert get_session.return_value.post.call_count == 3

    def test_read_timeout_is_http_error_not_unreadable(
        self, jarvisd, png_path, _quiet_logger
    ):
        # requests errors are OSErrors too; a timeout must not blame the file.
        with patch("clients.rest_client.RestClient._get_session") as get_session:
            get_session.return_value.post.side_effect = requests.ReadTimeout("slow")
            result = _post([Attachment(path=png_path, mime="image/png")])
        assert result == "http_error"
        assert get_session.return_value.post.call_count == 1
        messages = [c.args[0] for c in _quiet_logger.error.call_args_list]
        assert "inbox attachment upload failed" in messages
        assert "inbox attachment unreadable" not in messages


class TestRejections:
    @pytest.mark.parametrize(
        "status,tag",
        [(413, "attachment_too_large"), (415, "attachment_type"), (400, "http_error")],
    )
    def test_upload_rejection(self, jarvisd, png_path, _quiet_logger, status, tag):
        jarvisd.upload_replies = [(status, {"error": "nope"})]
        result = _post([Attachment(path=png_path, mime="image/png")])

        assert result == tag
        assert len(jarvisd.uploads) == 1  # 4xx is never retried
        assert jarvisd.items == []
        _quiet_logger.error.assert_any_call(
            "inbox attachment rejected",
            command="comfyui",
            status=status,
            mime="image/png",
            size=len(_PNG),
            filename="out.png",
            reason='{"error": "nope"}',
        )

    def test_422_on_item_is_invalid(self, jarvisd, png_path):
        jarvisd.item_replies = [(422, {"error": "unknown attachment id"})]
        assert _post([Attachment(path=png_path, mime="image/png")]) == "invalid"

    def test_413_on_item_is_too_large(self, jarvisd, png_path):
        jarvisd.item_replies = [(413, {"error": "item total over cap"})]
        result = _post([Attachment(path=png_path, mime="image/png")])
        assert result == "attachment_too_large"

    def test_item_sent_false_is_http_error(self, jarvisd, png_path):
        jarvisd.item_replies = [(201, {"id": "i", "sent": False})]
        assert _post([Attachment(path=png_path, mime="image/png")]) == "http_error"

    def test_missing_file_is_invalid(self, jarvisd, tmp_path):
        att = Attachment(path=tmp_path / "gone.png", mime="image/png")
        assert _post([att]) == "invalid"
        assert jarvisd.uploads == []


class TestFacade:
    def test_jarvis_inbox_end_to_end(self, jarvisd, png_path):
        init_inbox_backend()
        result = JarvisInbox("comfyui").post(
            title="Your image",
            attachments=[Attachment(path=png_path, mime="image/png")],
        )
        assert result == "ok"
        assert jarvisd.uploads[0]["body"] == _PNG
        assert jarvisd.items[0]["attachment_ids"] == ["att-1"]
        assert jarvisd.items[0]["title"] == "Your image"
