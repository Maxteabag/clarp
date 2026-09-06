# Fleet map — specification and implementation plan

The map shows what a fleet of agents is doing, as a living picture rather than
a transcript. It exists for the reason in the README: a person should be
exposed to novel information, not made to filter repetitive junk by hand. A
glance should answer "what is everyone working on" without reading anything.

Branch: `feat/fleet-map`. This document is the handover spec.

## 1. What already works

| file | what it does | state |
|---|---|---|
| `server/lib/viz_normalize.py` | rules turning `state_log` tool calls into `(actor, verb, target)` | **96.1%** of 59,407 live events |
| `server/lib/viz_archetypes.py` | five behaviours + `validate_assignment` | done, tested |
| `server/lib/viz_rule_author.py` | asks a model for new rules | built, **needs reshaping** (see §5) |
| `server/server.py` | `GET /viz/events`, `GET /viz` | done |
| `static/viz.html` | canvas: force layout, pulses, scrubber | done, screenshot-verified |
| `tests/unit/test_viz_*.py` | 22 tests | green |

Verified working: live view and scrub-to-instant both render real data; HUD
reports what is drawn, not the window total.

## 2. Architecture: three tiers

Each tier discards ~99% before the next sees anything. That is the whole cost
model.

```
tier 0   dictionary       known → known                ~59,000×   ~4µs
tier 1   gpt-5.3-codex-spark   "is this new?"          ~hundreds  ~200ms
tier 2   gpt-6-astra           "design it"             ~few/week  ~40s
```

**Tier 1 cannot invent.** It returns an existing entity id or `NOVEL`. That is
what stops `python3`, `/usr/bin/python3` and `python3.13` becoming three nodes
with three different icons. Aliasing is its main job.

**Tier 2 designs.** Archetype, shape, icon, and logic when needed.

They never message each other. Tier 2 writes to the shared library; tier 1
receives that library in its prompt on every call. The library *is* the
channel — no ordering, no liveness coupling.

### Message contracts

```jsonc
// renderer → tier 1
{"ask":"identify", "exe":"pnpm", "raw":"pnpm install --frozen-lockfile",
 "cwd":"/home/peter/GIT/clarp",
 "known_verbs":[…], "known_entities":[…], "known_archetypes":[…]}

// tier 1 → renderer  (matched)
{"verdict":"variant", "of":"npm", "verb":"build", "kind":"toolchain",
 "confidence":0.94}

// tier 1 → tier 2  (escalation)
{"ask":"design", "raw":"git commit -m 'fix' (cwd /home/peter/GIT/clarp)",
 "why_novel":"acts on a local checkout; no entity for un-pushed repo state",
 "library":{"archetypes":[…], "entities":[…], "shapes":[…]}}

// tier 2 → system
{"entity":{"id":"repo:clarp@local","kind":"repo","shape":"box",
           "icon":"glyph:branch"},
 "rule":{"exe":"git","sub":"commit","verb":"vcs","target":"repo:{cwd}@local"},
 "archetype":"accumulator",
 "notes":"a commit adds to a local place; it should swell, not flash"}
```

## 3. Stores — where learning accumulates

| store | grown by | read by |
|---|---|---|
| rule table `exe → (verb, kind)` | tier 1 and tier 2 | the dictionary |
| archetype library | tier 2 | renderer **and tier 1's prompt** |
| entity registry (id, shape, icon) | tier 2 | renderer **and tier 1's prompt** |

The system learns by making itself dumber: every escalation becomes a
dictionary entry and never escalates again.

**Do not add a schema migration for these.** `origin/main` is at
`_SCHEMA_VERSION` 66 while feature branches are at 69, and a v68 collision has
already had to be repaired once (`f521fad`). Use a JSON store under
`xdg.data_dir()` until the branches converge.

## 4. The provisional node

The canvas must never block on a model.

```
unknown appears → node renders IMMEDIATELY  ◌ ?  (grey, shimmering)
tier 1 matched  → adopt the existing look
tier 1 novel    → keep shimmering, show "designing <name>" in the HUD
tier 2 returns  → morph into the real thing
```

## 5. Reshape `viz_rule_author.py`

It currently implements tier 2's rule half and queues proposals for review.
Change it to:

