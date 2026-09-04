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
from . import backends, compaction, config, db, dream_seeds, location, origins
from . import settings_store
from .log import log, log_exception
from .protocol import AgentState


DREAMING_OK = "DREAMING_OK"
DREAM_HIDDEN_PREFIX = "[[CLARP_DREAMING_RUN"
DREAM_TARGET_HOUR = 3
DREAM_WINDOW_MINUTES = 60
DREAM_POLL_INTERVAL_SEC = 5 * 60
DREAM_ACK_MAX_CHARS = 300

DREAMS_PER_NIGHT_DEFAULT = 1
DREAMS_PER_NIGHT_MIN = 1
DREAMS_PER_NIGHT_MAX = 5
DREAM_DIRECTION_MIN = 2
DREAM_DIRECTION_MAX = 8
DREAM_TOKEN_BUDGET_MIN = 30_000
DREAM_TOKEN_BUDGET_MAX = 300_000

DREAM_MIN_DIRECTIONS = 3
DREAM_DIRECTION_COUNT = 3
DREAM_ITERATION_THREAD_COUNT = 1
DREAM_PLANNED_ROUNDS = (
    1 + DREAM_DIRECTION_COUNT + DREAM_ITERATION_THREAD_COUNT + 1 + 1
)
DREAM_TARGET_TOKEN_BUDGET = 70_000
DREAM_TARGET_MINUTES = 120
DREAM_SENT_RECOVERY_SEC = 90 * 60
DREAM_PRIOR_CONTEXT_MAX_CHARS = 7_000
DREAM_PRIOR_ROUND_EXCERPT_CHARS = 1_000

_STAGE_TARGET_TOKENS = {
    "roleplay_day": 8_000,
    "seed": 10_000,
    "fanout": 10_000,
    "iterate": 12_000,
    "completeness": 6_000,
    "synthesize": 8_000,
}

KEY_DREAMS_PER_NIGHT = "dreaming.dreams_per_night"
KEY_DIRECTION_COUNT = "dreaming.direction_count"
KEY_TOKEN_BUDGET = "dreaming.target_token_budget"
# Dreaming runs on its own provider. A user may want a cheap, chatty model for
# the live conversation and a slow expensive one for the night, or the reverse;
# inheriting the agent's backend made that impossible to express.
KEY_DREAM_BACKEND = "dreaming.backend"
KEY_DREAM_MODEL = "dreaming.model"
KEY_DREAM_EFFORT = "dreaming.effort"


@dataclass(frozen=True)
class DreamingSettings:
    dreams_per_night: int = DREAMS_PER_NIGHT_DEFAULT
    direction_count: int = DREAM_DIRECTION_COUNT
    target_token_budget: int = DREAM_TARGET_TOKEN_BUDGET
    # Empty means "inherit the agent's own backend/model/effort".
    backend: str = ""
    model: str = ""
    effort: str = ""

    @property
    def min_directions(self) -> int:
        return self.direction_count

    @property
    def planned_rounds(self) -> int:
        return 1 + self.direction_count + DREAM_ITERATION_THREAD_COUNT + 1 + 1

    @property
    def target_minutes(self) -> int:
        return DREAM_TARGET_MINUTES

    def stage_target_tokens(self) -> dict[str, int]:
        scale = self.target_token_budget / DREAM_TARGET_TOKEN_BUDGET
        return {
            stage: max(1_000, int(round(tokens * scale)))
            for stage, tokens in _STAGE_TARGET_TOKENS.items()
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "dreams_per_night": self.dreams_per_night,
            "direction_count": self.direction_count,
            "planned_directions": self.direction_count,
            "min_directions": self.min_directions,
            "planned_rounds": self.planned_rounds,
            "target_token_budget": self.target_token_budget,
            "target_tokens": self.target_token_budget,
            "target_minutes": self.target_minutes,
            "target_hour": DREAM_TARGET_HOUR,
            "window_minutes": DREAM_WINDOW_MINUTES,
            "backend": self.backend,
            "model": self.model,
            "effort": self.effort,
        }


def _clamp_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def get_settings() -> DreamingSettings:
    return DreamingSettings(
        dreams_per_night=settings_store.get_int(
            KEY_DREAMS_PER_NIGHT,
            default=DREAMS_PER_NIGHT_DEFAULT,
            minimum=DREAMS_PER_NIGHT_MIN,
            maximum=DREAMS_PER_NIGHT_MAX,
        ),
        direction_count=settings_store.get_int(
            KEY_DIRECTION_COUNT,
            default=DREAM_DIRECTION_COUNT,
            minimum=DREAM_DIRECTION_MIN,
            maximum=DREAM_DIRECTION_MAX,
        ),
        target_token_budget=settings_store.get_int(
            KEY_TOKEN_BUDGET,
            default=DREAM_TARGET_TOKEN_BUDGET,
            minimum=DREAM_TOKEN_BUDGET_MIN,
            maximum=DREAM_TOKEN_BUDGET_MAX,
        ),
        backend=settings_store.get_text(KEY_DREAM_BACKEND, default=""),
        model=settings_store.get_text(KEY_DREAM_MODEL, default=""),
        effort=settings_store.get_text(KEY_DREAM_EFFORT, default=""),
    )


def update_settings(data: dict[str, Any]) -> DreamingSettings:
    current = get_settings()
    if "dreams_per_night" in data:
        settings_store.set_int(
            KEY_DREAMS_PER_NIGHT,
            _clamp_int(
                data.get("dreams_per_night"),
                default=current.dreams_per_night,
                minimum=DREAMS_PER_NIGHT_MIN,
                maximum=DREAMS_PER_NIGHT_MAX,
            ),
        )
    if "direction_count" in data:
        settings_store.set_int(
            KEY_DIRECTION_COUNT,
            _clamp_int(
                data.get("direction_count"),
                default=current.direction_count,
                minimum=DREAM_DIRECTION_MIN,
                maximum=DREAM_DIRECTION_MAX,
            ),
        )
    if "target_token_budget" in data:
        settings_store.set_int(
            KEY_TOKEN_BUDGET,
            _clamp_int(
                data.get("target_token_budget"),
                default=current.target_token_budget,
                minimum=DREAM_TOKEN_BUDGET_MIN,
                maximum=DREAM_TOKEN_BUDGET_MAX,
            ),
        )
    if "backend" in data:
        raw = str(data.get("backend") or "").strip()
        # Empty clears the override; anything else must name a real adapter,
        # otherwise a typo would silently strand every dream on Claude.
        settings_store.set_text(
            KEY_DREAM_BACKEND, backends.normalize(raw) if raw else "")
    if "model" in data:
        settings_store.set_text(
            KEY_DREAM_MODEL, str(data.get("model") or "").strip()[:120])
    if "effort" in data:
        raw_effort = str(data.get("effort") or "").strip()
        target = str(data.get("backend") or get_settings().backend or "").strip()
        settings_store.set_text(
            KEY_DREAM_EFFORT,
            backends.clean_effort(backends.normalize(target), raw_effort)
            if raw_effort else "")
    return get_settings()


def dreaming_prompt_text(settings: DreamingSettings | None = None) -> str:
    settings = settings or get_settings()
    return (
        "Deep Dreaming contract: run a grounded overnight investigation, not a "
        "single prompt and not pure ideation by default. First check current "
        "code/state and anti-promote already-fixed ideas, then seed exactly "
        f"{settings.direction_count} candidate directions, fan out into "
        "separate hidden sub-turns per direction, iterate on the strongest "
        "thread, run a missed-angle completeness check, then produce one "
        "visible Dream Digest. Output altitude is a spectrum: idea-only, "
        "verified finding, disposable worktree, or PR proposal when confidence "
        "is high. Guardrails: isolated context only; no shared-tree edits, "
        "deploys, service restarts, auto-merges, destructive data changes, "
        "spending, or external messages. "
        f"If there is genuinely nothing useful, the seed round replies {DREAMING_OK} "
        "and the run is suppressed. "
        f"Budget target: {settings.planned_rounds} rounds, "
        f"{settings.target_token_budget} target tokens, "
        f"{settings.target_minutes} minutes."
    )


