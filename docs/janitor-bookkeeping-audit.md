# Agent bookkeeping that Janitors could own

Status: investigation, September 2026. No code changes.

Goal: a task agent should be able to work inside Clarp knowing almost nothing
about Clarp. Everything the app needs in order to render tasks, jobs, decisions,
artifacts, team feeds and labels should be observed by the Host or produced by a
Janitor from bounded evidence. This audit lists what the Host currently asks
agents to do for it, sorts that into what needs no model at all, what a Janitor
should own, and what genuinely belongs to the working agent.

`docs/janitor-demand-workers.md` already states the principle: "No worker agent
has to report extra bookkeeping." Today that holds only for routing and tool
explanations. This document applies it to the rest.

## 1. Where agents currently receive Clarp instructions

| Surface | Injected when | What it asks for |
|---|---|---|
| `codex_runner.py:171` persona identity | every app turn, all backends | "Use the installed clarp-background-jobs skill for work that continues after your final response" |
| `_NO_INTERACTIVE_QUESTIONS` in `codex_runner.py:73` and duplicated in `plugin/hooks/pwa_source_flag.py:218` | every app turn | never use CLI question tools; use the clarp-decisions helper for questions and approvals; never self-resolve |
| `_SPEAK_INSTRUCTIONS` / `_VOICE_INSTRUCTION` / `_NATURAL_SPEECH` | voice turns | `<speak>` gating, first-line spoken ack before any tool call, split long summaries into ~1 minute blocks, fillers in `<vox>`, breaks, speed |
| `team_store.py:355-450` team wrappers | turns of agents on teams | post `<team>` updates on start/finish/blocker/handoff; report terminal results to the leader via self-prompt `--from`; leaders track owner/objective/proof/risk/time |
| `heartbeat.py:25` | idle heartbeat | read HEARTBEAT.md, review the durable plan, "audit durable background jobs and update stale ones", reply HEARTBEAT_OK |
| `server.py:4314` job-cancelled prompt | user cancels a job | run `clarp-agent-bg job-cancelled` and verify worker identity before stopping anything |
| 33 managed skills symlinked into `~/.claude/skills` and `~/.codex/skills` (`managed_skills.py`) | always; the core pack cannot be disabled | see section 2 |
| Repo `AGENTS.md` "Push to main" | every turn in this repo | once the relevant gates pass, commit and push to main directly; never leave the turn's work uncommitted |

The Host already observes a lot without asking: hook sensors write thinking/tool/
done/waiting/compacting states and tool summaries (`plugin/hooks/*.py`),
transcript tails extract `<speak>` and `<team>` blocks, runners record token
usage and terminal results, and `janitor_context.build_context` already
reconstructs the user objective, active plan and latest reply for the task-label
Janitor. What the Host does not observe at all today: **git state in the agent's
working tree** and **detached worker processes**. Both are needed below.

## 2. Inventory of obligations, sorted by who should own them

Legend: **H** = Host mechanics, no model. **J** = Janitor (cheap model, bounded
evidence, guarded effect, run receipts). **A** = stays with the working agent.

### 2.1 Background jobs (`clarp-background-jobs`, forced into every prompt)

| Obligation today | Owner | Why |
|---|---|---|
| Detach with setsid/systemd-run | A | The detachment itself is real work; the Host cannot do it for the agent |
| `job-upsert` with kind/title | H+J | The launch is observable (PostToolUse on the Bash call, or a `clarp-agent-bg run` wrapper). Title can default to the command; a Janitor can rename it later the way task labels are renamed |
| Heartbeat every 2 minutes | H | Liveness is `kill -0 PID`. A wrapper that owns the PID heartbeats itself |
| `job-finish` / `job-fail REASON` | H | Exit code of the wrapped process. Reason text is a Janitor summary of the tail of the log, not an agent duty |
| `job-active` gate before delivery | H | Server-side gate on the job handle; the wrapper can refuse to deliver |
| Set `CLARP_BACKGROUND_WORKER_PID` | H | The helper can read its own parent PID |
| Heartbeat prompt "audit durable background jobs" | H | A reconciler over PIDs and heartbeats replaces asking an LLM to audit |
| `job-cancelled` handshake on user cancel | H | The wrapper owns the PID and can stop it; the agent only needs to be told afterwards |

Net: replace the skill with one command, `clarp-agent-bg run -- <cmd>`, that
detaches, registers, heartbeats, finishes and honours cancellation. The only
sentence left in the prompt: "for work that must outlive this reply, start it
with clarp-agent-bg run".

### 2.2 Task plans (`clarp-tasks`)

