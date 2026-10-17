# Docker nodes

Docker is an optional deployment for an isolated Clarp computer. Native setup
remains the default when Clarp should work directly in a user's existing home
and repositories.

One container is one server. It owns its agent roster, CLI logins, database,
skills, models, media, and repositories. Containers never share those objects
unless an operator deliberately configures a network peer or bind mount.

New Docker nodes start with an empty roster. Personas are global from the
iPhone's perspective, so create or move only the personas that should run on
that node; Docker never activates a second built-in Mike or Rachel implicitly.

## Start a local node

```bash
export CLARP_IMAGE=ghcr.io/maxteabag/clarp:VERSION
docker compose -p clarp-work up -d
```

The default port is published on `0.0.0.0:7682` (or set `CLARP_LISTEN_ADDR=127.0.0.1`
for loopback-only). Read the generated bearer token without printing the
rest of the configuration:

```bash
docker compose -p clarp-work exec clarp python3 -c \
  "import tomllib; print(tomllib.load(open('/data/clarp/config.toml','rb'))['server']['auth_token'])"
```

Complete provider sign-in inside this node. The iOS Computer Settings screen
can start the same server-scoped device flows. For interactive terminal login,
use `-it` (or `-T` when piping standard input programmatically):

```bash
docker compose -p clarp-work exec -it clarp claude auth login --claudeai
docker compose -p clarp-work exec -it clarp codex login --device-auth
docker compose -p clarp-work exec -it clarp gh auth login
```

Generate an import link after selecting the phone-reachable URL:

```bash
docker compose -p clarp-work exec clarp \
  clarp-admin onboard --url https://clarp-work.example.ts.net
```

Open the resulting one-time `clarp://pair?...` link on the iPhone. Clarp shows the
server name and URL for confirmation, connects with the supplied token, and
checks the stable server identity before saving it. Treat the link as a secret
because it contains the server bearer token.

Clone repositories under `/data/workspace`. This is the only writable
workspace root in the standard deployment.

```bash
docker compose -p clarp-work exec clarp \
  clarp-admin repos clone https://github.com/OWNER/REPOSITORY
```

Use `clarp-admin repos list` and `clarp-admin repos health REPOSITORY` for the
server-confined inventory. The clone destination cannot escape `/data/workspace`.

## Instance data

Compose creates one private named volume per Clarp container and mounts it at
`/data`. It is not the host home and is not shared with another Clarp server.

```text
/data/
├── clarp/       configuration, identity, SQLite, backups
├── claude/      Claude settings, login, projects, transcripts
├── codex/       Codex settings, login, sessions
├── git/         GitHub CLI and optional SSH configuration
├── skills/      imported and Git-backed personal skills
├── models/      Faster-Whisper and OpenAI Whisper checkpoints
├── media/
├── uploads/
└── workspace/   cloned repositories
```

Clarp-managed skills and application code come from the immutable image.
OpenAI Whisper, CPU PyTorch, Faster-Whisper, and ffmpeg are installed in the
standard image; model weights are selected and persisted per server.

## Multiple nodes and duplicate agents

Compose project names isolate containers, networks, and volumes:

```bash
CLARP_PORT=7682 docker compose -p clarp-work up -d
CLARP_PORT=7683 docker compose -p clarp-personal up -d
```

Both servers may contain a `rachel` session. The iOS identity is
`(server profile, session)`, so Work Rachel and Personal Rachel remain distinct.

## Upgrades

Application code is immutable inside the image. Upgrade by replacing the
container while retaining its named volume:

```bash
docker compose -p clarp-work pull
docker compose -p clarp-work up -d
```

CLI authentication, conversations, skills, models, media, and workspaces remain
in the volume. Never run two Clarp containers against the same writable volume.

Create and verify a consistent backup while the server is running:

```bash
docker compose -p clarp-work exec clarp clarp-admin backup create
docker compose -p clarp-work exec clarp clarp-admin backup verify /data/clarp/backups/FILE
```

Restore is staged rather than overwriting a live SQLite database:

```bash
docker compose -p clarp-work exec clarp clarp-admin backup restore /data/clarp/backups/FILE
docker compose -p clarp-work restart clarp
```

