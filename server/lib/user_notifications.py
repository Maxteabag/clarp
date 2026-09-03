"""Server-side user-facing notification policy.

The invariant is intentionally small: a completed turn badges/marks unread when
it has deliberate user-facing content. Push follows that same decision unless
the agent is muted. Audio playback is separate.
"""
from __future__ import annotations

import hashlib
import re
import time
from typing import Any

from . import agents as agents_db, db, origins, settings_store
from .log import log
from .protocol import SSEType
from .voice_markup import clean_for_display


SSE_TYPE = SSEType.USER_NOTIFICATION
PREVIEW_MAX = 180

# Keep this conservative. `agent`, `schedule`, `automation`, heartbeat, and
# dreaming turns can contain stray <speak> markup from workers; that must never
# page the user. `leader_tick` is the explicit autonomous leader-to-user path, but
# only when the completing agent is actually a team leader; delegated worker
# turns can inherit that origin from leader orchestration and must stay private.
USER_FACING_ORIGINS = origins.USER_FACING_ORIGINS
SUPPRESSED_ORIGINS = origins.SUPPRESSED_ORIGINS

SETTLE_MARGIN_MS = 2000
SETTLE_TIMEOUT_S = 2.5
SETTLE_POLL_S = 0.2
# A completion notification must describe the completion, not the immediate
# "I'll do that" acknowledgment from the beginning of a long-running turn.
# Final prose is normally written close to DONE (or imported shortly after it),
# so exclude assistant rows substantially older than the completion boundary.
ASSISTANT_EARLY_WINDOW_MS = 60 * 1000
ASSISTANT_CANDIDATE_LIMIT = 20
# Keep delayed transcript recovery for identified backend conversations. When
# provenance lacks a backend session ID, however, a broad future window can
# bind an old DONE event to an unrelated later turn; use only a short ingestion
# grace period for that ambiguous legacy path.
ASSISTANT_LATE_WINDOW_MS = 10 * 60 * 1000
AMBIGUOUS_ASSISTANT_LATE_WINDOW_MS = 30 * 1000
RECLASSIFY_RECENT_WINDOW_MS = 15 * 60 * 1000

_SPEAK_BLOCK_RE = re.compile(r"<speak\b[^>]*>(.*?)</speak>", re.IGNORECASE | re.DOTALL)
_NOOP_MARKERS = ("HEARTBEAT_OK", "DREAMING_OK", "LEADER_NOOP")
_NOOP_DISPLAY_TEXTS = {
    "heartbeat check: no action needed.",
    "dreaming check: no action needed.",
    "leader check: no action needed.",
    "automated check: no action needed.",
}


def _notification_id(agent_id: str, done_ts: int) -> str:
    raw = f"{agent_id}\0{done_ts}"
    return "pn-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def _spoken_preview(raw: str | None) -> str:
    text = str(raw or "")
    matches = [m.group(1) for m in _SPEAK_BLOCK_RE.finditer(text)]
    if not matches:
        return ""
    preview = clean_for_display(" ".join(matches), oneline=True).strip()
    if not preview:
        return ""
    return preview[:PREVIEW_MAX - 1] + "…" if len(preview) > PREVIEW_MAX else preview


def _visible_text_preview(raw: str | None) -> str:
    text = str(raw or "")
    for marker in _NOOP_MARKERS:
        text = text.replace(marker, "")
    preview = clean_for_display(text, oneline=True).strip()
    if not preview:
        return ""
    if preview.lower() in _NOOP_DISPLAY_TEXTS:
        return ""
    return preview[:PREVIEW_MAX - 1] + "…" if len(preview) > PREVIEW_MAX else preview


def _previous_done(agent_id: str, done_ts: int) -> int:
    if not done_ts:
        return 0
    row = db.conn().execute(
        """SELECT COALESCE(MAX(ts), 0) AS previous_done
             FROM state_log
            WHERE agent_id = ? AND kind = 'done' AND ts < ?""",
        (agent_id, int(done_ts)),
    ).fetchone()
    return int(row["previous_done"] or 0) if row else 0


