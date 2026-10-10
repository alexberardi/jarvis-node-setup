"""Image parameters on the node: pass-through to commands, schema marker, redaction.

Wire format (server → node tool_call): an image parameter's argument is a list
of {"mime": "image/jpeg"|"image/png"|"image/webp", "data": "<base64>"}. The SDK's
execute() turns it into list[JarvisImage]; the node must never log the bytes or
echo them back in a tool result.
"""

from __future__ import annotations

import base64
import json
import struct
import sys
import zlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from jarvis_command_sdk import (
    CommandExample,
    CommandResponse,
    IJarvisCommand,
    JarvisImage,
    JarvisParameter,
)

from utils.image_redaction import redact_for_log, sanitize_tool_output
from utils.tool_result_formatter import format_tool_result
from utils.tool_schema_builder import _build_schema_from_sdk_command

_SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)


def _png(width: int = 4, height: int = 3, pad: int = 0) -> bytes:
    def chunk(tag: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"") + b"\x00" * pad
    )


def _wire(data: bytes, mime: str = "image/png") -> dict[str, str]:
    return {"mime": mime, "data": base64.b64encode(data).decode()}


# A ~200 KB image so a leak would be unmistakable in any captured output.
BIG_PNG: bytes = _png(pad=200_000)
BIG_B64: str = base64.b64encode(BIG_PNG).decode()


class PlantCommand(IJarvisCommand):
    """A third-party-style command taking photos."""

    def __init__(self, echo_images: bool = False) -> None:
        self.received: dict[str, Any] | None = None
        self._echo = echo_images

    @property
    def command_name(self) -> str:
        return "identify_plant"

    @property
    def description(self) -> str:
        return "Identify a plant from a photo"

    @property
    def parameters(self) -> list[JarvisParameter]:
        return [JarvisParameter("photos", "image", required=True, description="Photos of the plant.")]

    @property
    def required_secrets(self) -> list:
        return []

    @property
    def keywords(self) -> list[str]:
        return ["plant"]

    def generate_prompt_examples(self) -> list[CommandExample]:
        return [CommandExample("what plant is this", {"photos": [1]}, is_primary=True)]

    def generate_adapter_examples(self) -> list[CommandExample]:
        return self.generate_prompt_examples()

    def run(self, request_info: Any, **kwargs: Any) -> CommandResponse:
        self.received = kwargs
        photos = kwargs["photos"]
        ctx: dict[str, Any] = {"message": "It's a fern", "count": len(photos)}
        if self._echo:
            # A careless command echoing everything back.
            ctx.update({"photos": photos, "raw": photos[0].data, "wire": photos[0].to_wire()})
        return CommandResponse.success_response(context_data=ctx)


# ── Unit: redaction helpers ───────────────────────────────────────────


class TestRedactForLog:
    def test_wire_images_summarized(self):
        out = redact_for_log({"arguments": {"photos": [_wire(BIG_PNG)]}})
        item = out["arguments"]["photos"][0]
        assert item["mime"] == "image/png"
        assert item["data"] == f"<redacted {len(BIG_B64)} base64 chars>"

    def test_jarvis_image_uses_safe_repr(self):
        assert redact_for_log(JarvisImage(_png(4, 3))).startswith("JarvisImage(mime='image/png'")

    def test_bytes_and_long_strings(self):
        assert redact_for_log(b"abc") == "<3 bytes>"
        out = redact_for_log("x" * 1000)
        assert out.endswith("<truncated, 1000 chars>") and len(out) < 120
        assert redact_for_log("short") == "short"

    def test_does_not_mutate_and_handles_tuples_and_scalars(self):
        payload = {"a": (1, "y" * 500), "b": None, "c": 3.5}
        out = redact_for_log(payload)
        assert payload["a"][1] == "y" * 500
        assert out["a"][0] == 1 and out["b"] is None and out["c"] == 3.5

    def test_depth_limit(self):
        deep: Any = "leaf"
        for _ in range(30):
            deep = [deep]
        assert "<…>" in json.dumps(redact_for_log(deep), ensure_ascii=False)

    def test_non_image_data_dict_untouched(self):
        assert redact_for_log({"mime": "text/plain", "data": "hi"}) == {"mime": "text/plain", "data": "hi"}


