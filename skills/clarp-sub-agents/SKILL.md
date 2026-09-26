---
name: clarp-sub-agents
description: Spawn sub-agents (helper agents, parallel workers, delegated tasks) from a Clarp agent. Use this INSTEAD OF the built-in Agent/Task tool or Codex sub-agents for any sub-agent job longer than a few minutes or that edits code, because built-in sub-agents die when Clarp restarts the session after a usage limit. Each sub-agent runs detached, survives restarts, and shows in the apps as a running background job.
---

# Clarp sub-agents

**Read this before spawning any sub-agent.** Built-in sub-agents (Claude
Code's Agent/Task tool, Codex's sub-agents) live inside your session. When
Clarp restarts the session, for example after a usage limit, the teardown
kills them mid-task. Their transcript then says "didn't finish before the
previous session ended". `setsid` and `nohup` do not help, because the child
stays in your session's systemd cgroup and dies with it.

A Clarp sub-agent runs as its own systemd user unit on a model you choose.
The helper also registers it as a background job of kind `sub-agent` on your
session, so the phone and desktop show that you are waiting on it.

## When to use which

| Situation | Use |
|---|---|
| Short read-only lookup whose answer you need in this turn | Built-in Agent/Task tool |
| Anything longer than about five minutes | `clarp-sub-agent` |
| Anything that edits code or runs a test gate | `clarp-sub-agent` |
| Several parallel workstreams | `clarp-sub-agent`, one per stream |

## How

1. **Worktree.** Give each sub-agent its own git worktree. Never point two at
   one tree.
2. **Prompt file.** The sub-agent starts with no conversation, so write
   everything it needs to a file:
   - scope and the files it owns;
   - a WIP commit after every meaningful step, since it may be killed and resumed;
   - the test gate, chunked with `timeout 500` per chunk;
   - the final commit, a report written to a file, and "do not push".
3. **Launch.**

   ```bash
   clarp-sub-agent start stream-a /path/to/worktree /var/tmp/prompts/stream-a.md \
     --title "Refactor stream A"            # --model claude-opus-5-5 is the default
   clarp-sub-agent start probe /path/to/wt prompt.md --backend codex
   clarp-sub-agent status
   clarp-sub-agent stop stream-a
   ```

   Output goes to `/var/tmp/clarp-sub-agents/NAME.log`. `NAME.exit` holds the
   exit code when the sub-agent finishes. The background job is finished or
   failed to match.
4. **Model with quota.** Sub-agents share your account's quota. If your
   model is at its limit, a sub-agent on the same model dies on its first
   call ("You've hit your session limit"). Pass `--model` explicitly when
   yours is close to the limit.
5. **Resume.** To resume a killed sub-agent, start it again with the same
   name and prompt. It reads its own WIP commits and continues.
6. **Collect.** Read the report file and `git log` in the worktree, merge
   into your branch, then close out the worktree.

## Notes

- Linux only (systemd). On macOS `start` exits with status 3; use
  `launchctl submit` with the same command until a launchd path exists.
- Progress is visible through `git log` in the worktree. With
  `--output-format text`, the log file is written only at the end.
