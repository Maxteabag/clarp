# Provider visibility integration review (local only)

> Historical record. The Qt desktop client and the compiled reducer probe it
> describes have since been removed from this repository.

## SSE deduplication

Confirmed with live localhost `/events` SSE delivered to the **unchanged production Qt `BackgroundJobTracker.cpp`**, compiled as a standalone QCoreApplication with Qt 6.11.2. No window, desktop control, snapshot refetch, or iOS/Mac change was used.

The affected Qt surface is its active-job tracker (roster counts and process popover). The Updates list itself is populated only by snapshots, so this is not evidence that SSE directly duplicates Updates cards. AppController also rejects older in-flight refresh generations.

The pre-review candidate `4c9fc52` fails the new test: after explicit registration the reducer holds both `claude-task:…` and `sse-explicit`. AppController normally requests a canonical refetch, so this is transient when that fetch succeeds; it persists when refetch is delayed/fails. iOS AppModel does not add the event job directly: it invalidates its load generation/revision and refetches. Thus the proposed missing-invalidation hypothesis was too broad, but the Qt event projection defect is real.

Repair: the job store atomically records exact-identity native-view invalidations with explicit registration. The watcher projects matching native events as `status: superseded`, `projection_only: true`, with `replaced_by_job_id`. Existing Qt removes nonactive statuses immediately; existing iOS refetches as before. Later native events keep the same superseded projection. **The durable native job status, process ownership, cancellation, and result are untouched.** GET detail remains canonical. Contract 24 is still an unshipped candidate; its compatibility row documents the additive event semantics.

Receipts: `/var/tmp/solu-sse-qt-before.log` (reproduction failure), `/var/tmp/solu-sse-qt.log` and `.xml` (pass), and [captured live SSE](provider-visibility-review-evidence/sse-receipt.json). The reducer source harness is `tests/probes/background_job_reducer.cpp`; it links the repository's actual tracker implementation, not a reimplementation of its rules.

## Backlog and writer contention

Disposable fixture: 16 bound agents, 8 MiB of representative assistant JSONL each (134,221,120 total bytes), a concurrent managed-job registrar, and a pending managed event. It measures SQLite transaction spans and time to the in-process stream callback (not HTTP/network delivery); no inference or live transcript/process was used.

| Measurement | Pre-review candidate | First repaired sample |
|---|---:|---:|
| Bytes consumed in a pass | 67,113,280 | 524,960 |
| Pending managed event reached stream callback | 883.011 ms | 1.042 ms |
| Pass plus publication | 883.020 ms | 12.571 ms |
| Longest observed writer transaction | 64.083 ms | 0.517 ms |
| Longest concurrent managed registration | 943.457 ms | 7.073 ms |

Three subsequent repaired samples: pass 11.185–20.191 ms, pending-event delivery 0.813–3.197 ms, maximum writer span 0.431–2.387 ms, concurrent registration maximum 3.690–7.198 ms. [Before](provider-visibility-review-evidence/backlog-before.json), [after](provider-visibility-review-evidence/backlog-after.json), [three final samples](provider-visibility-review-evidence/backlog-final-three-runs.json). These are local fixture measurements, not production latency guarantees or a throughput comparison: the repair deliberately does much less work per visit.

The watcher now publishes managed events first. A retained observer rotates over at most two transcripts per provider tick. An unfinished roster sweep or backlog continues at the normal 250 ms watcher cadence; only a caught-up sweep returns to the five-second idle delay. This avoids imposing five seconds on every small catch-up batch. Each visit normally consumes at most 256 KiB, with a 20 ms cooperative parsing budget; an indivisible record can exceed those soft budgets but is capped at 4 MiB. A provider tick has a 50 ms cooperative budget checked between visits. File lookup/individual I/O cannot be preempted by these budgets. JSON, timestamp, notification and title parsing occur before the writer transaction. At most 64 job changes commit per visit, sharing the budget between normalized actions and stale reconciliation; large single records resume through a durable within-record action offset, without dropping the remaining actions. Compare-and-swap of the full cursor discards a stale parsed batch if another observer advanced it.

Measurement correction (retained rather than overwriting the first receipts): the first harness timed the SQLite trace callback for BEGIN through COMMIT, so those transaction spans include lock-acquisition wait. Instrumentation version 2 times **after BEGIN returns** through COMMIT and separately reports acquisition wait. Its paired rerun under ongoing local load measured: before, 2166.382 ms pending-event delay, 969.351 ms maximum lock hold, 13.250 ms acquisition wait and 2275.966 ms managed registration; after, 2.254 ms event delay, 0.246 ms maximum lock hold, 1.329 ms acquisition wait and 13.576 ms managed registration. [Corrected before](provider-visibility-review-evidence/lock-before-v2.json), [corrected after](provider-visibility-review-evidence/lock-after-v2.json). Scheduling/load varied, so these samples establish observed blocking and the effectiveness of bounded visits, not a claimed stable speedup ratio.

Tests prove nine continually-backlogged agents all receive a visit within five polls, no poll changes more than two cursors, a 130-action record commits as 64/64/2, 130 stale tasks also reconcile in capped batches, partial/replaced records remain safe, and parsing runs outside the write transaction. Schema 100's new cursor table gains its within-record offset before any deployment; live schema remains 99.

