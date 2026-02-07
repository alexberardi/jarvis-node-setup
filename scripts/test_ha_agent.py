#!/usr/bin/env python3
"""Quick test script for Home Assistant agent connectivity."""

import asyncio
import json
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from agents.home_assistant_agent import HomeAssistantAgent


async def main():
    agent = HomeAssistantAgent()

    # Check secrets
    missing = agent.validate_secrets()
    if missing:
        print(f"Missing secrets: {missing}")
        print("Set them with:")
        print('  python utils/set_secret.py set --key HOME_ASSISTANT_WS_URL --value "ws://IP:8123/api/websocket" --scope integration')
        print('  python utils/set_secret.py set --key HOME_ASSISTANT_API_KEY --value "your-token" --scope integration')
        return

    print("Secrets configured. Connecting to Home Assistant...")

    # Run the agent
    await agent.run()

    # Check results
    context = agent.get_context_data()

    if context.get("last_error"):
        print(f"\nError: {context['last_error']}")
        return

    print(f"\nSuccess! Found:")
    print(f"  - {context['device_count']} devices")
    print(f"  - {context['entity_count']} entities")
    print(f"  - {len(context['areas'])} areas: {', '.join(context['areas'][:10])}")

    if context['devices']:
        print(f"\nSample devices:")
        for device in context['devices'][:5]:
            area = device.get('area') or 'No area'
            entities = len(device.get('entities', []))
            print(f"  - {device['name']} ({area}) - {entities} entities")

    # Dump raw data to file for inspection
    output_file = Path(__file__).parent.parent / "ha_agent_dump.json"
    raw_data = {
        "areas": agent._areas,
        "devices": agent._devices,
        "entities": agent._entities,
        "states": agent._states,
        "context": context,
    }
    with open(output_file, "w") as f:
        json.dump(raw_data, f, indent=2, default=str)
    print(f"\nRaw data written to: {output_file}")


if __name__ == "__main__":
    asyncio.run(main())
