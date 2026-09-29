# clarp-desktop (Rust)

Rust rewrite of the native desktop client in `../desktop`. State, protocol,
networking, models and media are Rust (cxx-qt 0.10); presentation stays QML.
`PARITY.md` tracks what is ported and verified against the C++ app.

```sh
cargo build
QT_FORCE_STDERR_LOGGING=1 QT_QPA_PLATFORM=offscreen ./target/debug/clarp-desktop --probe-exit
```

Qt logs to journald unless `QT_FORCE_STDERR_LOGGING=1` is set.
