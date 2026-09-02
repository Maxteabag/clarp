"""Durable typed deliverables and approval decisions."""
from __future__ import annotations

import json
import math
import pathlib
import re
import secrets
from urllib.parse import urlsplit
from typing import Any

from . import agents, db, media_store
from .voice_markup import strip_hidden_blocks

TYPES = {"decision", "plan", "document", "research", "code_change", "data",
         "audio", "video", "file", "release", "directory", "workflow_run"}
STATUSES = {"draft", "active", "ready", "failed", "completed", "cancelled", "expired"}
_VALID_ID = re.compile(r"^[A-Za-z0-9._:-]{1,180}$")

# Artifact kinds are contracts, not decorative labels.  These fields are the
# minimum needed for the native client to offer the type's defining interaction.
# Existing rows created before this validation remain readable.
_REQUIRED_FIELDS = {
    "document": ("content",),
    "research": ("content",),
    "code_change": ("repository",),
    "data": ("columns", "rows"),
    "audio": ("url", "mime_type", "file_name"),
    "video": ("url", "mime_type", "file_name"),
    "file": ("url", "mime_type", "file_name"),
    "release": ("version",),
    "directory": ("root", "relative_path"),
    "workflow_run": ("provider", "run_id", "run_url", "workflow_name"),
}


def _new(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(12)}"


def _agent(session: str) -> dict:
    row = agents.get_by_session(session.strip())
    if not row:
        raise ValueError("unknown agent session")
    return row


def _payload(value: Any) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("payload must be an object")
    value = _sanitize_json(value)
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("payload must contain finite JSON values") from exc
    if len(encoded.encode()) > 131072:
        raise ValueError("artifact payload too large")
    string_fields = {"url", "thumbnail_url", "mime_type", "file_name", "content",
                     "source_url", "commit", "branch", "repository", "subject",
                     "starts_at", "ends_at", "time_zone", "location", "notes",
                     "version", "build", "environment", "diff", "path_label", "asset_id",
                     "root", "relative_path", "provider", "run_id", "run_url",
                     "workflow_name", "current_step", "conclusion"}
    integer_fields = {"duration_ms", "size_bytes", "row_count", "source_count",
                      "files_changed", "additions", "deletions", "total_steps", "completed_steps"}
    for key in string_fields & value.keys():
        if not isinstance(value[key], str): raise ValueError(f"payload {key} must be a string")
        value = dict(value)
        value[key] = strip_hidden_blocks(value[key])
    for key in integer_fields & value.keys():
        if isinstance(value[key], bool) or not isinstance(value[key], int):
            raise ValueError(f"payload {key} must be an integer")
        if value[key] < 0 or value[key] > 9223372036854775807:
            raise ValueError(f"payload {key} is outside the supported range")
    if "progress" in value and (isinstance(value["progress"], bool)
                                  or not isinstance(value["progress"], (int, float))):
        raise ValueError("payload progress must be numeric")
    if "progress" in value and not 0 <= float(value["progress"]) <= 1:
        raise ValueError("payload progress must be between 0 and 1")
    if "content" in value:
        value = dict(value)
        value["content"] = strip_hidden_blocks(value["content"])
    if "all_day" in value and not isinstance(value["all_day"], bool):
        raise ValueError("payload all_day must be a boolean")
    return value


