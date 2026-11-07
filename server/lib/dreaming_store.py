"""Moved out of server/lib/dreaming.py by tools/refactor/split_apply.py (split 'dreaming', recipe tools/refactor/splits/s9-dreaming.toml).
dreaming.py imports these names back. Generated: change the recipe, not this file."""

from __future__ import annotations

import json
import os
import pathlib
import random
import re
import threading
import uuid
from typing import Any
from . import agents as agents_db
from . import backends, config, db, dream_seeds, origins
from . import settings_store
from .log import log, log_exception
from .protocol import AgentState
from .dreaming_settings import DREAMING_OK, DREAM_ACK_MAX_CHARS, DREAM_HIDDEN_PREFIX, DREAM_ITERATION_THREAD_COUNT, DREAM_SENT_RECOVERY_SEC, DreamingSettings, _ALTITUDE_RE, _ARTIFACT_RE, _EVIDENCE_STATUS_RE, _EVIDENCE_SUMMARY_RE, _GUARDRAIL_REFUSAL_RE, _TOKEN_EDGE_RE, get_settings
from .dreaming_run_lookup import _settings_for_run, get_run
from .dreaming_worktree import _dream_worktree_path, harvest_worktree
from .dreaming_prompt import _build_prompt, _parse_slate_items, _preview


_advance_request_lock = threading.Lock()


def dreaming_enabled(agent: dict) -> bool:
    return bool(agent.get("dreaming_enabled"))


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


class ActiveDreamRunExists(ValueError):
    """The agent already has an active dream run (AUT-11)."""


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
    # One active run per agent, decided by this one statement: a check before
    # it (a manual start, the nightly loop) can be overtaken by the other.
    inserted = db.conn().execute(
        """INSERT INTO dream_runs (
               run_id, agent_id, session, local_date, timezone, timezone_source,
               status, stage, min_directions, planned_directions,
               planned_rounds, target_tokens, target_minutes, started_at,
               updated_at, seed_strategy, context_dose, seed_material
           ) SELECT ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?
              WHERE NOT EXISTS (SELECT 1 FROM dream_runs
                                 WHERE agent_id = ? AND status = 'active')""",
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
            agent["agent_id"],
        ),
    ).rowcount
    if not inserted:
        raise ActiveDreamRunExists(
            f"{agent.get('session')} already has an active dream run")
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


def claim_round(round_id: str) -> int:
    """Take a queued round for sending (AUT-11): only one caller, in any
    process, moves it from queued, so only that one sends it. Returns the
    claim's stamp (its sent_at), which identifies it to release and finish;
    0 when another caller holds the round. A crash after the claim is
    recovered like any stale sent round."""
    now = db.now_ms()
    claimed = db.conn().execute(
        """UPDATE dream_rounds
              SET status = 'sent', sent_at = ?, updated_at = ?
            WHERE round_id = ? AND status = 'queued'""",
        (now, now, round_id),
    ).rowcount
    return now if claimed else 0


def release_round(round_id: str, claimed_at: int) -> None:
    """A claimed round whose send failed goes back to queued for a later tick,
    only while it still holds this claim: after a stall past the recovery
    deadline another scheduler may have recovered and sent it (reviewer C)."""
    db.conn().execute(
        """UPDATE dream_rounds
              SET status = 'queued', sent_at = NULL, updated_at = ?
            WHERE round_id = ? AND status = 'sent' AND sent_at = ?""",
        (db.now_ms(), round_id, claimed_at),
    )


