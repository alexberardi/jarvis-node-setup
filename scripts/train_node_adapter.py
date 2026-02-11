#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from clients.jarvis_command_center_client import JarvisCommandCenterClient
from clients.rest_client import RestClient
from utils.command_discovery_service import get_command_discovery_service
from utils.config_service import Config
from utils.service_discovery import get_command_center_url, init as init_service_discovery


def _build_available_commands(date_context) -> list[Dict[str, Any]]:
    command_service = get_command_discovery_service()
    command_service.refresh_now()
    commands = command_service.get_all_commands()
    if not commands:
        raise RuntimeError("No commands discovered. Ensure commands load successfully.")
    available_commands = [
        cmd.get_command_schema(date_context, use_adapter_examples=True) for cmd in commands.values()
    ]
    return sorted(available_commands, key=lambda c: c.get("command_name", ""))


def main() -> None:
    parser = argparse.ArgumentParser(description="Request node adapter training via JCC.")
    parser.add_argument(
        "--base-model-id",
        default=Config.get_str("jcc_adapter_base_model_id"),
        help="llm-proxy base model id (required).",
    )
    parser.add_argument("--dataset-hash", help="Optional dataset hash.")
    parser.add_argument("--rank", type=int, help="Training rank (LoRA).")
    parser.add_argument("--epochs", type=int, help="Training epochs.")
    parser.add_argument("--batch-size", type=int, help="Training batch size.")
    parser.add_argument("--max-seq-len", type=int, help="Training max sequence length.")
    parser.add_argument("--hf-base-model-id", help="Hugging Face base model id.")
    parser.add_argument("--priority", default="normal", help="Job priority.")
    parser.add_argument("--timeout", type=int, default=120, help="Request timeout.")
    parser.add_argument("--dry-run", action="store_true", help="Print payload and exit.")
    args = parser.parse_args()

    init_service_discovery()
    base_url = get_command_center_url()
    if not base_url:
        raise SystemExit("Could not resolve command center URL from config service, JSON config, or defaults.")
    if not args.base_model_id:
        raise SystemExit("Missing base_model_id. Provide --base-model-id or set jcc_adapter_base_model_id.")
    if args.base_model_id.endswith(".gguf") and not args.hf_base_model_id:
        raise SystemExit("hf_base_model_id is required when base_model_id ends with .gguf.")

    jcc_client = JarvisCommandCenterClient(base_url)
    date_context = jcc_client.get_date_context()
    if not date_context:
        raise SystemExit("Failed to fetch date context from JCC.")

    available_commands = _build_available_commands(date_context)

    payload: Dict[str, Any] = {
        "base_model_id": args.base_model_id,
        "available_commands": available_commands,
        "priority": args.priority,
    }
    if args.dataset_hash:
        payload["dataset_hash"] = args.dataset_hash
    params: Dict[str, Any] = {}
    if args.rank is not None:
        params["rank"] = args.rank
    if args.epochs is not None:
        params["epochs"] = args.epochs
    if args.batch_size is not None:
        params["batch_size"] = args.batch_size
    if args.max_seq_len is not None:
        params["max_seq_len"] = args.max_seq_len
    if args.hf_base_model_id is not None:
        params["hf_base_model_id"] = args.hf_base_model_id
    if params:
        payload["params"] = params

    if args.dry_run:
        print(json.dumps(
            {
                "base_model_id": args.base_model_id,
                "available_commands": len(available_commands),
                "dataset_hash": args.dataset_hash,
                "params": params,
                "priority": args.priority,
            },
            indent=2,
        ))
        return

    response = RestClient.post(
        f"{base_url}/api/v0/adapters/train",
        timeout=args.timeout,
        data=payload,
    )
    if not response:
        raise SystemExit("Training request failed or returned no response.")

    print(json.dumps(response, indent=2))


if __name__ == "__main__":
    main()
