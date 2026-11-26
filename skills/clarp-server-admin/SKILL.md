---
name: clarp-server-admin
description: Diagnose, update, roll back, and repair a Clarp installation. Use for server health, installation, upgrade, or managed-skill problems.
---

# Clarp Server Administration

The Host is the Rust one: one `clarp-host` binary run as the `clarp-host.service`
systemd user unit, installed by `server-rs/tools/install-host`. Everything it
keeps is under `~/.local/share/clarp-host` and `~/.config/clarp-host`.

Where an install of issue 935 or later runs it, `clarp-host.socket` holds the
Host's port and hands it to the service: while the Host restarts, a request waits
in the socket's queue for the next Host instead of being refused. The socket also
starts the service when a request comes and the Host is not running, so a Host
stopped by hand stays stopped only with its socket stopped too
(`systemctl --user stop clarp-host.socket clarp-host.service`). A rollback to a
release from before the socket takes the socket away.

Agents do not deploy, update or restart the Host themselves: not
`install-host`, not `install-from-release`, not `systemctl --user restart
clarp-host`. Push the change and ask whoever gave you the task to have it
shipped; the owner can still deploy himself. What follows is for reading the
Host's state, and for the owner or the release owner who installs.

Start with these; they only read:

```bash
clarp-admin paths                                     # where this installation keeps what
cat ~/.local/share/clarp-host/data/DEPLOYED_VERSION   # the release installed now
systemctl --user status clarp-host.service            # running, since when, restarts
journalctl --user -u clarp-host.service --since -1h   # what it said
clarp-admin experience --open                         # what users are running into
```

`clarp-admin doctor`, `backup create|verify|restore`, `update`, `rollback` and
`skills repair-links` belonged to the retired Python Host. On the Rust Host they
answer "not available on this Host yet (the Rust Host). Nothing was done." Use
the routes below instead, and never hand-edit a release under
`~/.local/share/clarp-host/releases`.

## Where the installation lives

- `~/.local/share/clarp-host/bin`: `clarp-host` and `clarp-shim` of the release
  installed now.
- `~/.local/share/clarp-host/data`: the state. `fleet.db` and the other
  databases beside it, each agent's own log under `agents/`, the apps' client
  log, `crashes/`, and `DEPLOYED_VERSION`.
- `~/.local/share/clarp-host/releases/<version>`: each kept release's binaries,
  unit and skills, with `current` and `steady` beside them (see the rollback
  below).
- `~/.local/share/clarp-host/toolchains` and `environments`: the agents'
  Claude Code and Codex, and the Python that runs the Host's own speech engine,
  as this Host copied them for itself.
- `~/.local/share/clarp-host/tools`: the agents' Clarp tools (`clarp-admin`,
  `clarp-goal`, ...). Each is a pipe to this Host, which answers the tools it
  has and says of the others that it does not have them yet.
- `~/.local/share/clarp-host/libexec/install-host`: a copy of the installer,
  for a machine with no checkout at hand.
