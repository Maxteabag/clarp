#!/usr/bin/env python3
"""Preview or apply fallback models through authenticated, revision-guarded APIs."""

import argparse
import json
from pathlib import Path
from urllib.parse import quote
from agent_artifacts import _request


def configure(request, sessions, models, *, apply=False):
    # Read every revision before the first write. An unavailable route is an
    # error, never a successful empty configuration. Re-running skips matches.
    plans = []
    for session in sorted(set(sessions)):
        prior = request("GET", "/agent-fallbacks?session=" + quote(session, safe=""))
        plans.append(
            {
                "session": session,
                "models": models,
                "expected_revision": prior["revision"],
                "changed": prior["models"] != models,
            }
        )
    for plan in plans:
        if apply and plan["changed"]:
            body = {
                key: plan[key] for key in ("session", "models", "expected_revision")
            }
            response = request("POST", "/agent-fallbacks", body)
            if response["models"] != models:
                raise RuntimeError("Server did not retain requested fallback models")
    return {
        "applied": apply,
        "agents": len(plans),
        "changed": sum(p["changed"] for p in plans),
        "plans": plans,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    targets = parser.add_mutually_exclusive_group(required=True)
    targets.add_argument(
        "--all",
        action="store_true",
        help="All agents in this Host snapshot, including Janitors",
    )
    targets.add_argument("--session", action="append")
    parser.add_argument("--model", default="gemini-3.8-flash-low")
    parser.add_argument("--backend", default="agy")
    parser.add_argument("--effort", default="")
    parser.add_argument(
        "--apply", action="store_true", help="Without this flag, only preview"
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    sessions = (
        [a["session"] for a in _request("GET", "/agents/snapshot")["agents"]]
        if args.all
        else args.session
    )
    result = configure(
        _request,
        sessions,
        [{"backend": args.backend, "model": args.model, "effort": args.effort}],
        apply=args.apply,
    )
    text = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.write_text(text)
    print(text)


if __name__ == "__main__":
    main()
