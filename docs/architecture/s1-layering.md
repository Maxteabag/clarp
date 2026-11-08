# S1 layering: design (move-only)

Status: design for review (reviewer A), approval by the lead before any move.
Owner: contract owner (rf-contract). Findings: PT-04, PT-05, PT-14. Guards it
builds on: `tests/unit/test_import_layers.py`, `test_lazy_imports_resolve.py`
(S2, reviewed at 9bb0ba68, not yet on main) and `test_schema_golden.py` (on
main). Every count below was measured on main 658d18ed with the S2 layer
guard at 9bb0ba68; reviewer A reproduced them.

## 1. What S1 does and does not do

S1 makes the persistence layer clean and gives every later slice a stable map.
Every S1 commit is move-only: no behaviour changes, so no intended differences
need registering. The schema golden, the guards, the full suite and a G2 replay
compare prove it.

In scope:

1. The layer map: four refinements and the `lib.persistence` namespace rule in
   the S2 layer test (section 2).
2. One complete schema module, `lib/persistence/schema.py`, with no feature
   imports (PT-05): the feature `SCHEMA` constants move, under their unchanged
   names, into fragment modules under `lib/persistence/schema_parts/` (3.1).
3. One file per migration step function, `lib/persistence/migrations/vNNN.py`,
   with the runner `_migrate` moved unchanged to `lib/persistence/migrate.py`
   (PT-04).
4. The persistence layer made strict in the layer test, with an exact list of
   named exceptions.

Not in scope, with the owner:

- The 111 core → features edges. Most come from the turn engine and the
  backend runners (turn_dispatch 21, backend.* 46, message_live and
  message_writes 14, runtime_bridge 6) calling TTS, live, goal and job
  features after a turn. They are S4's turn-callback seam and S6. S1 only
  ratchets them.
- `features → http` (dispatch_adapters → janitor_http) is S3, and
  `core → http` (turn_slots → terminal_ws) is S4.
- The `agents.soft_delete` cascade (agents → task_plans, artifacts,
  goal_ledger). Its callers are core (agent_store, agent_lifecycle,
  runtime_bridge), so moving the function up a layer would only move the
  edge. Inverting it needs a registration seam, and a seam can drop a
  cascade if one process never registers it. That is a behaviour risk, not a
  move, so it goes to the fix lane or S4 with its own test and G2 (section 7,
  question 3).
- Freezing migration SQL as literal copies, a step registry replacing the
  `_migrate` if-chain, and extracting the inline steps into their own
  functions. None of them is a move: renames and rewritten control flow fail
  the AST identity gate. They belong to a later behaviour slice with its own
  proof (section 7, question 5).
- Splitting `db.py` (568 lines, under the 1,500-line limit). Tests patch its
  globals (`db.now_ms` 33 times, `db.conn`, `db.DB_PATH`,
  `db._open_connection`, the lock and retry constants). Moving code out of the
  module those patches target would quietly stop the patches reaching it.
  The gain is small and the risk real, so `db.py` stays one module (section
  7, question 1).

## 2. Target layer map

The four layers stay as today (persistence < core < features < http). S1
changes the map in four places, all in the test, all from what each module
actually does:

| Module | Today | Target | Why |
|---|---|---|---|
| `data_root_locks` | persistence | features | an ops module: it imports deployment, host_generation, instance_backup and server_identity |
| `http_utils` | http | core | Principal/Responder value types and helpers that features take as parameters (rule 8); 8 feature edges point at it |
| `ws` | http | core | WebSocket framing used by feature streams; 5 feature edges |
| `bonjour` | features | http | LAN service advertisement for the local transport |
| `lib.persistence` and every module under it | (new) | persistence | a namespace rule in the map, like `lib.backend` and `lib.policies` today, so a new file under it is classified without editing the map |

`log` stays core. Four persistence edges remain as named exceptions, each with
its reason, checked exactly rather than ratcheted (a new edge fails, a gone
one must be removed from the list):

| Edge | Reason |
|---|---|
| `db → log` | logging is cross-cutting |
| v99 `→ tool_explanation_learning` | the step calls `backfill_programs(con)`, live feature logic |
| v112 `→ goal_ledger` | the step calls `goal_ledger.backfill(con)` (and its ledger helpers) |
| v121 `→ provider_background_jobs` | the step calls `expire_lost_evidence` |

