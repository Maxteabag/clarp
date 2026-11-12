# Incremental transcript import (shadow mode)

Every watcher tick re-parses a whole transcript and compares every row with
the store, although a tick almost always only appends a few records. On
2026-10-11 the `transcript-inotify` thread alone used 347 s of CPU in a
1,500 s window (23 % of a core, about 830 CPU-s/h, read from
`/proc/<pid>/task/*/stat`), and the `/log` path's imports took about 740 s/h
of wall time on top. A full tick of the biggest transcripts costs 1.1 s
(Claude, 58 MB, 4,389 turns) to 2.4 s (Codex, 187 MB) of CPU; resuming from a
checkpoint at the last user turn costs about 5 ms on the same files.

`lib/transcript_incremental.py` holds that incremental import and runs it as a
**shadow only**. The full import stays the only thing that writes rows
clients see. On a sample of ticks the incremental result is computed next to
it without writing, compared, and logged. Nothing switches over on its own:
serving incremental results needs a separate review.

## How a tick runs

`import_transcript` is the import both the watcher (`transcript_streamer`)
and `/log` (`conversation`) call. With the shadow off, or not sampled, it is
exactly the old parse plus `store_transcript_turns`.

When the budget admits a shadow run:

1. The file identity is read, the full parse runs (authoritative), and the
   identity is read again. If the file grew meanwhile the run is skipped
   (`skipped:file_moved`): the full parse and a range parse would not have
   read the same bytes.
2. `decide` judges the stored checkpoint (below). If it allows an incremental
   import, the shadow parses only the bytes from the checkpoint's boundary,
   checks the turn count and the turns against the full parse, checks the
   turns before the boundary are unchanged, and plans the store of the tail
   with `plan_transcript_tail`: the import's own code over one read snapshot,
   with every write recorded instead of executed.
3. A new boundary is chosen for the next checkpoint (below).
4. The full store runs as always. Inside its last transaction it writes the
   new checkpoint, with the store's carried state captured at the boundary
   turn.
5. The planned rows are compared with the rows the full import produced:
   ids, text, origin, sender, timestamps, tools and cells, which rows got a
   new revision, which transcript rows changed and which rows were deleted.
6. One `transcript/import_shadow` event records the verdict and the cost.

Verdicts: `match`; `fallback:<reason>` (the incremental import would have
refused and done a full import, which is correct); `diverge:<what>` (it would
have produced different rows: a bug to investigate before any cutover);
`unchanged`; `skipped:file_moved`; `error:<step>` (the shadow failed; the
exception is logged as `transcriptShadowFail` and the full import goes on).

## The checkpoint (schema 117)

One row per `(agent_id, backend_session_id, source_file)` in
`transcript_import_checkpoints`:

