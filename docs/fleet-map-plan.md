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

## 6. Astra's authority

Tier 2 has full authority over how the system represents things, and standing
permission to *restructure*, not merely to add. Explicitly:

- invent a new archetype when the existing five do not fit, and add it to the
  library;
- **merge** entities that should have been one;
- **split** entities that conflate different things — the `file`, `path`,
  `script` and `repo` buckets are 84% of all traffic and almost certainly
  hide real structure;
- refactor the renderer, the stores, or these contracts when a better shape
  becomes apparent as the map grows;
- mark an earlier decision stale and redesign it.

That last point matters. A decision is frozen so the map stays readable, but
frozen must not become stuck: if a representation is wrong, Astra may
supersede it. Prefer an explicit "this one is wrong, think again" path over
periodic regeneration — regenerating nightly reintroduces the churn that
freezing exists to prevent.

### The three constraints that remain

These are mechanical, not permission gates:

1. **The hot path stays deterministic.** No model call during a render. Same
   command must always reach the same node, or position stops meaning
   anything and the map is worth less than a log.
2. **Generated logic is sandboxed and time-boxed.** Pure function: state in,
   drawing commands out. On throw or overrun the node reverts to the
   placeholder. Degrade to boring, never to broken.
3. **A live decision is stable until explicitly superseded.** Not immutable —
   stable.

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

## 10. Creative source authorship — current implementation

The owner's clarification supersedes the earlier recipe-only interpretation:
**the application writes its visual software, not just its icons.** Astra is
encouraged to improvise, invent systems, rewrite hierarchy and layout, develop
animations and change the premises of the whole world. A territory, machine,
organism or transport system is an artistic choice. There is no closed shape,
kind, archetype or animation vocabulary for the source author.

The operative creative brief is `server/lib/viz_creative_brief.md`. It is loaded
into real Astra requests with the current complete source and observed facts.
Spark identifies whether that software can meaningfully represent new evidence;
Astra develops a new source revision when it cannot. Explicit reinvention also
bypasses novelty triage. Both apply automatically, without a review queue.

### What the author actually writes

`program.files` contains executable CommonJS source modules. The entry exports
`render({ctx, scene, time, width, height, camera, playhead, interaction,
reducedMotion})`. It has the full Canvas API, ordinary JavaScript algorithms,
module state and imports of other supplied modules. It draws the entire world.
The server's `scene.entities` and `relations` are evidence helpers, not a required
visual schema: authored source can derive a different model from the raw events.

`static/viz-world/` contains **The Lantern Works**, produced by a real
`gpt-6-astra` development call: two source modules replace the initial renderer
with local repository workshops, file manuscripts, a GitHub harbor, origin
routes, action machinery and agents traveling as lantern sailboats. Its manifest
records the source digest and the author's explanation. This is the initial
software, not a permanent style restriction.

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

Source is syntax-checked without executing it, materialized into versioned
`viz-programs/<digest>/` directories, and atomically published through the JSON
library with a revision check. No schema migration is introduced. Compiler
feedback can trigger automatic repair. New source explicitly supersedes the
previous version; novelty is recorded so normal polling does not regenerate it.

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
  --reason 'Invent and implement a new visual system for this evidence'
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
