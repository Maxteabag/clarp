---
name: clarp-sub-agents
description: Spawn sub-agents (helper agents, parallel workers, delegated tasks) from a Clarp agent, or fork yourself into helpers that start from your own conversation. Use this INSTEAD OF the built-in Agent/Task tool or Codex sub-agents for any sub-agent job longer than a few minutes or that edits code, because built-in sub-agents die when Clarp restarts the session after a usage limit. Each sub-agent runs detached, survives restarts, and shows in the apps as a running background job.
---

# Clarp sub-agents

**Read this before spawning any sub-agent.** Built-in sub-agents (Claude
Code's Agent/Task tool, Codex's sub-agents) live inside your session. When
Clarp restarts the session, for example after a usage limit, the teardown
kills them mid-task. Their transcript then says "didn't finish before the
previous session ended". `setsid` and `nohup` do not help, because the child
stays in your session's systemd cgroup and dies with it.

A Clarp sub-agent survives that. It comes in two modes:

- **`--clarp-agent` (preferred when the owner should see and steer it).** The
  sub-agent is a real Clarp helper agent: role `helper`, parent = your
  session, cwd = its worktree. It nests under you in the chat list, the owner
  can open its chat, watch its tool calls and message it, and it reports back
  to you as an agent-origin message. Its state (`running`, `reported`,
  `done`, `failed`, `abandoned`) is in the snapshot.
- **Default (systemd).** A raw `claude -p` / `codex exec` process in its own
  systemd user unit. Use it for fire-and-forget workers nobody needs to open.

