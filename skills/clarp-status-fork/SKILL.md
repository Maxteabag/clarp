---
name: clarp-status-fork
description: Get a current status from, or ask a question of, a busy Clarp agent without interrupting it or queueing behind its turn, by forking its conversation read-only for one answer. Also sets up a front-desk agent that answers the user's status, decision and steering questions for a group of worker agents. Use for "status on X", "what is X doing", "what's waiting on me", "ask X why ...", or when the user keeps pinging busy workers for status.
---

# Status from a read-only fork

Asking a busy worker "Status?" waits behind its current turn, often for minutes, and
the user's own chat message closes that worker's pending question cards. Instead,
fork the worker's conversation for one question and discard the fork.

```bash
clarp-status-fork pebble hobbles kasper                 # standard status, in parallel
clarp-status-fork pebble --question "Why did you choose Container Apps?"
clarp-status-fork kasper --show-facts                   # only the live fact sheet
clarp-status-fork pebble --json                         # answer, seconds, token reuse
```

Agents are matched by session slug, slug prefix or persona name.

## How it works (keep these properties when changing it)

- **Same argv as the agent's own turns.** It builds the command with
  `ClaudeBackend.build_cmd`, so model, effort, plugin, MCP scope and persona prompt
  match and the fork reuses the agent's prompt cache. Any difference at the start of
  the request (for example `--tools ""`) is a cache miss that bills the whole context.
- **Read-only.** A PreToolUse hook (`scripts/read_only_guard.py`, passed with
  `--settings`) denies every tool except Read, Grep, Glob, LS and NotebookRead, and a
  hook's deny wins over inherited `permissions.allow` rules. `dontAsk` replaces
  `--dangerously-skip-permissions` as a second layer, but on its own it still runs
  pre-approved tools. Do not swap the hook for deny rules: a bare-tool or `Tool(*)`
  deny removes the tool from the request and loses the agent's prompt cache. Reads
  work, so the fork can answer questions about code and files as well as status. The
  working directory stays the agent's, because `--resume` finds the conversation by it
  and it is part of the cached system prompt.
- **Fail-closed, within limits.** Before launching, `preflight()` resolves the agent's
  saved working directory to one absolute, real directory and refuses a relative,
  missing or non-directory path; the launch uses exactly that directory, never the
  caller's. It checks the guard exists and passes a self-test, and that Claude Code is
  at least 2.1.285. It refuses, launching nothing, when a local managed settings file
  disables or restricts hooks, or (before 2.1.295) when any settings file it examines
  pre-approves a non-read tool: managed, user, and the project and local files of every
  ancestor of the directory, both as saved and resolved through symlinks. The hook
  overrides an inherited `disableAllHooks: true` and is wrapped in `|| exit 2`, so a
  guard that cannot start or crashes blocks the call. From 2.1.295 it also sets
  `onFailure: "block"`, so a timed-out guard blocks too; before that, a timeout falls
  back to `dontAsk`. `scripts/guard_controls.py` re-runs the live controls (real CLI,
  throwaway session, a few small-model calls).
- **What it does not cover.** The guard is a tool-call boundary, not a process
  sandbox: other hooks and plugins still run at their own lifecycle points.
  Server-managed policy delivered from the claude.ai console is not visible to a local
  preflight, so a hook restriction set there is not detected. Before Claude Code
  2.1.295, a guard timeout combined with an installed mod that approves calls through
  `tool.check` is not covered. Upgrading Clarp's Claude Code to 2.1.295 or later
  closes the timeout case.
- **Database reads** go only through `sqlite3 -readonly -json`, with values inlined
  as quoted literals.
- **No traces.** `--fork-session --no-session-persistence`, with the Clarp session
  variables removed, so nothing is written to disk or to Clarp's messages.
- **Live fact sheet.** Before forking, it reads the database read-only: the current
  time, busy or idle and queue, helper agents and their state, background jobs of the
  last 12 hours, and the agent's cards to the user (pending or recently resolved). It
  goes after the question (cache-safe) with "these override what you remember; say
  where they differ". Without it, a fork reports the agent's belief, which can be an
  hour old when the agent is waiting on a timer.

## Success checks

- Each result shows `Nk reused, ~0-4k new`. Mostly new tokens means the argv drifted
  from `build_cmd`.
- The answer names differences from the live facts when there are any.

## Limits

- Claude-backend agents only. Other backends return an error.
- The command refuses, launching nothing, when the agent's saved working directory is
  gone, when a local managed policy disables hooks, or (before Claude Code 2.1.295)
  when inherited settings pre-approve a non-read tool. See "What it does not cover".
- The prompt cache lives about an hour. The first fork of an agent idle for longer
  reloads its whole context once.
- About 0.3 GB RAM per fork for 10 to 60 seconds. Fork one agent at a time when the
  machine is short on memory.
- The facts cover Clarp state only. Cloud resources, GitHub or e-mail can still be
  stale; the fork should cite its source and time.
- Agent-written timestamps can be wrong. Trust the fact sheet's times.

## Front-desk agent

To give the user one always-available agent for status, decisions and steering across
a group of workers, follow [references/front-desk.md](references/front-desk.md).
