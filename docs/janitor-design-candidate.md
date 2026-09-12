# Janitor policy and execution candidate

This is an opt-in candidate, not a deployed feature. Authenticated Host routes:

- `GET /janitors/design-policy`: current revision and defaults.
- `POST /janitors/design-policy`: `{expected_revision, configuration}`; atomic compare-and-swap. Supports `heartbeat_gate`, quota threshold/TTL and an ordered model chain. Existing explicit Janitor model configuration wins over global fallback configuration.
- `GET /janitors/SESSION/effective-model-chain`: effective source and ordered model list.
- `POST /janitors/quota-observation`: account/window-attributed remaining percent and observed timestamp. It persists one crossing outcome and rejects duplicate/out-of-order observations. Stale/future values remain unknown. This route does not probe providers or send push notifications.
- `GET /janitor-runs/RUN/receipt?target=SESSION`: current Janitor target scope must still permit the receipt. Host authentication is the outer boundary. This candidate does not implement a multiuser ACL system or replace every legacy read route.

When `heartbeat_gate` is enabled, periodic admission requires an actual active durable plan with a pending or in-progress item. Default false preserves existing scheduler behavior. A blocked item, leftover failed job or missing plan does not create a commitment. This conservative first slice does not introduce an atomic dispatch outbox or resume background-job-only commitments; existing restart recovery remains separate.

`janitor_partition_executor.execute_partitions` is a bounded adapter for existing tool-explainer demand jobs. Each trusted target has one serial partition; distinct scoped subscriber identities may run concurrently. Existing `begin_run` / `claim_run` / `complete_run` enforce durable once-only claims and current-generation publication. An unavailable admission returns deferred. Compute exceptions become a bounded failed result. The adapter neither calls a real provider nor retries unknown spend; callers supply the provider adapter. It does not relax one-active-run-per-identity or provide durable queued backpressure.

## Verification

Run in an isolated checkout using the existing pytest fixtures:

```
python -m pytest -o addopts='' -q tests/unit/test_janitor_design_policy.py tests/unit/test_heartbeat.py tests/integration/test_janitors_http.py
```

The tests instantiate actual Host SQLite and loopback authenticated HTTP, not a copied policy model. Computation uses a deterministic fixture and a barrier proving overlap across two targets; repeating accepted requests does not recompute. Provider availability/billing, production push delivery, runtime account switching and native global-model editor and real provider adapters remain unimplemented integration gates. The optional partition adapter now executes the ordered effective chain: only ModelUnavailableBeforeExecution permits a next model. Unknown errors stop. Global policy revision is rechecked in the publication transaction; changed policy rejects the result. Accepted request retries recover a receipt without recomputation. This proves the backend callback contract, not real-provider behavior.
