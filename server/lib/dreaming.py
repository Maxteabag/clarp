"""Nightly deep dreaming for opted-in agents.

Dreaming is a multi-round, read-only investigation. Intermediate seed,
fan-out, iteration, and completeness-check outputs are hidden from the chat and
captured in a SQLite ledger; one final Dream Digest is delivered visibly.
"""
from __future__ import annotations

import json
import os
import pathlib
import random
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, Callable
from zoneinfo import ZoneInfo

from . import agents as agents_db
from . import backends, config, db, dream_seeds, location, origins
from . import settings_store
from . import trace as _trace
from .log import log, log_exception
from .protocol import AgentState
from .revisioned_cache import RevisionedCache


from .dreaming_settings import (  # noqa: E402,F401  (split_apply: dreaming)
    DREAMING_OK, DREAM_HIDDEN_PREFIX, DREAM_TARGET_HOUR, DREAM_WINDOW_MINUTES,
    DREAM_POLL_INTERVAL_SEC, DREAM_ACK_MAX_CHARS, DREAMS_PER_NIGHT_DEFAULT, DREAMS_PER_NIGHT_MIN,
    DREAMS_PER_NIGHT_MAX, DREAM_DIRECTION_MIN, DREAM_DIRECTION_MAX, DREAM_TOKEN_BUDGET_MIN,
    DREAM_TOKEN_BUDGET_MAX, DREAM_MIN_DIRECTIONS, DREAM_DIRECTION_COUNT,
    DREAM_ITERATION_THREAD_COUNT, DREAM_PLANNED_ROUNDS, DREAM_TARGET_TOKEN_BUDGET,
    DREAM_TARGET_MINUTES, DREAM_SENT_RECOVERY_SEC, DREAM_RUN_DEADLINE_SEC, _DEADLINE_REASON,
    DREAM_PRIOR_CONTEXT_MAX_CHARS, DREAM_PRIOR_ROUND_EXCERPT_CHARS, _STAGE_TARGET_TOKENS,
    KEY_DREAMS_PER_NIGHT, KEY_DIRECTION_COUNT, KEY_TOKEN_BUDGET, KEY_DREAM_BACKEND, KEY_DREAM_MODEL,
    KEY_DREAM_EFFORT, DreamingSettings, _clamp_int, get_settings, update_settings,
    dreaming_prompt_text, DREAMING_PROMPT, _TOKEN_EDGE_RE, _STAGE_OUTPUT_RE, _DIGEST_DONE_RE,
    _MD_PREFIX, _SLATE_LINE_RE, _FALLBACK_LINE_RE, _RUN_ID_RE, _EVIDENCE_STATUS_RE, _ALTITUDE_RE,
    _ARTIFACT_RE, _EVIDENCE_SUMMARY_RE, _GUARDRAIL_REFUSAL_RE,
)
_advance_request_callback: Callable[[str], None] | None = None
from .dreaming_store import (  # noqa: E402,F401  (split_apply: dreaming)
    _advance_request_lock, dreaming_enabled, should_skip_dream_prompt, strip_dreaming_ack,
    mark_dream_started, dream_runs_for_agent_date, active_run_for_agent, active_runs,
    ActiveDreamRunExists, create_dream_run, _gather_seed_material, list_dream_runs,
    next_round_for_run, claim_round, release_round, mark_round_sent, record_final_digest,
    _AlreadyPublished, _deliver_digest, abandon_run, _CLEANUP_PREFIX, _CLEANUP_BATCH,
    _harvest_abandoned, _WORKERS_PREFIX, _process_start, _claim_worker, _fill_worker,
    _worker_claims, _forget_workers, _workers_gone, _cleanup_quiet, _harvest_abandoned_runs,
    mark_active_noop, _record_thread_evidence, _parse_thread_metadata, _normalize_altitude,
    _skip_busy_reason, _recover_stale_sent_round, _env_int, _routine_self_activity_busy,
    _resolve_dream_llm, _assistant_text_from_result, _dose_snapshot,
    _with_isolated_context_snapshot, _recent_real_context_snapshot, _insert_round,
    _ensure_next_rounds, _rounds, _threads, _stage_exists, _incomplete_stages, _next_round_index,
    _create_threads_from_seed, _create_fanout_rounds, _create_iteration_rounds, _sync_run_progress,
)
@dataclass(frozen=True)
class ResolvedTimeZone:
    tz: tzinfo
    name: str
    source: str
    location: dict | None = None


