#!/usr/bin/env python3
"""Bounded, read-only task evidence for a Janitor. Source text is never instructions."""
import contextlib
import datetime as dt
import hashlib
import html
import json
import re
import sqlite3
from pathlib import Path

USER_LIMIT = 60
MAX_BRIEF_WORDS = 300
MAX_BRIEF_CHARS = 2400
TRIVIAL = re.compile(
    r"^(?:(?:yes|yeah|yep|no|nope|ok|okay|sure|thanks|thank you|great|cool|nice|"
    r"continue|proceed|go ahead|keep going|next|status|update|progress|hello|hi|hey|"
    r"u ok|you ok|are you there|can you hear me|sounds good|do it|please)[\s,.!?-]*)+$", re.I)
STATUS_ONLY = re.compile(
    r"^(?:(?:please|can you|could you|would you)\s+)?(?:give|tell|show|repeat)\b"
    r".{0,45}\b(?:status|update|progress|briefing)\b.{0,25}[.!?]*$", re.I)
REFINEMENT = re.compile(
    r"^(?:no[, ]|yes[, ]|yeah[, ]|also\b|and\b|but\b|actually\b|instead\b|"
    r"one (?:more|thing)\b|make (?:it|that|them)\b|keep\b|don't\b|do not\b)", re.I)
NOISE = ("the clarp server has just restarted", "this is a continuity check", "heartbeat_ok")
STOPWORDS = set("the a an and or to for of in on with work task this that do make please can you we i".split())
SHORT_REQUEST = re.compile(
    r"^(?:fix|add|remove|build|test|debug|investigate|repair|update|improve|implement|"
    r"refactor|rename|delete|restore|enable|disable)\s+(?!(?:it|that|this|them|things|stuff)\b)"
    r"[\w][\w /-]*[.!?]*$", re.I)
PRIVATE_KEY = re.compile(
    r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----.*?"
    r"(?:-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----|\Z)", re.S)
API_KEY = re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]+")
AUTH_BEARER = re.compile(
    r"(\bAuthorization[\"']?\s*[:=]\s*[\"']?Bearer\s+)(?!\[REDACTED TOKEN\])[^\s,;\"'<>]+", re.I)


def redact(text):
    """Minimize obvious credential leakage before clipping or hashing task text."""
    text = PRIVATE_KEY.sub("[REDACTED PRIVATE KEY]", text)
    text = API_KEY.sub("[REDACTED API KEY]", text)
    return AUTH_BEARER.sub(r"\1[REDACTED TOKEN]", text)


def clip(value, chars=600, words=90):
    text = redact(html.unescape(str(value or "")))
    text = re.sub(r"<[^>]*>", " ", text)
    text = " ".join(text.split())
    tokens = text.split()
    if len(tokens) > words:
        text = " ".join(tokens[:words]) + "…"
    if len(text) > chars:
        text = text[:chars - 1].rsplit(" ", 1)[0] + "…"
    return text


def useful(text):
    text = clip(text, 6000, 1000)
    if not text or text.lower().startswith(NOISE) or TRIVIAL.fullmatch(text):
        return False
    if re.fullmatch(r"(?:status|update|progress)[.!? ,;-]*(?:did you stop|are you there|you there|please)?[.!? ]*", text, re.I):
        return False
    if re.fullmatch(r"(?:hello|hey|hi)\s+\w+[, ]+can you hear me[.!? ]*", text, re.I):
        return False
    if re.fullmatch(r"I missed your last update[.!? ]*Can you tell me an update[.!? ]*", text, re.I):
        return False
    if STATUS_ONLY.fullmatch(text) or re.fullmatch(
        r"(?:what(?:'s| is) (?:the )?(?:status|progress)|how(?:'s| is) (?:it|that|the task|work) (?:going|doing)).{0,12}", text, re.I):
        return False
    return bool(SHORT_REQUEST.fullmatch(text)) or (len(text.split()) >= 3 and len(text) >= 12)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()