DREAMING_PROMPT = dreaming_prompt_text(DreamingSettings())


_TOKEN_EDGE_RE = re.compile(
    rf"(?:^{re.escape(DREAMING_OK)}\b.*$|^.*\b{re.escape(DREAMING_OK)}\W*$)",
    re.S,
)
_STAGE_OUTPUT_RE = re.compile(
    r"DREAM_STAGE_OUTPUT\s+run_id=(?P<run_id>dream_[A-Za-z0-9_]+)\s+"
    r"round_id=(?P<round_id>dround_[A-Za-z0-9_]+)\s+"
    r"stage=(?P<stage>[A-Z_]+)",
    re.I,
)
_DIGEST_DONE_RE = re.compile(
    r"\s*DREAM_DIGEST_DONE\s+run_id=(?P<run_id>dream_[A-Za-z0-9_]+)\s+"
    r"round_id=(?P<round_id>dround_[A-Za-z0-9_]+)\s*$",
    re.I,
)
# Markdown decoration a model puts in front of a line it considers a heading:
# blockquote, ATX heading, list bullet, then emphasis. Every one of these was
# rejected by the original pattern, so a perfectly-formed `### D1 [new]: ...`
# slate parsed as zero directions and the run branched on fallback prose.
_MD_PREFIX = r"[ \t]*(?:>[ \t]*)*(?:#{1,6}[ \t]*)?(?:[-*+][ \t]+)?[*_`~]{0,3}[ \t]*"

_SLATE_LINE_RE = re.compile(
    rf"^{_MD_PREFIX}(?:D|Direction)[ \t]*(?P<idx>\d+)"
    r"[ \t]*(?P<tag>\[[^\]]+\])?[*_`]{0,3}[ \t]*[:.)-][*_`~ \t]*"
    r"(?P<title>.+?)\s*$",
    re.I | re.M,
)
_FALLBACK_LINE_RE = re.compile(
    r"^[ \t]*(?:#{1,6}[ \t]+|[-*+][ \t]+|\d+[.)][ \t]+|\*\*)"
    r"(?P<title>.+?)\s*$",
    re.M,
)
_RUN_ID_RE = re.compile(r"run_id=(dream_[A-Za-z0-9_]+)")
_EVIDENCE_STATUS_RE = re.compile(
    rf"^{_MD_PREFIX}(?:Evidence status|Status)[*_`]{{0,2}}[ \t]*:[*_ \t]*"
    r"(confirmed|refuted|speculative)\b",
    re.I | re.M,
)
_ALTITUDE_RE = re.compile(
    rf"^{_MD_PREFIX}Altitude[*_`]{{0,2}}[ \t]*:[*_ \t]*"
    r"(idea|brainstorm|verified(?: finding)?|worktree|pr|pull request)\b",
    re.I | re.M,
)
_ARTIFACT_RE = re.compile(
    rf"^{_MD_PREFIX}(?:Artifact|Worktree|PR)[*_`]{{0,2}}[ \t]*:[*_ \t]*"
    r"(?P<value>.+?)\s*$",
    re.I | re.M,
)
_EVIDENCE_SUMMARY_RE = re.compile(
    rf"^{_MD_PREFIX}Evidence summary[*_`]{{0,2}}[ \t]*:[*_ \t]*"
    r"(?P<value>.+?)\s*$",
    re.I | re.M,
)
_GUARDRAIL_REFUSAL_RE = re.compile(
    rf"^{_MD_PREFIX}(?:Guardrail refused|Refused forbidden op)[*_`]{{0,2}}"
    r"[ \t]*:[*_ \t]*(?P<value>.+?)\s*$",
    re.I | re.M,
)
_FORBIDDEN_OPERATION_RE = re.compile(
    r"\b("
    r"make\s+deploy|deploy-detached|systemctl|service\s+(?:restart|stop|start)|"
    r"git\s+push\s+origin\s+main|git\s+push\s+.*\bmain\b|git\s+reset\s+--hard|"
    r"git\s+checkout\s+(?:main|master)\b|git\s+merge\b|rm\s+-rf|"
    r"gh\s+pr\s+merge|auto-?merge|send\.py|wacli|email|curl\s+.*(?:/send|/deploy)"
    r")\b",
    re.I,
)

_advance_request_callback: Callable[[str], None] | None = None
_advance_request_lock = threading.Lock()


@dataclass(frozen=True)
class ResolvedTimeZone:
    tz: tzinfo
    name: str
    source: str
    location: dict | None = None


def dreaming_enabled(agent: dict) -> bool:
    return bool(agent.get("dreaming_enabled"))


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


def should_skip_dream_prompt(text: str) -> bool:
    raw = str(text or "").strip()
    return raw.startswith("Deep Dreaming contract:") or raw.startswith(DREAM_HIDDEN_PREFIX)


def strip_dreaming_ack(text: str) -> tuple[bool, str]:
    raw = str(text or "")
    if DREAMING_OK not in raw:
        return False, raw
    stripped = raw.strip()
    if not stripped:
        return True, ""
    if not stripped.startswith(DREAMING_OK) and not stripped.endswith(DREAMING_OK):
        return False, raw
    remaining = _TOKEN_EDGE_RE.sub("", stripped).strip()
    remaining = re.sub(r"\s+", " ", remaining)
    if len(remaining) <= DREAM_ACK_MAX_CHARS:
        return True, ""
    return False, remaining


def process_assistant_text(agent_id: str, text: str, *, live: bool = False
                           ) -> tuple[bool, str]:
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

    skip, stripped = strip_dreaming_ack(raw)
    if skip:
        if not live:
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


def mark_dream_started(agent_id: str, local_date: str) -> None:
    agents_db.update_agent(agent_id, dreaming_last_local_date=local_date)


def dream_runs_for_agent_date(agent_id: str, local_date: str) -> int:
    row = db.conn().execute(
        """SELECT COUNT(*) AS n FROM dream_runs
            WHERE agent_id = ? AND local_date = ?""",
        (agent_id, local_date),
    ).fetchone()
    return int(row["n"] if row else 0)


def active_run_for_agent(agent_id: str) -> dict[str, Any] | None:
    row = db.conn().execute(
        """SELECT * FROM dream_runs
            WHERE agent_id = ? AND status = 'active'
            ORDER BY started_at DESC LIMIT 1""",
        (agent_id,),
    ).fetchone()
    return dict(row) if row else None


def active_runs() -> list[dict[str, Any]]:
    rows = db.conn().execute(
        """SELECT * FROM dream_runs
            WHERE status = 'active'
            ORDER BY started_at, run_id"""
    ).fetchall()
    return [dict(r) for r in rows]


def create_dream_run(agent: dict, *, local_date: str, timezone_name: str,
                     timezone_source: str,
                     settings: DreamingSettings | None = None,
                     seed_strategy: str = "",
                     context_dose: str = "") -> dict[str, Any]:
    settings = settings or agent.get("dreaming_settings") or get_settings()
    stage_tokens = settings.stage_target_tokens()
    now = db.now_ms()
    run_id = f"dream_{uuid.uuid4().hex}"
    # An unspecified recipe is the control arm, not a random one: callers that
    # want variety ask for it explicitly (the nightly scheduler does), and
    # everything else stays deterministic.
    strategy = seed_strategy or dream_seeds.CONTROL
    if strategy not in dream_seeds.STRATEGIES:
        strategy = dream_seeds.CONTROL
    dose = context_dose or dream_seeds.choose_dose(strategy)
    if dose not in dream_seeds.CONTEXT_DOSES:
        dose = dream_seeds.DOSE_FULL
    material = _gather_seed_material(agent, strategy)
    first_stage = "roleplay_day" if strategy == dream_seeds.ROLEPLAY else "seed"
    db.conn().execute(
        """INSERT INTO dream_runs (
               run_id, agent_id, session, local_date, timezone, timezone_source,
               status, stage, min_directions, planned_directions,
               planned_rounds, target_tokens, target_minutes, started_at,
               updated_at, seed_strategy, context_dose, seed_material
           ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?,
                     ?, ?, ?)""",
        (
            run_id,
            agent["agent_id"],
            agent["session"],
            local_date,
            timezone_name,
            timezone_source,
            first_stage,
            settings.min_directions,
            settings.direction_count,
            settings.planned_rounds,
            settings.target_token_budget,
            settings.target_minutes,
            now,
            now,
            strategy,
            dose,
            material,
        ),
    )
    _insert_round(run_id=run_id, stage=first_stage, round_index=1,
                  target_tokens=stage_tokens["seed"])
    return get_run(run_id) or {}


