"""What a live server does to a turn that does not end the way it should.

Issue #11 pinned three shapes on the *restart* path: a failure detected by the
system but told to the wrong party, one classifier reason covering two distinct
events, and a busy state left behind when a child died without its terminal
callback. `server/lib/interrupted_turns.py` fixed the restart case. This file
asks the next question — what happens to those same three shapes while the
server is alive and nothing restarted at all.

Conventions (see `docs/hardening/README.md`): a confirmed, unfixed bug is a
strict xfail asserting the *desired* behaviour, carrying its evidence as
`file:line` and a proposed fix. Everything else is a passing test pinning an
axiom that other code already relies on.

Run: uv run --frozen pytest tests/regression/test_dispatch_states.py -q -n0
"""
from __future__ import annotations

import json

import pytest

from lib import agents as agents_db
from lib import codex_app_server, db, error_classify, message_store, origins
from lib import reconcile, user_notifications
from lib.protocol import AgentState
from tests.regression.dispatch_support import (
    FakeProc,
    Harness,
    reset_dispatcher_globals,
)


@pytest.fixture(autouse=True)
def _dispatcher_globals():
    """`_INFLIGHT`/`_QUEUED` are module-level and outlive one test."""
    reset_dispatcher_globals()
    yield
    reset_dispatcher_globals()


def _terminal_calls() -> tuple[list, object, object]:
    """(recorder, on_result, on_error) — what a runner's drain thread calls."""
    seen: list[tuple[str, object]] = []
    return (seen,
            lambda event: seen.append(("result", event)),
            lambda message: seen.append(("error", message)))


def _classify(harness: Harness, trace_id: str = "") -> dict:
    state = agents_db.latest_state(harness.agent_id) or {}
    return user_notifications.classify_completed_turn(
        agent_id=harness.agent_id, session=harness.session, persona="P",
        done_ts=int(state.get("ts") or db.now_ms()),
        backend_session_id=harness.backend_session_id, trace_id=trace_id,
        settle_timeout_s=0)


# =====================================================================
# Axioms — behaviour other code already depends on. These must stay green.
# =====================================================================

def test_axiom_a_classified_failure_ends_the_state_log_non_busy(tmp_path):
    """A recognised backend failure is terminal: the badge must not stay busy.

    This is the half of issue #11 that already works on the live path. The
    dispatcher's `_mark_interrupted` records INTERRUPTED, which is outside
    `AgentState.busy_states()`, so the agent stops looking like it is working.
    """
    harness = Harness(tmp_path)
    harness.send("yo", client_msg_id="c-1")

    harness.last_call()["on_error"]("clarp exited rc=1")

    assert harness.latest_kind() == AgentState.INTERRUPTED
    assert harness.latest_kind() not in AgentState.busy_states()
    detail = (agents_db.latest_state(harness.agent_id) or {}).get("detail") or {}
    assert detail["reason"] == error_classify.RUNNER_EXIT


@pytest.mark.parametrize("runner_name", ["clarp", "codex", "grok", "opencode"])
def test_axiom_every_runner_reports_a_child_that_exits_with_no_output(runner_name):
    """A child that produces nothing must still deliver exactly one terminal
    callback, or the dispatcher never frees the slot and the agent wedges.

    Drives each runner's real drain-thread body against a process that exits
    immediately with empty stdout and empty stderr.
    """
    seen, on_result, on_error = _terminal_calls()
    if runner_name == "clarp":
        from lib import clarp_runner
        clarp_runner._drain_stdout(FakeProc(), None, on_result, on_error, "t")
    elif runner_name == "codex":
        from lib import codex_runner
        codex_runner._drain_stdout(
            proc=FakeProc(), agent_id="", session="s", trace_id="t",
            backend_session_id="", handle=None, on_session_init=None,
            on_result=on_result, on_error=on_error, stream=None,
            enqueue=lambda **kwargs: 0)
    elif runner_name == "grok":
        from lib import grok_runner
        grok_runner._drain_stdout(
            proc=FakeProc(returncode=2), agent_id="", session="s", trace_id="t",
            handle=None, backend_session_id="", minted=False,
            on_session_init=None, on_result=on_result, on_error=on_error,
            stream=None, enqueue=lambda **kwargs: 0)
    else:
        from lib import opencode_runner
        opencode_runner._drain_stdout(
            proc=FakeProc(returncode=2), agent_id="", session="s", trace_id="t",
            handle=None, backend_session_id="", on_session_init=None,
            on_result=on_result, on_error=on_error, stream=None,
            enqueue=lambda **kwargs: 0)

    assert len(seen) == 1, f"{runner_name} delivered {seen!r}"


