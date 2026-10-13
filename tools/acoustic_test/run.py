#!/usr/bin/env python3
"""Acoustic end-to-end test of the voice path, driven through a smart speaker.

Home Assistant speaks ``Hey Jarvis, <phrase>`` on a speaker placed near the node (text to speech, played to the
speaker), then this tool reads the node's log to see what it did: wake score, transcript, route, reply and the time
from the end of recording to the first audio. No one needs to be present, so settings can be compared on the same
phrases (wake threshold, Whisper prompt, microphone gain, distance).

    python -m tools.acoustic_test.run --out runs.jsonl < phrases.json

``phrases.json`` is a JSON list. Each item is a string, or an object ``{"utt": "...", "wake": "", "vol": 0.3,
"voice": "..."}``; ``"wake": ""`` marks non-wake chatter that should NOT be answered.

Configuration (environment variables; none is required to be on the same machine as the node):

    HA_URL            Home Assistant, e.g. http://homeassistant.local:8123
    HA_TOKEN          long-lived access token (or HA_TOKEN_FILE=path to a file holding it)
    PLAYER_ENTITY     media_player entity of the speaker, e.g. media_player.living_room_speaker
    NODE_SSH          ssh target that can run ``docker logs`` for the node, e.g. pi@jarvis-node.local
                      (empty = run docker locally)
    NODE_CONTAINER    node container name (default jarvis-node-audio)
    SERVER_SSH        optional ssh target for command-center, to also record route and wake-verification
    SERVER_CONTAINER  command-center container name (default jarvis-command-center)
    SSH_KEY           optional identity file for the ssh targets
    HA_MEDIA_BASE_URL optional address the SPEAKER can fetch Home Assistant media from (default: the Home Assistant
                      Cloud / external URL; Cast devices cannot fetch from a LAN address over HTTP)
    TTS_ENGINE        Home Assistant TTS entity (default tts.home_assistant_cloud), TTS_LANGUAGE (default en-US)

Practical notes, from using it:

* Volume matters more than anything. Run ``mic_level`` first and compare the speech peak with the room noise floor;
  a speaker next to the microphone at high volume flatters every result, and speech at or below the noise floor is
  simply not heard by the wake model. Pick a volume that matches a person at the distance you care about.
* Results vary between runs of identical audio (Cast start-up clipping, household noise). Compare settings from
  several runs, or run a larger set, rather than single phrases.
* Stop the node before recording a clip bank: it answers the clips and records its own replies.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import subprocess
import sys
import time
import urllib.request
from typing import Any

from tools.acoustic_test.log_parser import classify, estimate_clock_offset, parse_node_log, parse_server_log

DEFAULT_VOICES = ["WilliamNeural", "NatashaNeural", "DarrenNeural", "AnnetteNeural", "NeilNeural", "KimNeural"]


class Config:
    """Settings read from the environment once."""

    def __init__(self) -> None:
        self.ha_url = os.environ.get("HA_URL", "").rstrip("/")
        token_file = os.environ.get("HA_TOKEN_FILE", "")
        token = os.environ.get("HA_TOKEN", "")
        if not token and token_file:
            with open(os.path.expanduser(token_file), encoding="utf-8") as f:
                token = f.read().strip()
        self.ha_token = token
        self.player = os.environ.get("PLAYER_ENTITY", "")
        self.node_ssh = os.environ.get("NODE_SSH", "")
        self.node_container = os.environ.get("NODE_CONTAINER", "jarvis-node-audio")
        self.server_ssh = os.environ.get("SERVER_SSH", "")
        self.server_container = os.environ.get("SERVER_CONTAINER", "jarvis-command-center")
        self.ssh_key = os.path.expanduser(os.environ.get("SSH_KEY", ""))
        self.media_base = os.environ.get("HA_MEDIA_BASE_URL", "").rstrip("/")
        self.tts_engine = os.environ.get("TTS_ENGINE", "tts.home_assistant_cloud")
        self.tts_language = os.environ.get("TTS_LANGUAGE", "en-US")

    def require(self) -> None:
        missing = [n for n, v in (("HA_URL", self.ha_url), ("HA_TOKEN or HA_TOKEN_FILE", self.ha_token),
                                  ("PLAYER_ENTITY", self.player)) if not v]
        if missing:
            sys.exit("Missing configuration: " + ", ".join(missing) + " (see the module docstring)")


def run_command(cfg: Config, target: str, command: str, timeout: float = 30.0) -> str:
    """Run a shell command on ``target`` over ssh, or locally when ``target`` is empty."""
    if target:
        args = ["ssh", "-o", "BatchMode=yes", "-o", "LogLevel=ERROR"]
        if cfg.ssh_key:
            args += ["-i", cfg.ssh_key]
        args += [target, command]
    else:
        args = ["sh", "-c", command]
    done = subprocess.run(args, capture_output=True, text=True, timeout=timeout, env={**os.environ, "LC_ALL": "C"})
    return done.stdout


_clock_offsets: dict[str, float] = {}


def clock_offset(cfg: Config, target: str) -> float:
    """Seconds to add to this machine's clock to get ``target``'s (0 when it is this machine).

    ``docker logs --since`` is evaluated on the machine that holds the container, so using this machine's clock
    loses or duplicates log lines when the two clocks differ.
    """
    if not target:
        return 0.0
    if target not in _clock_offsets:
        before = time.time()
        remote = run_command(cfg, target, "date +%s", timeout=15).strip()
        after = time.time()
        _clock_offsets[target] = estimate_clock_offset(float(remote), before, after) if remote.isdigit() else 0.0
    return _clock_offsets[target]


def node_log(cfg: Config, since: float) -> str:
    """The node's log since ``since`` (this machine's epoch, converted to the node's clock)."""
    node_since = since + clock_offset(cfg, cfg.node_ssh)
    cmd = f"docker logs --since {int(node_since)} {shlex.quote(cfg.node_container)} 2>&1"
    return run_command(cfg, cfg.node_ssh, cmd)


