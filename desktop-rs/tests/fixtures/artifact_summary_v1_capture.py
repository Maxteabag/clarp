"""Capture for tests/fixtures/artifact_summary_v1_host.json (the desktop's tests never run this).

Synthetic data only, in a temp DB (the Host's conftest `_isolated_db`), over the
Host's own in-process HTTP harness (tests/integration/test_artifact_summary_v1_http.py:
its `host` fixture builds ContextHTTPServer/Handler from server.py). Never the live
Host, never a copy of its state.sqlite. Run from a DETACHED Host worktree at the
pinned commit, through /var/tmp/dagger/bounded-run.sh, serially:

  WT=~/GIT/clarp-worktrees/host-parity-<sha>   # git worktree add --detach $WT <sha>
  cd $WT && CAPTURE_OUT=/var/tmp/.../raw.json CAPTURE_SHA=$(git rev-parse HEAD) \\
    /var/tmp/dagger/bounded-run.sh 2G 600 env PYTHONPATH=$WT/tests \\
    $PYTHON -m pytest -p conftest -p no:cacheprovider -n0 --timeout=300 \\
    --basetemp=/var/tmp/.../pytest --rootdir=$WT -c $WT/pyproject.toml THIS_FILE

then sanitize with make_artifact_summary_v1_host.py.
"""
import importlib.util
import json
import os
import subprocess
from pathlib import Path

from lib import artifacts, db

_harness_path = Path.cwd() / "tests/integration/test_artifact_summary_v1_http.py"
_spec = importlib.util.spec_from_file_location("artifact_summary_v1_http_harness", _harness_path)
harness = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(harness)
host = harness.host          # the Host's own fixture: synthetic agent, plan, forms, decision

# Revision 5's c1 vectors, as form contents ("" is written directly: creation refuses it).
VECTORS = ["", "<p>Hi</p>", "é漢😀", "line1\nline2\t\"q\" \\ /", "<img src=\"data:image/png;base64,iVBORw0KGgo=\">"]
BIG_116 = ("<html><body><p>é漢😀 report</p><img src=\"data:image/png;base64,"
           + "iVBORw0KGgoAAAANSUhEUgAA" * 48_300 + "\"></body></html>")   # ~1.16 MB


def _capture(world, path):
    status, body = harness._raw(world, path)
    try:
        parsed = json.loads(body)
    except ValueError:
        parsed = body.decode("utf-8", "replace")
    return {"path": path, "status": status, "bytes": len(body), "body": parsed, "raw": body.decode("utf-8")}


def _set_content(artifact_id, content):
    payload = json.loads(db.conn().execute("SELECT payload_json FROM artifacts WHERE artifact_id=?",
                                           (artifact_id,)).fetchone()[0])
    db.conn().execute("UPDATE artifacts SET payload_json=? WHERE artifact_id=?",
                      (json.dumps({**payload, "content": content}), artifact_id))


def test_capture_real_responses(host, monkeypatch):
    world, ids, clock = host, host.ids, host.clock
    sha = os.environ["CAPTURE_SHA"]
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == sha
    for n, content in enumerate(VECTORS):
        clock["now"] += 7
        ids[f"vector{n}"] = harness._form(content or "placeholder", f"c1 vector {n}")["artifact_id"]
        if not content:
            _set_content(ids[f"vector{n}"], content)
    clock["now"] += 7
    ids["big116"] = harness._form(BIG_116, "Big report 1.16 MB")["artifact_id"]
    clock["now"] += 7
    ids["surrogate"] = harness._form("<p>ok</p>", "Lone surrogate form")["artifact_id"]
    _set_content(ids["surrogate"], "bad \ud800 text")
    clock["now"] += 7
    ids["question"] = artifacts.create_decision(
        session="theo", title="Which region?", question="Where?", response_type="single_choice",
        options=[{"id": "eu", "label": "EU"}, {"id": "us", "label": "US"}])["artifact_id"]
    clock["now"] += 10_000
    q = harness._q
    out = {"host_sha": sha, "ids": ids, "vectors": VECTORS}
    # Pending markers first, while nothing is cached: a 10-byte hash budget.
    monkeypatch.setattr(artifacts, "SUMMARY_HASH_BUDGET_BYTES", 10)
    out["summary_pending"] = _capture(world, "/artifacts?" + q(representation="summary-v1", limit=200))
    monkeypatch.setattr(artifacts, "SUMMARY_HASH_BUDGET_BYTES", 64 * 1024 * 1024)
    out["server_info"] = _capture(world, "/server-info")
    out["summary"] = _capture(world, "/artifacts?" + q(representation="summary-v1", limit=200))
    out["flat"] = _capture(world, "/artifacts?" + q(representation="flat-v1", limit=200))
    out["pinned_summary"] = _capture(world, "/pinned-artifacts?" + q(representation="summary-v1"))
    out["pinned_flat"] = _capture(world, "/pinned-artifacts?" + q(representation="flat-v1"))
    out["refused"] = _capture(world, "/artifacts?" + q(representation="summary-v9"))
    out["pinned_refused"] = _capture(world, "/pinned-artifacts?" + q(representation="summary-v9"))
    out["inbox_refused"] = _capture(world, "/attention/inbox?" + q(representation="summary-v9"))
    out["missing"] = _capture(world, "/artifacts/art_does_not_exist?representation=flat-v1")
    names = ["small", "plan_artifact", "decision", "question", "surrogate", "big116"] + [f"vector{n}" for n in range(len(VECTORS))]
    out["items"] = {name: {"legacy": _capture(world, f"/artifacts/{ids[name]}"),
                           "flat": _capture(world, f"/artifacts/{ids[name]}?representation=flat-v1"),
                           "other": _capture(world, f"/artifacts/{ids[name]}?representation=bogus-v0")}
                    for name in names}
    Path(os.environ["CAPTURE_OUT"]).write_text(json.dumps(out, ensure_ascii=True))
