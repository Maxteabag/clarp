# Host ⇄ iOS compatibility

Compatibility between the Host and the iOS app is decided by two integers on
each side, not by version strings. The Host advertises `contract.host` (the
contract it implements) and `contract.min_ios` (the oldest iOS contract it
still serves) in `GET /server-info`. The app carries `ClientContract.current`
and `ClientContract.minimumHost`, and sends `X-Clarp-Client: ios/<build>
contract=<n>` on every request. A pair is compatible when
`host ≥ minimumHost` and `current ≥ min_ios`.

**Every change to `HOST_CONTRACT` or `MIN_IOS_CONTRACT` in
`server/lib/server_identity.py` adds a row at the top of this table.**
`tests/unit/test_client_contract.py` fails when the newest row and the
constants disagree. The iOS repository keeps the mirror table in its own
`docs/compatibility.md`, checked by `scripts/verify_static.py`.

| Host contract | Min iOS contract | Host release | What changed |
|---|---|---|---|
| 10 | 1 | local candidate, 2026-09-24 | Hybrid tool explanations: `POST /tool-explanations` ready items gain `source` (`scripted`, `jev` or `llm`, the original producer) and `cached` (served from the Host cache); `include_provenance: true` adds `provenance` `{template_version, template_id?, parameters?, confidence?, learned?, fallback_reason?}`. Known tool calls answer synchronously from typed templates; the tool-explainer Janitor gains option `explanation_sources` (0 scripted→Jev→model, 1 scripted→model, 2 model only); judgment site `explanations`. Feature `tool_explanation_sources`. Older clients ignore the new fields. |
| 9 | 1 | local candidate, 2026-09-21 | Normal Oracle v2 durable owner/contact conversation resume and explicit fresh context; context_memory/context_reset capabilities and oracle_v2.context identity event. Agent histories and ongoing work retained. |
| 8 | 1 | controller-narration | `GET /controller-narration?text=` speaks one short line (at most 240 characters) in the Host's Cartesia voice for the iOS Flic tutorial mode and caches it per text; 400 for no text, 503 without a Cartesia key or voice, 502 when synthesis fails. Feature `controller_narration`. Older clients ignore it. |
| 8 | 1 | local candidate, 2026-09-20 | Optional per-session Oracle v2 direct-to-primary routing on normal/tinkered WS and tinkered RTC; capability direct_contact, routing_mode receipt. Legacy clients unchanged. |
| 7 | 1 | oracle-voice-and-queue | `POST /turn-queue/resume` `{session}` lifts the pause a Stop left on an agent's queue and returns the queue state; a new user message now lifts it on its own (`queueResumedBySend`). `GET/POST /oracle/contact` is the Host-owned Oracle contact, validated against the live roster, `{session, persona, stale}`; a stale one is cleared and reported once. Features `oracle_contact`, `turn_queue_resume`. Oracle sessions carry the roster and contact in their instructions and the routers regain `get_agent_status`; `oracle_v2.quiet` fires after 1.5 s of Oracle silence instead of 0.5 s and is a status hint only. `judgment_decisions` rows gain `question_json` and `state_json`. Older clients ignore all of it. |
| 6 | 1 | parked-turn-activity | A turn parked by Claude account failover already recorded `thinking` with `account_recovery` and a `message` in its state detail, which `agent-activity` flattened to phase `thinking` / summary `Thinking` — indistinguishable from real work. That activity event now carries phase `account_recovery`, action `waiting for account` and the Host's own message as the summary. The `agent-state` detail is unchanged; clients that only read `kind` see no difference. |
| 5 | 1 | agent-goals | Goals: `GET/POST /agent-goal` and `POST /agent-goal/{pause,resume,clear}` start, pause, resume, clear and read the objective a Codex agent keeps working toward on its own (native `thread/goal/*`). Each `/agents/snapshot` row gains `goal` (null or `{objective, status, token_budget, tokens_used, time_used_seconds, native, backend, created_at, updated_at}`); `goal-updated` SSE carries the new goal. Feature `agent_goals`. Non-Codex backends answer 501. Older clients ignore all of it. |
| 4 | 1 | quota-notice-followups | `backend_quota` gains `reason` (`credits_depleted` or `usage_limit`), read from the provider's own refusal, and keeps a reset time when the provider reports no usage window. Clients that ignore `reason` keep working. |
| 3 | 1 | backend-quota-notice | Each `/agents/snapshot` row gains `backend_quota` (null, or why the next turn is expected to fail and when the provider resets). `agent-roster` with `kind: "backend-quota"` asks clients to refetch. Feature `backend_quota`. Older clients ignore both. |
| 2 | 1 | PR112 | Adds authenticated `/attention/inbox` source-owned artifact eligibility and guarded pagination. Older clients/endpoints remain supported. |
| 1 | 1 | main, 2026-09-14 | Contract handshake introduced. `/server-info` gains `contract` and `client`; the Host reads `X-Clarp-Client`. Baseline for every feature already advertised in `capabilities.features`. |

## When to bump

- **Host contract** (`HOST_CONTRACT`): whenever the Host adds or changes
  something a client may depend on: an endpoint, a response field, an SSE
  event, a feature in `FEATURES`. New features also get an entry in
  `FEATURE_CONTRACTS` at the new number. Never decrease.
- **Min iOS contract** (`MIN_IOS_CONTRACT`): only when something older apps
  rely on is removed or changed incompatibly. This makes every app below the
  number show a red "update the app" banner, so it is a deliberate decision.
- The iOS app bumps `ClientContract.current` when it starts using a new Host
  capability and `ClientContract.minimumHost` to the Host contract that
  carries it. Its table row names the TestFlight build.

## Reading a mismatch

- App shows "Update Clarp on \<host\>": the Host's `contract.host` is below the
  app's `minimumHost`. Fix: update the Host (`clarp-admin update` or the
  in-app button).
- App shows "Update the Clarp app": the Host's `contract.min_ios` is above the
  app's `current`. Fix: update the app from TestFlight or the App Store.
- Host event log `clientContractOutdated`: an app below `MIN_IOS_CONTRACT`
  connected; the record names the build.