| Obligation today | Owner | Why |
|---|---|---|
| Create a plan before multi-step work; write 5–8 outcome steps | A, with J fallback | Intent is the agent's. But when an agent forgets, a **task-plan Janitor** can seed a plan from the same evidence the label Janitor already reads (objective, tool activity, TodoWrite todos captured by `transcript_log.py:107`) |
| Use the returned `plan_id`, not the supplied one | H | Helper detail; return the same id or resolve by session |
| Keep exactly one item `in_progress`; update at transitions | J | Step boundaries correlate with tool-call clusters and TodoWrite changes; a Janitor marks progress and closes stale `in_progress` items on `agent-work-completed` |
| `finish` for blocked/cancelled plans | J | Terminal turn state plus final reply is enough evidence |
| Cross-host migration recreates plans by hand (`clarp-switch-agent-host`) | H | Export/import plan rows |

Trigger: `agent-work-completed@1` and `active-interval@1` already exist. New
effect: `task_plan` (create, advance item, finish). The label Janitor's
`insufficient_context` / `same_task` outcomes map directly.

### 2.3 Questions and approvals (`clarp-decisions`, `_NO_INTERACTIVE_QUESTIONS`)

| Obligation today | Owner | Why |
|---|---|---|
| Never use CLI popups | A | Must remain; the popup would hang the turn. One sentence, not a paragraph |
| Ask via `clarp-agent-artifacts question` with 2–3 options, `--recommend`, `--blocks-progress`, `--priority-reason`, `--urgency`, `--effort`, `--deadline-at`, `--context`, `--reference`, `--expires-at` | J | Agent asks in plain text and stops. A **question-shaping Janitor** (demand worker, like the tool explainer) turns a final reply that ends in a question into the native card: options, recommendation, effort. `blocks_progress` is observable: the agent ended the turn without doing the work |
| Check `attention` before asking to avoid duplicates | H | Server-side dedupe by reference/idempotency key |
| Verify the returned artifact shape | H | The helper asserts it |
| On timeout, inspect attention before retrying | H | Idempotency key on `POST /decisions` |
| `delivery_pending`, do not interrupt another turn | H | Already server facts |
| Yes/No approval for protected actions | A | Authorization must be requested by the party about to act. Keep the decision helper, but it can be the single remaining artifact CLI an agent learns |
| `[Clarp decision resolved]` callback prompts | H (keep) | Already Host-authored; fine |