def test_axiom_a_queued_message_still_runs_after_the_in_flight_turn_fails(tmp_path):
    """`_mark_interrupted` ends with `_finish_turn`, so a classified failure
    drains the queue instead of stranding it. The user's second message runs."""
    harness = Harness(tmp_path)
    harness.send("first", client_msg_id="c-a", queue_if_busy=True)
    _, queued = harness.send("second", client_msg_id="c-b", queue_if_busy=True)
    assert queued.queued is True
    assert len(harness.backends.spawned) == 1

    harness.backends.spawned[0]["on_error"]("clarp exited rc=1")

    assert len(harness.backends.spawned) == 2
    assert harness.backends.spawned[1]["text"] == "second"
    assert [m["text"] for m in harness.messages()] == ["first", "second"]


def test_axiom_a_queued_message_is_invisible_until_it_starts(tmp_path):
    """Queue admission is deliberately not a transcript row: the durable
    `queued_turns` ledger owns it until execution begins
    (turn_dispatch.py:751-756). Nothing is shown, not even a queued marker."""
    harness = Harness(tmp_path)
    harness.send("first", client_msg_id="c-a", queue_if_busy=True)
    harness.send("second", client_msg_id="c-b", queue_if_busy=True)

    assert [m["text"] for m in harness.messages()] == ["first"]
    assert [m["text"] for m in harness.messages(include_automated=True)] == ["first"]


def test_axiom_markers_and_agent_rows_survive_the_pwa_automation_filter(tmp_path):
    """The PWA polls /log with include_automated=0. Exactly the routine
    automation origins may be hidden; a server-written `system` marker — the
    row issue #11 added so the user can see an unanswered turn — must not be."""
    harness = Harness(tmp_path)
    for origin in ("user", "agent", "oracle", "schedule", "automation",
                   "watcher", "heartbeat", "dreaming", "leader_tick"):
        message_store.record_user_message(
            agent_id=harness.agent_id,
            backend_session_id=harness.backend_session_id,
            client_msg_id=f"c-{origin}", text=f"from {origin}", origin=origin)
    message_store.record_interruption_marker(
        agent_id=harness.agent_id,
        backend_session_id=harness.backend_session_id,
        cause_message_id=harness.user_message_id("c-user"),
        text="Turn interrupted by server restart")

    visible = {m["origin"] for m in harness.messages()}
    hidden = {m["origin"] for m in harness.messages(include_automated=True)} - visible

    assert origins.MARKER_ORIGIN in visible
    assert {"user", "agent", "oracle", "schedule", "automation"} <= visible
    assert hidden == origins.ROUTINE_AUTOMATION_ORIGINS | {"watcher"}


def test_axiom_only_speak_and_text_reply_notify(tmp_path):
    """`notify` is exactly `reason in {speak, text-reply}`
    (user_notifications.py:361). A reply that is only a <team> broadcast is
    private to the team feed, so it must not page the user."""
    harness = Harness(tmp_path)
    trace, _ = harness.send("yo", client_msg_id="c-1")
    message_store.upsert_live_assistant_message(
        agent_id=harness.agent_id,
        backend_session_id=harness.backend_session_id,
        trace_id=trace, text="<team>for my teammates only</team>")
    harness.last_call()["on_result"]({"duration_ms": 5})

    notification = _classify(harness, trace)

    assert notification["notify"] is False
    assert notification["reason"] not in {"speak", "text-reply"}


