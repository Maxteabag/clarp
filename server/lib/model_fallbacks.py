"""Provider retry for background Janitor and orchestrator model calls.

Conversation agents never switch provider: an agent that hits its usage limit
keeps its own session and waits for the reset (see ``turn_dispatch``). This
module only serves one-shot structured calls -- Janitor decisions, tool
explanations, orchestrator phases -- that carry no conversation. Their chain
comes from the global Janitor model policy (``janitor_design_policy``).

Only an **AI/provider failure** hands such a call to the next model in the
chain: the account is out of quota, the connection dropped, the API returned
back-pressure, the CLI died, the request timed out, or the model produced no
usable answer. A failed invocation may try each configured model once;
cancellation, user interrupts and permission failures never grant a retry.
"""

from __future__ import annotations
import json
import os
import re
from contextlib import contextmanager

from . import lifecycle as lk
from .lifecycle import Edge, Lifecycle, WaitForever
from .persistence.schema_parts.model_fallbacks import SCHEMA

# One fallback invocation's receipt (docs/architecture/lifecycles.md). This
# module is the only writer of model_fallback_attempts.status. claim() inserts
# the row as running; the primary key is (agent_id, request_id, attempt), so the
# kit's single key column is SQLite's rowid.
_FINISHED = ("completed", "failed", "cancelled")
FALLBACK_ATTEMPT = Lifecycle(
    name="model_fallback_attempt", owner="lib/model_fallbacks.py",
    table="model_fallback_attempts", key="rowid", state_column="status",
    entered_columns={status: "finished_at" for status in _FINISHED},
    initial="running", final=frozenset(_FINISHED),
    edges=tuple(Edge(f"finish_{status}", frozenset({"running"}), status) for status in _FINISHED),
    waits={"running": WaitForever(
        "the invoking call finishes it; a row left by a dead process stays as the"
        " receipt that refuses a second attempt")},
)


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


def get(agent_id):
    """The retry chain for one agent's background calls.

    Only Janitors that inherit the global model policy have one; every other
    agent gets an empty chain and stays on its primary model.
    """
    from . import agents, janitor_design_policy

    agent = agents.get_by_agent_id(agent_id)
    if agent and agent.get("is_janitor"):
        effective = janitor_design_policy.effective_chain(agent["session"])
        if effective["source"] == "global":
            chain = effective["chain"]
            return {"models": [{"backend": v["provider"], "model": v["model"], "effort": ""} for v in chain[1:]],
                    "revision": -(effective["revision"] + 1), "source": "global"}
    return {"models": [], "revision": 0}


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
        if getattr(cause, "denied", False):
            return False
        cause = (
            cause.category
            if isinstance(cause, ProviderFailure)
            else error_classify.classify_error(message)
        )
    return bool(cause in PROVIDER_FAILURES and not _DENIED_RE.search(message))


