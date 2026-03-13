#!/usr/bin/env python3
"""Interactive text-based alert testing.

Injects test alerts, then lets you type voice commands to test the full
pipeline (pre-route → execute → LLM composition) without needing a mic.

Usage:
    python scripts/test_alerts_interactive.py
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Set config service URL before library imports
if not os.environ.get("JARVIS_CONFIG_URL"):
    try:
        _config_path = os.environ.get("CONFIG_PATH", "config-mac.json")
        with open(_config_path) as _f:
            _url = json.load(_f).get("jarvis_config_service_url")
        if _url:
            os.environ["JARVIS_CONFIG_URL"] = _url
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        pass

os.environ.setdefault("JARVIS_SKIP_PROVISIONING_CHECK", "true")

from datetime import datetime, timedelta, timezone

from jarvis_log_client import init as init_logging, JarvisLogger

init_logging(app_id="jarvis-node", app_key="")
logger = JarvisLogger(service="jarvis-node")

from core.alert import Alert
from services.alert_queue_service import get_alert_queue_service
from services.led_service import get_led_service
from utils.command_execution_service import CommandExecutionService


def inject_alerts() -> int:
    """Inject sample alerts and return count."""
    queue = get_alert_queue_service()
    now = datetime.now(timezone.utc)

    alerts = [
        Alert(
            source_agent="calendar_alerts",
            title="Upcoming: Team Standup",
            summary="Team Standup in 12 minutes",
            created_at=now,
            expires_at=now + timedelta(minutes=15),
            priority=3,
        ),
        Alert(
            source_agent="news_alerts",
            title="SpaceX launches Starship to orbit",
            summary="SpaceX successfully launched its Starship vehicle to orbit for the first time.",
            created_at=now - timedelta(minutes=5),
            expires_at=now + timedelta(hours=4),
            priority=1,
        ),
        Alert(
            source_agent="news_alerts",
            title="Fed holds interest rates steady",
            summary="The Federal Reserve kept interest rates unchanged at its latest meeting.",
            created_at=now - timedelta(minutes=8),
            expires_at=now + timedelta(hours=4),
            priority=1,
        ),
    ]

    for alert in alerts:
        queue.add_alert(alert)

    return len(alerts)


def main() -> None:
    # Wire LED callback
    led = get_led_service()
    queue = get_alert_queue_service()
    queue.on_change = lambda count: print(f"  [LED] → {'ALERT BLINK' if count > 0 else 'normal'} ({count} pending)")

    # Inject alerts
    count = inject_alerts()
    print(f"\nInjected {count} test alerts. Queue has {queue.count()} pending.\n")

    # Init command service (registers tools with CC)
    print("Initializing command pipeline (registering tools with CC)...")
    command_service = CommandExecutionService()

    # Warm up — register tools
    try:
        command_service.process_voice_command("hello")
    except Exception:
        pass

    print("\n" + "=" * 60)
    print("INTERACTIVE ALERT TESTING")
    print("=" * 60)
    print("Type a voice command and press Enter.")
    print("Try: \"what's up\", \"any alerts\", \"what did I miss\"")
    print("Or any other command to test normal flow.")
    print("Type 'inject' to add more alerts, 'status' to check queue, 'quit' to exit.")
    print("=" * 60 + "\n")

    while True:
        try:
            text = input("You: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nBye!")
            break

        if not text:
            continue

        if text.lower() == "quit":
            break

        if text.lower() == "inject":
            count = inject_alerts()
            print(f"  Injected {count} alerts. Queue: {queue.count()} pending.\n")
            continue

        if text.lower() == "status":
            print(f"  Queue: {queue.count()} pending alerts")
            for a in queue.get_pending():
                print(f"    [{a.priority}] {a.title} (expires {a.expires_at.strftime('%H:%M')})")
            print()
            continue

        # Process through full pipeline
        try:
            result = command_service.process_voice_command(text)
            if result:
                msg = result.get("spoken_response") or result.get("message", "")
                if not msg and result.get("api_results"):
                    for r in result["api_results"]:
                        ctx = r.get("context_data", {})
                        msg = ctx.get("message", "")
                        if msg:
                            break
                print(f"\nJarvis: {msg}\n")
            else:
                print("\nJarvis: (no response)\n")
        except Exception as e:
            print(f"\nError: {e}\n")


if __name__ == "__main__":
    main()
