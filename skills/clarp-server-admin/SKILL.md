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

## Install a Host on a new machine

On Linux with systemd and glibc 2.34+ (x86_64 or aarch64), as the user the Host is to run as,
in that user's login session:

```bash
curl -fsSL https://raw.githubusercontent.com/Maxteabag/clarp/main/server-rs/tools/install-from-release | bash
```

It installs the newest `host-builds` pre-release (built from main), checks its
`SHA256SUMS`, and starts the Host as the `clarp-host.service` user unit on
`127.0.0.1:7690`. Options after `bash -s --` go to `server-rs/tools/install-host`;
running it again updates in place. It needs `curl`, `python3` and a systemd
user session, never uses sudo, and refuses root and macOS. Never run it on a
machine whose Host someone else owns or deploys.

## A Rust Host that crash-loops right after an install: roll back

When `clarp-host.service` keeps restarting right after an install (the restart count
in `systemctl --user show clarp-host.service -p NRestarts` climbs, or `coredumpctl
list` shows new core dumps of `clarp-host`), put back the last release that ran
steady instead of waiting for a fixed build:

```bash
server-rs/tools/install-host --rollback                 # from a checkout that has it
~/.local/share/clarp-host/libexec/install-host --rollback   # or the copy every install leaves
```

It says which release it left and which it started, and refuses, changing nothing,
when no release is known to have run steady or that release is the one installed.
"Steady" means current for at least 30 minutes, with the unit's restart count
unchanged and no core dump of the unit's in all the time it was current. It is
judged when the next install replaces a release, or on demand with `install-host --judge`.
`~/.local/share/clarp-host/releases/steady` names that release and how it was judged,
and pruning never removes it. A Host installed before this existed gets its first
steady release one install after the first install that keeps binaries.

A rollback puts back that release's binaries and its unit as they were installed.
The config, the data and the agents' tools stay as they are. If the release left
raised a store's schema, the older Host refuses that store ("has schema N, this
build reads schema M") and does not answer. A rollback cannot help then: fix
forward. A crash loop that has nothing to do with an install is not a reason to
roll back. Read its journal and core dumps first.

## What the user is experiencing

On a Rust Host, `clarp-admin experience --open` lists outstanding findings.
Use `--since 1h` to limit the time window and `--json` for stored rows. The
watcher reads late sends, stuck or paused queues, app crash reports and
crash events, failed sends, skill links, old decision cards, release-blocking
jobs, and backends whose turns go wrong (`backend_failures`, one per backend:
two or more turns of the last hour that failed, ended done with no reply or
note recorded in any audience (a hidden coordination reply counts as a reply),
or failed on sign-in, or every turn of the hour; usage limits, Stops and turns
ended from outside the Host do not count). Relaunch counts provide context for a
crash finding. A turn that failed with the kind `ended_from_outside` (Rust Host
contract 190, "The turn's process was ended from outside (SIGTERM)") was not the
tool's failure: another process sent its shim a signal, as a manual `kill` or a
service manager does. Look for who sent it; the Host never stops a turn by a
signal.

The watcher is off unless `[experience_watch] enabled = true` is set in
the Host configuration. Each finding's owner and the configured front
desk hear once on open and once on clear, from `experience-watch`. An owner
may be a list, and a recipient may be `session@host` for an agent of a paired
Host (delivered as `clarp-admin prompt --server host` does). The
front desk also gets one daily summary. Findings are stored even without
a configured recipient. See [the watcher configuration](../../server-rs/docs/experience-watch.md).

## Read why an app crashed

The Host keeps the apps' client log and crash reports whatever the diagnostics
setting says: errors, crashes, hangs and MetricKit reports for 7 days with the
30 minutes before them, everything else for 30 minutes (bounded, secrets cut).
Read it as JSON lines, oldest first:

```bash
clarp-admin client-log --since 2h --level error
clarp-admin client-log --device device_<id> --since 30m     # one phone, with context
clarp-admin client-log --event metrickit_report --since 7d  # crash reports (file names under crashes/)
```

