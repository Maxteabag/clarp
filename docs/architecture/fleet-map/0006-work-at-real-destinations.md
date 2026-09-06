# 0006 — Actions happen at their real destinations

Status: Accepted

Date: 2026-09-06

Supersedes: Generic Reading room, Revision press, Engine house and other action-category destinations in World.

## Context

Peter proposed moving an agent to the file or repository it is actually working
on and showing the activity there. He approved the implementation plan. Separate
rooms for reading or editing obscure where the work occurs and collect unrelated
agents at generic stations.

## Decision

Avatars visit real files, directories, checkouts and integration targets. Show the
action beside the avatar and on the target. Commits occur at the local checkout;
pushes retain the avatar there and depict a transfer toward a verified/configured
remote. Do not imply that a generic GitHub relationship proves the remote used by
an explicitly different push command.

Distinguish running, completed, failed and merely observed work. Create/delete
completion effects require an evidenced result. If several files are affected,
show their common scope and highlight the actual targets. Repeated operations
should not restart travel or create frantic hopping.

A known execution workspace is not proof of a specific file. Keep that distinction
in labels. Missing locations remain visibly unknown, never inferred from the
agent's previous position or mutable current directory. Native invocation context
may be shown while the actual tool destination is pending, with that qualification.

## Consequences

Prefer complete native tool records bound to the agent's recorded runtime and
backend session, with full command, parsed targets, execution-time cwd and
start/end timestamps. Read them incrementally and do not retain tool output.
Legacy records remain a fallback where complete native records are unavailable.
The shared Codex runner's unrelated dirty edits are preserved. Hooks can record
explicit cwd and call IDs prospectively without a schema migration.

Preserve avatars, the workshop/harbor language, pan/zoom, and Agent cabinets as
the complementary overview. The expanded world may exceed the viewport. Start
near an active workspace; Fit world can show the larger extent. Verify both real
history and clearly labeled lifecycle scenarios, including failures and unknowns.
