#!/usr/bin/env python3
"""One-time SimpliSafe OAuth setup.

Generates a PKCE authorization URL, walks the user through the browser
login + MFA flow, exchanges the code for tokens, and stores the refresh
token in JarvisStorage.

Usage:
    python scripts/authorize.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Add parent so device_families is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from device_families.simplisafe.simplisafe_client import (  # noqa: E402
    exchange_authorization_code,
    generate_code_challenge,
    generate_code_verifier,
    build_authorize_url,
)

try:
    from jarvis_command_sdk import JarvisStorage
    _storage = JarvisStorage("simplisafe")
except Exception:
    _storage = None


def main() -> None:
    print("=" * 60)
    print("  SimpliSafe OAuth Setup")
    print("=" * 60)
    print()

    # Step 1: Generate PKCE pair
    code_verifier = generate_code_verifier()
    code_challenge = generate_code_challenge(code_verifier)
    auth_url = build_authorize_url(code_challenge)

    print("1. Open this URL in your browser:\n")
    print(f"   {auth_url}\n")
    print("2. Log in with your SimpliSafe credentials")
    print("3. Complete MFA (SMS or email verification)")
    print("4. After login, the browser will try to redirect to a")
    print("   'com.simplisafe.mobile://...' URL that won't load.")
    print("   Copy the FULL URL from the address bar.\n")

    redirect_input = input("Paste the redirect URL (or just the code): ").strip()

    # Extract the authorization code
    if redirect_input.startswith("http") or redirect_input.startswith("com."):
        match = re.search(r"[?&]code=([^&]+)", redirect_input)
        if not match:
            print("\nError: Could not find 'code=' in the URL.", file=sys.stderr)
            sys.exit(1)
        authorization_code = match.group(1)
    else:
        authorization_code = redirect_input

    print(f"\nExchanging authorization code...")

    try:
        token_data = exchange_authorization_code(authorization_code, code_verifier)
    except Exception as e:
        print(f"\nError exchanging code: {e}", file=sys.stderr)
        sys.exit(1)

    refresh_token = token_data.get("refresh_token")
    if not refresh_token:
        print("\nError: No refresh_token in response.", file=sys.stderr)
        print(f"Response: {token_data}", file=sys.stderr)
        sys.exit(1)

    # Store the token
    if _storage:
        try:
            _storage.set_secret(
                "SIMPLISAFE_REFRESH_TOKEN", refresh_token, scope="integration",
            )
            print("\nRefresh token saved to JarvisStorage.")
        except Exception as e:
            print(f"\nWarning: Could not save to JarvisStorage: {e}")
            print(f"Refresh token: {refresh_token}")
    else:
        print(f"\nRefresh token (save this as SIMPLISAFE_REFRESH_TOKEN):")
        print(f"  {refresh_token}")

    print("\nSetup complete! Your SimpliSafe devices should now be discoverable.")


if __name__ == "__main__":
    main()
