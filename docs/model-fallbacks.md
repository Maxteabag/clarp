# Per-agent model fallbacks

Every agent can carry an ordered list of up to four fallback models. When the
**AI/provider** fails, Clarp finishes the unfinished request on the next
configured model instead of leaving the agent stopped. The agent keeps its
persona, its native conversation, its voice, and its primary model: a fallback
is a recovery route, never a reassignment.

This is per-agent and provider-agnostic. It complements
[Claude account recovery](claude-account-failover.md), which switches *accounts*
within Claude; fallbacks switch *providers*.

## What triggers a fallback

Only an AI/provider failure. Clarp classifies how a turn ended
(`lib/error_classify.py`) and hands over on exactly these categories:

| Category | Meaning |
|---|---|
| `usage_limit` | the account is out of quota, credits, or usage |
| `connection` | the pipe between CLI and API dropped mid-stream |
| `transient` | the API answered with back-pressure or a 5xx |
| `runner_exit` | the CLI process died without a recognisable error |
| `timeout` | the backend itself reported a timeout |

Everything else keeps the agent on its own model:

- **A tool that returns an error is not a provider failure.** A failing test, a
  non-zero shell command, a rejected patch — the turn was clean and the model is
  working. Switching providers mid-debugging would throw away the context that
  makes the next step obvious, so Clarp does not.
- **A deliberate stop is never a fallback.** `interrupted` (`/stop`, SIGTERM,
  Codex `turn_aborted`, user Esc) leaves the turn stopped. The user is in control.
- **A refusal is an answer, not an outage.** Text matching permission denied,
  approval required, not authorized, ownership lost, or configuration changed
  never falls back; retrying it elsewhere would launder a decision the user or
  the sandbox already made.
- **An unclassifiable failure does not fall back** on the dispatch path. Without
  evidence the provider failed, Clarp reports rather than guesses.

One deliberate asymmetry: `model_fallbacks.execute()` — used for pure model
calls such as the orchestrator's routing decision and the tool explainer's
translation — promotes *any* unnamed error to a provider failure. Its `invoke`
callable does nothing but call a model, so there is no other candidate culprit,
and a quota message phrased in a way no pattern matches must not strand the
agent. Cancellations and permission refusals are still excluded there.

## Which providers can serve as a fallback

All routing-capable backends: **Claude, Codex, Antigravity, Grok, OpenCode**.
Each adapter exposes `routing_cmd()`/`routing_text()` for one isolated,
non-persisted request, so `json_call` drives every backend through the same
argv builder and the same answer extractor with no per-CLI branching. A new CLI
becomes a valid fallback the moment its adapter declares `routing_module`.

Antigravity encodes effort in the model id (`gemini-3.8-flash-low`), so
configuring an explicit `effort` alongside an `agy` model is rejected.

## Two execution paths

**Pure model calls** (`model_fallbacks.execute`) — the orchestrator's routing
decision, the tool explainer's translation. `invoke` may run several times, so
it must have no external side effect; apply the effect only after `execute`
returns. Structured requests go through `json_call`, which runs the provider in
a throwaway workspace, in its own process group, under a hard deadline.

**Whole agent turns** (`turn_model_fallback.run`) — a real request that failed
mid-flight. This one is side-effecting, so it is deliberately careful:

- The primary's handle is terminated and its children reaped *before* another
  model touches the same files, so two models never write concurrently.
- The fallback runs isolated: `is_new_session=True`, no backend session id, and
  its provider UUID is never bound onto the primary conversation. AGY isolated
  runs also get an explicit `--add-dir` for the workspace, because AGY otherwise
  picks its global scratch project for a non-Git cwd.
- The continuation prompt carries the last 20 messages as *context, not
  authority*, and instructs the model to inspect existing results before
  repeating any tool call, so completed external work is not redone.
- Delivery happens outside the retry block. A successful model may already have
  performed external work, so a delivery exception must never rerun it.
- Ownership is rechecked at every step. If the turn is superseded, stopped, or
  the fallback configuration changes mid-flight, the attempt is cancelled.

## Once-only receipts

Each attempt is claimed in `model_fallback_attempts` with
`PRIMARY KEY(agent_id, request_id, attempt)`, so the same fallback can never run
twice for one request — even across a runtime restart. `status` moves from
`running` to `completed`, `failed`, or `cancelled`. A completed receipt carries
the answer text, which `continuation_context()` injects into the agent's next
prompt so it does not repeat work a fallback already finished. Receipts are
scoped to the current runtime: a previous conversation's fallback text never
leaks into a new one.

The configuration itself is revision-guarded. `GET /agent-fallbacks` returns the
current `revision`; `POST /agent-fallbacks` requires it as `expected_revision`
and returns HTTP 409 on a mismatch, so two editors cannot silently overwrite
each other.

## API

```http
GET /agent-fallbacks?session=<slug>
{"session":"mike","models":[...],"revision":3,"supported_backends":["claude","codex","agy","grok","opencode"]}

POST /agent-fallbacks
{"session":"mike","models":[{"backend":"agy","model":"gemini-3.8-flash-low","effort":""}],"expected_revision":3}
```

Models are validated against the live backend catalogue on save; an unavailable
model is rejected rather than stored and discovered broken later.

## Configuring agents

`scripts/configure_model_fallbacks.py` reads every revision before its first
write and skips agents that already match, so it is safe to re-run. It previews
unless given `--apply`:

```bash
scripts/configure_model_fallbacks.py --all --backend agy --model gemini-3.8-flash-low
scripts/configure_model_fallbacks.py --all --backend agy --model gemini-3.8-flash-low --apply
```

## Verifying it works

Unit and integration coverage is in `tests/unit/test_model_fallbacks.py`
(`make py`). Because a mocked provider cannot prove a real one recovers, three
paid probes exercise the live path in a throwaway database:

```bash
scripts/probe_provider_fallbacks.py --output matrix.json   # every provider answers
scripts/probe_model_fallback.py     --output explainer.json # explainer failover
scripts/probe_worker_fallback.py    --output worker.json    # real file-writing turn
```

Grok is skipped by default: its Build account balance is exhausted (HTTP 402),
so a probe there cannot distinguish a code fault from an unpaid account. Pass
`--backend grok` to include it once the account has balance.
