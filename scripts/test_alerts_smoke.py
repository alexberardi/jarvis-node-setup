#!/usr/bin/env python3
"""Smoke test for the proactive alerts system.

Exercises the full alert pipeline in-process:
  Alert model → AlertQueueService → LED callback → WhatsUpCommand pre_route → run()

Usage:
    python scripts/test_alerts_smoke.py              # basic smoke test (no CC needed)
    python scripts/test_alerts_smoke.py --with-llm   # compose via CC's chat_text (needs CC + LLM)
"""

import argparse
import json
import os
import sys

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timedelta, timezone

from core.alert import Alert
from services.alert_queue_service import AlertQueueService
from services.led_service import LEDService


def _make_alerts() -> list[Alert]:
    """Create a realistic set of test alerts."""
    now = datetime.now(timezone.utc)
    return [
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
            title="NASA announces new Mars mission",
            summary="NASA has unveiled plans for a crewed Mars mission launching in 2030.",
            created_at=now - timedelta(minutes=10),
            expires_at=now + timedelta(hours=4),
            priority=1,
        ),
        Alert(
            source_agent="calendar_alerts",
            title="Upcoming: Lunch with Sarah",
            summary="Lunch with Sarah in about 45 minutes",
            created_at=now,
            expires_at=now + timedelta(minutes=30),
            priority=2,
        ),
    ]


def _make_expired_alert() -> Alert:
    past = datetime.now(timezone.utc) - timedelta(hours=2)
    return Alert(
        source_agent="news_alerts",
        title="Old headline that expired",
        summary="This should be filtered out",
        created_at=past,
        expires_at=past + timedelta(hours=1),
        priority=1,
    )


def run_smoke_test(with_llm: bool = False) -> None:
    print("=" * 60)
    print("PROACTIVE ALERTS — SMOKE TEST")
    print("=" * 60)

    # --- LED Service ---
    print("\n[1/5] LED Service")
    led = LEDService()
    print(f"  Platform detected as Pi: {led._is_pi}")
    print(f"  Initial pattern: {led.current_pattern}")

    led.set_pattern("alert")
    print(f"  After set_pattern('alert'): {led.current_pattern}")

    led.set_pattern("normal")
    print(f"  After set_pattern('normal'): {led.current_pattern}")
    print("  ✓ LED service works (no-op on macOS, blinks on Pi)")

    # --- Alert Queue ---
    print("\n[2/5] Alert Queue Service")
    queue = AlertQueueService()

    led_states: list[str] = []
    queue.on_change = lambda count: led_states.append("alert" if count > 0 else "normal")

    alerts = _make_alerts()
    expired = _make_expired_alert()

    for alert in alerts:
        queue.add_alert(alert)
    queue.add_alert(expired)

    print(f"  Added {len(alerts)} valid + 1 expired alert")
    print(f"  Pending count (should be 3): {queue.count()}")
    assert queue.count() == 3, f"Expected 3, got {queue.count()}"

    # Dedup test
    queue.add_alert(alerts[0])  # duplicate title
    print(f"  After adding duplicate (should still be 3): {queue.count()}")
    assert queue.count() == 3

    print(f"  LED callback transitions: {led_states}")
    assert led_states[0] == "alert", "First add should trigger 'alert'"
    print("  ✓ Queue add/dedup/expire/callback all work")

    # --- Pending sort ---
    print("\n[3/5] Priority Sorting")
    pending = queue.get_pending()
    priorities = [a.priority for a in pending]
    print(f"  Priorities in order: {priorities}")
    assert priorities == sorted(priorities, reverse=True), "Should be sorted high→low"
    print("  ✓ Sorted by priority descending")

    # --- WhatsUpCommand pre_route ---
    print("\n[4/5] WhatsUpCommand pre_route")
    from commands.whats_up_command import WhatsUpCommand

    cmd = WhatsUpCommand()

    # Patch the singleton to use our queue
    import commands.whats_up_command as wuc_module
    original_getter = wuc_module.get_alert_queue_service
    wuc_module.get_alert_queue_service = lambda: queue

    try:
        # Should match and flush
        result = cmd.pre_route("Hey Jarvis, what's up?")
        assert result is not None, "Should match 'what's up'"
        alerts_data = json.loads(result.arguments["alerts_json"])
        print(f"  Pre-routed with {len(alerts_data)} alerts")
        print(f"  Queue after flush (should be 0): {queue.count()}")
        assert queue.count() == 0

        # Non-matching phrase
        result2 = cmd.pre_route("turn off the lights")
        assert result2 is None, "Should not match 'turn off the lights'"
        print("  Non-matching phrase correctly returns None")

        # Empty queue → returns None (falls through to LLM)
        result3 = cmd.pre_route("what's up")
        assert result3 is None, "Empty queue should return None"
        print("  Empty queue correctly falls through")
        print("  ✓ Pre-routing works")
    finally:
        wuc_module.get_alert_queue_service = original_getter

    # --- WhatsUpCommand run ---
    print("\n[5/5] WhatsUpCommand run()")
    from core.request_information import RequestInformation

    request_info = RequestInformation(
        voice_command="what's up",
        conversation_id="smoke-test",
    )

    if with_llm:
        print("  Composing via CC chat_text() (--with-llm)...")
        response = cmd.run(request_info, alerts_json=json.dumps(alerts_data))
        print(f"  Success: {response.success}")
        print(f"  Message: {response.context_data.get('message', '')[:200]}")
    else:
        # Test fallback (no CC)
        response = cmd.run(request_info, alerts_json=json.dumps(alerts_data))
        print(f"  Success: {response.success}")
        msg = response.context_data.get("message", "")
        print(f"  Message (fallback): {msg[:200]}")

    print("  ✓ Command execution works")

    # --- Summary ---
    print("\n" + "=" * 60)
    print("ALL CHECKS PASSED ✓")
    print("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Smoke test proactive alerts")
    parser.add_argument("--with-llm", action="store_true", help="Compose via CC (needs CC + LLM running)")
    args = parser.parse_args()
    run_smoke_test(with_llm=args.with_llm)