| Column | Meaning |
| --- | --- |
| `backend`, `parser_version` | A hash of the parser sources and the store code; any edit refuses old checkpoints. |
| `device`, `inode`, `size`, `mtime_ns` | The file as the full parse read it. |
| `complete_size` | Bytes up to the last newline. A half-written record after it is read again next time, so the boundary and the hashes never cover it. |
| `boundary_offset`, `boundary_seq` | The byte offset and turn number of the last user turn. The next import re-parses from there, so the last turn, which may still be streaming, is always parsed again. |
| `turn_count` | The true total, so the stale-row delete past the last turn still works on a tail. |
| `prefix_hash` | The bytes before the boundary: the first 4 KiB, sixteen 4 KiB samples spread across it and the 64 KiB just before it. |
| `tail_hash` | Every complete byte after the boundary as it was then. |
| `prefix_turns_hash` | A digest of the parsed turns before the boundary (the shadow's check that they really did not change). |
| `parser_carry_json` | What the reference scan needs about the turns before the boundary: unfinished Claude tool ids, Claude sub-agent ids, the agy boundary step and whether an agy tool still runs. |
| `store_carry_json` | The store loop's state at the boundary: the client rows already linked, in order (the send-order provenance cursor), the heartbeat/leader/dreaming origin and heartbeat key, the request trace and retry key, the agy assistant ordinal, collected authority tools and closed authority traces, the adopted twin ids and the newest trace. |
| `imports_since_audit`, `audited_at` | For the periodic full import. |

It is written in the full import's final transaction, behind a savepoint, after
every row: a failure to write it is logged (`transcriptCheckpointWriteSkip`)
and never fails the import. When the probe pass found nothing to write, the
checkpoint upgrades the probe's read snapshot to a write, so it describes
exactly the rows the probe read; if another writer committed meanwhile SQLite
refuses and the checkpoint is skipped. Of two importers racing, a checkpoint
never replaces one of the same file that covers more of it.

Choosing a boundary: the latest candidates (a Claude user record, a Codex
`task_started`, an agy `USER_INPUT`) within the tail budget are tried newest
first. A candidate is taken only when the range from it, parsed alone,
reproduces the full parse's last turns exactly, begins with a user turn, and
refers to nothing before it.

## The frontier (schema 124)

A tail import needs more than the checkpoint: the digest of every imported
byte of the file, and the database rows the import read (every message row
of the agent, its turn rows, the session's `/send` rows, the latest user
row, its Oracle rows). Until schema 124 both lived in the Host's memory, so
every Host start made the first import of every conversation a full one, of
a file that had not changed.

`transcript_import_frontiers` holds one row beside each checkpoint, written
in the checkpoint's transaction: the parser version, a digest of that
checkpoint, the digest of the imported bytes and a digest of those rows. No
row is written for a session no frontier is kept for (Oracle history).

A session that holds an automation's `/send` row (a heartbeat, a goal wake or
schedule, a watcher event, a leader tick, a maintenance run) keeps a frontier
like any other. It used to keep none (`automation_provenance`), so every
import of it was a full one from its first heartbeat on. The reference lane
(`tests/readmodel/test_rm_n14_automation.py`) compares every product table
after every tick for each of those origins: the prompt and its reply arriving
by tail, the same words sent again, an automation that was sent and never
ran, a heartbeat accounted once, and a Host start.

A process that holds no frontier for a file takes the stored one when it
never had one (a Host start), lost it to the cache's size limit, or the last
import wrote nothing and could not publish (`checkpoint_not_written`: an
import with nothing to write publishes by upgrading its read snapshot, which
SQLite refuses once any other writer has committed; the frontier it started
from is still the last one published). The stored frontier is used only when
it names the stored checkpoint, the parser version is the current one and
the rows digest to the stored value. Then:

- the file is exactly the one the checkpoint describes (device, inode, size,
  mtime, complete to its last byte): nothing is imported
  (`transcriptTailDecision code=unchanged_file`);
- otherwise the import is a tail, under the checks below.

A rewritten transcript is found by: another inode or device; a size below
the imported bytes; an mtime that went back; the stored hashes of the head
(4 KiB), sixteen samples and the 64 KiB before the boundary; the hash of
every byte after the boundary; and the digest of all imported bytes. That
last one costs a read of the whole file. It is checked:

- at the first tail after a Host start (this process has not read the bytes);
- whenever the size is unchanged but the mtime moved;
- at every audit, which is a full import (`audit_every` imports or
  `audit_seconds`) and compares the bytes it reads with the stored digest;
- at every tail when `digest_every_tail = true` in `[transcript_shadow]`,
  which is how every tail worked before.

Otherwise a tail adds the new bytes to a running hash. What that leaves
open: an old record rewritten in place, same length, outside every sample,
together with an append, is imported as a tail and found at the next of the
moments above, not at once. Whenever a check finds the imported bytes or the
file's identity changed, one line names the file and the check
(`transcriptPrefixChanged file=… what=…`) and the import is full.

Rolling back to a 123 release: the schema guard refuses older code on the
newer database unless the rollback is asked for. With the override, the 123
release never reads the new table and imports as it always did; the rows in
it go stale and are replaced by the first import after the next upgrade
(they name a parser version and a checkpoint that will no longer match).

