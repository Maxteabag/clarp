# Bridge121 descriptor evidence and early-gate placement

Source-only supplement to f639, base f666fb0db240513ae6dae1eda8227046b4001088.
DESIGN EVIDENCE, NOT permission or a source/runtime verdict. No product import,
SQLite open, test or job was executed to derive this file.

## Concrete object descriptor input

`pt02-bridge121-object-manifest.json` derives TWO full variants from the exact
checked-in fresh.sql and v61_upgraded.sql. Each has368 schema objects including
implicit indexes and SQLite-owned objects,60868 normalized-DDL bytes and a largest
normalized definition of1028 bytes. Both files are82343 bytes, but five object
records differ; equal file size is not shape equality. The whole variants must
remain distinct. Do not accept a synthetic mixture of their individual definitions.

The JSON pins source file SHA256, per-object type/name/table/normalized-DDL hash,
implicit marker, size and the whole ordered-manifest digest. It does not contain
live data. Existing test_schema_golden.py:98-105 dumps ALL sqlite_master rows;
there is no 'ignore sqlite_%' rule in this contract. Dropping extra unknown objects
or implicit indexes from the check would weaken it. Exact golden ordering is
ORDER BY type,name and SQL NULL normalizes to '(implicit)'. The production pure
normalizer would preserve quoted string contents/doubled quotes while normalizing
comments/whitespace, as the existing golden owner does. No imported test module or
SQLite parser was executed in generating these documentation records.

Proposed input caps:512 total objects,8192 bytes per raw SQL definition and131072
total normalized-DDL bytes. Read no more than513 metadata rows and refuse an
oversized single definition BEFORE returning it to Python (bound SQLite projection
length and return explicit oversize code). Count/size checks cannot truncate and
then compare a prefix as complete. These are fixed bounded-work proposals for
review, not measured startup latency, valid121 compatibility proof or performance
results. Real earlier-release schemas/index variants must be exercised by the
captain's existing matrix; the two fixtures alone do not prove all installed121
shapes. Unknown121 variants refuse rather than repair themselves.

At code generation time, the installed descriptor must carry these reviewed
expected records directly; do not require tests/fixtures to exist on the installed
Host or silently load an arbitrary caller-supplied manifest. Exact manifest source
and normalizer need A2 review before code acceptance. Descriptor has only version121;
no future W/journal format is certified. No persisted metadata or schema stamp is
written by validation.

## Actual initial import/gate seams

| Owner | Exact base boundary | Proposed placement / remaining proof |
|---|---|---|
| Direct Host process | server.py:16 imports transcript_import_cache then calls note_host_started; clip_store/viz_normalize imports at19-20, long before main | Minimal schema-check module must be stdlib-only and called in direct-execution bootstrap BEFORE16. Imported-server test/library path must remain inert; central conn gate covers actual store work, and transitive top-level call audit is still required. |
| Runtime | runtime.py:12 imports ServerContext after stdlib path setup | Direct-execution check before12, before context/dispatch/import-time effects. Never consume handoff/recovery marker or bind on refusal. |
| clarp-admin | Path setup at31, lib service_manager/xdg at32, command functions choose actual stores later | Do NOT globally reject no-DB diagnostics/backup/update on import. Gate each DB-using command before its first DB effect, plus central conn defense. Exact function exception/output mapping remains owed. |
| Plugin hooks | _clarp_lib imports establish path; agents/turn_lifecycle import precedes main | _clarp_lib must stay path-only; minimal gate at first actual DB operation and imports proven inert. Preserve existing hook catch/stdout/exit. No generic sys.exit1 replacing hook fallback. |
| Helpers agent_tasks/agent_bg and job scripts | lib stores imported near startup | Inert imports plus conn gate before durable operation; no schema-creating import. Actual command dispatch/error mapping still owed. |
| Stamp/cache | db._stamp_connection | Existing-file ro open, same descriptor check; no parent.mkdir or new DB file on cache request. No logging recursion on refusal. |

Static inspection found server/lib/__init__.py and persistence/__init__.py empty.
Current db module top-level calls allocate Path/thread locks/counters/type objects;
its imports include timing,xdg,clock. That is evidence for investigating a small
pure opening layer, NOT proof the full transitive startup import graph is inert.
Do not import db_schema/db_migrations to discover the expected121 shape: the schema
aggregates numerous feature imports and migration authority must stay unavailable
to the minimal check. Proposed pure descriptor/check module is a design name only,
not an implementation authored by this note.

Hook source demonstrates why exact mapping matters: stop_state resolves inside an
Exception catch which returns0, whereas several scripts have distinct outer
handlers/import fallbacks. A typed refusal must flow through each real owner without
DB-dependent diagnostics or unrelated prompt-context/provider-policy changes. The
full per-command/hook mapping is still an A2 HOLD, not inferred from a family label.

## Superseded custody discussion and current decision

The prior source discussion of current830 child/lock structure remains valid as
an observation, but a universal new custodian/supervisor is NOT required for R.
Lead937 preserves existing serialization/activation and names the old-binary race
as an OPEN residual. Release-owner addendum878aba and A2 no-regression boundary
supersede that earlier R hold. The full old discussion is in Git history.

This object manifest is DIAGNOSTIC evidence only. R does not install a whole-schema
allowlist or refuse additional currently supported121 shapes based on these two
fixtures. The exact R version/readiness/cache/error contract is specified in
pt02-r-lead-settled-scope.md; that document is authoritative.
