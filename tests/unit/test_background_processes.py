"""Sub-agents versus background processes, dead workers, and job inspection."""
from __future__ import annotations

import importlib.util
import os
import pathlib
import subprocess
import sys

import pytest

from lib import agents, background_jobs, turn_lifecycle
from lib.background_job_watcher import BackgroundJobWatcher
from lib.policies.helper_state import HelperEvent, Role
from lib.snapshot import build_agent_snapshot


_SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "agent_bg.py"
_SPEC = importlib.util.spec_from_file_location("agent_bg_processes", _SCRIPT)
assert _SPEC and _SPEC.loader
agent_bg = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(agent_bg)


def _parent(session: str = "boss", cwd: str = "/srv/none") -> str:
    aid = agents.create_agent(persona=session.title(), voice_id="", cwd=cwd, session=session)
    agents.record_state(aid, "done", {})
    return aid


def _helper(parent_id: str, session: str) -> str:
    aid = agents.create_agent(persona=session, voice_id="", cwd="/srv/none", session=session)
    agents.set_lineage(aid, parent_agent_id=parent_id, role=Role.HELPER)
    return aid


def _row(agent_id: str) -> dict:
    return next(r for r in build_agent_snapshot(None)["agents"] if r["agent_id"] == agent_id)


# ---- counting --------------------------------------------------------------

def test_running_helpers_are_sub_agents_not_processes():
    boss = _parent()
    _helper(boss, "stream-a")
    _helper(boss, "stream-b")
    done = _helper(boss, "stream-c")
    agents.apply_helper_event(done, HelperEvent.REPORTED)

    row = _row(boss)

    assert row["background_jobs"] == {"count": 0, "sub_agents": 2}
    assert row["running_children"] == 2
    assert row["status_text"] == "2 sub-agents"
    assert row["latest_state"] == "background"


def test_processes_only_count_as_background_processes():
    boss = _parent()
    background_jobs.upsert(session="boss", job_id="ci", kind="ci", title="CI")

    row = _row(boss)

    assert row["background_jobs"] == {"count": 1, "sub_agents": 0}
    assert row["status_text"] == "1 background process"


def test_helper_mirror_job_is_not_a_second_sub_agent():
    """Observed live: three helpers plus their three watcher jobs read as
    "6 sub-agents running"."""
    boss = _parent()
    for name in ("a", "b", "c"):
        _helper(boss, f"helper-{name}")
        background_jobs.upsert(
            session="boss", job_id=f"sub-agent-{name}", kind="sub-agent",
            title=f"Helper {name}", detail=f"helper-{name}")
    background_jobs.upsert(session="boss", job_id="deploy", kind="deploy", title="Deploy")

    row = _row(boss)

    assert row["background_jobs"] == {"count": 1, "sub_agents": 3}
    assert row["status_text"] == "3 sub-agents · 1 process"


def test_mirror_is_recognised_from_metadata_and_only_for_own_helpers():
    boss = _parent()
    other = _parent("other")
    _helper(boss, "mine")
    _helper(other, "theirs")
    background_jobs.upsert(
        session="boss", job_id="meta", kind="sub-agent", title="Mine",
        metadata={"helper_session": "mine"})
    # Names a helper, but not one of boss's: a real process.
    background_jobs.upsert(
        session="boss", job_id="foreign", kind="sub-agent", title="Theirs",
        detail="theirs")

    row = _row(boss)

    assert row["background_jobs"] == {"count": 1, "sub_agents": 1}


def test_detached_workers_and_legacy_sub_agent_jobs_are_processes():
    boss = _parent()
    background_jobs.upsert(session="boss", job_id="w1", kind="worker", title="W1", detail="w1")
    # A pre-change systemd sub-agent registered kind sub-agent with its name.
    background_jobs.upsert(session="boss", job_id="w2", kind="sub-agent", title="W2", detail="w2")

    row = _row(boss)

    assert row["background_jobs"] == {"count": 2, "sub_agents": 0}
    assert row["status_text"] == "2 background processes"


# ---- dead workers ------------------------------------------------------------

def _dead_pid() -> tuple[int, str]:
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    token = background_jobs.process_start_token(proc.pid)
    proc.kill()
    proc.wait()
    return proc.pid, token


def test_dead_worker_fails_without_waiting_for_heartbeat_timeout():
    _parent()
    pid, token = _dead_pid()
    assert token
    job = background_jobs.upsert(
        session="boss", job_id="stopped", kind="worker", title="Stopped",
        worker_pid=pid, worker_start_token=token)

    changed = background_jobs.reconcile_stale(now_ms=job["heartbeat_at"] + 1)

    assert changed == ["stopped"]
    failed = background_jobs.get("stopped", reconcile=False)
    assert failed["status"] == "failed"
    assert failed["terminal_reason"] == "worker_vanished"