def test_axiom_a_marker_row_id_cannot_collide_with_a_real_assistant_row(tmp_path):
    """Markers are keyed by the causing user row, in their own `marker-`
    namespace; transcript rows are `msg-`, live rows `live-`, user rows `u-`.
    A second boot therefore cannot add a second marker or overwrite a reply."""
    harness = Harness(tmp_path)
    user_row = message_store.record_user_message(
        agent_id=harness.agent_id,
        backend_session_id=harness.backend_session_id,
        client_msg_id="c-1", text="hi", origin="user")
    marker_id = message_store.marker_message_id(user_row["id"])
    assert marker_id.startswith("marker-")
    assert not marker_id.startswith(("msg-", "live-", "u-"))

    first = message_store.record_interruption_marker(
        agent_id=harness.agent_id,
        backend_session_id=harness.backend_session_id,
        cause_message_id=user_row["id"], text="Turn interrupted")
    second = message_store.record_interruption_marker(
        agent_id=harness.agent_id,
        backend_session_id=harness.backend_session_id,
        cause_message_id=user_row["id"], text="Turn interrupted")

    assert first is not None and second is None
    assert message_store.has_interruption_marker(user_row["id"]) is True


def test_axiom_a_duplicate_client_msg_id_after_reconnect_spawns_one_turn(tmp_path):
    """A client outbox retries the same send after a reconnect. The durable
    user row is the idempotency key: one row, one spawn, no second turn."""
    harness = Harness(tmp_path)
    harness.send("hello", client_msg_id="same")
    harness.send("hello", client_msg_id="same")

    assert len(harness.backends.spawned) == 1
    assert [m["text"] for m in harness.messages()] == ["hello"]


# =====================================================================
# Confirmed bugs — strict xfail asserting the behaviour we want.
# =====================================================================

@pytest.mark.xfail(strict=True, reason="live-server interruptions write no marker")
def test_a_failed_turn_leaves_no_marker_in_the_transcript(tmp_path):
    """BUG (high): the fix for issue #11 covers restarts only.

    A turn killed by a *restart* gets a visible marker row under the user's
    message (`interrupted_turns.py:118-126`). The identical outcome on a live
    server — the backend process died, the user's message will never be
    answered — writes nothing at all: `_mark_interrupted` records an
    INTERRUPTED state and stops (`turn_dispatch.py:1206-1229`). The state is a
    transient badge that the next turn overwrites, so the transcript ends up
    exactly as issue #11 described it: a user message with no reply and no
    marker, indistinguishable from an agent that chose to say nothing.

    Evidence: server/lib/turn_dispatch.py:1206 `_mark_interrupted` has no
    `message_store` call; server/lib/interrupted_turns.py:118 does.
    Proposed fix: call `message_store.record_interruption_marker` from
    `_mark_interrupted` for non-`_SILENT_ORIGINS` turns, with the human text
    already computed there ("Usage limit reached", "Turn timed out", …), and
    reuse `interrupted_turns._SILENT_ORIGINS` so both paths agree on who sees
    a marker.
    """
    harness = Harness(tmp_path)
    harness.send("yo", client_msg_id="c-1")

    harness.last_call()["on_error"]("Usage limit reached — resets at 5pm")

    assert harness.latest_kind() == AgentState.INTERRUPTED
    assert message_store.has_interruption_marker(harness.user_message_id("c-1"))