def _causing_user_row(agent_id: str, backend_session_id: str,
                      lower_bound: int, upper_bound: int):
    params: list[Any] = [agent_id, lower_bound, upper_bound]
    where = "agent_id = ? AND role = 'user' AND updated_at >= ? AND updated_at <= ?"
    if backend_session_id:
        where += " AND backend_session_id = ?"
        params.append(backend_session_id)
    return db.conn().execute(
        f"""SELECT message_id, origin, sender_agent_id, updated_at,
                   backend_session_id, text
              FROM messages
             WHERE {where}
             ORDER BY updated_at DESC, revision DESC, seq DESC
             LIMIT 1""",
        tuple(params),
    ).fetchone()


def _next_user_after(agent_id: str, backend_session_id: str, done_ts: int) -> int:
    params: list[Any] = [agent_id, done_ts]
    where = "agent_id = ? AND role = 'user' AND updated_at > ?"
    if backend_session_id:
        where += " AND backend_session_id = ?"
        params.append(backend_session_id)
    row = db.conn().execute(
        f"""SELECT updated_at
              FROM messages
             WHERE {where}
             ORDER BY updated_at ASC, revision ASC, seq ASC
             LIMIT 1""",
        tuple(params),
    ).fetchone()
    return int(row["updated_at"] or 0) if row else 0


def _assistant_candidates(agent_id: str, backend_session_id: str, floor_ms: int,
                          ceiling_ms: int):
    params: list[Any] = [agent_id, floor_ms, ceiling_ms]
    where = (
        "agent_id = ? AND role = 'assistant' "
        "AND updated_at >= ? AND updated_at <= ? "
        "AND TRIM(COALESCE(text, '')) != '' "
        # Server-written markers ("turn interrupted by restart") sit in the
        # assistant role so they render as a reply, but they are not one.
        "AND COALESCE(origin, 'user') != ?"
    )
    params.append(origins.MARKER_ORIGIN)
    if backend_session_id:
        where += " AND backend_session_id = ?"
        params.append(backend_session_id)
    params.append(ASSISTANT_CANDIDATE_LIMIT)
    return db.conn().execute(
        f"""SELECT message_id, text, updated_at, origin, kind
              FROM messages
             WHERE {where}
             ORDER BY updated_at DESC, revision DESC, seq DESC
             LIMIT ?""",
        tuple(params),
    ).fetchall()


def _select_content_source(rows) -> tuple[Any | None, str, str]:
    """Pick the newest assistant row that actually addresses the user.

    Real leader turns can import several assistant rows at once: empty/tool
    rows, leader no-op bookkeeping, and the actual prose reply. The newest row
    is therefore not necessarily the user-facing one; scan newest-to-oldest and
    skip no-op/team-only/internal rows until visible content appears.
    """
    for row in rows:
        # Codex exposes whether an assistant message is intermediate commentary
        # or the final answer. Progress commentary must never become a
        # completion notification merely because the final row is still being
        # imported. Backends without phase metadata continue through the
        # timestamp/provenance safeguards below.
        if (row["kind"] or "").strip().lower() == "commentary":
            continue
        raw = row["text"] if row is not None else ""
        spoken = _spoken_preview(raw)
        if spoken:
            return row, spoken, "speak"
        visible = _visible_text_preview(raw)
        if visible:
            return row, visible, "text-reply"
    return None, "", ""


def _turn_was_interrupted(cause_message_id: str) -> bool:
    if not cause_message_id:
        return False
    from . import message_store
    return message_store.has_interruption_marker(cause_message_id)


def _is_team_leader(agent_id: str) -> bool:
    if not agent_id:
        return False
    try:
        from . import team_store

        return any(
            (team.get("leader_agent_id") or "") == agent_id
            for team in team_store.teams_for_agent(agent_id)
        )
    except Exception as exc:  # pragma: no cover - defensive around notification path
        log("userNotificationLeaderCheckFailed", f"{agent_id}: {exc}")
        return False


def _row_payload(row) -> dict[str, Any]:
    return {
        "notification_id": row["notification_id"],
        "agent_id": row["agent_id"],
        "session": row["session"],
        "persona": row["persona"],
        "backend_session_id": row["backend_session_id"] or "",
        "trace_id": row["trace_id"] or "",
        "done_ts": int(row["done_ts"]),
        "source_message_id": row["source_message_id"] or "",
        "cause_message_id": row["cause_message_id"] or "",
        "origin": row["origin"] or "",
        "notify": bool(row["notify"]),
        "push": bool(row["push"]),
        "badge": bool(row["badge"]),
        "unread": bool(row["unread"]),
        "muted": bool(row["muted"]) if "muted" in row.keys() else False,
        "preview": row["preview"] or "",
        "reason": row["reason"] or "",
        "created_at": int(row["created_at"]),
        "updated_at": int(row["updated_at"]),
    }


