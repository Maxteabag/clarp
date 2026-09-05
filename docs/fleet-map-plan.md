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

## 10. Implemented contract and verification

The map is a **desktop, pannable world**. Its minimum viewport is **900 × 600**;
smaller windows show an explanatory message. Off-screen nodes are valid.
Panning, pointer-centered zoom and Fit change the camera, never clamp node
positions to the viewport. This supersedes any phone-fit interpretation.

- `viz_corpus.tool_rows` uses bounded keyset pages and a high-water mark.
  Both the author CLI and HTTP map use it; the map retains only a bounded
  projection of the newest events. Raw corpus rows are never fetched together.
- `viz_library.json` lives under `xdg.data_dir()`. Atomic replacement, a writer
  lock, revision checks and explicit decision IDs protect stable decisions.
  No schema migration is introduced. Seed rules also require supersession.
- Spark points at a current library entity or returns `NOVEL`. Astra can add
  semantic verbs, kinds and archetypes, choose shapes, author bounded drawing
  logic, redirect merged identities and supersede prior rule selectors.
- HTTP sends provisional events before offering work to one bounded cold
  worker. Failed tools have a retry cooldown; successful decisions become
  dictionary lookups. CLI calls are isolated, process-group owned and timed.
- Generated logic is a pure JSON drawing language interpreted inside a worker:
  64 commands, 512 expression operations, depth 8, a 4 ms evaluation deadline,
  and a 1 s worker startup/completion fuse. It has no eval, loops, property
  access, filesystem, DOM or network capability. Failures retain a placeholder.
- Known service marks are vendored and cached; long-tail glyphs and shapes
  persist with the entity decision. The renderer preserves positions across
  refresh and alias adoption. New live events animate without replaying old
  history. Repository/file roles use explicit paths and Git worktree metadata.
- Desktop sidebar and Agents overview link to `/viz`. Clicking an authored or
  seeded registry node exposes “Rethink this representation”, which submits an
  explicit supersession. This applies automatically, with no review queue.

Repeatable checks (from this checkout):

```bash
uv sync --frozen --group dev
.venv/bin/python scripts/viz_author.py --db ~/.local/share/clarp/state.sqlite
# Optional real-model run, with an isolated JSON store:
.venv/bin/python scripts/viz_author.py --db ~/.local/share/clarp/state.sqlite \
  --learn --limit 1 --library /var/tmp/fleet-map-smoke/library.json
# Foreground, read-only preview; no runtime startup or automatic model calls:
.venv/bin/python scripts/viz_preview.py --db ~/.local/share/clarp/state.sqlite --port 7699
# In another shell after npm ci and installing Playwright Chromium:
node scripts/viz_screenshot.mjs http://127.0.0.1:7699/viz /var/tmp/fleet-map-check
make py
make js
npm run build
```

Open the resulting screenshots; assertions are not a substitute for looking.
The browser harness covers replay, distinct labels, camera navigation, the
minimum-size gate, provisional identity adoption and a throwing generation
while animation frames continue. Unit tests also cover worker timeout,
subcommand-only model rules, aliasing, explicit supersession, corrupted JSON,
and canonical Git worktree identity. The clamp and runtime-cwd limitations in
§7 remain upstream instrumentation work.