- `~/.config/clarp-host`: `config.toml`, `env` (the owner's token, mode 0600),
  `tools.toml` (the agents' tools' token), `install.conf` (what the
  installation was told, so an update needs no options again), `relay.json`
  and `peers.json`.

`GET /server-info` says the release (`version`), the contract and the
features; `GET /host/start`, with the owner's token only, says how many agents
the Host has, how many are still opening and how many could not be opened, and
which run of the Host answers (`instance`) and how far its start has come
(`stage`: `starting`, `agents`, `started`; contract 210, feature
`host_start_readiness`). A Host answers `/server-info` and `/host/start` from the
first moment of its start, one agent's `/log`, `/live` and `/send` once that agent
is open, the roster (`/agents/snapshot`) and `/events` once every agent is open,
and everything else once `started`: a request is held, never refused. An install
counts only once the run that answered says `started`.

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

## Update a Host

An update is the same install again, by whoever ships releases (not an agent
on its own initiative): `install-from-release` once more, or, from a checkout
whose binaries were built by `server-rs/tools/build release`,
`server-rs/tools/install-host --start`. What the installation was told before
is kept in `install.conf`, so no option needs repeating; what is named now
replaces it, and `default` forgets it (`--push-relay default`).
`install-host --files-only` writes the files and asks systemd nothing, to look
at what would be installed. The full list of options is in the comment at the
top of `server-rs/tools/install-host`.

`--start` restarts a running Host. Turns that are running go on, each in a
systemd scope of its own. An update keeps the agents' Claude Code and Codex versions; to move
them to another version, name it (`--claude PATH`, `--codex PATH`).

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
debug or simulator build (`1`) or a test build opens nothing. From Android build
1154 (app commit 7d90af77) on, the Android app marks a build that is not a release
(`<code>-debug`, `<code>-emulator`), so only its release builds count; an older
Android build's debug and emulator builds still count. The log below still lists
every flash:

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

## The Host's databases and backups

The stores are SQLite in WAL mode. An agent's log and the key-value stores
carry their schema in `PRAGMA user_version`: a Host creates such a store it
does not find and refuses one whose
schema is newer than it reads, naming both ("has schema N, this build reads
schema M"). There is no downgrade: a rollback past a schema change is a fix
forward.

Only the installed Host opens its live stores. A turn's environment carries no
database path of the Host's, and the retired Python Host's variables
(`CLAUDE_PWA_DB` and its kin) are cleared from it. To try Host code from a
checkout, give it a scratch `--data` directory, never
`~/.local/share/clarp-host/data`.

`clarp-admin backup` is not available on the Rust Host. Never copy a live
store with `cp`: the file and its `-wal` move apart while the Host writes. For
a copy of one store, use SQLite's online backup into a private directory of
your own, and only when the owner asked for it:

```bash
sqlite3 ~/.local/share/clarp-host/data/fleet.db ".backup '/private/dir/fleet.db'"
```

## Skill links

The Host links the agents' skills when it keeps the machine's tools
(`install-host --accounts select`): each release ships its skills under
`releases/<version>`, and at its start the Host moves every link a release
before it left. `clarp-admin skills repair-links` is not available on the Rust
Host; a link that reads as outdated is put right at the Host's next start. With
`--accounts follow` or `none` the Host links no skills: whoever keeps the
machine's tools does.

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
(`supports_steer`).

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

## Reaching the Host from a phone

The Host listens on the loopback (`127.0.0.1:7690` by default); whatever the
phone reaches sits in front of it, as Tailscale does. Without Tailscale, a
Host is reached from anywhere through a relay its owner deploys on Cloudflare:
`relay/README.md` in the clarp repository says how to deploy it, write
`~/.config/clarp-host/relay.json`, turn it on (`install-host --relay on`) and
pair through it (`clarp-admin pair create --relay`). `clarp-host network relay
status`, with the owner's token in `CLARP_HOST_TOKEN`, shows the connection.

## Vendor CLI version blocks a resumed agent

A successful Host restart is not proof that a native agent resumed. Inspect its
actual transcript for errors such as a model requiring a newer Claude Code CLI.
The programs a turn runs are the unit's `CLARP_CLAUDE_PROGRAM`,
`CLARP_CODEX_PROGRAM` and their kin (`systemctl --user cat clarp-host.service`),
usually a copy under `~/.local/share/clarp-host/toolchains`. Check that
program, not the one on your own path; a user-local version check can differ.
Run only `--version` for this check; it does not prove a successful model
response.

An update keeps the versions the agents have. Moving them to another Claude
Code or Codex is an install with `--claude PATH` or `--codex PATH`, by whoever
ships releases; Claude Code must take `--thinking-display` (2.1.285 and later
do). Never run `npm ci` or `npm install` inside a toolchain the Host runs from:
it replaces packages under running turns. Keep running processes, credentials
and unrelated CLI versions intact, and verify an actual resumed response or
tool call afterwards, in the exact conversation.