Chat-view scroll flashes (the apps' scroll dashcam, Rust Host from main
after e98202b6): the apps send `scroll_flash` (level `warning` or `fault`; kept by its name
whatever the level) when a chat
jumps and comes back without a gesture. Its detail names the chat, the pane,
the viewport height, the rule (`jump_back` or `top_flip`), up to 60 changed
frames (`t`, `y`, `h`, `top`, `g`, and `d`, the frame's duration in ms; iOS names
the pane `uikit` or `swiftui`) and up to 30 nearby events (`reload`,
`insert`, `remove`, `height`, `live`). The Host keeps it 7 days like an error,
with up to 16 KB of detail. The experience watcher opens one finding per device
(`scroll_flash_<platform>`) on its first flash and tells the app's owner and the
front desk, again at most hourly while new flashes come, and clears it after 24
hours without one (see the watcher configuration). An iOS or Android device's
flashes count only from a released build number (four digits or more): an iOS
debug or simulator build (`1`) or a test build opens nothing. The Android app
sends its version code from every build type today, so its debug and emulator
builds still count. The log below still lists every flash:

```bash
clarp-admin client-log --event scroll_flash --since 1d
```

The owner's `clarp-admin pair list` also works on the Rust Host. Use
`--all` to include revoked devices. The full MetricKit payloads are the files
the `metrickit_report` events name, beside the state database in `crashes/`.

`clarp-admin pair create|list|revoke` answer only the owner. Inside an agent's
turn the tool speaks as the agent ("owner token required"); an agent the owner
asked to pair a device runs it as the owner, on the Rust Host:

```bash
env -u CLARP_TOOL_TOKEN \
  CLARP_HOST_TOKEN="$(sed -n 's/^CLARP_HOST_TOKEN=//p' ~/.config/clarp-host/env)" \
  clarp-admin pair create --name NAME --scope full   # prints the clarp:// link once
```

The link is single-use and carries the code: hand it only to the device, and
revoke a test pairing afterwards (`pair revoke DEVICE_ID`).