def _require_payload(type: str, payload: dict, artifact_id: str, session: str = "") -> None:
    """Reject artifact-shaped markdown blobs that cannot power their renderer."""
    if type in {"decision", "plan"}:
        return
    missing = [key for key in _REQUIRED_FIELDS.get(type, ())
               if key not in payload or payload[key] in (None, "", {})
               or (payload[key] == [] and not (type == "data" and key == "rows"))]
    if missing:
        raise ValueError(f"{type} artifact requires payload field(s): {', '.join(missing)}")
    for field in ("source_url", "thumbnail_url"):
        if payload.get(field):
            _safe_url(str(payload[field]), field=field, allow_relative=field == "thumbnail_url")
    if type == "data":
        columns, rows = payload["columns"], payload["rows"]
        if not isinstance(columns, list) or not columns or not all(
                isinstance(item, str) and item.strip() for item in columns):
            raise ValueError("data artifact columns must be a non-empty string array")
        if len(columns) > 30:
            raise ValueError("data artifact cannot contain more than 30 columns")
        if not isinstance(rows, list) or not all(isinstance(row, list) for row in rows):
            raise ValueError("data artifact rows must be an array of arrays")
        if len(rows) > 500:
            raise ValueError("data artifact cannot contain more than 500 rows")
        if any(len(row) != len(columns) for row in rows):
            raise ValueError("data artifact rows must match the column count")
        if any(not _artifact_scalar(cell) for row in rows for cell in row):
            raise ValueError("data artifact cells must be strings, numbers, booleans, or null")
        chart = payload.get("chart")
        if chart is not None:
            if (not isinstance(chart, dict) or chart.get("kind") not in {"bar", "line"}
                    or any(key in chart and not isinstance(chart[key], str)
                           for key in ("category_column", "value_column"))):
                raise ValueError("data artifact chart must use a supported typed specification")
            category_name = chart.get("category_column") or columns[0]
            value_name = chart.get("value_column") or (columns[1] if len(columns) > 1 else "")
            if category_name not in columns or value_name not in columns:
                raise ValueError("data artifact chart columns must exist in the dataset")
            value_index = columns.index(value_name)
            if not rows or any(isinstance(row[value_index], bool)
                               or not isinstance(row[value_index], (int, float)) for row in rows):
                raise ValueError("data artifact chart value column must contain numbers")
    elif type == "research":
        sources = payload.get("sources", [])
        if not isinstance(sources, list) or not all(
                isinstance(item, dict) and isinstance(item.get("title"), str)
                and isinstance(item.get("url"), str) for item in sources):
            raise ValueError("research artifact sources must contain string title and url fields")
        for source in sources:
            _safe_url(source["url"], field="sources.url", allow_relative=False)
    elif type in {"audio", "video", "file"}:
        mime = str(payload["mime_type"]).lower()
        expected = {"audio": "audio/", "video": "video/"}.get(type)
        if expected and not mime.startswith(expected):
            raise ValueError(f"{type} artifact mime_type must start with {expected}")
        raw_url = str(payload["url"])
        match = re.fullmatch(r"/media/([A-Za-z0-9_-]{1,180})", raw_url)
        if not match:
            raise ValueError(f"{type} artifact url must be a same-origin media path")
        asset = media_store.get(match.group(1))
        if not asset or (session and asset["session"] != session):
            raise ValueError(f"{type} artifact media asset not found for this session")
        if asset["mime_type"] != payload["mime_type"]:
            raise ValueError(f"{type} artifact mime_type must match its media asset")
        if asset["source_name"] != payload["file_name"]:
            raise ValueError(f"{type} artifact file_name must match its media asset")
    elif type == "directory":
        root, relative = str(payload["root"]), str(payload["relative_path"])
        if root not in {"workspace", "home"}: raise ValueError("directory root must be workspace or home")
        if relative.startswith("/") or ".." in pathlib.PurePosixPath(relative).parts:
            raise ValueError("directory relative_path must stay within its root")
    elif type == "workflow_run":
        if payload["provider"] != "github": raise ValueError("workflow_run provider must be github")
        _safe_url(str(payload["run_url"]), field="run_url", allow_relative=False)
        parsed=urlsplit(str(payload["run_url"]))
        if parsed.hostname!="github.com" or not re.fullmatch(r"/[^/]+/[^/]+/actions/runs/\d+/?",parsed.path):
            raise ValueError("workflow_run URL must identify a GitHub Actions run")
        total, done = int(payload.get("total_steps", 0)), int(payload.get("completed_steps", 0))
        if total < 0 or done < 0 or (total and done > total): raise ValueError("workflow run step counts are invalid")


def _safe_url(raw: str, *, field: str, allow_relative: bool) -> None:
    if raw.startswith("/") and not raw.startswith("//"):
        if allow_relative: return
        raise ValueError(f"payload {field} must be an HTTPS URL")
    try:
        parsed = urlsplit(raw)
        hostname = parsed.hostname
        parsed.port  # Force invalid ports to fail validation here.
    except ValueError as exc:
        raise ValueError(f"payload {field} must be an HTTPS URL or safe same-origin path") from exc
    if parsed.scheme != "https" or not hostname or parsed.username or parsed.password:
        raise ValueError(f"payload {field} must be an HTTPS URL or safe same-origin path")


def _artifact_scalar(value: Any) -> bool:
    if value is None or isinstance(value, (str, bool)): return True
    if not isinstance(value, (int, float)): return False
    if isinstance(value, int) and abs(value) > 9007199254740991: return False
    try: return math.isfinite(float(value))
    except (OverflowError, ValueError): return False


