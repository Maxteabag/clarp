# Web client

Svelte 5 + Vite. Replaces the hand-rolled `static/app.js` (2752 lines) that
this client grew out of.

## Working on it

```
npm run dev       # dev server on :5174 with HMR — this is the daily loop
npm run build     # web/ -> static/index.html + static/app/bundle.js
npm run preview   # build, then serve the real bundle on :5175
make build        # same as npm run build
```

`npm run dev` proxies everything it does not own to a running Clarp (default
`http://127.0.0.1:7682`, override with `CLARP_UPSTREAM`), injecting the
`auth_token` from `~/.config/clarp/config.toml` so the browser never needs a
`?token=`. SSE on `/events` streams unbuffered. Editing a component hot-swaps
it without a reload, so scroll position and an unsent composer draft survive.

`static/` is served off disk in dev too, so `styles.css` and the vendored
libraries are live without a build.

One dev-only substitution: `/sw.js` is replaced with a no-op service worker.
The real one caches the shell and would keep serving the previous bundle after
every hot update. To exercise the actual service worker, run
`make deploy-static` and use the real server.

`scripts/deploy_static.sh` runs the build itself before syncing, so
`make deploy-static` cannot ship a stale bundle. The build output under
`static/` **is committed** — the Python server serves `static/` directly and
the deploy target has no Node toolchain.

## Why the build lands where it does

The Python server's asset routing is fixed: `/` serves `static/index.html`,
`/static/*` serves `static/*`, and a few root paths are special-cased in
`server.py`. Anything else falls through to the API router and 404s. So Vite
is configured with `base: '/static/'` and `outDir: '../static'`, and the bundle
name is **not** content-hashed — `static/sw.js` has to list it as a literal
path in its offline shell, and cache invalidation already happens through the
service worker's VERSION, which the server derives from the newest mtime under
`static/`.

`emptyOutDir` is off: `static/` also holds fonts, avatars, images, the
vendored libraries and `sw.js`, none of which are build output.

## Layout

```
web/index.html            shell — OS probe, stylesheet, vendored libs
web/src/main.js           auth bootstrap, then mount
web/src/App.svelte        composition root, boot effects, window handlers
web/src/lib/*.svelte      components
web/src/lib/*.js          rendering, lazy highlighting, fetch/log plumbing
web/src/stores/*.svelte.js  reactive state ($state), one module per concern
```

`@core/*` aliases to `static/lib/*` — the framework-free modules (state
machine, audio scheduler, player adapter, protocol constants, agent snapshot).
Those stay where they are: `tests/state/*.test.js` imports them from that path
and they are the one source of truth shared with the tests.

## Notes for editing

- **Prose vs chrome.** `styles.css` sets monospace as the default and opts the
  turn body back into a proportional face. Keep new chrome in mono.
- **Ids matter.** `styles.css` masks button icons off ids like
  `#historyToolsToggle` and `#overviewReload`. Renaming one silently removes
  its icon.
- **Turn keying.** `Transcript.svelte` keys `{#each}` on `turn.id` and
  `Turn.svelte` derives its HTML from `turn.revision`. That pair is what keeps
  an update from re-rendering the whole transcript — don't key on the index.