The three backfills run feature code inside a migration. Freezing or
relocating them is behaviour work, not a move, so S1 keeps them as they are.

Upward edges: 149 today; 131 after the map change (persistence 18); 117
after S1. Persistence goes from 22 to the four named exceptions: commit 3
removes the 11 `db_schema` edges, and the import rewrites in commit 5 remove
three of the six `db_migrations` edges (audio_bookkeeper, janitor_autonomy and
judgments, which the migrations import only for their SCHEMA strings). The
remaining 113 are owned as listed in section 1 (core → features 111,
features → http 1, core → http 1).

## 3. Package layout and the move map

```
server/lib/persistence/
    __init__.py           (empty)
    schema.py             was db_schema.py, unchanged except its 11 feature imports, which now point at fragments
    schema_parts/         one module per feature schema constant (3.1)
        __init__.py       (empty)
        html_forms.py ... provider_background_jobs.py
    migrate.py            was db_migrations.py: _migrate, _create_schema, _user_version
    migrations/
        __init__.py       (empty)
        v062.py ... v121.py   each holds its _migrate_to_vNNN, moved verbatim
server/lib/db_schema.py      alias of lib.persistence.schema (see 3.3)
server/lib/db_migrations.py  alias of lib.persistence.migrate
server/lib/db.py             unchanged
```

### 3.1 Feature schema constants (PT-05)

Each constant moves byte-identical, under its unchanged name, into its own
fragment module `persistence/schema_parts/<feature>.py`. Nothing is renamed:

- The feature module imports it back under the same name
  (`from .persistence.schema_parts.html_forms import SCHEMA`), so
  `html_forms.SCHEMA` keeps working and is the same object.
- `persistence/schema.py` changes only the source of its existing imports.
  `from .html_forms import SCHEMA as _HTML_FORMS_SCHEMA` becomes
  `from .schema_parts.html_forms import SCHEMA as _HTML_FORMS_SCHEMA`, binding
  the same local name to the same object.
- `schema.py` stays the one entry point that assembles `_SCHEMA_SQL`, and its
  size does not change (1,398 lines today; the 15 constants live in 14 small
  fragment modules, about 360 lines, instead of growing it past the 1,500-line target).

| Old (feature module, name unchanged) | New home |
|---|---|
| `tool_explanation_mappings.SCHEMA` | `persistence/schema_parts/tool_explanation_mappings.py` |
| `tool_explanation_learning.SCHEMA` | `persistence/schema_parts/tool_explanation_learning.py` |
| `html_forms.SCHEMA` | `persistence/schema_parts/html_forms.py` |
| `model_fallbacks.SCHEMA` | `persistence/schema_parts/model_fallbacks.py` |
| `podcast_history.SCHEMA` | `persistence/schema_parts/podcast_history.py` |
| `agent_goals.SCHEMA` | `persistence/schema_parts/agent_goals.py` |
| `oracle_memory.SCHEMA` | `persistence/schema_parts/oracle_memory.py` |
| `audio_bookkeeper.SCHEMA_STATEMENTS` | `persistence/schema_parts/audio_bookkeeper.py` |
| `html_report_revisions.SCHEMA` | `persistence/schema_parts/html_report_revisions.py` |
| `html_form_events.SCHEMA` | `persistence/schema_parts/html_form_events.py` |
| `goal_ledger.SCHEMA` | `persistence/schema_parts/goal_ledger.py` |
| `janitor_autonomy.SCHEMA` | `persistence/schema_parts/janitor_autonomy.py` |
| `judgments.SCHEMA` | `persistence/schema_parts/judgments.py` |
| `provider_background_jobs.SCHEMA`, `.TURN_SCHEMA` | `persistence/schema_parts/provider_background_jobs.py` (both names) |

A feature module that reads its own constant elsewhere in the file keeps
working through the re-import. If a fragment depends on anything besides string literals, it is
listed in the move map and checked by the AST gate like any moved
definition.

