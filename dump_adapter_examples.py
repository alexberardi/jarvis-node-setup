#!/usr/bin/env python3
"""Dump baked `generate_adapter_examples()` from installed commands as
dataset_ref-shaped JSON ready for llm-proxy training.

Usage:
    CONFIG_PATH=config-eval.json .venv/bin/python dump_adapter_examples.py \\
        -c music set_timer get_weather calculate \\
        -o /tmp/real_schema_dataset.json
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path


# Pantry-style extra command paths we also scan. The Pantry installer normally
# copies/symlinks these into commands/custom_commands/, but when they're only
# in sibling repos we can still import them directly by file path.
_PANTRY_COMMAND_ROOTS = [
    Path("/Users/alexanderberardi/jarvis/jarvis-cmd-music-assistant/commands/music/command.py"),
    Path("/Users/alexanderberardi/jarvis/jarvis-cmd-meteo-weather/commands/get_weather/command.py"),
]


def _import_from_path(path: Path):
    """Import a module by filesystem path, returning the module object."""
    pkg_root = path.parent.parent  # e.g. jarvis-cmd-music-assistant/commands
    sys.path.insert(0, str(pkg_root.parent))  # make the repo importable
    spec = importlib.util.spec_from_file_location(path.parent.name + "_command", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-c", "--commands", nargs="+", required=True,
                    help="command_name values to dump; accepts name=source_name to remap "
                         "(e.g. get_weather=get_weather_meteo)")
    ap.add_argument("-o", "--out", type=Path, required=True)
    args = ap.parse_args()
    remaps: dict[str, str] = {}
    for spec in args.commands:
        if "=" in spec:
            out_name, src_name = spec.split("=", 1)
            remaps[out_name] = src_name
        else:
            remaps[spec] = spec

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from utils.command_discovery_service import get_command_discovery_service
    from jarvis_command_sdk import IJarvisCommand as SDKIJarvisCommand

    svc = get_command_discovery_service()
    svc.refresh_now()
    registry: dict = dict(svc.get_all_commands())

    # Supplement with Pantry sibling repos
    for cmd_path in _PANTRY_COMMAND_ROOTS:
        if not cmd_path.is_file():
            continue
        try:
            module = _import_from_path(cmd_path)
            for attr in dir(module):
                cls = getattr(module, attr)
                if not isinstance(cls, type):
                    continue
                if cls is SDKIJarvisCommand:
                    continue
                try:
                    if issubclass(cls, SDKIJarvisCommand):
                        inst = cls()
                        registry[inst.command_name] = inst
                except TypeError:
                    continue
        except Exception as e:
            print(f"  (failed loading {cmd_path}: {e})", file=sys.stderr)

    print(f"Total commands: {len(registry)}: {sorted(registry.keys())}")

    out_commands: list[dict] = []
    total = 0
    missing: list[str] = []
    for out_name, src_name in remaps.items():
        cmd = registry.get(src_name)
        if cmd is None:
            missing.append(src_name)
            continue
        examples = cmd.generate_adapter_examples() or []
        rows = [
            {
                "voice_command": ex.voice_command,
                "expected_tool_call": {
                    "name": out_name,  # use user-facing name (after remap)
                    "arguments": ex.expected_parameters,
                },
            }
            for ex in examples
        ]
        out_commands.append({"command_name": out_name, "examples": rows})
        total += len(rows)
        remap_note = f" (remapped from {src_name})" if out_name != src_name else ""
        print(f"  {out_name}: {len(rows)} baked examples{remap_note}")

    if missing:
        print(f"⚠️ missing commands: {missing}", file=sys.stderr)
        return 2

    dataset = {
        "format": "inline-json",
        "data": {"commands": out_commands},
    }
    args.out.write_text(json.dumps(dataset, indent=2))
    print(f"✓ wrote {total} total examples across {len(out_commands)} commands → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