def test_start_token_mismatch_counts_as_a_dead_worker():
    _parent()
    job = background_jobs.upsert(
        session="boss", job_id="reused", kind="worker", title="Reused",
        worker_pid=os.getpid(), worker_start_token="boot:someone-else")

    assert background_jobs.reconcile_stale(
        job_id="reused", now_ms=job["heartbeat_at"] + 1) == ["reused"]


def test_live_worker_with_fresh_heartbeat_stays_running():
    _parent()
    job = background_jobs.upsert(
        session="boss", job_id="alive", kind="worker", title="Alive",
        worker_pid=os.getpid(),
        worker_start_token=background_jobs.process_start_token(os.getpid()))

    assert background_jobs.reconcile_stale(now_ms=job["heartbeat_at"] + 1) == []
    assert background_jobs.get("alive", reconcile=False)["status"] == "running"


def test_heartbeat_timeout_applies_even_with_a_live_worker_pid():
    _parent()
    job = background_jobs.upsert(
        session="boss", job_id="wedged", kind="worker", title="Wedged",
        heartbeat_timeout_ms=30_000, worker_pid=os.getpid(),
        worker_start_token=background_jobs.process_start_token(os.getpid()))

    changed = background_jobs.reconcile_stale(
        now_ms=job["heartbeat_at"] + job["heartbeat_timeout_ms"] + 1)

    assert changed == ["wedged"]
    assert background_jobs.get("wedged", reconcile=False)["terminal_reason"] == "heartbeat_expired"


def test_owner_session_can_cancel_but_not_fail_a_fenced_job(monkeypatch):
    _parent()
    _parent("stranger")
    monkeypatch.setattr(background_jobs, "worker_is_alive", lambda _pid, _token: True)
    job = background_jobs.upsert(
        session="boss", job_id="fenced", kind="worker", title="Fenced",
        worker_pid=4242, worker_start_token="boot:worker")
    handle = background_jobs.job_handle(job)
    monkeypatch.setattr(background_jobs, "current_worker_identity", lambda: (0, ""))

    assert agent_bg.main(["agent_bg.py", "boss", "job-fail", handle, "gave up"]) == 1
    assert agent_bg.main(["agent_bg.py", "stranger", "job-cancel", handle]) == 1
    assert agent_bg.main(["agent_bg.py", "boss", "job-cancel", "bg1:9:fenced"]) == 1
    assert background_jobs.get("fenced", reconcile=False)["status"] == "running"

    assert agent_bg.main(["agent_bg.py", "boss", "job-cancel", handle]) == 0
    cancelled = background_jobs.get("fenced", reconcile=False)
    assert cancelled["status"] == "cancelled"
    assert cancelled["terminal_reason"] == "owner_cancelled"


# ---- progress ------------------------------------------------------------------

class _Stream:
    def __init__(self):
        self.events: list[dict] = []

    def broadcast(self, event: dict) -> None:
        self.events.append(event)


def test_progress_verb_stores_text_bumps_revision_and_emits_update():
    _parent()
    _parent("stranger")
    job = background_jobs.upsert(session="boss", job_id="build", kind="worker", title="Build")
    handle = background_jobs.job_handle(job)
    watcher = BackgroundJobWatcher(_Stream())
    watcher._last_id = background_jobs.latest_event_id()

    assert agent_bg.main(["agent_bg.py", "stranger", "job-progress", handle, "nope"]) == 1
    assert agent_bg.main(["agent_bg.py", "boss", "job-progress", handle, "  tests\n 3/10 "]) == 0

    updated = background_jobs.get("build", reconcile=False)
    assert updated["progress_text"] == "tests 3/10"
    assert updated["progress_at"] is not None
    assert updated["revision"] > job["revision"]
    watcher._poll_once()
    sent = [e for e in watcher.stream.events if e["type"] == "background-job-updated"]
    assert [e["job"]["progress_text"] for e in sent] == ["tests 3/10"]
    assert sent[0]["change_revision"] == updated["revision"]


def test_new_generation_clears_progress_and_log(tmp_path):
    _parent()
    job = background_jobs.upsert(session="boss", job_id="again", kind="worker", title="Again")
    background_jobs.set_progress("again", session="boss", generation=job["generation"], text="half")
    background_jobs.set_log("again", session="boss", generation=job["generation"],
                            path=str(tmp_path / "x.log"))
    background_jobs.finish("again", generation=job["generation"])

    rerun = background_jobs.upsert(session="boss", job_id="again", kind="worker", title="Again")

    assert rerun["generation"] == job["generation"] + 1
    assert rerun["progress_text"] == "" and rerun["log_path"] == ""


# ---- detail --------------------------------------------------------------------

