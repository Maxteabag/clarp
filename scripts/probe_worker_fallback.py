#!/usr/bin/env python3
"""Explicit paid probe: failed primary -> real Gemini tool use, in private state."""

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="clarp-worker-fallback-proof-") as folder:
        root = Path(folder)
        for key, path in {
            "CLARP_SHARE_DIR": "share",
            "CLARP_CONFIG_DIR": "config",
            "CLARP_CACHE_DIR": "cache",
            "CLAUDE_PWA_DB": "state.sqlite",
        }.items():
            os.environ[key] = str(root / path)
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
        from lib import agents, backends, db, model_fallbacks, settings_store
        from lib.turn_dispatch import TurnDispatchService

        settings_store.set_text(
            "provider.agy.last_observed_model_ids", '["gemini-3.8-flash-low"]'
        )
        workspace = root / "workspace"
        workspace.mkdir()
        aid = agents.create_agent(
            persona="Fallback Probe",
            voice_id="",
            cwd=str(workspace),
            session="fallback-probe",
            backend="codex",
        )
        agents.start_runtime(aid, "fallback-probe")
        model_fallbacks.configure(
            aid,
            [{"backend": "agy", "model": "gemini-3.8-flash-low", "effort": ""}],
            expected_revision=0,
        )
        calls = []

        class Primary:
            process_group = None

            def terminate(self):
                pass

            def wait(self, timeout=None):
                return 1

        class Registry:
            def __getattr__(self, name):
                return getattr(backends, name)

            def spawn_turn(self, backend, **kwargs):
                calls.append(backend)
                if backend == "codex":
                    threading.Timer(
                        0.1,
                        lambda: kwargs["on_error"](
                            "usage limit reached (injected probe)"
                        ),
                    ).start()
                    return Primary()
                return backends.spawn_turn(backend, **kwargs)

        stream = SimpleNamespace(broadcast=lambda event: None)
        service = TurnDispatchService(
            SimpleNamespace(
                default_session="fallback-probe",
                stream=stream,
                agents_path=root / "agents.json",
            ),
            backend_registry=Registry(),
        )
        started = time.monotonic()
        service.dispatch(
            text="In this temporary workspace only, write proof.txt containing exactly fallback-ok, then reply that the file is written. Do not change any other workspace or contact any service.",
            requested_session="fallback-probe",
            trace_id="probe",
            synthesize_audio=False,
        )
        while time.monotonic() - started < 100:
            state = agents.latest_state(aid)
            if state and state["kind"] in ("done", "interrupted"):
                break
            time.sleep(0.25)
        receipts = model_fallbacks.attempts(aid, "probe")
        text = (
            (workspace / "proof.txt").read_text().strip()
            if (workspace / "proof.txt").exists()
            else ""
        )
        good = (
            text == "fallback-ok"
            and calls == ["codex", "agy"]
            and bool(receipts)
            and receipts[0]["status"] == "completed"
        )
        result = {
            "verified": good,
            "calls": calls,
            "file_contents": text,
            "state": dict(state) if state else None,
            "primary_backend": agents.get_by_agent_id(aid)["backend"],
            "primary_conversation": agents.live_backend_session(aid),
            "receipt_statuses": [r["status"] for r in receipts],
            "assistant_text": [
                json.loads(r["result_json"] or "{}").get("text", "") for r in receipts
            ],
            "elapsed_seconds": round(time.monotonic() - started, 2),
        }
        good = (
            good
            and result["primary_backend"] == "codex"
            and result["primary_conversation"] == ""
        )
        result["verified"] = good
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result))
        backends.interrupt_any(aid)
        db.close_local()
        if not good:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
