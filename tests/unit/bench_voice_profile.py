"""Explicit benchmark (not collected by the suite): what voice_profile costs one snapshot build.

    uv run --frozen --group dev python -m pytest -n 0 -s tests/unit/bench_voice_profile.py

For 50, 150 and 400 agents on a scratch store: the build time and the body size with the
profiles and without them (the resolver stubbed out), and the queries each build makes.
"""
import json
import statistics
import time

import pytest

from lib import agent_ring_live, agents, backends, config, db, personas, snapshot, voice_profile

RUNS = 7


def _seed(count: int):
    c = db.conn()
    ids = [f"bench-vp-{i:04d}" for i in range(count)]
    # A Host's mix: contacts with a stored Cartesia voice, archetype agents, silent helpers.
    def voice(i):
        return ['{"cartesia":"c0000000-0000-4000-8000-%012d"}' % (i % 66), "{}", ""][i % 3]
    c.executemany("""INSERT INTO agents (agent_id,persona,voice_id,cwd,session,backend,created_at)
        VALUES (?,?,?,'/fixture',?,'codex',1000)""",
        [(a, ("Claude-%04x" % i) if i % 3 == 1 else f"Persona {i}", voice(i), a)
         for i, a in enumerate(ids)])
    c.executemany("INSERT INTO runtimes(agent_id,session,backend_session_id,started_at) VALUES(?,?,?,1000)",
                  [(a, a, a) for a in ids])


def _build(monkeypatch, *, profiles: bool):
    with monkeypatch.context() as m:
        if not profiles:
            m.setattr(voice_profile.Projection, "__init__", lambda self, host, rows: None)
            m.setattr(voice_profile.Projection, "for_row", lambda self, a: None)
        times, body, queries = [], b"", []
        for _ in range(RUNS):
            counted = [0]
            db.conn().set_trace_callback(lambda _s: counted.__setitem__(0, counted[0] + 1))
            start = time.perf_counter()
            body = json.dumps(snapshot.build_agent_snapshot(None)).encode()
            times.append(time.perf_counter() - start)
            db.conn().set_trace_callback(None)
            queries.append(counted[0])
        if not profiles:
            # The stub left None on each row; a build without the feature has no key at all.
            data = json.loads(body)
            for row in data["agents"]:
                row.pop("voice_profile", None)
            data.pop("voice_defaults", None), data.pop("voice_profile_version", None)
            body = json.dumps(data).encode()
        return statistics.median(times), len(body), statistics.median(queries)


@pytest.mark.parametrize("count", [50, 150, 400])
def test_voice_profile_cost(monkeypatch, count):
    _seed(count)
    monkeypatch.setattr(config, "read_global_mcp_servers", lambda: {})
    monkeypatch.setattr(personas, "ensure_builtins", lambda: None)
    monkeypatch.setattr(snapshot, "_exhausted_backends", lambda: {})
    monkeypatch.setattr(agent_ring_live, "user_call_profiles", lambda **kw: {})
    monkeypatch.setattr(backends, "active_handles", lambda *a: [])
    snapshot.build_agent_snapshot(None)                      # repairs and caches; not measured
    off = _build(monkeypatch, profiles=False)
    on = _build(monkeypatch, profiles=True)
    print(f"\nagents={count} build_ms off={off[0] * 1000:.1f} on={on[0] * 1000:.1f} "
          f"(+{(on[0] - off[0]) * 1000:.1f}) bytes off={off[1]} on={on[1]} (+{on[1] - off[1]}, "
          f"{(on[1] - off[1]) / count:.0f}/agent) queries off={off[2]} on={on[2]}")
    assert on[2] == off[2]
