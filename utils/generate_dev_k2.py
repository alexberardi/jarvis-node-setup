#!/usr/bin/env python3
"""Generate a shared K2 key for dev use.

Creates K2 on the node side and outputs a base64url QR payload string
that can be pasted into the mobile app's Import Key screen (simulator).

Usage:
    cd jarvis-node-setup
    python utils/generate_dev_k2.py
    python utils/generate_dev_k2.py --force   # overwrite existing K2
"""

import argparse
import base64
import json
import os
import sys
from datetime import datetime, timezone

# Add parent dir so imports resolve when running as a script
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from utils.encryption_utils import (
    has_k2,
    initialize_encryption_key,
    save_k2,
)
from utils.config_service import Config


def _b64url_encode(data: bytes) -> str:
    """Base64url encode without padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate K2 for dev")
    parser.add_argument("--force", action="store_true", help="Overwrite existing K2")
    args = parser.parse_args()

    # Check for existing K2
    if has_k2() and not args.force:
        print("K2 already exists. Use --force to overwrite.")
        sys.exit(1)

    # Read node_id from config
    node_id: str = Config.get_str("node_id", "") or ""
    if not node_id:
        print("Error: node_id not found in config. Run authorize_node.py first.")
        sys.exit(1)

    # Ensure K1 exists (needed to encrypt K2 at rest)
    initialize_encryption_key()

    # Generate 32 random bytes for K2
    k2_raw: bytes = os.urandom(32)
    k2_b64url: str = _b64url_encode(k2_raw)

    # Build key ID
    now: datetime = datetime.now(timezone.utc)
    kid: str = f"k2-dev-{now.strftime('%Y%m%d%H%M%S')}"

    # Save K2 to ~/.jarvis/k2.enc
    save_k2(k2_b64url, kid, now)

    # Build plain QR payload
    payload: dict = {
        "v": 1,
        "mode": "plain",
        "node_id": node_id,
        "kid": kid,
        "k2": k2_b64url,
        "created_at": now.isoformat(),
    }

    # Base64url encode for mobile import
    payload_json: str = json.dumps(payload, separators=(",", ":"))
    payload_b64url: str = _b64url_encode(payload_json.encode("utf-8"))

    print()
    print("=== K2 Generated ===")
    print(f"  node_id:    {node_id}")
    print(f"  kid:        {kid}")
    print(f"  k2 (b64url): {k2_b64url}")
    print(f"  created_at: {now.isoformat()}")
    print()
    print("=== Paste this into the mobile app Import Key screen ===")
    print(payload_b64url)
    print()


if __name__ == "__main__":
    main()
