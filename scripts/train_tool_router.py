#!/usr/bin/env python3
import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from clients.jarvis_command_center_client import JarvisCommandCenterClient
from clients.rest_client import RestClient
from utils.command_discovery_service import get_command_discovery_service
from utils.config_service import Config


def _repo_root() -> Path:
    return REPO_ROOT


def _load_extra_jsonl(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    if path == "-":
        return os.sys.stdin.read()
    file_path = Path(path)
    if not file_path.exists():
        print(f"[ToolRouterTrain] Extra JSONL not found: {file_path}")
        return None
    content = file_path.read_text(encoding="utf-8")
    return content.strip() if content.strip() else None


def _dedupe_entries(entries: List[Dict[str, str]]) -> List[Dict[str, str]]:
    deduped: List[Dict[str, str]] = []
    seen: set[Tuple[str, str]] = set()
    for entry in entries:
        utterance = (entry.get("utterance") or "").strip()
        tool_name = (entry.get("tool_name") or "").strip()
        if not utterance or not tool_name:
            continue
        key = (utterance.lower(), tool_name)
        if key in seen:
            continue
        seen.add(key)
        deduped.append({"utterance": utterance, "tool_name": tool_name})
    return deduped


def _load_test_utterances(date_context) -> List[Dict[str, str]]:
    from test_command_parsing import create_test_commands_with_context

    test_commands = create_test_commands_with_context(date_context)
    entries = [
        {"utterance": test.voice_command, "tool_name": test.expected_command}
        for test in test_commands
    ]
    return _dedupe_entries(entries)


def _build_available_commands(date_context) -> List[Dict[str, Any]]:
    command_service = get_command_discovery_service()
    command_service.refresh_now()
    commands = command_service.get_all_commands()
    if not commands:
        raise RuntimeError("No commands discovered. Ensure commands load successfully.")
    available_commands = [
        cmd.get_command_schema(date_context) for cmd in commands.values()
    ]
    return sorted(available_commands, key=lambda c: c.get("command_name", ""))


def main() -> None:
    default_jsonl = _repo_root() / "training" / "tool_router_extra_utterances.jsonl"
    parser = argparse.ArgumentParser(description="Train tool router via JCC endpoint.")
    parser.add_argument(
        "--extra-jsonl",
        default=str(default_jsonl),
        help="Path to extra training JSONL file (or '-' for stdin).",
    )
    parser.add_argument(
        "--no-extra-jsonl",
        action="store_true",
        help="Do not send extra_training_jsonl.",
    )
    parser.add_argument(
        "--no-test-utterances",
        action="store_true",
        help="Do not include utterances from test_command_parsing.py.",
    )
    parser.add_argument(
        "--output-model-path",
        help="Optional output model path on the server (e.g., /tmp/tool_classifier.bin).",
    )
    parser.add_argument(
        "--no-save-training-jsonl",
        action="store_true",
        help="Disable server-side saving of the combined training JSONL.",
    )
    parser.add_argument("--epoch", type=int, help="FastText epoch count.")
    parser.add_argument("--lr", type=float, help="FastText learning rate.")
    parser.add_argument("--word-ngrams", type=int, help="FastText word ngrams.")
    parser.add_argument("--timeout", type=int, default=120, help="Request timeout.")
    parser.add_argument("--dry-run", action="store_true", help="Print payload and exit.")
    args = parser.parse_args()

    base_url = Config.get_str("jarvis_command_center_api_url")
    if not base_url:
        raise SystemExit("Missing jarvis_command_center_api_url in config.")

    jcc_client = JarvisCommandCenterClient(base_url)
    date_context = jcc_client.get_date_context()
    if not date_context:
        raise SystemExit("Failed to fetch date context from JCC.")

    available_commands = _build_available_commands(date_context)

    extra_training: List[Dict[str, str]] = []
    if not args.no_test_utterances:
        extra_training = _load_test_utterances(date_context)

    extra_training_jsonl = None
    if not args.no_extra_jsonl:
        extra_training_jsonl = _load_extra_jsonl(args.extra_jsonl)

    payload: Dict[str, Any] = {
        "available_commands": available_commands,
    }
    if extra_training_jsonl:
        payload["extra_training_jsonl"] = extra_training_jsonl
    if extra_training:
        payload["extra_training"] = extra_training
    if args.output_model_path:
        payload["output_model_path"] = args.output_model_path
    if args.no_save_training_jsonl:
        payload["save_training_jsonl"] = False
    if args.epoch is not None:
        payload["epoch"] = args.epoch
    if args.lr is not None:
        payload["lr"] = args.lr
    if args.word_ngrams is not None:
        payload["word_ngrams"] = args.word_ngrams

    if args.dry_run:
        print(json.dumps(
            {
                "available_commands": len(available_commands),
                "extra_training": len(extra_training),
                "extra_training_jsonl_bytes": len(extra_training_jsonl or ""),
                "output_model_path": args.output_model_path,
            },
            indent=2,
        ))
        return

    response = RestClient.post(
        f"{base_url}/api/v0/tool-router/train",
        timeout=args.timeout,
        data=payload,
    )
    if not response:
        raise SystemExit("Training request failed or returned no response.")

    print(json.dumps(response, indent=2))


if __name__ == "__main__":
    main()
