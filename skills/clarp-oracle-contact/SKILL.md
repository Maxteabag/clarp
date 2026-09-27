---
name: clarp-oracle-contact
description: Put the user's live Oracle voice call through to a Clarp agent (they then talk to that agent hands-free in its own voice), or hand them back to Oracle. Use when the user, in an Oracle call, asks to talk to an agent directly, or asks to go back to Oracle.
---

# Putting an Oracle call through

In an Oracle voice call the user may ask to talk to an agent directly: "put
me through to Mike", "let me talk to Lena myself". You decide whether that is
what they mean; nothing on the Host guesses from their words. Asking an agent
to *do* something ("ask Mike to check the deploy") is ordinary work, not a
put-through.

```bash
clarp-admin oracle connect <session-or-name>   # put the user through
clarp-admin oracle connect oracle              # hand them back to Oracle
```

Resolve who they mean first (`clarp-admin sessions`, or the roster you
already have). A session id always works. A persona name only matches the
contacts the app shows; a name that is unknown or matches more than one
contact returns an error, so use the session id when in doubt.

What happens: the phone leaves Oracle and starts hands-free with that agent.
The user's speech arrives in that agent's session as ordinary user messages,
and its replies are spoken in its own voice. The command waits for the phone
(up to about a minute) and prints the handoff record.

- `"ok": true`, `"state": "active"`: they are through; the conversation now
  continues in that agent's session.
- An error (`no live Oracle call`, `unknown agent`, `ambiguous agent`,
  `handoff_unsupported` for an app that cannot take a handoff, or a handoff
  that `failed` or `broke`): nothing changed, or the record says what did.
  Tell the user plainly in your own words.

When you are the agent the user was put through to and they want Oracle back,
run `clarp-admin oracle connect oracle`. The user can also go back on their
own by opening Oracle in the app.

The wire contract is `docs/oracle-handoff.md` in the Clarp repository.