def epoch(value):
    if isinstance(value, (int, float)):
        return value / 1000 if value > 1e11 else value
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return 0


def ref(row):
    return {"message_id": row["message_id"], "revision": row["revision"] or 0}


def choose_objective(rows, plan):
    """A bounded heuristic: retain a substantial request through short followups."""
    if not rows:
        return None
    roots = [r for r in rows if not REFINEMENT.match(r["text"].strip())] or rows[:1]
    if plan:
        preceding = [r for r in roots if epoch(r["timestamp"]) <= epoch(plan["created_at"])]
        roots = preceding or roots
        topic = set(re.findall(r"[a-z0-9]+", plan["title"].lower())) - STOPWORDS
        # Prefer an explicit topical request over a later generic approval.
        scored = [(len(topic & set(re.findall(r"[a-z0-9]+", r["text"][:650].lower()))), i, r)
                  for i, r in enumerate(roots)]
        return max(scored, key=lambda x: (x[0], x[1]))[2]
    return roots[-1]


def build_context(db_path, session):
    """Return bounded evidence and stable comparison keys; never write to SQLite.

    task_key identifies the active plan or selected user request. change_key adds
    important user/plan phase identity. fingerprint hashes actual bounded content,
    excluding operational state, status labels, timestamps and source-only revisions.
    has_context requires a real user objective or unfinished plan, not a folder name.
    """
    uri = Path(db_path).expanduser().resolve().as_uri() + "?mode=ro"
    with contextlib.closing(sqlite3.connect(uri, uri=True, timeout=5)) as c:
        c.execute("BEGIN")
        return build_context_from_connection(c, session)


