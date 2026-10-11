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
