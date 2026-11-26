"""Restart lifecycle: what a `systemctl --user restart clarp` takes with it.

The user report behind this file: "server restarts are killing agents'
background tasks". Issue #11 fixed the *turn* half of that (a turn killed by a
restart now leaves an INTERRUPTED state and a marker row,
server/lib/interrupted_turns.py). The worker half is still open.

`systemd/clarp.service` sets no `KillMode`, so it gets systemd's default,
`control-group`. On restart every process in the unit's cgroup is SIGTERM'd.
Detached workers in this codebase are launched with `start_new_session=True`,
which is a new *session and process group*, not a new *cgroup* -- so they are
inside the sweep. On macOS the same flag does work, because launchd's default
`AbandonProcessGroup=false` kills only the process group, which a new session
escapes. The behaviour therefore differs by platform, which is why nobody
noticed.

Convention (docs/hardening/README.md): a confirmed, unfixed bug is a strict
xfail asserting the *desired* behaviour; a confirmed axiom is a passing test.
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import subprocess
import sys
import time

import pytest

from lib import agents, background_jobs, interrupted_turns, origins
from lib import scheduler, server_update, service_manager, turn_queue
from lib.db import conn
from lib.protocol import AgentState

from . import restart_cgroup as cg


pytestmark = pytest.mark.timeout(60)

linux_only = pytest.mark.skipif(
    sys.platform != "linux" or not pathlib.Path("/proc/self/cgroup").exists(),
    reason="control-group kill emulation needs Linux cgroup v2 under /proc",
)

WATCH_MESSAGES = (pathlib.Path(__file__).resolve().parents[2]
                  / "skills/clarp-message-watch/scripts/watch_messages.py")


@pytest.fixture
def marker():
    token = cg.new_marker()
    yield token
    cg.reap(token)


@pytest.fixture
def worker_script(tmp_path):
    return cg.write_worker(tmp_path)


def _agent(session: str = "nadia-restart") -> dict:
    agents.create_agent(
        persona="Nadia", voice_id="voice", cwd="/tmp", session=session)
    return agents.get_by_session(session)


def _load_watch_messages():
    spec = importlib.util.spec_from_file_location(
        "restart_watch_messages", WATCH_MESSAGES)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------
# 1. Reproduction: the control-group kill
# --------------------------------------------------------------------------

@linux_only
def test_start_new_session_worker_shares_the_server_cgroup_and_dies_with_it(
    worker_script, marker, tmp_path,
):
    """Documentation of current behaviour, and the reproduction of the report.

    Every detached worker in this codebase is launched with
    `start_new_session=True` and nothing else -- server/lib/server_update.py:263,
    server/lib/service_manager.py:261, server/lib/transcription_models.py:529,
    server/lib/portrait_generation.py:131, scripts/server_update_job.py:140.
    That escapes the *process group*, which is what a terminal or launchd
    would kill, and nothing else. The cgroup is inherited, so systemd's
    default `KillMode=control-group` reaches it on every restart.
    """
    ready = tmp_path / "ready-session"
    process = cg.spawn_same_session_worker(worker_script, marker, ready)
    worker = cg.wait_for_ready(ready)

    # It really did detach the only way the codebase detaches.
    assert os.getpgid(worker) != os.getpgid(0)
    # And it is still in the same control group as the process that spawned it.
    assert cg.cgroup_of(worker) == cg.own_cgroup()

    signalled = cg.control_group_kill(marker)

    assert signalled == [worker]
    assert cg.wait_gone(worker), "worker survived the control-group kill"
    process.wait(timeout=5)


@linux_only
def test_transient_scope_worker_survives_the_same_control_group_kill(
    worker_script, marker, tmp_path,
):
    """Counterfactual: a new cgroup, not a new session, is what saves a worker.

    Same worker, same sweep, one difference -- `systemd-run --user --scope`
    puts it in a transient unit under `user@.service`, a sibling of
    `clarp.service` rather than a member of it.
    """
    if not cg.systemd_run_available():
        pytest.skip("systemd-run --user is unavailable in this environment")
    ready = tmp_path / "ready-scope"
    cg.spawn_scope_worker(worker_script, marker, ready)
    worker = cg.wait_for_ready(ready)

    assert cg.cgroup_of(worker) != cg.own_cgroup()
    assert ".scope" in cg.cgroup_of(worker)

    assert cg.control_group_kill(marker) == []
    time.sleep(0.3)
    assert cg.is_alive(worker), "scope worker should be out of the sweep"


@linux_only
def test_spawn_detached_worker_places_a_worker_outside_the_restart_sweep(
    worker_script, marker, tmp_path,
):
    """The fix: `background_jobs.spawn_detached_worker` prefers a transient unit.

    This is the test that was a strict xfail before the prototype landed. It
    exercises the real preference logic in
    `background_jobs.spawn_detached_worker`; only the systemd-run invocation
    itself is substituted (`restart_cgroup.scope_launcher`), because the shared
    conftest fixture forbids tests from driving the live service manager.
    """
    if not cg.systemd_run_available():
        pytest.skip("systemd-run --user is unavailable in this environment")
    ready = tmp_path / "ready-detached"
    launches: list[tuple[list[str], str]] = []

    ok, mechanism, error = background_jobs.spawn_detached_worker(
        [sys.executable, str(worker_script), marker, str(ready)],
        unit="clarp-hardening-probe",
        launcher=cg.scope_launcher(launches),
    )

    assert (ok, mechanism, error) == (True, background_jobs.DETACH_TRANSIENT_UNIT, "")
    assert launches[0][1] == "clarp-hardening-probe"
    worker = cg.wait_for_ready(ready)
    assert cg.cgroup_of(worker) != cg.own_cgroup()

    assert cg.control_group_kill(marker) == []
    time.sleep(0.3)
    assert cg.is_alive(worker), "detached worker must survive a restart"


def test_spawn_detached_worker_falls_back_to_a_new_session_and_says_so():
    """No usable systemd (macOS, container, no user D-Bus) still gets a worker.

    `service_manager.launch_detached` returns `(False, error)` on Linux when
    systemd-run fails and never falls back (server/lib/service_manager.py:230),
    so the fallback lives in the shared launcher instead. The mechanism is
    returned rather than swallowed so the caller can warn: on Linux
    `new_session` means the worker is still inside the server's cgroup.
    """
    spawned: list[dict] = []

    def failing_launcher(command, *, unit, environment=None):
        return False, "Failed to connect to bus"

    def fake_popen(command, **kwargs):
        spawned.append({"command": command, **kwargs})
        return object()

    ok, mechanism, error = background_jobs.spawn_detached_worker(
        ["/bin/true"], unit="clarp-fallback",
        launcher=failing_launcher, popen=fake_popen)

    assert ok is True
    assert mechanism == background_jobs.DETACH_NEW_SESSION
    assert error == "Failed to connect to bus"
    assert spawned[0]["start_new_session"] is True


def test_spawn_detached_worker_falls_back_when_the_launcher_raises():
    """A launcher that raises must not lose the worker entirely."""
    spawned = []

    def exploding_launcher(command, *, unit, environment=None):
        raise FileNotFoundError("systemd-run")

    ok, mechanism, error = background_jobs.spawn_detached_worker(
        ["/bin/true"], unit="clarp-fallback",
        launcher=exploding_launcher,
        popen=lambda command, **kwargs: spawned.append(command))

    assert (ok, mechanism) == (True, background_jobs.DETACH_NEW_SESSION)
    assert "FileNotFoundError" in error
    assert spawned == [["/bin/true"]]


def test_spawn_detached_worker_defaults_to_the_transient_unit_helper(monkeypatch):
    """The default launcher is the one that already knows about systemd/launchd."""
    seen = {}

    def fake_launch_detached(command, *, unit, environment=None):
        seen.update(command=command, unit=unit, environment=environment)
        return True, ""

    monkeypatch.setattr(
        service_manager, "launch_detached", fake_launch_detached)

    ok, mechanism, _ = background_jobs.spawn_detached_worker(
        ["/bin/true", "--flag"], unit="clarp-default",
        environment={"CLARP_X": "1"})

    assert (ok, mechanism) == (True, background_jobs.DETACH_TRANSIENT_UNIT)
    assert seen == {
        "command": ["/bin/true", "--flag"], "unit": "clarp-default",
        "environment": {"CLARP_X": "1"},
    }


def test_message_watch_detach_relaunches_through_the_shared_launcher(monkeypatch):
    """The message-watch worker is the first adopted worker, and it was exposed.

    `background_jobs.current_worker_identity` adopts `watch_messages.py`
    specially (server/lib/background_jobs.py:120), but the watcher is started
    from an agent's own shell, so it inherited the server's cgroup and died on
    every restart. `--detach` relaunches it through the shared launcher.
    """
    module = _load_watch_messages()
    calls = {}

    class FakeJobs:
        DETACH_TRANSIENT_UNIT = "transient_unit"

        @staticmethod
        def spawn_detached_worker(command, *, unit, environment=None):
            calls.update(command=command, unit=unit)
            return True, "transient_unit", ""

    monkeypatch.setattr(module, "_load_background_jobs", lambda: FakeJobs)

    code = module.detach_self(
        ["whatsapp", "--detach", "--session", "nadia", "--watch", "4712=Ann"])

    assert code == 0
    assert "--detach" not in calls["command"]
    assert calls["command"][1].endswith("watch_messages.py")
    assert calls["command"][2:] == [
        "whatsapp", "--session", "nadia", "--watch", "4712=Ann"]
    assert calls["unit"].startswith("clarp-message-watch-")


# --------------------------------------------------------------------------
# 2. The update job, which restarts the server it was started by
# --------------------------------------------------------------------------

def test_update_worker_is_launched_into_its_own_transient_unit(
    monkeypatch, tmp_path,
):
    """Questioned assumption: "the updater is inside the cgroup it restarts".

    It is not, on Linux. `server_update.request_update` already shells out to
    `systemd-run --user --collect --unit=clarp-update`
    (server/lib/server_update.py:249-253), so the process that runs
    `systemctl --user restart clarp` lives in a sibling unit and survives its
    own restart. This is the precedent the other detached launchers should
    have followed, and the reason a broken update never showed up as "the
    update died halfway".
    """
    _agent("mike")
    monkeypatch.setattr(server_update, "get_update_status", lambda: {
        "update_method": "managed", "update_command": "clarp-admin update",
        "update_available": True, "latest_version": "next",
    })
    monkeypatch.setattr(service_manager, "platform_kind", lambda: "linux")
    monkeypatch.setattr(
        server_update, "_worker_script", lambda: tmp_path / "worker.py")
    launched = []

    class Result:
        returncode = 0
        stderr = ""

    def fake_run(command, **_kwargs):
        if command[0] == "systemctl":
            # `is-active clarp-update.service`: no update is running yet.
            return type("Probe", (), {"returncode": 1, "stderr": ""})()
        launched.append(command)
        return Result()

    monkeypatch.setattr(server_update.subprocess, "run", fake_run)

    code, result = server_update.request_update("mike")

    assert code == 202 and result["status"] == "queued"
    assert launched[0][:2] == ["systemd-run", "--user"]
    assert "--unit=clarp-update" in launched[0]


def test_update_worker_on_macos_escapes_the_process_group_kill(
    monkeypatch, tmp_path,
):
    """The macOS fallback is `start_new_session`, and there that is enough.

    launchd's default `AbandonProcessGroup=false` kills the job's process
    group. A new session is a new process group, so the update worker escapes.
    Same flag, same code, opposite outcome from Linux -- which is exactly why
    the Linux gap stayed invisible.
    """
    _agent("mike")
    monkeypatch.setattr(server_update, "get_update_status", lambda: {
        "update_method": "managed", "update_command": "clarp-admin update",
        "update_available": True, "latest_version": "next",
    })
    monkeypatch.setattr(service_manager, "platform_kind", lambda: "macos")
    monkeypatch.setattr(
        server_update, "_worker_script", lambda: tmp_path / "worker.py")
    monkeypatch.setenv("CLAUDE_PWA_LOG_DIR", str(tmp_path / "logs"))
    calls = []
    monkeypatch.setattr(
        server_update.subprocess, "Popen",
        lambda command, **kwargs: calls.append((command, kwargs)))

    code, _ = server_update.request_update("mike")

    assert code == 202
    assert calls[0][1]["start_new_session"] is True


# --------------------------------------------------------------------------
# 3. What the job registry knows about a worker that died in the restart
# --------------------------------------------------------------------------

def _dead_worker_job(job_id: str = "watch-ann") -> dict:
    """A registered job whose worker no longer exists (the restart killed it)."""
    _agent()
    return background_jobs.upsert(
        session="nadia-restart", job_id=job_id, kind="whatsapp",
        title="WhatsApp: Ann", worker_pid=999_999,
        worker_start_token="boot-id:before-the-restart")


def test_job_whose_worker_died_in_the_restart_stays_running_for_ten_minutes():
    """Documentation of current behaviour: the ten-minute lie.

    `reconcile_stale` only *looks* at a row once the heartbeat clock has run
    out (server/lib/background_jobs.py:696-704: the SQL filters on
    `(now - heartbeat) > heartbeat_timeout_ms` before any liveness probe). The
    default timeout is ten minutes, so for ten minutes after a restart the PWA
    shows a watcher as `running` with `worker_freshness: fresh`, even though
    the process is provably gone.
    """
    assert background_jobs.DEFAULT_HEARTBEAT_TIMEOUT_MS == 600_000
    job = _dead_worker_job()
    nine_minutes_later = job["updated_at"] + 9 * 60_000

    changed = background_jobs.reconcile_stale(
        now_ms=nine_minutes_later, process_probe=lambda pid, token: False)

    assert changed == []
    observed = background_jobs.get(
        job["job_id"], reconcile=False, observed_at=nine_minutes_later)
    assert observed["status"] == "running"
    assert observed["worker_freshness"] == "fresh"
    assert observed["outcome_state"] == "pending"


@pytest.mark.xfail(strict=True, reason=(
    "BUG: a job is not reconciled against its worker's start token until the "
    "heartbeat timeout expires, so a worker killed by a restart is reported "
    "as running for ten minutes. Evidence: "
    "server/lib/background_jobs.py:696-704 filters on the heartbeat clock "
    "before probing liveness, and server/server.py:4714-4722 runs "
    "interrupted_turns/heartbeat restart recovery at boot but never touches "
    "background_jobs at all. Fix: run a boot pass that probes "
    "(worker_pid, worker_start_token) for every active job regardless of the "
    "heartbeat clock -- process_start_token already embeds the boot id, so a "
    "dead worker is unambiguous -- and call it from build_server next to "
    "interrupted_turns.recover_after_restart."))
def test_boot_reconciles_a_job_whose_worker_start_token_no_longer_matches():
    job = _dead_worker_job()
    one_second_later = job["updated_at"] + 1_000

    changed = background_jobs.reconcile_stale(
        now_ms=one_second_later, process_probe=lambda pid, token: False)

    assert changed == [job["job_id"]]
    assert background_jobs.get(
        job["job_id"], reconcile=False)["status"] == "failed"


@pytest.mark.xfail(strict=True, reason=(
    "BUG: the terminal reason for a worker the server itself killed is "
    "'worker_vanished', the same reason a worker that segfaulted gets. This "
    "is issue #11's shape one layer down: a failure is detected and "
    "attributed to the wrong cause. Evidence: "
    "server/lib/background_jobs.py:712-715 picks between 'heartbeat_expired' "
    "and 'worker_vanished' and has no concept of a restart, while "
    "server/lib/interrupted_turns.py:22 already has RESTART_SOURCE for the "
    "turn half. Fix: record the server's boot id (or the worker's cgroup/unit) "
    "with the job at registration; when the recorded boot id differs from the "
    "current one and the worker is gone, fail it with a distinct reason such "
    "as 'killed_by_server_restart' so the PWA can say so."))
def test_restart_killed_job_says_a_restart_killed_it():
    job = _dead_worker_job()
    eleven_minutes_later = job["updated_at"] + 11 * 60_000

    background_jobs.reconcile_stale(
        now_ms=eleven_minutes_later, process_probe=lambda pid, token: False)

    observed = background_jobs.get(job["job_id"], reconcile=False)
    assert observed["status"] == "failed"
    assert "restart" in observed["terminal_reason"]


def test_a_lost_worker_reports_an_unknown_outcome_not_a_failed_one():
    """Axiom worth keeping: 'we stopped hearing from it' is not 'it failed'.

    server/lib/background_jobs.py:864-873 keeps `status=failed` as the durable
    worker-lifecycle state but reports `outcome_state=unknown`, so a restart
    that kills a watcher can never be rendered as "the watch failed".
    """
    job = _dead_worker_job()
    later = job["updated_at"] + 11 * 60_000

    background_jobs.reconcile_stale(
        now_ms=later, process_probe=lambda pid, token: False)

    observed = background_jobs.get(job["job_id"], reconcile=False)
    assert observed["terminal_reason"] == "worker_vanished"
    assert observed["status"] == "failed"
    assert observed["outcome_state"] == "unknown"
    assert observed["worker_freshness"] == "stale"


def test_restarting_a_job_with_a_new_worker_opens_a_new_generation():
    """Axiom: a relaunched worker after a restart cannot be fenced out.

    A worker that comes back with a different start token gets a fresh
    generation (server/lib/background_jobs.py:373-381), so the stale
    generation's late `finish` can never close the new one.
    """
    job = _dead_worker_job()

    revived = background_jobs.restart(
        session="nadia-restart", job_id=job["job_id"], kind="whatsapp",
        title="WhatsApp: Ann", worker_pid=999_998,
        worker_start_token="boot-id:after-the-restart")

    assert revived["generation"] == job["generation"] + 1
    assert revived["status"] == "running"
    assert background_jobs.finish(
        job["job_id"], generation=job["generation"], worker_pid=999_999,
        worker_start_token="boot-id:before-the-restart") is None


# --------------------------------------------------------------------------
# 4. Turns that were queued or in flight across the restart
# --------------------------------------------------------------------------

def test_queued_turns_survive_a_restart_as_durable_rows():
    """Axiom: the explicit queue is a table, so a restart cannot drop it.

    `TurnDispatchService.recover_queued` re-admits it at boot
    (server/lib/turn_dispatch.py:244-288, called from server/server.py:4723).
    """
    agent = _agent()
    assert turn_queue.enqueue(
        queue_id="q-1", agent_id=agent["agent_id"], session=agent["session"],
        text="ping", trace_id="t-1", client_msg_id="c-1",
        synthesize_audio=False, origin="user", sender_agent_id="")

    # Restart: the process is gone, the row is not.
    assert [row["queue_id"] for row in turn_queue.pending()] == ["q-1"]


def test_a_turn_claimed_when_the_server_died_returns_to_the_queue():
    """Axiom: an in-flight manual send is not lost, only delayed.

    A claim is a lease, and boot recovery releases abandoned leases first
    (`turn_queue.reset_stale_claims`, server/lib/turn_dispatch.py:254).
    """
    agent = _agent()
    turn_queue.enqueue(
        queue_id="q-1", agent_id=agent["agent_id"], session=agent["session"],
        text="ping", trace_id="t-1", client_msg_id="c-1",
        synthesize_audio=False, origin="user", sender_agent_id="")
    assert turn_queue.claim("q-1") is not None
    assert turn_queue.pending() == []

    assert turn_queue.reset_stale_claims(max_age_ms=0) == 1
    assert [row["queue_id"] for row in turn_queue.pending()] == ["q-1"]


def test_a_turn_in_flight_at_the_restart_is_marked_interrupted():
    """Axiom (issue #11's fix): a busy state at boot becomes INTERRUPTED."""
    agent = _agent()
    agents.start_runtime(agent["agent_id"], agent["session"])
    agents.record_state(
        agent["agent_id"], AgentState.THINKING, {"trace_id": "t-9"})

    recovered = interrupted_turns.recover_after_restart()

    assert [row["agent_id"] for row in recovered] == [agent["agent_id"]]
    latest = agents.latest_state(agent["agent_id"])
    assert latest["kind"] == AgentState.INTERRUPTED
    assert latest["detail"]["source"] == interrupted_turns.RESTART_SOURCE


def test_a_scheduled_turn_killed_by_a_restart_is_not_silently_swallowed():
    """Axiom: the scheduler's origin is user-visible, unlike heartbeat/dreaming.

    `interrupted_turns._SILENT_ORIGINS` suppresses the transcript marker for
    routine automation only. The scheduler dispatches with `origin="automation"`
    (server/server.py:4703), which is outside that set, so a scheduled run
    killed by a restart still leaves a marker the user can see.
    """
    assert "automation" not in interrupted_turns._SILENT_ORIGINS
    assert "schedule" not in interrupted_turns._SILENT_ORIGINS
    assert not origins.is_routine_automation("automation")
    assert interrupted_turns._SILENT_ORIGINS == frozenset(
        {"heartbeat", "leader_tick", "dreaming", "watcher"})


# --------------------------------------------------------------------------
# 5. Scheduled jobs across the downtime (PR #24)
# --------------------------------------------------------------------------

def _schedule(cron: str = "@hourly", *, due_ms_ago: int) -> dict:
    agent = _agent()
    item = scheduler.create_schedule(
        agent["session"], "Nightly sweep", cron, "run the sweep")
    with conn() as c:
        c.execute(
            "UPDATE agent_schedules SET next_run_at = ? WHERE schedule_id = ?",
            (scheduler.now_ms() - due_ms_ago, item["schedule_id"]))
    return scheduler.get_schedule(item["schedule_id"])


def test_a_schedule_that_came_due_during_the_downtime_runs_once_after_boot():
    """Axiom: the due check is `next_run_at <= now`, so downtime is caught up.

    server/lib/scheduler.py:274-286. The run is late, not lost.
    """
    item = _schedule(due_ms_ago=5 * 60_000)
    dispatched = []
    runner = scheduler.AgentScheduleRunner(
        dispatch_turn=lambda session, text: dispatched.append((session, text)))

    assert runner.tick() == 1
    assert dispatched == [(item["session"], "run the sweep")]
    assert runner.tick() == 0
    assert scheduler.get_schedule(item["schedule_id"])["next_run_at"] > scheduler.now_ms()


def test_five_missed_hourly_runs_collapse_into_a_single_run():
    """Documentation: long downtime does not produce a burst of catch-up turns.

    `advance_schedule` recomputes the next run from *now*, not from the missed
    slot (server/lib/scheduler.py:289-301), so an `@hourly` schedule that was
    down for five hours fires once. That is almost certainly the behaviour we
    want for an agent prompt; it is nowhere stated, and it silently makes
    `last_run_at` a poor audit trail.
    """
    item = _schedule("@hourly", due_ms_ago=5 * 60 * 60_000)
    dispatched = []
    runner = scheduler.AgentScheduleRunner(
        dispatch_turn=lambda session, text: dispatched.append(text))

    assert runner.tick() == 1
    assert runner.tick() == 0
    assert len(dispatched) == 1
    assert scheduler.get_schedule(item["schedule_id"])["last_run_at"] is not None


@pytest.mark.xfail(strict=True, reason=(
    "BUG: a scheduled run is marked as taken before it is dispatched, so a "
    "dispatch that never happens is indistinguishable from one that did, and "
    "the run is dropped with no retry and no user-visible trace. Evidence: "
    "server/lib/scheduler.py:352-358 calls advance_schedule(sid, now) and only "
    "then _dispatch_turn inside a try/except that logs and moves on. A server "
    "restart between the two writes has the same effect. Fix: give "
    "agent_schedules a claim (running_since / attempt), advance only after "
    "dispatch is accepted, and let boot re-offer a claimed-but-never-finished "
    "run -- the same lease shape turn_queue.claim/reset_stale_claims already "
    "uses (server/lib/turn_queue.py:135-172)."))
def test_a_scheduled_run_whose_dispatch_never_happened_is_offered_again():
    item = _schedule(due_ms_ago=60_000)

    def dispatch_that_dies_with_the_server(session, text):
        raise RuntimeError("server restarted mid-dispatch")

    runner = scheduler.AgentScheduleRunner(
        dispatch_turn=dispatch_that_dies_with_the_server)
    assert runner.tick() == 0

    still_due = [row["schedule_id"]
                 for row in scheduler.due_schedules(scheduler.now_ms() + 1)]
    assert still_due == [item["schedule_id"]]


@pytest.mark.xfail(strict=True, reason=(
    "BUG: the schedule runner's first evaluation is one whole check interval "
    "after boot, so a restart always delays every already-due schedule by up "
    "to 15 s and a restart loop can starve them indefinitely. Evidence: "
    "server/lib/scheduler.py:341 -- `while not self._stop_event.wait(interval)` "
    "waits before the first tick, and server/server.py:4706 starts the runner "
    "with the default 15 s interval. Fix: tick once immediately on start(), "
    "the way the boot recovery passes beside it already do."))
def test_the_schedule_runner_evaluates_due_work_as_soon_as_it_starts():
    _schedule(due_ms_ago=60_000)
    dispatched = []
    runner = scheduler.AgentScheduleRunner(
        dispatch_turn=lambda session, text: dispatched.append(text),
        check_interval_sec=30.0)

    runner.start()
    try:
        deadline = time.monotonic() + 2.0
        while not dispatched and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        runner.stop()

    assert dispatched == ["run the sweep"]