`_SCHEMA_SQL` is assembled in the same order from the same strings, so
`sqlite_master` of a fresh database is unchanged: the schema golden must pass
byte-identical. Six tables from three schema modules are created on a fresh
database only by the steps after `_create_schema` (see 3.2):
janitor_continuity and janitor_quota_receipts (janitor_autonomy),
judgment_decisions (judgments), and provider_job_cursors, provider_turns and
provider_task_turns (provider_background_jobs). They stay created there.
Folding them into `_SCHEMA_SQL` would change what `_create_schema` runs, so
that is not a move.

### 3.2 Migrations (PT-04)

Today `_migrate` is one function. It calls 40 `_migrate_to_vN` helpers and
holds about 20 inline steps. The steps from v84 on sit outside the `else`, so
they also run on a fresh database (that is how the six tables in 3.1 get
created). There are exceptions inside that region: v121 runs only for
`0 < version < 121`, so a fresh database skips it.

S1 moves code without changing any control flow:

- Each `_migrate_to_vNNN` moves verbatim, with the same name, to
  `migrations/vNNN.py`.
- `_migrate`, `_create_schema` and `_user_version` move verbatim to
  `persistence/migrate.py`. The if-chain stays exactly as it is, including
  the `else` boundary, the v84+ region, the `0 < version < 121` predicate, the
  transaction and the final `PRAGMA user_version`. The inline steps stay
  inline.
- The only edits are import lines. `migrate.py` imports each step function
  by its unchanged name, so the call sites are byte-identical. A lazy
  `from .judgments import SCHEMA as judgments_schema` (likewise
  janitor_autonomy, audio_bookkeeper, provider_background_jobs `SCHEMA` and
  `TURN_SCHEMA`) becomes an import of the same object from its
  `persistence.schema_parts` fragment (3.1). The binding name and the call-time lookup stay
  the same, and so does the value.
- The v99, v112 and v121 steps keep their lazy feature imports and calls (the
  named exceptions in section 2).
- `db.__getattr__` keeps forwarding through the `db_migrations` alias:
  `_migrate`, `_create_schema` and every `_migrate_to_vN` stay attributes of
  `persistence.migrate` (imported names). `db.py` itself is not edited.
- Behaviour is pinned beyond the schema golden. A start-version matrix test
  runs, for every start version from 61 to the head and for a fresh database,
  which steps execute (recorded through the moved functions) and the final
  stamp. It runs on the old layout before commit 5 and must be identical
  after it. It proves, for example, that a fresh database skips v121 and an
  upgrade from 119 runs it.

### 3.3 Old import paths keep working: alias, not facade

`db_schema` and `db_migrations` are imported, and patched, by name in tests,
hooks and scripts (for example `monkeypatch.setattr(db_schema,
"_SCHEMA_VERSION", ...)`, `db._migrate_to_vN`). A re-exporting facade would be
a second module object, and a patch on it would not reach the moved code.
So the old file becomes an alias of the new module:

```python
# server/lib/db_schema.py
import sys
from .persistence import schema as _schema
sys.modules[__name__] = _schema
```

Python's import system returns the module found in `sys.modules` after
executing the file, so `from . import db_schema`, `import lib.db_schema` and
`lib.db_schema` as a package attribute all name the same object as
`lib.persistence.schema`. A test proves it, including that a monkeypatch on
the old name is seen by the new code. `db.__getattr__` keeps forwarding
`db._migrate`, `db._SCHEMA_VERSION` and `db._migrate_to_vN`: every step
function stays an attribute of `persistence.migrate`, because `migrate.py`
imports each one by its unchanged name, and `db_migrations` is that module.

## 4. Order of moves

One commit each, in this order. Each passes the gates in section 5 before
the next starts.

| # | Commit | Kind |
|---|---|---|
| 1 | layer map refinement and the `lib.persistence` namespace rule (section 2), and the named-exception mechanism | test only |
| 2 | `lib/persistence/` package; `db_schema.py` moves to `persistence/schema.py`; alias | move |
| 3 | the feature SCHEMA constants move into `persistence/schema_parts/` under unchanged names (3.1) | move |
| 4 | the start-version matrix test (3.2), on the old layout | test only |
| 5 | `db_migrations.py` moves to `persistence/migrate.py` and each `_migrate_to_vNNN` to `migrations/vNNN.py`, names and control flow unchanged; SCHEMA imports retargeted to `persistence.schema_parts`; alias | move |
| 6 | persistence made strict in the layer test: zero upward edges besides the four named exceptions (section 2) | test only |

**Inverted alias until the end (lead, 3 Nov).** A main edit to a file that
an alias replaced conflicts on every merge into refactor/integration (git
cannot follow a rename when the old path still exists). So commit 2 keeps the
code in `lib/db_schema.py` and makes `lib/persistence/schema.py` the alias
of it; commits 3 and 6 follow on that layout. The physical move of
`db_schema.py` (flipping the alias) and commit 5 are made last, against the
final main, in one claim window with full gates. Until then every merge of
main into integration runs the schema golden and the migration matrix, and a
conflict in `db_schema.py`, a feature's SCHEMA string or `db_migrations.py`
is resolved by the contract owner as a commit reviewed by reviewer A.

Commit 2 is a pure rename plus the alias. Commit 5 is the largest, but every
definition in it is moved verbatim, so the AST identity gate covers all of it.

## 5. Gates for every move commit

- **AST identity** (new tool, `scripts/refactor/ast_identity.py`): for every
  top-level function, class and constant that moved, `ast.dump` of the old
  and new definition is equal, under its unchanged name. The only allowed
  differences are import statements. A changed import must bind the same name
  to the same object: the tool resolves both and compares identity. Nothing
  else is normalized: no renames, no control-flow changes.
- `test_schema_golden.py` byte-identical (fresh and v61-upgraded databases),
  `test_db_migrations.py`, `test_import_layers.py`,
  `test_lazy_imports_resolve.py`, `test_table_writers.py`, the alias test.
- The migration rehearsal (synthetic, production-shaped, v61 → head) on
  heavy-remote: same row counts and state_log digest as before the move,
  migration time within ±20% of the baseline run.
- Full suite on heavy-remote (G1), and G2. G2 is a replay compare of head
  against its own base, using the harness version that has reviewer B's
  SOURCE YES, with zero differences. Moves register no intended differences.
  Replay jobs run one at a time on the VM under heavy-remote's replay lock.
- Reviewer A's SOURCE YES on the exact commit, and the merge captain.

## 6. Rebase helper and move map for other branches

Every commit ships a machine-readable move map,
`docs/architecture/s1-moves.json`: per commit, `{old_module, new_module,
symbols: {old_name: new_name}}` (in S1 every new name equals the old one).
The helper, `scripts/refactor/s1_rebase.py <branch> [--apply]`, does three
things:

1. **Report:** lists the branch's commits that touch a moved file, and which
   symbols they change. It reads the parked-branch manifest.
2. **Transplant, by three-way merge.** For each moved symbol the branch
   changed, it takes three versions: base (the symbol in the old file at the
   branch's merge-base with main), ours (the symbol at its mapped destination
   on current main) and theirs (the symbol in the old file on the branch).
   - If ours equals base (main has not touched the symbol since), it replaces
     the destination span with theirs.
   - Otherwise it runs `git merge-file` on the three texts. A clean merge is
     applied. A conflict is reported and the symbol is left untouched; it
     never takes either side wholesale.
   - Edits outside moved symbols are applied as ordinary patches.
   - Anything it cannot locate is reported, never guessed.
3. **Imports:** optional, because the aliases keep old imports working. It
   rewrites `from .db_schema import X` style imports to the new module.

It never pushes and works on a new branch `<branch>-s1`. Its tests use
synthetic branches:
- a branch edit to a moved migration step while main is unchanged (replace);
- a branch edit and a separate main edit to different lines of the same moved
  function (three-way merge keeps both);
- overlapping edits (conflict reported, destination unchanged);
- an edit to a moved schema constant;
- a symbol the helper cannot locate.

## 7. Questions for the reviewer and the lead

1. Keep `db.py` whole, as recommended because of the monkeypatch evidence?
   Or split it, with `lib.db` aliased to `persistence.connection` (the whole
   module, so patches still work)?
