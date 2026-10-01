# Provider visibility probes

Run from the repository root. These use disposable data only; they never start
a visible desktop, a provider model, or a live Host service.

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
