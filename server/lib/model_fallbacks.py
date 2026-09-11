"""Per-agent recovery models and once-only invocation receipts.

Primary settings remain unchanged. Only an **AI/provider failure** hands work to
a fallback model: the account is out of quota, the connection dropped, the API
returned back-pressure, the CLI died, the request timed out, or the model
produced no usable answer. A tool that returns an error -- a failing test, a
non-zero shell command, a rejected patch -- is the agent's own work and stays
with its primary model, so ordinary debugging never switches providers.

A failed invocation may try each configured fallback once; cancellation,
user interrupts and permission failures never grant a retry.
"""

from __future__ import annotations
import json
import re
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_model_fallbacks (
 agent_id TEXT PRIMARY KEY REFERENCES agents(agent_id),
 models_json TEXT NOT NULL DEFAULT '[]', revision INTEGER NOT NULL DEFAULT 1,
 updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS model_fallback_attempts (
 agent_id TEXT NOT NULL REFERENCES agents(agent_id), request_id TEXT NOT NULL,
 attempt INTEGER NOT NULL, backend TEXT NOT NULL, model TEXT NOT NULL, effort TEXT NOT NULL,
 status TEXT NOT NULL, reason TEXT NOT NULL, result_json TEXT,
 started_at INTEGER NOT NULL, finished_at INTEGER, runtime_id INTEGER,
 PRIMARY KEY(agent_id,request_id,attempt)
);
"""


class Conflict(ValueError):
    pass


class Cancelled(RuntimeError):
    pass


class AlreadyAttempted(RuntimeError):
    pass


class ProviderFailure(RuntimeError):
    """The AI/provider itself failed: no answer, or an answer we cannot use.

    Raised where the caller knows the model is at fault and the free-text
    message would not classify on its own (an empty reply, a schema-invalid
    answer, a runner that exited without a result).
    """

    def __init__(self, message, category=""):
        super().__init__(message)
        self.category = category or "runner_exit"


@contextmanager
def _write():
    from . import db

    c = db.conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        yield c
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise


def validate(models):
    from . import backends

    if not isinstance(models, list) or len(models) > 4:
        raise ValueError("fallback models must be an array of at most four models")
    result = []
    for item in models:
        if not isinstance(item, dict) or set(item) - {"backend", "model", "effort"}:
            raise ValueError("invalid fallback model")
        backend, model, effort = (
            item.get(k, "") for k in ("backend", "model", "effort")
        )
        if not all(isinstance(x, str) for x in (backend, model, effort)):
            raise ValueError("fallback fields must be strings")
        backend, model, effort = backend.strip(), model.strip(), effort.strip()
        if (
            not backends.get(backend)
            or not model
            or not backends.is_valid_model(backend, model)
        ):
            raise ValueError("unavailable fallback model")
        if effort and effort not in backends.valid_efforts(backend):
            raise ValueError("invalid fallback effort")
        if backend == "agy" and effort:
            # Antigravity encodes Low in the model ID; adding --effort is not
            # a supported model-specific combination in the current adapter.
            raise ValueError("Antigravity effort is included in the model choice")
        value = dict(backend=backend, model=model, effort=effort)
        if value in result:
            raise ValueError("duplicate fallback model")
        result.append(value)
    return result


def supported_backends(agent_id):
    """Providers that can serve as a fallback for this agent.

    Every routing-capable adapter qualifies, including for janitors: their
    structured work goes through :func:`json_call`, which drives each backend
    through the same one-shot ``routing_cmd``/``routing_text`` interface.
    """
    from . import backends

    return [adapter.id for adapter in backends.routing_adapters()]


def get(agent_id):
    from . import db

    row = (
        db.conn()
        .execute("SELECT * FROM agent_model_fallbacks WHERE agent_id=?", (agent_id,))
        .fetchone()
    )
    return {
        "models": json.loads(row["models_json"]) if row else [],
        "revision": row["revision"] if row else 0,
        "supported_backends": supported_backends(agent_id),
    }


def configure(agent_id, models, *, expected_revision):
    from . import db

    models = validate(models)
    if any(model["backend"] not in supported_backends(agent_id) for model in models):
        raise ValueError("fallback provider is not supported for this agent's job")
    if not isinstance(expected_revision, int) or isinstance(expected_revision, bool):
        raise ValueError("expected_revision required")
    with _write() as c:
        if not c.execute(
            "SELECT 1 FROM agents WHERE agent_id=? AND deleted_at IS NULL", (agent_id,)
        ).fetchone():
            raise ValueError("agent not found")
        prior = get(agent_id)
        if prior["revision"] != expected_revision:
            raise Conflict("fallback settings changed; reload")
        c.execute(
            """INSERT INTO agent_model_fallbacks VALUES (?,?,?,?) ON CONFLICT(agent_id)
            DO UPDATE SET models_json=excluded.models_json,revision=excluded.revision,updated_at=excluded.updated_at""",
            (agent_id, json.dumps(models), expected_revision + 1, db.now_ms()),
        )
    return get(agent_id)


# The only failure categories that mean "the AI/provider failed". Everything
# else -- a clean turn whose tools reported errors, a deliberate stop, an
# unclassifiable message -- keeps the agent on its primary model.
PROVIDER_FAILURES = frozenset(
    {"connection", "transient", "usage_limit", "runner_exit", "timeout"}
)

# A refusal is an answer, not an outage: retrying it on another provider would
# launder a decision the user or the sandbox already made.
_DENIED_RE = re.compile(
    r"permission denied|approval required|not authorized|ownership lost|"
    r"configuration changed",
    re.I,
)


def is_provider_failure(cause, message=""):
    """True only when the AI/provider itself failed.

    ``cause`` is either a category already named by :mod:`error_classify` (the
    dispatcher classified the turn) or the exception raised around a model
    call. ``message`` supplies the failure text when ``cause`` is a category.
    """
    from . import error_classify

    if isinstance(cause, BaseException):
        message = message or str(cause)
        if isinstance(cause, (Cancelled, AlreadyAttempted, KeyboardInterrupt)):
            return False
        cause = (
            cause.category
            if isinstance(cause, ProviderFailure)
            else error_classify.classify_error(message)
        )
    return bool(cause in PROVIDER_FAILURES and not _DENIED_RE.search(message))


def provider_error(message):
    """Categorise a runner-level failure into the exception it deserves.

    A runner that stopped because the user pressed stop is never a provider
    failure, however the CLI phrased its exit.
    """
    from . import error_classify

    category = error_classify.classify_error(message)
    if category == error_classify.INTERRUPTED:
        return Cancelled(message)
    return ProviderFailure(
        message,
        category if category in PROVIDER_FAILURES else error_classify.RUNNER_EXIT,
    )


def _as_provider_failure(error):
    """Promote an error raised around a pure model call, or refuse it.

    :func:`execute` invokes nothing but a model, so an error it could not name
    is still the provider's -- a quota message phrased in a way no pattern
    matches must not strand the agent. Interrupts, permission refusals and
    Clarp's own control exceptions are never provider failures, whatever the
    wording. Returns ``None`` when the error must propagate untouched.
    """
    if isinstance(error, (Cancelled, AlreadyAttempted, KeyboardInterrupt)):
        return None
    if _DENIED_RE.search(str(error)):
        return None
    promoted = provider_error(str(error))
    return None if isinstance(promoted, Cancelled) else promoted


def claim(agent_id, request_id, attempt, model, reason):
    from . import db, agents

    with _write() as c:
        return (
            c.execute(
                """INSERT OR IGNORE INTO model_fallback_attempts
            (agent_id,request_id,attempt,backend,model,effort,status,reason,started_at,runtime_id)
            VALUES (?,?,?,?,?,?,'running',?,?,?)""",
                (
                    agent_id,
                    request_id,
                    attempt,
                    model["backend"],
                    model["model"],
                    model.get("effort", ""),
                    reason[:300],
                    db.now_ms(),
                    agents.current_runtime_id(agent_id),
                ),
            ).rowcount
            == 1
        )


def finish(agent_id, request_id, attempt, *, status, result=None):
    from . import db

    with _write() as c:
        c.execute(
            """UPDATE model_fallback_attempts SET status=?,result_json=?,finished_at=?
            WHERE agent_id=? AND request_id=? AND attempt=? AND status='running'""",
            (
                status,
                json.dumps(result) if result is not None else None,
                db.now_ms(),
                agent_id,
                request_id,
                attempt,
            ),
        )


def attempts(agent_id, request_id):
    from . import db

    return [
        dict(r)
        for r in db.conn().execute(
            "SELECT * FROM model_fallback_attempts WHERE agent_id=? AND request_id=? ORDER BY attempt",
            (agent_id, request_id),
        )
    ]


def execute(agent_id, request_id, primary, invoke, *, current=lambda: True):
    """Run one model call, retrying it once per configured fallback provider.

    ``invoke`` must be a *pure model call* with no external side effect: it may
    run several times, so apply its effect only after this returns. Because it
    calls nothing but a model, any failure it raises other than a cancellation
    or a permission refusal counts as an AI/provider failure.
    """
    snapshot = get(agent_id)
    if not current():
        raise Cancelled("request is no longer current")
    try:
        return invoke(primary)
    except Exception as error:
        if _as_provider_failure(error) is None:
            raise
        failure = error
    for index, model in enumerate(snapshot["models"]):
        if model == primary:
            continue
        if not current() or get(agent_id)["revision"] != snapshot["revision"]:
            raise Cancelled("fallback configuration changed")
        if not claim(agent_id, request_id, index, model, type(failure).__name__):
            raise AlreadyAttempted(
                "fallback invocation already claimed; inspect its receipt"
            )
        try:
            result = invoke(model)
            if not current() or get(agent_id)["revision"] != snapshot["revision"]:
                raise Cancelled("request is no longer current")
            finish(agent_id, request_id, index, status="completed")
            return result
        except Exception as error:
            promoted = _as_provider_failure(error)
            finish(
                agent_id,
                request_id,
                index,
                status="failed" if promoted is not None else "cancelled",
            )
            if promoted is None:
                raise
            failure = error
    raise failure


def json_call(model, prompt, schema, *, current=lambda: True, timeout=45):
    """One bounded structured request to any routing-capable provider.

    Every backend adapter exposes ``routing_cmd``/``routing_text`` for a single
    isolated, non-persisted request, so a fallback works on Claude, Codex,
    Antigravity, Grok and OpenCode alike without per-CLI branching here. The
    run gets its own throwaway workspace, its own process group, and a hard
    deadline; nothing it does can touch the caller's conversation.
    """
    import importlib
    import os
    import shutil
    import signal
    import subprocess
    import tempfile
    import time
    from . import backends

    adapter = backends.get(model["backend"])
    if adapter is None or not adapter.supports_routing:
        raise ValueError("fallback provider cannot answer an isolated request")
    runner = importlib.import_module(f"lib.{adapter.routing_module}")
    prompt += (
        "\nReturn only the assistant answer as one JSON object matching this exact schema: "
        + json.dumps(schema)
    )
    cmd = runner.routing_cmd(
        prompt, model=model["model"], effort=model.get("effort", "")
    )
    if shutil.which(cmd[0]) is None:
        raise ProviderFailure(f"{cmd[0]} is not on PATH")
    with tempfile.TemporaryDirectory(prefix="clarp-model-fallback-") as root:
        if not current():
            raise Cancelled("request is no longer current")
        process = subprocess.Popen(
            cmd,
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        deadline = time.monotonic() + timeout
        try:
            while True:
                if not current():
                    raise Cancelled("request is no longer current")
                if time.monotonic() >= deadline:
                    raise ProviderFailure("fallback model timed out", "timeout")
                try:
                    stdout, stderr = process.communicate(
                        timeout=min(0.25, max(0.01, deadline - time.monotonic()))
                    )
                    break
                except subprocess.TimeoutExpired:
                    pass
            if process.returncode:
                raise provider_error(
                    (stderr or stdout or "").strip()[:1000]
                    or f"{cmd[0]} exited rc={process.returncode}"
                )
            return schema_object(runner.routing_text(stdout or ""), schema)
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            process.communicate()


def conversation_rows(agent_id):
    from . import db, agents
    from datetime import datetime, timezone

    runtime = agents.current_runtime_id(agent_id)
    if runtime is None:
        return []
    result = []
    for row in db.conn().execute(
        "SELECT * FROM model_fallback_attempts WHERE agent_id=? AND runtime_id=? AND status='completed' AND result_json IS NOT NULL ORDER BY started_at DESC LIMIT 20",
        (agent_id, runtime),
    ):
        text = json.loads(row["result_json"]).get("text", "")
        if text:
            result.append(
                {
                    "id": f"fallback-{row['request_id']}-{row['attempt']}",
                    "role": "assistant",
                    "text": text,
                    "timestamp": datetime.fromtimestamp(
                        row["finished_at"] / 1000, timezone.utc
                    ).isoformat(),
                    "trace_id": row["request_id"],
                    "origin": "automation",
                    "fallback_model": row["model"],
                    "revision": 0,
                }
            )
    return list(reversed(result))


def continuation_context(agent_id):
    """Hand the next turn the fallback answer that already did the work.

    Delimited so the transcript importer can strip it: appended bare, it was
    stored as part of the user's own message and the chat showed every turn
    twice. Offered only until the agent's own model answers again, because a
    fallback result stops being "work already completed" the moment the real
    model has spoken — before this check it was re-appended to every prompt for
    the life of the runtime.
    """
    from . import message_store

    rows = conversation_rows(agent_id)
    if not rows or _superseded_by_own_reply(agent_id, rows[-1]):
        return ""
    return (
        "\n" + message_store.FALLBACK_CONTEXT_OPEN + "\n"
        "Clarp fallback work already completed in this conversation."
        " Use its result; do not repeat completed actions:\n"
        + rows[-1]["text"][-12000:]
        + "\n" + message_store.FALLBACK_CONTEXT_CLOSE + "\n"
    )


def _superseded_by_own_reply(agent_id, row):
    """True once the agent's own assistant turn is newer than the fallback.

    The fallback's own delivered answer is itself stored as an assistant
    message, so it is excluded by trace: counting it would suppress the
    context on the very next turn and strand the work it just did.
    """
    from . import db

    finished = db.conn().execute(
        """SELECT MAX(COALESCE(
               CAST((julianday(timestamp) - 2440587.5) * 86400000 AS INTEGER),
               updated_at))
             FROM messages
            WHERE agent_id = ? AND role = 'assistant'
              AND COALESCE(text, '') != ''
              AND COALESCE(origin, 'user') != 'automation'
              AND COALESCE(trace_id, '') != ?""",
        (agent_id, row.get("trace_id") or ""),
    ).fetchone()[0]
    if not finished:
        return False
    try:
        from datetime import datetime
        completed = datetime.fromisoformat(row["timestamp"]).timestamp() * 1000
    except (TypeError, ValueError):
        return False
    return int(finished) > int(completed)


def schema_object(text, schema):
    """The first schema-valid JSON object in a provider's reply text.

    Providers wrap the answer differently: Antigravity can append a final
    ``structured_output`` housekeeping tool that describes *finishing* rather
    than the answer, and most CLIs fence JSON in markdown. Scanning for the
    first object that actually validates -- while skipping the tool envelope --
    keeps one extractor correct for every backend.
    """
    import jsonschema

    if not isinstance(text, str) or len(text) > 512000:
        raise ProviderFailure("invalid fallback answer")
    decoder = json.JSONDecoder()
    for start in list(re.finditer(r"\{", text))[:64]:
        try:
            value, _ = decoder.raw_decode(text[start.start() :])
            if (
                not isinstance(value, dict)
                or "toolAction" in value
                or "toolSummary" in value
            ):
                continue
            jsonschema.validate(value, schema)
            return value
        except (ValueError, jsonschema.ValidationError):
            continue
    raise ProviderFailure("fallback did not return a schema-valid assistant answer")
