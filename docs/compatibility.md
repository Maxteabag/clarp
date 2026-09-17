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
