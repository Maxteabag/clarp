# Sub-agents

Status: proposed 2026-09-26. Decision owner: Peter. The minimum data model,
creation with `--parent`, the helper lifecycle and the snapshot fields are
implemented (schema v93, Host contract 11); `clarp-sub-agent start
--clarp-agent` creates helpers. The app rendering below is still to do.

## The question

When a Clarp agent needs a helper, should it use the harness's built-in
sub-agents (Claude Code's Agent/Task tool, Codex's `spawn_agent`), or spawn a
first-class Clarp agent that reports to it?

## What happens today

- **Harness sub-agents are used a lot and die a lot.** Since 2026-09-12,
  Clarp agents made 89 Claude `Agent` calls (15 agents) and 108 Codex
  `spawn_agent` calls (7 agents). 66 of the 69 Claude calls that set
  `run_in_background` asked for a background worker. That is exactly the kind
  that dies when Clarp restarts the parent session after a usage limit.
- **They are mostly invisible.** Codex sub-agents appear only as `subagents`
  cells in the transcript (`codex_transcript.py`), which the iOS activity card
  draws. Claude sub-agent transcripts under `<session>/subagents/agent-*.jsonl`
  are never read (121 files in the last 14 days). The desktop shows neither.
- **Clarp helpers exist but have no parent.** "Junior" agents are ordinary
  agents, and the `agents` table has no parent or role column. A fork's
  source is only logged. Of 8 juniors so far, one has a traceable parent, and
  5 were left to be soft-deleted by hand. They sit as equal peers in the chat
  list.
- **Reporting back already works.** `clarp-admin prompt --from` makes an
  agent-origin message, and `agent_conversations.py` builds the parent–helper
  pair chat from it.
- **Grouping already exists for teams.** iOS (`AgentTeam.overviewRows`) walks
  `parent_team_id` depth-first. The same walk
  works for agents keyed on a parent id.
- **An earlier design stops halfway.** claude-pwa PR #11 (open, design only,
  2026-08-26) defines a `ChildRun` entity for delegated runs. It deliberately
  says a child run is not a session, so it would not make helpers
  inspectable agents.

## Recommendation

Make long-running helpers first-class Clarp agents with a parent. Keep harness
sub-agents only for short read-only lookups whose answer is needed in the
current turn.

Why:

- **Survives restarts.** A Clarp agent has its own runtime row, restart
  heartbeats and transcript. A harness sub-agent dies with its parent.
- **Inspectable.** You can open the helper's chat, see its tool calls and
  steer it. This is the transparency argument.
- **Quota.** A helper can run on a model that is not at its limit.
- **Mostly built.** Reporting, pair chats and tree rendering already exist.

What the harness gives for free, and Clarp has to match: parallel fan-out
(many helpers from one call), an isolated context, a return value to the
caller, and cleanup when done.

## Minimum data model

- `agents.parent_agent_id TEXT NULL`: the creator. Set on create and fork,
  with `clarp-admin agent create --parent "$CLARP_SESSION"`.
- `agents.role TEXT NOT NULL DEFAULT 'agent'`: `agent`, `helper` (and later
  `janitor`, replacing `is_janitor`).
- `agents.helper_state`: `running`, `reported`, `done`, `failed`,
  `abandoned`, plus `completed_at`. A helper sets `reported` when its final
  message reaches the parent. The parent or user marks it `done`, and it
  archives itself after a grace period. If the parent is deleted, its helpers
  are flagged as orphans, not cascaded.
- Snapshot row: `parent_agent_id`, `role`, `helper_state`, `child_count`,
  `running_children`.

## How the apps show it

- Helpers nest under their parent in the chat list, iOS and desktop, reusing
  the team tree walk. They never sort to the top level. A "show helpers as
  peers" switch reuses the grouped/flat toggle.
- The parent row shows a small animated agent glyph and a count while any
  helper is running. This sits next to the existing blue hourglass, which
  means a background job.
- Finished helpers collapse into one line ("3 helpers done") that expands on
  tap. The parent–helper pair chat becomes the helper's report view.

