"""Capture for tests/fixtures/artifact_summary_v1_host.json (not run by the desktop's tests).

Append this to a copy of the Host's tests/integration/test_artifact_summary_v1_http.py
(its `host` fixture, `_form`, `_raw`, `_q`), run it in-process against the conftest's
isolated temp DB (Python only, never the live Host), then sanitize the output with
make_artifact_summary_v1_host.py. Captured from feat/artifact-summary-v1 @ dfc489bb.
"""
# fmt: off
# ---- desktop parity capture (not part of the Host suite) ---------------------

BIG_116 = ("<html><body><p>é漢😀 report</p><img src=\"data:image/png;base64,"
           + "iVBORw0KGgoAAAANSUhEUgAA" * 48_300 + "\"></body></html>")   # ~1.16 MB
OUT = Path("/var/tmp/clarp-summary-capture/real_host_dfc489bb.json")


def _capture(world, path):
    status, body = _raw(world, path)
    try:
        parsed = json.loads(body)
    except ValueError:
        parsed = body.decode("utf-8", "replace")
    return {"path": path, "status": status, "bytes": len(body), "body": parsed, "raw": body.decode("utf-8")}


def test_capture_real_responses(host, monkeypatch):
    world = host
    ids = world.ids
    clock = world.clock
    clock["now"] += 7
    ids["big116"] = _form(BIG_116, "Big report 1.16 MB")["artifact_id"]
    clock["now"] += 7
    ids["surrogate"] = _form("<p>ok</p>", "Lone surrogate form")["artifact_id"]
    payload = json.loads(db.conn().execute("SELECT payload_json FROM artifacts WHERE artifact_id=?",
                                           (ids["surrogate"],)).fetchone()[0])
    db.conn().execute("UPDATE artifacts SET payload_json=? WHERE artifact_id=?",
                      (json.dumps({**payload, "content": "bad \ud800 text"}), ids["surrogate"]))
    clock["now"] += 7
    question = artifacts.create_decision(session="theo", title="Which region?", question="Where?",
                                         response_type="single_choice",
                                         options=[{"id": "eu", "label": "EU"}, {"id": "us", "label": "US"}])
    ids["question"] = question["artifact_id"]
    clock["now"] += 10_000
    out = {"ids": ids}
    # Pending markers first, while nothing is cached: a budget of 10 bytes.
    monkeypatch.setattr(artifacts, "SUMMARY_HASH_BUDGET_BYTES", 10)
    out["summary_pending"] = _capture(world, "/artifacts?" + _q(representation="summary-v1", limit=200))
    monkeypatch.setattr(artifacts, "SUMMARY_HASH_BUDGET_BYTES", 64 * 1024 * 1024)
    out["server_info"] = _capture(world, "/server-info")
    out["summary"] = _capture(world, "/artifacts?" + _q(representation="summary-v1", limit=200))
    out["flat"] = _capture(world, "/artifacts?" + _q(representation="flat-v1", limit=200))
    out["pinned_summary"] = _capture(world, "/pinned-artifacts?" + _q(representation="summary-v1"))
    out["pinned_flat"] = _capture(world, "/pinned-artifacts?" + _q(representation="flat-v1"))
    out["refused"] = _capture(world, "/artifacts?" + _q(representation="summary-v9"))
    out["pinned_refused"] = _capture(world, "/pinned-artifacts?" + _q(representation="summary-v9"))
    out["inbox_refused"] = _capture(world, "/attention/inbox?" + _q(representation="summary-v9"))
    out["missing"] = _capture(world, "/artifacts/art_does_not_exist?representation=flat-v1")
    items = {}
    for name in ("small", "plan_artifact", "decision", "question", "surrogate", "big116"):
        artifact_id = ids[name]
        items[name] = {"legacy": _capture(world, f"/artifacts/{artifact_id}"),
                       "flat": _capture(world, f"/artifacts/{artifact_id}?representation=flat-v1"),
                       "other": _capture(world, f"/artifacts/{artifact_id}?representation=summary-v1")}
    out["items"] = items
    OUT.write_text(json.dumps(out, ensure_ascii=True))
