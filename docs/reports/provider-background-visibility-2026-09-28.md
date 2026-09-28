# Provider-native background visibility: local candidate

This initial report is retained as history. The [integration review](provider-background-visibility-integration-review.md) supersedes its SSE projection, polling cadence, batch bounds and final validation details; its original authorization and proof limits remain.

## Observed omission

Read-only inspection of Bella's Claude 2.1.283 native transcript established:

- Native session `1ab7df52-76aa-423e-83b3-6eafc7ef8eb2`, Bash tool `toolu_01FdHka33VdcPRqDDyLBPdJ1` requested background execution at 2026-09-28T11:54:46.762Z.
- Its tool result at 11:54:47.110Z contained structured `backgroundTaskId=b3mni59xr` and an exact task output-file location. No PID was supplied.
- At 12:08:11.553Z a native queue-operation notification identified the same tool and task as stopped, with no completion record before the previous process ended. The subsequent user-role copy was a duplicate delivery.
- Existing transcript presentation handles Agent/Task subagents, but background Bash tasks never enter the durable job registry. A tool request alone cannot establish running work.

No Bella workflow, watcher, deployment, or live process was modified. The raw command and private output are deliberately absent from this report.

## Candidate implementation

The existing background-job watcher incrementally observes only Claude transcripts resolved from Clarp runtime bindings. Cursor and projection changes commit together. Launches older than the owner's recorded native binding are not adopted. Identity is agent + native session + tool-use ID, with a receipt-fenced task ID. It uses existing job detail, snapshot, agent counts, change events, and timeline APIs. Schema 100 adds a dedicated cursor table; Host contract 24 advertises the additive fields/semantics. Parent must reconcile version numbers if upstream moves.

Requests enter queued/launching. A structured receipt enters running (provider-reported, not OS-verified). Ten minutes without provider evidence or an ended runtime becomes queued/unknown, without manufactured heartbeats or failure. Exact native completion/failure settles the task; stopped/killed means unknown outcome. Replayed terminal jobs never restart. Running output is inspectable but is not liveness evidence. Ended runtime observation remains eligible for later completion records.

Provider tasks cannot be cancelled by this Host: can_cancel is false, and DELETE returns 409 without dispatch or signalling. This prevents stopping the deployment merely to stop its watcher. Native tasks are fenced from manual heartbeat, finish, progress/log registration, or re-registration.

The output path is derived from the indexed project, native ID, and task ID under `/tmp/claude-<uid>/<project>/<native>/tasks/`. Arbitrary paths in tool-result text are ignored. Regular-file checks, symlink boundaries, a 64 KiB tail, and common token/private-key redaction apply; command bodies and notification summaries are not persisted. Redaction cannot identify an arbitrary unlabeled secret. Unknown layout means unavailable output, never filesystem searching.

Polling runs every five seconds. Unchanged transcripts acquire no write lock and do not refresh job activity. Replay is bounded to approximately 4 MiB per transcript per poll, with partial-line retry and oversized-record skipping; no transcript is loaded wholesale. Existing history is recovered incrementally at startup. This also means a long historical backlog can delay discovery. Oversized lifecycle records above that cap are not imported. Persistent unknown tasks are retained rather than incorrectly declared terminal.

## Coverage and limitations

Claude background Bash with explicit run_in_background=true, structured backgroundTaskId receipt, and native queue-operation terminal notification is covered. Agent/Task helpers and assistant/user prose are excluded. Automatic timeout-backgrounding without the explicit flag, TaskOutput-only lifecycle evidence, and other provider/layout versions need additional representative probes.

Codex source currently converts tool events to transcript refresh/state changes. In this helper's actual code-mode execution, exec_command output includes a session_id, but it is nested in a functions.exec tool result; it does not supply a trusted OS process identity or a detached worker cancellation boundary. This candidate does not parse arbitrary printed session IDs into jobs. Codex and arbitrary systemd/nohup services retain explicit background-job registration. No paid inference was used.

Exact provider/native/tool metadata on an explicitly registered job suppresses its automatic mirror. The updated Clarp PreToolUse hook carries that non-secret identity through a shell-quoted environment export in `updatedInput`, preserving the original command and all other inputs. It returns no permission decision. The legacy registration helper now retains this provenance automatically only when agent/backend/live native binding match. A disposable test executes the actual hook, its emitted Bash command, and the real legacy helper; one explicit job remains in snapshot/counts without manually authored metadata. Missing/foreign/ended-binding provenance is rejected.

