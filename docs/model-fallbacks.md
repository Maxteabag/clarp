# Model retry for background calls

**Conversation agents never switch provider.** When an agent's account runs out
of usage, its turn ends with the provider's own message and the agent keeps its
persona, native session, voice and model until the quota resets. Clarp does not
start a different model behind the agent's back: a switch would carry none of
the native conversation and the agent would answer without its history.

What still retries is *background* work that carries no conversation: Janitor
decisions, tool explanations and orchestrator phases. These are one-shot
structured calls (`lib/model_fallbacks.json_call` / `execute`). When such a
call fails because the **provider** failed, it is retried once on each next
model of the chain, then gives up.

## What counts as a provider failure

Classified by `lib/error_classify.py`: `usage_limit`, `connection`,
`transient`, `runner_exit`, `timeout`. A tool error (a failing test, a
non-zero command) or a refusal (permission denied, ownership lost,
configuration changed) is never retried on another model.

## Where the chain comes from

Only from the global Janitor model policy (`lib/janitor_design_policy.py`,
settings in the Janitor models screen). A Janitor that does not inherit the
policy, and every non-Janitor agent, has an empty chain and stays on its own
model.

## Once-only receipts

Each retry claims a row in `model_fallback_attempts` before it runs, so a
restart or a duplicated request cannot invoke the same fallback twice. Read
them with `model_fallbacks.attempts(agent_id, request_id)`.

## History

Earlier releases let any agent carry a per-agent fallback chain (endpoint
`/agent-fallbacks`, table `agent_model_fallbacks`) that ran a failed turn on a
different provider. That was removed; schema version 101 drops the table and
the endpoint no longer exists, so an old app calling it gets a plain 404.
Conversation history that already contains a `Clarp fallback context` block is
still hidden from the chat by the transcript importer.

## Verifying background retry

```bash
scripts/probe_provider_fallbacks.py   # real json_call against each installed provider
```
