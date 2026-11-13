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
byte of the file, and the database rows the import read (the session's
message rows, or with `carry_session_rows` off every message row of the
agent; the agent's turn rows; the session's `/send` rows; the latest user
row; the agent's Oracle rows). Until schema 124 both lived in the Host's memory, so
every Host start made the first import of every conversation a full one, of
a file that had not changed.

`transcript_import_frontiers` holds one row beside each checkpoint, written
in the checkpoint's transaction: the parser version, a digest of that
checkpoint, the digest of the imported bytes and a record of those rows. No
row is written for a session no frontier is kept for (Oracle history).

The record (`lib/message_carry.py`, `record`) is what a frontier keeps of the
rows, in the process and in the store alike. It is not the rows: a
conversation of 24,000 message rows weighed 45 MiB as rows, more than the
32 MiB the process's cache has for all frontiers together, so it kept none
and every append was a full import; two conversations of 11,000 rows pushed
each other out. The record is about a kilobyte for a conversation of any
size:

- the mark: the highest revision among the message rows;
- digests per kind of message row (`/send`, live, final, transcript, other,
  another session's), over every column of every row of that kind;
- three digests of the turn rows: all of them, those that began up to two
  seconds after the last turn before the boundary, and how the turn of each
  trace on a recorded row ended;
- a digest of the latest user row and one of the Oracle rows.
- a digest of each live row of the session by itself (there are few), for
  `carry_live_rows`.

The digests are taken in a fixed order that SQLite has no say in: the
columns by name, the rows by message id. At a tail the rows are read again
and split by the mark. Those at or below it must hash to the recorded
digests, which proves every old row is there and unchanged in every column.
The revision is not trusted for that: an old row edited without a new
revision changes its digest; one edited with a new revision, or removed, is
missing from it; a row that appears with an old revision lands in a digest
and breaks it. The rows above the mark are then exactly the new ones, and
the rules below judge them. What the rules need of the old rows (the words
said before, the traces) is read from the rows at or below the mark, once
the digests have shown them to be the recorded ones.

The rows at or below the mark are hashed in two parts, split at a second,
lower revision (the base): the rows up to it, nearly all of a long
conversation, and the few written since. A tail that leaves the first part
untouched publishes its record with that part's digests as they are and
hashes only the second, so a tail hashes the conversation once, not twice.
Untouched is not assumed: the rows the guard hashed are compared, value for
value, with the rows read at publication; a full import, or a tail that
changed a row of the first part, hashes everything and moves the base up to
the mark.

What a tail still costs: the session's message rows are read from SQLite
three times (by the probe pass, again under the write lock, and at
publication) and hashed once. On a conversation of 23,870 rows that is 80 MB
read and 0.36 s of CPU a tail, about 80 ms of it the hashing; on 10,855 rows
31 MB and 0.15 s. The record does not lower that floor. Reading only the
rows above the mark would, by trusting that every change to a row gives it a
new revision; the record is built so that nothing rests on that today.

The record is stored in the `inputs_digest` column as a short JSON text.
Until it existed that column held one digest of all the rows. Neither is
ever taken for the other: the parser version is checked first and differs
between the releases, and a text that is not a record is no frontier.

A session that holds an automation's `/send` row (a heartbeat, a goal wake or
schedule, a watcher event, a leader tick, a maintenance run) keeps a frontier
like any other. It used to keep none (`automation_provenance`), so every
import of it was a full one from its first heartbeat on. The reference lane
(`tests/readmodel/test_rm_n14_automation.py`) compares every product table
after every tick for each of those origins: the prompt and its reply arriving
by tail, the same words sent again, an automation that was sent and never
ran, a heartbeat accounted once, and a Host start.

### Which rows a tail may find changed

A tail leaves the turns before its boundary alone. That equals a full
replay only while a full replay would leave them alone too and reach the
boundary with the same carried state, so a tail is refused
(`full_carry_inputs_changed`) when a row those turns could read has changed
since the frontier was published. Until November 2026 "could read" was every
row above, identical; during a running turn about every second import was a
full one. `lib/message_carry.py` names three kinds of row the old turns
cannot read. Each is a switch in `[transcript_shadow]`, on by default; off,
that kind of change is a full import again.

- `carry_session_rows`: message rows of the agent's other sessions. The
  replay of one transcript reads message rows by this session, by a row id of
  this session, or by the trace of a row it has just changed. The turn rows
  stay agent-wide.
- `carry_later_turns`: turn rows that began more than two seconds after the
  last turn before the boundary (a turn counts as running from two seconds
  before it began), unless the turn's trace is on a row the session already
  held: a reply's trace and its tools' final status go by that. The
  checkpoint's store carry records the time (`prefix_latest_ms`).
- `carry_new_sends`: a new `/send` row whose words no turn before the
  boundary said, by the linker's own key: the text of every old turn stored
  as a transcript row and of every `/send` row an old turn was linked to. A
  row that begins with the delivery notice always counts as said (it decides
  whether an old copy keeps its notice). The latest user row changes with
  such a row; it counts only when replies before the boundary read it (a
  transcript that opens with a call opening), which the store carry records
  (`initial_provenance_used`).

A fourth rule is off unless switched on:

- `carry_live_rows`: a live row of this session, the row a reply streams
  into. It is a different kind of rule: the runtime rewrites that row in
  place with every delta (four times a second while a reply streams), so
  what the rule lets through is a CHANGED old row, not only a new one. By
  day on an active Host this was nearly every refusal left (all 137 in one
  hour of 13 November named `row_live`). What a turn before the boundary
  reads of a live row is one thing: a full import removes a leftover live
  row when a reply of the row's own trace with related words is in its
  batch, and its batch holds every reply where a tail's holds those from the
  boundary. So a live row may be new, rewritten or gone unless a reply
  stored before the boundary carries its trace (the running turn then spans
  the boundary: a message was steered into it), whatever its words. A live
  row that is as it was when the frontier was published is not asked; the
  record keeps a digest of each live row for that. A finalized row
  (`final:`) is not a live row and counts as before. The rule does not
  change the publication fence: a delta committed while a tail has given
  the writer away still refuses that tail's publication.

What stays a full import on purpose: any other change to a row of this
session (a finalized row, a turn's rows re-stamped when it settles, and
without `carry_live_rows` a streamed reply's live row), a new `/send` row
with words said before (every heartbeat, once), and the Oracle rows. A refusal logs which rows stood in the
way, as fixed words: `transcriptCarryInputsChanged what=row_live,turn_of_old_row`
(`row_send`, `row_live`, `row_final`, `row_transcript`, `row_other`,
`row_other_session`, `send_said_before`, `live_row_of_old_reply`, `turn_rows`,
`turn_before_boundary`, `turn_of_old_row`, `latest_user_row`, `oracle_rows`). The rules apply to a
frontier the process holds and to one taken from the store alike, so also
after a Host start and after the cache has dropped the frontier: both are
the same record.

`tests/readmodel/test_rm_n14_carry.py` and `test_rm_n14_live_rows.py` run
every rule in pairs: the change it lets through is a tail and equals the full
replay; its nearest harmful twin is a full import and equals it too, and
imported as a tail leaves different rows.

A process that holds no frontier for a file takes the stored one when it
never had one (a Host start), lost it to the cache's size limit, or the last
import wrote nothing and could not publish (`checkpoint_not_written`: an
import with nothing to write publishes by upgrading its read snapshot, which
SQLite refuses once any other writer has committed; the frontier it started
from is still the last one published). The stored frontier is used only when
it names the stored checkpoint, the parser version is the current one and it
holds a record. Then:

- the file is exactly the one the checkpoint describes (device, inode, size,
  mtime, complete to its last byte), every recorded row stands with no rule
  applied, and a tail from that checkpoint would be allowed: nothing is
  imported (`transcriptTailDecision code=unchanged_file`). (With a row that a
  rule lets through, the tail runs: such a row can be for a turn after the
  boundary.)
  The second half matters. Every check a tail passes is made first, so an
  unchanged file is imported in full when a sub-agent child appeared or went
  while the parent was idle (`full_claude_subagent_children_changed`). When
  its audit is due its bytes are read once and compared with the stored
  digest (`unchanged_file_audited`): the rows it was imported with are
  proven unchanged already, so there is nothing to replay. A mismatch is a
  full import with the `transcriptPrefixChanged` line. Nothing is written,
  so this is not the audit a grown file gets (that stays a full import and
  starts the clock again), and each Host start after the hour reads the
  file once. `audit_unchanged_by_digest = false` makes it a full import
  (`full_audit`) instead;
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
moments above, not at once. A Host start by itself is not one of them: after
it an unchanged file is not read at all, and a file that was rewritten and
then went idle keeps its old rows across any number of starts while its
audit is not due. The bound is the audit: an unchanged file is left alone
only until `audit_seconds` have passed since its last full import (or it has
had `audit_every` imports); the first import after that, the first time the
conversation is opened after a Host start for instance, reads every byte
and compares. Within one process a file whose size and mtime do not
change is not handed to the importer again, so there the next growth, touch
or Host start is what brings the audit. A rewrite that keeps the inode, the
size and the mtime is found the same way, by the audit. Whenever a check
finds the imported bytes or the file's identity changed, one line names the
file and the check (`transcriptPrefixChanged file=… what=…`) and the import
is full.

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
