# Contributing

Thanks for looking at Clarp. This page covers the development loop; the
design is in [ARCHITECTURE.md](ARCHITECTURE.md) and the client contract in
[docs/protocol.md](docs/protocol.md).

## Setup

```bash
git clone https://github.com/Maxteabag/clarp
cd clarp
uv sync --frozen          # Python 3.12+, locked environment in .venv
npm install               # Svelte, Vite, vitest, Playwright
```

Run the server from the checkout with `uv run python server/server.py`; the
PWA hot-reloads with `npm run dev`, which proxies API calls to a server on
`127.0.0.1:7682` (override with `CLARP_UPSTREAM`).

## Tests

| Command | What | Time |
|---|---|---|
| `make py` | Python unit and integration tests, all cores, 60 s per-test timeout | ~30 s |
| `make js` | JavaScript unit tests (vitest) | ~15 s |
| `make e2e` | Playwright against a throwaway Docker node | minutes |
| `make docker-test` | Build the image; exercise install, restart, backup | minutes |

Tests never touch a real install: every test owns a temporary database,
config, and cache; `systemctl` and `launchctl` are shimmed; and any socket to
a non-loopback address fails the test. Keep it that way: stub the provider
call, do not lower the guard.

The browser suite refuses to run without `CLARP_BASE_URL`. `make e2e` sets it
to a disposable container; never point it at a server you care about.

## Making changes

- **Wire constants** live in `server/lib/protocol.py`, `static/lib/protocol.js`,
  and the PWA protocol module. Change them together.
- **Schema changes**: edit `_SCHEMA_SQL` in `server/lib/db.py`, bump
  `_SCHEMA_VERSION`, and add a `_migrate_to_vN` that upgrades an existing
  database. `tests/unit/test_db_migrations.py` checks the two agree.
- **Endpoints**: handlers in `server/server.py` delegate to a module in
  `server/lib/`. Add the route to `docs/protocol.md` if a client is meant to
  call it.
- **The PWA bundle** (`static/app/`) is committed. Run `npm run build` and
  include the result when you change anything under `web/src` or `static/lib`.
- **No compatibility shims.** Server and clients ship together. When a shape
  changes, change the producer and every consumer; do not keep the old path.

## Commits and pull requests

Write the commit message for the person reading `git log` in a year: what
changed and why, in plain sentences. Keep pull request descriptions short and
concrete. Run `make py js` before pushing.

Contributions are accepted under the [CLA](CLA.md); the project is licensed
under [PolyForm Shield 1.0.0](LICENSE.md).
