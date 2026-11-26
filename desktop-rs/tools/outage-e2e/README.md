# Outage end-to-end check

Proves what the desktop shows while its Host restarts behind a gateway: a separate disposable
Rust Host (its own directories, port, token and name, no peers, `--launcher direct`,
`--accounts none`, a Claude Code stand-in so no model is asked), `gateway.py` in front of it
answering 502 while the Host is down or while told to, and the engine headless
(`cargo build -p clarp-engine --example outage_probe`) reading through the gateway.

- S1: the gateway cuts every connection and answers 502 for about 2 s while a turn happens.
- S2: the Host stops as for an update (SIGTERM, so it sends `host-updating`), the gateway answers
  502 for 44.5 s in all, the Host starts again on the same data and a turn happens while the
  gateway still answers 502.
- S3: a device token the Host never issued is said at once.

Run it on the build VM, never against a live Host (the test-clarp-host skill's
`guard_not_live.sh` is sourced first; copy it next to `run.sh` or point `GUARD` at it):

    HOSTBIN=.../server-rs/target/release/clarp-host SHIMBIN=.../clarp-shim \
    PROBE=.../desktop-rs/target/debug/examples/outage_probe bash run.sh
    python3 analyse.py /var/tmp/dr502-e2e-<stamp>

`analyse.py` prints the timeline and PASS/FAIL for each promise: no banner, nothing shown for
15 s, the host-updating line kept, the calm `reconnecting` after the grace, recovery by itself,
the gap's turns once and in order, one server identity, `Last-Event-ID` on every reopened stream.