Setting `CLARP_HOST_TOKEN` by hand is the owner's mode: on a Rust Host with
`tools_owner_free` (contract 193) the tool says so (`X-Clarp-Owner-Mode`) and the Host
writes a log line for each such caller (tool, verb, parent process, directory), at most
one every five minutes with a count of the calls since. Use it
only for what the owner asked. Everything else an agent's tools do presents the token of
the agents' tools: a turn's `CLARP_TOOL_TOKEN`, or outside a turn
`~/.config/clarp-host/tools.toml` (`[server] kind, bind_addr, port, tool_token`, mode
0600, written by install-host, never holding the owner's token). A script that reads
`auth_token` from `config.toml` and calls the Host from no app and no turn is named in the
Host's log (`clarp-host: admin token without a turn:`), so it can be moved to that file.

## Which code may meet which database

Only the installed Host and runtime services upgrade a database, when they
start. Every agent turn carries `CLAUDE_PWA_DB` naming the live database, so
any Host code run from a checkout would otherwise open it: a script, a
`python -c`, a probe, a test outside the fixtures. Such a process that meets
a database below its own schema waits ten seconds for a service to upgrade
it and then refuses with both versions (`DatabaseOlderThanCode`). The wait is
spent once in a process; a second open is refused at once. The refusing
reader leaves the database file and its `-wal` untouched; the `-shm` index
beside them can change, and is created if a `-wal` had none. Logging does not
open the database at all, and importing Host code creates nothing. A Host or
runtime that is not one of the installation's own releases gets no grant
either, and if the database is below its schema it exits 78 at once. To work on a checkout, point `CLAUDE_PWA_DB`, the
log, config, share and cache variables at a scratch directory first.

The other direction is refused too. `clarp-admin update`, `clarp-admin
rollback`, `install.sh` and the Host at start-up all refuse code whose schema
is below the database's, naming both versions. `--allow-older-schema` on
`update` and `rollback` is the override for a deliberate rollback: older code
does not know the newer tables and columns, so restore a database backup that
matches the release if you stay on it. The override is recorded for exactly
that pair of versions (`schema-rollback.json` in the data directory) and
allows nothing else.

A Host that meets a newer database unasked exits 78 and is not restarted;
its message names the command that lets it run: `clarp-admin rollback
<its release> --allow-older-schema` (with the override, the release that is
already current can be named). After a failed install the release that
`install.sh` or `clarp-admin` puts back gets that override by itself, since
the failed release may have upgraded the database before it failed.

A bare `clarp-admin update` says what the channel resolved to and refuses it
when it is older than what is installed (the stable channel's newest tag can
be months behind an installed main). `--ref <ref>` installs what you name.

## Rehearse an additive state upgrade

Before deploying a migration, compare the candidate's `_SCHEMA_VERSION` with
the actual database's `PRAGMA user_version` and required columns. A database
previously opened by another feature branch can have a higher marker without
the new feature's columns. Do not lower the marker or assume version order alone
proves compatibility. Fix and test the candidate migration first.

### Choose the version number against every source, not just main

`_migrate` returns early when `PRAGMA user_version >= _SCHEMA_VERSION`, so a
number another branch already used means your migration **silently never runs**
on that host: the deploy succeeds, the tables are missing, and the feature fails
at runtime. Several agents work this repo in parallel and deploy feature
branches to the live host, so `origin/main` alone does not tell you what is
taken. Read all four before picking:

```bash
git fetch origin
grep -n '_SCHEMA_VERSION = ' server/lib/db_schema.py                       # your branch
git show origin/main:server/lib/db_schema.py | grep -n '_SCHEMA_VERSION = ' # merged
grep -n '_SCHEMA_VERSION = ' ~/.local/share/clarp/current/lib/db_schema.py  # deployed (the Python Host's install)
sqlite3 ~/.local/share/clarp/state.sqlite 'pragma user_version;'     # live DB (the Python Host's; a Rust Host keeps its own)
```

When first assigning a migration, choose a number above the versions already
used by these sources. An already-assigned candidate number can be retained if
no other source has used it; do not repeatedly increment it merely because the
candidate itself appears in the comparison. Add a migration guarded by
`if version < NN:`, and keep every statement `CREATE TABLE IF NOT EXISTS` so
re-applying is harmless whichever branch lands first. Re-check after every
rebase: a merge that lands mid-task can take your number. Verify after
deploying that `pragma user_version` advanced *and* the new tables exist --
a matching version number alone does not prove the migration ran.

Use the helper from this skill to migrate a new private backup, never the live
database. Choose an existing private directory for the output:

```bash
# clarp-admin paths names the Python Host's state.sqlite
python3 scripts/rehearse_state_upgrade.py \
  --source /path/from/clarp-admin-paths/state.sqlite \
  --server-root /candidate/checkout/server \
  --output /private/backups/new-rehearsal.sqlite
```

It uses SQLite online backup, refuses to overwrite output, and checks every
existing table's original columns and values after migration. Exit zero proves
an additive upgrade on that snapshot, not a deployment. Intentional data
transformations need their own validation; this helper reports them as changes.

For a migration that intentionally inserts catalog or seed rows, opt in only
the expected existing tables with repeatable `--allow-added-rows TABLE` flags:

```bash
# clarp-admin paths names the Python Host's state.sqlite
python3 scripts/rehearse_state_upgrade.py \
  --source /path/from/clarp-admin-paths/state.sqlite \
  --server-root /candidate/checkout/server \
  --output /private/backups/new-catalog-rehearsal.sqlite \
  --allow-added-rows janitor_trigger_definitions
```

The default remains strict. Each allowed table retains a complete row multiset
in memory across all original columns, including generated columns, and must
preserve every original value, type and duplicate count. Only additional rows
are accepted; changed/deleted rows and removed tables/columns still fail.
Unknown table names fail before migration. The JSON `allowed_added_rows` field
reports `before`, `after`, `added` and `missing` counts per allowed table, or a
schema error if comparison is impossible. It never prints the row contents.

Keep backups private. Release rollback does not automatically undo a database
migration. Prefer the supported `clarp-admin update --ref FULL_SHA` once the
candidate is verified; do not hand-edit generated releases.

For an authorized update that must survive the HTTP connection restarting, run
`bash scripts/update_with_job.sh SESSION FULL_SHA PRIVATE_STATE_DIR` in an owned
`systemd-run --user` unit with a private append log and the normal CLI PATH.
Use `--dry-run` first to inspect its target without starting a job. Keep the
script in a permanent location outside the generated release being switched.
Invoke it with `/usr/bin/bash` in the unit too: managed copies may not have an
executable bit. Resolve symlinks before choosing the helper path; a dotfiles
skill link can still point into the generated release.
It heartbeats a process-fenced job and records `installer-exit`; job completion
means the installer finished, not that phone/runtime verification is complete.
Once installation starts, the installer owns rollback; cancelling the tracking
job is not an emergency stop for the installer. Verify the deployed SHA, schema,
runtime availability, and `clarp-admin doctor` afterwards.

## Host, runtime and plugin activation are separate

The supported installer restarts the HTTP Host and enables the runtime service;
it does not force-restart an already-running runtime. Current `runtime.py` starts
`RuntimeReleaseMonitor`, which watches the installed `RUNTIME_RELEASE_ID` once
`RUNTIME_READY` and `INSTALL_OK` exist. In the default natural mode
(`[runtime] release_drain = "natural"`, or `CLARP_RELEASE_DRAIN=natural`) a busy
runtime holds nothing: it waits (`drain.phase` `waiting_idle`, live `blockers`)
for a moment with nothing to cut short and fences only for the seconds of the
seal check. With `release_drain = "fenced"` it fences admissions (new work
queues durably, in-flight turns finish), hands over when nothing is owned, and
lifts the fence after 15 minutes with a growing backoff; see
`docs/runtime-restarts.md` "Graceful release drain". Runtimes from before the
drain hand over only when fully idle; runtimes from before natural mode always
fence. Verify the mechanism in the candidate
and deployed source.

HTTP endpoints and schema migrations activate with the Host restart. Runner
code becomes active after the natural idle handoff; installer success alone
is not proof of that transition. Read the runtime status `release_id`,
`draining` and `drain` fields and compare with the installed release. In
`GET /status` (contract 62 on), `runtime.draining` is true from the admission
fence on and `runtime.sealed` is the moment before the handoff; `available:
false` with a `reason` means the runtime did not answer (unknown, not idle).

The standard Claude runner launches one CLI process per turn. Its plugin path
resolves through `share/plugin -> current/plugin` on each launch, so the next
naturally admitted turn can load the new plugin even while the runtime process
still uses the predecessor's compatible runner code. Hook subprocesses resolve
the library colocated with their plugin, and managed helper wrappers resolve
`current` rather than retaining an inherited old code root. Do not claim that
an already-running Claude turn hot-reloaded its plugin; verify an actual
provenance receipt on subsequent natural work. Directory-plugin caching and
manual persistent CLI sessions need their own verification.

Never force a runtime or fleet restart to make a rollout look complete. A manual
restart interrupts in-flight turns and requires explicit authorization. Preserve
an existing recovery pause, active conversations, account selection, and rollback.

Runtime startup performs no generic assistant continuity checks. The agent
performing an authorized planned restart owns targeted resumption: capture the
exact unfinished work/native-thread inventory first, checkpoint its own handoff,
then recheck each target after activation. Prompt only genuinely stranded work
directly from the verified owner, preserving the original task and limits. Do
not prompt busy agents, valid queued continuations, legitimate waits, completed
work or idle conversations. Verify an actual native response/tool call afterward.
See the Host's `docs/runtime-restarts.md`. Unexpected crashes retain visible
interruption markers; explicitly enrolled goals and user/peer queued work keep
their independent ownership and recovery rules.

## Codex chat turns on a lane (Rust Host)

On a Rust Host built from issue 742 on, a Codex agent's chat turns can run on
one long-lived `codex app-server` per agent (its lane, under the shim)
instead of a `codex exec` per message. Then a user's message reaches the
running turn (`turn/steer`), Stop interrupts it (`turn/interrupt`), and a turn
starts without launching Codex. Three top-level keys in the Rust Host's own
`~/.config/clarp-host/config.toml`, read at each turn (no restart, no build):

```toml
codex_chat_lane = true        # off by default; false rolls back to exec
codex_lane_idle_minutes = 15  # a lane with no turn and no running goal leaves
codex_lane_most = 8           # live lanes kept; the longest idle one leaves first
```

A command a lane turn started in the background ends with that turn, as with exec. Start
something that must outlive the turn as a detached process (`setsid`, `nohup`), or as a
tracked job (`clarp-background-jobs`).

Turning it off is the rollback: each lane leaves before the agent's next exec
turn, and exec continues the same Codex thread. A lane is
`<run>/<agent>-cxg.sock`. Its `Inspect` shows the thread, the claim, the model and
effort Codex keeps, and `idle_ms`. A lane turn's Clarp tools take their turn's
identity from `CLARP_PROVIDER_TURN_FILE` (`<run>/<agent>-turn.token`), which the
lane rewrites at each turn. The apps do not yet advertise Codex steering
(`supports_steer`); the Python Host steers Codex in its own way.

## Claude chat turns on a kept process (Rust Host)

The owner switched the Claude lane on for both of his Rust Hosts on 25 Nov 2026. On a Rust Host
with `claude_chat_lane = true`, a Claude agent's
chat turns after the first can run on one kept `claude -p --input-format stream-json` per agent
instead of a new `claude -p --resume` per message. It is off unless the owner sets it, and
switching it on for the owner's agents is his decision. A top-level key of the Rust Host's own
`~/.config/clarp-host/config.toml`, read at each turn; the idle time
(`codex_lane_idle_minutes`) and the limits are shared by every lane. The limits follow the
machine's free memory unless fixed (`lane_limits = "auto"`, the default): a lane starts while
`MemAvailable`, less the new lane, stays above `lane_reserve_percent` (20) of the memory, however
many lanes run (from Rust Host contract 186 auto has no count; `lane_ceiling` is read, never
written, and bounds
nothing), and idle lanes leave on memory pressure. Setting the count or the budget
from an app switches the Host to fixed with that value (feature `lane_auto_limits`,
Rust Host contract 180; the apps read and set these through `/lane-settings`). No turn waits for the machine's
pressure (from Rust Host contract 194; contracts 186 to 193 held the turns nobody waited on while
io, cpu or memory pressure was high, `turn_pressure_admission`). `GET /lane-settings` shows `live`
(the lanes that live, idle and busy, per tool) and `cold_starts_last_hour` (turns that started a
tool anew, per tool; feature `lanes_live`): look there first when helpers seem slow to start or
the machine is loaded. The memory reserve is what protects the machine: a lane starts only while
`MemAvailable` stays above `lane_reserve_percent`, and idle lanes leave when memory runs short; no
turn waits for it. With `lane_limits = "fixed"`,
the count of lanes (`codex_lane_most`, 8) and the memory budget (`lane_memory_mb`, 2,200 MB)
bound them instead:

```toml
claude_chat_lane = true       # off by default; false rolls back to a process per turn
```

No turn waits for the lane: a turn with no lane there for its conversation starts its Claude Code
as the lane and runs on it, so a busy agent's queued turns, and a first turn, start one process
between them (issue 833). A turn for which no lane may start (no place, or one nobody waits on
under pressure) runs in a process of its own, and a lane may start once it is gone. Stop is Claude
Code's own interrupt and keeps the process; steering is as before. A background Bash command of
a lane turn can run on after the reply, but ends whenever the lane leaves (idle, a change of
model, effort, identity, MCP servers or account, an error or compaction, a terminal, a move):
start work that must outlive it with `clarp-background-jobs`. A lane is `<run>/<agent>-cll.sock`;
its `inspect` shows the session, the claim, `claims` and `idle_ms`. Turning the key off is the
rollback: the lane leaves before the agent's next turn.

Who keeps a lane place when all are taken (every tool's lanes, the owner's rule of 25 Nov 2026):
the owner's agents first. A lane is the owner's when its agent had a turn the owner sent (the
Oracle's included) that started or ended in the last 30 minutes, or one of his apps watches its
chat live; every other lane (helpers, janitors,
one agent asking another) is the agents' own. Idle lanes of the agents' own leave first, then idle
lanes of the owner's, longest idle first; a busy lane never leaves. A lane of the agents' own never
takes an owner's place: when only his are idle it does not start, and its agent runs each turn as
a process of its own, which is no slower than before lanes. A Codex goal's lane always starts.

## Reading the lane settings (Rust Host, feature `lane_settings`)

Only the owner changes the lane settings. Today he does that by editing the keys above in the Host's
`config.toml`, or through `POST /lane-settings` with his own token. The apps do not show these
settings yet. A "Kept processes" section in each Host's settings is coming to the iOS, Android and
desktop apps. Do not change the settings for him, by the file or by the route, unless he asked you
to. An agent may read them, on a Rust Host whose `GET /server-info` lists `lane_settings` in
`features` (contract 178):

```bash
curl -s -H "Authorization: Bearer $CLARP_AUTH_TOKEN" "$CLARP_SERVER_URL/lane-settings"
```

It answers each tool's switch (`codex_chat_lane`, `claude_chat_lane`, `muse_chat_lane`,
`opencode_chat_lane`), `most`, `memory_mb`,
`idle_minutes`, their `ranges`, and `held`: the lanes that run now, in total and per tool
(`lanes`, `busy`, `memory_mb`), from each lane's own memory reading, with `unanswered` for lanes
that did not answer within about two seconds. Reading it starts no lane.
`POST /lane-settings` is refused to an agent's token (`403`) and to any request that carries a
turn's `X-Clarp-Turn`. The shapes are in the Host's `docs/protocol.md`, "The owner's settings of
lanes".

## Muse chat turns on a lane (Rust Host, not built into any release yet)

On a Rust Host built with issue 762's Muse lane (check that `clarp-host` knows
`muse_chat_lane` before you rely on it), a Muse agent's chat turns can run on one
kept `muse serve` per agent (MSP over stdio, under the shim) instead of a
`muse exec` per message. It is off unless the owner sets, in the same
`config.toml`:

