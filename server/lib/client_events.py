"""The apps' client log and crash events, always kept on the Host, bounded (feature client_events_kept).

POST /clog used to store nothing while the diagnostics setting was off, so an app that crashed
left no trace an agent could read. This store keeps, whatever that setting says:

- every event whose level or name says error, crash, exception, fatal or hang, and every
  MetricKit / crash report (POST /crash), as `keep` rows;
- the CONTEXT_MS (30 minutes) before such an event, from the same device, pinned with it;
- every other event for CONTEXT_MS after it arrives, then dropped.

Bounds, oldest first: MAX_AGE_MS (7 days), MAX_EVENTS (50 000) and MAX_BYTES (50 MB) in total,
and DEVICE_MAX_EVENTS / DEVICE_MAX_BYTES per device. A detail is capped at DETAIL_BYTES.

Each row: device (the paired device's id, or 'administrator' / 'local'), client name, build and
contract (X-Clarp-Client), this Host's server_id, received_at, event, level, detail and
trace_id. No token is ever kept: a key named like a token, secret, password, cookie or
authorization has its value replaced, and a bearer value, a Clarp device token or pairing code,
or a `token=` parameter inside any text is cut out (redact()).

It is the owner's own app reporting to the owner's own Host; the diagnostics setting still
decides the fuller capture (every event into diagnostic_events) it always has.

Agents read it: GET /client-log (full scope only) and `clarp-admin client-log`.
"""
from __future__ import annotations

import json
import re
import threading
import time

from . import telemetry

CONTEXT_MS = 30 * 60 * 1000
MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000
MAX_EVENTS = 50_000
MAX_BYTES = 50 * 1024 * 1024
DEVICE_MAX_EVENTS = 20_000
DEVICE_MAX_BYTES = 20 * 1024 * 1024
DETAIL_BYTES = 4096
PRUNE_EVERY_S = 30.0
MAX_READ = 5000

SCHEMA = """
CREATE TABLE IF NOT EXISTS client_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at INTEGER NOT NULL,
    device TEXT NOT NULL,
    client TEXT NOT NULL DEFAULT '',
    client_build TEXT NOT NULL DEFAULT '',
    client_contract INTEGER,
    server_id TEXT NOT NULL DEFAULT '',
    event TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT 'info',
    detail TEXT,
    trace_id TEXT,
    keep INTEGER NOT NULL DEFAULT 0,
    size INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_client_events_received ON client_events(received_at);
CREATE INDEX IF NOT EXISTS idx_client_events_device ON client_events(device, received_at);
CREATE INDEX IF NOT EXISTS idx_client_events_keep ON client_events(keep, received_at);
"""

_IMPORTANT = re.compile(r"error|crash|exception|fatal|hang|fault|metrickit", re.I)
_SECRET_KEY = re.compile(r"token|secret|password|passwd|authorization|cookie|api[_-]?key|credential", re.I)
_SECRET_TEXT = [
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=\-]{8,}"), r"\1 [redacted]"),
    (re.compile(r"\b(cld|clp)_[A-Za-z0-9_\-]{16,}"), r"\1_[redacted]"),
    (re.compile(r"(?i)\b((?:access_|auth_|api_)?token|secret|password|key)=([^&\s\"']+)"), r"\1=[redacted]"),
    (re.compile(r"(?i)(authorization\"?\s*[:=]\s*\"?)[^\",}\s]+"), r"\1[redacted]"),
]

_ready: set[str] = set()
_ready_lock = threading.Lock()
_last_prune = [0.0]


def _conn():
    con = telemetry.conn()
    path = str(telemetry.TELEMETRY_PATH)
    if path not in _ready:
        with _ready_lock:
            if path not in _ready:
                con.executescript(SCHEMA)
                _ready.add(path)
    return con


def _redact_text(text: str) -> str:
    for pattern, replacement in _SECRET_TEXT:
        text = pattern.sub(replacement, text)
    return text


def redact(value, depth=0):
    """`value` with every secret-looking key's value replaced and every secret-looking text cut."""
    if depth > 12:
        return "[deep]"
    if isinstance(value, dict):
        return {str(k)[:80]: ("[redacted]" if _SECRET_KEY.search(str(k)) else redact(v, depth + 1))
                for k, v in list(value.items())[:200]}
    if isinstance(value, list):
        return [redact(v, depth + 1) for v in value[:200]]
    if isinstance(value, str):
        return _redact_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact_text(str(value))


def _detail_text(detail) -> str | None:
    if detail is None or detail == "":
        return None
    text = json.dumps(redact(detail), ensure_ascii=False, separators=(",", ":"), default=str)
    raw = text.encode("utf-8")
    if len(raw) > DETAIL_BYTES:
        text = raw[:DETAIL_BYTES - 16].decode("utf-8", "ignore") + "…[truncated]"
    return text


def important(event: str, level: str) -> bool:
    return bool(_IMPORTANT.search(event or "") or _IMPORTANT.search(level or ""))


def _level(item: dict, event: str) -> str:
    level = str(item.get("level") or "").strip().lower()[:16]
    if level:
        return level
    return "error" if _IMPORTANT.search(event) else "info"


