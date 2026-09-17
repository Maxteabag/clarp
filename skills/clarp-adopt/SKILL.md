---
name: clarp-adopt
description: Resume an existing Claude, Codex, Grok, or OpenCode terminal conversation as a Clarp agent with its history. Use for "make this session Theo", "move this conversation into Clarp", "continue this on my phone", or adopting a past CLI session.
---

# Clarp Adopt

Turn a conversation that started in a plain CLI into a Clarp agent. Nothing is
copied: Clarp binds the CLI's own session id and resumes it natively, so the
agent keeps the full history.

```bash
clarp-adopt Theo            # inside the conversation: adopt it as Theo
clarp-adopt                 # same; Clarp picks a free compatible contact
clarp-adopt Theo --dry-run  # show backend, native id and folder; change nothing
clarp-adopt --all           # outside a conversation: pick a recent session
clarp-adopt Theo --id NATIVE_ID [--backend grok] [--cwd DIR] [--model M --effort E]
```

Run it once, in the original conversation. It reads that conversation's id from
`CLAUDE_CODE_SESSION_ID` or `CODEX_THREAD_ID`; a helper agent's environment
names the helper's own conversation. Grok and OpenCode export no id, so use the
picker (`--all`, `--pick N`) or `--id`. The picker marks sessions a live agent
already owns; choosing one reuses that agent.

Exit status: `0` the agent exists and its history was verified through `/log`;
`2` the agent exists but the history was not verified; `1` nothing was changed.
Rerunning is safe: an id that is already bound is reused, never duplicated.
`--json` prints the result for scripts.

Relay the agent name, session slug and whether it printed `Verified`. On a
nonzero exit, relay the message and stop. Do not retry with another contact,
replace an occupied one, or edit Clarp's database. After a verified result, tell
the user to exit the terminal client before messaging the agent: two writers on
one conversation conflict, and an interactive Codex client holds a lock that
blocks Clarp's first turn. Creating the agent runs no model turn.

The conversation must be on the same machine as the Clarp Host. Changing
backend (for example Codex history continued on Claude) needs the transcript
converted first; this skill only binds a conversation its own backend can read.
A name with no existing contact creates the session without a contact record;
create the contact with `clarp-agent-admin`.
