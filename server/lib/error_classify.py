"""Classify how a turn ended, so the dispatcher knows whether to silently
retry, notify the user, or treat it as a clean finish.

A turn can end three interesting ways beyond "clean":

  * CONNECTION — the network pipe between the CLI and the API dropped
    mid-stream (socket closed, ECONNRESET, fetch failed, …). The model
    did not choose to stop; the link broke. These are worth a few silent
    auto-retries: a wifi/VPN blip usually clears in a second or two.

  * TRANSIENT — the API answered, but with a back-pressure / server error
    (overloaded, rate limited, 5xx). Retrying instantly tends to make it
    worse, so we surface these to the user rather than hammering.

  * INTERRUPTED — the turn was deliberately aborted (SIGTERM from /stop,
    Codex `turn_aborted`, user Esc). Never retry; the user is in control.

  * USAGE_LIMIT — the backend account is out of quota / credits / usage.
    Never retry silently; the user needs to know the agent cannot continue.

  * RUNNER_EXIT — the CLI process exited non-zero without a recognisable
    backend error. This is also user-visible because otherwise the agent can
    appear to stop and fall back to "Connected" with no explanation.

  * TIMEOUT — the backend itself reported a timeout in its error output.
    (There is no server-side per-turn watchdog any more — a hung turn is
    ended by the next preempting send — so this only matches backend text.)
    Never retry silently (it would likely wedge again), surface it to the user.

Anything else is CLEAN / UNKNOWN and flows through the normal path.

Both an error string (from `on_error`) and a result event (from
`on_result`, which can carry `is_error` / an error subtype) are accepted —
the dispatcher checks both surfaces because Claude Code reports an API
failure as an error-result on some paths and a non-zero exit on others.
"""
from __future__ import annotations

import re
from typing import Any

CLEAN = "clean"
CONNECTION = "connection"
TRANSIENT = "transient"
INTERRUPTED = "interrupted"
USAGE_LIMIT = "usage_limit"
RUNNER_EXIT = "runner_exit"
TIMEOUT = "timeout"
UNKNOWN = "unknown"

# Categories that should flip the agent to the INTERRUPTED badge once we
# stop trying (connection errors only reach here after retries are spent).
NOTIFY = frozenset({CONNECTION, TRANSIENT, INTERRUPTED, USAGE_LIMIT, RUNNER_EXIT,
                    TIMEOUT})

# Order matters: INTERRUPTED is checked before CONNECTION because a SIGTERM'd
# turn often *also* prints a broken-pipe message as it dies, and we must not
# auto-retry something the user deliberately stopped.
_TIMEOUT_RE = re.compile(
    r"turn timed out|watchdog|idle timeout|no output for",
    re.I,
)
_INTERRUPTED_RE = re.compile(
    r"turn[_ ]aborted|deliberately interrupted|user interrupted|"
    r"sigterm|sigint|killed by signal|aborted by user|interrupted the previous",
    re.I,
)
_CONNECTION_RE = re.compile(
    r"socket connection was closed|socket hang ?up|connection closed|"
    r"econnreset|epipe|etimedout|enetunreach|econnrefused|"
    r"fetch failed|network (error|timeout)|stream (disconnected|interrupted)|"
    r"connection error|connection reset|premature close|terminated unexpectedly",
    re.I,
)
_TRANSIENT_RE = re.compile(
    r"overloaded|rate[ _]?limit|too many requests|\b429\b|"
    r"\b5\d{2}\b|internal server error|service unavailable|bad gateway|"
    r"gateway time-?out|api (error|timeout).*(retry|temporar)|temporarily unavailable",
    re.I,
)
_USAGE_LIMIT_RE = re.compile(
    r"out of usage|usage (limit|cap|quota|exhausted|exceeded|reached)|"
    r"session limit|hit your .*limit.*resets|"
    r"out of credits?|workspace is out of credits?|"
    r"(quota|credits?|credit balance) (exceeded|exhausted|depleted|reached|used up)|"
    r"insufficient (quota|credits?|credit balance)|"
    r"exceeded your current quota|monthly limit|billing hard limit|"
    r"resource[_ ]exhausted|no credits? remaining|trial quota",
    re.I,
)
_RUNNER_EXIT_RE = re.compile(
    r"\b(codex|clarp|agy|claude|agent)\s+exited\s+rc=\d+|"
    r"\b(exit status|exited with code)\s+\d+|"
    r"\bprocess exited\b.*\b(rc|code)=?\s*\d+",
    re.I,
)


def classify_error(message: str | None) -> str:
    """Classify a free-text error string from a runner's `on_error`."""
    text = (message or "").strip()
    if not text:
        return UNKNOWN
    # Checked before INTERRUPTED: a watchdog kill is a SIGTERM, so its death
    # message can also match the interrupt patterns — but it's a timeout, not
    # a user-initiated stop, and should be reported as such.
    if _TIMEOUT_RE.search(text):
        return TIMEOUT
    if _INTERRUPTED_RE.search(text):
        return INTERRUPTED
    if _USAGE_LIMIT_RE.search(text):
        return USAGE_LIMIT
    if _CONNECTION_RE.search(text):
        return CONNECTION
    if _TRANSIENT_RE.search(text):
        return TRANSIENT
    if _RUNNER_EXIT_RE.search(text):
        return RUNNER_EXIT
    return UNKNOWN


def classify_result(event: Any) -> str:
    """Classify a stream-json `result` event.

    A clean turn returns CLEAN. An error-result (Claude sets `is_error` or a
    `subtype` like ``error_during_execution``) is classified from whatever
    error text the event carries, falling back to TRANSIENT for an
    error-result with no recognisable message (better to notify than to hang
    on a silent failure)."""
    if not isinstance(event, dict):
        return CLEAN
    subtype = str(event.get("subtype") or "")
    is_error = bool(event.get("is_error")) or "error" in subtype.lower()
    if not is_error:
        return CLEAN
    text = " ".join(
        str(event.get(k) or "")
        for k in ("result", "error", "message", "subtype")
    )
    cat = classify_error(text)
    return cat if cat != UNKNOWN else TRANSIENT