# A CLI's own words on failure are what an operator debugs with ("rate limit reached", "unknown
# model ..."), but a CLI that repeats its input puts the request into them (issue 360, R85). They
# are kept only when they hold none of it: no run of ECHO_RUN characters in common with the prompt.
ECHO_RUN = 24
KEPT_MOST = 300
_SPACE = re.compile(r"[\s\x00-\x1f\x7f]+")
_ESCAPE = re.compile(r"\\(u[0-9a-fA-F]{4}|[\"\\/bfnrt'])")
_ESCAPED = {"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}


def _plain(text):
    """One line: control characters and runs of white space become one space."""
    return _SPACE.sub(" ", text).strip()


def _unescaped(text):
    """``text`` with JSON (and Python repr) string escapes undone, as a tool that prints its
    input as a JSON or repr string writes it."""
    def one(match):
        code = match.group(1)
        if code[0] == "u":
            return chr(int(code[1:], 16))
        return _ESCAPED.get(code, code)
    return _ESCAPE.sub(one, text)


def echoes(text, prompt):
    """Whether ``text`` shares a run of ECHO_RUN characters (the whole prompt when it is shorter)
    with ``prompt``, read as written or with its string escapes undone; white space compared as
    one space. Covered: JSON and Python string escapes, newlines and tabs written as spaces. Not
    covered: base64, percent or HTML encoding, a run broken by an inserted ellipsis."""
    request = _plain(prompt or "")
    if not request:
        return False
    run = min(ECHO_RUN, len(request))
    for form in {_plain(text), _plain(_unescaped(text))}:
        if any(form[at:at + run] in request for at in range(0, len(form) - run + 1)):
            return True
    return False


def cli_failure(backend, output, *, prompt, returncode=None):
    """What a routing CLI's failure says, wherever it is caught: its own words as one line of at
    most KEPT_MOST characters when they hold none of the request, else "<backend> exited with
    status N (<category>)". The category is always kept: the fallback chain and the attempt rows
    read it, never the words."""
    from . import error_classify

    words = (output or "").strip()
    category = error_classify.classify_error(words)
    if category != error_classify.INTERRUPTED and category not in PROVIDER_FAILURES:
        category = error_classify.RUNNER_EXIT
    status = f"exited with status {returncode}" if returncode is not None else "failed"
    line = _plain(words)[:KEPT_MOST]
    if not line or echoes(line, prompt):
        line = f"{backend} {status} ({category})"
    if category == error_classify.INTERRUPTED:
        return Cancelled(line)
    failure = ProviderFailure(line, category)
    # A failure's facts are attributes, read by the code; its words are for a person (R85, ruled):
    # whatever text is kept, a refusal stays a refusal and the exit stays known.
    failure.denied = bool(_DENIED_RE.search(words))
    failure.backend = backend
    failure.returncode = returncode
    return failure


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
    if getattr(error, "denied", False) or _DENIED_RE.search(str(error)):
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
        lk.compare_and_set_where(
            c, FALLBACK_ATTEMPT, f"finish_{status}",
            where="agent_id=? AND request_id=? AND attempt=?",
            params=(agent_id, request_id, attempt), now=db.now_ms(),
            values={"result_json": json.dumps(result) if result is not None else None})


def attempts(agent_id, request_id):
    from . import db

    return [
        dict(r)
        for r in db.conn().execute(
            "SELECT * FROM model_fallback_attempts WHERE agent_id=? AND request_id=? ORDER BY attempt",
            (agent_id, request_id),
        )
    ]


def content_free(error, backend):
    """A failure whose words could hold a command line (TimeoutExpired, CalledProcessError: for
    a CLI that takes its prompt as an argument, the prompt) as words that do not (issue 360,
    R82). Any other error is returned as it is. The original's type name stays on the failure
    for the attempt rows."""
    import subprocess
    if isinstance(error, subprocess.TimeoutExpired):
        failure = ProviderFailure(f"{backend} timed out after {float(error.timeout):g} s", "timeout")
    elif isinstance(error, subprocess.CalledProcessError):
        failure = ProviderFailure(f"{backend} exited with status {error.returncode}")
    else:
        return error
    failure.origin_type = type(error).__name__
    return failure


def _without_command_line(invoke, model):
    """``invoke(model)``, a command line never in what it raises: not in its words, nor in a
    traceback's chained exception (raised from None)."""
    import subprocess
    try:
        return invoke(model)
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as error:
        raise content_free(error, str(model.get("backend") or "the model")) from None


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
        return _without_command_line(invoke, primary)
    except Exception as error:
        if _as_provider_failure(error) is None:
            raise
        failure = error
    for index, model in enumerate(snapshot["models"]):
        if model == primary:
            continue
        if not current() or get(agent_id)["revision"] != snapshot["revision"]:
            raise Cancelled("fallback configuration changed")
        if not claim(agent_id, request_id, index, model,
                     getattr(failure, "origin_type", type(failure).__name__)):
            raise AlreadyAttempted(
                "fallback invocation already claimed; inspect its receipt"
            )
        try:
            result = _without_command_line(invoke, model)
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
    import os
    import shutil
    import signal
    import subprocess
    import tempfile
    import time
    from . import backends

    runner = backends.get(model["backend"])
    if runner is None or not runner.supports_routing:
        raise ValueError("fallback provider cannot answer an isolated request")
    prompt += (
        "\nReturn only the assistant answer as one JSON object matching this exact schema: "
        + json.dumps(schema)
    )
    cmd = runner.routing_cmd(
        prompt, model=model["model"], effort=model.get("effort", "")
    )
    if shutil.which(cmd[0]) is None:
        raise ProviderFailure(f"{cmd[0]} is not on PATH")
    from .routing_input import prepared
    with tempfile.TemporaryDirectory(prefix="clarp-model-fallback-") as root, \
            prepared(runner.id, cmd, prompt) as call:
        if not current():
            raise Cancelled("request is no longer current")
        process = subprocess.Popen(
            call.argv,
            cwd=root,
            stdin=subprocess.PIPE if call.input is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            env={**os.environ, **runner.routing_env()},
        )
        deadline = time.monotonic() + timeout
        payload = call.input
        try:
            while True:
                if not current():
                    raise Cancelled("request is no longer current")
                if time.monotonic() >= deadline:
                    raise ProviderFailure("fallback model timed out", "timeout")
                try:
                    out, err = process.communicate(
                        payload, timeout=min(0.25, max(0.01, deadline - time.monotonic()))
                    )
                    # Bytes in (routing_input encodes the prompt as argv would), text out as before.
                    stdout, stderr = (out or b"").decode(), (err or b"").decode()
                    break
                except subprocess.TimeoutExpired:
                    payload = None  # communicate keeps its first input across retries
            if process.returncode:
                raise cli_failure(runner.id, stderr or stdout, prompt=prompt,
                                  returncode=process.returncode)
            return schema_object(runner.routing_text(stdout or ""), schema)
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            process.communicate()


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
