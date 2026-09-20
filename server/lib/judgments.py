"""Optional typed judgments from a System One model (TypeSafe Jev).

Clarp makes many small decisions about language: was that real speech, who is
the user talking to, why did a turn die. Each of those has deterministic code
today — a regex, a blocklist, an ordered set of patterns. This module offers a
second opinion from a model that returns a typed answer with calibrated
probabilities instead of prose.

The contract is deliberately one-sided: `judge()` returns None whenever the
provider cannot answer — no API key, the site is switched off, a timeout, an
HTTP error, or the breaker is open — and the caller keeps its existing code as
the fallback. Nothing is deleted, so turning this off restores today's behaviour
exactly.

Wiring a new site takes three things: a name in `SITES`, a question built with
the helpers below, and a call site shaped like

    answer = judgments.judge("junk", state, questions)
    if answer is None:
        return todays_code(...)      # unchanged
    return answer.noul("junk") >= 0.5
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any

from . import config, settings_store
from .db import conn, now_ms
from .log import log, log_exception

API_URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"

# Per-site switches. Each name is a settings key suffix and a log column value.
SITES = ("errors", "junk", "router", "naming", "watch")

KEY_ENABLED = "judgments.enabled"
KEY_TIMEOUT_MS = "judgments.timeout_ms"
DEFAULT_TIMEOUT_MS = 900          # live voice sites; background sites pass more
MAX_TIMEOUT_MS = 30000

# The breaker keeps a dead or slow provider from costing every request its full
# timeout. Failures are counted across sites because they share one endpoint.
FAILURES_BEFORE_OPEN = 3
OPEN_SECONDS = 120.0
# HTTP 402 means the account is out of credit; probing it every two minutes
# only produces a failure log line each time. Hold for an hour instead.
BILLING_OPEN_SECONDS = 3600.0

_BREAKER_LOCK = threading.Lock()
_consecutive_failures = 0
_open_until = 0.0

# A fresh TLS handshake per judgment measured 620-700 ms against the same
# endpoint that answers in ~250 ms on a warm connection — which is most of the
# latency budget for a live voice site. Hold the connection open, the way
# apns.py does, and rebuild it if httpx.Client itself is swapped out.
_CLIENT_LOCK = threading.Lock()
_CLIENT = None
_CLIENT_CTOR = None


SCHEMA = """
CREATE TABLE IF NOT EXISTS judgment_decisions (
    judgment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    site TEXT NOT NULL,
    trace_id TEXT,
    question_ids TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL DEFAULT '',
    answers_json TEXT NOT NULL DEFAULT '{}',
    latency_ms INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    fallback_used INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_judgment_decisions_site
    ON judgment_decisions(site, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_judgment_decisions_created
    ON judgment_decisions(created_at DESC);
"""


# Columns added after the table shipped. CREATE TABLE IF NOT EXISTS cannot add
# them, so they are ensured here on first use; ALTER TABLE ADD COLUMN is
# idempotent when guarded by the pragma check.
_DIAGNOSTIC_COLUMNS = (
    ("question_json", "TEXT NOT NULL DEFAULT '{}'"),   # the questions as sent
    ("state_json", "TEXT NOT NULL DEFAULT ''"),         # the state that was judged
)
_COLUMNS_LOCK = threading.Lock()
STATE_LOG_LIMIT = 4000
QUESTION_LOG_LIMIT = 2000


def _ensure_columns() -> None:
    """Add the diagnostic columns to a table that predates them.

    Checked per call rather than once per process: the pragma is one cheap
    read, and a process-wide flag goes stale when the database file is swapped
    underneath it (tests do this per case; so does a restore from backup).
    """
    with _COLUMNS_LOCK:
        present = {row[1] for row in conn().execute("PRAGMA table_info(judgment_decisions)")}
        for name, definition in _DIAGNOSTIC_COLUMNS:
            if name not in present:
                conn().execute(f"ALTER TABLE judgment_decisions ADD COLUMN {name} {definition}")


# ---- question builders -------------------------------------------------
# Plain dicts in the wire format, so this module needs no SDK dependency and
# a question can be logged or diffed as ordinary JSON.

def noul(instructions: str, *, yes: str = "", no: str = "") -> dict:
    """A yes/no question. The answer is the probability of yes."""
    question: dict[str, Any] = {"type": "noul", "instructions": instructions}
    if yes or no:
        question["criteria"] = {"true": yes, "false": no}
    return question


def choice(instructions: str, criteria: dict[str, str]) -> dict:
    """Pick one of `criteria`. The answer carries the full distribution."""
    return {"type": "choice", "instructions": instructions, "criteria": criteria}


def score(instructions: str, levels: list[str]) -> dict:
    """Rate along ordered `levels`."""
    return {"type": "score", "instructions": instructions, "criteria": levels}


class Answers:
    """Typed accessors over one response, so call sites never touch raw JSON."""

    def __init__(self, raw: dict[str, Any], usage: dict[str, Any]) -> None:
        self.raw = raw
        self.usage = usage

    def noul(self, question_id: str) -> float:
        return float((self.raw.get(question_id) or {}).get("noul") or 0.0)

    def choice(self, question_id: str) -> str:
        return str((self.raw.get(question_id) or {}).get("choice") or "")

    def score(self, question_id: str) -> float:
        return float((self.raw.get(question_id) or {}).get("score") or 0.0)

    def confidence(self, question_id: str) -> float:
        return float((self.raw.get(question_id) or {}).get("confidence") or 0.0)

    def probabilities(self, question_id: str) -> dict[str, float]:
        values = (self.raw.get(question_id) or {}).get("probabilities") or {}
        return {str(k): float(v) for k, v in values.items()}

    def probability(self, question_id: str, option: str) -> float:
        return self.probabilities(question_id).get(option, 0.0)

    def top(self, question_id: str, count: int = 3) -> str:
        """Readable distribution for logs: 'agent_message 87%, ignored 9%'."""
        ranked = sorted(self.probabilities(question_id).items(), key=lambda kv: -kv[1])
        return ", ".join(f"{k} {v:.0%}" for k, v in ranked[:count])


# ---- availability ------------------------------------------------------

def api_key() -> str:
    return config.load().typesafe_key()


def available() -> bool:
    """True when a key is configured and the master switch is on."""
    return bool(api_key()) and settings_store.get_bool(KEY_ENABLED, default=False)


def site_enabled(site: str) -> bool:
    """True when this particular decision may ask the model."""
    if site not in SITES:
        raise ValueError(f"unknown judgment site: {site}")
    return available() and settings_store.get_bool(f"judgments.{site}", default=False)


def status() -> dict[str, Any]:
    """Shape mirrors tts_providers.status(): what is configured, what is on."""
    with _BREAKER_LOCK:
        open_for = max(0.0, _open_until - time.monotonic())
    return {
        "credential": "TYPESAFE_API_KEY",
        "credential_present": bool(api_key()),
        "enabled": settings_store.get_bool(KEY_ENABLED, default=False),
        "timeout_ms": timeout_ms(),
        "breaker_open_seconds": round(open_for, 1),
        "sites": {s: settings_store.get_bool(f"judgments.{s}", default=False)
                  for s in SITES},
    }


def timeout_ms() -> int:
    return settings_store.get_int(
        KEY_TIMEOUT_MS, default=DEFAULT_TIMEOUT_MS, minimum=100, maximum=MAX_TIMEOUT_MS)


# ---- breaker -----------------------------------------------------------

def _breaker_open() -> bool:
    with _BREAKER_LOCK:
        return time.monotonic() < _open_until


def _record_success() -> None:
    global _consecutive_failures, _open_until
    with _BREAKER_LOCK:
        _consecutive_failures = 0
        _open_until = 0.0


def _record_failure(*, hold_seconds: float | None = None) -> None:
    """Count a failure; `hold_seconds` opens the breaker at once for that long."""
    global _consecutive_failures, _open_until
    with _BREAKER_LOCK:
        _consecutive_failures += 1
        if hold_seconds is not None:
            _open_until = max(_open_until, time.monotonic() + hold_seconds)
            log("judgmentBreakerOpen", f"billing failure; pausing {hold_seconds:.0f}s")
        elif _consecutive_failures >= FAILURES_BEFORE_OPEN:
            _open_until = time.monotonic() + OPEN_SECONDS
            log("judgmentBreakerOpen",
                f"{_consecutive_failures} consecutive failures; pausing {OPEN_SECONDS:.0f}s")


def reset_breaker() -> None:
    """Test and settings-change helper; not used on the request path."""
    global _consecutive_failures, _open_until
    with _BREAKER_LOCK:
        _consecutive_failures = 0
        _open_until = 0.0


# ---- transport ---------------------------------------------------------

def _pooled_client(seconds: float):
    global _CLIENT, _CLIENT_CTOR
    import httpx
    with _CLIENT_LOCK:
        if _CLIENT is None or _CLIENT_CTOR is not httpx.Client:
            if _CLIENT is not None:
                try:
                    _CLIENT.close()
                except Exception as e:
                    log_exception("judgmentClientCloseFail", e)
            _CLIENT = httpx.Client(timeout=seconds)
            _CLIENT_CTOR = httpx.Client
        return _CLIENT


def _post(payload: dict, key: str, seconds: float) -> dict:
    """One blocking POST. Separate so tests can replace it without a socket."""
    client = _pooled_client(seconds)
    response = client.post(
        API_URL,
        content=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        timeout=seconds,
    )
    response.raise_for_status()
    return response.json()


def _log_decision(*, site: str, trace_id: str, question_ids: list[str],
                  outcome: str, answers: dict, latency_ms: float,
                  usage: dict, fallback_used: bool, error: str,
                  questions: dict | None = None, state: Any = None) -> None:
    """One row per call, including failed ones.

    `questions` and `state` are what went over the wire, so a decision can be
    read later as: this input, this question, this probability, this outcome.
    Both are bounded; the state is user content and stays on this Host.
    """
    try:
        _ensure_columns()
        conn().execute(
            """INSERT INTO judgment_decisions (
                   site, trace_id, question_ids, outcome, answers_json,
                   latency_ms, input_tokens, output_tokens, fallback_used,
                   error, created_at, question_json, state_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (site, trace_id, ",".join(question_ids), outcome,
             json.dumps(answers, ensure_ascii=False)[:4000], int(latency_ms),
             int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0),
             1 if fallback_used else 0, error[:500], now_ms(),
             json.dumps(questions or {}, ensure_ascii=False)[:QUESTION_LOG_LIMIT],
             json.dumps(state, ensure_ascii=False, default=str)[:STATE_LOG_LIMIT] if state is not None else ""),
        )
    except Exception as e:
        # A log failure must never cost the caller its answer.
        log_exception("judgmentLogFail", e)


def judge(site: str, state: Any, questions: dict[str, dict], *,
          timeout_ms_override: int | None = None, trace_id: str = "",
          outcome: str = "") -> Answers | None:
    """Ask `questions` about `state`, or return None so the caller falls back.

    Independent questions belong in one call: they are answered in parallel for
    one copy of the state, which is both cheaper and faster than asking twice.
    """
    if not questions:
        return None
    if not site_enabled(site):
        return None
    key = api_key()
    if not key:
        return None
    if _breaker_open():
        _log_decision(site=site, trace_id=trace_id, question_ids=list(questions),
                      outcome="", answers={}, latency_ms=0, usage={},
                      fallback_used=True, error="breaker open",
                      questions=questions, state=state)
        return None

    budget_ms = timeout_ms_override if timeout_ms_override is not None else timeout_ms()
    payload = {"state": state, "model": MODEL, "questions": questions}
    started = time.perf_counter()
    try:
        data = _post(payload, key, max(0.1, budget_ms / 1000.0))
    except Exception as e:                      # timeout, HTTP error, bad JSON
        latency = (time.perf_counter() - started) * 1000
        status = getattr(getattr(e, "response", None), "status_code", None)
        _record_failure(hold_seconds=BILLING_OPEN_SECONDS if status == 402 else None)
        detail = str(getattr(getattr(e, "response", None), "text", ""))[:200]
        log_exception(f"judgmentFail:{site}", e)
        _log_decision(site=site, trace_id=trace_id, question_ids=list(questions),
                      outcome="", answers={}, latency_ms=latency, usage={},
                      fallback_used=True, error=f"{e!r} {detail}".strip(),
                      questions=questions, state=state)
        return None

    latency = (time.perf_counter() - started) * 1000
    raw = data.get("answers")
    if not isinstance(raw, dict) or any(q not in raw for q in questions):
        # A malformed answer is a failure like any other: fall back, do not guess.
        _record_failure()
        _log_decision(site=site, trace_id=trace_id, question_ids=list(questions),
                      outcome="", answers={}, latency_ms=latency, usage={},
                      fallback_used=True, error=f"unexpected response: {str(data)[:200]}",
                      questions=questions, state=state)
        return None

    _record_success()
    usage = data.get("usage") or {}
    _log_decision(site=site, trace_id=trace_id, question_ids=list(questions),
                  outcome=outcome, answers=raw, latency_ms=latency, usage=usage,
                  fallback_used=False, error="", questions=questions, state=state)
    return Answers(raw, usage)


def record_outcome(site: str, outcome: str) -> None:
    """Attach the decision a caller actually took to its most recent row."""
    try:
        conn().execute(
            """UPDATE judgment_decisions SET outcome = ?
               WHERE judgment_id = (
                   SELECT judgment_id FROM judgment_decisions
                   WHERE site = ? ORDER BY judgment_id DESC LIMIT 1)""",
            (outcome, site),
        )
    except Exception as e:
        log_exception("judgmentOutcomeFail", e)


def recent(limit: int = 50, site: str = "") -> list[dict]:
    """Read the decision log. This is what replaces a shadow-mode comparison."""
    _ensure_columns()
    sql = "SELECT * FROM judgment_decisions"
    params: list[Any] = []
    if site:
        sql += " WHERE site = ?"
        params.append(site)
    sql += " ORDER BY judgment_id DESC LIMIT ?"
    params.append(max(1, min(500, int(limit))))
    return [dict(row) for row in conn().execute(sql, params)]


def update_settings(data: dict[str, Any]) -> dict[str, Any]:
    """Apply a settings patch from /judgments/settings and return the status."""
    if "enabled" in data:
        if not isinstance(data["enabled"], bool):
            raise ValueError("enabled must be a boolean")
        settings_store.set_bool(KEY_ENABLED, data["enabled"])
    if "timeout_ms" in data:
        value = data["timeout_ms"]
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError("timeout_ms must be an integer")
        if not 100 <= value <= MAX_TIMEOUT_MS:
            raise ValueError(f"timeout_ms must be between 100 and {MAX_TIMEOUT_MS}")
        settings_store.set_int(KEY_TIMEOUT_MS, value)
    sites = data.get("sites")
    if sites is not None:
        if not isinstance(sites, dict):
            raise ValueError("sites must be an object")
        for name, value in sites.items():
            if name not in SITES:
                raise ValueError(f"unknown judgment site: {name}")
            if not isinstance(value, bool):
                raise ValueError(f"sites.{name} must be a boolean")
            settings_store.set_bool(f"judgments.{name}", value)
    reset_breaker()
    return status()