class TestSanitizeToolOutput:
    def test_images_become_metadata(self):
        img = JarvisImage(_png(4, 3))
        out = sanitize_tool_output({"photos": [img], "raw": b"1234", "wire": _wire(BIG_PNG), "t": (img,)})
        assert out["photos"] == [{"image": {"mime": "image/png", "size_bytes": img.size_bytes, "width": 4, "height": 3}}]
        assert out["raw"] == "<4 bytes>"
        assert out["wire"]["data"].startswith("<redacted")
        assert out["t"][0]["image"]["width"] == 4
        json.dumps(out)  # serializable

    def test_long_text_kept(self):
        text = "recipe " * 1000
        assert sanitize_tool_output({"text": text}) == {"text": text}

    def test_depth_limit_returns_object(self):
        deep: Any = [b"x"]
        for _ in range(30):
            deep = [deep]
        assert sanitize_tool_output(deep) is not None


# ── tool_call over MQTT ───────────────────────────────────────────────


def _invoke_tool_call(cmd: IJarvisCommand, arguments: Any) -> tuple[dict, MagicMock]:
    import mqtt_tts_listener as mtl

    posted: dict = {}
    discovery = MagicMock()
    discovery.get_all_commands.return_value = {cmd.command_name: cmd}
    fake_logger = MagicMock()

    def fake_post(request_id: str, result: dict) -> None:
        posted["request_id"] = request_id
        posted["result"] = result

    with patch.object(mtl, "_post_tool_call_result", side_effect=fake_post), \
         patch.object(mtl, "logger", fake_logger), \
         patch.object(mtl.Config, "get_dict", return_value={}), \
         patch("utils.command_execution_service._build_secrets", return_value={}), \
         patch("utils.command_discovery_service.get_command_discovery_service", return_value=discovery):
        mtl.handle_tool_call({
            "command_name": cmd.command_name,
            "arguments": arguments,
            "tool_call_id": "tc-1",
            "reply_request_id": "req-1",
            "user_id": 1,
        })
    return posted, fake_logger


def _assert_no_image_bytes(*texts: str) -> None:
    for t in texts:
        assert BIG_B64[:200] not in t
        assert BIG_B64[-200:] not in t


class TestToolCallImages:
    def test_command_receives_list_of_jarvis_image(self, capsys):
        cmd = PlantCommand()
        posted, fake_logger = _invoke_tool_call(cmd, {"photos": [_wire(BIG_PNG), _wire(_png(8, 6))]})

        photos = cmd.received["photos"]
        assert [type(p) for p in photos] == [JarvisImage, JarvisImage]
        assert photos[0].data == BIG_PNG
        assert (photos[1].width, photos[1].height) == (8, 6)
        assert posted["result"]["output"]["success"] is True
        assert posted["result"]["output"]["count"] == 2
        _assert_no_image_bytes(capsys.readouterr().out, repr(fake_logger.mock_calls))

    def test_arguments_as_json_string(self):
        cmd = PlantCommand()
        _invoke_tool_call(cmd, json.dumps({"photos": [_wire(_png())]}))
        assert isinstance(cmd.received["photos"][0], JarvisImage)

    def test_echoed_images_stripped_from_result(self, capsys):
        cmd = PlantCommand(echo_images=True)
        posted, fake_logger = _invoke_tool_call(cmd, {"photos": [_wire(BIG_PNG)]})
        out = posted["result"]["output"]
        assert out["photos"][0]["image"]["mime"] == "image/png"
        assert out["raw"] == f"<{len(BIG_PNG)} bytes>"
        assert out["wire"]["data"].startswith("<redacted")
        serialized = json.dumps(posted["result"])
        _assert_no_image_bytes(serialized, capsys.readouterr().out, repr(fake_logger.mock_calls))

    def test_too_many_images_is_validation_error(self):
        cmd = PlantCommand()
        posted, _ = _invoke_tool_call(cmd, {"photos": [_wire(_png())] * 5})
        out = posted["result"]["output"]
        assert out["success"] is False
        assert "at most 4" in out["error"]
        assert cmd.received is None

    def test_not_an_image_is_validation_error_without_data(self, capsys):
        cmd = PlantCommand()
        junk = base64.b64encode(b"GIF89a" + b"z" * 5000).decode()
        posted, fake_logger = _invoke_tool_call(cmd, {"photos": [{"mime": "image/png", "data": junk}]})
        out = posted["result"]["output"]
        assert out["success"] is False
        assert "not a JPEG, PNG or WebP" in out["error"]
        for t in (json.dumps(posted), capsys.readouterr().out, repr(fake_logger.mock_calls)):
            assert junk[:100] not in t

    def test_exception_text_truncated(self):
        class Boom(PlantCommand):
            def run(self, request_info: Any, **kwargs: Any) -> CommandResponse:
                raise RuntimeError("bad " + BIG_B64)

        posted, fake_logger = _invoke_tool_call(Boom(), {"photos": [_wire(_png())]})
        assert posted["result"]["output"]["success"] is False
        assert len(posted["result"]["output"]["error"]) < 200
        _assert_no_image_bytes(json.dumps(posted), repr(fake_logger.mock_calls))


