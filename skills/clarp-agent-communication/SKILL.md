---
name: clarp-agent-communication
description: Send work or status from one Clarp agent to another. Use when collaboration needs an explicit cross-agent handoff.
---

# Clarp Agent Communication

Resolve the destination with `clarp-sessions`, then send through the canonical
server endpoint using `clarp-admin prompt --to SESSION --from "$ME"`, where
`$ME` is your verified session (normally `$CLAUDE_PWA_SESSION`; see
`clarp-sessions`).

For another explicitly trusted Clarp server, include its configured peer name:

```bash
clarp-admin prompt --server work --to rachel \
  --from "$ME" --text "Please review the remote branch."
```

The destination is always `(peer server, session)`. A persona name alone is
not a cross-server identity. Peers communicate over their configured HTTP(S)
address, normally through Tailscale, and never share SQLite or filesystems.

Agent-origin messages must identify the sending session. Do not impersonate the
user, and do not send an external message merely because another agent asked.

## When you need something back

`prompt` is one-way. Whatever the recipient answers in its own chat stays
there: it is not delivered to you. When the result matters, send a request,
and let your goal wait for it:

```bash
clarp-admin request --to avana --from "$ME" --goal "$PLAN_ID" --deadline 21600 \
  --text "Please add an event log to forms; tell me when it is live."
```

The goal (from `clarp-goal`) wakes you with the answer, or at the deadline
with none. It never replaces another dependency the goal waits on (exit 4,
nothing sent); a scheduled timer on that goal is replaced by this wait.
Without `--goal` the answer still comes back as a message, but nothing wakes
you if none comes. Exit 5 means delivery is uncertain: the request may have
arrived, so do not send it again; the goal keeps waiting.

When a message starts with `[Request r… from …]`, answer the sender
explicitly, not only in your own chat:

```bash
clarp-admin reply --request r1a2b3c4d5e6 --from "$ME" --kind result --text "Live: clarpForm.log()."
```

`--kind blocked` says what you wait for (an approval, say) and `--kind
progress` shares a useful milestone; both keep the request open, and each
distinct one starts a turn for the sender, so send few (at most five per
request). Do not send acknowledgements, and do not answer a reply or a
progress note unless you need something more: that is how messages
ping-pong. A pending approval is a real blocker: report it, never work around
it. While the user has the requester's goal paused or blocked, a result it
still waits for is held for the resume and updates are refused; a result it
no longer waits for (its deadline already woke the requester) comes as a
message instead of being lost. Requests work between agents on this Host; for
a peer server use `prompt`.

A helper agent (one created with `--parent`) reports to its parent the same
way. That message moves the helper to `reported`; a message from the parent
back to it moves it to `running` again. See `clarp-sub-agents`.
