#!/usr/bin/env python3
"""Measure the level the node's microphone sees from the speaker, and the room noise floor.

    python -m tools.acoustic_test.mic_level 0.15 0.35

Plays a fixed neutral sentence (no wake word) on the speaker while the node records its microphone through
PulseAudio/PipeWire, then reports ffmpeg's mean and peak level per volume. Compare the speech peak with the noise
floor's peak: speech at or below the floor is not heard by the wake model. Same environment variables as
``tools.acoustic_test.run``, plus ``MIC_SOURCE`` (the PulseAudio source name; default ``@DEFAULT_SOURCE@``).
Needs ``parecord`` and ``ffmpeg`` in the node container.
"""

from __future__ import annotations

import os
import re
import shlex
import sys
import threading
import time

from tools.acoustic_test.run import Config, run_command, speak

PHRASE = "The quick brown fox jumps over the lazy dog. Pack my box with five dozen liquor jugs."


def parse_levels(text: str) -> tuple[str, str]:
    """(mean_dB, peak_dB) from ffmpeg ``volumedetect`` output; ``?`` when absent."""
    mean = re.search(r"mean_volume: (-?[\d.]+) dB", text)
    peak = re.search(r"max_volume: (-?[\d.]+) dB", text)
    return (mean.group(1) if mean else "?", peak.group(1) if peak else "?")


def record(cfg: Config, seconds: int, path: str) -> str:
    source = os.environ.get("MIC_SOURCE", "@DEFAULT_SOURCE@")
    inner = (f"timeout {seconds} parecord --device={shlex.quote(source)} --rate=16000 --channels=1 "
             f"--file-format=wav {path}; ffmpeg -hide_banner -nostats -i {path} -af volumedetect -f null - 2>&1 | "
             "grep -E 'mean_volume|max_volume'")
    command = f"docker exec {shlex.quote(cfg.node_container)} sh -c {shlex.quote(inner)}"
    return run_command(cfg, cfg.node_ssh, command, timeout=seconds + 30)


def main() -> None:
    cfg = Config()
    cfg.require()
    mean, peak = parse_levels(record(cfg, 6, "/tmp/noise.wav"))
    print(f"room noise floor: mean {mean} dB | peak {peak} dB")
    for volume in [float(a) for a in sys.argv[1:]] or [0.15]:
        captured: dict[str, str] = {}
        worker = threading.Thread(target=lambda: captured.update(text=record(cfg, 14, "/tmp/level.wav")))
        worker.start()
        time.sleep(2.5)
        played = speak(cfg, PHRASE, volume, None)
        worker.join()
        mean, peak = parse_levels(captured.get("text", ""))
        print(f"volume {volume}: played {played}s | mean {mean} dB (includes silence) | peak {peak} dB", flush=True)


if __name__ == "__main__":
    main()