## When the import must be full

`decide` returns a reason for a full import when there is no checkpoint, the
parser version or backend differs, the inode or device changed (rotation or a
rewrite renamed into place), the file is shorter than the checkpoint's
complete bytes, the mtime went backwards, the audit is due
(`audit_every` imports or `audit_seconds`), the tail exceeds
`max_tail_bytes`, the sampled bytes before the boundary changed, the bytes
after it that were already parsed changed, or a record after the boundary
reaches before it:

- Claude: a tool result for a tool still unfinished before the boundary, an
  unnamed tool result while one is unfinished, a sub-agent notification for
  an Agent call before the boundary, or an assistant record whose parent is
  not in the tail;
- Codex: an output whose call was not made in the tail, or a record of a
  Codex turn that did not start in the tail;
- agy: a step numbered before the boundary (written out of order), or a
  background task ending while one started before the boundary still runs.

A checkpoint is also refused (`effect_bearing_prefix`, counted as
`bootstrap_effect`) while a turn before the boundary carries an effect that a
full replay could repeat or change: a Codex sub-agent cell or an Agent/Task
tool, a `<team>` block (capture follows the agent's current teams), or a
dream prompt or dream output (a replayed `DREAMING_OK` acts on whichever run
is active now). Heartbeat and leader-tick prompts and acks need no replay
once their row is stored (see `_prefix_effects`). Nor does a Claude sub-agent
cell: the one thing it reads outside the parent's bytes is which child
transcript its Agent call finds in `<parent>/subagents`, and the checkpoint
records that answer for every old cell (`subagent_children` in the parser
carry, found or not). A tail asks the directory again; a child that appeared,
went or was bound to another call refuses it
(`claude_subagent_children_changed`) and the full import rebuilds the cell.
A checkpoint over such settled effects is logged as
`transcriptTailRetention code=checkpoint_closed_effects`.

Residual risk: a same-length in-place edit that misses every sampled block
before the boundary is not detected by the hashes; the periodic audit (a full
import) repairs it, and in shadow mode the turn digest comparison reports it.

## Kill switch and budget

```toml
[transcript_shadow]
enabled = false                 # the default: no shadow, no checkpoints
sample_every = 20               # at most one shadow run per 20 imports
max_cpu_seconds_per_hour = 30.0 # CPU the shadow may spend per hour
max_load_per_cpu = 0.75         # skip when load1 / CPUs is above this
max_io_pressure = 10.0          # skip when /proc/pressure/io some avg10 is above this
max_memory_pressure = 10.0      # the same for memory
max_tail_bytes = 16777216       # tails larger than this mean a full import
audit_every = 50                # imports between forced full imports
audit_seconds = 3600.0
```

`CLARP_TRANSCRIPT_SHADOW=0` in the Host's environment turns it off whatever
the file says. The config is re-read about once a second, so `enabled` takes
effect without a restart. Never two shadow runs at once: a second one is
skipped (`concurrent`). Every `import_shadow` event carries
`shadow_cpu_ms`/`shadow_wall_ms` for the run, the hour's totals so far and the
skip counts since the last run.

Measured on copies of the three largest live transcripts (2026-10-11, scratch
database, niced): one shadow run costs 136 ms (Claude, 58 MB), 162 ms
(Claude, 63 MB) and 145 ms (Codex, 187 MB) of CPU, mostly the per-turn digests
and the boundary choice; all three verdicts were `match`. At one run per 20
of about 650 imports an hour that is about 5 CPU-s/h, capped at 30.

## Rollout

1. Deploy with the shadow off (the default). Schema 117 only adds the table;
   older code ignores it, so a rollback needs no down-migration.
2. Turn it on with `enabled = true` and watch `import_shadow` events: the
   share of `match`, the fallback reasons, every `diverge`, and the cost.
3. Cutover (serving the incremental result) is a separate, reviewed change.
