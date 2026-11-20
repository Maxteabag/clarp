"""A launch a backend refused before anything was sent is known-unlaunched (R121).

Codex refuses a turn while the agent's thread is still held by another process
(CodexSessionStuck), inside spawn_turn and before turn/start. The dispatcher
marked every exception out of spawn_turn as "backend invoked", so the queued row
read launch 'unknown': never run again, never ended, and its receipt logged as
deferred again on every recovery pass.

(a) A LaunchRefused out of spawn_turn is a pre-invoke failure: the row reads
'unstarted' and recovery runs it again with its backoff. (b) After PARK_AFTER
identical refusals the row ends 'failed' and its sender is told, instead of
parking for good. (c) queueReceiptDeferred is written once per receipt and
reason, not once per pass."""
from __future__ import annotations

import pytest

from lib import agents as agents_db
from lib import db, recovery_backoff, turn_queue
from lib.backend.base import CliMissing, LaunchRefused
from lib.codex_app_server import CodexSessionStuck
from tests.unit.test_queued_recovery_backoff import (  # noqa: F401 - the harness and its fixture
    _Backends, _local_dispatch_runtime, _run_until)

STUCK = "Waiting for the previous Codex session to release this conversation"


class _Refusing(_Backends):
    def __init__(self):
        super().__init__()
        self.refused = []

    def spawn_turn(self, backend, **kwargs):
        if kwargs["agent_id"] in self.broken:
            self.refused.append(kwargs["trace_id"])
            raise CodexSessionStuck(STUCK)
        return super().spawn_turn(backend, **kwargs)


def _refusing_service(tmp_path, monkeypatch):
    import itertools
    from types import SimpleNamespace
    from lib.turn_dispatch import TurnDispatchService
    from tests.unit.test_queued_recovery_backoff import _Clock
    backends, clock, timers, ids = _Refusing(), _Clock(), [], itertools.count(1)
    ctx = SimpleNamespace(default_session="miso", agents_path=tmp_path / "unused.json",
                          stream=SimpleNamespace(broadcast=lambda _e: None))
    service = TurnDispatchService(
        ctx, backend_registry=backends, home=tmp_path,
        uuid_factory=lambda: f"backend-session-{next(ids)}", now=clock,
        retry_scheduler=lambda delay, fn: timers.append((clock.t + delay, fn)))
    agent_id = agents_db.create_agent(persona="Miso", voice_id="V", cwd=str(tmp_path),
                                      session="miso", backend="claude")
    agents_db.start_runtime(agent_id, "miso")
    backends.broken.add(agent_id)
    return service, backends, clock, timers, agent_id


def _send(service, trace, *, origin="agent", sender="", client=""):
    try:
        service.dispatch(text=f"message {trace}", requested_session="miso", forced_session="miso",
                         trace_id=trace, client_msg_id=client or f"q-{trace}", synthesize_audio=False,
                         queue_if_busy=True, origin=origin, sender_agent_id=sender)
    except Exception:  # noqa: BLE001 - the refusal is the dispatch's answer
        pass


def _row(trace):
    return db.conn().execute("SELECT * FROM queued_turns WHERE trace_id=?", (trace,)).fetchone()


def test_the_types_keep_what_their_handlers_catch():
    assert issubclass(CodexSessionStuck, LaunchRefused) and issubclass(CodexSessionStuck, RuntimeError)
    assert issubclass(CliMissing, LaunchRefused) and issubclass(CliMissing, FileNotFoundError)
    from lib.backend.agy import AgyLaunchInvalid, AgyOwnershipLost
    assert issubclass(AgyLaunchInvalid, ValueError) and issubclass(AgyOwnershipLost, RuntimeError)


def test_an_agy_command_that_cannot_be_built_is_a_refusal():
    from lib.backend.agy import AgyBackend, AgyLaunchInvalid
    with pytest.raises(AgyLaunchInvalid):
        AgyBackend().build_cmd("", model="x", effort="high")