```toml
muse_chat_lane = true         # off by default; false rolls back to exec
```

The idle time (`codex_lane_idle_minutes`), the count (`codex_lane_most`) and the
memory budget (`lane_memory_mb`, PSS of all lanes together) are shared with every
tool's lanes. A Muse lane weighs about 255-300 MB.

- A conversation's first turn runs on exec, which names the session. The lane
  starts beside it without the session and takes it once exec has exited, or just
  before the turn queued behind exec, which then runs on the lane (issue 833). No
  turn waits for a cold lane. The turns after run on the lane.
- A message sent while a lane turn runs is steered into it (`turn/steer`) and
  enters the conversation when Muse reads it. Words Muse has not read when the
  turn ends, or when a Stop comes, are taken back and run as a turn of their own.
- Stop interrupts the turn (`turn/interrupt`) and keeps the lane.
- Muse refuses a second process on a session it holds. Before any exec of the
  agent's session (the switch turned off, an opening turn), the lane leaves,
  waited for at most 15 s; a lane that does not let go fails that turn in words.
  A terminal on the agent and a move do the same.
- Muse's reminder agents are off on the lane as on exec, as the owner chose
  (decision-d4c9cad82138ed99698f3691), until the lane's switch-on card asks him
  again. Were they on, a lane turn would still end for the user at its answer.
