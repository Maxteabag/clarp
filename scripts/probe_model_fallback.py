#!/usr/bin/env python3
"""Paid, explicitly invoked Gemini failover probe; all Clarp state is private.

Creates an isolated DB, forces the primary explainer to fail, then runs the real
configured fallback and verifies the answer describes the supplied operation.
"""

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="clarp-fallback-proof-") as folder:
        root = Path(folder)
        for key, path in {
            "CLARP_SHARE_DIR": "share",
            "CLARP_CONFIG_DIR": "config",
            "CLARP_CACHE_DIR": "cache",
            "CLAUDE_PWA_DB": "state.sqlite",
        }.items():
            os.environ[key] = str(root / path)
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
        from lib import janitor_builtins, model_fallbacks, settings_store, db
        from lib.tool_explanations import ToolExplanations

        settings_store.set_text(
            "provider.agy.last_observed_model_ids", '["gemini-3.8-flash-low"]'
        )
        owner = janitor_builtins.ensure_builtins(cwd=str(root))["tool-explainer"]
        model_fallbacks.configure(
            owner["agent_id"],
            [{"backend": "agy", "model": "gemini-3.8-flash-low", "effort": ""}],
            expected_revision=0,
        )
        calls = []

        def fail(*_args, **_kwargs):
            calls.append("forced_primary_failure")
            raise RuntimeError("usage limit reached (isolated test injection)")

        started = time.monotonic()
        with ToolExplanations(debounce=0.01) as service:
            service._run_codex = fail
            while time.monotonic() - started < 65:
                result = service.request(
                    3, [{"id": "probe", "activity": {"command": "ls -la"}}]
                )["items"][0]
                if result["status"] != "pending":
                    break
                time.sleep(0.25)
            receipts = [
                dict(x)
                for x in db.conn().execute(
                    "SELECT backend,model,status FROM model_fallback_attempts"
                )
            ]
            good = result["status"] == "ready" and any(
                term in result.get("text", "").lower()
                for term in ("files", "directory", "folder")
            )
            good = (
                good
                and calls == ["forced_primary_failure"]
                and len(receipts) == 1
                and receipts[0]["status"] == "completed"
            )
            evidence = {
                "verified": good,
                "result": result,
                "primary_calls": calls,
                "receipts": receipts,
                "elapsed_seconds": round(time.monotonic() - started, 2),
            }
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(evidence, indent=2) + "\n")
            print(json.dumps(evidence))
        db.close_local()
        if not good:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
