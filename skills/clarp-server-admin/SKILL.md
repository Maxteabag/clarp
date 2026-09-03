---
name: clarp-server-admin
description: Diagnose, update, roll back, and repair a Clarp installation. Use for server health, installation, upgrade, or managed-skill problems.
---

# Clarp Server Administration

Prefer the supported commands:

```bash
clarp-admin doctor
clarp-admin update
clarp-admin rollback
clarp-admin skills repair-links
```

Never hand-edit the active generated release. Use `clarp-admin paths` to find
the platform-native release, configuration, database, cache, log, service, and
toolchain paths.

## Docker Container Administration

When diagnosing or managing Clarp inside a Docker container:

- Run diagnostics inside the container:
  ```bash
  docker compose -p clarp exec clarp clarp-admin doctor
  ```

- Authenticating AI providers inside headless containers:
  * For terminal/interactive use, pass `-it` to keep stdin open for OAuth codes:
    ```bash
    docker compose -p clarp exec -it clarp claude auth login --claudeai
    docker compose -p clarp exec -it clarp codex login --device-auth
    ```
  * When automating or scripting the login flow via agent/subprocess, use `-T` and keep the standard input stream connected to feed the callback code to the active prompt without terminating the session or expiring challenge parameters.
  * Ensure the container has working outbound DNS (configured in `compose.yaml`) before initiating OAuth token exchanges.

- Tailscale and phone connectivity:
  * Prefer the `compose.tailscale.yaml` sidecar mode for isolated Tailnet identity, auto-HTTPS, and zero host firewall conflicts.
  * If publishing ports on the host directly (`CLARP_PORT=...`), ensure host firewalls (`ufw` / `DOCKER-USER`) allow Tailscale CGNAT traffic (`100.64.0.0/10` / `tailscale0`) to forward to Docker bridge networks.