- A lane is `<run>/<agent>-mus.sock`. Its `inspect` shows the session, whether it
  holds it (`loaded`), the claim and the model it gave Muse. A lane turn's Clarp
  tools read their turn's identity from `CLARP_PROVIDER_TURN_FILE`.

## OpenCode and DeepSeek chat turns on a lane (Rust Host)

On a Rust Host built from issue 763 on, an OpenCode or DeepSeek agent's chat turns can run on
one kept `opencode serve` per agent instead of a new server per turn (about 1.4 s less before
each turn's first output). One top-level key in the same `config.toml`, read at each turn:

```toml
opencode_chat_lane = true     # off by default; false rolls back to a server per turn
```

The idle time and the limits are shared with every tool's lanes (above); a kept OpenCode serve
weighs about 590 MB. The owner switches it from his apps too (`POST /lane-settings`). An agent's first turn, and
any turn whose configuration changed, runs on its own server while the lane starts beside it.
A lane is `<run>/<agent>-ocl.sock`; its `inspect` shows the session, the serve's pid and version,
and its memory. It runs the Host's `CLARP_OPENCODE_PROGRAM` and keeps only a 1.x serve. A change
in the words of the agent's `opencode.json`, the global OpenCode configuration or `auth.json`
starts a new lane. A message sent while a lane turn runs is read into that turn, as on a Codex
lane (issue 784): OpenCode reads it at its next step and answers it in the same turn. Words it
has not read when the turn ends, or a Stop ends it, run as a turn of their own, unless OpenCode
begins them by itself as the turn ends (then the turn stays open, and they are answered in it);
a turn on its own server takes none.

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

## Vendor CLI version blocks a resumed agent

A successful Host restart is not proof that a native agent resumed. Inspect its
actual transcript for errors such as a model requiring a newer Claude Code CLI.
Read the runtime process's PATH and resolve the executable using that PATH; a
user-local version check can differ from the managed one, and an earlier Host
`bin/claude` entry can shadow a hash-pinned toolchain wrapper. Run only `--version`
for this check; it does not prove a successful model response.

The canonical managed pins and upgrade procedure are in `toolchain/README.md`.
Build a new hash-addressed toolchain with `scripts/install_agent_toolchain.py`;
never run `npm ci` in an active prefix to repair one vendor, because it also
replaces the other vendor's packages. Keep running processes, credentials and
unrelated CLI versions intact. Coordinate ownership before switching executable
routing, recheck its current target immediately before mutation, and retain a
rollback receipt. A newer competing route is a reason to reconcile, not overwrite.
A runtime restart is not needed merely to make a future child resolve a changed
executable path, but existing child processes keep their current binary. Verify
an actual resumed response/tool activity separately, in the exact conversation.