## Vocabulary

Two words, used the same way in the snapshot, the apps and the skills
(Host contract 16):

- A **sub-agent** is a Clarp helper agent: `role = helper` with a
  `parent_agent_id`. It is counted from the parent's running children
  (`background_jobs.sub_agents`, equal to `running_children`).
- A **background process** is a durable background job that is not a helper
  mirror: a systemd worker (kind `worker`), a message watcher, a CI wait
  (`background_jobs.count`). `GET /background-jobs/<job_id>` shows its
  timeline, progress, owner and log tail.

`clarp-sub-agent start --clarp-agent` still registers a watcher job of kind
`sub-agent` whose detail is the helper's session. That job only mirrors the
helper, so it is not counted as a process; counting both made three helpers
read as "6 sub-agents running". The counts are the badge; the parent's
`status_text` describes activity instead of repeating them ("slice5-helper:
running tests", "3 working, 1 waiting", a process's progress line or title).
A job whose worker PID has exited fails at
once (`worker_vanished`), and the owning session can close its own job with
`clarp-agent-bg SESSION job-cancel HANDLE` even when the worker is gone.

## Keeping the working labels honest

An agent looks busy while it has a running helper, an active process or a
status line of its own, so any of those left behind keeps it blue for hours.
Two mechanisms retire them.

**Stale-work reconcile** (`policies/stale_work.py`, `stale_work.py`, and
`background_jobs.reconcile_stale`) is deterministic. The background-job
watcher applies the job rule on every pass and the other two once a minute.
Thresholds live in `[agents]`; 0 turns a rule off.

| Rule | Condition | Action |
|---|---|---|
| Job heartbeat | an agent's running job with no worker PID to verify, heartbeat and progress silent for `job_stale_after_minutes` (15) | timeline note `reconcile: heartbeat stale` |
| | still silent `job_heartbeat_grace_minutes` (15) later | fails with `heartbeat_lost` |
| Helper idle | `helper_state = running`, no turn running or queued, no process, no running helper of its own, for `helper_idle_after_minutes` (30) | moves to `reported` through `helper_agents.note_idle` (event `went_idle`); the parent still decides done or failed; noted on the mirror job |
| Status TTL | an agent's own status older than `custom_status_ttl_hours` (2), nothing counted and no turn | cleared through `agents.clear_stale_custom_status`; a declared `background` state settles to `idle` (event `background_expired`) |

It never touches a job whose worker PID is verified alive (the existing
`heartbeat_timeout_ms` rule still owns a wedged live worker), a
computer-owned job, an agent in a turn, or a Janitor-maintained label (that
has its own validity window). A status needs a write time to expire;
`agents.custom_status_at` (schema v96) records it, and statuses from before
the upgrade start their clock at the upgrade.

**Label checker** (`label_audit.py`, Janitor `label-auditor`) is judgment,
report only. Hourly, when the `labels` Jev site is on, it asks whether each
working label still describes what the agent is doing and lists the ones
that do not in one Updates item. A label that changed in the last 15
minutes is skipped, since the turn that set it may only just have ended.
It changes nothing; see
[janitor-autonomy.md](../janitor-autonomy.md#label-checker).

## Until then

- The `clarp-sub-agents` skill without `--clarp-agent` runs a detached
  worker as its own systemd unit and registers it as a background job of
  kind `worker`, with its streamed log registered for inspection. Any agent
  with a running helper or an active process shows as `background`. The
  apps already draw the running hourglass for that, and can switch to the
  agent glyph when `sub_agents > 0`.
- Harness sub-agents: Claude's `Agent`/`Task` calls now become the same
  `subagents` cell Codex gets, marked `ephemeral: true` ("dies with the
  turn") so it is clear which helpers survive a restart, and linked to the
  sub-agent's `subagents/agent-*.jsonl` transcript. Those transcripts are
  not imported as conversations. See "Display cells" in
  [docs/protocol.md](../protocol.md). The desktop renderer is still missing.
