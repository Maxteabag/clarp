#!/usr/bin/env python3
"""Refresh-boundary and Antigravity projection cases from the isolated Python Host."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
OUT = HERE.parent / "crates/clarp-kernel/tests/fixtures/usage-ask-vectors.json"


def cases(place):
    spec = importlib.util.spec_from_file_location("replay_host", ROOT / "tests/replay/host.py")
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    (place / "scenario-config.toml").write_text("")
    host.isolate(place)
    host.loopback_only()
    import sys
    sys.path.insert(0, str(ROOT / "server"))
    from lib import backend_usage as usage, backend_usage_agy as agy, backend_usage_store as store
    began = 1900000000000
    now = [began]
    for module in [usage, agy, store]:
        module._now_ms = lambda: now[0]
    store._computer_id = lambda: "a-host"
    groups = [
        {"name": "Example", "buckets": [
            {"window": "5h", "remaining_fraction": 0.625, "reset_time": "2030-03-17T17:47:40Z", "id": "alpha"},
            {"window": "weekly", "remaining_fraction": 1.0, "reset_time": "2030-03-24T17:46:40Z"},
            {"window": "other", "remaining_fraction": 0.0},
            {"window": "5h", "remaining_fraction": "0.75"}]},
        {"buckets": [{"window": "weekly", "remaining_fraction": True}]}]
    payload = {"command": {"data": {"groups": groups}}}
    agy._agy_executable = lambda: "/example/agy"
    agy._run_agy_usage = lambda *args: (json.dumps(payload), "", 0)
    assert agy._agy_needs_refresh()
    agy.fetch_agy_usage()
    rows = []
    for delta in [0, 59999, 60000, 299999, 300000, 300001, 3599999, 3600000, 3600001]:
        now[0] = began + delta
        rows.append({"now": now[0], "due": agy._agy_needs_refresh(),
                     "provider": usage._structured_provider("agy", now[0])})
    from lib import backend_usage_codex as codex
    codex.log = lambda *args, **kwargs: None
    from lib import backend_usage_claude as claude
    from lib import db
    active = ["account-one"]
    for module in [claude, codex]:
        module._now_ms = lambda: now[0]
    claude._active_claude_account_ref = lambda **kwargs: active[0]
    codex._active_codex_identity = lambda **kwargs: ("generation-one", active[0])
    db.conn().execute(
        "INSERT INTO agents (agent_id, persona, voice_id, cwd, session, created_at, backend) VALUES (?,?,?,?,?,?,?)",
        ("codex-example", "Example", "", "/", "example", began, "codex"))
    direct = {"rate_limit": {"primary_window": {"used_percent": 100, "limit_window_seconds": 18000,
                                                 "reset_at": began // 1000 + 5000}}}
    now[0] = began
    codex.capture_codex_rate_limits(direct, source_detail="/wham/usage", identity=("generation-one", active[0]))
    store._upsert("claude", used_percentage=22, resets_at="", source="anthropic-oauth-usage",
                  raw={"account_scope_ref": active[0]})
    ages = []
    for completed in [False, True]:
        if completed:
            db.conn().execute("INSERT INTO state_log (agent_id, runtime_id, ts, kind, detail) VALUES (?,?,?,?,?)",
                              ("codex-example", None, began + 1, "done", "{}"))
        for delta in [0, 1, 299999, 300000, 300001]:
            now[0] = began + delta
            for account in ["account-one", "another"]:
                active[0] = account
                ages.append({"now": now[0], "active": account, "done": completed,
                             "claude": claude._claude_needs_refresh(), "codex": codex._codex_needs_refresh()})
    return {"at": began, "payload": payload, "cases": rows, "direct": direct, "ages": ages}


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as root, contextlib.redirect_stdout(io.StringIO()):
        result = cases(Path(root))
    OUT.write_text(json.dumps(result, sort_keys=True, indent=1) + "\n", encoding="ascii")