The official [PreToolUse contract](https://code.claude.com/docs/en/hooks#pretooluse-decision-control) documents input replacement and that permission rules are evaluated against the updated input. Host and plugin must be rolled out together. Historical launches using the old plugin and services that strip environment cannot be retroactively correlated safely; they can still show separate records. This does not use command/title guessing or process discovery. The actual paid Claude runtime consuming this hook response was not invoked; hook/shell/helper execution and documented provider contract are the evidence boundary.

No iOS changes or Mac builds were made. API/source evidence is not phone proof. No push, deployment, account/billing change, or recovery-pause change is authorized or performed.

## Validation

Disposable tests replay representative provider-shaped records into isolated SQLite databases and a real localhost HTTP server. They cover launch vs receipt, owner/purpose/elapsed/output/terminal APIs, runtime-end unknown, restart and replacement, partial/oversized records, stale and mismatched IDs, terminal replay, explicit correlation deduplication, helpers exclusion, cancel rejection, manual lifecycle fencing, redaction/path boundaries, transactional cursor rollback, and migration.

Checks completed so far:

- Focused unit suite after guard repairs: **105 passed** in 9.35s (`/var/tmp/solu-provider-final-unit.log`, machine-readable `/var/tmp/solu-provider-final-unit.xml`). Includes jobs, process projection, watcher, migrations, client contract, and new provider cases.
- JavaScript gate: **34 files, 344 tests passed** (`/var/tmp/solu-provider-js.log`). Installed dependencies only in this isolated worktree with `npm ci --ignore-scripts`.
- Initial broad focused run: six HTTP failures under parallel load. Five passed unchanged on serial recheck. The prompt-history authenticated-send failure independently reproduced against an archive of untouched parent HEAD (`/var/tmp/solu-provider-base-recheck.log`), which was removed afterward. It returned HTTP 409 after stale-inflight recovery; it is not a green baseline.
- A concurrent repeat of the new HTTP test timed out on the existing two-second `/agents/snapshot` request. Its assertions/timeouts have not been relaxed. Final isolated HTTP test **passed in 1.47s** (`/var/tmp/solu-provider-http.log`, XML alongside it). It starts the real BackgroundJobWatcher thread, observes fixture records automatically, and calls actual localhost job/snapshot/cancel endpoints; no direct observer calls bypass the watcher. Full Python outcome follows below.
- Bundled skill frontmatter validation and `git diff --check` passed.

The full gate is invoked with `timeout 500` and registered as the visible, owned job `solu-provider-full-gate`; its actual exit code and log (`/var/tmp/solu-provider-full-gate.log`) determine the result. A successful command launch is not a passing gate.


Full Python gate before the final guard repairs: **5307 passed, 48 failed, 1 skipped**, 9 subtests passed, 468.65 seconds, actual exit 1. Replaying all 48 failed nodes against untouched base produced **46 failures and 2 passes** in 52.38 seconds (`/var/tmp/solu-provider-base-failures.log`). The two candidate-only failures were the single-writer and backend-identity guards; both were repaired without changing assertions or allowlists and pass in the 105-test focused result. The remaining failures include existing recovery/heartbeat/goal tests under the unchanged global recovery pause, the existing module-state guard, and authenticated prompt-history HTTP 409. No global pause was modified. This is not a green full gate.

Final combined gate after the provenance hook and store-boundary fixes: **117 passed in 31.13s** (`/var/tmp/solu-provider-final.log`, `/var/tmp/solu-provider-final.xml`). This includes the automatic watcher-to-HTTP lifecycle, real hook-to-Bash-to-legacy-registration deduplication, wrong-owner/ended-binding provenance rejection, registry/process/watcher tests, migrations, client contract, backend capability guards, table-writer guards, and hook resolution. No assertion or allowlist was weakened. JavaScript remained unchanged after its 344-test pass. No further full-suite run is claimed.

Rollout review must retain these boundaries: source/API/hook-shell proof only; no paid provider inference, deployed Host, or phone proof. Roll out the Host and plugin together, inspect one real task end-to-end, and verify phone handling of can_cancel=false and unknown progress before describing this as deployed. Codex and arbitrary detached services continue to require explicit registration. Keep the existing global recovery pause intact.