@pytest.mark.xfail(strict=True, reason="only DONE is classified for the user")
def test_a_failed_turn_creates_no_user_notification_row(tmp_path):
    """BUG (high): a turn that fails is never classified for the user.

    `state_watcher._poll_once` classifies a completed turn only when the state
    it just observed is DONE (`state_watcher.py:110`), and DONE has exactly one
    writer — the clean-result branch of the dispatcher
    (`turn_dispatch.py:1027`). Every non-clean ending (usage limit, runner
    exit, transient API error, exhausted retries, timeout) therefore produces
    no `user_notifications` row at all: no badge, no unread, no push, and
    nothing for `reclassify_recent_suppressed` to retry later.

    Evidence: server/lib/state_watcher.py:110 `if r["kind"] == AgentState.DONE`.
    Proposed fix: classify INTERRUPTED the same way DONE is classified, with a
    dedicated reason per `error_classify` category (`turn-failed:usage_limit`,
    `turn-failed:runner_exit`, …), so the failure reaches the same badge/push
    policy as a reply instead of living only in the log file.
    """
    harness = Harness(tmp_path)
    harness.send("yo", client_msg_id="c-1")

    harness.last_call()["on_error"]("Usage limit reached — resets at 5pm")

    reasons = [row["reason"] for row in harness.notifications()]
    assert reasons, "a failed turn produced no user-facing classification"


@pytest.mark.xfail(strict=True, reason="unknown category flips to IDLE silently")
def test_an_unclassified_backend_error_flips_to_idle_and_tells_nobody(tmp_path):
    """BUG (high): an unrecognised error string is treated as a clean idle.

    `_handle_failure` ends in a fallback that records IDLE and logs
    `clarpTurnFail` (`turn_dispatch.py:1152-1162`). IDLE is the same state a
    finished turn leaves behind, so the UI shows "Connected" and the user is
    told nothing. Every string the classifier does not recognise lands here —
    and the classifier recognises far less than the runners emit (see the
    grok/opencode/codex-app-server tests below).

    Evidence: server/lib/turn_dispatch.py:1152 comment "keep the old
    behaviour — flip to IDLE so the UI doesn't hang on THINKING".
    Proposed fix: delete the fallback. UNKNOWN is a failure like any other;
    record INTERRUPTED with `reason=unknown` and the raw text, so an
    unclassified crash is visible and shows up as a classifier gap to fix
    rather than as silence.
    """
    harness = Harness(tmp_path)
    harness.send("yo", client_msg_id="c-1")

    harness.last_call()["on_error"](
        "Error: ENOENT: no such file or directory, open '/workspace'")

    assert harness.latest_kind() == AgentState.INTERRUPTED


@pytest.mark.xfail(strict=True, reason="RUNNER_EXIT regex lists only 5 binaries")
def test_grok_and_opencode_exit_reports_are_unclassified():
    """BUG (medium): two backends' own exit messages are UNKNOWN.

    `grok_runner.py:242` reports `f"grok exited rc={rc}"` and
    `opencode_runner.py:228` reports `f"opencode exited rc={rc}"`, but
    `_RUNNER_EXIT_RE` (`error_classify.py:79`) enumerates only
    `codex|clarp|agy|claude|agent`. Both therefore classify UNKNOWN and take
    the silent IDLE branch above: a Grok or OpenCode agent whose CLI crashes
    goes quiet and looks connected.

    This is the "capabilities are declared, never assumed" shape from the
    sweep README — the classifier hardcodes a backend list that the adapter
    registry already owns.
    Proposed fix: build the alternation from `backends.ids()` (plus the
    binaries the adapters declare) instead of a literal, or better, have the
    runners raise a typed `RunnerExit(rc, stderr)` so no regex is needed to
    recognise a process that exited.
    """
    assert error_classify.classify_error("grok exited rc=2") == \
        error_classify.RUNNER_EXIT
    assert error_classify.classify_error("opencode exited rc=2") == \
        error_classify.RUNNER_EXIT


@pytest.mark.xfail(strict=True, reason="codex app-server strings are UNKNOWN")
def test_codex_app_server_interruption_and_crash_are_unclassified():
    """BUG (medium): the Codex app-server's own terminal strings match nothing.

    `codex_app_server.py:263` reports an interrupted turn as
    "codex turn interrupted" and `codex_app_server.py:128` reports the
    app-server process dying as "codex app-server exited". Neither matches
    `_INTERRUPTED_RE` (which knows `turn_aborted`, `user interrupted`,
    `sigterm`, …) nor `_RUNNER_EXIT_RE` (which requires an `rc=<n>`), so a
    barge-in and a crashed app-server both end as UNKNOWN → silent IDLE.

    An interruption misclassified as UNKNOWN is also the issue #11 conflation
    in reverse: two very different events collapse into one non-event.
    Proposed fix: have the app-server client raise/emit the category directly
    (it knows `status == "interrupted"` versus process death) rather than
    encoding it in prose that a regex on the other side must re-derive.
    """
    assert error_classify.classify_error("codex turn interrupted") == \
        error_classify.INTERRUPTED
    assert error_classify.classify_error("codex app-server exited") == \
        error_classify.RUNNER_EXIT