The text fallback path already exists in the prompt ("if native questions are
unavailable, ask in ordinary text"). Making it the primary path costs one Janitor.

### 2.4 Uncommitted work (repo `AGENTS.md` rule)

Today each agent must notice its own dirty tree, name only its files, and raise
the three-option question. Git state is unobserved by the Host, so this is the
one rule that cannot be enforced or automated without a new sensor.

Proposed: a **git sensor** records, per agent cwd at turn end, `git status
--porcelain`, branch, ahead/behind, and which dirty paths this turn touched
(from Edit/Write/Bash tool activity, already in `agent_activity`). A
**workspace Janitor** on `agent-work-completed` raises the commit / branch+PR /
leave question with the diff summary, scoped to the files this agent touched.
The same Janitor can publish the `code_change` artifact when a turn ends with
new commits (`git diff --shortstat`, `rev-parse`, remote URL), replacing
`clarp-code-changes` entirely. Effects: `decision_question`, `artifact_publish`.

### 2.5 Deliverables (`clarp-media`, `clarp-files`, `clarp-audio`, `clarp-video`, `clarp-data`, `clarp-research`, `clarp-releases`, `clarp-directories`, `clarp-documents`, `clarp-github-actions`)

| Obligation today | Owner | Why |
|---|---|---|
| Run `clarp-media-publish` and paste the returned Markdown exactly | H | The Host wrote the Markdown; it can inject it into the reply |
| Supply `mime_type`, `file_name`, `size_bytes`, `duration_ms`, `thumbnail_url` | H | Probe the file |
| Safe relative path for `directory` | H | Sanitize server-side |
| Receipt files and reconcile-before-retry for PDFs and forms | H | Idempotency key on artifact create |
| Version bump when HTML form changes | H | Hash the payload |
| `workflow_run` polling | H | `github_workflow_artifact.py` already does it; start it from a PostToolUse on `gh run watch`/`gh pr merge` |
| Decide *whether* a produced file is a durable deliverable | J | A **deliverable Janitor** watches Write/Bash output paths and media files created during a turn and publishes the ones worth reopening. This is the same judgment the label Janitor makes about text |
| Choose chart kind for `data`, sources for `research` | A or J | Mostly authoring; sources are recoverable from fetched URLs in the transcript |
| Wire HTML forms to `window.clarpForm` | A | Authoring, not bookkeeping |

### 2.6 Teams and leader (`team_store.py`, `leader_memory.py`, `team_leader.py`)

| Obligation today | Owner | Why |
|---|---|---|
| Write `<team>` updates on start/finish/blocker/handoff | J | Turn start, terminal state, `waiting` state and the final reply are all observed. A **team-feed Janitor** posts the one-line update; agents that write `<team>` voluntarily still can |
| Report terminal results to the leader via `self-prompt --from me --to leader` | H | Terminal turn plus final answer is the evidence; the Host can deliver it, with `--from` filled in by the server |
| `--from` and `--origin automation` on agent-to-agent prompts | H | The server knows the caller |
| Leader standing orders, decision log CLI | A | Leader judgment; out of scope here |
| Pending digest injection | H (keep) | Already Host-owned |

### 2.7 Voice formatting

`<speak>` gating, first-line ack, splitting into one-minute blocks, `<vox>`
fillers and `<break>` cues are persona and delivery rules, not bookkeeping, but
they are the largest block of Host text an agent reads on every voice turn. A
**speech-shaping demand worker** (same executor as the tool explainer) could take
a plain final reply and produce the spoken gist and chunking. The first-line
acknowledgement is latency-sensitive and should stay with the agent or be
synthesised by the Host. Treat this as optional and evaluate after the items
above.

### 2.8 Admin skills (`clarp-janitors`, `clarp-agent-admin`, `clarp-server-admin`, transcription, voice adapters)

These are operator tasks, not per-turn burdens. Their remaining bookkeeping is
Host mechanics: `--expected-revision` fetched by the helper, request-id ledgers
made automatic, post-install test triads scripted, schema version max+1 by a
lint, deploy-drift check by `doctor`. No Janitor needed.

## 3. Defects found on the way

- `$CLARP_SESSION` is referenced by `clarp-agent-communication`, `clarp-location`
  and `skills/calendar`, and nothing in the repo sets it. Runners export only
  `CLAUDE_PWA_SESSION`. Those commands expand to an empty session today.
- `_NO_INTERACTIVE_QUESTIONS` is maintained twice (`codex_runner.py:73`,
  `plugin/hooks/pwa_source_flag.py:218`).
- `skills/calendar/` and `skills/clarp-switch-agent-host/` exist but are not in
  `skills/manifest.json`, so they are never installed.
- `clarp-avatar-generation` contains hard-coded `/home/peter/...` paths that
  `personal_skills.py:278` would flag as a host-path dependency.

## 4. What the Janitor framework needs to host this

Current catalog: templates `task-labels`, `message-delegator`, `tool-explainer`;
effects `task_label`, `message_route`, `tool_explanation`; triggers
`agent-work-completed@1`, `schedule@1`, `active-interval@1`,
`routing-requested@1`, `tool-explanation-requested@1`.

Additions, in dependency order:

1. **Sensors** (Host, no model): git sensor per agent cwd at turn end; managed
   background-job wrapper owning PID, heartbeat, exit code and cancellation;
   produced-file detection from Write/Bash activity.
2. **Effects**: `task_plan`, `decision_question`, `artifact_publish`,
   `team_message`, `background_job_meta`. Each keeps the existing contract:
   frozen candidates, atomic receipt, generation fence, no-op becomes
   `same_task`-style outcome, user-owned text preserved.
3. **Generalised context builder**: `janitor_context.build_context` is
   label-specific but already collects objective, plan, updates and final
   reply. Extend it with tool-activity clusters, TodoWrite todos, git summary
   and produced files, still redacted and clipped.
4. **Demand trigger for reply shaping**: `reply-finalized@1`, used by the
   question-shaping and speech-shaping workers, following the routing-requested
   claim/expiry pattern so a reply is shaped once and never twice.
5. **Templates**: `task-plans`, `workspace` (uncommitted work + code_change),
   `deliverables`, `team-feed`, `question-shaper`. Same rusty-robot family,
   Codex Spark defaults, paused on creation.

## 5. Suggested order

1. Host mechanics with no model: background-job wrapper, `--from`/origin
   inference, artifact idempotency and metadata probing, media Markdown
   injection, the `$CLARP_SESSION` fix. This removes roughly half of the
   obligations and shortens `clarp-background-jobs`, `clarp-media`,
   `clarp-files`, `clarp-audio`, `clarp-video`, `clarp-code-changes` to one
   line each or deletes them.
2. Git sensor plus the workspace Janitor. Replaces the `AGENTS.md` rule, which
   is currently unenforceable.
3. Task-plan Janitor as an extension of task labels.
4. Question-shaping demand worker; shrink `_NO_INTERACTIVE_QUESTIONS` to one
   sentence and delete its duplicate.
5. Team-feed Janitor and Host-delivered terminal reports to leaders.
6. Deliverable Janitor.
7. Evaluate speech shaping.

## 6. What an agent would still need to know

After steps 1–6, the standing Host text for a worker is: its persona line, "you
are in a phone app: no CLI popups, ask in plain text and stop", the voice tag
rules if kept, and "start long work with clarp-agent-bg run". Approvals for
protected actions remain an explicit agent request. Everything else is
observed or produced by a Janitor with a receipt.
