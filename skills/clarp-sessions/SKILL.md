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