def _persist(payload: dict[str, Any]) -> dict[str, Any]:
    database = db.conn()
    database.execute(
        """INSERT INTO user_notifications (
               notification_id, agent_id, session, persona,
               backend_session_id, trace_id, done_ts, source_message_id,
               cause_message_id, origin, notify, push, badge, unread, muted,
               preview, reason, created_at, updated_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(agent_id, done_ts) DO UPDATE SET
               session = excluded.session,
               persona = excluded.persona,
               backend_session_id = excluded.backend_session_id,
               trace_id = excluded.trace_id,
               source_message_id = excluded.source_message_id,
               cause_message_id = excluded.cause_message_id,
               origin = excluded.origin,
               notify = excluded.notify,
               push = excluded.push,
               badge = excluded.badge,
               unread = excluded.unread,
               muted = excluded.muted,
               preview = excluded.preview,
               reason = excluded.reason,
               updated_at = excluded.updated_at""",
        (
            payload["notification_id"],
            payload["agent_id"],
            payload["session"],
            payload["persona"],
            payload["backend_session_id"],
            payload["trace_id"],
            payload["done_ts"],
            payload["source_message_id"],
            payload["cause_message_id"],
            payload["origin"],
            1 if payload["notify"] else 0,
            1 if payload["push"] else 0,
            1 if payload["badge"] else 0,
            1 if payload["unread"] else 0,
            1 if payload["muted"] else 0,
            payload["preview"],
            payload["reason"],
            payload["created_at"],
            payload["updated_at"],
        ),
    )
    row = database.execute(
        "SELECT * FROM user_notifications WHERE agent_id = ? AND done_ts = ?",
        (payload["agent_id"], payload["done_ts"]),
    ).fetchone()
    return _row_payload(row)


def classify_completed_turn(*, agent_id: str, session: str, persona: str,
                            done_ts: int, backend_session_id: str = "",
                            trace_id: str = "",
                            settle_timeout_s: float | None = None) -> dict[str, Any]:
    """Classify and persist the completed turn's user-facing notification.

    The causing user row is the durable source of provenance. We deliberately do
    not trust live assistant-row origin because fast backends can stream before
    the dispatch path finishes appending the pending user row.
    """
    agent_id = (agent_id or "").strip()
    session = (session or "").strip()
    persona = (persona or "").strip()
    backend_session_id = (backend_session_id or "").strip()
    trace_id = (trace_id or "").strip()
    done_ts = int(done_ts or db.now_ms())
    now = db.now_ms()
    notification_id = _notification_id(agent_id, done_ts)
    lower_bound = _previous_done(agent_id, done_ts)
    cause = _causing_user_row(agent_id, backend_session_id, lower_bound, done_ts)
    origin = ""
    cause_message_id = ""
    cause_updated_at = lower_bound
    if cause is not None:
        origin = (cause["origin"] or "user").strip() or "user"
        cause_message_id = cause["message_id"] or ""
        cause_updated_at = int(cause["updated_at"] or lower_bound)
        backend_session_id = backend_session_id or (cause["backend_session_id"] or "")

    floor_ms = max(
        lower_bound,
        cause_updated_at - SETTLE_MARGIN_MS,
        done_ts - ASSISTANT_EARLY_WINDOW_MS,
    )
    late_window_ms = (
        ASSISTANT_LATE_WINDOW_MS
        if backend_session_id
        else AMBIGUOUS_ASSISTANT_LATE_WINDOW_MS
    )
    ceiling_ms = done_ts + late_window_ms
    next_user_at = _next_user_after(agent_id, backend_session_id, done_ts)
    if next_user_at:
        ceiling_ms = min(ceiling_ms, next_user_at - 1)
    source = None
    preview = ""
    content_reason = ""
    timeout = SETTLE_TIMEOUT_S if settle_timeout_s is None else float(settle_timeout_s)
    deadline = time.monotonic() + timeout
    while True:
        source, preview, content_reason = _select_content_source(
            _assistant_candidates(agent_id, backend_session_id, floor_ms, ceiling_ms)
        )
        if source is not None or time.monotonic() >= deadline:
            break
        time.sleep(SETTLE_POLL_S)

    source_message_id = source["message_id"] if source is not None else ""
    special_automation = settings_store.get_bool(
        "automation_special_treatment", default=False)
    if not agent_id:
        reason = "missing-agent"
    elif cause is None:
        reason = "missing-causing-row"
    elif special_automation and origin not in USER_FACING_ORIGINS:
        reason = f"not-user-facing-origin:{origin or 'unknown'}"
    elif (special_automation and origin == "leader_tick"
          and not _is_team_leader(agent_id)):
        reason = "leader-tick-non-leader"
    elif content_reason:
        reason = content_reason
    elif _turn_was_interrupted(cause_message_id):
        # The turn was killed before it could say anything; that is not the
        # same event as an agent that had nothing to say.
        reason = "turn-interrupted"
    else:
        reason = "no-user-facing-content"
    notify = reason in {"speak", "text-reply"}
    muted = False
    if notify:
        agent = agents_db.get_by_agent_id(agent_id)
        muted = bool(agent and agent.get("muted"))
        if muted:
            reason = f"{reason}-muted"
    payload = {
        "notification_id": notification_id,
        "agent_id": agent_id,
        "session": session,
        "persona": persona,
        "backend_session_id": backend_session_id,
        "trace_id": trace_id,
        "done_ts": done_ts,
        "source_message_id": source_message_id,
        "cause_message_id": cause_message_id,
        "origin": origin,
        "notify": notify,
        "push": notify and not muted,
        "badge": notify,
        "unread": notify,
        "muted": muted,
        "preview": preview if notify else "",
        "reason": reason,
        "created_at": now,
        "updated_at": now,
    }
    result = _persist(payload)
    log(
        "userNotificationClassified",
        f"notification={result['notification_id']} agent={agent_id} session={session} "
        f"done_ts={done_ts} cause={result['cause_message_id'] or '-'} "
        f"source={result['source_message_id'] or '-'} reason={result['reason']} "
        f"source_delta_ms={int(source['updated_at']) - done_ts if source is not None else '-'} "
        f"push={int(result['push'])}",
    )
    if not result["notify"]:
        log("userNotificationSuppressed",
            f"{persona or agent_id} session={session} reason={result['reason']}")
    return result


