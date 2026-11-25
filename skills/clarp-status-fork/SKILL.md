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

On a Rust Host, the script selects the agent from the local snapshot, excludes
moved-away entries, and reads queue, background-job and attention facts over
loopback. It presents the token of the agents' tools, never the owner's: its turn's
`CLARP_TOOL_TOKEN`, else `tools.toml` beside the Host's `config.toml`, else that token
made from `config.toml` as the Host makes it. The Host answers it through bound privacy
as the turn it presents (`X-Clarp-Turn`), so a bound agent it may not read is refused.
These reads need a Rust Host with `tools_owner_free` (contract 193). If the
agent still lives on the local Python Host, the script uses the existing Python
path.

Rust Claude forks use the Host's configured executable, the agent's saved session
and working directory, and the same guard and preflight described below. Model
and effort come from the snapshot. Cache reuse can differ from a normal Host turn
with custom instructions or MCP settings. Rust Codex forks use the Host's
configured executable and its ephemeral fork command with a read-only sandbox,
approval policy `never`, and shell, plugins and configured MCP tools disabled.

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
  time, busy or idle and queue (on a Rust Host from contract 169, how many of the user's
  queued messages go to the agent together, as one turn, when its turn ends), helper agents
  and their state, background jobs of the last 12 hours, and the agent's cards to the user (pending or recently resolved). It
  goes after the question (cache-safe) with "these override what you remember; say
  where they differ". Without it, a fork reports the agent's belief, which can be an
  hour old when the agent is waiting on a timer.

## Success checks

- Each result shows `Nk reused, ~0-4k new`. Mostly new tokens means the argv drifted
  from `build_cmd`.
- The answer names differences from the live facts when there are any.

## Limits

- Claude on either Host and Codex on the Rust Host. Other backends return an error.
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
- An agent bound to a model provider of its own (Rust Host contract 179, feature
  `bound_privacy`): run from an agent's turn, the script presents that turn
  (`X-Clarp-Turn`), and where the Host answers the bound agent's row with
  `words_withheld: true` it stops and launches nothing ("refused, nothing launched: <name>
  is bound to a model provider of its own …"). That is the answer: you are not one of the
  agents its owner lets read it, so do not relay its status. Ask for access with
  `clarp-admin agent provider --session <it> --reader <you>` (a card to the owner), or name
  it as working or idle from its row. Run by the owner (no turn), or by an agent bound to
  the same home and provider or let in, a bound Codex agent is forked in its own Codex home
  and on its own provider (its `config.toml` is read, `-c model_provider=<its provider>`),
  never the machine's; a home that does not define that provider launches nothing.

## Front-desk agent

To give the user one always-available agent for status, decisions and steering across
a group of workers, follow [references/front-desk.md](references/front-desk.md).
