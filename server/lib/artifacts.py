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

TYPES = {"decision", "question", "plan", "document", "research", "code_change", "data",
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
    if type in {"decision", "question", "plan"}:
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
    if type.strip().lower() in {"decision", "question", "plan"}:
        raise ValueError(f"{type} must use its dedicated lifecycle endpoint")
    return _create(session=session, type=type, title=title, summary=summary,
                   status=status, reference_id=reference_id, payload=payload,
                   artifact_id=artifact_id)


def _create(*, session: str, type: str, title: str, summary: str = "",
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
            _create(session=session, type="plan", title=title, status="active",
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
        "UPDATE artifacts SET status=?,updated_at=MAX(?,updated_at+1),completed_at=? WHERE type='plan' AND reference_id=?",
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
    if item["type"] in {"decision", "question"}:
        decision = db.conn().execute(
            "SELECT * FROM artifact_decisions WHERE artifact_id=?", (item["artifact_id"],)).fetchone()
        item["decision"] = (_public_decision(decision, archived_at=item["archived_at"])
                            if decision else None)
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
    if current["type"] in {"decision", "question", "plan"}:
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
    now = max(db.now_ms(), current["updated_at"] + 1)
    completed = (current["completed_at"] if status == current["status"] else
                 (now if status in {"completed", "cancelled", "expired"} else None))
    db.conn().execute(
        """UPDATE artifacts SET title=?,summary=?,status=?,payload_json=?,updated_at=MAX(?,updated_at+1),
                  completed_at=? WHERE artifact_id=?""",
        (title[:300],
         strip_hidden_blocks(str(data.get("summary") if "summary" in data else current["summary"]))[:4000],
         status, json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
         now, completed, artifact_id))
    return get(artifact_id) or {}


def _decision_text(value: Any, field: str, limit: int, *, required: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    value = strip_hidden_blocks(value).strip()
    if required and not value:
        raise ValueError(f"{field} required")
    if len(value) > limit:
        raise ValueError(f"{field} too large")
    return value


def _integer(value: Any, field: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    if value < minimum or value > 9007199254740991:
        raise ValueError(f"{field} is outside the supported range")
    return value


def _decision_options(options: Any) -> list[dict]:
    if not isinstance(options, list) or not 2 <= len(options) <= 3:
        raise ValueError("questions require 2 or 3 options")
    normalized, ids = [], set()
    for option in options:
        if not isinstance(option, dict) or set(option) - {"id", "label", "description"}:
            raise ValueError("options must contain id, label and optional description")
        identifier = _decision_text(option.get("id"), "option id", 80, required=True)
        if not _VALID_ID.fullmatch(identifier) or identifier in ids:
            raise ValueError("option ids must be valid and unique")
        label = _decision_text(option.get("label"), "option label", 200, required=True)
        item = {"id": identifier, "label": label}
        if "description" in option:
            item["description"] = _decision_text(option["description"], "option description", 1000)
        normalized.append(item)
        ids.add(identifier)
    return normalized


def _priority(item: dict) -> int:
    return (200 if item["blocks_progress"] else 100) + (10 if item["urgency"] == "time_sensitive" else 0)


def _public_decision(row, *, archived_at: int | None = None) -> dict:
    item = dict(row)
    item["options"] = json.loads(item.pop("options_json"))
    item["answer"] = json.loads(item.pop("answer_json") or "null")
    item["allow_custom_text"] = bool(item["allow_custom_text"])
    item["blocks_progress"] = bool(item["blocks_progress"])
    item["priority"] = _priority(item)
    item["archived_at"] = archived_at
    item["delivery_pending"] = delivery_pending(item["decision_id"])
    return item


def create_decision(*, session: str, title: str, question: str, context: str = "",
                    yes_label: str = "Yes", no_label: str = "No", payload: Any = None,
                    reference_id: str = "", expires_at: int | None = None,
                    response_type: str = "approval", options: Any = None,
                    allow_custom_text: bool | None = None,
                    recommended_option_id: str | None = None,
                    blocks_progress: bool = False, priority_reason: str = "",
                    urgency: str = "normal", response_effort: str = "review",
                    deadline_at: int | None = None) -> dict:
    question = _decision_text(question, "decision question", 4000, required=True)
    context = _decision_text(context, "decision context", 16000)
    yes_label = _decision_text(yes_label, "decision label", 80) or "Yes"
    no_label = _decision_text(no_label, "decision label", 80) or "No"
    if response_type not in ("approval", "single_choice"):
        raise ValueError("unsupported response_type")
    if allow_custom_text is None:
        allow_custom_text = response_type == "single_choice"
    if not isinstance(allow_custom_text, bool):
        raise ValueError("allow_custom_text must be a boolean")
    if not isinstance(blocks_progress, bool):
        raise ValueError("blocks_progress must be a boolean")
    if urgency not in ("normal", "time_sensitive"):
        raise ValueError("unsupported urgency")
    if response_effort not in ("quick", "short", "review"):
        raise ValueError("unsupported response_effort")
    priority_reason = _decision_text(priority_reason, "priority_reason", 2000)
    if (blocks_progress or urgency != "normal") and not priority_reason:
        raise ValueError("blocking or time-sensitive requests require priority_reason")
    if response_type == "single_choice":
        options = _decision_options(options)
        if recommended_option_id is not None:
            recommended_option_id = _decision_text(
                recommended_option_id, "recommended_option_id", 80, required=True)
            if recommended_option_id not in {option["id"] for option in options}:
                raise ValueError("recommended_option_id must identify an option")
    else:
        if options is not None or recommended_option_id is not None or allow_custom_text:
            raise ValueError("approval requests do not support options or custom text")
        options = []
    if expires_at is not None:
        _integer(expires_at, "expires_at")
        if expires_at > db.now_ms() + 365 * 24 * 60 * 60 * 1000:
            raise ValueError("expires_at is too far in the future")
    if deadline_at is not None:
        _integer(deadline_at, "deadline_at")
    con = db.conn(); con.execute("BEGIN IMMEDIATE")
    try:
        artifact = _create(session=session, type="question" if response_type == "single_choice" else "decision",
                           title=title, summary=question, status="active",
                           reference_id=reference_id, payload=payload)
        con.execute(
            """INSERT INTO artifact_decisions(decision_id,artifact_id,question,context,yes_label,
                   no_label,expires_at,response_type,options_json,allow_custom_text,
                   recommended_option_id,blocks_progress,priority_reason,urgency,
                   response_effort,deadline_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (_new("decision"), artifact["artifact_id"], question, context,
             yes_label, no_label, expires_at, response_type,
             json.dumps(options, ensure_ascii=False, separators=(",", ":")),
             allow_custom_text, recommended_option_id, blocks_progress, priority_reason,
             urgency, response_effort, deadline_at))
        con.execute("COMMIT")
        return get(artifact["artifact_id"]) or {}
    except BaseException:
        con.execute("ROLLBACK"); raise


def _answer(row, *, choice: Any, answer: Any) -> tuple[str, dict]:
    if row["response_type"] == "approval":
        if answer is not None or not isinstance(choice, str):
            raise ValueError("approval requires an explicit accepted or rejected choice")
        choice = choice.strip().lower()
        if choice not in {"accepted", "rejected"}:
            raise ValueError("invalid decision choice")
        return choice, {"choice": choice}
    if row["response_type"] != "single_choice":
        raise ValueError("unsupported response_type")
    if choice is not None:
        raise ValueError("questions require a typed answer, not a binary choice")
    if not isinstance(answer, dict) or set(answer) not in ({"option_id"}, {"text"}):
        raise ValueError("answer must contain exactly one of option_id or text")
    if "option_id" in answer:
        identifier = answer["option_id"]
        if not isinstance(identifier, str):
            raise ValueError("answer option_id must be a string")
        for option in json.loads(row["options_json"]):
            if option["id"] == identifier.strip():
                return "answered", {"option_id": option["id"], "label": option["label"]}
        raise ValueError("answer option_id does not identify an option")
    if not row["allow_custom_text"]:
        raise ValueError("custom text answers are not enabled")
    # This is the user's actual wording, not agent-authored presentation text.
    # Preserve casing, internal whitespace and markup in the durable snapshot.
    text = answer["text"]
    if not isinstance(text, str) or not text.strip() or len(text.strip()) > 4000:
        raise ValueError("answer text must be a nonempty string of at most 4000 characters")
    return "answered", {"text": text.strip()}


def _queue_delivery(con, row, *, choice: str, answer: dict | None, now: int) -> None:
    artifact = con.execute("SELECT session,reference_id,payload_json FROM artifacts WHERE artifact_id=?",
                           (row["artifact_id"],)).fetchone()
    con.execute(
        """INSERT INTO decision_deliveries(decision_id,artifact_id,session,question,
               context,reference_id,payload_json,choice,status,created_at,response_type,answer_json)
           VALUES(?,?,?,?,?,?,?,?, 'pending',?,?,?) ON CONFLICT(decision_id) DO NOTHING""",
        (row["decision_id"], row["artifact_id"], artifact["session"], row["question"],
         row["context"], artifact["reference_id"], artifact["payload_json"], choice, now,
         row["response_type"], json.dumps(answer, ensure_ascii=False) if answer is not None else None))


def resolve(decision_id: str, *, expected_revision: int,
            choice: str | None = None, answer: Any = None) -> tuple[dict, bool]:
    _integer(expected_revision, "expected_revision", minimum=1)
    _expire_decisions()
    con = db.conn(); con.execute("BEGIN IMMEDIATE")
    try:
        row = con.execute("SELECT * FROM artifact_decisions WHERE decision_id=?", (decision_id,)).fetchone()
        if not row: raise ValueError("decision not found")
        outcome, normalized = _answer(row, choice=choice, answer=answer)
        if row["status"] != "pending":
            if row["status"] == "expired":
                raise ValueError("decision expired")
            if row["status"] != outcome or json.loads(row["answer_json"] or "null") != normalized:
                raise ValueError("decision already resolved with a different choice or answer")
            con.execute("COMMIT"); return get(row["artifact_id"]) or {}, False
        if row["expires_at"] is not None and row["expires_at"] <= db.now_ms():
            raise ValueError("decision expired")
        if row["revision"] != expected_revision: raise ValueError("decision revision changed")
        artifact = get(row["artifact_id"])
        if not artifact: raise ValueError("artifact not found")
        now = max(db.now_ms(), artifact["updated_at"] + 1)
        con.execute("""UPDATE artifact_decisions SET status=?,resolved_choice=?,answer_json=?,resolved_at=?,
                     resolved_by='user',revision=revision+1 WHERE decision_id=?""",
                    (outcome, outcome, json.dumps(normalized, ensure_ascii=False), now, decision_id))
        con.execute("UPDATE artifacts SET status='completed',updated_at=?,completed_at=? WHERE artifact_id=?",
                    (now, now, row["artifact_id"]))
        _queue_delivery(con, row, choice=outcome, answer=normalized, now=now)
        con.execute("COMMIT"); return get(row["artifact_id"]) or {}, True
    except BaseException:
        con.execute("ROLLBACK"); raise


def dismiss(decision_id: str, *, expected_revision: int) -> tuple[dict, bool]:
    """User discards a pending request without answering or granting permission."""
    _integer(expected_revision, "expected_revision", minimum=1)
    _expire_decisions()
    con = db.conn(); con.execute("BEGIN IMMEDIATE")
    try:
        row = con.execute("SELECT * FROM artifact_decisions WHERE decision_id=?", (decision_id,)).fetchone()
        if not row: raise ValueError("decision not found")
        if row["status"] == "cancelled" and row["resolved_choice"] == "dismissed":
            con.execute("COMMIT"); return get(row["artifact_id"]) or {}, False
        if row["status"] != "pending": raise ValueError("decision is no longer pending")
        if row["expires_at"] is not None and row["expires_at"] <= db.now_ms():
            raise ValueError("decision expired")
        if row["revision"] != expected_revision: raise ValueError("decision revision changed")
        artifact = get(row["artifact_id"])
        if not artifact: raise ValueError("artifact not found")
        now = max(db.now_ms(), artifact["updated_at"] + 1)
        con.execute("""UPDATE artifact_decisions SET status='cancelled',resolved_choice='dismissed',
                       resolved_by='user',resolved_at=?,revision=revision+1 WHERE decision_id=?""",
                    (now, decision_id))
        con.execute("UPDATE artifacts SET status='cancelled',updated_at=?,completed_at=? WHERE artifact_id=?",
                    (now, now, row["artifact_id"]))
        _queue_delivery(con, row, choice="dismissed", answer=None, now=now)
        con.execute("COMMIT"); return get(row["artifact_id"]) or {}, True
    except BaseException:
        con.execute("ROLLBACK"); raise


def archive(artifact_id: str, *, archived: bool, expected_updated_at: int) -> tuple[dict, bool]:
    """Hide/restore an inbox item independently of its underlying work state."""
    if not isinstance(archived, bool): raise ValueError("archived must be a boolean")
    _integer(expected_updated_at, "expected_updated_at")
    con = db.conn(); con.execute("BEGIN IMMEDIATE")
    try:
        current = get(artifact_id)
        if not current: raise ValueError("artifact not found")
        if (current["archived_at"] is not None) == archived:
            con.execute("COMMIT"); return current, False
        if current["updated_at"] != expected_updated_at: raise ValueError("artifact changed")
        now = max(db.now_ms(), current["updated_at"] + 1)
        con.execute("UPDATE artifacts SET archived_at=?,updated_at=? WHERE artifact_id=?",
                    (now if archived else None, now, artifact_id))
        con.execute("COMMIT"); return get(artifact_id) or {}, True
    except BaseException:
        con.execute("ROLLBACK"); raise


def discard(artifact_id: str, *, expected_updated_at: int) -> tuple[dict, bool]:
    """Soft-delete a record only; never delete files or stop its agent/job."""
    _integer(expected_updated_at, "expected_updated_at")
    con = db.conn(); con.execute("BEGIN IMMEDIATE")
    try:
        raw = con.execute("""SELECT a.*,g.persona AS agent_name FROM artifacts a JOIN agents g
                              ON g.agent_id=a.agent_id WHERE artifact_id=?""", (artifact_id,)).fetchone()
        if not raw: raise ValueError("artifact not found")
        current = _public(raw)
        if current["type"] in {"decision", "question"}:
            raise ValueError("decisions and questions must use their dedicated dismissal endpoint")
        if current["deleted_at"] is not None:
            con.execute("COMMIT"); return current, False
        if current["updated_at"] != expected_updated_at: raise ValueError("artifact changed")
        now = max(db.now_ms(), current["updated_at"] + 1)
        con.execute("UPDATE artifacts SET deleted_at=?,updated_at=? WHERE artifact_id=?",
                    (now, now, artifact_id))
        con.execute("COMMIT")
        return {**current, "deleted_at": now, "updated_at": now}, True
    except BaseException:
        con.execute("ROLLBACK"); raise


def attention(*, include_questions: bool = False, include_archived: bool = False) -> list[dict]:
    _expire_decisions()
    rows = db.conn().execute(
        """SELECT d.*,a.agent_id,a.session,a.title,a.summary,a.created_at,a.updated_at,a.archived_at,
                  g.persona AS agent_name FROM artifact_decisions d JOIN artifacts a
                  ON a.artifact_id=d.artifact_id JOIN agents g ON g.agent_id=a.agent_id
            WHERE d.status='pending' AND a.deleted_at IS NULL AND g.deleted_at IS NULL
              AND (? OR d.response_type='approval') AND (? OR a.archived_at IS NULL)
              AND (d.expires_at IS NULL OR d.expires_at>?)""",
        (include_questions, include_archived, db.now_ms())).fetchall()
    items = [{**_public_decision(row, archived_at=row["archived_at"]),
              "kind": "question" if row["response_type"] == "single_choice" else "decision"}
             for row in rows]
    return sorted(items, key=lambda item: (
        -item["priority"], item["deadline_at"] is None, item["deadline_at"] or 0,
        item["created_at"], item["decision_id"]))


def pending_deliveries() -> list[dict]:
    return [{**dict(row), "answer": json.loads(row["answer_json"] or "null")}
            for row in db.conn().execute(
        """SELECT x.* FROM decision_deliveries x JOIN agents g ON g.session=x.session
            WHERE x.status='pending' AND g.deleted_at IS NULL ORDER BY x.created_at""")]


def format_delivery_prompt(delivery: dict) -> str:
    """One durable snapshot interpretation for foreground and retry delivery."""
    choice = delivery["choice"]
    response_type = delivery.get("response_type", "approval")
    answer = delivery.get("answer")
    if answer is None:
        answer = json.loads(delivery.get("answer_json") or "null")
    if choice == "expired":
        outcome = ("The request expired without an answer or approval. Do not infer a choice or "
                   "perform the protected action. Continue independent work only.")
    elif choice == "dismissed":
        outcome = ("The user discarded this request. This is not an answer or approval. Do not "
                   "guess permission or repeat the unchanged request. Continue independent work only.")
    elif response_type == "single_choice" and choice == "answered" and isinstance(answer, dict):
        outcome = ("The user answered this clarification: " + json.dumps(answer, ensure_ascii=False)
                   + ". Continue using this answer. This does not grant approval for unrelated protected actions.")
    elif response_type == "approval" and choice in {"accepted", "rejected"}:
        outcome = (f"The user chose: {choice}. " + (
            "Approval applies only to the described action. Revalidate it before acting."
            if choice == "accepted" else "Do not perform the protected action."))
    else:
        raise ValueError("invalid durable decision delivery")
    return ("[Clarp decision resolved]\n"
            f"Decision ID: {delivery['decision_id']}\nArtifact ID: {delivery['artifact_id']}\n"
            f"Question: {delivery['question']}\nContext: {delivery['context']}\n"
            f"Reference: {delivery['reference_id']}\nPayload: {delivery['payload_json']}\n{outcome}")


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
        "SELECT artifact_id FROM artifacts WHERE agent_id=? AND type IN ('decision','question') AND status='active'",
        (agent_id,)).fetchall()]
    if ids:
        con.executemany(
            "UPDATE artifact_decisions SET status='cancelled',revision=revision+1 WHERE artifact_id=? AND status='pending'",
            [(artifact_id,) for artifact_id in ids])
        con.executemany(
            "UPDATE artifacts SET status='cancelled',updated_at=MAX(?,updated_at+1),completed_at=? WHERE artifact_id=?",
            [(now, now, artifact_id) for artifact_id in ids])
        con.executemany(
            "UPDATE decision_deliveries SET status='cancelled' WHERE decision_id IN (SELECT decision_id FROM artifact_decisions WHERE artifact_id=?)",
            [(artifact_id,) for artifact_id in ids])
    con.execute(
        """UPDATE artifacts SET status='cancelled',updated_at=MAX(?,updated_at+1),completed_at=?
             WHERE agent_id=? AND status='active'""",
        (now, now, agent_id))


def _expire_decisions() -> None:
    now = db.now_ms(); con = db.conn()
    con.execute("BEGIN IMMEDIATE")
    try:
        rows = con.execute(
            """SELECT d.*,a.updated_at
                 FROM artifact_decisions d JOIN artifacts a ON a.artifact_id=d.artifact_id
                WHERE d.status='pending' AND d.expires_at IS NOT NULL AND d.expires_at<=?""",
            (now,)).fetchall()
        if rows:
            ids = [str(row["artifact_id"]) for row in rows]
            con.execute(
                "UPDATE artifact_decisions SET status='expired',resolved_at=?,revision=revision+1 WHERE status='pending' AND expires_at IS NOT NULL AND expires_at<=?",
                (now, now))
            con.executemany(
                "UPDATE artifacts SET status='expired',updated_at=MAX(?,updated_at+1),completed_at=? WHERE artifact_id=?",
                [(now, now, artifact_id) for artifact_id in ids])
            for row in rows:
                _queue_delivery(con, row, choice="expired", answer=None, now=now)
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise
