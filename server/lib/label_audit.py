"""Does each working label still describe the work? Report only.

The Label checker Janitor (built-in role ``label-auditor``) runs this hourly
from ``janitor_autonomy``. For every agent the apps show as working in the
background (a declared background state, a status line, or counted helpers
and processes) it builds a small evidence packet: the label as shown, how
long the state has held, job titles, progress lines and heartbeat ages,
helper names and states, and the last few messages. Jev judges all of them in
one request (``judgment_sites.audit_work_labels``). Labels it finds wrong are
listed in one report per run, in the Label checker's own chat: a Janitor is a
quiet agent (``quiet_agents``), so the report is not in Updates.

Nothing here changes an agent, a job or a label. The Janitor's
``autocorrect`` option is reserved for that and is not acted on yet.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from . import agents as agents_db
from . import artifacts, background_jobs, janitors, snapshot, stale_work
from .janitor_context import clip, redact

ROLE = "label-auditor"
KIND = "label_mismatch"
PREFIX = "label-audit:"
MAX_AGENTS = 12
MAX_JOBS = 3
MAX_HELPERS = 4
RECENT_MESSAGES = 3
# A background state with no words of its own still reads as "working".
UNNAMED_LABEL = "Working in the background"
# A label that changed moments ago is not stale yet: the turn that set it
# may only just have ended.
FRESH_GRACE_MS = 15 * 60 * 1000

_REASONS = {
    "finished": "the work it describes looks finished",
    "waiting_for_user": "it looks like it is waiting for your answer",
    "stalled": "nothing seems to be happening",
    "different_work": "it seems to be doing something else",
}


def _minutes(now: int, then: int | None) -> int | None:
    return max(0, (now - int(then)) // 60000) if then else None


def _age(minutes: int | None) -> str:
    if minutes is None:
        return "a while"
    if minutes < 90:
        return f"{minutes} min"
    return f"{round(minutes / 60)}h"


def candidates(now: int) -> list[dict[str, Any]]:
    """Agents shown as working in the background, with compact evidence.

    An agent in a live turn is skipped: its turn describes itself. Janitors
    and archived agents are skipped. At most ``MAX_AGENTS``, the longest
    unchanged first, since those are the likeliest to be stale.
    """
    rows = agents_db.list_agents()
    states = agents_db.dashboard_states()
    visible = janitors.visible_labels()
    tree = snapshot.helper_tree(rows, states, visible)
    jobs = background_jobs.active_by_agent()
    activity = stale_work.activities(rows)
    found: list[dict[str, Any]] = []
    for a in rows:
        agent_id = a["agent_id"]
        act = activity.get(agent_id)
        if a.get("is_janitor") or a.get("archived_at") or act is None or act.busy:
            continue
        if now - int(act.idle_since_ms or 0) < FRESH_GRACE_MS:
            continue
        state = states.get(agent_id, {})
        processes = background_jobs.background_processes(
            jobs.get(agent_id, []), tree.helper_sessions.get(agent_id, set()))
        helpers = tree.helper_activity.get(agent_id, [])
        label = snapshot.shown_status(a, state, visible, helpers, processes)
        if not (label or processes or helpers or state.get("kind") == "background"):
            continue
        found.append({"agent": a, "state": state, "processes": processes,
                      "helpers": helpers, "label": label or UNNAMED_LABEL,
                      "idle_since": act.idle_since_ms})
    found.sort(key=lambda item: item["idle_since"])
    return [_evidence(item, now) for item in found[:MAX_AGENTS]]


def _evidence(item: dict[str, Any], now: int) -> dict[str, Any]:
    a, state = item["agent"], item["state"]
    recent = agents_db.list_messages(
        agent_id=a["agent_id"], backend_session_id=agents_db.live_backend_session(a["agent_id"]),
        limit=RECENT_MESSAGES)
    detail = state.get("detail") if isinstance(state.get("detail"), dict) else {}
    return {
        "agent_id": a["agent_id"],
        "name": str(a.get("persona") or a.get("session") or ""),
        "packet": {
            "label": item["label"],
            "state": str(state.get("kind") or ""),
            "state_detail": clip(redact(str(detail.get("message") or detail.get("summary") or "")), 160, 30),
            "state_age_min": _minutes(now, state.get("ts")),
            "turn_ended_min_ago": _minutes(now, state.get("last_turn_end")),
            "jobs": [{"title": clip(str(job.get("title") or ""), 120, 20),
                      "kind": str(job.get("kind") or ""),
                      "progress": clip(redact(str(job.get("progress_text") or "")), 160, 30),
                      "progress_age_min": _minutes(now, job.get("active_at")),
                      "heartbeat_age_min": _minutes(now, job.get("heartbeat_at"))}
                     for job in item["processes"][:MAX_JOBS]],
            "helpers": [{"name": h["session"], "activity": h["bucket"],
                         "status": clip(str(h["text"] or ""), 120, 20),
                         "age_min": _minutes(now, h["ts"])}
                        for h in item["helpers"][:MAX_HELPERS]],
            "recent_messages": [{"role": str(m.get("role") or ""),
                                 "text": clip(redact(str(m.get("text") or "")), 300, 50)}
                                for m in recent[-RECENT_MESSAGES:]],
        },
        "idle_min": _minutes(now, item["idle_since"]),
    }


def packets(evidence: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """What goes to Jev: short keys, no agent ids."""
    return {f"a{i + 1}": entry["packet"] for i, entry in enumerate(evidence)}


def mismatches(evidence: list[dict[str, Any]], verdicts: dict[str, dict]) -> list[dict[str, Any]]:
    found = []
    for i, entry in enumerate(evidence):
        verdict = verdicts.get(f"a{i + 1}") or {}
        if not verdict.get("mismatch"):
            continue
        reason = _REASONS.get(verdict["verdict"], "it may be out of date")
        packet = entry["packet"]
        found.append({
            "agent_id": entry["agent_id"], "name": entry["name"], "label": packet["label"],
            "verdict": verdict["verdict"], "match": round(float(verdict["match"]), 3),
            "line": (f"{entry['name']} says '{packet['label']}', but {reason} "
                     f"(no change for {_age(entry['idle_min'])})."),
        })
    return found


def report(owner: dict[str, Any], run_id: str, found: list[dict[str, Any]],
           *, checked: int, last_fingerprint: str = "") -> tuple[dict | None, str]:
    """Create this run's Updates item. Returns (artifact or None, fingerprint).

    The same set of wrong labels is reported once; it is not repeated every
    hour while it stays wrong. The caller keeps the fingerprint.
    """
    fingerprint = hashlib.sha256(json.dumps(
        sorted((f["agent_id"], f["label"], f["verdict"]) for f in found)).encode()).hexdigest()
    if not found or fingerprint == last_fingerprint:
        return None, fingerprint
    count = len(found)
    title = f"{count} working label{'s' if count != 1 else ''} look{'s' if count == 1 else ''} wrong"
    content = "\n".join(f"- {f['line']}" for f in found)
    artifact_id = "label-audit-" + hashlib.sha256(run_id.encode()).hexdigest()[:40]
    artifact = artifacts.create(
        session=owner["session"], type="document", status="completed",
        artifact_id=artifact_id, reference_id=PREFIX + run_id, title=title,
        summary=found[0]["line"] if count == 1 else f"{found[0]['line']} And {count - 1} more.",
        payload={"attention_kind": KIND, "run_id": run_id, "checked": checked,
                 "content": f"Checked {checked} working labels. These look out of date; "
                            f"nothing was changed.\n\n{content}",
                 "items": [{k: f[k] for k in ("agent_id", "name", "label", "verdict", "match")}
                           for f in found]})
    return artifact, fingerprint
