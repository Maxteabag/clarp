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
- **Grouping already exists for teams.** iOS (`AgentTeam.overviewRows`) and
  desktop (`TeamsPanel.qml`) walk `parent_team_id` depth-first. The same walk
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

## Until then

- The `clarp-sub-agents` skill runs a detached sub-agent as its own systemd
  unit and registers it as a background job of kind `sub-agent`. The snapshot
  now reports `background_jobs.count` and `background_jobs.sub_agents`, and
  shows any agent with an active job as `background`. The apps already draw
  the running hourglass for that, and can switch to the agent glyph when
  `sub_agents > 0`.
- Harness sub-agents: Claude's `Agent`/`Task` calls now become the same
  `subagents` cell Codex gets, marked `ephemeral: true` ("dies with the
  turn") so it is clear which helpers survive a restart, and linked to the
  sub-agent's `subagents/agent-*.jsonl` transcript. Those transcripts are
  not imported as conversations. See "Display cells" in
  [docs/protocol.md](../protocol.md). The desktop renderer is still missing.