def test_a_refused_launch_is_retryable_and_is_run_again(tmp_path, monkeypatch):
    service, backends, clock, timers, agent_id = _refusing_service(tmp_path, monkeypatch)
    _send(service, "t1")
    row = _row("t1")
    assert row is not None and row["status"] == "queued"
    assert turn_queue._launch_status(row) == "retryable"           # main: 'unknown', never run again
    assert [r["trace_id"] for r in turn_queue.pending_for_dispatch() if r["agent_id"] == agent_id] == ["t1"]
    _run_until(clock, timers, clock.t + 5)
    assert backends.refused.count("t1") >= 2                        # recovery tried it again


def test_the_eighth_identical_refusal_ends_the_row_and_tells_the_sender(tmp_path, monkeypatch):
    service, backends, clock, timers, agent_id = _refusing_service(tmp_path, monkeypatch)
    _send(service, "t1", origin="user")
    _send(service, "t2", origin="user")
    queue_id = str(_row("t1")["queue_id"])

    _run_until(clock, timers, clock.t + 3600)

    # The send's own attempt, then PARK_AFTER by recovery.
    assert backends.refused.count("t1") == 1 + recovery_backoff.PARK_AFTER
    assert _row("t1")["status"] == "failed"
    notice = db.conn().execute("SELECT role, origin, text FROM messages WHERE message_id=?",
                               (f"notice:{turn_queue.REFUSED_LAUNCH}:{queue_id}",)).fetchone()
    assert notice is not None and notice["role"] == "assistant" and notice["origin"] == "system"
    assert notice["text"].startswith("A message you sent was never run.\n\n")
    assert STUCK in notice["text"] and f"{recovery_backoff.PARK_AFTER} times" in notice["text"]
    # The next row is the head now and goes the same way, on its own count.
    assert backends.refused.count("t2") == 1 + recovery_backoff.PARK_AFTER
    assert _row("t2")["status"] == "failed"


def test_a_different_error_is_not_a_refusal_and_still_parks(tmp_path, monkeypatch):
    """Only a LaunchRefused ends a row. A failure before the backend that is not one (here the
    launch's preparation, as in test_queued_recovery_backoff) parks after PARK_AFTER as before:
    the row stays queued."""
    from lib import backends as registry
    from lib.turn_dispatch import DispatchError
    service, backends, clock, timers, agent_id = _refusing_service(tmp_path, monkeypatch)
    backends.broken.discard(agent_id)
    attempts = []
    runner = registry.by_id("claude")

    def prepare(*args, **kwargs):
        attempts.append(clock.t)
        raise DispatchError(500, "preparation: failed to read thread 019a-x")
    monkeypatch.setattr(runner, "arm_source_marker", prepare)
    _send(service, "t1")
    _run_until(clock, timers, clock.t + 3600)
    assert len(attempts) == 1 + recovery_backoff.PARK_AFTER
    assert _row("t1")["status"] == "queued"


def test_a_deferred_receipt_is_logged_once_per_reason(capsys):
    told = []
    for reason in ("unknown", "unknown", "unknown", "unstarted_open"):
        if turn_queue._deferred_is_news("q-1", 7, reason):
            told.append(reason)
    assert told == ["unknown", "unstarted_open"]
    assert turn_queue._deferred_is_news("q-1", 8, "unknown")       # a new row of the same id
    turn_queue._forget_deferred(["q-1"])
    assert turn_queue._deferred_is_news("q-1", 7, "unstarted_open")


def test_a_pass_writes_the_deferred_line_once(capsys):
    from tests.unit.test_receipt_pass_open_turns import OPEN_TWO_ROWS, _receipt, _turn
    _receipt("waiting", status="queued", trace="t2")
    _turn(OPEN_TWO_ROWS, trace="t2")
    capsys.readouterr()
    told = []
    for _ in range(3):
        turn_queue.reconcile_receipts(on_deferred=lambda queue_id, _seq, reason: told.append(queue_id))
    lines = [line for line in capsys.readouterr().err.splitlines() if "queueReceiptDeferred" in line]
    # The caller hears of it on every pass that examines it; the journal once.
    assert len(told) >= 2 and set(told) == {"waiting"} and len(lines) == 1



