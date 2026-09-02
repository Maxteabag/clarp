#!/usr/bin/env python3
"""Request an Apple Calendar event through the Clarp server."""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-url", default=os.environ.get("CLARP_SERVER_URL")
                        or os.environ.get("CLAUDE_PWA_URL")
                        or "http://127.0.0.1:8765")
    parser.add_argument("--token", default=os.environ.get("CLARP_AUTH_TOKEN")
                        or os.environ.get("CLAUDE_PWA_TOKEN")
                        or "")
    parser.add_argument("--session", default=os.environ.get("CLARP_SESSION", ""))
    parser.add_argument("--title", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--time-zone", default="")
    parser.add_argument("--location", default="")
    parser.add_argument("--notes", default="")
    parser.add_argument("--url", default="")
    parser.add_argument("--calendar", default="")
    parser.add_argument("--all-day", action="store_true")
    args = parser.parse_args()

    session = args.session.strip()
    if not session:
        parser.error("--session is required when CLARP_SESSION is not set")

    body = {
        "session": session,
        "title": args.title,
        "start": args.start,
        "end": args.end,
        "time_zone": args.time_zone,
        "location": args.location,
        "notes": args.notes,
        "url": args.url,
        "calendar": args.calendar,
        "all_day": args.all_day,
    }
    data = json.dumps(body).encode()
    base = args.server_url.rstrip("/")
    request = urllib.request.Request(
        f"{base}/calendar/request",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    if args.token:
        request.add_header("Authorization", f"Bearer {args.token}")

    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        print(f"calendar request failed: HTTP {exc.code} {detail}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"calendar request failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(payload, indent=2, sort_keys=True))
    if payload.get("ok"):
        print(f"requested calendar event: {args.title}")
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
