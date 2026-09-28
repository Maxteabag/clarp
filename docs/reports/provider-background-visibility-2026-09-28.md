# Provider-native background visibility: local candidate

## Observed omission

Read-only inspection of Bella's Claude 2.1.283 native transcript established:

- Native session `1ab7df52-76aa-423e-83b3-6eafc7ef8eb2`, Bash tool `toolu_01FdHka33VdcPRqDDyLBPdJ1` requested background execution at 2026-09-28T11:54:46.762Z.
- Its tool result at 11:54:47.110Z contained structured `backgroundTaskId=b3mni59xr` and an exact task output-file location. No PID was supplied.
- At 12:08:11.553Z a native queue-operation notification identified the same tool and task as stopped, with no completion record before the previous process ended. The subsequent user-role copy was a duplicate delivery.
- Existing transcript presentation handles Agent/Task subagents, but background Bash tasks never enter the durable job registry. A tool request alone cannot establish running work.

No Bella workflow, watcher, deployment, or live process was modified. The raw command and private output are deliberately absent from this report.

## Candidate implementation

The existing background-job watcher incrementally observes only Claude transcripts resolved from Clarp runtime bindings. Cursor and projection changes commit together. Identity is agent + native session + tool-use ID, with a receipt-fenced task ID. It uses existing job detail, snapshot, agent counts, change events, and timeline APIs. Schema 100 adds a dedicated cursor table; Host contract 24 advertises the additive fields/semantics. Parent must reconcile version numbers if upstream moves.

Requests enter queued/launching. A structured receipt enters running (provider-reported, not OS-verified). Ten minutes without provider evidence or an ended runtime becomes queued/unknown, without manufactured heartbeats or failure. Exact native completion/failure settles the task; stopped/killed means unknown outcome. Replayed terminal jobs never restart. Running output is inspectable but is not liveness evidence. Ended runtime observation remains eligible for later completion records.

Provider tasks cannot be cancelled by this Host: can_cancel is false, and DELETE returns 409 without dispatch or signalling. This prevents stopping the deployment merely to stop its watcher. Native tasks are fenced from manual heartbeat, finish, progress/log registration, or re-registration.

The output path is derived from the indexed project, native ID, and task ID under `/tmp/claude-<uid>/<project>/<native>/tasks/`. Arbitrary paths in tool-result text are ignored. Regular-file checks, symlink boundaries, a 64 KiB tail, and common token/private-key redaction apply; command bodies and notification summaries are not persisted. Redaction cannot identify an arbitrary unlabeled secret. Unknown layout means unavailable output, never filesystem searching.

Replay is bounded to approximately 4 MiB per transcript per poll, with partial-line retry and oversized-record skipping; no transcript is loaded wholesale. Existing history is recovered incrementally at startup. This also means a long historical backlog can delay discovery. Oversized lifecycle records above that cap are not imported. Persistent unknown tasks are retained rather than incorrectly declared terminal.

## Coverage and limitations

Claude background Bash with explicit run_in_background=true, structured backgroundTaskId receipt, and native queue-operation terminal notification is covered. Agent/Task helpers and assistant/user prose are excluded. Automatic timeout-backgrounding without the explicit flag, TaskOutput-only lifecycle evidence, and other provider/layout versions need additional representative probes.

Codex source currently converts tool events to transcript refresh/state changes. In this helper's actual code-mode execution, exec_command output includes a session_id, but it is nested in a functions.exec tool result; it does not supply a trusted OS process identity or a detached worker cancellation boundary. This candidate does not parse arbitrary printed session IDs into jobs. Codex and arbitrary systemd/nohup services retain explicit background-job registration. No paid inference was used.

Exact provider/native/tool metadata on an explicitly registered job suppresses its automatic mirror. The legacy helper does not supply these IDs automatically, so manually registered work without correlation metadata may still appear twice; guessing based on command/title would risk hiding independent work. This remaining gap must be considered before rollout.

No iOS changes or Mac builds were made. API/source evidence is not phone proof. No push, deployment, account/billing change, or recovery-pause change is authorized or performed.

## Validation

Disposable tests replay representative provider-shaped records into isolated SQLite databases and a real localhost HTTP server. They cover launch vs receipt, owner/purpose/elapsed/output/terminal APIs, runtime-end unknown, restart and replacement, partial/oversized records, stale and mismatched IDs, terminal replay, explicit correlation deduplication, helpers exclusion, cancel rejection, manual lifecycle fencing, redaction/path boundaries, transactional cursor rollback, and migration.

Final check commands and outcomes are recorded in the parent handoff after the test runs finish. Temporary logs: `/var/tmp/solu-provider-focused.log`, `/var/tmp/solu-provider-new.log`.