Reproduce with a fresh directory and explicit new `CLAUDE_PWA_DB` using `tests/probes/provider_backlog.py`; it refuses an existing database. The original fixture command and generated data are disposable; measurements above are retained.

## Packaging and activation

- `install.sh` copies `plugin/` and `server/lib/` into the same staged release, then atomically replaces `share/current`; `share/plugin` points through it. Installer tests compare the packaged hook, manifest, hook configuration and provenance module byte-for-byte with source.
- Read-only inspection of this Host found `share/plugin -> share/current/plugin`. The deployed `lib/deployment.py` and `lib/backend/claude.py` match this checkout byte-for-byte (SHA-256 prefixes `6cae297f3cda9042` and `9e99d9cae6aa980f`). The runtime status read reported release `90953b3b60a85799a34a7f6c`, `draining:false`; no runtime mutation occurred.
- A long-lived disposable process imports the old release's deployment module once, waits across atomic activation, and then resolves the same lexical plugin argument to the new release. A fresh hook subprocess with stale inherited PYTHONPATH resolves `_clarp_lib.py` to the new colocated job library. This test passes without restarting that process.
- The installed `clarp-agent-bg` wrapper reads `share/current/SERVICE_PYTHON`, sets `CLARP_CODE_ROOT` to current, and executes current's helper. It does not keep a prior inherited code root when current is valid.
- Claude `start_turn` launches one process per turn and closes stdin after one user message. On the next naturally admitted turn, even an older still-loaded Clarp runtime calls `plugin_dir()` again, supplies `--plugin-dir share/plugin`, and resumes the same native conversation. That fresh process loads the new hook and helpers; no forced fleet restart is needed for this path.
- Existing in-flight Claude turns are **not claimed hot-updated**. Official [plugin loading documentation](https://code.claude.com/docs/en/plugins/loading) says directory plugins load in place, but the loaded plugin set refreshes at session start or `/reload-plugins`. We did not invoke a paid Claude process or inject reload into anyone's session. Wait for the normal next turn and verify its actual provenance receipt after an approved rollout. A manually persistent CLI outside the one-turn Host path needs its own normal reload/new session.
- Runtime-affecting source changes also change the release fingerprint. Existing runtime-release tests verify a runtime remains intact while busy and drains only when idle; no global recovery pause or service/runtime restart was changed here.

## Validation and limits

Before the final full gate: 66 focused integration/unit tests passed (including actual versioned installer, runtime drain policy, release-pointer activation and live SSE reducer); 58 further focused tests passed after hard bounding large action batches. `git diff --check` passed. The final full gate removes `CLARP_HEARTBEATS_DISABLED` **only from its test subprocess environment**, runs under timeout 500, and sets the standalone reducer path so its proof test is actually executed rather than skipped. Host policy/environment is unchanged.

Final runtime-source commit tested: `d63a856a8c8cce0c877e2492f06e14bac14d1ff2`. All discovered test directories were covered, with unit files partitioned into two explicit lists. The manifest is `/var/tmp/solu-review-final2-manifest.json`; [committed gate receipt](provider-visibility-review-evidence/final-gates.json) retains source, summaries and JUnit counts.

| Final chunk | Result | Time | Receipt |
|---|---|---:|---|
| Unit A | 2472 passed, 1 baseline failure; 9 subtests passed | 180.32 s | `/var/tmp/solu-review-final2-unit-a.log` and `.xml` |
| Unit B | 2522 passed | 233.47 s | `/var/tmp/solu-review-final2-unit-b.log` and `.xml` |
| Integration, contract, API, hook | 374 passed, 1 skipped, 1 existing thread warning | 156.53 s | `/var/tmp/solu-review-final2-remaining.log` and `.xml` |
| JavaScript | 34 files, 344 tests passed | 14.26 s | `/var/tmp/solu-review-js-final.log` |

Combined Python: **5368 passed, 1 failed, 1 skipped**, plus 9 passing subtests. Every chunk completed within its timeout 500. The compiled Qt proof was enabled and ran; the skip is elsewhere. The sole failure is `test_module_state_guard.py::test_no_new_module_state`, listing pre-existing `_counts`, `_misses`, `_peer`, `_recent`, `_reported` in unchanged `lib/tool_explanation_commands.py`. It reproduces against untouched `aab0315` in the same sanitized environment (`/var/tmp/solu-review-base-sanitized.log`). Assertions and allowlists were not weakened. Therefore the full gate is **not green**, but the remaining failure is proven on base.

Earlier review runs are retained: one monolithic sanitized run reported 5363 passed/4 failed/1 skipped; its three timeout failures passed unchanged on both candidate and base in isolated retries. A pre-cadence partition run had four additional timing-sensitive unit failures; all passed in the final partitioned gate. These historical failures are not hidden or relabeled as successful executions.

A final read-only check still found live schema **99** and this agent's inherited `CLARP_HEARTBEATS_DISABLED=1`. Only child test environments removed that flag. The owned full-gate job truthfully ended failed because of the baseline guard. No live Host policy, account, deployment or runtime binding was modified. No push, deployment, paid inference, phone/device proof, iOS source change, Mac build, or forced fleet restart is included. Codex/arbitrary detached-service coverage and unsupported native cancellation retain the original report's limits.