def reclassify_recent_suppressed(*, agent_id: str, backend_session_id: str = "",
                                 now_ms_value: int | None = None
                                 ) -> list[dict[str, Any]]:
    """Retry recent suppressed user-facing rows after transcript import.

    DONE can arrive before the backend transcript's final prose is imported into
    SQLite. When transcript rows later land, retry only rows that were silent
    because content was missing, and only inside a short recency window. Rows
    already notified, non-user-facing origins, and no-op-only turns stay put.
    """
    now_value = int(now_ms_value or db.now_ms())
    params: list[Any] = [
        agent_id,
        now_value - RECLASSIFY_RECENT_WINDOW_MS,
    ]
    where = (
        "agent_id = ? AND notify = 0 AND done_ts >= ? "
        "AND reason IN ('no-user-facing-content', 'no-speak')"
    )
    if backend_session_id:
        where += " AND backend_session_id = ?"
        params.append(backend_session_id)
    rows = db.conn().execute(
        f"""SELECT agent_id, session, persona, backend_session_id, trace_id, done_ts
              FROM user_notifications
             WHERE {where}
             ORDER BY done_ts DESC
             LIMIT 5""",
        tuple(params),
    ).fetchall()
    flipped: list[dict[str, Any]] = []
    for row in rows:
        notification = classify_completed_turn(
            agent_id=row["agent_id"],
            session=row["session"],
            persona=row["persona"],
            backend_session_id=row["backend_session_id"] or "",
            trace_id=row["trace_id"] or "",
            done_ts=int(row["done_ts"]),
            settle_timeout_s=0,
        )
        if notification.get("notify"):
            flipped.append(notification)
    return flipped


def event_payload(notification: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": SSE_TYPE,
        "notification_id": notification["notification_id"],
        "agent_id": notification["agent_id"],
        "session": notification["session"],
        "persona": notification["persona"],
        "done_ts": notification["done_ts"],
        "source_message_id": notification["source_message_id"],
        "cause_message_id": notification["cause_message_id"],
        "origin": notification["origin"],
        "push": bool(notification["push"]),
        "badge": bool(notification["badge"]),
        "unread": bool(notification["unread"]),
        "muted": bool(notification.get("muted")),
        "preview": notification["preview"],
        "reason": notification["reason"],
    }
