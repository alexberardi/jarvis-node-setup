#!/usr/bin/env python3
"""Scan for smart home devices using all available protocol adapters.

Usage:
    python scripts/scan_devices.py              # scan only
    python scripts/scan_devices.py --report     # scan and report to command center
    python scripts/scan_devices.py --family govee  # scan a single family
"""

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from utils.device_family_discovery_service import DeviceFamilyDiscoveryService


async def main() -> None:
    parser = argparse.ArgumentParser(description="Scan for smart home devices")
    parser.add_argument("--family", help="Scan a single family (e.g., govee, lifx, kasa)")
    parser.add_argument("--report", action="store_true", help="Report discovered devices to command center")
    parser.add_argument("--timeout", type=float, default=10.0, help="Scan timeout in seconds (default: 10)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    discovery = DeviceFamilyDiscoveryService()
    all_families = discovery.get_all_families_for_snapshot()

    if not all_families:
        print("No device families found. Check device_families/ directory.")
        return

    if args.family:
        if args.family not in all_families:
            print(f"Family '{args.family}' not found. Available: {', '.join(sorted(all_families.keys()))}")
            return
        families = {args.family: all_families[args.family]}
    else:
        families = all_families

    # Show what we're scanning
    if not args.json:
        print(f"\nScanning {len(families)} device family(s)...\n")
        for name, fam in families.items():
            missing = fam.validate_secrets()
            status = f"missing secrets: {missing}" if missing else "ready"
            print(f"  {fam.friendly_name} ({fam.connection_type}) - {status}")
        print()

    # Run discovery for all families
    all_devices = []
    for name, family in families.items():
        missing = family.validate_secrets()
        if missing:
            if not args.json:
                print(f"  Skipping {family.friendly_name}: missing {missing}")
            continue

        if not args.json:
            print(f"  Scanning {family.friendly_name}...", end=" ", flush=True)

        try:
            devices = await family.discover(timeout=args.timeout)
            all_devices.extend(devices)
            if not args.json:
                print(f"found {len(devices)} device(s)")
        except Exception as e:
            if not args.json:
                print(f"error: {e}")

    if args.json:
        output = [
            {
                "entity_id": d.entity_id,
                "name": d.name,
                "domain": d.domain,
                "manufacturer": d.manufacturer,
                "model": d.model,
                "protocol": d.protocol,
                "local_ip": d.local_ip,
                "cloud_id": d.cloud_id,
                "is_controllable": d.is_controllable,
            }
            for d in all_devices
        ]
        print(json.dumps(output, indent=2))
        return

    # Pretty print results
    if not all_devices:
        print("\nNo devices discovered.")
    else:
        print(f"\n{len(all_devices)} device(s) found:\n")
        for d in all_devices:
            icon = "+" if d.is_controllable else "-"
            location = d.local_ip or d.cloud_id or "unknown"
            print(f"  [{icon}] {d.entity_id}")
            print(f"      {d.name} ({d.manufacturer} {d.model})")
            print(f"      protocol={d.protocol}, location={location}")

    # Report to CC if requested
    if args.report and all_devices:
        from utils.config_service import Config
        from utils.service_discovery import get_command_center_url

        cc_url = get_command_center_url()
        node_id = Config.get_str("node_id", "") or ""
        api_key = Config.get_str("api_key", "") or ""
        household_id = Config.get_str("household_id", "") or ""

        # Fetch household_id from CC if not in local config
        if cc_url and node_id and api_key and not household_id:
            try:
                import httpx
                resp = httpx.get(
                    f"{cc_url}/api/v0/nodes/{node_id}",
                    headers={"X-API-Key": f"{node_id}:{api_key}"},
                    timeout=5,
                )
                if resp.status_code == 200:
                    household_id = resp.json().get("household_id", "")
            except Exception:
                pass

        if not all([cc_url, node_id, api_key, household_id]):
            missing = [k for k, v in [("cc_url", cc_url), ("node_id", node_id), ("api_key", api_key), ("household_id", household_id)] if not v]
            print(f"\nCannot report: missing {missing}")
            return

        from services.device_scanner_service import DeviceScannerService

        admin_key = os.environ.get("ADMIN_API_KEY", "") or Config.get_str("admin_api_key", "") or ""
        scanner = DeviceScannerService(cc_url, node_id, api_key, household_id, admin_key=admin_key)
        result = await scanner.report_to_cc(all_devices)
        print(f"\nReported to CC: {result}")


if __name__ == "__main__":
    asyncio.run(main())
