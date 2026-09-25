# Startup and sidebar latency

What a cold desktop start asked the Host for on 2026-09-25 (request telemetry,
`~/.local/share/clarp/telemetry.sqlite`):

| Request | Host time | Payload | Notes |
| --- | --- | --- | --- |
| `/agents/snapshot` | 280–375 ms, up to 1.2 s under CPU load | 36–240 KB | Called twice within a second at start |
| `/agent-conversations` | 400–1000 ms | 13 KB | One SQL statement over ~100k pair rows |
| `/artifacts?limit=50` | 82 ms | 149 KB | |
| 31 avatar PNGs | 1–6 ms each | ~380 KB each, 11 MB total | Re-downloaded and re-decoded on every start |

The sidebar's agents and contacts come from the snapshot, and the pair section
from `/agent-conversations`, so those two handlers set how long the sidebar
stays empty. The avatar PNGs were decoded from 1024 px and re-encoded as
192 px circles on the GUI thread, which stalled the first paint after the
list appeared.

Changes made:

- `agent_conversations.list_conversations` memoises its result per write
  generation (`db.write_generation()`, bumped on every INSERT/UPDATE/DELETE the
  Host executes). Between writes every poll answers from memory; the 0.4 s scan
  runs at most once per change.
- `message_previews.dashboard_messages` finds each agent's newest user-origin
  activity through `idx_messages_dashboard_activity` with a per-agent `LIMIT 1`
  instead of grouping every message (35 ms to 11 ms on a 140k-row store).
- The desktop decodes portraits on the thread pool and caches the rounded PNG
  under the user cache directory (`portraits/<sha1(host+url)>.png`). A later
  start serves them from disk without a request; the versioned `?v=` URL keys
  the cache, so a changed portrait is fetched again.

Not changed: the snapshot's 50-candidates-per-agent window query (47 ms warm)
and its Python formatting, and the per-token `/log` delta polls described in
`memory-diagnostics.md`.

## Second pass: the snapshot storm

After the first deploy, request telemetry showed `/agents/snapshot` served up
to 80 times a minute at 370–550 ms each, 60 % of it in two window-function
queries that ranked every message of every live agent. Two changes:

- `message_previews.dashboard_messages` now reads each agent's message
  revisions first (one indexed GROUP BY) and keeps a per-agent cache keyed by
  them. An unchanged agent costs nothing; a changed one runs three index walks
  through `idx_messages_dashboard_activity` that stop after at most 50 rows
  (65 ms for all 102 agents cold, versus 200–450 ms before).
- The desktop coalesces snapshot requests closer together than 700 ms into one
  trailing request, so a burst of roster events during streaming produces one
  fetch instead of a fetch per event.
