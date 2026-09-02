---
name: clarp-tasks
description: Track substantial multi-step work as a durable, user-visible Clarp task plan.
---

# Clarp Tasks

Use `clarp-agent-tasks` when work has
three or more meaningful steps or will likely take more than a few minutes.
Do not create a plan for short answers or one-step edits.

Create one detailed plan before substantial work. By default, use roughly 5–8
meaningful outcome steps so the user can understand the full intended approach
before implementation starts. Use stable lowercase IDs and at most two levels:
tasks and subtasks. Prefer explicit steps for discovery/evidence, ownership and
scope boundaries, each independently meaningful implementation slice,
verification/review, and integration or handoff. State what will change and
what will remain untouched when that distinction matters. Do not pad the plan
with routine tool calls or split genuinely small work into artificial steps.

```bash
clarp-agent-tasks create SESSION PLAN_ID "Outcome" '[{"id":"inspect","title":"Inspect current behavior and evidence"},{"id":"boundaries","title":"Confirm ownership, scope, and preserved behavior"},{"id":"contract","title":"Define the implementation contract"},{"id":"implement","title":"Implement the change","subtasks":[{"id":"server","title":"Update server behavior"},{"id":"ios","title":"Update iOS presentation"}]},{"id":"regressions","title":"Add focused regression coverage"},{"id":"verify","title":"Run focused checks and review"},{"id":"integrate","title":"Rebase, inspect scope, and hand off"}]'
```

Use the `plan_id` returned by `create` in every later update. The supplied
PLAN_ID is only a readable stable alias; the server namespaces each run.

Mark exactly one concrete item `in_progress` when possible. Update only at real
transitions; the server measures elapsed active time from these transitions.

```bash
clarp-agent-tasks update RETURNED_PLAN_ID inspect in_progress
clarp-agent-tasks update RETURNED_PLAN_ID inspect completed
clarp-agent-tasks update RETURNED_PLAN_ID server blocked "Waiting for credentials"
```

Completing every item completes the plan automatically. Use `finish` only for
an explicit blocked or cancelled plan, or when closing a plan without items.
