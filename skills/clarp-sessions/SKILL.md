---
name: clarp-sessions
description: Resolve Clarp agent personas, session slugs, backends, and working directories. Use before targeting another agent or diagnosing session state.
---

# Clarp Sessions

Read the canonical local database through the platform-aware admin command:

```bash
clarp-admin sessions
```

Use the `session` slug for Clarp API requests. Do not guess a session from a
persona when more than one row could match.

To replace one or more active conversations with genuinely fresh sessions
while preserving their agent configuration, use the server-owned reset:

```bash
clarp-admin sessions reset <session> [<session> ...]
```

The command returns each old and new session slug. Do not reproduce this with
manual DELETE and POST calls: reset is transactional and intentionally keeps
old messages bound only to the soft-deleted identities.

Reset refuses the configured `[server] default_session`; select another active
default and restart Clarp before resetting that conversation.

Reset also refuses a session with active work, a turn still starting, or a live
interactive terminal. Stop the work or close the terminal, then retry; reset
never kills an in-flight turn as a side effect.