class _Alternating(_Refusing):
    """Refusals of two kinds in turn (a held thread, a missing command), so no eight are identical;
    from ``clears_at`` on (the n-th refusal of a trace) the launch goes through."""
    def __init__(self, clears_at=None):
        super().__init__()
        self.clears_at = clears_at
        self.launched = []

    def spawn_turn(self, backend, **kwargs):
        trace = kwargs["trace_id"]
        if kwargs["agent_id"] in self.broken and (
                self.clears_at is None or self.refused.count(trace) + 1 < self.clears_at):
            self.refused.append(trace)
            if self.refused.count(trace) % 2:
                raise CodexSessionStuck(STUCK)
            raise CliMissing("codex: command not found")
        self.launched.append(trace)
        return _Backends.spawn_turn(self, backend, **kwargs)


def _alternating_service(tmp_path, monkeypatch, clears_at=None):
    service, _backends, clock, timers, agent_id = _refusing_service(tmp_path, monkeypatch)
    backends = _Alternating(clears_at)
    backends.broken.add(agent_id)
    service.backends = backends
    return service, backends, clock, timers, agent_id


def _notices(queue_id):
    return db.conn().execute("SELECT COUNT(*) FROM messages WHERE message_id=?",
                             (f"notice:{turn_queue.REFUSED_LAUNCH}:{queue_id}",)).fetchone()[0]


def test_refusals_that_alternate_end_the_row_at_the_twentieth_with_one_notice(tmp_path, monkeypatch):
    """Two kinds in turn never make eight identical; the second bound ends the row (R121)."""
    service, backends, clock, timers, agent_id = _alternating_service(tmp_path, monkeypatch)
    _send(service, "t1", origin="user")
    queue_id = str(_row("t1")["queue_id"])
    _run_until(clock, timers, clock.t + 6 * 3600)
    # The send's own attempt, then REFUSED_IN_ALL by recovery.
    assert backends.refused.count("t1") == 1 + recovery_backoff.REFUSED_IN_ALL
    assert _row("t1")["status"] == "failed" and _notices(queue_id) == 1


def test_a_refusal_that_clears_at_the_nineteenth_runs_once(tmp_path, monkeypatch):
    service, backends, clock, timers, agent_id = _alternating_service(
        tmp_path, monkeypatch, clears_at=recovery_backoff.REFUSED_IN_ALL)   # 1 + 18 refused, then it runs
    _send(service, "t1", origin="user")
    queue_id = str(_row("t1")["queue_id"])
    _run_until(clock, timers, clock.t + 6 * 3600)
    assert backends.refused.count("t1") == recovery_backoff.REFUSED_IN_ALL - 1
    assert backends.launched == ["t1"] and _notices(queue_id) == 0
    assert _row("t1")["status"] != "failed"


def test_the_count_is_the_rows_own(tmp_path, monkeypatch):
    """The next row of the agent starts at nought."""
    service, backends, clock, timers, agent_id = _alternating_service(tmp_path, monkeypatch)
    _send(service, "t1", origin="user")
    _send(service, "t2", origin="user")
    _run_until(clock, timers, clock.t + 12 * 3600)
    assert backends.refused.count("t1") == 1 + recovery_backoff.REFUSED_IN_ALL
    assert backends.refused.count("t2") == 1 + recovery_backoff.REFUSED_IN_ALL
    assert _row("t1")["status"] == _row("t2")["status"] == "failed"


def test_a_restart_starts_the_count_again(tmp_path, monkeypatch):
    """What is true: the counts are kept in the dispatcher's memory, not with the row, so a Host
    restart between refusals starts them at nought. The row still ends, REFUSED_IN_ALL refusals
    after the restart."""
    from lib.turn_dispatch_state import _RECOVERY_BACKOFF
    service, backends, clock, timers, agent_id = _alternating_service(tmp_path, monkeypatch)
    _send(service, "t1", origin="user")
    while backends.refused.count("t1") < 1 + 10:
        when, fn = min(timers, key=lambda timer: timer[0])
        timers.remove((when, fn))
        clock.t = max(clock.t, when)
        fn()
    assert _row("t1")["status"] == "queued"
    _RECOVERY_BACKOFF.reset_for_tests()                                  # what a restart loses
    _run_until(clock, timers, clock.t + 6 * 3600)
    assert backends.refused.count("t1") == 1 + 10 + recovery_backoff.REFUSED_IN_ALL
    assert _row("t1")["status"] == "failed"
