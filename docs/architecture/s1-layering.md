# S1 layering: design (move-only)

Status: design for review (reviewer A), approval by the lead before any move.
Owner: contract owner (rf-contract). Findings: PT-04, PT-05, PT-14. Guards it
builds on: `tests/unit/test_import_layers.py`, `test_lazy_imports_resolve.py`,
`test_schema_golden.py` (all S2 or Phase 1, ratchet mode).

## 1. What S1 does and does not do

S1 makes the persistence layer clean and gives every later slice a stable map.
Every S1 commit is move-only: no behaviour changes, so no intended differences
need registering. The schema golden, the guards, the full suite and a G2 replay
compare prove it.

In scope:

1. The layer map: four refinements in the S2 layer test (section 2).
2. One complete schema module, `lib/persistence/schema.py`, with no feature
   imports (PT-05): the feature `SCHEMA` constants move into it.
3. One file per migration, `lib/persistence/migrations/vNNN.py`, with frozen
   SQL, and one runner, `lib/persistence/migrate.py` (PT-04).
4. The persistence layer made strict in the layer test.

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

`log` stays core. Two persistence edges remain as named exceptions with
their reasons, checked exactly rather than ratcheted: `db → log` ("logging is
cross-cutting") and v121's call into `provider_background_jobs` (3.2).

Upward edges: 149 today, 131 after the map change, 115 after S1. Persistence
goes from 22 to the two named exceptions. The remaining 113 are owned as
listed in section 1 (core → features 111, features → http 1, core → http 1).

## 3. Package layout and the move map

```
server/lib/persistence/
    __init__.py           (empty)
    schema.py             was db_schema.py, plus the feature SCHEMA constants
    migrate.py            was db_migrations.py: _migrate, _create_schema, _user_version
    migrations/
        __init__.py       STEPS: ordered (version, fn) table, the single list
        v062.py ... v121.py   one per version, as they run today
server/lib/db_schema.py      alias of lib.persistence.schema (see 3.3)
server/lib/db_migrations.py  alias of lib.persistence.migrate
server/lib/db.py             unchanged
```

### 3.1 Feature schema constants (PT-05)

Each constant moves byte-identical into `persistence/schema.py` under a
prefixed name. The feature module keeps its public name by importing
downwards (`from .persistence.schema import HTML_FORMS_SCHEMA as SCHEMA`),
so every existing import keeps working:

| Old | New |
|---|---|
| `tool_explanation_mappings.SCHEMA` | `persistence.schema.EXPLANATION_MAPPINGS_SCHEMA` |
| `tool_explanation_learning.SCHEMA` | `persistence.schema.EXPLANATION_LEARNING_SCHEMA` |
| `html_forms.SCHEMA` | `persistence.schema.HTML_FORMS_SCHEMA` |
| `model_fallbacks.SCHEMA` | `persistence.schema.MODEL_FALLBACKS_SCHEMA` |
| `podcast_history.SCHEMA` | `persistence.schema.PODCAST_HISTORY_SCHEMA` |
| `agent_goals.SCHEMA` | `persistence.schema.AGENT_GOALS_SCHEMA` |
| `oracle_memory.SCHEMA` | `persistence.schema.ORACLE_MEMORY_SCHEMA` |
| `audio_bookkeeper.SCHEMA_STATEMENTS` | `persistence.schema.AUDIO_BOOKKEEPING_STATEMENTS` |
| `html_report_revisions.SCHEMA` | `persistence.schema.HTML_REPORT_REVISIONS_SCHEMA` |
| `html_form_events.SCHEMA` | `persistence.schema.HTML_FORM_EVENTS_SCHEMA` |
| `goal_ledger.SCHEMA` | `persistence.schema.GOAL_LEDGER_SCHEMA` |
| `janitor_autonomy.SCHEMA` | `persistence.schema.JANITOR_AUTONOMY_SCHEMA` |
| `judgments.SCHEMA` | `persistence.schema.JUDGMENTS_SCHEMA` |
| `provider_background_jobs.SCHEMA`, `.TURN_SCHEMA` | `persistence.schema.PROVIDER_JOBS_SCHEMA`, `PROVIDER_TURNS_SCHEMA` |

`_SCHEMA_SQL` is assembled in the same order from the same strings, so
`sqlite_master` of a fresh database is unchanged: the schema golden must pass
byte-identical. The three tables that only the v84+ steps create on a fresh
database (janitor_autonomy, judgments, provider_background_jobs; see 3.2)
stay created there. Folding them into `_SCHEMA_SQL` would change what
`_create_schema` runs, so that is not a move.

### 3.2 Migrations (PT-04)

Today `_migrate` is one function with 40 `_migrate_to_vN` helpers and about
20 inline steps. The steps from v84 on sit outside the `else`, so they run
on fresh databases too; that is how janitor_autonomy, judgments and the
provider job tables get created. The move keeps that control flow
exactly:

- Each version becomes `migrations/vNNN.py` with one function, `apply(con)`.
  It holds what the step runs today: the moved `_migrate_to_vNNN` body, or
  the inline block.
- `migrations/__init__.py` holds two ordered tables: `UPGRADE_ONLY`
  (62–83, run only in the `else`) and `ALWAYS` (84+, run for fresh and old
  databases alike), mirroring today's structure.
- `migrate._migrate` iterates them with the same version tests, the same
  transaction and the same final `PRAGMA user_version`.
- **Frozen SQL:** a step that reads a live constant today
  (`db_schema._HTML_FORMS_SCHEMA`, `judgments.SCHEMA`, ...) gets a literal copy
  of that string as it is at the move commit. The copy is byte-identical
  today, so behaviour is unchanged. A later edit to a feature's schema then
  no longer rewrites a past migration.
- **v121** calls `provider_background_jobs.expire_lost_evidence` (feature
  logic in a migration). I propose keeping that one call as a lazy import
  with a named exception for now (section 7, question 2).

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
`db._migrate`, `db._SCHEMA_VERSION` and `db._migrate_to_vN` (it resolves the
latter through the step table).

## 4. Order of moves

One commit each, in this order. Each passes the gates in section 5 before
the next starts.

| # | Commit | Kind |
|---|---|---|
| 1 | layer map refinement (section 2) and the named-exception mechanism | test only |
| 2 | `lib/persistence/` package; `db_schema.py` moves to `persistence/schema.py`; alias | move |
| 3 | the feature SCHEMA constants move into `persistence/schema.py` (3.1) | move |
| 4 | `db_migrations.py` moves to `persistence/migrate.py`; alias | move |
| 5 | split `_migrate` into `migrations/vNNN.py` with frozen SQL (3.2) | move + freeze |
| 6 | persistence made strict in the layer test (zero upward edges besides the two named exceptions) | test only |

Commits 2 and 4 are pure renames plus the alias, so their diffs are small and
easy to replay on parked branches. Commit 5 is the only large one.

## 5. Gates for every move commit

- **AST identity** (new tool, `scripts/refactor/ast_identity.py`): for every
  top-level function, class and constant that moved, `ast.dump` of the old
  and new definition is equal. The only allowed differences are import lines
  and the frozen copies in commit 5, which are checked against the original
  constant's value.
- `test_schema_golden.py` byte-identical (fresh and v61-upgraded databases),
  `test_db_migrations.py`, `test_import_layers.py`,
  `test_lazy_imports_resolve.py`, `test_table_writers.py`, the alias test.
- The migration rehearsal (synthetic, production-shaped, v61 → head) on
  heavy-remote: same row counts and state_log digest as before the move,
  migration time within ±20% of the baseline run.
- Full suite on heavy-remote (G1), and G2: a replay compare of head against
  its base with zero differences. Moves carry no intended differences.
- Reviewer A's SOURCE YES on the exact commit, and the merge captain.

## 6. Rebase helper and move map for other branches

Every commit ships a machine-readable move map,
`docs/architecture/s1-moves.json`: per commit, `{old_module, new_module,
symbols: {old_name: new_name}}`. The helper, `scripts/refactor/s1_rebase.py
<branch> [--apply]`, uses it in three ways:

1. **Report:** lists the branch's commits that touch a moved file, and which
   symbols they change. It reads the parked-branch manifest.
2. **Transplant:** for an edit to a moved symbol, it takes the branch's
   version of that symbol (located by AST in the old file) and replaces the
   symbol's span in the new file, then commits. Edits outside moved symbols
   are applied as ordinary patches. Anything it cannot place is reported,
   never guessed.
3. **Imports:** optional, because the aliases keep old imports working. It
   rewrites `from .db_schema import X` style imports to the new module.

It never pushes and works on a new branch `<branch>-s1`. It is tested on
synthetic branches: an edit to a moved migration, to a moved schema constant,
and an unplaceable conflict.

## 7. Questions for the reviewer and the lead

1. Keep `db.py` whole, as recommended because of the monkeypatch evidence?
   Or split it, with `lib.db` aliased to `persistence.connection` (the whole
   module, so patches still work)?
2. v121's call into `provider_background_jobs.expire_lost_evidence`: keep the
   lazy import as a named exception (recommended), or freeze a copy of the
   function into `v121.py` (a duplicate that can drift)?
3. The `agents.soft_delete` cascade: confirm it leaves S1 for the fix lane or
   S4, as a registration seam with a test that every composition root
   (server, runtime) registers the cascades before any delete can run.
4. The named-exception mechanism (`db → log`, v121 → provider_background_jobs):
   agree that exact, reasoned exceptions are acceptable in a strict layer,
   instead of a ratchet?