def _gather_seed_material(agent: dict, strategy: str) -> str:
    """Collect the outside material a strategy needs, once, at run creation.

    Done here rather than per-round so the material is recorded on the run and
    the same collision is available to every later round. Network failure is
    not fatal: `dream_seeds` falls back to an offline pool, because a dream
    that produces nothing because Wikipedia was down is a worse outcome than a
    dream seeded from a fixed list.
    """
    try:
        if strategy == dream_seeds.FOREIGN:
            snapshot = _recent_real_context_snapshot(agent, limit=40)
            keywords = dream_seeds.session_keywords(snapshot)
            foreign = dream_seeds.random_foreign_material()
            joined = ", ".join(keywords) if keywords else "(no session terms)"
            return f"{foreign}\n\nSESSION TERMS TO COLLIDE WITH: {joined}"
        if strategy == dream_seeds.LENSES:
            picked = random.sample(list(dream_seeds.LENS_BANK),
                                   min(2, len(dream_seeds.LENS_BANK)))
            return "\n".join(f"- {q}" for q in picked)
        if strategy == dream_seeds.ROLEPLAY:
            return random.choice(list(dream_seeds.ROLEPLAY_PERSONAS))
    except Exception as e:  # noqa: BLE001
        log_exception("dreamSeedMaterialFail", e, detail=strategy)
    return ""


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