@pytest.mark.xfail(strict=True, reason="no auth category in error_classify")
def test_a_backend_auth_failure_is_unclassified():
    """BUG (medium): expired credentials are a silent turn.

    `backend_auth.py:254-258` already owns the vocabulary of an auth failure
    ("expired", "auth failed", "http 401", "http 403", "credentials
    unavailable") and turns it into a per-backend sign-in state. The turn
    classifier has no such category, so when a CLI fails a turn because its
    token expired the string falls through to UNKNOWN and the user gets the
    silent IDLE flip — while Settings separately knows perfectly well that the
    backend is signed out. That is issue #10's shape: a signal is collected
    and never read.

    Proposed fix: add `error_classify.AUTH` (never retried, always notified,
    NOTIFY set) reusing `backend_auth`'s vocabulary in one shared constant,
    and have `_mark_interrupted` deep-link the failure to that backend's
    sign-in row.
    """
    for text in ("Invalid API key · Please run /login",
                 "OAuth token has expired",
                 "auth failed: HTTP 401"):
        assert error_classify.classify_error(text) != error_classify.UNKNOWN, text


@pytest.mark.xfail(strict=True, reason="stderr text replaces the exit code")
def test_clarp_drops_the_exit_code_when_the_child_wrote_to_stderr():
    """BUG (medium): the one fact that classifies a crash is thrown away.

    `clarp_runner.py:410` reports `on_error(err or f"clarp exited rc={rc}")`.
    The exit code — the only part of the message `_RUNNER_EXIT_RE` can match —
    survives only when stderr happened to be empty. Any child that prints a
    diagnostic before dying reports just that diagnostic, classifies UNKNOWN,
    and takes the silent IDLE branch. `codex_runner.py:470`,
    `grok_runner.py:242` and `opencode_runner.py:228` have the same `err or`.

    Evidence: `clarpExitErr rc=3 stderr='boom'` is logged with the code, and
    then `on_error("boom")` is called without it.
    Proposed fix: always include the exit code — `f"clarp exited rc={rc}: {err}"`
    — or pass `(rc, stderr)` as structured data so classification never
    depends on string formatting.
    """
    seen, on_result, on_error = _terminal_calls()
    from lib import clarp_runner
    clarp_runner._drain_stdout(
        FakeProc(stderr="boom", returncode=3), None, on_result, on_error, "t")

    assert [kind for kind, _ in seen] == ["error"]
    assert error_classify.classify_error(seen[0][1]) == error_classify.RUNNER_EXIT


@pytest.mark.xfail(strict=True, reason="result event and bad exit both fire")
def test_clarp_fires_two_terminal_callbacks_when_a_result_precedes_a_bad_exit():
    """BUG (low): a turn can report both success and failure.

    `clarp_runner._drain_stdout` fires `on_result` for a `result` event and
    then, at `clarp_runner.py:405`, fires `on_error` for any non-zero exit —
    with no `saw_result` guard (contrast `codex_runner.py:468`, which at least
    guards on `st.failed_error`). A CLI that answers and then exits non-zero
    during shutdown delivers DONE followed by a failure for the same trace.

    Today the damage is contained by luck: `_handle_failure`'s superseded
    check finds the slot already freed by `_finish_turn` and drops the second
    callback (`turn_dispatch.py:1108-1115`). "Exactly one terminal callback per
    turn" is an invariant the dispatcher relies on, so it should hold in the
    runner rather than in the consumer.
    Proposed fix: skip the non-zero-exit `on_error` when `saw_result` is set,
    logging `clarpExitAfterResult` instead.
    """
    seen, on_result, on_error = _terminal_calls()
    from lib import clarp_runner
    result_line = json.dumps({"type": "result", "subtype": "success",
                              "result": "hi"}) + "\n"
    clarp_runner._drain_stdout(
        FakeProc(stdout_lines=[result_line], returncode=1, stderr="late crash"),
        None, on_result, on_error, "t")

    assert [kind for kind, _ in seen] == ["result"]


@pytest.mark.xfail(strict=True, reason="a codex abort is delivered as a clean result")
def test_a_codex_abort_is_reported_to_the_dispatcher_as_a_clean_turn():
    """BUG (medium): `turn_aborted` never reaches the dispatcher.

    `codex_runner.py:604-608` handles `turn_aborted` by recording IDLE inside
    the runner and returning — it does not set `st.failed_error` and does not
    call `on_error`. When the process then exits zero, the drain thread's
    clean-exit branch synthesises a success result (`codex_runner.py:476-485`).
    The dispatcher records DONE for a turn that was aborted, the notification
    classifier sees a completed turn with no content, and reports
    `no-user-facing-content` — the exact conflation issue #11 asked to be
    separated, reproduced on a path that has nothing to do with restarts.

    Proposed fix: set `st.failed_error = "codex turn_aborted"` (already an
    `_INTERRUPTED_RE` match) and let the dispatcher own the state transition;
    runners should report events, not decide agent state.
    """
    from lib import codex_runner
    seen, on_result, on_error = _terminal_calls()
    aborted = json.dumps({"type": "turn_aborted"}) + "\n"

    codex_runner._drain_stdout(
        proc=FakeProc(stdout_lines=[aborted]), agent_id="", session="s",
        trace_id="t", backend_session_id="", handle=None,
        on_session_init=None, on_result=on_result, on_error=on_error,
        stream=None, enqueue=lambda **kwargs: 0)

    assert [kind for kind, _ in seen] == ["error"]


@pytest.mark.xfail(strict=True, reason="reconcile repairs the badge, not the transcript")
def test_a_child_that_dies_without_a_callback_is_repaired_silently(tmp_path):
    """BUG (high): the live-server twin of issue #11, with no marker either.

    A backend child that is OOM-killed, segfaults, or is killed by anything
    other than /stop fires no terminal callback at all. Nothing notices until
    the next snapshot read, when `reconcile` INV1 sees a busy state with no
    live process and writes IDLE (`reconcile.py:77-81`). The badge clears —
    and that is the whole repair. No marker, no notification, no eventlog
    entry the user can see; the transcript is a user message followed by
    nothing, and the state log now ends non-busy so a later
    `interrupted_turns.recover_after_restart` will not find it either.

    Proposed fix: make the repair record INTERRUPTED (not IDLE) with
    `reason="reconcile"`, and route it through the same marker writer as
    `interrupted_turns._mark`, so "the server noticed your turn died" has one
    implementation regardless of which watchdog noticed.
    """
    harness = Harness(tmp_path)
    harness.send("yo", client_msg_id="c-1")
    harness.backends.live = False

    repaired = reconcile.reconcile_agent(harness.agent_id, "claude", home=tmp_path)

    assert repaired.get("state") == AgentState.THINKING
    assert harness.latest_kind() not in AgentState.busy_states()
    assert message_store.has_interruption_marker(harness.user_message_id("c-1"))


