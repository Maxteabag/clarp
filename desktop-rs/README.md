# clarp-desktop (Rust)

Rust rewrite of the native desktop client in `../desktop`. State, protocol,
networking, models and media are Rust (cxx-qt 0.10); presentation stays QML.
`PARITY.md` tracks what is ported and verified against the C++ app.

```sh
cargo build
# Never point probes at the real Host: they connect, select and send.
CLARP_BASE_URL=http://127.0.0.1:9 CLARP_TOKEN=probe CLARP_SETTINGS=off CLARP_WORKSPACE_STORE=off \
  QT_FORCE_STDERR_LOGGING=1 QT_QPA_PLATFORM=offscreen ./target/debug/clarp-desktop --probe-exit
app/tests/run-qml-probes.sh   # every offscreen QML probe; controller probes use app/tests/fake_host.py
```

Qt logs to journald unless `QT_FORCE_STDERR_LOGGING=1` is set.