The entrypoint verifies and applies the archive before the server opens SQLite.
Backups contain CLI and peer credentials; they are mode `0600` but are not
encrypted. Encrypt them before copying them outside the Docker host.

### Restore needs exclusive use of the volume

A restore replaces the whole data volume, so it is applied only when the Host
owns the volume alone. The entrypoint holds two locks in `/data/clarp/backups`
(`host-owner.lock` for the running Host, `data-root-users.lock` for everything
it starts):

- `backup restore` stages only inside the running Host (`exec`, as above). It
  records the archive's exact size and SHA-256; an archive staged any other way,
  or changed after staging, is never applied.
- The archive is applied only by the Host's own start (the image's default
  command), only while no other Clarp process holds the volume, and only on a
  local filesystem (ext4, xfs, btrfs or zfs; others are refused unless
  `CLARP_RESTORE_ALLOW_FILESYSTEM` names that type). An archive that carries
  anything under `clarp/backups` is refused.
- A refusal keeps the staged archive and changes nothing. The entrypoint exits
  with 75 (another Host owns this volume), 76 (the volume is in use, or a
  restore elsewhere is still running), 77 (locking is unsupported here) or
  78 (the archive or its staging record is not acceptable).
- A failure once the apply has begun (`restore.applying` written) is not a
  refusal: it is reported as interrupted and incomplete, with the same exit
  codes, and the volume may be partially replaced. The marker stays, so it is
  recovered as below.
- On a filesystem where locking is unsupported, the Host still starts, with a
  warning that the one-Host guarantee is unavailable; only a restore is refused.
- An interrupted restore (the archive and `restore.applying` both still in
  `/data/clarp/backups`) is completed by the next Host start. Until then
  maintenance commands refuse with 76. If that volume's filesystem refuses
  locking (77), recover with every container on the volume stopped: move the
  volume to supported storage and start the Host, which re-applies the restore;
  or deliberately delete `restore.pending.tar.gz`, `restore.pending.owner` and
  `restore.applying`, accepting the partly replaced volume.
- The Host command takes no extra arguments; with extra arguments the
  entrypoint refuses (64) instead of starting a server without the Host's locks.
- `/data/clarp/backups/instance.generation` is this volume's instance
  generation: a random token every Clarp start creates once if it is absent
  (0600, owned by the container user) and never repairs. Every applied restore
  replaces it before anything is deleted; if it cannot be replaced, the restore
  is refused (77) and nothing changes. Never copy it between volumes or edit it.
  Deleting it is safe but makes the next start create a new one.

The locks only see Clarp processes started by this image's entrypoint. These
are unsupported while a restore is staged or applied, and nothing can detect
them: `docker compose run` against a running node (use `docker compose exec`),
`docker exec` into another container that mounts the volume, `--entrypoint`
overrides, other images or sidecars mounting the volume (including the `chown`
command below), and host-side access to the volume path or a bind mount.

After upgrading from an image without these locks, the old container holds no
lock. Before staging a restore, stop the old stack and make sure no other
container mounts the volume (`docker ps --filter volume=…`), then start the new
Host: `docker compose pull && docker compose up -d` already stops the old
container first; for a plain `docker run` install use `docker stop clarp &&
docker rm clarp`, then `docker run` the new image. Never start a new Host next
to a running old one.

## Tailscale

The optional sidecar gives the node its own Tailnet identity; the Docker host
does not need Tailscale installed. Create a restricted Tailscale auth key and
start both Compose files:

```bash
export TS_AUTHKEY='supply-this-securely'
export TS_HOSTNAME='clarp-work'
docker compose -p clarp-work \
  -f compose.yaml -f compose.tailscale.yaml up -d
```

The sidecar has its own small state volume because it is a separate container.
It shares only Clarp's network namespace, not `/data`. The Clarp port is not
published on the host in this mode. Keep Clarp bearer authentication enabled
even on a Tailnet.

Without `TS_AUTHKEY`, follow the one-time login URL printed by
`docker compose logs -f tailscale`; the sidecar keeps that request alive until
approval. With a restricted auth key, onboarding is unattended. Both modes
persist the node identity and Serve configuration in the sidecar volume.

Do not commit an auth key to Compose or an environment file. Prefer a
file-backed Docker secret for long-lived deployments. Userspace networking is
the default, avoiding privileged mode, capabilities, and `/dev/net/tun`.

### Host networking on an existing Tailscale machine

If the host machine is already authenticated to your Tailnet, you do not need to create an extra Tailscale device or auth key. Run Clarp directly on the host network interfaces:

```bash
CLARP_PORT=7684 docker compose -f compose.yaml -f compose.host.yaml up -d
```

This joins the host's existing `tailscale0` and LAN interfaces directly without Docker bridge NAT, avoiding firewall forward drops and port translation.

### Host port mapping over Tailscale (Bridge Mode without Sidecar)

If running in standard bridge mode with published ports (`CLARP_PORT=...`) instead of host networking or the sidecar, note that host firewalls (such as UFW and `ufw-docker`) drop Tailscale CGNAT traffic (`100.64.0.0/10`) to Docker bridge subnets by default.

Ensure the host firewall permits Tailscale traffic to reach the container:

```bash
# Allow ingress on the Tailscale interface
sudo ufw allow in on tailscale0 to any port $CLARP_PORT proto tcp

# Allow Docker forwarding from Tailscale nodes (100.64.0.0/10)
sudo iptables -I DOCKER-USER 2 -s 100.64.0.0/10 -j RETURN
sudo iptables -I DOCKER-USER 2 -i tailscale0 -j RETURN
```

## Personal skills

Preferred options, in order:

1. Clone a Git-backed skill collection beneath `/data/skills/git`.
2. Import a snapshot beneath `/data/skills/imported`.
3. For local development only, mount one narrow skill directory read-only.

Never mount the host's complete `.claude`, `.codex`, or home directory. Those
contain credentials, sessions, hooks, and mutable configuration in addition to
skills. A host symlink alone cannot cross the container boundary.

Commands:

```bash
clarp-admin skills import /imports/my-skill
clarp-admin skills source-add https://github.com/OWNER/skills --name personal
clarp-admin skills source-update personal
```

Imported and Git-backed sources persist in `/data`. Clarp reports obvious
host-specific paths as a portability failure and never overwrites a conflicting
skill already present in either CLI home.

## Cross-server agent messages

Configure peers explicitly; Tailnet discovery does not imply trust:

```bash
clarp-admin peers add work https://clarp-work.example.ts.net --token TOKEN
clarp-admin prompt --server work --to rachel \
  --from "$CLARP_SESSION" --text "Please review the branch."
```

The address is always `(peer, session)`. The peer token is stored with mode
`0600` inside this node's volume and is redacted from `peers list` output.

## Accessing host files and credentials

To allow container agents to work directly on host repositories and reuse existing host GitHub / Git authentication without re-authenticating, create a local `compose.override.yaml` (which is automatically ignored by git):

```yaml
# compose.override.yaml
services:
  clarp:
    # Run as your host UID:GID so file permissions match and Claude Code permits --dangerously-skip-permissions
    user: "1000:1000"
    tmpfs:
      - /tmp:size=512m,mode=1777
      - /home/clarp:size=64m,mode=1777
    environment:
      # Reuse host GitHub CLI authentication
      GH_CONFIG_DIR: /host/home/USER/.config/gh
    volumes:
      # Mount host filesystem
      - /:/host
      # Reuse host Git identity (name, email, helpers)
      - /home/USER/.config/git:/home/clarp/.config/git:ro
```

When switching an existing volume to a custom UID, ensure the data volume ownership matches (with every Clarp container on the volume stopped, and never while a restore is staged):
```bash
docker run --rm -v clarp_clarp-data:/data alpine chown -R 1000:1000 /data
```

Never mount the Docker socket (`/var/run/docker.sock`). Clarp reports update instructions but does not control Docker itself.

## Removal

Removing only the container preserves the server:

```bash
docker compose -p clarp-work down
```

Deleting its named volume destroys that Clarp server's credentials,
conversations, models, personal skills, media, and unpushed repository work.
Treat volume deletion as the Docker equivalent of `clarp-admin uninstall
--purge-data`.