- add the tier-1 triage call (`gpt-5.3-codex-spark`) with the library in-prompt;
- **drop the review queue** — decisions apply themselves (owner's decision);
- keep `validate()` for the *closed* vocabulary tier 1 uses; tier 2 is not
  restricted by it (§6);
- keep the model injectable so tests stay hermetic (12 already exist).

**Known defect:** running it over the whole corpus was killed (`exit 137`,
memory). Page the `state_log` query instead of loading 59k rows at once. This
is the first work package.

## 6. Astra's authority: creative, compatible expansion

The owner clarified the policy after a disruptive boat-to-moth redesign:
**freedom to develop software is not permission to replace established concepts
on every new event.** Keep The Lantern Works as the default world and retain
Agent cabinets as a separate optional view.

Astra may add new mechanisms, hierarchy, interactions and semantic types, and
repair demonstrated defects. Ordinary evolution must grow in the direction the
app is already going: preserve familiar identities, visual language, existing
behavior and places. A new agent, file, checkout or repeated action normally
instantiates an existing representation and needs no code change.

A structural rewrite requires a strong, specific demand or a demonstrated
incompatibility that cannot be repaired locally. Broad aesthetic redesign is a
separate explicitly requested operation, not a consequence of novelty, a
revision increment, or clicking “Improve this detail”. Routine work applies
itself without a review queue; uncertain cases may leave the source unchanged.

The mechanical constraints remain: models never run in a frame, generated code
is sandboxed and time-boxed with fallback, and established decisions remain
stable until explicitly superseded for a concrete reason.

## 7. Blockers outside this branch

Both cap target resolution at ~19%; neither is fixable retroactively.

- `server/lib/codex_runner.py` clamps `"tool": name[:80]`, so most events lose
  the path, repo and subcommand. That file has another agent's uncommitted
  work in it — coordinate before touching.
- `runtimes` records no `cwd`, so relative paths cannot be anchored to a repo.
  Every tool row already carries `runtime_id`, so adding `cwd` at spawn fixes
  it going forward. Do **not** approximate with `agents.cwd`; it is a single
  mutable value and agents move between checkouts, so old events would be
  attributed to the wrong repo.

## 8. Work packages, in order

| # | package | acceptance |
|---|---|---|
| 1 | page the corpus query | full-corpus run completes, no OOM |
| 2 | tier-1 triage + aliasing | `pnpm` resolves as a variant of `npm`; variants of `python3` collapse to one node |
| 3 | entity registry + provisional nodes | unknown renders as `◌ ?` within one frame; never blocks |
| 4 | tier-2 design loop + sandbox | a novel entity gets an archetype and shape; a throwing generation falls back without dropping a frame |
| 5 | icons | real marks for known services; generated only for the long tail; cached by node id, one style contract |
| 6 | supersede path | a wrong decision can be redesigned once, without nightly churn |
| 7 | PWA entry point | reachable from the app |

## 9. Acceptance for the whole thing

- `make py` and `make js` green; no regression in the existing 22 viz tests.
- Rule coverage stays ≥96% on the live corpus.
- **Screenshot it and look at it.** Every defect found in this work so far —
  an empty canvas, invented `repo:null` nodes, two nodes both labelled
  `clarp` — passed its HTTP test and returned 200 with no console errors.
  A green suite is not evidence that a picture is right.

## 10. Source authorship with continuity — current implementation

The operative instructions are `server/lib/viz_creative_brief.md`. They encourage
creative local systems, compatible expansion and targeted repairs while
preserving the established world. Old instructions inviting wholesale novelty-
driven reinvention are superseded by §6 and this section.

The default **World** is The Lantern Works, with repository workshops, file
manuscripts, GitHub harbor, origin routes, action machinery and lantern sailboats.
The **Agent cabinets** option preserves The Jacquard Observatory's side-by-side
agent activity and nested cabinets. The selector is local to the browser, persists
across refreshes, and does not ask a model to regenerate either view. Cameras and
fallback source are kept separate. Routine autonomous learning targets World;
it does not silently replace the optional cabinets view.

### What the author writes

Published `program.files` contains executable CommonJS modules with the full
Canvas API. The render entry point and evidence helpers remain as before.
However, ordinary model replies now describe the concrete need and what they
preserve, then supply unique exact source-fragment edits and optional new helper
files. They may return `unchanged` when the current source already fits.

`viz_library.evolution_program` preserves title, entry and untouched modules,
rejects replacement programs, whole-file replacement and replacing existing files
through `new_files`, and requires an explicit redesign allowance for disruptive
changes. No live observer or generic improvement button grants that allowance.
This is a source-update contract, not a fixed drawing vocabulary. Existing
metaphors are reinforced by the brief; automatic semantic correctness is still
checked through runtime/visual verification, not inferred from a revision number.

`static/viz-world/` holds the original Astra-authored Lantern Works. Its exact
Jacquard successor is preserved under `static/viz-cabinets/`. Both manifests
record source provenance. Restoring the default is an explicit new JSON-library
revision with a backup of previous decisions; it requires no Git checkout.

### Evidence and honest uncertainty

`viz_world.py` retains explicit paths, recorded cwd, command text and actions;
reads Git metadata for real checkout/remote relationships; and derives file
purpose from type or source headers. It never substitutes mutable `agents.cwd`
for historical context. The old recording clamp still means many events omit
their target. Astra's first world represents those as an action machinery quay
and a fragment cabinet rather than fabricating a file, database or repository.
The preview and replay use real recorded fleet activity. Actions indicate
observed commands, not an inferred successful result.

### Mechanical containment

Targeted edits are applied to the current revision; the resulting source is
syntax-checked without executing it, materialized into versioned
`viz-programs/<digest>/` directories, and atomically published through the JSON
library with a revision check. No schema migration is introduced. Compiler
feedback can trigger automatic repair. A compatible source revision updates only its scoped behavior; it does not grant
permission to change the world's concepts. Covered novelty is recorded so normal
polling does not regenerate it.

The browser runs source in a worker inside an opaque-origin sandboxed iframe.
It has no host storage, DOM, filesystem or network. The host receives pixels and
inert inspection metadata. Startup and frame execution have deadlines; a throw
or overrun restores the prior world. There is no model call in a frame, and the
old restricted JSON drawing interpreter has been removed.

The desktop shell supplies pan, pointer-centered zoom, Fit, inspection and a
labeled 120x history replay. Minimum viewport: 900 × 600. Camera movement never
clamps world geometry to the viewport; off-screen content is normal.

### Repeatable proof

```bash
uv sync --frozen --group dev
.venv/bin/python scripts/viz_author.py --db ~/.local/share/clarp/state.sqlite
# A real source-writing call, isolated from the live library:
.venv/bin/python scripts/viz_evolve.py --db ~/.local/share/clarp/state.sqlite \
  --library /var/tmp/fleet-world/library.json \
  --reason 'Extend the current world where this evidence needs new behavior'
# Read-only preview of that exact generated source, no automatic model calls:
.venv/bin/python scripts/viz_preview.py --db ~/.local/share/clarp/state.sqlite \
  --library /var/tmp/fleet-world/library.json --port 7700
# In another shell, with Playwright Chromium installed:
node scripts/viz_world_check.mjs http://127.0.0.1:7700/viz /var/tmp/fleet-world-proof
make py
make js
npm run build
```

Inspect the screenshots and the recorded video. The verifier checks real
hierarchy/remotes, source execution, agent movement through recorded playback,
throws, infinite loops, opaque origin and blocked host storage. A successful
model call alone is not proof that the authored software works.


### Hosted live development

The local service must pass `--learn --library PERSISTENT_PATH` to
`scripts/viz_host.py`. Without `--learn`, the preview intentionally does not run
models. The learning-enabled host observes the live corpus every 15 seconds,
including when no browser is open, and accepts focused compatible improvements from
the inspector. It keeps the activity database read-only while publishing source
revisions to its separate JSON library. The HUD exposes actual novelty checking,
Astra development, and the last failure. New observations do not imply a redraw:
Spark first determines whether the current source already represents them.

The running `clarp-fleet-preview.service` uses an archived release and persistent
library under `~/.local/share/clarp-fleet-preview/`; use the service's
WorkingDirectory and ExecStart to verify its installed version and learning flag.


### Agent portraits

The owner explicitly replaced World-view boats with the agents' existing Clarp
portraits. This supersedes the boat-marker portion of the Lantern Works baseline,
not the workshop/harbor metaphor, activity machinery, routes or cabinet option.
`actors[].avatar_url` identifies the exact agent's uploaded image; the shared PWA
resolver supplies bundled persona portraits when appropriate. Missing images use
initials. Cached, resized image blobs enter the sandbox independently of frames;
the worker exposes decoded ImageBitmaps as `avatars[agent_id]` to visual modules.
`viz_avatar_upgrade.avatar_edits` applies only this scoped marker change to live
learned source so unrelated generated improvements survive deployment.