def server_log(cfg: Config, since: float) -> str:
    server_since = since + clock_offset(cfg, cfg.server_ssh)
    cmd = (f"docker logs --since {int(server_since)} {shlex.quote(cfg.server_container)} 2>&1 | "
           "grep -E 'Router predicted|Streaming path|CLIENT TOOL|SERVER TOOL|wake-verify|llm_stream_first_token|Returning'")
    return run_command(cfg, cfg.server_ssh, cmd)


def ha_call(cfg: Config, path: str, data: dict[str, Any] | None = None) -> Any:
    req = urllib.request.Request(
        f"{cfg.ha_url}/api/{path}",
        headers={"Authorization": f"Bearer {cfg.ha_token}", "Content-Type": "application/json"},
        data=json.dumps(data).encode() if data is not None else None,
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def media_base_url(cfg: Config) -> str:
    """Address the speaker fetches generated speech from (Home Assistant Cloud URL unless overridden)."""
    if cfg.media_base:
        return cfg.media_base
    import websockets  # imported here: only needed to look up the cloud address

    async def status() -> dict[str, Any]:
        url = cfg.ha_url.replace("http", "ws", 1) + "/api/websocket"
        async with websockets.connect(url, max_size=None) as ws:
            await ws.recv()
            await ws.send(json.dumps({"type": "auth", "access_token": cfg.ha_token}))
            await ws.recv()
            await ws.send(json.dumps({"id": 1, "type": "cloud/status"}))
            return json.loads(await ws.recv())["result"]

    return "https://" + asyncio.run(status())["remote_domain"]


def speak(cfg: Config, text: str, volume: float, voice: str | None) -> float | None:
    """Speak ``text`` on the player; seconds it took to play, or None if playback never started."""
    ha_call(cfg, "services/media_player/volume_set", {"entity_id": cfg.player, "volume_level": volume})
    payload: dict[str, Any] = {"engine_id": cfg.tts_engine, "message": text, "cache": True,
                               "language": cfg.tts_language}
    if voice:
        payload["options"] = {"voice": voice}
    path = ha_call(cfg, "tts_get_url", payload)["path"]
    ha_call(cfg, "services/media_player/play_media", {
        "entity_id": cfg.player, "media_content_id": media_base_url(cfg) + path, "media_content_type": "music"})
    started = time.time()
    played = False
    while time.time() - started < 40:
        state = ha_call(cfg, "states/" + cfg.player)["state"]
        if state == "playing":
            played = True
        elif played and state != "buffering":
            return round(time.time() - started, 1)
        elif not played and time.time() - started > 8:
            return None
        time.sleep(0.2)
    return None


def run_one(cfg: Config, utt: str, voice: str | None, volume: float, wake: str = "Hey Jarvis, ",
            settle: float = 1.5) -> dict[str, Any]:
    t0 = time.time() - 1
    try:
        played = speak(cfg, wake + utt, volume, voice)
    except Exception as exc:  # noqa: BLE001 - report and continue with the next phrase
        return {"utt": utt, "voice": voice, "error": f"speaker: {exc}"}
    result: dict[str, Any] = {"utt": utt, "voice": voice, "played": played, "volume": volume}
    deadline = time.time() + 6
    log = ""
    while time.time() < deadline:
        time.sleep(1.0)
        log = node_log(cfg, t0)
        if "Wake fired" in log:
            if "wake-cycle COMPLETE" in log.split("Wake fired")[-1]:
                break
            deadline = max(deadline, time.time() + 40)
    result.update(parse_node_log(log))
    if result["woke"] and cfg.server_ssh:
        result.update(parse_server_log(server_log(cfg, t0)))
    time.sleep(settle)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", required=True, help="JSONL file to append results to")
    parser.add_argument("--volume", type=float, default=0.3, help="default speaker volume 0..1 (default 0.3)")
    parser.add_argument("--voices", default=",".join(DEFAULT_VOICES),
                        help="comma-separated voice names rotated per phrase (empty = engine default)")
    args = parser.parse_args()
    cfg = Config()
    cfg.require()
    voices = [v for v in args.voices.split(",") if v] or [None]
    items = json.loads(sys.stdin.read())
    counts: dict[str, int] = {}
    with open(args.out, "a", encoding="utf-8") as out:
        for i, item in enumerate(items):
            spec = {"utt": item} if isinstance(item, str) else dict(item)
            wake = spec.get("wake", "Hey Jarvis, ")
            result = run_one(cfg, spec["utt"], spec.get("voice", voices[i % len(voices)]),
                             float(spec.get("vol", args.volume)), wake=wake)
            result["expect_answer"] = wake != ""
            verdict = classify(result)
            counts[verdict] = counts.get(verdict, 0) + 1
            out.write(json.dumps(result) + "\n")
            out.flush()
            print(f"{verdict:9s} {result.get('score', '-')!s:5s} {spec['utt']!r} -> {result.get('heard')!r}", flush=True)
    print("summary:", json.dumps(counts))


if __name__ == "__main__":
    main()