class TestOnMessageRedaction:
    def test_debug_log_of_payload_is_redacted(self, capsys):
        import mqtt_tts_listener as mtl

        payload = [{
            "command": "tool_call",
            "details": {"command_name": "identify_plant", "arguments": {"photos": [_wire(BIG_PNG)]},
                        "reply_request_id": "r1"},
        }]
        msg = SimpleNamespace(topic="jarvis/nodes/n1/commands", payload=json.dumps(payload).encode())
        fake_logger = MagicMock()
        executor = MagicMock()
        with patch.object(mtl, "logger", fake_logger), patch.object(mtl, "_task_executor", executor):
            mtl.on_message(MagicMock(), None, msg)

        # The handler still gets the full, unredacted details.
        submitted = executor.submit.call_args.args
        assert submitted[1] == "tool_call"
        assert submitted[3]["arguments"]["photos"][0]["data"] == BIG_B64
        debug_calls = [c for c in fake_logger.debug.call_args_list if c.args and c.args[0] == "MQTT message received"]
        assert debug_calls, "expected the payload debug log"
        _assert_no_image_bytes(repr(fake_logger.mock_calls), capsys.readouterr().out)


# ── Tool reports carry the schema marker ──────────────────────────────


class TestToolReports:
    def test_report_tools_posts_marker(self):
        import mqtt_tts_listener as mtl

        discovery = MagicMock()
        discovery.get_all_commands.return_value = {"identify_plant": PlantCommand()}
        discovery.get_failed_modules.return_value = {}
        cc_client = MagicMock()
        cc_client.get_date_context.return_value = None
        posted: dict = {}

        def fake_post(url: str, data: Any = None, timeout: Any = None) -> dict:
            posted["data"] = data
            return {"ok": True}

        with patch("utils.service_discovery.get_command_center_url", return_value="http://cc:7703"), \
             patch("clients.rest_client.RestClient.post", side_effect=fake_post), \
             patch("utils.command_discovery_service.get_command_discovery_service", return_value=discovery), \
             patch("utils.agent_discovery_service.get_agent_discovery_service", return_value=MagicMock()), \
             patch("clients.jarvis_command_center_client.JarvisCommandCenterClient", return_value=cc_client), \
             patch("services.command_store_service.list_installed", return_value=[]):
            mtl.handle_report_tools({"reply_request_id": "req-rt-1"})

        # Round-trip through JSON, as RestClient.post does.
        data = json.loads(json.dumps(posted["data"]))
        prop = data["client_tools"][0]["function"]["parameters"]["properties"]["photos"]
        assert prop["x-jarvis-type"] == "image"
        assert prop["type"] == "array" and prop["items"]["type"] == "integer"
        assert data["available_commands"][0]["parameters"][0]["type"] == "image"

    def test_fallback_schema_builder_marks_images(self):
        cmd = SimpleNamespace(
            command_name="identify_plant",
            description="Identify a plant",
            parameters=[JarvisParameter("photos", "array<image>", required=True, description="Plant photos.")],
            keywords=[],
            allow_direct_answer=False,
            generate_prompt_examples=lambda: [],
        )
        tool, _ = _build_schema_from_sdk_command(cmd)
        prop = tool["function"]["parameters"]["properties"]["photos"]
        assert prop["x-jarvis-type"] == "image"
        assert prop["description"].startswith("Plant photos.")
        assert tool["function"]["parameters"]["required"] == ["photos"]


def test_voice_path_formatter_strips_images():
    img = JarvisImage(BIG_PNG)
    resp = CommandResponse.success_response(context_data={"message": "ok", "photo": img, "raw": BIG_PNG})
    out = format_tool_result("tc-1", resp)
    assert out["output"]["context"]["photo"]["image"]["size_bytes"] == len(BIG_PNG)
    _assert_no_image_bytes(json.dumps(out))