def record(items, *, device: str, client: tuple[str, str, int | None] = ("", "", None),
           server_id: str = "", now_ms: int | None = None) -> int:
    """Keeps a batch of /clog items (dicts); returns how many were kept."""
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    device = (device or "unknown")[:120]
    name, build, contract = client
    rows, pin = [], False
    for item in items:
        if not isinstance(item, dict):
            continue
        event = str(item.get("event") or "log")[:64]
        level = _level(item, event)
        detail = _detail_text(item.get("detail"))
        trace = str(item.get("trace_id") or "")[:80] or None
        keep = 1 if important(event, level) else 0
        pin = pin or bool(keep)
        size = len(event) + len(detail or "") + len(trace or "") + 64
        rows.append((now, device, name[:32], build[:80], contract, server_id[:64], event, level, detail, trace, keep, size))
    if not rows:
        return 0
    con = _conn()
    con.execute("BEGIN IMMEDIATE")
    try:
        con.executemany(
            "INSERT INTO client_events(received_at,device,client,client_build,client_contract,server_id,"
            "event,level,detail,trace_id,keep,size) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        if pin:
            # A crash keeps what led up to it on that device.
            con.execute("UPDATE client_events SET keep=1 WHERE device=? AND keep=0 AND received_at>=?",
                        (device, now - CONTEXT_MS))
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    if time.monotonic() - _last_prune[0] >= PRUNE_EVERY_S:
        prune(now_ms=now)
    return len(rows)


def prune(*, now_ms: int | None = None) -> int:
    """Applies every bound, oldest first. Returns the rows removed."""
    _last_prune[0] = time.monotonic()
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    con = _conn()
    removed = 0
    removed += con.execute("DELETE FROM client_events WHERE received_at < ?", (now - MAX_AGE_MS,)).rowcount
    removed += con.execute("DELETE FROM client_events WHERE keep=0 AND received_at < ?",
                           (now - CONTEXT_MS,)).rowcount
    for row in con.execute("SELECT device, COUNT(*) n, COALESCE(SUM(size),0) b FROM client_events GROUP BY device "
                           "HAVING n > ? OR b > ?", (DEVICE_MAX_EVENTS, DEVICE_MAX_BYTES)).fetchall():
        removed += _trim(con, "WHERE device=?", (row["device"],), DEVICE_MAX_EVENTS, DEVICE_MAX_BYTES)
    removed += _trim(con, "", (), MAX_EVENTS, MAX_BYTES)
    return removed


def _trim(con, where: str, params: tuple, max_events: int, max_bytes: int) -> int:
    """Deletes the oldest rows matching `where` until both bounds hold."""
    total = con.execute(f"SELECT COUNT(*) n, COALESCE(SUM(size),0) b FROM client_events {where}", params).fetchone()
    count, size = int(total["n"]), int(total["b"])
    if count <= max_events and size <= max_bytes:
        return 0
    drop, dropped_bytes, cutoff = 0, 0, None
    for row in con.execute(f"SELECT id, size FROM client_events {where} ORDER BY id", params):
        if count - drop <= max_events and size - dropped_bytes <= max_bytes:
            break
        drop += 1
        dropped_bytes += int(row["size"])
        cutoff = int(row["id"])
    if cutoff is None:
        return 0
    joiner = "AND" if where else "WHERE"
    return con.execute(f"DELETE FROM client_events {where} {joiner} id <= ?", (*params, cutoff)).rowcount


def parse_since(text: str, now_ms: int | None = None) -> int | None:
    """'30m', '2h', '7d', '45s' back from now, or milliseconds since the epoch; '' for none."""
    text = (text or "").strip()
    if not text:
        return None
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    match = re.fullmatch(r"(\d+)\s*([smhd])", text)
    if match:
        unit = {"s": 1000, "m": 60_000, "h": 3_600_000, "d": 86_400_000}[match.group(2)]
        return now - int(match.group(1)) * unit
    if text.isdigit():
        return int(text)
    raise ValueError("since is 30m, 2h, 7d or milliseconds since the epoch")


def read(*, device: str = "", since_ms: int | None = None, level: str = "", event: str = "",
         limit: int = 200) -> list[dict]:
    """The newest events matching every filter given, oldest first (at most `limit`, <= MAX_READ).
    `level`: that level or worse ('error' includes 'fault', 'critical', 'fatal')."""
    where, params = [], []
    if device:
        where.append("device = ?"); params.append(device)
    if since_ms is not None:
        where.append("received_at >= ?"); params.append(int(since_ms))
    if level:
        levels = {"error": ("error", "fault", "critical", "fatal"),
                  "warning": ("warning", "warn", "error", "fault", "critical", "fatal")}.get(level.lower(), (level.lower(),))
        where.append(f"level IN ({','.join('?' * len(levels))})"); params.extend(levels)
    if event:
        where.append("event = ?"); params.append(event)
    sql = ("SELECT received_at, device, client, client_build, client_contract, server_id, event, level, detail, "
           "trace_id, keep FROM client_events" + (" WHERE " + " AND ".join(where) if where else "")
           + " ORDER BY id DESC LIMIT ?")
    params.append(max(1, min(int(limit), MAX_READ)))
    rows = [dict(r) for r in _conn().execute(sql, params).fetchall()]
    for row in rows:
        row["kept"] = bool(row.pop("keep"))
        if row["detail"]:
            try:
                row["detail"] = json.loads(row["detail"])
            except ValueError:
                pass
    return list(reversed(rows))