def reset_for_tests() -> None:
    set_advance_request_callback(None)


def set_advance_request_callback(callback: Callable[[str], None] | None) -> None:
    global _advance_request_callback
    with _advance_request_lock:
        _advance_request_callback = callback


def _request_advance(run_id: str) -> None:
    with _advance_request_lock:
        callback = _advance_request_callback
    if callback is None:
        return
    try:
        callback(run_id)
    except Exception as e:  # noqa: BLE001
        log_exception("dreamingAdvanceRequestFail", e, detail=run_id)


def process_assistant_text(agent_id: str, text: str, *, origin: str,
                           live: bool = False) -> tuple[bool, str]:
    """Capture dreaming control markers and return display decision.

    Live streamed text is suppressed/cleaned but does not advance the ledger;
    durable transcript import owns stage completion so partial output cannot
    accidentally trigger the next sub-turn.
    """
    raw = str(text or "")
    stage = _STAGE_OUTPUT_RE.search(raw)
    if stage:
        if not live:
            response = raw[stage.end():].strip()
            record_round_output(
                agent_id=agent_id,
                run_id=stage.group("run_id"),
                round_id=stage.group("round_id"),
                stage=stage.group("stage").lower(),
                response=response,
            )
        return True, ""

    done = _DIGEST_DONE_RE.search(raw)
    if done:
        visible = raw[:done.start()].rstrip()
        if not live:
            record_final_digest(
                agent_id=agent_id,
                run_id=done.group("run_id"),
                round_id=done.group("round_id"),
                digest=visible,
            )
        return False, visible

    # DREAMING_OK counts only in a dreaming turn (AUT-07); every caller names
    # the turn's origin, so a new caller cannot get the dream side effects by
    # default.
    skip, stripped = origins.ack_for_origin(
        strip_dreaming_ack(raw), raw, token=DREAMING_OK, origin=origin, own="dreaming")
    if skip:
        if not live and origin == "dreaming":
            mark_active_noop(agent_id)
        return True, ""
    return False, stripped


def pending_dreaming_agents(
    *,
    now: float | None = None,
    timezone_resolver: Callable[[str, float], ResolvedTimeZone] | None = None,
) -> list[dict]:
    now = time.time() if now is None else now
    resolve_tz = timezone_resolver or resolve_user_timezone
    settings = get_settings()
    out: list[dict] = []
    for agent in agents_db.list_agents():
        if not dreaming_enabled(agent):
            continue
        agent_id = agent["agent_id"]
        session = (agent.get("session") or "").strip()
        if not session:
            continue
        if active_run_for_agent(agent_id):
            continue
        reason = _skip_busy_reason(agent)
        if reason:
            log("dreamingSkip", f"agent={agent_id} session={session} reason={reason}")
            continue
        resolved = resolve_tz(session, now)
        local_dt = datetime.fromtimestamp(now, resolved.tz)
        local_date = local_dt.date().isoformat()
        if not _in_dream_window(local_dt):
            continue
        runs_today = dream_runs_for_agent_date(agent_id, local_date)
        if runs_today >= settings.dreams_per_night:
            continue
        out.append({
            **agent,
            "dreaming_local_date": local_date,
            "dreaming_timezone": resolved.name,
            "dreaming_timezone_source": resolved.source,
            "dreaming_settings": settings,
        })
    return out


