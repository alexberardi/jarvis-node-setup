#!/usr/bin/env python3
"""E2E test for proactive alerts with live services.

Starts the agent scheduler, injects alerts, and simulates the full
"what's up" voice command flow through CC.

Requires:
  - jarvis-command-center running (port 7703)
  - jarvis-llm-proxy-api running (port 7704)
  - Valid node credentials in config-mac.json

Usage:
    python scripts/test_alerts_e2e.py
    python scripts/test_alerts_e2e.py --run-news-agent   # also run the news agent
"""

import argparse
import json
import os
import sys
import time

# Ensure project root is on path
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

from jarvis_log_client import init as init_logging

from core.alert import Alert
from services.alert_queue_service import AlertQueueService, get_alert_queue_service
from services.led_service import get_led_service

init_logging(app_id="jarvis-node", app_key="")


def check_cc_health() -> bool:
    """Check if command center is reachable."""
    try:
        from utils.service_discovery import get_command_center_url
        from clients.rest_client import RestClient

        cc_url = get_command_center_url()
        resp = RestClient.get(f"{cc_url}/health", timeout=5)
        return resp is not None
    except Exception:
        return False


def inject_test_alerts(queue: AlertQueueService) -> list[dict]:
    """Inject realistic test alerts into the queue."""
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
            summary="SpaceX successfully launched its Starship vehicle to orbit for the first time today.",
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

    return [a.to_dict() for a in alerts]


def test_whats_up_flow(queue: AlertQueueService) -> None:
    """Simulate the full 'what's up' voice command flow."""
    from commands.whats_up_command import WhatsUpCommand
    from core.request_information import RequestInformation
    import commands.whats_up_command as wuc_module

    cmd = WhatsUpCommand()

    # Patch singleton to our queue
    original = wuc_module.get_alert_queue_service
    wuc_module.get_alert_queue_service = lambda: queue

    try:
        print("\n--- Simulating: 'Hey Jarvis, what's up?' ---")

        result = cmd.pre_route("Hey Jarvis, what's up?")
        if result is None:
            print("  pre_route returned None (no alerts or no match)")
            return

        alerts_data = json.loads(result.arguments["alerts_json"])
        print(f"  pre_route matched, flushed {len(alerts_data)} alerts")

        request_info = RequestInformation(
            voice_command="what's up",
            conversation_id="e2e-test",
        )

        print("  Sending to CC for LLM composition...")
        response = cmd.run(request_info, **result.arguments)

        print(f"\n  Success: {response.success}")
        print(f"  Queue after flush: {queue.count()} alerts remaining")
        print(f"\n  === Jarvis says ===")
        print(f"  {response.context_data.get('message', '(empty)')}")
        print(f"  ==================")

    finally:
        wuc_module.get_alert_queue_service = original


def test_news_agent(queue: AlertQueueService) -> None:
    """Run the news agent and check for alerts."""
    import asyncio

    from agents.news_alert_agent import NewsAlertAgent

    agent = NewsAlertAgent()

    print("\n--- Running News Alert Agent ---")
    print("  First run (seeding)...")
    asyncio.run(agent.run())
    print(f"  Seeded {len(agent._previous_titles)} article titles")
    print(f"  Alerts after seed (should be 0): {len(agent.get_alerts())}")

    # Can't easily test "new article detection" without waiting 30 min,
    # but we can verify the agent runs without error
    print("  Second run (checking for new)...")
    asyncio.run(agent.run())
    new_alerts = agent.get_alerts()
    print(f"  New alerts: {len(new_alerts)}")

    for alert in new_alerts:
        queue.add_alert(alert)
        print(f"    → {alert.title}")

    print(f"  Queue count: {queue.count()}")


def main() -> None:
    parser = argparse.ArgumentParser(description="E2E test for proactive alerts")
    parser.add_argument("--run-news-agent", action="store_true", help="Also run the news alert agent")
    args = parser.parse_args()

    print("=" * 60)
    print("PROACTIVE ALERTS — E2E TEST")
    print("=" * 60)

    # Check CC
    print("\n[1] Checking command center...")
    if check_cc_health():
        print("  ✓ Command center is healthy")
    else:
        print("  ✗ Command center not reachable!")
        print("  Start it with: cd jarvis-command-center && bash run-docker-dev.sh")
        sys.exit(1)

    # Init services
    print("\n[2] Initializing alert queue + LED...")
    led = get_led_service()
    queue = get_alert_queue_service()
    queue.on_change = lambda count: print(f"  [LED] → {'ALERT BLINK' if count > 0 else 'normal'} ({count} pending)")

    # Inject alerts
    print("\n[3] Injecting test alerts...")
    alerts_data = inject_test_alerts(queue)
    print(f"  Injected {len(alerts_data)} alerts")
    for a in alerts_data:
        print(f"    [{a['priority']}] {a['title']}")

    # Optionally run news agent
    if args.run_news_agent:
        print("\n[4] Running news agent...")
        test_news_agent(queue)
    else:
        print("\n[4] Skipping news agent (use --run-news-agent to enable)")

    # Test the voice command flow
    print("\n[5] Testing 'what's up' flow...")
    test_whats_up_flow(queue)

    print("\n" + "=" * 60)
    print("E2E TEST COMPLETE")
    print("=" * 60)


if __name__ == "__main__":
    main()
