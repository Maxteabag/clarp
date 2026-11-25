# Contributing

Thanks for looking at Clarp. This page covers the development loop; the
design is in [ARCHITECTURE.md](ARCHITECTURE.md) and the client contract in
[docs/protocol.md](docs/protocol.md).

## Setup

```bash
git clone https://github.com/Maxteabag/clarp
cd clarp
npm ci                   # the web app's Svelte, Vite and vitest, from the lockfile
```

The Host is in `server-rs/`; read [server-rs/REBUILD.md](server-rs/REBUILD.md)
before changing it. Its Rust compiles on the build machine through
`server-rs/tools/build` (`check`, `test`, `release`), one build at a time. The
web app hot-reloads with `npm run dev`, which proxies API calls to a Host
(`CLARP_UPSTREAM`).

## Tests

- `server-rs/tools/final-pass`: the one short pass before a Host is installed.
- `server-rs/tools/build test [filter]`: the Rust tests, on the build machine.
- `npm test`: the web app's JavaScript tests (vitest).
- The desktop app's checks run in GitHub Actions only (`desktop-checks.yml`).

## Making changes

- **Wire constants** live in the Host (`server-rs/crates/clarp-protocol`) and
  `static/lib/protocol.js`. Change them together.
- **Endpoints**: add the route to `docs/protocol.md` if a client is meant to
  call it, and a row to `docs/compatibility.md` when the Host contract moves.
- **The web app bundle** (`static/app/`) is committed. Run `npm run build` and
  include the result when you change anything under `web/src` or `static/lib`.
- **Compatibility is deliberate.** The Host and the native clients release
  independently: the external core contract is additive, unknown response
  fields are ignored, and optional features use capabilities. Breaking
  changes require a protocol version and an explicit upgrade path.

## Commits and pull requests

Write the commit message for the person reading `git log` in a year: what
changed and why, in plain sentences. Keep pull request descriptions short and
concrete. A change lands once it builds and a reviewer has read it; tests run once, when
the work is complete and before it is installed (see AGENTS.md). No workflow
starts by itself; the full suite runs only when someone starts it on GitHub.

Contributions are accepted under the [CLA](CLA.md); the project is licensed
under [PolyForm Shield 1.0.0](LICENSE.md).
