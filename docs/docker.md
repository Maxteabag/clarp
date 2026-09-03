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

The default port is published only on host loopback at
`http://127.0.0.1:7682`. Read the generated bearer token without printing the
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

## Explicit host workspaces

An advanced operator may bind a repository into `/data/workspace`, but this
grants Clarp agents write and delete access to that host directory. The normal
workflow is to clone inside the private volume, push a branch, and open a pull
request using `gh`.

Never mount the Docker socket. Clarp reports update instructions but does not
control Docker itself.

## Removal

Removing only the container preserves the server:

```bash
docker compose -p clarp-work down
```

Deleting its named volume destroys that Clarp server's credentials,
conversations, models, personal skills, media, and unpushed repository work.
Treat volume deletion as the Docker equivalent of `clarp-admin uninstall
--purge-data`.