def test_detail_has_timeline_progress_log_and_owner_links(tmp_path, monkeypatch):
    monkeypatch.setattr(background_jobs, "SHARED_LOG_ROOTS", ())
    boss = _parent(cwd=str(tmp_path))
    trace_id = "0123456789abcdef"
    turn_lifecycle.open_turn(agent_id=boss, source="pwa", trace_id=trace_id)
    job = background_jobs.upsert(session="boss", job_id="run", kind="worker", title="Run")
    generation = job["generation"]
    log = tmp_path / "run.log"
    log.write_text("starting\nworking\n")
    background_jobs.set_progress("run", session="boss", generation=generation, text="working")
    assert background_jobs.set_log("run", session="boss", generation=generation, path=str(log))
    background_jobs.cancel("run")

    detail = background_jobs.detail("run")

    assert detail["owner_session"] == "boss"
    assert detail["owner_agent_id"] == boss
    assert detail["started_trace_id"] == trace_id
    assert detail["helper_session"] == "" and detail["is_process"] is True
    assert detail["handle"] == f"bg1:{generation}:run"
    assert detail["progress"]["text"] == "working"
    assert [(e["change"], e["status"], e["note"]) for e in detail["timeline"]] == [
        ("status", "running", ""),
        ("progress", "running", "working"),
        ("status", "cancelled", ""),
    ]
    assert detail["log"] == {
        "path": str(log), "available": True, "reason": "", "text": "starting\nworking\n",
        "size": len("starting\nworking\n"), "truncated": False,
    }
    assert background_jobs.detail("missing") is None


def test_detail_links_the_helper_a_job_mirrors():
    boss = _parent()
    _helper(boss, "stream-a")
    background_jobs.upsert(
        session="boss", job_id="sub-agent-stream-a", kind="sub-agent",
        title="Stream A", detail="stream-a")

    detail = background_jobs.detail("sub-agent-stream-a")

    assert detail["helper_session"] == "stream-a"
    assert detail["is_process"] is False


def test_log_tail_is_the_last_64_kb_on_a_line_boundary(tmp_path):
    log = tmp_path / "big.log"
    lines = [f"line {i:06d}" for i in range(20_000)]
    log.write_text("\n".join(lines) + "\n")

    tail = background_jobs.read_log_tail(str(log), [str(tmp_path)])

    assert tail["available"] and tail["truncated"]
    assert len(tail["text"].encode()) <= background_jobs.LOG_TAIL_BYTES
    assert tail["text"].endswith("line 019999\n")
    assert tail["text"].split("\n", 1)[0] in lines


def test_log_tail_refuses_paths_outside_the_allowed_roots(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    link = root / "escape.log"
    link.symlink_to(outside)
    sub = root / "dir.log"
    sub.mkdir()
    inner = root / "ok.log"
    inner.write_text("fine\n")
    inner_link = root / "alias.log"
    inner_link.symlink_to(inner)
    roots = [str(root)]

    assert background_jobs.read_log_tail(str(outside), roots)["reason"] == "outside_allowed_roots"
    assert background_jobs.read_log_tail(str(link), roots)["reason"] == "outside_allowed_roots"
    assert background_jobs.read_log_tail(
        str(root / ".." / "secret.txt"), roots)["reason"] == "outside_allowed_roots"
    assert background_jobs.read_log_tail(str(sub), roots)["reason"] == "not_a_regular_file"
    assert background_jobs.read_log_tail(str(root / "gone.log"), roots)["reason"] == "missing"
    assert background_jobs.read_log_tail("relative.log", roots)["reason"] == "not_absolute"
    assert background_jobs.read_log_tail("", roots)["reason"] == "no_log"
    assert background_jobs.read_log_tail(str(inner_link), roots)["text"] == "fine\n"
    assert "secret" not in str(background_jobs.read_log_tail(str(link), roots))


def test_log_roots_are_worker_cwd_owner_cwd_and_var_tmp_never_slash(tmp_path):
    job = {"worker_cwd": str(tmp_path / "wt")}

    assert background_jobs.log_roots(job, owner_cwd=str(tmp_path)) == [
        os.path.realpath(tmp_path / "wt"), os.path.realpath(tmp_path),
        os.path.realpath("/var/tmp")]
    assert background_jobs.log_roots({"worker_cwd": "/"}, owner_cwd="/") == [
        os.path.realpath("/var/tmp")]


def test_log_verb_requires_an_absolute_path_and_records_the_worker_cwd(tmp_path, monkeypatch):
    _parent()
    job = background_jobs.upsert(session="boss", job_id="logged", kind="worker", title="Logged")
    handle = background_jobs.job_handle(job)
    monkeypatch.chdir(tmp_path)

    assert agent_bg.main(["agent_bg.py", "boss", "job-log", handle, "rel.log"]) == 2
    assert agent_bg.main(["agent_bg.py", "boss", "job-log", handle, str(tmp_path / "w.log")]) == 0

    stored = background_jobs.get("logged", reconcile=False)
    assert stored["log_path"] == str(tmp_path / "w.log")
    assert stored["worker_cwd"] == str(tmp_path)
    with pytest.raises(ValueError):
        background_jobs.set_log("logged", session="boss", generation=job["generation"],
                                path="x.log")
