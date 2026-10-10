"""Keep image bytes out of logs and tool results.

Image parameters arrive in a ``tool_call`` as ``[{"mime": "image/jpeg",
"data": "<base64>"}, ...]`` (up to 4 x 2 MiB). The SDK turns those into
``JarvisImage`` objects before ``run()``. Neither form may reach a log line,
and a command that echoes its images into ``context_data`` must not ship
megabytes of base64 back to the server (and from there into the model's
context).

- :func:`redact_for_log` — for log/print arguments. Wire images and
  JarvisImage become short summaries; bytes and any other long string are
  truncated.
- :func:`sanitize_tool_output` — for results posted back to the server.
  Images become metadata-only summaries and bytes a size marker; ordinary
  strings are left alone (results legitimately carry long text).
"""

from __future__ import annotations

from typing import Any

try:
    from jarvis_command_sdk import JarvisImage
except ImportError:  # jarvis-command-sdk < 0.10.0 has no image support
    JarvisImage = None  # type: ignore[assignment,misc]

LOG_MAX_STR: int = 256
_MAX_DEPTH: int = 12


def _is_wire_image(obj: Any) -> bool:
    if not isinstance(obj, dict) or not isinstance(obj.get("data"), str):
        return False
    mime = obj.get("mime")
    return isinstance(mime, str) and mime.startswith("image/")


def _is_jarvis_image(obj: Any) -> bool:
    return JarvisImage is not None and isinstance(obj, JarvisImage)


def _wire_summary(obj: dict) -> dict[str, Any]:
    out = {k: v for k, v in obj.items() if k != "data"}
    out["data"] = f"<redacted {len(obj['data'])} base64 chars>"
    return out


def _image_summary(img: Any) -> dict[str, Any]:
    return {
        "image": {
            "mime": img.mime,
            "size_bytes": img.size_bytes,
            "width": img.width,
            "height": img.height,
        }
    }


def redact_for_log(obj: Any, max_str: int = LOG_MAX_STR, _depth: int = 0) -> Any:
    """Return a copy of ``obj`` that is safe to log (never mutates ``obj``)."""
    if _depth > _MAX_DEPTH:
        return "<…>"
    if _is_jarvis_image(obj):
        return repr(obj)  # JarvisImage.__repr__ never includes the bytes
    if isinstance(obj, (bytes, bytearray, memoryview)):
        return f"<{len(obj)} bytes>"
    if isinstance(obj, str):
        if len(obj) > max_str:
            return f"{obj[:64]}…<truncated, {len(obj)} chars>"
        return obj
    if _is_wire_image(obj):
        return _wire_summary(obj)
    if isinstance(obj, dict):
        return {k: redact_for_log(v, max_str, _depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact_for_log(v, max_str, _depth + 1) for v in obj]
    return obj


def sanitize_tool_output(obj: Any, _depth: int = 0) -> Any:
    """Strip image bytes from a tool result before it is sent to the server."""
    if _depth > _MAX_DEPTH:
        return obj
    if _is_jarvis_image(obj):
        return _image_summary(obj)
    if isinstance(obj, (bytes, bytearray, memoryview)):
        return f"<{len(obj)} bytes>"
    if _is_wire_image(obj):
        return _wire_summary(obj)
    if isinstance(obj, dict):
        return {k: sanitize_tool_output(v, _depth + 1) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize_tool_output(v, _depth + 1) for v in obj]
    if isinstance(obj, tuple):
        return tuple(sanitize_tool_output(v, _depth + 1) for v in obj)
    return obj
