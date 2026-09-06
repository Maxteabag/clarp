# Stage three evidence map — one route across systems

Working design aid (Mappy, 2026-09-06). It names the real workflow the map must
make legible, and separates what is recorded, what is attributed, and what is
missing. Nothing below is invented; every row points at a Clarp record.

## The route: Paper Cuts Man ships native HTML form artifacts

Plan `ee25562a20f43eb9:html-forms-release:652f0cd3` (active, started 20:47).
Replay window: `/viz?view=flow&window=7200&until=<now>` while it is active.

| Boundary crossed | Record | Relationship type | Basis |
| --- | --- | --- | --- |
| Workshop (paper-cuts-documents worktree) | task plan + native Codex edits, tests | making, checking | attributed: same agent inside the plan window |
| Theo (another agent) | `prompt_admissions` agent-origin messages both ways, incl. Theo's "stop sending progress" | observed collaboration (thread with knot) | explicit record; not a transfer, no plan ID named |
| GitHub (remote) | push events with a configured origin; `workflow_run` artifacts | delivery; remote checks | explicit push record; conclusions only once evidenced |
| Host update (global Clarp server) | background job `host-update-10e53d0c54c4`, kind server-update, succeeded | wait on the Host boundary → released | explicit job record |
| Mac test lane | jobs `papercuts-html-form-*-watch`, kind external_test_watch, succeeded | waits on a test lane → released | explicit job records |
| GitHub Actions verification | jobs `html-form-testflight-3404…`, kind github-workflow, one succeeded, one running | wait → released; wait still open | explicit job records; running = waiting, never inferred from silence |
| Owner | pending decisions (`artifact_decisions`) when present | wait on the owner | explicit record with blocks_progress |

What is **not** recorded and therefore not drawn: that Theo's merge caused the
Host update (only their order in time is known); which GitHub run a job watched
unless its metadata carries `run_url`; that a released wait deployed anything
beyond what its own record says. A push never draws a deployment.

## The honest counterexample: Theo's TestFlight release wait expires

Job `janitors-testflight-34040836952` (kind release → TestFlight) failed with
`heartbeat_expired` after 15 minutes. The map shows a mooring to the TestFlight
post that frays and a hollow ring with a slash: the wait ended without evidence
of the release. Nothing claims the release failed or succeeded.

## Waiting versus quiet versus history

- **Waiting** is only a recorded open job or pending decision: a mooring line
  with a slow pulse to a hollow, turning ring at the boundary post. A stale
  heartbeat dashes the line but is still not a failure.
- **Quiet** is the absence of records: nothing is drawn, lanterns dim, avatars
  sit still. Silence is never a blocker.
- **History** is browser memory of repeated observed interactions (deliveries
  to a remote, messages between two agents, waits on a boundary), drawn as a
  still dotted route only when seen at least twice, bounded to 200 routes and
  labeled as a pattern, not a dependency.

## Smallest useful instrumentation gaps

- Jobs carry no `run_url` unless the registering skill sets metadata; the map
  offers an Open run link only when present. Suggest skills record it.
- Host updates and service restarts are recorded as jobs or compound shell
  operations; a deployment artifact type exists but is rarely written. When it
  is, it will appear as a lantern outcome and a release at the Host post.