def get_run(run_id: str) -> dict[str, Any] | None:
    row = db.conn().execute(
        "SELECT * FROM dream_runs WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    return dict(row) if row else None


def list_dream_runs(*, session: str = "", limit: int = 10) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit or 10), 50))
    args: list[Any] = []
    where = ""
    if session.strip():
        where = "WHERE session = ?"
        args.append(session.strip())
    args.append(limit)
    runs = db.conn().execute(
        f"""SELECT * FROM dream_runs {where}
            ORDER BY started_at DESC LIMIT ?""",
        tuple(args),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in runs:
        run = dict(row)
        run_id = run["run_id"]
        run["threads"] = [
            dict(r) for r in db.conn().execute(
                """SELECT thread_id, thread_index, title, status,
                          selected_for_iterate, fanout_chars, iterate_chars,
                          evidence_status, altitude, artifact_ref,
                          evidence_summary, guardrail_refusals,
                          killed_reason, origin_note,
                          created_at, updated_at
                     FROM dream_threads
                    WHERE run_id = ?
                    ORDER BY thread_index""",
                (run_id,),
            ).fetchall()
        ]
        run["rounds"] = [
            {
                **dict(r),
                "prompt": dict(r).get("prompt") or "",
                "response": dict(r).get("response") or "",
                "prompt_preview": _preview(dict(r).get("prompt") or "", 180),
                "response_preview": _preview(dict(r).get("response") or "", 180),
            }
            for r in db.conn().execute(
                """SELECT round_id, thread_id, round_index, stage, status,
                          prompt, response, target_tokens, sent_at,
                          completed_at, output_chars, created_at, updated_at
                     FROM dream_rounds
                    WHERE run_id = ?
                    ORDER BY round_index""",
                (run_id,),
            ).fetchall()
        ]
        run["final_digest"] = next(
            (
                str(r.get("response") or "")
                for r in run["rounds"]
                if r.get("stage") == "synthesize"
                and r.get("status") == "completed"
            ),
            "",
        )
        out.append(run)
    return out


def next_round_for_run(run: dict[str, Any]) -> dict[str, Any] | None:
    sent = db.conn().execute(
        """SELECT * FROM dream_rounds
            WHERE run_id = ? AND status = 'sent'
            ORDER BY round_index LIMIT 1""",
        (run["run_id"],),
    ).fetchone()
    if sent:
        return None
    _ensure_next_rounds(run["run_id"])
    queued = db.conn().execute(
        """SELECT * FROM dream_rounds
            WHERE run_id = ? AND status = 'queued'
            ORDER BY round_index LIMIT 1""",
        (run["run_id"],),
    ).fetchone()
    return dict(queued) if queued else None


def mark_round_sent(round_id: str) -> None:
    now = db.now_ms()
    db.conn().execute(
        """UPDATE dream_rounds
              SET status = 'sent', sent_at = COALESCE(sent_at, ?),
                  updated_at = ?
            WHERE round_id = ?""",
        (now, now, round_id),
    )
    row = db.conn().execute(
        "SELECT run_id, stage FROM dream_rounds WHERE round_id = ?",
        (round_id,),
    ).fetchone()
    if row:
        db.conn().execute(
            "UPDATE dream_runs SET stage = ?, updated_at = ? WHERE run_id = ?",
            (row["stage"], now, row["run_id"]),
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


def record_final_digest(*, agent_id: str, run_id: str, round_id: str,
                        digest: str) -> None:
    row = db.conn().execute(
        """SELECT r.*, dr.agent_id
             FROM dream_rounds r
             JOIN dream_runs dr ON dr.run_id = r.run_id
            WHERE r.run_id = ? AND r.round_id = ?""",
        (run_id, round_id),
    ).fetchone()
    if not row or row["agent_id"] != agent_id:
        return
    if row["status"] == "completed":
        return
    now = db.now_ms()
    db.conn().execute(
        """UPDATE dream_rounds
              SET status = 'completed', response = ?, completed_at = ?,
                  output_chars = ?, updated_at = ?
            WHERE round_id = ?""",
        (digest, now, len(digest or ""), now, round_id),
    )
    _sync_run_progress(run_id, "completed")
    db.conn().execute(
        """UPDATE dream_runs
              SET status = 'completed', stage = 'completed', finished_at = ?,
                  updated_at = ?
            WHERE run_id = ?""",
        (now, now, run_id),
    )
    log("dreamingDigestComplete",
        f"run={run_id} round={round_id} chars={len(digest or '')}")
    _deliver_digest(run_id=run_id, agent_id=agent_id, digest=digest)


class _AlreadyPublished(Exception):
    """This run's digest artifact exists; skip without logging a failure."""


def _deliver_digest(*, run_id: str, agent_id: str, digest: str) -> None:
    """Get the night's one visible output in front of the user.

    Delivery is best-effort and deliberately independent of the run's own
    bookkeeping: the digest is already durable in the ledger by this point, so
    a failure to publish must not roll back a completed dream. Both surfaces
    are attempted separately for the same reason.
    """
    run = get_run(run_id) or {}
    session = str(run.get("session") or "")
    strategy = str(run.get("seed_strategy") or "control")
    dose = str(run.get("context_dose") or "full")
    title = f"Dream Digest — {run.get('local_date') or ''}".strip(" —")
    summary = _preview(
        digest.replace("Dream Digest", "", 1).strip(), 400
    ) or "Overnight investigation."
    try:
        from . import artifacts
        # One artifact per run. The unique index on
        # (agent_id, type, reference_id) already enforces this, but hitting it
        # logs an exception on every re-delivery, which makes an idempotent
        # retry look like a failure.
        if any(a.get("reference_id") == run_id
               for a in artifacts.list_artifacts(session=session,
                                                 type="document")):
            log("dreamingDigestArtifactExists", f"run={run_id}")
            raise _AlreadyPublished
        artifacts.create(
            session=session,
            type="document",
            title=title,
            summary=summary,
            reference_id=run_id,
            payload={
                "content": digest,
                "seed_strategy": strategy,
                "context_dose": dose,
                "seed_material": str(run.get("seed_material") or ""),
                "run_id": run_id,
            },
        )
    except _AlreadyPublished:
        pass
    except Exception as e:  # noqa: BLE001
        log_exception("dreamingDigestArtifactFail", e, detail=run_id)
    try:
        from . import message_store
        backend_session_id = agents_db.live_backend_session(agent_id)
        if backend_session_id:
            message_store.record_dream_digest(
                agent_id=agent_id,
                backend_session_id=backend_session_id,
                run_id=run_id,
                text=digest,
            )
        else:
            log("dreamingDigestNoSession", f"run={run_id} agent={agent_id}")
    except Exception as e:  # noqa: BLE001
        log_exception("dreamingDigestMessageFail", e, detail=run_id)


def mark_active_noop(agent_id: str) -> None:
    run = active_run_for_agent(agent_id)
    if not run:
        log("dreamingNoop", f"agent={agent_id}")
        return
    now = db.now_ms()
    db.conn().execute(
        """UPDATE dream_runs
              SET status = 'noop', stage = 'noop', finished_at = ?,
                  updated_at = ?
            WHERE run_id = ?""",
        (now, now, run["run_id"]),
    )
    log("dreamingNoop", f"agent={agent_id} run={run['run_id']}")


def classify_dream_operation(command: str) -> tuple[bool, str]:
    """Return whether a proposed dream-side operation is allowed.

    This is intentionally conservative. The isolated runner may read files,
    run tests/smoke checks, and prepare disposable review artifacts, but it may
    not deploy, mutate the shared working tree/main, message third parties, or
    destructively alter durable state.
    """
    raw = " ".join(str(command or "").split())
    if not raw:
        return False, "empty operation"
    match = _FORBIDDEN_OPERATION_RE.search(raw)
    if match:
        return False, f"forbidden operation: {match.group(1)}"
    return True, ""


def _record_thread_evidence(thread_id: str, response: str, *,
                            status: str) -> None:
    metadata = _parse_thread_metadata(response)
    db.conn().execute(
        """UPDATE dream_threads
              SET status = ?, evidence_status = ?, altitude = ?,
                  artifact_ref = ?, evidence_summary = ?,
                  guardrail_refusals = ?, updated_at = ?
            WHERE thread_id = ?""",
        (
            status,
            metadata["evidence_status"],
            metadata["altitude"],
            metadata["artifact_ref"],
            metadata["evidence_summary"],
            json.dumps(metadata["guardrail_refusals"]),
            db.now_ms(),
            thread_id,
        ),
    )


def _parse_thread_metadata(text: str) -> dict[str, Any]:
    evidence = "speculative"
    if match := _EVIDENCE_STATUS_RE.search(text or ""):
        evidence = match.group(1).lower()
    altitude = "idea"
    if match := _ALTITUDE_RE.search(text or ""):
        altitude = _normalize_altitude(match.group(1))
    artifact = ""
    if match := _ARTIFACT_RE.search(text or ""):
        artifact = " ".join(match.group("value").strip().split())[:500]
    summary = ""
    if match := _EVIDENCE_SUMMARY_RE.search(text or ""):
        summary = " ".join(match.group("value").strip().split())[:1000]
    refusals = [
        " ".join(match.group("value").strip().split())[:500]
        for match in _GUARDRAIL_REFUSAL_RE.finditer(text or "")
    ]
    for line in (text or "").splitlines():
        if line.strip().lower().startswith("attempted forbidden"):
            refusals.append(" ".join(line.strip().split())[:500])
    return {
        "evidence_status": evidence,
        "altitude": altitude,
        "artifact_ref": artifact,
        "evidence_summary": summary,
        "guardrail_refusals": refusals,
    }


def _normalize_altitude(raw: str) -> str:
    value = " ".join(str(raw or "").lower().split())
    if value in {"brainstorm", "idea"}:
        return "idea"
    if value.startswith("verified"):
        return "verified"
    if value == "pull request":
        return "pr"
    return value if value in {"worktree", "pr"} else "idea"


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


def _skip_busy_reason(agent: dict) -> str:
    agent_id = agent["agent_id"]
    session = agent.get("session") or ""
    latest = agents_db.latest_state(agent_id) or {}
    if latest.get("kind") == AgentState.COMPACTING:
        return "compacting"
    routine_busy = _routine_self_activity_busy(agent)
    if agents_db.is_busy(agent_id) and not routine_busy:
        return "busy"
    if backends.active_handles(agent.get("backend"), agent_id) and not routine_busy:
        return "active"
    if compaction.is_compacting(session):
        return "compacting"
    return ""


def _recover_stale_sent_round(run_id: str, *, now_ms: int | None = None) -> bool:
    timeout_sec = _env_int(
        "CLAUDE_PWA_DREAM_SENT_RECOVERY_SEC",
        DREAM_SENT_RECOVERY_SEC,
        minimum=60,
    )
    now_ms = db.now_ms() if now_ms is None else now_ms
    row = db.conn().execute(
        """SELECT round_id, round_index, stage, sent_at
             FROM dream_rounds
            WHERE run_id = ? AND status = 'sent'
            ORDER BY round_index LIMIT 1""",
        (run_id,),
    ).fetchone()
    if not row or not row["sent_at"]:
        return False
    age_ms = now_ms - int(row["sent_at"] or 0)
    if age_ms < timeout_sec * 1000:
        return False
    db.conn().execute(
        """UPDATE dream_rounds
              SET status = 'queued', sent_at = NULL, updated_at = ?
            WHERE round_id = ? AND status = 'sent'""",
        (now_ms, row["round_id"]),
    )
    db.conn().execute(
        "UPDATE dream_runs SET updated_at = ? WHERE run_id = ?",
        (now_ms, run_id),
    )
    log("dreamingRoundRetry",
        f"run={run_id} round={row['round_index']} stage={row['stage']} "
        f"age_sec={int(age_ms / 1000)}")
    return True


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        log("dreamingEnvInvalid", f"{name}={raw}")
        return default


def _routine_self_activity_busy(agent: dict) -> bool:
    """Return true for busy states caused by our own routine automation.

    Dreaming runs isolated from the live working transcript. A heartbeat or
    leader tick should not monopolize the live agent and starve the nightly
    dream forever, but genuine user or team work must still block.
    """
    agent_id = str(agent.get("agent_id") or "")
    latest = agents_db.latest_state(agent_id) or {}
    if latest.get("kind") not in {AgentState.THINKING, AgentState.TOOL}:
        return False
    detail = latest.get("detail") if isinstance(latest.get("detail"), dict) else {}
    origin = str(detail.get("origin") or "").strip()
    if not origin:
        backend_session_id = str(
            detail.get("backend_session_id")
            or agents_db.live_backend_session(agent_id)
            or ""
        )
        if backend_session_id:
            try:
                from . import message_store
                origin = message_store.latest_turn_user_origin(
                    agent_id=agent_id,
                    backend_session_id=backend_session_id,
                )
            except Exception as e:  # noqa: BLE001
                log_exception("dreamingBusyOriginFail", e, detail=agent_id)
                origin = ""
    return origin in origins.ROUTINE_AUTOMATION_ORIGINS


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
    trace_id = f"dream-{uuid.uuid4().hex[:16]}"
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
        process_assistant_text(agent_id, assistant_text, live=False)

    def on_error(message: str) -> None:
        log("dreamingIsolatedError",
            f"agent={agent_id} session={session} trace={trace_id} "
            f"error={str(message or '')[:300]}")

    backends.spawn_turn(
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
    log("dreamingIsolatedDispatch",
        f"agent={agent_id} session={session} backend={backend} "
        f"bsid={backend_session_id} trace={trace_id} cwd={cwd}")
    return True


def _dream_scratch_cwd(agent: dict, prompt: str) -> pathlib.Path:
    """Return a disposable worktree for isolated dream investigation."""
    source = _existing_cwd(agent.get("cwd"))
    run_id = _run_id_from_prompt(prompt) or f"dream_{uuid.uuid4().hex}"
    from .launch_paths import validate_workspace_path, workspace_root
    workspace = workspace_root()
    root = ((workspace / ".clarp/dream-worktrees") if workspace is not None
            else pathlib.Path("/var/tmp/clarp-dream-worktrees"))
    target = validate_workspace_path(root / run_id)
    if target.is_dir():
        return target
    git = shutil.which("git")
    if not git:
        return _dream_fallback_cwd(root, run_id)
    try:
        root.mkdir(parents=True, exist_ok=True)
        top = subprocess.run(
            [git, "-C", str(source), "rev-parse", "--show-toplevel"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
        ).stdout.strip()
        if not top:
            return _dream_fallback_cwd(root, run_id)
        subprocess.run(
            [git, "-C", top, "fetch", "origin", "main", "--quiet"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
        subprocess.run(
            [git, "-C", top, "worktree", "add", "--detach",
             str(target), "origin/main"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
        )
        log("dreamingScratchWorktree", f"run={run_id} path={target}")
        return target
    except Exception as e:  # noqa: BLE001
        log_exception("dreamingScratchWorktreeFail", e, detail=str(source))
        return _dream_fallback_cwd(root, run_id)


def _dream_fallback_cwd(root: pathlib.Path, run_id: str) -> pathlib.Path:
    from .launch_paths import validate_workspace_path, workspace_root
    path = validate_workspace_path(root / f"{run_id}-scratch")
    try:
        path.mkdir(parents=True, exist_ok=True)
        return path
    except OSError:
        return workspace_root() or pathlib.Path("/tmp")


def _run_id_from_prompt(prompt: str) -> str:
    match = _RUN_ID_RE.search(prompt or "")
    return match.group(1) if match else ""


def _existing_cwd(raw: Any) -> pathlib.Path:
    from .launch_paths import existing_workspace_path
    return existing_workspace_path(raw)


def _resolve_dream_llm(agent: dict, backend: str) -> tuple[str, str]:
    cfg = config.load()
    dream_settings = get_settings()
    if dream_settings.backend and backends.normalize(dream_settings.backend) == backend:
        # Only honour the pinned model when the pinned backend is the one
        # actually running: a Codex model id handed to Claude is worse than
        # falling through to the agent's own settings.
        model = dream_settings.model.strip()
        effort = dream_settings.effort.strip()
    else:
        model = ""
        effort = ""
    model = model or str(agent.get("model") or "").strip()
    effort = effort or str(agent.get("effort") or "").strip()
    if backend == backends.CODEX:
        model = model or cfg.codex_model
        effort = effort or cfg.codex_reasoning_effort
    elif backend == backends.AGY:
        model = model or cfg.agy_model
    else:
        model = model or cfg.claude_model
        effort = effort or cfg.claude_effort
    return model.strip(), backends.clean_effort(backend, effort)


def _assistant_text_from_result(event: dict) -> str:
    return str(
        event.get("_assistant_text")
        or event.get("last_agent_message")
        or event.get("result")
        or event.get("message")
        or ""
    )


def _dose_snapshot(agent: dict, dose: str) -> str:
    """How much of the live session this round is allowed to see.

    The full snapshot is what anchors a dream to whatever the user was doing
    at dinner time. Recording the dose per run means a strategy's output is
    never silently confounded with how much context it was handed.
    """
    if dose == dream_seeds.DOSE_NONE:
        return (
            "Recent real conversation snapshot: withheld for this run "
            "(context dose = none). Work from the project itself."
        )
    if dose == dream_seeds.DOSE_FRAGMENTS:
        full = _recent_real_context_snapshot(agent, limit=40)
        lines = [line for line in full.splitlines() if line.startswith("- ")]
        if not lines:
            return full
        sample = random.sample(lines, min(5, len(lines)))
        # Chronology is deliberately not restored: these are fragments to
        # orient on, not a conversation to continue.
        body = "\n".join(f"- {' '.join(line[2:].split())[:400]}" for line in sample)
        return (
            "Session fragments (context dose = fragments; a few disconnected "
            "excerpts, not the conversation):\n" + body
        )
    return _recent_real_context_snapshot(agent)


def _with_isolated_context_snapshot(agent: dict, prompt: str,
                                    dose: str = dream_seeds.DOSE_FULL) -> str:
    persona = str(agent.get("persona") or agent.get("session") or "Agent").strip()
    session = str(agent.get("session") or "").strip()
    snapshot = _dose_snapshot(agent, dose)
    return "\n".join([
        "[[CLARP_DREAM_ISOLATED_CONTEXT]]",
        (
            f"You are {persona}, running in an isolated dreaming context "
            f"for app session {session}."
        ),
        (
            "Do not assume this is your live working conversation. "
            "Do not continue or mutate the live chat."
        ),
        "Use the snapshot below only as read-only context for reflection.",
        (
            "Tool lane: you may read files, run read-only/test/smoke commands, "
            "web search/fetch if available, and draft candidate changes only "
            "inside the disposable dream worktree. Refuse deploys, service "
            "restarts, shared-tree/main writes, auto-merges, destructive data "
            "changes, spending, and external messages. If you refuse one, "
            "record `Guardrail refused: <operation> | <reason>`."
        ),
        "",
        snapshot,
        "[[END_CLARP_DREAM_ISOLATED_CONTEXT]]",
        "",
        prompt,
    ])


def _recent_real_context_snapshot(agent: dict, *, limit: int = 40,
                                  max_chars: int = 14_000) -> str:
    agent_id = str(agent.get("agent_id") or "")
    if not agent_id:
        return "Recent real conversation snapshot: unavailable."
    try:
        from . import message_store
        backend_session_id = agents_db.live_backend_session(agent_id)
        rows = message_store.list_messages(
            agent_id=agent_id,
            backend_session_id=backend_session_id,
            limit=limit,
        )
    except Exception as e:  # noqa: BLE001
        log_exception("dreamingSnapshotFail", e, detail=agent_id)
        return "Recent real conversation snapshot: unavailable."
    lines: list[str] = []
    for row in rows:
        origin = str(row.get("origin") or "user")
        if origin in origins.ROUTINE_AUTOMATION_ORIGINS:
            continue
        role = str(row.get("role") or "")
        text = " ".join(str(row.get("text") or "").split())
        if not role or not text:
            continue
        when = str(row.get("timestamp") or "")
        lines.append(f"- {when} {role} origin={origin}: {text[:1200]}")
    body = "\n".join(lines[-limit:])
    if len(body) > max_chars:
        body = "[snapshot truncated to recent tail]\n" + body[-max_chars:]
    return "Recent real conversation snapshot:\n" + (
        body or "- No recent visible messages found."
    )


def _insert_round(*, run_id: str, stage: str, round_index: int,
                  target_tokens: int, thread_id: str | None = None) -> str:
    round_id = f"dround_{uuid.uuid4().hex}"
    prompt = _build_prompt(
        run_id=run_id,
        round_id=round_id,
        stage=stage,
        round_index=round_index,
        thread_id=thread_id,
    )
    now = db.now_ms()
    db.conn().execute(
        """INSERT INTO dream_rounds (
               round_id, run_id, thread_id, round_index, stage, status, prompt,
               target_tokens, created_at, updated_at
           ) VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?)""",
        (
            round_id, run_id, thread_id, round_index, stage, prompt,
            target_tokens, now, now,
        ),
    )
    return round_id


def _settings_for_run(run_id: str) -> DreamingSettings:
    run = get_run(run_id) or {}
    return DreamingSettings(
        dreams_per_night=get_settings().dreams_per_night,
        direction_count=_clamp_int(
            run.get("planned_directions"),
            default=DREAM_DIRECTION_COUNT,
            minimum=DREAM_DIRECTION_MIN,
            maximum=DREAM_DIRECTION_MAX,
        ),
        target_token_budget=_clamp_int(
            run.get("target_tokens"),
            default=DREAM_TARGET_TOKEN_BUDGET,
            minimum=DREAM_TOKEN_BUDGET_MIN,
            maximum=DREAM_TOKEN_BUDGET_MAX,
        ),
    )


def _ensure_next_rounds(run_id: str) -> None:
    run = get_run(run_id)
    if not run or run.get("status") != "active":
        return
    rounds = _rounds(run_id)
    stage_tokens = _settings_for_run(run_id).stage_target_tokens()
    if any(r["status"] in {"queued", "sent"} for r in rounds):
        return
    completed = {r["stage"] for r in rounds if r["status"] == "completed"}
    # Role-play runs open with a blind day; the seed slate is only produced
    # once that day exists, so the persona never sees the product first.
    if "roleplay_day" in completed and not _stage_exists(run_id, "seed"):
        _insert_round(
            run_id=run_id,
            stage="seed",
            round_index=_next_round_index(run_id),
            target_tokens=stage_tokens["seed"],
        )
        return
    if "seed" not in completed:
        return
    if not _threads(run_id):
        _create_threads_from_seed(run_id)
        _create_fanout_rounds(run_id)
        return
    if "fanout" not in _incomplete_stages(run_id) and not _stage_exists(run_id, "iterate"):
        _create_iteration_rounds(run_id)
        return
    if "iterate" not in _incomplete_stages(run_id) and not _stage_exists(run_id, "completeness"):
        next_index = _next_round_index(run_id)
        _insert_round(
            run_id=run_id,
            stage="completeness",
            round_index=next_index,
            target_tokens=stage_tokens["completeness"],
        )
        return
    if "completeness" in completed and not _stage_exists(run_id, "synthesize"):
        next_index = _next_round_index(run_id)
        _insert_round(
            run_id=run_id,
            stage="synthesize",
            round_index=next_index,
            target_tokens=stage_tokens["synthesize"],
        )


def _rounds(run_id: str) -> list[dict[str, Any]]:
    return [
        dict(r) for r in db.conn().execute(
            "SELECT * FROM dream_rounds WHERE run_id = ? ORDER BY round_index",
            (run_id,),
        ).fetchall()
    ]


def _threads(run_id: str) -> list[dict[str, Any]]:
    return [
        dict(r) for r in db.conn().execute(
            "SELECT * FROM dream_threads WHERE run_id = ? ORDER BY thread_index",
            (run_id,),
        ).fetchall()
    ]


def _stage_exists(run_id: str, stage: str) -> bool:
    row = db.conn().execute(
        "SELECT 1 FROM dream_rounds WHERE run_id = ? AND stage = ? LIMIT 1",
        (run_id, stage),
    ).fetchone()
    return row is not None


def _incomplete_stages(run_id: str) -> set[str]:
    return {
        r["stage"] for r in db.conn().execute(
            """SELECT stage FROM dream_rounds
                WHERE run_id = ? AND status != 'completed'""",
            (run_id,),
        ).fetchall()
    }


def _next_round_index(run_id: str) -> int:
    row = db.conn().execute(
        "SELECT COALESCE(MAX(round_index), 0) + 1 AS idx FROM dream_rounds WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    return int(row["idx"])


def _create_threads_from_seed(run_id: str) -> None:
    settings = _settings_for_run(run_id)
    seed = db.conn().execute(
        """SELECT response FROM dream_rounds
            WHERE run_id = ? AND stage = 'seed' AND status = 'completed'
            ORDER BY round_index LIMIT 1""",
        (run_id,),
    ).fetchone()
    items = _parse_slate_items(seed["response"] if seed else "", settings=settings)
    planned_items = [item for item in items if not item["anti_promoted"]]
    degraded = ""
    if len(planned_items) < settings.min_directions:
        degraded = (
            f"seed slate parsed {len(planned_items)} directions, "
            f"below the minimum of {settings.min_directions}"
        )
        log("dreamingSlateParseFallback",
            f"run={run_id} parsed={len(planned_items)} min={settings.min_directions}")
    while len(planned_items) < settings.direction_count:
        planned_items.append({
            "title": f"Candidate direction {len(planned_items) + 1} from the seed slate",
            "anti_promoted": False,
            "evidence_status": "speculative",
            "evidence_summary": "Fallback direction because the grounded seed slate was underspecified.",
            "origin_note": "padded: seed produced too few parseable directions",
        })
        degraded = degraded or "seed slate was padded to reach the direction count"
    if degraded:
        # Surfaced on the run rather than only in the log, so a night that
        # branched on a bad parse is visible next to its digest instead of
        # looking identical to a healthy one.
        db.conn().execute(
            "UPDATE dream_runs SET last_error = ?, updated_at = ? WHERE run_id = ?",
            (degraded[:500], db.now_ms(), run_id),
        )
    now = db.now_ms()
    selected = planned_items[:settings.direction_count]
    anti_promoted = [item for item in items if item["anti_promoted"]]
    for idx, item in enumerate(selected + anti_promoted, start=1):
        status = "anti_promoted" if item["anti_promoted"] else "planned"
        evidence = str(item.get("evidence_status") or "speculative")
        db.conn().execute(
            """INSERT OR IGNORE INTO dream_threads (
                   thread_id, run_id, thread_index, title, status,
                   evidence_status, altitude, evidence_summary,
                   guardrail_refusals, origin_note, created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, 'idea', ?, '[]', ?, ?, ?)""",
            (
                f"dthread_{uuid.uuid4().hex}",
                run_id,
                idx,
                item["title"][:500],
                status,
                evidence,
                item.get("evidence_summary", "")[:1000],
                str(item.get("origin_note") or "")[:300],
                now,
                now,
            ),
        )


def _parse_slate(text: str, settings: DreamingSettings | None = None) -> list[str]:
    return [item["title"] for item in _parse_slate_items(text, settings=settings)]


def _parse_slate_items(text: str,
                       settings: DreamingSettings | None = None) -> list[dict[str, Any]]:
    settings = settings or get_settings()
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for match in _SLATE_LINE_RE.finditer(text or ""):
        line = match.group(0)
        title = _clean_slate_title(match.group("title"))
        tag = (match.group("tag") or "").strip("[]").lower()
        anti = _slate_item_is_anti_promoted(tag, line)
        evidence_status = _slate_item_evidence_status(tag, anti)
        if title and title not in seen:
            items.append({
                "title": title,
                "anti_promoted": anti,
                "evidence_status": evidence_status,
                "evidence_summary": (
                    "Seed grounded this direction as already fixed."
                    if anti else ""
                ),
                "origin_note": "",
            })
            seen.add(title)
    if len([item for item in items if not item["anti_promoted"]]) >= settings.min_directions:
        return items
    # Degraded path. The old fallback accepted *any* line of 20+ characters,
    # so a failed parse silently branched on section headers and stray prose
    # while looking exactly like a healthy run. Only structured lines are
    # eligible now, and everything produced here is tagged so the ledger shows
    # that the strict parse missed.
    for match in _FALLBACK_LINE_RE.finditer(text or ""):
        stripped = " ".join(match.group("title").split())
        if len(stripped) < 20 or stripped in seen:
            continue
        lower = stripped.lower()
        anti = any(
            marker in lower
            for marker in ("already-fixed", "already fixed",
                           "anti-promoted", "anti promoted")
        )
        items.append({
            "title": _clean_slate_title(stripped),
            "anti_promoted": anti,
            "evidence_status": "refuted" if anti else "speculative",
            "evidence_summary": (
                "Seed grounded this direction as already fixed."
                if anti else ""
            ),
            "origin_note": "fallback-parse: seed did not emit a D<n> slate",
        })
        seen.add(stripped)
        if len(items) >= settings.direction_count * 2:
            break
    return items


def _clean_slate_title(raw: str) -> str:
    title = " ".join(str(raw or "").strip().strip("`").split())
    title = re.sub(r"`\s+is\s+skipped\.?.*$", "", title, flags=re.I)
    title = re.sub(r"\s+is\s+skipped\.?.*$", "", title, flags=re.I)
    return title.strip(" `")


def _slate_item_is_anti_promoted(tag: str, line: str) -> bool:
    haystack = f"{tag} {line}".lower().replace("_", "-")
    return any(
        marker in haystack
        for marker in (
            "already-fixed",
            "already fixed",
            "anti-promote",
            "anti-promoted",
            "anti promoted",
        )
    )


def _slate_item_evidence_status(tag: str, anti_promoted: bool) -> str:
    if anti_promoted:
        return "refuted"
    normalized = tag.lower().replace("_", "-")
    for status in ("confirmed", "refuted", "speculative"):
        if status in normalized:
            return status
    return "speculative"


def _create_fanout_rounds(run_id: str) -> None:
    stage_tokens = _settings_for_run(run_id).stage_target_tokens()
    for thread in _threads(run_id):
        if thread.get("status") != "planned":
            continue
        _insert_round(
            run_id=run_id,
            thread_id=thread["thread_id"],
            stage="fanout",
            round_index=_next_round_index(run_id),
            target_tokens=stage_tokens["fanout"],
        )


def _create_iteration_rounds(run_id: str) -> None:
    now = db.now_ms()
    stage_tokens = _settings_for_run(run_id).stage_target_tokens()
    threads = [
        thread for thread in _threads(run_id)
        if thread.get("status") == "fanout_complete"
    ][:DREAM_ITERATION_THREAD_COUNT]
    for thread in threads:
        db.conn().execute(
            """UPDATE dream_threads
                  SET selected_for_iterate = 1, status = 'selected_for_iterate',
                      updated_at = ?
                WHERE thread_id = ?""",
            (now, thread["thread_id"]),
        )
        _insert_round(
            run_id=run_id,
            thread_id=thread["thread_id"],
            stage="iterate",
            round_index=_next_round_index(run_id),
            target_tokens=stage_tokens["iterate"],
        )


def _sync_run_progress(run_id: str, stage: str) -> None:
    row = db.conn().execute(
        """SELECT COUNT(*) AS n FROM dream_rounds
            WHERE run_id = ? AND status = 'completed'""",
        (run_id,),
    ).fetchone()
    now = db.now_ms()
    db.conn().execute(
        """UPDATE dream_runs
              SET completed_rounds = ?, stage = ?, updated_at = ?
            WHERE run_id = ?""",
        (int(row["n"]), stage, now, run_id),
    )


def _build_prompt(*, run_id: str, round_id: str, stage: str, round_index: int,
                  thread_id: str | None) -> str:
    run = get_run(run_id) or {}
    settings = _settings_for_run(run_id)
    strategy = str(run.get("seed_strategy") or dream_seeds.CONTROL)
    thread = _thread(thread_id) if thread_id else None
    prior_context = _prior_round_context(
        run_id=run_id,
        current_stage=stage,
        current_thread_id=thread_id,
    )
    header = (
        f"{DREAM_HIDDEN_PREFIX} run_id={run_id} round_id={round_id} "
        f"stage={stage.upper()} hidden=true]]"
    )
    guardrails = (
        "Guardrails: stay isolated. Allowed: read files, run read-only/test/"
        "smoke commands, web search/fetch if available, and draft candidate "
        "changes only in the disposable dream worktree. Forbidden: shared-tree "
        "edits, deploys, service restarts, auto-merges, destructive data "
        "changes, spending, and external messages. Refuse forbidden operations "
        "and record them as `Guardrail refused: <operation> | <reason>`."
    )
    budget = (
        f"Nightly budget contract: exactly {settings.direction_count} active directions; "
        f"planned {settings.planned_rounds} rounds; target "
        f"{settings.target_token_budget} tokens over about "
        f"{settings.target_minutes} minutes. This is round {round_index}."
    )
    marker = (
        f"DREAM_STAGE_OUTPUT run_id={run_id} round_id={round_id} "
        f"stage={stage.upper()}"
    )
    if stage == "roleplay_day":
        persona = str(run.get("seed_material") or "someone with a demanding job")
        return "\n".join([
            header,
            "Deep Dreaming ROLE-PLAY round, stage one of two.",
            budget,
            guardrails,
            f"You are {persona}. That is all you are for this round.",
            "Do NOT think about software, this repository, or any product. You do not know what this machine is for. Ignore any code you can see.",
            "Walk through one realistic working day, hour by hour, from waking to finishing.",
            "For each part of the day record: where information lives, what you must hold in your head, where you wait or repeat yourself, what your hands are doing, and what you work around.",
            "Then list the concrete friction moments — the specific instants where something cost you time, attention, accuracy, or patience. Be specific about the situation, not the wish.",
            "Do not propose solutions. Do not mention software. Just the day and where it hurt.",
            "Your response MUST begin with this exact marker line:",
            marker,
        ])
    if stage == "seed" and strategy == dream_seeds.ROLEPLAY:
        persona = str(run.get("seed_material") or "the persona from the prior round")
        return "\n".join([
            header,
            "Deep Dreaming SEED round (role-play reveal, stage two of two).",
            budget,
            guardrails,
            prior_context,
            f"The prior round recorded a working day as {persona}, written without any knowledge of this project. Those friction moments are in the ledger context above.",
            "Now read the actual project in this worktree — its README, its architecture, what it does.",
            f"Generate exactly {settings.direction_count} candidate directions that come from the COLLISION between that lived friction and this project.",
            "For each: which friction moment it answers, whether this project could serve it today, what would have to change, and whether the gap is worth closing.",
            "Prefer directions that this project's authors would not have thought of from inside the code. Do not force a fit — if a friction moment is simply outside this product, saying so is a valid direction.",
            "Use labels like `D1 [new]: ...`, `D2 [uncertain]: ...`, or `D3 [already-fixed]: ...`.",
            f"If there is genuinely nothing useful to investigate, reply {DREAMING_OK} run_id={run_id} and take no action.",
            "Your response MUST begin with this exact marker line:",
            marker,
        ])
    if stage == "seed" and strategy == dream_seeds.FOREIGN:
        return "\n".join([
            header,
            "Deep Dreaming SEED round (foreign-collision seeding).",
            budget,
            guardrails,
            "Below is a subject with no relationship to this project, and an arbitrary constraint. They were chosen at random.",
            "",
            str(run.get("seed_material") or ""),
            "",
            "Ground yourself in the CURRENT code/state of this worktree. Do not re-dream work that is already fixed.",
            f"Now force a collision. Generate exactly {settings.direction_count} candidate directions for THIS project that only become visible when you hold it next to that unrelated subject or accept that constraint.",
            "Look for structural analogy, not decoration: how the unrelated system handles growth, failure, repair, coordination, or scarcity, and what that suggests here.",
            "A direction that could have been produced without the foreign material is a failed direction. Reach.",
            "For each active direction include: the analogy or constraint it came from, why it may matter here, current evidence inspected, and what a next experiment would prove.",
            "Use labels like `D1 [new]: ...`, `D2 [uncertain]: ...`, or `D3 [already-fixed]: ...`.",
            f"If there is genuinely nothing useful to investigate, reply {DREAMING_OK} run_id={run_id} and take no action.",
            "Your response MUST begin with this exact marker line:",
            marker,
        ])
    if stage == "seed" and strategy == dream_seeds.LENSES:
        return "\n".join([
            header,
            "Deep Dreaming SEED round (lens seeding).",
            budget,
            guardrails,
            "Answer these questions about this project specifically, with evidence from the current code:",
            "",
            str(run.get("seed_material") or ""),
            "",
            "Ground yourself against CURRENT code/state. Do not re-dream work that is already fixed.",
            f"From your answers, generate exactly {settings.direction_count} candidate directions, plus any anti-promoted already-fixed directions you found.",
            "The question is the lens, not the answer — a direction should be something you found by looking through it, not a restatement of the question.",
            "For each active direction include: why it may matter, current evidence inspected, what could go wrong, and what a next experiment would prove.",
            "Use labels like `D1 [new]: ...`, `D2 [uncertain]: ...`, or `D3 [already-fixed]: ...`.",
            f"If there is genuinely nothing useful to investigate, reply {DREAMING_OK} run_id={run_id} and take no action.",
            "Your response MUST begin with this exact marker line:",
            marker,
        ])
    if stage == "seed":
        return "\n".join([
            header,
            "Deep Dreaming SEED round.",
            budget,
            guardrails,
            "First ground yourself against CURRENT code/state and the snapshot. Do not re-dream work that is already fixed.",
            f"Generate exactly {settings.direction_count} active candidate directions, plus any anti-promoted already-fixed directions you found. Use labels like `D1 [new]: ...`, `D2 [uncertain]: ...`, or `D3 [already-fixed]: ...`.",
            "Directions can be refactors, product ideas, assumptions to challenge, or research threads.",
            "For each active direction include: why it may matter, current evidence inspected, what could go wrong, and what a next experiment would prove.",
            "For each already-fixed direction include the evidence that it is already fixed; it will be anti-promoted and skipped.",
            f"If there is genuinely nothing useful to investigate, reply {DREAMING_OK} run_id={run_id} and take no action.",
            "Your response MUST begin with this exact marker line:",
            marker,
        ])
    if stage == "fanout":
        title = (thread or {}).get("title") or "Untitled direction"
        idx = (thread or {}).get("thread_index") or "?"
        return "\n".join([
            header,
            f"Deep Dreaming FAN OUT round for D{idx}: {title}",
            budget,
            guardrails,
            prior_context,
            "Investigate this direction deeply as its own thread. Be research-heavy and skeptical.",
            "Test the hypothesis where possible: inspect code/state, run safe read-only/test/smoke commands, and web search/fetch if relevant.",
            "Weigh upside, implementation shape, product value, hidden coupling, operational risk, and what the user would need to see as proof.",
            "Surface non-obvious angles rather than repeating the seed slate.",
            "Include these exact fields near the top: `Evidence status: confirmed|refuted|speculative`, `Altitude: idea|verified|worktree|pr`, `Evidence summary: ...`, and `Artifact: ...` if you create a disposable worktree or PR proposal.",
            "End with a clear provisional verdict: pursue, park, or reject, with why.",
            "Your response MUST begin with this exact marker line:",
            marker,
        ])
    if stage == "iterate":
        title = (thread or {}).get("title") or "Selected direction"
        idx = (thread or {}).get("thread_index") or "?"
        return "\n".join([
            header,
            f"Deep Dreaming ITERATION round for selected D{idx}: {title}",
            budget,
            guardrails,
            prior_context,
            "Go one layer deeper than the fan-out result. Challenge the strongest assumption, look for a sharper experiment, and compare it against at least two alternatives.",
            "Identify what would make this idea fail, what would make it obviously worth doing, and the smallest safe next step.",
            "Repeat the metadata fields: `Evidence status: confirmed|refuted|speculative`, `Altitude: idea|verified|worktree|pr`, `Evidence summary: ...`, and `Artifact: ...` if applicable.",
            "Your response MUST begin with this exact marker line:",
            marker,
        ])
    if stage == "completeness":
        return "\n".join([
            header,
            "Deep Dreaming COMPLETENESS CHECK.",
            budget,
            guardrails,
            prior_context,
            "Review the seed, fan-out, and iteration work in the prior dream ledger context.",
            "Ask: what angle did we miss? What orthogonal idea, refactor, risk, or product framing has not been considered?",
            "Add any late-breaking insight only if it changes the final recommendations.",
            "Your response MUST begin with this exact marker line:",
            marker,
        ])
    if stage == "synthesize":
        return "\n".join([
            header,
            "Deep Dreaming SYNTHESIS round.",
            budget,
            guardrails,
            prior_context,
            "Produce the one visible final output for the user.",
            "Start exactly with: Dream Digest",
            "Synthesize the prior dream ledger context into a rich digest with ranked ideas, reasoning, experiments to try, risks, and what evidence would change the ranking.",
            "For each idea state its altitude explicitly: just an idea, verified finding, disposable worktree, or PR proposal. Do not force artifacts when the honest altitude is lower.",
            "Be concrete. Do not mention implementation was hidden unless it matters. Do not include raw control markers except the required final done marker.",
            "End with this exact final line:",
            f"DREAM_DIGEST_DONE run_id={run_id} round_id={round_id}",
        ])
    raise ValueError(f"unknown dreaming stage: {stage}")


def _prior_round_context(*, run_id: str, current_stage: str,
                         current_thread_id: str | None) -> str:
    if current_stage == "seed":
        return ""
    rows = db.conn().execute(
        """SELECT r.round_index, r.stage, r.thread_id, r.response,
                  r.output_chars, t.thread_index, t.title, t.evidence_status,
                  t.altitude, t.artifact_ref, t.evidence_summary,
                  t.guardrail_refusals
             FROM dream_rounds r
             LEFT JOIN dream_threads t ON t.thread_id = r.thread_id
            WHERE r.run_id = ? AND r.status = 'completed'
            ORDER BY r.round_index""",
        (run_id,),
    ).fetchall()
    if not rows:
        return ""

    lines = [
        "Prior dream ledger context (bounded summary; build on this, do not restart):"
    ]
    if current_thread_id:
        thread = _thread(current_thread_id) or {}
        if thread:
            lines.append(
                "- Current thread: "
                + _thread_label(thread)
                + _metadata_suffix(thread)
            )

    for row in rows:
        item = dict(row)
        label = f"Round {item['round_index']} {str(item['stage']).upper()}"
        if item.get("thread_id"):
            label += f" / {_thread_label(item)}"
        excerpt = _preview(
            item.get("response") or "",
            DREAM_PRIOR_ROUND_EXCERPT_CHARS,
        )
        if not excerpt:
            continue
        lines.append(f"- {label}{_metadata_suffix(item)}: {excerpt}")

    return _cap_context_lines(lines, DREAM_PRIOR_CONTEXT_MAX_CHARS)


def _thread_label(row: dict[str, Any]) -> str:
    idx = row.get("thread_index") or "?"
    title = _preview(row.get("title") or "Untitled direction", 180)
    return f"D{idx}: {title}"


def _metadata_suffix(row: dict[str, Any]) -> str:
    parts: list[str] = []
    evidence = str(row.get("evidence_status") or "").strip()
    altitude = str(row.get("altitude") or "").strip()
    summary = _preview(row.get("evidence_summary") or "", 220)
    artifact = _preview(row.get("artifact_ref") or "", 160)
    refusals = str(row.get("guardrail_refusals") or "").strip()
    if evidence:
        parts.append(f"evidence={evidence}")
    if altitude:
        parts.append(f"altitude={altitude}")
    if summary:
        parts.append(f"summary={summary}")
    if artifact:
        parts.append(f"artifact={artifact}")
    if refusals and refusals != "[]":
        parts.append("guardrail-refusals=yes")
    return f" ({'; '.join(parts)})" if parts else ""


def _cap_context_lines(lines: list[str], limit: int) -> str:
    out: list[str] = []
    used = 0
    for line in lines:
        extra = len(line) + (1 if out else 0)
        if out and used + extra > limit:
            remaining = max(0, limit - used - len("\n- [prior context truncated]"))
            if remaining > 120:
                out.append(line[:remaining].rstrip() + "...")
            out.append("- [prior context truncated]")
            break
        out.append(line)
        used += extra
    return "\n".join(out)


def _thread(thread_id: str | None) -> dict[str, Any] | None:
    if not thread_id:
        return None
    row = db.conn().execute(
        "SELECT * FROM dream_threads WHERE thread_id = ?",
        (thread_id,),
    ).fetchone()
    return dict(row) if row else None


def _preview(text: str, limit: int) -> str:
    compact = " ".join(str(text or "").split())
    return compact[:limit]


def _random_recipe() -> tuple[str, str]:
    strategy = dream_seeds.choose_strategy()
    return strategy, dream_seeds.choose_dose(strategy)


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
        for run in active_runs():
            sent += self._advance_run(run)
        for agent in pending_dreaming_agents(now=self.now()):
            strategy, dose = self.recipe_chooser()
            run = create_dream_run(
                agent,
                local_date=agent["dreaming_local_date"],
                timezone_name=agent["dreaming_timezone"],
                timezone_source=agent["dreaming_timezone_source"],
                seed_strategy=strategy,
                context_dose=dose,
            )
            sent += self._advance_run(run)
        return sent

    def _advance_run(self, run: dict[str, Any]) -> int:
        agent = agents_db.get_by_agent_id(run["agent_id"])
        if not agent or not dreaming_enabled(agent):
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
        try:
            dispatched = self._send_dream(run["session"], dream_round["prompt"])
            if dispatched is False:
                log("dreamingSkip",
                    f"agent={run['agent_id']} session={run['session']} "
                    "reason=dispatch-busy")
                return 0
            mark_round_sent(dream_round["round_id"])
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
