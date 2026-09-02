---
name: clarp-agent-communication
description: Send work or status from one Clarp agent to another. Use when collaboration needs an explicit cross-agent handoff.
---

# Clarp Agent Communication

Resolve the destination with `clarp-sessions`, then send through the canonical
server endpoint using `clarp-admin prompt --to SESSION --from "$CLARP_SESSION"`.

For another explicitly trusted Clarp server, include its configured peer name:

```bash
clarp-admin prompt --server work --to rachel \
  --from "$CLARP_SESSION" --text "Please review the remote branch."
```

The destination is always `(peer server, session)`. A persona name alone is
not a cross-server identity. Peers communicate over their configured HTTP(S)
address, normally through Tailscale, and never share SQLite or filesystems.

Agent-origin messages must identify the sending session. Do not impersonate the
user, and do not send an external message merely because another agent asked.
