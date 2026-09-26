# Background processes and sub-agents

An agent can keep working after its turn ends: a background job (a watcher, a
long build) or a sub-agent. The desktop shows that work in three places.

## Indicators

The agent row and the conversation header draw one mark next to the time:

- **Blue hourglass**: the agent is waiting on background jobs.
- **Animated agent glyph**: a sub-agent is running, either a background job of
  kind `sub-agent` or a child helper whose `helper_state` is `running`. It
  replaces the hourglass and stops moving under reduced motion.
- **Count badge**: the number of running processes, once there is more than
  one (jobs plus running helpers).

Clicking the mark opens the process list: one line per active job (kind, title,
how long it has run, last heartbeat) and one per running helper. Choosing a
helper opens its conversation. Escape or a click outside closes the list.

Counts come from the snapshot's `background_jobs` until `GET /background-jobs`
has loaded; from then on the live job list wins. `background-job-updated`
applies the event's job at once and then refetches the list, and
`agent-roster` refetches the snapshot.

## Helpers in the agent list

Agents with `role: "helper"` and a `parent_agent_id` nest under their parent,
using the same depth-first walk as the Teams hierarchy (`TreeOrder.h`). A
helper whose parent is not in the list stays at the top level rather than
disappearing. Finished helpers (`done`, `reported`, `abandoned`) collapse into
one "N helpers done" line under the parent; clicking it expands them. Failed
helpers stay visible. Searching or the Unread filter shows a flat list that
includes finished helpers. A Host without these fields shows the list exactly
as before.

## Sub-agent cells in the transcript

`subagents` display cells (Codex `spawn_agent`/`wait_agent`, and Claude
sub-agents once the Host emits them) render like other tool cells, with the
agent glyph, a phase (`spawned`, `waiting`, `finished`, `failed`), the
sub-agent's name and its task. The phase is derived in `describeSubagentCell`
and added to the cell as `_subagent` when the transcript is read, so the cache
keeps the Host's shape.

## Checking it

`CLARP_SCREENSHOT_SCENARIO=background-processes` seeds agents with 0, 1 and 3
processes, a nested running helper, two collapsed finished helpers and a
transcript with sub-agent cells. `CLARP_SCREENSHOT_SELECT_SESSION=atlas|beacon|nova`
picks the chat and `CLARP_SCREENSHOT_OPEN_PROCESSES=header|sidebar` opens the
process list. The `clarp-desktop-background-processes` CTest runs it offline.
