# Codex connection recovery

A persistent Codex app-server can reject a turn with `usage_limit` while a new
connection using the current login works. That error alone does not prove the
whole account, or another model's quota bucket, is exhausted.

Clarp fingerprints the private credential file in memory when opening its
connection. Before admitting a turn it retires an idle connection if credentials
changed or a refresh was requested. Login/logout recycling also defers retirement
when another logical turn is active. No credential contents or fingerprints are
logged. The runtime service, agent identity, model and native thread are preserved.

A failed Codex turn before any output or tool activity can trigger one connection
refresh and one retry of the original admitted message. Admission and connection
retirement share a lock; another active turn prevents immediate recycling. The
next idle admission refreshes a deferred connection. Stop or supersession cancels
the pending retry, including a stop while checking quota.

The fresh connection reads `account/rateLimits/read`. A confirmed exhausted
matching bucket prevents a retry. A missing/sparse bucket is unknown: Clarp allows
one recovery attempt without claiming the account has available quota. A failed
quota read surfaces the original failure. A second usage error ends the attempt;
there is no connection-recovery loop. Requests that already produced output or
started tools are never automatically replayed by this recovery path.

The state stream reports “Reconnecting… Your message is saved.” during the retry
backoff. The PWA displays that state and clears it when normal work resumes. On
failure, the activity summary explains that the reply stopped and the message is
saved. Unverified Codex errors do not create inferred account-wide quota events.
The original provider error remains in the state detail for diagnostics.

## Verification

Run the real dispatcher against an isolated fake stdio app-server:

```sh
uv run --frozen --group dev python -m pytest \
  tests/unit/test_codex_connection_recovery.py \
  tests/unit/test_codex_app_server.py tests/unit/test_turn_dispatch.py
npm test -- tests/state/agent-recovery-banner.test.js
```

The fixtures cover a connection that only recovers after process replacement,
confirmed exhaustion, missing model quota, partial tool activity, unchanged thread
and admitted message identity, active sibling turns, changed credentials, and
cancellation. They use no live credentials or paid provider requests. These tests
prove the recovery mechanism, not a particular provider-side root cause.
