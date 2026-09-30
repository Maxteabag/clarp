# Experiment: the quick switcher in Slint

Clarp's Ctrl+K switcher as a Rust-native Slint window (winit + Slint's
software renderer, no Qt), to judge whether rewriting the QML UI in a Rust
toolkit is worth it. It reuses the Rust engine: the roster, ranking
(`switcher_rank`) and reading theme from `clarp-core`, the Host client from
`clarp-net`.

Run against your local Host (read only; Enter prints the chosen session):

    cargo run --release -p clarp-switcher-slint

Offline render, as used below:

    clarp-switcher-slint --fixture switcher-slint/tests/snapshot.json \
        --query ada --press Down --screenshot out.png --size 640x440 --scale 2

## First results (release builds, offscreen, 2x scale)

| | Slint switcher | Same switcher in QML (Qt) |
|---|---|---|
| Start to first frame | ~53 ms | ~130 ms |
| Peak memory | ~31 MB | ~97 MB |
| Shared libraries | 13, no Qt | 79 |
| Binary, stripped | 27 MB (toolkit linked in) | 23 MB + Qt libraries |

Typing, Up/Down/Enter/Escape, ranking and the theme work; `docs/*.png`
are renders from the fixture. Not tried yet: IME input, screen readers,
long rich-text transcripts (the hard part of the real UI).
