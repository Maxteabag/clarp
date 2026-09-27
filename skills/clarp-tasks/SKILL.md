---
name: clarp-tasks
description: Compatibility route for Clarp task plans. Use clarp-goal for durable outcomes, adaptive planning, evidence and continuation.
---

# Clarp Tasks

For substantial authorized work, use [clarp-goal](../clarp-goal/SKILL.md).
Goals contain the outcome, acceptance criteria, limits and continuation; plans
are their editable working strategy. There is one persisted task-plan artifact,
not a second planner. Choose a useful strategy without a prescribed phase order.

Older Hosts still support:

```sh
clarp-agent-tasks create SESSION ALIAS "Outcome" '[{"id":"probe","title":"Probe current behavior"}]'
clarp-agent-tasks update RETURNED_PLAN_ID probe in_progress
clarp-agent-tasks update RETURNED_PLAN_ID probe completed "Verified result"
clarp-agent-tasks show SESSION
```

Keep the returned namespaced ID. Old checklists have no recovery guarantee:
use the supported self-prompt/background-job helpers for unfinished commitments.
Never count skipped work as accomplished or complete with pending required work.
New Hosts preserve multiple unfinished commitments; pause/cancel/supersede only
explicitly. Interactive HTML planning forms remain proposal/review surfaces,
not wake schedulers or automatic execution authorization.
