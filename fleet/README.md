# Compute fleet

A standalone, optional Clarp component. It does not restart or modify the Clarp Host.
A broker places bounded jobs on explicitly enrolled Tailscale/SSH peers. Agents use
`clarp-fleet plan` before submitting work. The Waybar badge opens a Quickshell panel.

## Execution and return path

1. The parent declares CPU/RAM/GPU, profile, immutable inputs, parent host/session/task,
   and a stable job ID. The broker admits against fresh telemetry and reservations.
2. SSH launches the peer supervisor. Compilation/rendering execute tools; `agent`
   launches the peer's already-authenticated Codex CLI with explicit model/effort.
   This is not creation of a remote Clarp persona or conversation.
3. Attempt IDs fence late results. Output, exit status and checksummed artifacts
   remain retrievable through the broker. Disconnects do not imply success or retry.
4. Add `--notify-parent` to `clarp-fleet submit` on a Linux parent Host to start
   a managed completion watcher automatically. It registers a durable Clarp job,
   heartbeats, honors cancellation, and records delivery. Alternatively run
   `python3 -m fleet.notify JOB --parent-agent SESSION` on the originating Host
   to wait and send an automation notice through that Host's supported Clarp API.
   A remote parent uses its own client credentials and local Clarp session.
5. The parent inspects `clarp-fleet status JOB`, downloads artifacts, then runs
   `clarp-fleet ack JOB`. Message admission is not acknowledgement or task acceptance.

The notifier retries transient broker read failures under the same job ID and
keeps its durable watcher heartbeat active. Authentication failures are terminal.
New notifications persist a stable Clarp client message ID before sending and
retry transient response failures with that same ID. The Host deduplicates admission
and queues the notice while the parent is busy. Legacy notifications sent without
a stable ID retain their uncertain status and require inbox reconciliation.
It never includes raw worker output in the wake prompt. Its Host/session ownership
check prevents routing a job to a different local parent. Host authentication scopes
jobs by parent host, not by individual agent within a trusted host.

## Commands

```sh
clarp-fleet peers
clarp-fleet plan --profile compile-cpp --file main.cpp=/absolute/main.cpp --cpu 2 --ram-mb 1024
clarp-fleet submit --profile compile-cpp --file main.cpp=/absolute/main.cpp --cpu 2 --ram-mb 1024 --id BUILD_ID --parent-agent SESSION --parent-task TASK
clarp-fleet wait BUILD_ID
clarp-fleet artifact BUILD_ID program --output /private/result/program
clarp-fleet ack BUILD_ID
clarp-fleet cancel BUILD_ID
clarp-fleet transcribe /absolute/pcm.wav --engine auto --json
```

For agent jobs use profile `agent`, file `task.md=/absolute/task.md`, model and
 effort explicitly. Provide source with `--repo PATH --commit SHA`; the 32 MiB
bundle cap requires small jobs. No automatic distribution of every existing agent
command, live agent migration, or provider quota scheduling is implemented.

Install from source with `python3 -m fleet.install --peer ID=local --peer ID=SSH_ALIAS
--listen TAILSCALE_IP --apply --start`. Re-running installs immutable component
releases; restart only `clarp-fleet.service` after checking no active work needs its
old configuration. Never use this as a reason to restart Clarp. Panel installation:
`python3 -m fleet.install_panel --apply`, then reload Waybar. The panel starts hidden.

## Boundaries and current limits

- Broker uses authenticated tailnet HTTP and SSH, local SQLite, bounded queues and
  input bundles. It is a single coordinator, not replicated/high-availability.
- Linux tool jobs use cgroup resource limits and bubblewrap. Agent jobs use their
  CLI sandbox; Whisper is a trusted dedicated process. These are different isolation
  levels. macOS enforcement is weaker; no Linux cgroup guarantee applies there.
- Placement is a transparent capacity heuristic, not a trained runtime predictor.
  Unknown outcomes retain reservations for reconciliation instead of redispatch.
- Interactive capacity is reserved for inference. GPU jobs reserve exclusive GPU
  admission; this does not stop unrelated programs from using the same hardware.
- GPU rendering is advertised only when the installed toolchain supports it.
  Private CPU FFmpeg and tested Whisper CUDA dependencies do not imply NVENC support.
- Whisper uses the existing small.en cache, final-result transcription and a warm
  model worker. Existing microphone/dictation hotkeys are not changed. No streaming,
  multilingual quality claim or latency percentile guarantee.
- Run the notifier as a durable parent-host background job for long work, with a
  generation-specific Clarp background handle. The helper alone is foreground.

Run `python3 -m unittest discover -s fleet/tests -v`. Real peer receipts, failures,
recovery and inspected private-compositor screenshots are separate deployment proof.