def _sanitize_json(value: Any) -> Any:
    if isinstance(value, str): return strip_hidden_blocks(value)
    if isinstance(value, list): return [_sanitize_json(item) for item in value]
    if isinstance(value, dict): return {str(key): _sanitize_json(item) for key, item in value.items()}
    return value


def create(*, session: str, type: str, title: str, summary: str = "",
           status: str = "ready", reference_id: str = "", payload: Any = None,
           artifact_id: str = "") -> dict:
    agent = _agent(session)
    type, status = type.strip().lower(), status.strip().lower()
    title = strip_hidden_blocks(title).strip()
    if type not in TYPES: raise ValueError("unsupported artifact type")
    if status not in STATUSES: raise ValueError("unsupported artifact status")
    if not title: raise ValueError("artifact title required")
    artifact_id = artifact_id.strip() or _new("artifact")
    if not _VALID_ID.fullmatch(artifact_id): raise ValueError("invalid artifact id")
    payload = _payload(payload)
    _require_payload(type, payload, artifact_id, str(agent["session"]))
    now = db.now_ms()
    db.conn().execute(
        """INSERT INTO artifacts(artifact_id,agent_id,session,type,title,summary,
               status,reference_id,payload_json,created_at,updated_at,completed_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (artifact_id, agent["agent_id"], agent["session"], type, title[:300],
         strip_hidden_blocks(summary).strip()[:4000], status,
         strip_hidden_blocks(reference_id).strip()[:500],
         json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False),
         now, now, now if status in {"completed", "cancelled", "expired"} else None))
    return get(artifact_id) or {}


def ensure_plan(*, plan_id: str, session: str, title: str) -> dict:
    row = db.conn().execute(
        "SELECT artifact_id FROM artifacts WHERE type='plan' AND reference_id=?",
        (plan_id,)).fetchone()
    return (get(row["artifact_id"]) if row else
            create(session=session, type="plan", title=title, status="active",
                   reference_id=plan_id, payload={"plan_id": plan_id})) or {}


def sync_plan(plan_id: str) -> None:
    plan = db.conn().execute(
        "SELECT status,updated_at,completed_at,session,title FROM task_plans WHERE plan_id=?",
        (plan_id,)).fetchone()
    if not plan: return
    ensure_plan(plan_id=plan_id, session=plan["session"], title=plan["title"])
    status = {"active": "active", "completed": "completed", "blocked": "failed"}.get(
        plan["status"], "cancelled")
    db.conn().execute(
        "UPDATE artifacts SET status=?,updated_at=?,completed_at=? WHERE type='plan' AND reference_id=?",
        (status, plan["updated_at"], plan["completed_at"], plan_id))


def _public(row) -> dict:
    item = dict(row)
    try: item["payload"] = json.loads(item.pop("payload_json") or "{}")
    except json.JSONDecodeError: item["payload"] = {}
    for key in ("url", "thumbnail_url", "mime_type", "file_name", "content",
                "source_url", "commit", "branch", "repository", "progress",
                "duration_ms", "size_bytes", "row_count", "source_count",
                "sources", "columns", "rows", "chart", "recipients", "subject",
                "starts_at", "ends_at", "time_zone", "location", "notes", "all_day",
                "version", "build", "environment", "files_changed", "additions",
                "deletions", "diff", "path_label", "artifact_ids", "asset_id", "asset_ids",
                "root", "relative_path", "provider", "run_id", "run_url", "workflow_name",
                "current_step", "conclusion", "total_steps", "completed_steps"):
        if key in item["payload"] and _public_field_valid(key, item["payload"][key]):
            item[key] = item["payload"][key]
    if item["type"] == "decision":
        decision = db.conn().execute(
            "SELECT * FROM artifact_decisions WHERE artifact_id=?", (item["artifact_id"],)).fetchone()
        item["decision"] = dict(decision) if decision else None
    elif item["type"] == "plan" and item.get("reference_id"):
        from . import task_plans
        item["plan"] = task_plans.get(item["reference_id"])
    return item


def _public_field_valid(key: str, value: Any) -> bool:
    strings = {"url", "thumbnail_url", "mime_type", "file_name", "content", "source_url",
               "commit", "branch", "repository", "subject", "starts_at", "ends_at",
               "time_zone", "location", "notes", "version", "build", "environment",
               "diff", "path_label", "asset_id", "root", "relative_path", "provider",
               "run_id", "run_url", "workflow_name", "current_step", "conclusion"}
    integers = {"duration_ms", "size_bytes", "row_count", "source_count", "files_changed",
                "additions", "deletions", "total_steps", "completed_steps"}
    if key in strings: return isinstance(value, str)
    if key in integers:
        return (isinstance(value, int) and not isinstance(value, bool)
                and -9223372036854775808 <= value <= 9223372036854775807)
    if key == "progress":
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(float(value)) and 0 <= float(value) <= 1)
    if key == "all_day": return isinstance(value, bool)
    if key in {"columns", "recipients", "artifact_ids", "asset_ids"}:
        return isinstance(value, list) and all(isinstance(item, str) for item in value)
    if key == "rows":
        return isinstance(value, list) and all(
            isinstance(row, list) and all(_artifact_scalar(cell) for cell in row) for row in value)
    if key == "sources":
        return isinstance(value, list) and all(
            isinstance(source, dict) and isinstance(source.get("title"), str)
            and isinstance(source.get("url"), str) for source in value)
    if key == "chart":
        return (isinstance(value, dict) and value.get("kind") in {"bar", "line"}
                and all(field not in value or isinstance(value[field], str)
                        for field in ("category_column", "value_column")))
    return False


def get(artifact_id: str) -> dict | None:
    row = db.conn().execute(
        """SELECT a.*,g.persona AS agent_name FROM artifacts a JOIN agents g
             ON g.agent_id=a.agent_id WHERE artifact_id=? AND a.deleted_at IS NULL""",
        (artifact_id,)).fetchone()
    return _public(row) if row else None


def list_artifacts(*, session: str = "", agent_id: str = "", type: str = "", search: str = "",
                   created_from: int | None = None, created_to: int | None = None,
                   limit: int = 100, offset: int = 0,
                   order: str = "updated") -> list[dict]:
    _expire_decisions()
    # Retired presentation-only rows stay readable by ID for compatibility,
    # but never reappear in chat or the artifact library. Images remain in the
    # media store and retain their original inline/gallery/profile behavior.
    where, params = ["a.deleted_at IS NULL", "a.type NOT IN ('image','image_gallery','live_task','event')"], []
    if session: where.append("a.session=?"); params.append(session)
    if agent_id: where.append("a.agent_id=?"); params.append(agent_id)
    if type: where.append("a.type=?"); params.append(type)
    if search:
        where.append("(LOWER(a.title) LIKE ? ESCAPE '\\' OR LOWER(a.summary) LIKE ? ESCAPE '\\')")
        literal=search.lower()[:200].replace("\\","\\\\").replace("%","\\%").replace("_","\\_")
        needle = f"%{literal}%"; params.extend([needle, needle])
    if created_from is not None: where.append("a.created_at>=?"); params.append(created_from)
    if created_to is not None: where.append("a.created_at<?"); params.append(created_to)
    params.extend([max(1, min(int(limit), 500)), max(0, int(offset))])
    order_column = "created_at" if order == "created" else "updated_at"
    rows = db.conn().execute(
        f"""SELECT a.*,g.persona AS agent_name FROM artifacts a JOIN agents g
              ON g.agent_id=a.agent_id WHERE {' AND '.join(where)}
             ORDER BY a.{order_column} DESC,a.artifact_id DESC LIMIT ? OFFSET ?""",
        tuple(params)).fetchall()
    return [_public(row) for row in rows]


def update(artifact_id: str, data: dict) -> dict:
    current = get(artifact_id)
    if not current: raise ValueError("artifact not found")
    if current["type"] in {"decision", "plan"}:
        raise ValueError(f"{current['type']} must use its dedicated lifecycle endpoint")
    status = str(data.get("status") or current["status"]).lower()
    if status not in STATUSES: raise ValueError("unsupported artifact status")
    if "payload" in data and "payload_patch" in data:
        raise ValueError("provide payload or payload_patch, not both")
    if "payload_patch" in data:
        payload = _payload({**current["payload"], **_payload(data["payload_patch"])})
    else:
        payload = current["payload"] if "payload" not in data else _payload(data["payload"])
    if "payload" in data or "payload_patch" in data:
        _require_payload(current["type"], payload, artifact_id, str(current["session"]))
    title = strip_hidden_blocks(str(data.get("title") or current["title"])).strip()
    if not title: raise ValueError("artifact title required")
    now = db.now_ms()
    completed = (current["completed_at"] if status == current["status"] else
                 (now if status in {"completed", "cancelled", "expired"} else None))
    db.conn().execute(
        """UPDATE artifacts SET title=?,summary=?,status=?,payload_json=?,updated_at=?,
                  completed_at=? WHERE artifact_id=?""",
        (title[:300],
         strip_hidden_blocks(str(data.get("summary") if "summary" in data else current["summary"]))[:4000],
         status, json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
         now, completed, artifact_id))
    return get(artifact_id) or {}


def create_decision(*, session: str, title: str, question: str, context: str = "",
                    yes_label: str = "Yes", no_label: str = "No", payload: Any = None,
                    reference_id: str = "", expires_at: int | None = None) -> dict:
    question = strip_hidden_blocks(question).strip()
    context = strip_hidden_blocks(context).strip()
    if not question: raise ValueError("decision question required")
    if len(question) > 4000 or len(context) > 16000:
        raise ValueError("decision text too large")
    yes_label = strip_hidden_blocks(yes_label).strip() or "Yes"
    no_label = strip_hidden_blocks(no_label).strip() or "No"
    if len(yes_label) > 80 or len(no_label) > 80:
        raise ValueError("decision label too large")
    if expires_at is not None and (isinstance(expires_at, bool) or not isinstance(expires_at, int)):
        raise ValueError("expires_at must be an integer")
    if expires_at is not None and expires_at > db.now_ms() + 365 * 24 * 60 * 60 * 1000:
        raise ValueError("expires_at is too far in the future")
    con = db.conn(); con.execute("BEGIN IMMEDIATE")
    try:
        artifact = create(session=session, type="decision", title=title,
                          summary=question, status="active", reference_id=reference_id,
                          payload=payload)
        con.execute(
            """INSERT INTO artifact_decisions(decision_id,artifact_id,question,context,yes_label,
                   no_label,expires_at) VALUES(?,?,?,?,?,?,?)""",
            (_new("decision"), artifact["artifact_id"], question, context,
             yes_label, no_label, expires_at))
        con.execute("COMMIT")
        return get(artifact["artifact_id"]) or {}
    except BaseException:
        con.execute("ROLLBACK"); raise


def resolve(decision_id: str, *, choice: str, expected_revision: int) -> tuple[dict, bool]:
    choice = choice.strip().lower()
    if choice not in {"accepted", "rejected"}: raise ValueError("invalid decision choice")
    _expire_decisions()
    con = db.conn(); con.execute("BEGIN IMMEDIATE")
    try:
        row = con.execute("SELECT * FROM artifact_decisions WHERE decision_id=?", (decision_id,)).fetchone()
        if not row: raise ValueError("decision not found")
        if row["expires_at"] is not None and int(row["expires_at"]) <= db.now_ms():
            raise ValueError("decision expired")
        if row["status"] != "pending":
            if row["status"] != choice:
                raise ValueError("decision already resolved with a different choice")
            con.execute("COMMIT"); return get(row["artifact_id"]) or {}, False
        if int(row["revision"]) != int(expected_revision): raise ValueError("decision revision changed")
        now = db.now_ms()
        con.execute("""UPDATE artifact_decisions SET status=?,resolved_choice=?,resolved_at=?,
                     resolved_by='user',revision=revision+1 WHERE decision_id=?""",
                    (choice, choice, now, decision_id))
        con.execute("UPDATE artifacts SET status='completed',updated_at=?,completed_at=? WHERE artifact_id=?",
                    (now, now, row["artifact_id"]))
        artifact = con.execute("SELECT session,reference_id,payload_json FROM artifacts WHERE artifact_id=?",
                               (row["artifact_id"],)).fetchone()
        con.execute(
            """INSERT INTO decision_deliveries(decision_id,artifact_id,session,question,
                   context,reference_id,payload_json,choice,status,created_at)
               VALUES(?,?,?,?,?,?,?,?, 'pending',?) ON CONFLICT(decision_id) DO NOTHING""",
            (decision_id, row["artifact_id"], artifact["session"], row["question"],
             row["context"], artifact["reference_id"], artifact["payload_json"], choice, now))
        con.execute("COMMIT"); return get(row["artifact_id"]) or {}, True
    except BaseException:
        con.execute("ROLLBACK"); raise


def attention() -> list[dict]:
    _expire_decisions()
    rows = db.conn().execute(
        """SELECT d.*,a.agent_id,a.session,a.title,a.summary,a.created_at,
                  g.persona AS agent_name FROM artifact_decisions d JOIN artifacts a
                  ON a.artifact_id=d.artifact_id JOIN agents g ON g.agent_id=a.agent_id
            WHERE d.status='pending' AND a.deleted_at IS NULL AND g.deleted_at IS NULL
              AND (d.expires_at IS NULL OR d.expires_at>?) ORDER BY a.created_at""",
        (db.now_ms(),)).fetchall()
    return [{**dict(row), "kind": "decision", "priority": 100} for row in rows]


def pending_deliveries() -> list[dict]:
    return [dict(row) for row in db.conn().execute(
        """SELECT x.* FROM decision_deliveries x JOIN agents g ON g.session=x.session
            WHERE x.status='pending' AND g.deleted_at IS NULL ORDER BY x.created_at""")]


def mark_delivered(decision_id: str) -> None:
    db.conn().execute(
        "UPDATE decision_deliveries SET status='delivered',delivered_at=? WHERE decision_id=?",
        (db.now_ms(), decision_id))


def delivery_pending(decision_id: str) -> bool:
    row = db.conn().execute(
        "SELECT status FROM decision_deliveries WHERE decision_id=?", (decision_id,)
    ).fetchone()
    return bool(row and row["status"] == "pending")


def cancel_for_agent(agent_id: str) -> None:
    now = db.now_ms(); con = db.conn()
    agent = con.execute("SELECT session FROM agents WHERE agent_id=?", (agent_id,)).fetchone()
    if agent:
        con.execute(
            "UPDATE decision_deliveries SET status='cancelled' WHERE session=? AND status='pending'",
            (agent["session"],))
    ids = [row["artifact_id"] for row in con.execute(
        "SELECT artifact_id FROM artifacts WHERE agent_id=? AND type='decision' AND status='active'",
        (agent_id,)).fetchall()]
    if ids:
        con.executemany(
            "UPDATE artifact_decisions SET status='cancelled',revision=revision+1 WHERE artifact_id=? AND status='pending'",
            [(artifact_id,) for artifact_id in ids])
        con.executemany(
            "UPDATE artifacts SET status='cancelled',updated_at=?,completed_at=? WHERE artifact_id=?",
            [(now, now, artifact_id) for artifact_id in ids])
        con.executemany(
            "UPDATE decision_deliveries SET status='cancelled' WHERE decision_id IN (SELECT decision_id FROM artifact_decisions WHERE artifact_id=?)",
            [(artifact_id,) for artifact_id in ids])
    con.execute(
        """UPDATE artifacts SET status='cancelled',updated_at=?,completed_at=?
             WHERE agent_id=? AND status='active'""",
        (now, now, agent_id))


def _expire_decisions() -> None:
    now = db.now_ms(); con = db.conn()
    con.execute("BEGIN IMMEDIATE")
    try:
        rows = con.execute(
            """SELECT d.decision_id,d.artifact_id,d.question,d.context,a.session,
                      a.reference_id,a.payload_json
                 FROM artifact_decisions d JOIN artifacts a ON a.artifact_id=d.artifact_id
                WHERE d.status='pending' AND d.expires_at IS NOT NULL AND d.expires_at<=?""",
            (now,)).fetchall()
        if rows:
            ids = [str(row["artifact_id"]) for row in rows]
            con.execute(
                "UPDATE artifact_decisions SET status='expired',revision=revision+1 WHERE status='pending' AND expires_at IS NOT NULL AND expires_at<=?",
                (now,))
            con.executemany(
                "UPDATE artifacts SET status='expired',updated_at=?,completed_at=? WHERE artifact_id=?",
                [(now, now, artifact_id) for artifact_id in ids])
            con.executemany(
                """INSERT INTO decision_deliveries(decision_id,artifact_id,session,question,
                       context,reference_id,payload_json,choice,status,created_at)
                   VALUES(?,?,?,?,?,?,?,'expired','pending',?)
                   ON CONFLICT(decision_id) DO NOTHING""",
                [(row["decision_id"], row["artifact_id"], row["session"], row["question"],
                  row["context"], row["reference_id"], row["payload_json"], now) for row in rows])
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise
