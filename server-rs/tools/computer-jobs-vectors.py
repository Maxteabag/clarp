#!/usr/bin/env python3
"""Capture computer-owned jobs without starting a worker or creating an agent."""
import json
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "server-rs/crates/clarp-kernel/tests/fixtures/computer-jobs.json"


def record():
    replay = runpy.run_path(str(ROOT / "tests/replay/host.py"))
    with tempfile.TemporaryDirectory() as tmp:
        replay["isolate"](Path(tmp))
        replay["loopback_only"]()
        sys.path.insert(0, str(ROOT / "server"))
        from lib import background_jobs as jobs, db
        from server import Handler

        class Route:
            def _expected_generation(self):
                return True, self.expected

            def _json(self, code, value):
                return [code, value]

            def _json_error(self, code, reason):
                return self._json(code, {"error": reason})

            def _json_ok(self, value):
                return self._json(200, value)

        route = Route()
        result = []
        for kind, title in [("transcription-model-install", "Install speech model"),
                            ("portrait", "Make picture")]:
            job_id = f"fixture-{kind}"
            for action in ("absent", "register", "join", "start", "stale_finish",
                           "stale_cancel", "cancel", "cancel_again", "restart",
                           "finish", "late_cancel"):
                now = 1000000 + 1000 * len(result)
                def register():
                    return jobs.upsert_computer(computer_id="computer-fixture",
                        job_id=job_id, kind=kind, title=title, detail="Preparing",
                        metadata={"fixture": True}, status="queued", restart_cancelled=True,
                        heartbeat_timeout_ms=120000)
                with patch.object(db, "now_ms", return_value=now):
                    receipt = None
                    if action in ("register", "join", "restart"):
                        register()
                    elif action == "start":
                        jobs.start(job_id, generation=1)
                    elif action in ("finish", "stale_finish"):
                        jobs.finish(job_id, generation=2)
                    elif action in ("absent", "stale_cancel", "cancel", "cancel_again", "late_cancel"):
                        route.expected = {"stale_cancel": 2, "cancel": 1, "late_cancel": 2}.get(action)
                        receipt = Handler._handle_background_job_cancel(route, job_id)
                    result.append(dict(id=job_id, kind=kind, title=title, now=now, action=action,
                                       receipt=receipt, detail=jobs.detail(job_id), listing=jobs.snapshot()))
        return result


if __name__ == "__main__":
    if sys.argv[1:] == ["--record"]:
        print(json.dumps(record(), ensure_ascii=True, separators=(",", ":")))
    else:
        OUT.write_bytes(subprocess.check_output([sys.executable, "-IB", __file__, "--record"],
                        env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}))