def mark_round_sent(round_id: str, claimed_at: int | None = None) -> None:
    """The round's send was accepted. With `claimed_at`, only this claim
    finishes: a round recovered to queued meanwhile is taken back (it was
    sent; leaving it queued would send it again), and a round another
    scheduler has claimed since is left to that one. A round that was
    abandoned or completed meanwhile is never touched."""
    now = db.now_ms()
    if claimed_at is None:
        db.conn().execute(
            """UPDATE dream_rounds
                  SET status = 'sent', sent_at = COALESCE(sent_at, ?),
                      updated_at = ?
                WHERE round_id = ? AND status IN ('queued', 'sent')""",
            (now, now, round_id),
        )
    else:
        db.conn().execute(
            """UPDATE dream_rounds
                  SET status = 'sent', sent_at = COALESCE(sent_at, ?),
                      updated_at = ?
                WHERE round_id = ?
                  AND (status = 'queued' OR (status = 'sent' AND sent_at = ?))""",
            (now, now, round_id, claimed_at),
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
    # Only a run still in flight completes: an abandoned (deadline, opt-out) or
    # already finished run is left as it is and nothing is delivered (AUT-06b).
    completed = db.conn().execute(
        """UPDATE dream_runs
              SET status = 'completed', stage = 'completed', finished_at = ?,
                  updated_at = ?
            WHERE run_id = ? AND status = 'active'""",
        (now, now, run_id),
    ).rowcount
    if not completed:
        log("dreamingDigestLate", f"run={run_id} round={round_id}")
        return
    db.conn().execute(
        """UPDATE dream_rounds
              SET status = 'completed', response = ?, completed_at = ?,
                  output_chars = ?, updated_at = ?
            WHERE round_id = ?""",
        (digest, now, len(digest or ""), now, round_id),
    )
    _sync_run_progress(run_id, "completed")
    log("dreamingDigestComplete",
        f"run={run_id} round={round_id} chars={len(digest or '')}")
    _deliver_digest(run_id=run_id, agent_id=agent_id, digest=digest)
    try:
        harvest_worktree(run_id)
    except Exception as e:  # noqa: BLE001
        log_exception("dreamHarvestUnexpected", e, detail=run_id)
    _forget_workers(run_id)


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


def abandon_run(run_id: str, reason: str = "dreaming disabled while in flight", *,
                harvest: bool = True) -> None:
    """End a run whose agent is no longer opted in, keeping what it built.

    Called when dreaming is switched off while a run is in flight. The run is
    closed rather than left active, and its worktree goes through the ordinary
    harvest so anything already written survives as a branch instead of
    leaking a directory nobody owns.
    """
    run = get_run(run_id)
    if not run or run.get("status") != "active":
        return
    now = db.now_ms()
    with settings_store.transaction():
        # Only a run still in flight is abandoned: a completion that won the
        # race keeps the run (AUT-06 review). The cleanup intent is written
        # with it and kept until the worktree is gone.
        abandoned = db.conn().execute(
            """UPDATE dream_runs
                  SET status = 'abandoned', stage = 'abandoned',
                      finished_at = ?, updated_at = ?,
                      last_error = COALESCE(NULLIF(last_error, ''), ?)
                WHERE run_id = ? AND status = 'active'""",
            (now, now, reason, run_id),
        ).rowcount
        if abandoned:
            db.conn().execute(
                """UPDATE dream_rounds SET status = 'abandoned', updated_at = ?
                    WHERE run_id = ? AND status IN ('queued', 'sent')""",
                (now, run_id),
            )
            settings_store.set_text(_CLEANUP_PREFIX + run_id, run["agent_id"])
    if not abandoned:
        return
    log("dreamingAbandoned",
        f"run={run_id} session={run.get('session')} "
        f"rounds={run.get('completed_rounds')}")
    if not harvest:
        # Its backend still uses the worktree; the scheduler harvests it once
        # that backend has stopped (_harvest_abandoned_runs).
        log("dreamHarvestDeferred", f"run={run_id}")
        return
    _harvest_abandoned(run_id)


_CLEANUP_PREFIX = "dreaming.cleanup_pending."
_CLEANUP_BATCH = 50


def _harvest_abandoned(run_id: str) -> None:
    try:
        harvest_worktree(run_id)
    except Exception as e:  # noqa: BLE001
        log_exception("dreamAbandonHarvestFail", e, detail=run_id)
    if _dream_worktree_path(run_id) is None:
        settings_store.delete(_CLEANUP_PREFIX + run_id)
        _forget_workers(run_id)


_WORKERS_PREFIX = "dreaming.workers."


def _process_start(pid: int) -> str | None:
    """The kernel's start time of ``pid`` (Linux), to tell a reused pid apart."""
    try:
        stat = pathlib.Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    return stat.rsplit(")", 1)[-1].split()[19]


def _claim_worker(run_id: str) -> str:
    """Before a spawn: a claim of its own (one key, one upsert, no list to
    rewrite), unknown until the spawn reports its pid. Returns the claim key."""
    key = f"{_WORKERS_PREFIX}{run_id}.{uuid.uuid4().hex}"
    settings_store.set_text(key, json.dumps({"pid": None, "start": None}))
    return key


def _fill_worker(claim: str, pid: int) -> None:
    settings_store.set_text(claim, json.dumps({"pid": pid, "start": _process_start(pid)}))


def _worker_claims(run_id: str) -> list[tuple[str, str]]:
    low, high = settings_store._prefix_range(f"{_WORKERS_PREFIX}{run_id}.")
    return settings_store.items_in_range(low, high, limit=1000)


def _forget_workers(run_id: str) -> None:
    settings_store.delete_keys([key for key, _value in _worker_claims(run_id)])


def _workers_gone(run_id: str) -> bool:
    """Positive proof that no dream worker of the run is alive: every claimed
    worker's process has exited, or its pid now belongs to another process
    (a KNOWN recorded start time that differs). A claim without a pid, or a
    live pid whose original start was unreadable, is unknown, which is not
    proof. A run with no claims never spawned one. The workers are Host-local
    processes, so a separate runtime's view of the agent is not consulted."""
    for _key, raw in _worker_claims(run_id):
        try:
            worker = json.loads(raw)
        except ValueError:
            return False
        pid = worker.get("pid") if isinstance(worker, dict) else None
        if not isinstance(pid, int) or pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        except PermissionError:
            pass
        recorded, current = worker.get("start"), _process_start(pid)
        if recorded is None or current is None or current == recorded:
            return False
    return True


def _cleanup_quiet(run_id: str, agent_id: str) -> bool:
    """Nothing may still write in the run's worktree: every recorded dream
    worker is gone, and neither the Host-local backend nor the backends facade
    reports a handle for the agent. Any one of them saying busy, or failing to
    answer, defers the cleanup; the worker proof does not need the agent."""
    if not _workers_gone(run_id):
        return False
    agent = agents_db.get_by_agent_id(agent_id) if agent_id else None
    if agent is None:
        return True
    backend = backends.normalize(get_settings().backend or agent.get("backend"))
    try:
        return not (backends.by_id(backend).active_handles(agent_id)
                    or backends.active_handles(backend, agent_id))
    except Exception as exc:  # noqa: BLE001 - an unreadable registry is no proof
        log_exception("dreamCleanupQuietCheckFail", exc, detail=run_id)
        return False


def _harvest_abandoned_runs() -> None:
    """Harvest the worktrees abandoned runs still hold, once their backend is
    quiet. The intent stays until the worktree is gone; one bounded batch per
    pass, in key order."""
    low, high = settings_store._prefix_range(_CLEANUP_PREFIX)
    for key, agent_id in settings_store.items_in_range(low, high, limit=_CLEANUP_BATCH):
        run_id = key[len(_CLEANUP_PREFIX):]
        if _dream_worktree_path(run_id) is not None and not _cleanup_quiet(run_id, agent_id):
            continue
        _harvest_abandoned(run_id)


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
    from . import turn_dispatch
    if turn_dispatch.live_work(agent_id, session=session).compacting:
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
    # Only the expired claim read above: another scheduler may have recovered
    # it and claimed the round afresh since (AUT-11, reviewer C).
    if not db.conn().execute(
        """UPDATE dream_rounds
              SET status = 'queued', sent_at = NULL, updated_at = ?
            WHERE round_id = ? AND status = 'sent' AND sent_at = ?""",
        (now_ms, row["round_id"], row["sent_at"]),
    ).rowcount:
        return False
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
    default_model, default_effort = backends.default_model_effort(backend, cfg)
    model = model or default_model
    effort = effort or default_effort
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
