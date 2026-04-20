"""Pull negative-rated transcripts into a JSONL review queue (Phase 2).

Queries command-center's `GET /api/v0/transcripts/recent`, filters for
`user_rating == -1` (thumbs-down in the mobile Feedback tab), and writes one
line per transcript to `review_queue.jsonl`. A human then reviews each line
and promotes the actionable ones into new `CommandTest(...)` entries in
`test_command_parsing.py` so real regressions become permanent test cases.

Usage:
    # Requires a valid user JWT — either env var or CLI arg
    export JARVIS_USER_TOKEN=<jwt>
    python import_review_queue.py --since 2026-04-01T00:00:00Z
    python import_review_queue.py --since 7d --out review_queue.jsonl

Env:
    JARVIS_USER_TOKEN  — user JWT with access to the transcripts API (required
                         unless --token is passed)
    JARVIS_CC_URL      — command-center base URL (default http://localhost:7703)

Output (one JSONL per line):
    {
      "id": 42,
      "created_at": "2026-04-18T22:14:03Z",
      "user_message": "play some jazz in the kitchen",
      "tool_call": { "name": "music", "arguments": {"action": "play", "query": "jazz"} },
      "assistant_message": "playing jazz",
      "conversation_id": "…",
      "user_id": 1,
      "rating_notes": "wrong room — should have been living room"
    }
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

DEFAULT_CC_URL = "http://localhost:7703"
DEFAULT_LIMIT = 200  # API caps at 200 — enough for a reviewing session


def parse_since(value: str) -> str:
    """Accepts ISO-8601 timestamps or relative shorthand like '7d', '48h', '30m'.

    Returns an ISO-8601 string suitable for the /recent?since= query param.
    """
    # Relative shorthand: <number><unit> where unit is d/h/m
    m = re.match(r"^(\d+)([dhm])$", value.strip())
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        delta = {"d": timedelta(days=n), "h": timedelta(hours=n), "m": timedelta(minutes=n)}[unit]
        dt = datetime.now(timezone.utc) - delta
        return dt.isoformat().replace("+00:00", "Z")

    # Validate as ISO-8601 — let fromisoformat raise on garbage
    # Accept trailing Z by replacing with +00:00 first.
    test = value.replace("Z", "+00:00") if value.endswith("Z") else value
    datetime.fromisoformat(test)  # raises ValueError if bad
    return value


def fetch_recent(base_url: str, token: str, since: str, limit: int) -> list[dict]:
    params = {"limit": limit, "since": since}
    url = f"{base_url.rstrip('/')}/api/v0/transcripts/recent?{urlencode(params)}"
    req = Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    })
    with urlopen(req, timeout=30) as resp:
        body = resp.read()
    return json.loads(body)


def first_tool_call(row: dict) -> dict | None:
    tcs = row.get("tool_calls")
    if isinstance(tcs, list) and tcs:
        first = tcs[0]
        if isinstance(first, dict):
            return first
    return None


def to_review_row(row: dict) -> dict:
    return {
        "id": row["id"],
        "created_at": row["created_at"],
        "user_id": row["user_id"],
        "conversation_id": row["conversation_id"],
        "user_message": row["user_message"],
        "tool_call": first_tool_call(row),
        "assistant_message": row.get("assistant_message"),
        "rating_notes": row.get("rating_notes"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Pull thumbs-down transcripts into a review JSONL")
    parser.add_argument("--since", type=str, default="7d",
                        help="ISO-8601 or relative (e.g. '7d', '48h', '30m'). Default: 7d")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                        help=f"Max transcripts to fetch (default {DEFAULT_LIMIT}, API cap 200)")
    parser.add_argument("--out", type=Path, default=Path("review_queue.jsonl"),
                        help="Output JSONL path (default: review_queue.jsonl)")
    parser.add_argument("--token", type=str, default=None,
                        help="User JWT (falls back to JARVIS_USER_TOKEN env)")
    parser.add_argument("--cc-url", type=str, default=None,
                        help="Command-center base URL (falls back to JARVIS_CC_URL env or localhost:7703)")
    parser.add_argument("--include-unrated", action="store_true",
                        help="Include all recent transcripts in the output, not just thumbs-down. "
                             "Useful for one-off review of a period.")
    args = parser.parse_args()

    token = args.token or os.getenv("JARVIS_USER_TOKEN")
    if not token:
        print("ERROR: user JWT required via --token or JARVIS_USER_TOKEN env", file=sys.stderr)
        return 2

    base_url = args.cc_url or os.getenv("JARVIS_CC_URL") or DEFAULT_CC_URL

    try:
        since_iso = parse_since(args.since)
    except ValueError as e:
        print(f"ERROR: bad --since value {args.since!r}: {e}", file=sys.stderr)
        return 2

    try:
        rows = fetch_recent(base_url, token, since_iso, args.limit)
    except Exception as e:  # urllib raises HTTPError on 4xx/5xx
        print(f"ERROR: fetch failed: {e}", file=sys.stderr)
        return 1

    if args.include_unrated:
        filtered = rows
        label = "all"
    else:
        filtered = [r for r in rows if r.get("user_rating") == -1]
        label = "thumbs-down"

    with args.out.open("w", encoding="utf-8") as f:
        for row in filtered:
            f.write(json.dumps(to_review_row(row), ensure_ascii=False) + "\n")

    print(
        f"wrote {len(filtered)} {label} transcripts to {args.out} "
        f"(fetched {len(rows)} total since {since_iso})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
