"""Pure parsing of a node's (and optionally command-center's) log for one acoustic test turn.

Kept free of I/O so it can be unit-tested against sample log text.
"""

from __future__ import annotations

import re
import time
from typing import Any

_TS_RE = re.compile(r"(\d+-\d+-\d+ \d+:\d+:\d+),(\d+)")


def line_timestamp(line: str) -> float | None:
    """Epoch seconds of a ``2026-10-12 19:17:52,032 [INFO] ...`` log line, or None."""
    m = _TS_RE.match(line)
    if not m:
        return None
    return time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")) + int(m.group(2)) / 1000


def parse_node_log(log: str) -> dict[str, Any]:
    """What the node did with one spoken request.

    Returns ``woke`` (bool) plus, when present: ``score`` (wake score), ``wakes`` (count),
    ``rec`` (recording seconds), ``hit_max``, ``heard`` (transcript), ``path`` (``stream``,
    ``local`` or ``not_for_me``), ``said`` (reply text), ``exec`` and ``errors`` (lines),
    ``speech_s``, ``rec_to_audio`` and ``send_to_audio`` (seconds).
    """
    result: dict[str, Any] = {"woke": False}
    stamps: dict[str, float | None] = {}
    for line in log.splitlines():
        if "Wake fired" in line:
            result["woke"] = True
            m = re.search(r"'score': ([\d.]+)", line)
            result["score"] = float(m.group(1)) if m else None
            result["wakes"] = result.get("wakes", 0) + 1
            stamps["wake"] = line_timestamp(line)
        elif "Recording complete" in line:
            m = re.search(r"'duration': '([\d.]+)s', 'hit_max': (\w+)", line)
            if m:
                result["rec"] = float(m.group(1))
                result["hit_max"] = m.group(2) == "True"
            stamps["rec"] = line_timestamp(line)
        elif "Transcription received" in line:
            m = re.search(r"'text': (.*)}$", line)
            result["heard"] = m.group(1).strip()[1:-1] if m else line
            stamps["stt"] = line_timestamp(line)
        elif "Sending unified voice command" in line:
            stamps["send"] = line_timestamp(line)
        elif "PCM stream starting" in line and "audio" not in stamps:
            stamps["audio"] = line_timestamp(line)
            result["path"] = "stream"
        elif "] Speaking " in line:
            m = re.search(r"Speaking (.*) via", line)
            result["said"] = m.group(1)[1:-1] if m else line
            stamps.setdefault("audio", line_timestamp(line))
            result.setdefault("path", "local")
        elif "not-for-me" in line and "silent abort" in line:
            # A not-for-me abort after a spoken reply is the follow-up window closing, not a dropped request.
            if "said" not in result and "audio" not in stamps:
                result["path"] = "not_for_me"
        elif "[ERROR]" in line or "Traceback" in line:
            result.setdefault("errors", []).append(line[24:220])
        elif "Executing" in line or "Running command" in line:
            result.setdefault("exec", []).append(line[24:200])
        elif "PCM stream complete" in line:
            m = re.search(r"'duration_s': ([\d.]+)", line)
            if m:
                result["speech_s"] = float(m.group(1))
    if stamps.get("rec") and stamps.get("audio"):
        result["rec_to_audio"] = round(stamps["audio"] - stamps["rec"], 2)
    if stamps.get("send") and stamps.get("audio"):
        result["send_to_audio"] = round(stamps["audio"] - stamps["send"], 2)
    return result


def parse_server_log(text: str) -> dict[str, Any]:
    """Optional command-center side: router choice, wake-clip verdict and tool calls."""
    result: dict[str, Any] = {}
    m = re.search(r"Router predicted tool=(\w+) score=([\d.]+)", text)
    if m:
        result["route"], result["rscore"] = m.group(1), float(m.group(2))
    m = re.search(r"wake-verify \| verdict=(\w+).*?transcript='(.*?)' phrase", text)
    if m:
        result["verify"], result["clip"] = m.group(1), m.group(2)
    tools = re.findall(r"(?:CLIENT|SERVER) TOOL\] (?:Forwarding to client|Executing)?:? ?(\w+)", text)
    if tools:
        result["tools"] = tools
    m = re.search(r"llm_stream_first_token=(\d+)ms", text)
    if m:
        result["first_token_ms"] = int(m.group(1))
    m = re.search(r"Returning (\d+) (\w+) \(stop_reason=(\w+)\)", text)
    if m:
        result["stop"] = m.group(3)
    return result


def classify(result: dict[str, Any]) -> str:
    """One verdict per turn: ``error``, ``miss`` (no wake), ``dropped`` or ``answered``."""
    if result.get("error"):
        return "error"
    if not result.get("woke"):
        return "miss"
    if result.get("path") == "not_for_me":
        return "dropped"
    return "answered"
