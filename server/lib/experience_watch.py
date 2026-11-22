"""The user's experience, watched from Host state (issue 703); the owner hears first.

Every `interval_seconds` (300) the Host reads its own state, never a test and never a model:

- wait:      user messages that waited longer than `wait_seconds` before a running turn read them,
             per agent (steered and waited counted apart);
- queue:     a queue paused while a user message is in it, and queue rows stuck in queued,
             claimed or launch_unknown past `queue_stuck_seconds`;
- app_crash_<platform>: per device, crash/exception/MetricKit events in the client log store
             (lib.client_events, when this Host has it) and relaunch bursts (many POST /devices or
             GET /server-info from one device), with the app build in use;
- sends:     the user's sends that failed or were refused;
- skills:    release skills not linked for a backend;
- cards:     open decision cards older than `card_age_hours`;
- release:   a check job that held a release or install longer than `release_hold_minutes`.

Each finding is one row per (kind, subject), open until a pass no longer sees it, with first_seen,
last_seen, count (the passes that saw it) and detail; bounded (MAX_ROWS, cleared rows kept
CLEARED_KEEP_MS). `clarp-admin experience [--since 1h] [--open]` prints them.

When a finding opens, the owner named for its kind in `[experience_watch] owners` gets one
message, and one more when it clears; never again while it stays open. The front desk
(`front_desk`) gets a one-line summary once a day at `summary_hour`. A kind with no owner is
stored only. Messages go through the Host's own agent-to-agent send from EXPERIENCE_SENDER.

Off unless `[experience_watch] enabled = true`.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

EXPERIENCE_SENDER = "experience-watch"
MAX_ROWS = 2000
CLEARED_KEEP_MS = 7 * 24 * 3600 * 1000
DETAIL_CHARS = 2000

DEFAULT_THRESHOLDS: dict[str, float] = {
    "wait_seconds": 60,          # a user message waiting longer than this before it was read
    "wait_count": 1,             # ... this many times in the window opens `wait`
    "queue_stuck_seconds": 600,  # a queue row in queued/claimed/launch_unknown this long
    "crash_events": 10,          # crash/exception/MetricKit events from one device in the window
    "relaunch_burst": 5,         # POST /devices or GET /server-info from one device in the window
    "window_minutes": 15,
    "failed_sends": 1,           # the user's failed or refused sends in the window
    "card_age_hours": 24,
    "release_hold_minutes": 10,
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS experience_findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    subject TEXT NOT NULL,
    first_seen INTEGER NOT NULL,
    last_seen INTEGER NOT NULL,
    count INTEGER NOT NULL DEFAULT 1,
    detail TEXT NOT NULL DEFAULT '',
    cleared_at INTEGER,
    notified_open TEXT,
    notified_clear TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_experience_open
    ON experience_findings(kind, subject) WHERE cleared_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_experience_seen ON experience_findings(last_seen);
CREATE TABLE IF NOT EXISTS experience_watch_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

_ready: set[str] = set()
_ready_lock = threading.Lock()


@dataclass(frozen=True)
class Finding:
    kind: str
    subject: str
    detail: str


@dataclass(frozen=True)
class Settings:
    enabled: bool = False
    interval_seconds: float = 300.0
    summary_hour: int = 9
    thresholds: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_THRESHOLDS))
    owners: dict[str, str] = field(default_factory=dict)
    front_desk: str = ""

    @classmethod
    def from_config(cls, cfg=None) -> "Settings":
        if cfg is None:
            from . import config
            cfg = config.load()
        raw = getattr(cfg, "experience_watch", {}) or {}
        thresholds = dict(DEFAULT_THRESHOLDS)
        for key, value in (raw.get("thresholds") or {}).items() if isinstance(raw.get("thresholds"), dict) else ():
            try:
                if key in thresholds and float(value) >= 0:
                    thresholds[key] = float(value)
            except (TypeError, ValueError):
                pass
        owners = {str(k): str(v).strip() for k, v in (raw.get("owners") or {}).items()
                  if isinstance(v, str) and v.strip()} if isinstance(raw.get("owners"), dict) else {}
        try:
            interval = max(30.0, float(raw.get("interval_seconds", 300)))
        except (TypeError, ValueError):
            interval = 300.0
        try:
            hour = int(raw.get("summary_hour", 9)) % 24
        except (TypeError, ValueError):
            hour = 9
        return cls(enabled=raw.get("enabled") is True, interval_seconds=interval, summary_hour=hour,
                   thresholds=thresholds, owners=owners,
                   front_desk=str(raw.get("front_desk") or "").strip())

    def owner(self, kind: str) -> str:
        """The session told about a finding of ``kind``: its own entry, else its family's
        (app_crash_ios -> app_crash, wait -> queue, sends -> queue)."""
        for key in (kind, kind.rsplit("_", 1)[0] if kind.startswith("app_crash_") else "",
                    "queue" if kind in ("wait", "sends") else ""):
            if key and self.owners.get(key):
                return self.owners[key]
        return ""


# ---------------------------------------------------------------- storage

def _conn():
    from . import telemetry
    con = telemetry.conn()
    path = str(telemetry.TELEMETRY_PATH)
    if path not in _ready:
        with _ready_lock:
            if path not in _ready:
                con.executescript(SCHEMA)
                _ready.add(path)
    return con


def findings(*, since_ms: int | None = None, open_only: bool = False, limit: int = 500) -> list[dict]:
    """Stored findings, newest last_seen first."""
    where, params = [], []
    if since_ms is not None:
        where.append("last_seen >= ?"); params.append(int(since_ms))
    if open_only:
        where.append("cleared_at IS NULL")
    sql = ("SELECT kind, subject, first_seen, last_seen, count, detail, cleared_at, notified_open, "
           "notified_clear FROM experience_findings" + (" WHERE " + " AND ".join(where) if where else "")
           + " ORDER BY last_seen DESC, id DESC LIMIT ?")
    params.append(max(1, min(int(limit), MAX_ROWS)))
    return [dict(r) for r in _conn().execute(sql, params).fetchall()]


def _meta(key: str) -> str:
    row = _conn().execute("SELECT value FROM experience_watch_meta WHERE key=?", (key,)).fetchone()
    return str(row[0]) if row else ""


def _set_meta(key: str, value: str) -> None:
    _conn().execute("INSERT INTO experience_watch_meta(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def reconcile(found: Iterable[Finding], *, now_ms: int) -> tuple[list[dict], list[dict]]:
    """Store this pass: a new (kind, subject) opens, one seen again is updated, an open one not
    seen is cleared. Returns (opened, cleared) rows."""
    seen = {(f.kind, f.subject): f for f in found}
    con = _conn()
    opened, cleared = [], []
    con.execute("BEGIN IMMEDIATE")
    try:
        current = {(r["kind"], r["subject"]): dict(r) for r in con.execute(
            "SELECT id, kind, subject, first_seen, last_seen, count, detail FROM experience_findings "
            "WHERE cleared_at IS NULL").fetchall()}
        for key, finding in seen.items():
            detail = finding.detail[:DETAIL_CHARS]
            if key in current:
                con.execute("UPDATE experience_findings SET last_seen=?, count=count+1, detail=? WHERE id=?",
                            (now_ms, detail, current[key]["id"]))
            else:
                cur = con.execute(
                    "INSERT INTO experience_findings(kind, subject, first_seen, last_seen, count, detail) "
                    "VALUES(?, ?, ?, ?, 1, ?)", (finding.kind, finding.subject, now_ms, now_ms, detail))
                opened.append({"id": cur.lastrowid, "kind": finding.kind, "subject": finding.subject,
                               "first_seen": now_ms, "detail": detail})
        for key, row in current.items():
            if key not in seen:
                con.execute("UPDATE experience_findings SET cleared_at=? WHERE id=?", (now_ms, row["id"]))
                cleared.append({**row, "cleared_at": now_ms})
        con.execute("DELETE FROM experience_findings WHERE cleared_at IS NOT NULL AND cleared_at < ?",
                    (now_ms - CLEARED_KEEP_MS,))
        con.execute("DELETE FROM experience_findings WHERE id IN (SELECT id FROM experience_findings "
                    "WHERE cleared_at IS NOT NULL ORDER BY id DESC LIMIT -1 OFFSET ?)", (MAX_ROWS,))
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    return opened, cleared


def _mark(row_id: int, column: str, session: str) -> None:
    _conn().execute(f"UPDATE experience_findings SET {column}=? WHERE id=?", (session, row_id))


# ---------------------------------------------------------------- one pass

Check = Callable[[Settings, int], list[Finding]]


def run_pass(settings: Settings | None = None, *, now_ms: int | None = None,
             send: Callable[[str, str], Any] | None = None,
             checks: Iterable[Check] | None = None) -> dict:
    """Compute, store, tell. Returns what it did, for the log and tests."""
    from .log import log, log_exception
    settings = settings or Settings.from_config()
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    found: list[Finding] = []
    for check in (checks if checks is not None else CHECKS):
        try:
            found.extend(check(settings, now))
        except Exception as exc:  # noqa: BLE001 - one source failing never hides the others
            log_exception("experienceCheckFail", exc, detail=getattr(check, "__name__", ""))
    opened, cleared = reconcile(found, now_ms=now)
    send = send or deliver
    told = 0
    for row in opened:
        owner = settings.owner(row["kind"])
        if owner and _tell(send, owner, f"Experience watch: {row['kind']} opened for {row['subject']}. "
                                        f"{row['detail']} (clarp-admin experience --open)"):
            _mark(row["id"], "notified_open", owner)
            told += 1
    for row in cleared:
        owner = settings.owner(row["kind"])
        if owner and _tell(send, owner, f"Experience watch: {row['kind']} cleared for {row['subject']} "
                                        f"(open since {_clock(row['first_seen'])}, seen {row['count']}x)."):
            _mark(row["id"], "notified_clear", owner)
            told += 1
    summary = _daily_summary(settings, now, send)
    if opened or cleared:
        log("experienceWatch", f"found={len(found)} opened={len(opened)} cleared={len(cleared)} told={told}")
    return {"found": found, "opened": opened, "cleared": cleared, "told": told, "summary": summary}


def _clock(ms: int) -> str:
    return time.strftime("%H:%M", time.localtime(ms / 1000))


def _tell(send, session: str, text: str) -> bool:
    from .log import log_exception
    try:
        send(session, text)
        return True
    except Exception as exc:  # noqa: BLE001 - the finding stays stored; the next opening tells
        log_exception("experienceTellFail", exc, detail=session)
        return False


def _daily_summary(settings: Settings, now_ms: int, send) -> str:
    """One line to the front desk, once a day, at or after summary_hour local time."""
    if not settings.front_desk:
        return ""
    local = time.localtime(now_ms / 1000)
    today = time.strftime("%Y-%m-%d", local)
    if local.tm_hour < settings.summary_hour or _meta("summary_date") == today:
        return ""
    day_ago = now_ms - 24 * 3600 * 1000
    rows = findings(since_ms=day_ago)
    still = [r for r in rows if r["cleared_at"] is None]
    kinds: dict[str, int] = {}
    for r in still:
        kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
    opened_today = sum(1 for r in rows if r["first_seen"] >= day_ago)
    line = (f"Experience watch, last 24 h: {opened_today} opened, "
            f"{sum(1 for r in rows if r['cleared_at'] is not None)} cleared, {len(still)} still open"
            + (" (" + ", ".join(f"{k} {n}" for k, n in sorted(kinds.items())) + ")" if kinds else "")
            + ". clarp-admin experience --since 24h")
    if _tell(send, settings.front_desk, line):
        _set_meta("summary_date", today)
        return line
    return ""


# ---------------------------------------------------------------- the worker

class ExperienceWatchWorker:
    """Runs a pass every interval_seconds on its own thread, the first one interval_seconds
    after start (out of the start's busiest seconds). Re-reads the settings each pass, so a
    configuration edit takes effect without a restart; a pass while off does nothing."""

    def __init__(self, settings_fn: Callable[[], Settings] = Settings.from_config):
        self._settings = settings_fn
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> "ExperienceWatchWorker":
        self._thread = threading.Thread(target=self._run, name="experience-watch", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self._interval()):
            self._once()

    def _interval(self) -> float:
        try:
            return self._settings().interval_seconds
        except Exception:  # noqa: BLE001
            return 300.0

    def _once(self) -> None:
        from . import db, telemetry
        from .log import log_exception
        try:
            settings = self._settings()
            if settings.enabled:
                run_pass(settings)
        except Exception as exc:  # noqa: BLE001 - never take the Host down for the watcher
            log_exception("experienceWatchFail", exc, detail="")
        finally:
            db.close_local()
            telemetry.close_local()


def worker_enabled() -> bool:
    """Off under tests (CLARP_EXPERIENCE_WATCH_WORKER=0); the setting itself is read each pass."""
    import os
    return os.environ.get("CLARP_EXPERIENCE_WATCH_WORKER", "1") != "0"