def start_manual_run(session: str, *, seed_strategy: str = "",
                     context_dose: str = "") -> dict[str, Any]:
    """Begin a dream now, outside the nightly window.

    The scheduled path depends on resolving the user's local 03:00 from their
    phone's last known position, which is the right default and the wrong
    thing to rely on when you want a specific experiment to run tonight. This
    bypasses the window and the once-per-day guard; everything downstream (the
    round chain, the ledger, delivery) is identical.
    """
    agent = agents_db.get_by_session(session.strip())
    if not agent:
        raise ValueError(f"unknown session {session!r}")
    existing = active_run_for_agent(agent["agent_id"])
    if existing:
        raise ValueError(
            f"{session} already has an active dream run {existing['run_id']}")
    resolved = resolve_user_timezone(agent.get("session") or "")
    local_date = datetime.now(resolved.tz).strftime("%Y-%m-%d")
    run = create_dream_run(
        agent,
        local_date=local_date,
        timezone_name=resolved.name,
        timezone_source=resolved.source,
        seed_strategy=seed_strategy,
        context_dose=context_dose,
    )
    # Count a manual run against the night's quota. Without this the nightly
    # scheduler can fire a second, randomly-seeded run for the same agent once
    # the phone's local 03:00 comes round, which doubles the spend and muddies
    # a pinned experiment.
    mark_dream_started(agent["agent_id"], local_date)
    log("dreamingManualStart",
        f"run={run.get('run_id')} session={session} "
        f"strategy={run.get('seed_strategy')} dose={run.get('context_dose')}")
    _request_advance(str(run.get("run_id") or ""))
    return run


from .dreaming_run_lookup import (  # noqa: E402,F401  (split_apply: dreaming)
    get_run, _settings_for_run,
)
def record_round_output(*, agent_id: str, run_id: str, round_id: str,
                        stage: str, response: str) -> None:
    row = db.conn().execute(
        """SELECT r.*, dr.agent_id
             FROM dream_rounds r
             JOIN dream_runs dr ON dr.run_id = r.run_id
            WHERE r.run_id = ? AND r.round_id = ?""",
        (run_id, round_id),
    ).fetchone()
    if not row or row["agent_id"] != agent_id:
        log("dreamingLedgerMiss",
            f"agent={agent_id} run={run_id} round={round_id} stage={stage}")
        return
    if row["status"] == "completed":
        return
    now = db.now_ms()
    output_chars = len(response or "")
    db.conn().execute(
        """UPDATE dream_rounds
              SET status = 'completed', response = ?, completed_at = ?,
                  output_chars = ?, updated_at = ?
            WHERE round_id = ?""",
        (response, now, output_chars, now, round_id),
    )
    thread_id = row["thread_id"]
    if thread_id and stage.lower() == "fanout":
        _record_thread_evidence(thread_id, response, status="fanout_complete")
        db.conn().execute(
            """UPDATE dream_threads
                  SET status = 'fanout_complete', fanout_chars = ?,
                      updated_at = ?
                WHERE thread_id = ?""",
            (output_chars, now, thread_id),
        )
    if thread_id and stage.lower() == "iterate":
        _record_thread_evidence(thread_id, response, status="iteration_complete")
        db.conn().execute(
            """UPDATE dream_threads
                  SET status = 'iteration_complete', iterate_chars = ?,
                      updated_at = ?
                WHERE thread_id = ?""",
            (output_chars, now, thread_id),
        )
    _sync_run_progress(run_id, stage.lower())
    log("dreamingRoundComplete",
        f"run={run_id} round={round_id} stage={stage.lower()} chars={output_chars}")
    _request_advance(run_id)


def resolve_user_timezone(session: str, now: float | None = None) -> ResolvedTimeZone:
    """Resolve the user's current timezone from location, with explicit fallback."""
    now = time.time() if now is None else now
    loc = location.get_location(session) or location.latest_location()
    if loc:
        via_google = _google_timezone(loc, now)
        if via_google:
            return via_google
        fallback = _longitude_timezone(loc)
        log("dreamingTimezoneFallback",
            f"reason=no-google-timezone session={session} source={fallback.source}")
        return fallback
    fallback = _host_timezone()
    log("dreamingTimezoneFallback",
        f"reason=no-location session={session} source={fallback.source}")
    return fallback


# A zone id does not depend on the timestamp (ZoneInfo applies DST), so one
# Google lookup serves a location cell (2 decimals, about 1 km) for hours; a
# failure is retried after a pause instead of on every 300 s poll (AUT-12).
_TZ_CACHE_OK_S = 6 * 3600
_TZ_CACHE_FAIL_S = 600
# cell -> (until, zone id or ""); the revision is unused (always 0).
_TZ_CACHE: RevisionedCache[tuple[float, float], tuple[float, str]] = RevisionedCache(
    "dreaming-google-timezone", max_entries=256)


def _google_timezone(loc: dict, now: float) -> ResolvedTimeZone | None:
    key = (
        os.environ.get("GOOGLE_MAPS_API_KEY")
        or os.environ.get("GOOGLE_API_KEY")
        or os.environ.get("MAPS_API_KEY")
        or ""
    ).strip()
    if not key:
        return None
    lat = float(loc["lat"])
    lng = float(loc["lng"])
    cell = (round(lat, 2), round(lng, 2))
    entry = _TZ_CACHE.peek(cell)
    cached = entry[1] if entry is not None else None
    if cached is not None and now < cached[0]:
        return (ResolvedTimeZone(tz=ZoneInfo(cached[1]), name=cached[1], source="google-timezone",
                                 location=loc) if cached[1] else None)
    resolved = _google_timezone_lookup(loc, lat, lng, now, key)
    _TZ_CACHE.put(cell, 0, (now + _TZ_CACHE_OK_S, resolved.name) if resolved
                  else (now + _TZ_CACHE_FAIL_S, ""))
    return resolved


def _google_timezone_lookup(loc: dict, lat: float, lng: float, now: float,
                            key: str) -> ResolvedTimeZone | None:
    query = urllib.parse.urlencode({
        "location": f"{lat},{lng}",
        "timestamp": str(int(now)),
        "key": key,
    })
    url = f"https://maps.googleapis.com/maps/api/timezone/json?{query}"
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        log_exception("dreamingTimezoneGoogleFail", e)
        return None
    if payload.get("status") != "OK":
        log("dreamingTimezoneGoogleFail", f"status={payload.get('status')}")
        return None
    name = str(payload.get("timeZoneId") or "").strip()
    if not name:
        return None
    try:
        return ResolvedTimeZone(
            tz=ZoneInfo(name), name=name, source="google-timezone", location=loc,
        )
    except Exception as e:  # noqa: BLE001
        log_exception("dreamingTimezoneZoneInfoFail", e, detail=name)
        return None


def _longitude_timezone(loc: dict) -> ResolvedTimeZone:
    lng = float(loc["lng"])
    offset_hours = max(-12, min(14, int(round(lng / 15.0))))
    tz = timezone(timedelta(hours=offset_hours), _offset_name(offset_hours))
    return ResolvedTimeZone(
        tz=tz, name=_offset_name(offset_hours),
        source="longitude-offset-fallback", location=loc,
    )


def _host_timezone() -> ResolvedTimeZone:
    tz = datetime.now().astimezone().tzinfo or timezone.utc
    name = getattr(tz, "key", None) or str(tz)
    return ResolvedTimeZone(tz=tz, name=name, source="host-fallback")


def _offset_name(offset_hours: int) -> str:
    sign = "+" if offset_hours >= 0 else "-"
    return f"UTC{sign}{abs(offset_hours):02d}:00"


def _in_dream_window(local_dt: datetime) -> bool:
    start = DREAM_TARGET_HOUR * 60
    current = local_dt.hour * 60 + local_dt.minute
    return start <= current < start + DREAM_WINDOW_MINUTES


def dispatch_isolated_dream(agent: dict, prompt: str) -> bool:
    """Run one dream round outside the agent's live working session.

    The dream gets a read-only snapshot of recent chat context, then runs in a
    fresh backend session that is never bound to the agent runtime and never
    written to the chat read model.
    """
    agent_id = str(agent.get("agent_id") or "")
    session = str(agent.get("session") or "")
    if not agent_id or not session:
        return False
    backend = backends.normalize(
        get_settings().backend or agent.get("backend"))
    backend_session_id = str(uuid.uuid4())
    trace_id = _trace.new_trace_id()
    model, effort = _resolve_dream_llm(agent, backend)
    run_id = _run_id_from_prompt(prompt)
    run = get_run(run_id) if run_id else None
    dose = str((run or {}).get("context_dose") or dream_seeds.DOSE_FULL)
    text = _with_isolated_context_snapshot(agent, prompt, dose)
    cwd = _dream_scratch_cwd(agent, prompt)

    def on_result(event: dict) -> None:
        assistant_text = _assistant_text_from_result(event)
        if not assistant_text.strip():
            log("dreamingIsolatedNoOutput",
                f"agent={agent_id} session={session} trace={trace_id}")
            return
        process_assistant_text(agent_id, assistant_text, live=False, origin="dreaming")

    def on_error(message: str) -> None:
        log("dreamingIsolatedError",
            f"agent={agent_id} session={session} trace={trace_id} "
            f"error={str(message or '')[:300]}")
        if run_id:
            # Visible on the run, which its deadline later ends (AUT-06).
            db.conn().execute(
                "UPDATE dream_runs SET last_error = ?, updated_at = ? "
                "WHERE run_id = ? AND status = 'active'",
                (f"isolated round failed: {str(message or '')[:400]}",
                 db.now_ms(), run_id))

    claim = ""
    if run_id:
        # Before the spawn, so a worker is never alive unrecorded: cleanup of
        # the run's worktree waits for every claimed worker (AUT-06).
        claim = _claim_worker(run_id)
    handle = backends.spawn_turn(
        backend,
        text=text,
        cwd=cwd,
        backend_session_id=backend_session_id,
        is_new_session=True,
        session=session,
        agent_id=agent_id,
        on_session_init=None,
        on_result=on_result,
        on_error=on_error,
        trace_id=trace_id,
        model=model,
        effort=effort,
        stream=None,
        synthesize_audio=False,
        voice_preamble=False,
        isolated=True,
        hook_session="",
    )
    pid = getattr(handle, "pid", None)
    if claim and isinstance(pid, int):
        _fill_worker(claim, pid)
    log("dreamingIsolatedDispatch",
        f"agent={agent_id} session={session} backend={backend} "
        f"bsid={backend_session_id} trace={trace_id} cwd={cwd}")
    return True


from .dreaming_worktree import (  # noqa: E402,F401  (split_apply: dreaming)
    _dream_scratch_cwd, harvest_worktree, _dream_worktree_path, _repo_toplevel, _dream_fallback_cwd,
    _run_id_from_prompt, _existing_cwd,
)
from .dreaming_prompt import (  # noqa: E402,F401  (split_apply: dreaming)
    _parse_slate, _parse_slate_items, _clean_slate_title, _slate_item_is_anti_promoted,
    _slate_item_evidence_status, _build_prompt, _prior_round_context, _thread_label,
    _metadata_suffix, _cap_context_lines, _thread, _preview, _random_recipe,
)
class DreamingScheduler:
    """Polls for local-night dream windows and advances active investigations."""

    def __init__(
        self,
        *,
        send_dream: Callable[[str, str], bool | None],
        poll_interval_sec: float = DREAM_POLL_INTERVAL_SEC,
        now: Callable[[], float] = time.time,
        chain_delay_sec: float = 1.0,
        chain_retry_sec: float = 1.0,
        chain_attempts: int = 30,
        recipe_chooser: Callable[[], tuple[str, str]] | None = None,
    ):
        self._send_dream = send_dream
        self.recipe_chooser = recipe_chooser or _random_recipe
        self.poll_interval_sec = poll_interval_sec
        self.now = now
        self.chain_delay_sec = max(0.0, float(chain_delay_sec))
        self.chain_retry_sec = max(0.0, float(chain_retry_sec))
        self.chain_attempts = max(1, int(chain_attempts))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._wake_lock = threading.Lock()
        self._pending_wakes: set[str] = set()
        set_advance_request_callback(self.request_advance)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="agent-dreaming-scheduler",
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        set_advance_request_callback(None)

    def run_once(self) -> int:
        sent = 0
        _harvest_abandoned_runs()
        for run in active_runs():
            sent += self._advance_run(run)
        for agent in pending_dreaming_agents(now=self.now()):
            strategy, dose = self.recipe_chooser()
            try:
                run = create_dream_run(
                    agent,
                    local_date=agent["dreaming_local_date"],
                    timezone_name=agent["dreaming_timezone"],
                    timezone_source=agent["dreaming_timezone_source"],
                    seed_strategy=strategy,
                    context_dose=dose,
                )
            except ActiveDreamRunExists:
                # A manual start created its run after the listing.
                continue
            sent += self._advance_run(run)
        return sent

    def _advance_run(self, run: dict[str, Any]) -> int:
        agent = agents_db.get_by_agent_id(run["agent_id"])
        if not agent or not dreaming_enabled(agent):
            # Turning dreaming off used to strand whatever was in flight: the
            # run could no longer advance, and because recovery lived below
            # this check it could not be reaped either, so it sat active
            # forever holding an untracked worktree. Opting out ends the run
            # instead of abandoning it.
            abandon_run(run["run_id"])
            return 0
        if db.now_ms() - int(run.get("started_at") or 0) > DREAM_RUN_DEADLINE_SEC * 1000:
            # The run ends now; a backend still writing in its worktree keeps
            # it until that backend stops (AUT-06 review).
            abandon_run(run["run_id"], reason=_DEADLINE_REASON,
                        harvest=_cleanup_quiet(run["run_id"], run["agent_id"]))
            return 0
        _recover_stale_sent_round(run["run_id"])
        reason = _skip_busy_reason(agent)
        if reason:
            log("dreamingSkip",
                f"agent={run['agent_id']} session={run['session']} reason={reason}")
            return 0
        dream_round = next_round_for_run(run)
        if not dream_round:
            return 0
        # Claimed before the send, so a second advance (the wake thread, the
        # loop, another Host process) cannot send it too (AUT-11).
        claimed_at = claim_round(dream_round["round_id"])
        if not claimed_at:
            log("dreamingSkip",
                f"agent={run['agent_id']} session={run['session']} reason=claimed")
            return 0
        try:
            dispatched = self._send_dream(run["session"], dream_round["prompt"])
        except Exception as e:  # noqa: BLE001
            release_round(dream_round["round_id"], claimed_at)
            log_exception("dreamingTickFail", e, detail=run.get("session") or "")
            return 0
        if dispatched is False:
            release_round(dream_round["round_id"], claimed_at)
            log("dreamingSkip",
                f"agent={run['agent_id']} session={run['session']} "
                "reason=dispatch-busy")
            return 0
        try:
            mark_round_sent(dream_round["round_id"], claimed_at)
            if dream_round["round_index"] == 1:
                mark_dream_started(run["agent_id"], run["local_date"])
            log(
                "dreamingRoundSent",
                f"run={run['run_id']} agent={run['agent_id']} "
                f"round={dream_round['round_index']} stage={dream_round['stage']} "
                f"target_tokens={dream_round['target_tokens']}",
            )
            return 1
        except Exception as e:  # noqa: BLE001
            log_exception("dreamingTickFail", e, detail=run.get("session") or "")
            return 0

    def request_advance(self, run_id: str) -> None:
        """Advance a run soon after a round's hidden output reaches the ledger.

        The normal poll still exists as a safety net, but deep dreaming must not
        require repeated UI transcript fetches or 5-minute scheduler ticks
        between every sub-turn. A short retry loop lets the backend fully clear
        its active handle before the next round is dispatched.
        """
        if self._stop.is_set():
            return
        with self._wake_lock:
            if run_id in self._pending_wakes:
                return
            self._pending_wakes.add(run_id)
        wake = threading.Thread(
            target=self._advance_when_ready,
            args=(run_id,),
            daemon=True,
            name=f"dreaming-advance-{run_id[:12]}",
        )
        wake.start()

    def _advance_when_ready(self, run_id: str) -> None:
        try:
            for attempt in range(self.chain_attempts):
                delay = self.chain_delay_sec if attempt == 0 else self.chain_retry_sec
                if delay and self._stop.wait(delay):
                    return
                run = get_run(run_id)
                if not run or run.get("status") != "active":
                    return
                if self._advance_run(run):
                    return
                if not self.chain_retry_sec:
                    continue
        except Exception as e:  # noqa: BLE001
            log_exception("dreamingAdvanceFail", e, detail=run_id)
        finally:
            with self._wake_lock:
                self._pending_wakes.discard(run_id)

    def _loop(self) -> None:
        if self._stop.wait(min(60.0, self.poll_interval_sec)):
            return
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as e:  # noqa: BLE001
                log_exception("dreamingSchedulerFail", e)
            if self._stop.wait(self.poll_interval_sec):
                break