2. The v99, v112 and v121 backfill calls into feature code: keep them as named
   exceptions (recommended; reviewer A agrees), or freeze copies of the
   functions into the migration files (duplicates of live logic that can
   drift, and behaviour work)?
3. The `agents.soft_delete` cascade: confirm it leaves S1 for the fix lane or
   S4, as a registration seam with a test that every composition root
   (server, runtime) registers the cascades before any delete can run.
4. The named-exception mechanism (the four edges in section 2): agree that
   exact, reasoned exceptions are acceptable in a strict layer, instead of a
   ratchet?
5. The migration step registry, the extraction of inline steps and frozen
   literal SQL: confirm they leave S1. If wanted later, they are a behaviour
   slice proven by the start-version matrix test, the schema goldens and the
   rehearsal, with the lead's explicit classification.


## Wave 2: strict-layer code inversions (937)

- `terminal_presence` is a new core primitive owning the process-local terminal counter and lock. HTTP `terminal_ws` imports its mark/query operations; core `turn_slots` queries the same owner without importing PTY/WebSocket transport. This does not reclassify `terminal_ws`.
- `janitor_runtime.runtime_available` owns the shared feature-level Janitor capability query. HTTP `janitor_http` re-exports it; the scheduler imports the feature owner, preserving the in-process and fail-closed capability behavior.
- `turn_identity` owns the unchanged `TURN_ENV` literal and token regex used for both identity lookup and provider evidence. `provider_background_jobs` imports/re-exports that vocabulary; identity lookup no longer imports the feature observer. Token/environment/SQL behavior and layer assignments are unchanged.

- `agent_mcp_codec` is a new core primitive owning the stored MCP selection JSON codec. Feature `mcp_selection` re-exports the exact functions; core lifecycle and backend consumers import the canonical owner. The existing feature module retains its classification and stored JSON semantics.

- `stream_pacing` is a new core primitive owning generic markdown prefix pacing, per-instance timer generations and trailing throttling. Feature `live_pacing` preserves exact object exports; provider/runtime core consumers use the canonical owner. The existing feature module retains its classification. Muse timer observers patch the canonical scheduler read by the moved classes.

- `workspace_paths` is a new core primitive owning call-time native/container path confinement and recovery. Feature `launch_paths` preserves exact function exports; lifecycle and the generated Stop helper import the canonical owner through named recipe repoints. Environment/path semantics and Stop ownership remain unchanged.

- `runtime_rpc_server` owns the application runtime server and socket handler in the feature layer. Core `runtime_bridge` retains its canonical protocol/client; the runtime entry point constructs the same server from the new owner. Both class bodies and all Stop/admission/drain/cleanup order remain unchanged; there is no reverse core export.

- `live_projection` owns the unchanged reference reducer; `live_core` owns the live projection kernel and sole process registry. Feature `live_items`/`live_hub` preserve canonical exports; feature observation/relay/forwarding remains above the kernel. Producer used attributes retain exact object identity even though their module aliases now name `live_core`. Dispatcher/lifecycle feature edges remain for separate repairs.

- Core backend leaves `agy_layout` and `claude_models` own the unchanged call-time AGY brain path value and Claude model-pin normalization. Feature transcript/capability modules re-export the canonical functions; hooks and launch import those functions directly without loading feature services. Environment/path/argv semantics remain unchanged.

The first twenty-three repaired upward edges are removed from the ratchet; this section does not authorize reclassifying any existing module or expanding named exceptions.

## Wave 2: lead-signed map corrections (937)

These are the four existing-module corrections signed by Hobbles for assignment 937, paired with removal of their eight baseline edges in this commit. No other existing module is reclassified.

- `conversation`: core → features because it is the HTTP-called application importer/query service; no library importer acquires a new upward edge.
- `voice_markup`: features → core because its shared text normalization imports only `re`, independently of synthesis and delivery.
- `diagnostics_settings`: features → core because cross-cutting logging capture settings depend only on standard library code and core `settings_store`.
- `voice`: features → core because it is a pure JSON codec/constants module for `voice_id`, independently of voice providers and playback.

The seven named exceptions and two aliases are unchanged. A3 source approval and captain remote architecture/gate receipts remain required before landing.
