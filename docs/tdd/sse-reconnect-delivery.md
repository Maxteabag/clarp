# SSE replay handoff, reconnect ownership, and contact health

Draft TDD slice from umbrella audit #25, rebased onto `eeecc72`.
These are reproductions and an implementation contract, **not completed fixes**.
Keep this PR draft until the implementation and its positive/negative controls pass.
Do not merge red tests into main.

## Accepted reproductions

- **R01** `tests/integration/test_server_di.py::test_sse_event_seen_during_replay_is_not_delivered_again_as_live`: SSE replay/live overlap duplicates a persisted event. Adapted the test to finish on a live sentinel, so a correct fix can pass without waiting for a nonexistent duplicate.
- **R48** `tests/state/client-health.test.js — keeps counting error responses as contact after an earlier success`: Continuous HTTP errors are still server contact. Health currently ages the last success and falsely reports network silence. Preserve authentication/readiness distinctions.
- **R52** `tests/state/regression-sse-reconnect.test.js — carries the last durable event id into a replacement EventSource`: Replacing EventSource loses its native cursor because no lastEventId is carried. Explicit reconnects need a server-scoped cursor; a global cursor across different servers is unsafe.
- **R53** `tests/state/regression-sse-reconnect.test.js — coalesces repeated reconnect requests into one replacement stream`: Repeated signals schedule multiple EventSources and leak ownership. Coalesce timers, close superseded sources and ignore callbacks from stale connections.

## Implementation and verification

Fence replay/live overlap by event identity. Keep one reconnect timer and one current EventSource; ignore stale callbacks. Scope a retained cursor to the server identity and demonstrate credential replacement recovery before stopping auth retries. Preserve HTTP-error contact separately from successful application response.

## Qualified or excluded claims

- **R54** (needs-contract): Known rejection should avoid retry noise, but permanently stopping reconnect may prevent recovery after cookie/token replacement. Add a credential-change recovery test before a circuit breaker.