@pytest.mark.xfail(strict=True, reason="a preempted turn's outcome is discarded")
def test_a_preempted_turn_abandons_its_user_message_without_a_trace(tmp_path):
    """BUG (medium): the default send kills the running turn and says nothing.

    `POST /send` defaults `queue_if_busy` to false (`server.py:3916`), so a
    second message to a busy non-steerable agent takes the
    `_enqueue_if_busy` preempt branch and SIGTERMs the in-flight turn
    (`turn_dispatch.py:640-655`). The killed turn's terminal callback is then
    deliberately dropped by `_handle_failure`'s superseded guard
    (`turn_dispatch.py:1108-1115`), and the new turn's live row DELETEs the old
    one's partial text (`message_store.py:593-597`). The first user message
    ends with no reply, no partial, and no marker.

    Note also that `turn_dispatch.py:34-39` documents the opposite behaviour
    ("QUEUES behind it ... does NOT SIGTERM the running turn"); the code and
    its module docstring disagree about the default.
    Proposed fix: write a `Superseded by your next message` marker against the
    abandoned user row before preempting — the user asked for the preemption,
    so this is a transcript record, not an error.
    """
    harness = Harness(tmp_path)
    harness.send("first", client_msg_id="c-a")
    first_call = harness.last_call()
    harness.send("second", client_msg_id="c-b")
    assert harness.backends.interrupted == [harness.agent_id]

    first_call["on_error"]("SIGTERM")

    assert message_store.has_interruption_marker(harness.user_message_id("c-a"))


@pytest.mark.xfail(strict=True, reason="close_turn has no callers")
def test_a_finished_turn_never_closes_its_row_in_the_turns_table(tmp_path):
    """BUG (medium): the only explicit turn lifecycle in the schema is dead.

    `agents_db.open_turn` is called on every dispatch
    (`turn_dispatch.py:510`, `:753`) but `agents_db.close_turn`
    (`agents.py:593`) has no caller anywhere in `server/`, so `turns.ended_at`
    is NULL for every turn ever run. Four "is this the active turn?" queries
    are written as `WHERE ended_at IS NULL ORDER BY turn_id DESC LIMIT 1`
    (`message_store.py:417`, `:498`, `:551`, `:704`) and are correct only
    because that degenerates to "the newest turn row" — an accident, not a
    fence. It is also why issue #11's own suggested fix ("when a turn is found
    with ended_at IS NULL at startup") could not be implemented as written and
    `interrupted_turns` had to infer the orphan from the state log instead.

    Proposed fix: close the turn in `_finish_turn` (the one place every
    terminal path already funnels through) and give `turns` a `status`
    column — then "was this turn answered?" is a row, not an inference over a
    state log.
    """
    harness = Harness(tmp_path)
    harness.send("hi", client_msg_id="c-1")

    harness.last_call()["on_result"]({"duration_ms": 1})

    rows = db.conn().execute(
        "SELECT ended_at FROM turns WHERE agent_id = ?",
        (harness.agent_id,)).fetchall()
    assert rows and all(row["ended_at"] is not None for row in rows)


@pytest.mark.xfail(strict=True, reason="the marker is never retracted")
def test_a_restart_marker_survives_the_real_reply_it_was_wrong_about(tmp_path):
    """BUG (low): the transcript can show both "interrupted" and the answer.

    Boot recovery marks a turn interrupted from the state log alone. A backend
    that outlived the server (or a late transcript import) can still finalize
    that trace afterwards; `finalize_live_assistant_message`
    (`message_store.py:611`) knows nothing about markers, so the reply lands
    and the marker stays. Because display order is timestamp-then-seq and the
    marker's timestamp is forced past the user row's, the marker sorts *after*
    the real answer: the user reads the reply and is then told the turn was
    interrupted.

    Proposed fix: delete the marker for a cause the moment a real assistant
    row is written for the same trace — the same retraction
    `delete_live_assistant_message` already performs for provisional rows.
    """
    harness = Harness(tmp_path)
    trace, _ = harness.send("yo", client_msg_id="c-1")
    message_store.record_interruption_marker(
        agent_id=harness.agent_id,
        backend_session_id=harness.backend_session_id,
        cause_message_id=harness.user_message_id("c-1"),
        text="Turn interrupted by server restart")

    message_store.finalize_live_assistant_message(
        agent_id=harness.agent_id,
        backend_session_id=harness.backend_session_id,
        trace_id=trace, text="Actually, here is the answer")

    texts = [m["text"] for m in harness.messages()]
    assert "Actually, here is the answer" in texts
    assert "Turn interrupted by server restart" not in texts
