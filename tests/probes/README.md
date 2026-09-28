# Provider visibility probes

Run from the repository root. These use disposable data and Qt Core only; they
never start a visible desktop, a provider model, or a live Host service.

Build the production job reducer harness through CMake so Qt 6's own `moc` is
selected (the unqualified system `moc` can be Qt 5):

```sh
cmake -S tests/probes -B /var/tmp/clarp-job-reducer-probe -G Ninja
cmake --build /var/tmp/clarp-job-reducer-probe
CLARP_TEST_JOB_REDUCER=/var/tmp/clarp-job-reducer-probe/job-reducer \
  timeout 500 env -u CLARP_HEARTBEATS_DISABLED uv run --frozen --group dev \
  python -m pytest -n 0 tests/integration/test_server_di.py \
  -k provider_dedup_live_sse_existing_qt_reducer
```

That test sends real localhost HTTP SSE through the unmodified Qt reducer, keeps
the native mirror visible before explicit registration, and does not supply a
correcting snapshot to the reducer. Its temporary `sse-receipt.json` retains the
actual serialized events. Missing `CLARP_TEST_JOB_REDUCER` skips this optional
compiled-client proof; do not describe that skip as a passing client test.

Measure a 16-agent, 128 MiB transcript backlog and concurrent managed registration:

```sh
probe_dir=$(mktemp -d /var/tmp/clarp-provider-backlog-XXXXXX)
CLAUDE_PWA_DB="$probe_dir/state.sqlite" timeout 500 \
  uv run --frozen --group dev python tests/probes/provider_backlog.py "$probe_dir"
```

The script rejects an existing DB. Output names processed bytes, managed-event
delay, transaction hold time and managed-registration delay. Preserve the JSON
and identify source revision, machine/load, and reduced per-pass work when
comparing results. It is a bounded-fixture probe, not a production latency claim.
The test-only environment removal above never changes the Host recovery pause.