def build_context_from_connection(connection, session):
    """Read from the caller's transaction (including a writer's guarded snapshot)."""
    result = {"session": session, "agent_id": None, "conversation_id": None,
              "state_id": None, "state": "unavailable", "current_status": "",
              "deleted_at": None, "archived_at": None, "cwd": "", "active_plan": None,
              "user_objective": "", "recent_user_updates": [], "latest_result": "",
              "source_refs": {}, "has_context": False, "brief": ""}
    with contextlib.closing(connection.cursor()) as c:
        c.row_factory = sqlite3.Row
        agent = c.execute("SELECT agent_id,cwd,custom_status,deleted_at,archived_at FROM agents WHERE session=?",
                          (session,)).fetchone()
        if agent:
            result.update({k: agent[k] for k in ("agent_id", "deleted_at", "archived_at")})
            result.update(cwd=clip(agent["cwd"], 180, 25), current_status=clip(agent["custom_status"], 100, 20))
            state = c.execute("SELECT state_id,kind FROM state_log WHERE agent_id=? ORDER BY state_id DESC LIMIT 1",
                              (agent["agent_id"],)).fetchone()
            if state:
                result.update(state_id=state["state_id"], state=state["kind"])
            runtime = c.execute("SELECT runtime_id,backend_session_id FROM runtimes WHERE agent_id=? "
                                "ORDER BY started_at DESC,runtime_id DESC LIMIT 1", (agent["agent_id"],)).fetchone()
            result["conversation_id"] = runtime["backend_session_id"] if runtime else None
            plan = c.execute("SELECT plan_id,title,status,created_at FROM task_plans WHERE agent_id=? "
                             "AND status IN ('active','blocked') ORDER BY created_at DESC,plan_id DESC LIMIT 1",
                             (agent["agent_id"],)).fetchone()
            if plan:
                items = c.execute("SELECT item_id,title,detail,status FROM task_items WHERE plan_id=? "
                                  "AND status IN ('in_progress','blocked','pending') "
                                  "ORDER BY CASE status WHEN 'in_progress' THEN 0 WHEN 'blocked' THEN 1 ELSE 2 END,position,item_id LIMIT 3",
                                  (plan["plan_id"],)).fetchall()
                current = [i for i in items if i["status"] in ("in_progress", "blocked")]
                items = current or items[:1]
                result["active_plan"] = {"id": plan["plan_id"], "title": clip(plan["title"], 180, 28),
                    "status": plan["status"], "current_items": [
                        {"id": i["item_id"], "title": clip(i["title"], 140, 22),
                         "detail": clip(i["detail"], 220, 32), "status": i["status"]} for i in items]}
            users, finals = [], []
            if result["conversation_id"]:
                binding = (agent["agent_id"], result["conversation_id"])
                # Native seq may be negative and decrease over time; use recorded time.
                users = list(c.execute("SELECT message_id,revision,timestamp,substr(text,1,6000) AS text FROM messages "
                    "WHERE agent_id=? AND backend_session_id=? AND role='user' AND (origin='user' OR origin IS NULL) "
                    "ORDER BY timestamp DESC,message_id DESC LIMIT ?", binding + (USER_LIMIT,)))[::-1]
                users = [r for r in users if useful(r["text"])]
                finals = list(c.execute("SELECT message_id,revision,timestamp,substr(text,1,6000) AS text FROM messages "
                    "WHERE agent_id=? AND backend_session_id=? AND role='assistant' "
                    "AND (kind='final_answer' OR kind IS NULL) AND (origin IS NULL OR origin!='heartbeat') "
                    "ORDER BY timestamp DESC,message_id DESC LIMIT 12", binding))
                finals = [r for r in finals if useful(r["text"])]
            objective = choose_objective(users, plan)
            updates = [r for r in users if objective and epoch(r["timestamp"]) > epoch(objective["timestamp"])][-3:]
            latest = next((r for r in finals if not objective or epoch(r["timestamp"]) >= epoch(objective["timestamp"])), None)
            result.update(user_objective=clip(objective["text"], 550, 75) if objective else "",
                          recent_user_updates=[clip(r["text"], 240, 35) for r in updates],
                          latest_result=clip(latest["text"], 650, 90) if latest else "",
                          has_context=bool(plan or objective))
            result["source_refs"] = {"runtime_id": runtime["runtime_id"] if runtime else None,
                "objective": ref(objective) if objective else None,
                "updates": [ref(r) for r in updates], "result": ref(latest) if latest else None,
                "plan_id": plan["plan_id"] if plan else None}
    anchor = ("plan", result["active_plan"]["id"]) if result["active_plan"] else (
        "request", result["source_refs"].get("objective"))
    result["task_key"] = digest([result["agent_id"], result["conversation_id"], anchor])
    phase = result["active_plan"] or {}
    result["change_key"] = digest([result["task_key"], phase.get("title"), phase.get("status"),
        [(i["id"], i["status"]) for i in phase.get("current_items", [])],
        (result["source_refs"].get("updates") or [result["source_refs"].get("objective")])[-1]])
    sections = []
    for label, value in [("Current workspace (not proof of project)", result["cwd"]),
                         ("User objective", clip(result["user_objective"], 420, 55)),
                         ("Unfinished plan", phase.get("title"))]:
        if value: sections.append(f"{label}: {value}")
    for item in phase.get("current_items", []):
        sections.append(f"Plan item ({item['status']}): " + clip(item["title"] + ". " + item["detail"], 240, 34))
    for value in result["recent_user_updates"]:
        sections.append("User followup: " + clip(value, 180, 24))
    if result["latest_result"]: sections.append("Latest final reply: " + clip(result["latest_result"], 460, 62))
    if result["current_status"]: sections.append("Current label: " + result["current_status"])
    result["brief"] = clip("\n".join(sections), MAX_BRIEF_CHARS, MAX_BRIEF_WORDS)
    # Label ownership/state IDs are checked separately by the admission/writer guards.
    result["fingerprint"] = digest({k: result[k] for k in (
        "active_plan", "user_objective", "recent_user_updates", "latest_result", "has_context")})
    return result
