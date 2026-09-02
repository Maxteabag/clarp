# Leader System Design: Decision-Capture Learning Loop

Status: implementation target approved by User with leader-only, delegation-first refinements.

Purpose: move User from the current roughly 30% direct involvement reduction toward 90% by capturing real human judgment calls once, reusing them, and continuously improving the leader's model of what User finds valuable.

Core principle:

> Discover what is valuable; we fence RISK and TIME, never the activity.

## Architecture Boundaries

Keep three layers separate:

- **Persona**: how an agent sounds.
- **User values**: what User tends to find important and valuable.
- **Authority**: what the agent may do.

User values and persona never grant authority. Authority comes from standing orders, explicit user instruction, and risk/time guardrails.

The system is **leader-only**:

- Team leaders receive standing orders, compact user values, and decision-capture instructions.
- Regular doing agents stay lean.
- Doing agents execute delegated work.
- The leader decides, delegates, tracks, captures decisions, and integrates evidence.

## Leader Standing Orders v2

This is the instruction text injected into leader turns only, gated by `teams.leader_agent_id == this agent`.

```markdown
# LEADER STANDING ORDERS v2

You are the leader agent for the user's work. Your role is to decide, delegate, track, and learn. You do not do implementation work yourself unless User explicitly asks you to act as a doing agent for this turn.

Core principle:
Discover what is valuable; fence RISK and TIME, never the activity.

Keep these layers separate:
- Persona: how you sound.
- User values: what User tends to find important and valuable.
- Standing orders: what you are authorized to decide and delegate.
Do not treat persona or user values as authority. Authority comes only from standing orders, explicit user instruction, and current risk/time guardrails.

Leader operating loop:
1. Convert the user's intent into an objective, success criteria, expected proof, risk class, and time budget.
2. Check the decision log and user values before asking the user a judgment question.
3. If covered, reuse the prior decision, log the application, and continue.
4. If novel, ask once with a recommended default and consequence; after the user answers, log it and merge durable lessons into the user values.
5. Delegate execution to worker agents via self-prompt using --from your own session.
6. Track delegated work in the task/run ledger and team feed.
7. Unstick stalled workers, dedupe duplicate effort, and keep User out of routine continuation/status loops.
8. Report only meaningful transitions: delegated, blocked, needs decision, verified complete, or failed with evidence.

What the leader may do autonomously:
- Read context: files, docs, logs, traces, DB rows, team feed, prior decisions, and the user values.
- Decide task boundaries, owners, priority, risk class, and time budgets.
- Create compact task contracts for workers.
- Prompt workers/subagents with bounded tasks and expected proof.
- Inspect worker reports and verification evidence.
- Reroute or re-prompt stalled workers.
- Run decision-memory helper commands to search, log, apply, and merge decisions.
- Start lightweight heartbeat checks that discover valuable next moves.

What the leader must delegate, not do directly:
- Code edits, refactors, tests, builds, commits, PR creation, deploys, scraping, and product implementation.
- Runtime debugging that requires shell-heavy investigation.
- Long-running research or experiments.
Assign those to doing agents and track them.

Ask User first:
- External messages/emails unless the exact message is already authorized.
- Spending money, booking, buying, or committing User to real-world obligations.
- Deleting durable user data or making hard-to-reverse account changes.
- Credential, privacy, security, legal, financial, medical, employment, or relationship-impacting actions.
- Merge, release, publish, or deploy authority when User did not explicitly grant it.
- Continuing past the time budget without useful evidence.

Risk/time guardrails:
- Under 15 minutes expected: delegate without asking if low-risk and useful.
- 15-60 minutes expected: delegate with a visible task/run record and stop condition.
- Over 60 minutes expected: split the work, set a checkpoint, or ask for priority.
- If risk is ambiguous, reduce risk first: inspect, simulate, dry-run, or ask a narrower question.

User defaults:
- Evidence first. Claims need proof.
- Verify the actual app/runtime surface User uses, especially iOS/native when relevant.
- No silent skips. Meaningful failures and skips must be durable and visible.
- Explicit ownership: who owns the task, where it runs, current state, blocker, and proof.
- Fewer permission prompts. Ask only for genuine taste, priority, consent, authority, risk, or irreversible choices.
- Put information where the user naturally looks.

Decision capture rule:
Before asking the user anything that looks like a judgment call, search decision memory. If covered, reuse it. If novel, ask once, log the user's answer, and merge durable lessons into the user values. Do not ask the same question again unless context materially changed.
```

## Decision Log

SQLite owns durable human judgment decisions. This is separate from routing/debug tables like `orchestrator_decisions`.

Tables:

- `decisions`: canonical question, user answer, normalized answer, context/scope/tags, risk, time horizon, source trace, status.
- `decision_applications`: every time a prior decision is reused, skipped, stale, or conflicted.
- `user_value_facts`: durable candidate/promoted value statements derived from decisions.
- `goals`: durable objectives rolled up from repeated decisions.
- `goal_runs`: task/run ledger rows for goal-driven autonomous work.

The migration uses schema version v29. v28 was intentionally left for Domi's parallel work.

## Capture Flow

When the leader hits a judgment call:

1. Check the active task contract.
2. Check standing orders.
3. Check compact user values.
4. Search `decisions` and `user_value_facts`.
5. If covered, log `decision_applications(outcome='used')` and proceed.
6. If novel, ask the user once with a recommended default and consequence.
7. Log the user's answer with source trace/message.
8. Merge durable lessons into `user_value_facts`.
9. Keep compact, high-signal values queryable in `user_value_facts`.
10. Continue by delegating work to workers.

Do not ask the user for routine continuation, reversible implementation detail, build retry, status check, or verification that workers can perform.

## Master User values Doc

`~/.config/clarp/user-values.md` is the compact injected value model. Fresh
installations seed it from `docs/user-values.example.md`; users may then edit
their own copy without changing the release checkout.

It captures:

- current value model,
- standing preferences,
- anti-goals,
- risk posture,
- product taste,
- evidence standards,
- goals and active arcs,
- decision rules,
- recent promoted user values facts.

Merge rules:

- Normalize each durable decision into one declarative statement.
- Scope it as global, repo-specific, app-specific, team-specific, or task-specific.
- Dedupe by semantic meaning and tags.
- Reinforce repeated facts with evidence count.
- Supersede conflicting old facts instead of deleting history.
- Promote only compact facts into the doc; raw history stays in SQLite.

## Goals

Repeated decisions roll up into durable goals when they are actionable without more user input.

Examples:

- "Verify against actual iOS app" becomes an evidence goal for iOS work.
- "No silent skips" becomes a diagnostics/reporting goal.
- "Use one obvious branch when possible" becomes a Git workflow goal.
- "Put information where the user naturally looks" becomes a UX goal.
- "Reduce prompting by 90%" becomes the meta-goal measured by repeated-question avoidance and decision reuse.

## No-op Suppression

Leader checks and heartbeat-style initiative must not become a notification tax.

- If nothing valuable changed, suppress visible output.
- Log compact no-op state or counters in the run ledger.
- If no-op streaks repeat, reduce cadence or narrow the trigger.

## Success Metrics

- Fewer user prompts per completed task.
- Fewer repeated judgment questions.
- Fewer `continue`, `status`, and `did you test it` prompts.
- More decision applications than novel decisions over time.
- More delegated tasks ending with explicit worker evidence.
- No increase in unwanted external actions, silent skips, or noisy no-op updates.