The words matter, and the apps count them apart. A **sub-agent** is a Clarp
helper agent (`--clarp-agent`), counted from your running helpers. A
**background process** is a durable job, such as a systemd worker. A
`--clarp-agent` helper also gets a small watcher job (kind `sub-agent`) that
only mirrors it and is not counted again; a systemd worker registers kind
`worker` and counts as one background process. The apps show those counts
as a badge; your status line says what the work is doing ("stream-a:
running tests", "3 working, 1 waiting", a worker's latest progress line).

For non-agent watchers or detached systemd/nohup processes, use
[clarp-background-jobs](../clarp-background-jobs/SKILL.md) directly for visible
purpose, status, output and cancellation.

## When to use which

| Situation | Use |
|---|---|
| Short read-only lookup whose answer you need in this turn | Built-in Agent/Task tool |
| The child needs what you already know (the plan, decisions, files read) | `clarp-sub-agent fork` |
| Several parallel workstreams of a plan you just worked out | `clarp-sub-agent fork`, one per stream |
| Several attempts at the same problem, compared afterwards | `clarp-sub-agent fork NAME --count N` |
| A self-contained job a fresh agent can do from a written brief | `clarp-sub-agent start --clarp-agent` |
| A different backend or a clean context on purpose (an unbiased review) | `clarp-sub-agent start --clarp-agent` |
| Anything longer than about five minutes, or that edits code | `clarp-sub-agent` (fork or start) |
| Fire-and-forget batch work | `clarp-sub-agent start` (systemd mode) |

### Choosing Antigravity (`--backend agy`)

AGY runs Claude Opus/Sonnet 5.5 and Gemini on the Antigravity account, so
its quota is separate from your Claude and Codex accounts. Choose it when:

- your Claude accounts are at their limit but the job wants an Opus-class
  model: `--backend agy --model claude-opus-5-5-high`;
- the job is web research. AGY has built-in web search and page fetch
  (`search_web`, `read_url_content`), no flag needed, and they show as
  WebSearch/WebFetch rows in the helper's chat;
- you want cheap parallel probes: `gemini-3.8-flash-low`.

Effort is part of the model id (`claude-opus-5-5-low|medium|high`,
`claude-sonnet-5-5-*`, `gemini-3.8-flash-*`); a bare `claude-opus-5-5` or a
separate `--effort` is rejected. `agy models` lists what your account
offers. An AGY child of a fork is always a `seed` fork (see below), and AGY
streams no thinking text, so its chat shows messages and tool rows only.

```bash
clarp-sub-agent start research /path/to/wt prompt.md --clarp-agent \
  --backend agy --model claude-opus-5-5-high --title "Web research"
clarp-sub-agent fork probe --cwd /path/to/wt --backend agy \
  --model claude-opus-5-5-high --task "Search the web for X and report."
```

## Fork: a helper that starts from your conversation

A fork is a Clarp helper agent (role `helper`, parent = you, nested under
you in the chat list, reports back like any helper) whose conversation is a
copy of yours up to now. It already knows the plan, the decisions and what
you read, so its task can be one or two lines instead of a full brief. You
keep working; the child carries on alone from its copy.

```bash
# One child in its own worktree (created beside the repo as
# <repo>-worktrees/<branch>, or reused if that worktree exists):
clarp-sub-agent fork stream-a --task "Do part A of the plan above; WIP-commit, do not push." \
  --worktree ~/GIT/clarp:feat/stream-a

# Three tries at one problem, probe-1..probe-3, each on branch fix/x-1..fix/x-3:
clarp-sub-agent fork probe --count 3 --task "Try a different fix for the flake." \
  --worktree ~/GIT/clarp:fix/x

# Different tasks in one call, straight through the Host:
clarp-admin agent fork --spec children.json     # [{"name", "task", "worktree"?: {"repo", "branch"}}, ...]
```

Both forms fork `$CLARP_SESSION` (or `$CLAUDE_PWA_SESSION`); pass `--from`
to `clarp-admin` to fork another of your sessions. `--backend`, `--model`
and `--effort` apply to every child. Each child is sent its task from you
with instructions to report via `clarp-admin prompt --to <you> --from
<child>`; `clarp-sub-agent fork` also starts the same watcher job as
`start --clarp-agent`, and `helper-state`, `done` and re-tasking work the
same way (see Collect below).

How the copy is made (the `fork` field of each child row):

- `native`: Claude Code and Codex copy the conversation itself. Claude's
  copy goes into the child's own project directory, so it resumes from its
  worktree. The tool call you are making while forking is not in the copy
  (Claude Code writes it after the call returns); the child is told the
  conversation ended with the fork.
- `seed`: Grok, AGY, OpenCode and DeepSeek, or a child on a different
  backend than yours. The child starts a fresh session whose first message
  holds the most recent part of your transcript (about 60,000 characters,
  each turn trimmed to 4,000, tool calls as one-line summaries), then the
  task. Put anything it must not miss into the task itself.

Fork when the shared context is the point. Do not fork to escape a full
context window (the child inherits it) or for work that needs a fresh,
unbiased view. The worktree rule below holds for forks too: one tree per
child, never two children in one tree. A failed child (bad worktree, name
already in use) is reported in its row and on stderr; its siblings still
start, and the command exits 1.

## How

1. **Worktree.** Give each sub-agent its own git worktree. Never point two at
   one tree.
2. **Prompt file.** The sub-agent starts with no conversation, so write
   everything it needs to a file:
   - scope and the files it owns;
   - a WIP commit after every meaningful step, since it may be killed and resumed;
   - the test gate, chunked with `timeout 500` per chunk;
   - the final commit, a report written to a file, and "do not push";
   - how to wait: never end a turn on a pending external result (a Mac QA
     run, CI, a build). Wait inside the turn with a bounded watch, or
     schedule a wake-up (`clarp-admin prompt --to SELF --delay 10m`) before
     ending it. A turn that ends with "still running, I'll check" stalls:
     nothing wakes the helper again.
3. **Launch.**

   ```bash
   clarp-sub-agent start stream-a /path/to/worktree /var/tmp/prompts/stream-a.md \
     --title "Refactor stream A"            # --model claude-opus-5-5 is the default
   clarp-sub-agent start probe /path/to/wt prompt.md --backend codex
   clarp-sub-agent status
   clarp-sub-agent stop stream-a
   ```

   Output goes to `/var/tmp/clarp-sub-agents/NAME.log`, streamed as it
   happens: Claude runs with `--output-format stream-json --verbose` and its
   assistant text and one line per tool call are written to the log. The log
   is registered on the job, its latest line becomes the job's progress, and
   the apps can show the tail through `GET /background-jobs/<job_id>`.
   `NAME.exit` holds the exit code when the worker finishes. The background
   job is finished or failed to match.

   As a Clarp helper agent:

   ```bash
   clarp-sub-agent start stream-a /path/to/worktree /var/tmp/prompts/stream-a.md \
     --clarp-agent --backend codex --model gpt-5.5 --title "Refactor stream A"
   ```

   This runs `clarp-admin agent create stream-a --parent "$CLARP_SESSION"
   --role helper --cwd WORKTREE`, sends the prompt from your session with
   instructions to report back via `clarp-admin prompt --to <you> --from
   <helper>`, and starts a small watcher unit that holds the background job
   until the helper reports (job finished) or fails or is abandoned (job
   failed). Any Clarp backend works (`claude`, `codex`, `grok`, `agy`,
   `opencode`, `deepseek`). `NAME.session` holds the helper's session.
   Its report arrives in your chat as a message from the helper.
4. **Model with quota.** Sub-agents share your account's quota. If your
   model is at its limit, a sub-agent on the same model dies on its first
   call ("You've hit your session limit"). Pass `--model` explicitly when
   yours is close to the limit.
5. **Resume.** A runtime restart (`clarp-runtime` / a Host update) ends
   every helper's current turn. Afterwards, message each helper whose
   `helper_state` is `running`, not only the ones that looked busy: a helper
   waiting on a Mac or CI result looks idle and would otherwise never resume.
   To resume a killed sub-agent, start it again with the same
   name and prompt. It reads its own WIP commits and continues. A Clarp
   helper survives restarts on its own; starting it again with the same name
   reuses the same helper agent and re-sends the prompt.
6. **Collect.** Read the report file and `git log` in the worktree, merge
   into your branch, then close out the worktree. For a Clarp helper, mark
   it done when you have taken its result, and it archives itself after the
   grace period (24 h by default):

   ```bash
   clarp-admin agent helper-state stream-a-3f9c done --from "$CLARP_SESSION"
   clarp-admin agent helper-state stream-a-3f9c        # show its state
   ```

   To give a reported helper more work, just message it
   (`clarp-admin prompt --to <helper> --from "$CLARP_SESSION"`); it goes back
   to `running`. A helper you already marked `done` does not: `done` means
   accepted, and a message alone leaves it showing `done` while its new turn
   runs. Reopen it explicitly first, then message it, and keep the same helper
   and worktree rather than spawning a duplicate:

   ```bash
   clarp-admin agent helper-state stream-a-3f9c running --from "$CLARP_SESSION"
   clarp-admin agent helper-state stream-a-3f9c        # reads back running
   ```

   A queued prompt is not proof the work started; check the helper's own new
   activity. `clarp-sub-agent stop NAME` stops only the watcher in this
   mode; the helper agent stays until you mark it done or delete it.

## Notes

- **Never use `pgrep -f` or `pkill -f` inside a sub-agent.** They match
  full command lines, and a sub-agent's command line is its prompt. If the
  prompt mentions `install.sh` or `npm ci`, a "kill leaked install.sh"
  cleanup kills the sub-agent itself. Its working directory is the
  worktree, so a cwd filter matches too. On 2026-09-26 three workers each
  killed themselves this way right after their test gate. To clean up
  leaked test children, match on the executable (`pgrep -x bash` plus
  `/proc/PID/cmdline` starting with the script path) and skip your own
  process tree.
- Systemd mode is Linux only. On macOS `start` exits with status 3; use
  `launchctl submit` with the same command until a launchd path exists.
  `--clarp-agent` works on macOS too, but without systemd no background job
  is registered.
- Progress is visible through `git log` in the worktree and, in systemd
  mode, live in the log and the job's progress line.
- If a worker's unit was stopped, its job fails on its own as soon as the
  Host sees the PID is gone. To close one yourself, run `clarp-agent-bg
  "$CLARP_SESSION" job-cancel HANDLE`.
