# Hardening sweep

Six issues arrived on 2026-09-03 from one external tester. Every one of them
was real, precisely diagnosed, and small to fix. What they have in common is
more useful than any single fix, because the same shapes will recur elsewhere
in the codebase. This directory holds the sweep that followed: per-theme
findings, the regression tests that pin them, and the assumptions worth
questioning.

The tests live under `tests/regression/` (pytest) and
`tests/state/regression-*.test.js` (vitest). A confirmed, unfixed bug is
pinned as a strict expected failure (`xfail(strict=True)` / `it.fails`) that
asserts the *desired* behaviour, so the suite stays green today and turns red
the moment someone fixes the bug without deleting the marker. Each carries the
evidence as `file:line` and a proposed fix in its docstring.

## The six shapes

| Issue | Shape | Where else it can hide |
| --- | --- | --- |
| #8 lockfile missing | The source tree is assumed to equal the repository. | Every `cp` in staging, every `git` call on a tarball, `.gitattributes` export-ignore. |
| #12 update remote | One fact recorded in two places, read from the one that is empty. | `install.json` vs files on disk, config vs DB, server vs client mirrors. |
| #10 401 as "unreachable" | A signal is collected and never read. | Counters in health records, `fetchErrors`, states that exist but no branch checks. |
| #13 tap-to-record | A flag is cleared before the guard that checks it. | Any function that both owns a flag and calls a guarded function. |
| #11 restart marker | A failure is detected and told to the wrong party. | Restart recovery, watchdog kills, classifier reasons that merge distinct events. |
| #14 SSML spoken aloud | A global assumption is applied to every engine because no engine can declare otherwise. | Adapter manifests, provider capability tables, backend feature flags. |

## Cross-cutting questions

- **Restart is not an edge case.** Deploys, updates, adapter installs and
  crashes all restart the server, and the server owns every child process.
  What survives a restart should be a decision per process kind, not an
  accident of `start_new_session` and the default `KillMode`.
- **Duplicated logic needs contract tests.** Remote sanitising exists in bash
  and Python; markup cleaning exists in Python, JavaScript and Swift. A shared
  corpus that all implementations must agree on is cheaper than the next drift.
- **Capabilities are declared, never assumed.** Anything that varies by
  engine, backend or platform belongs in a manifest with a safe default.
- **Every silent branch needs a log line.** The failure paths that took the
  longest to diagnose were the ones with no output on the failing branch.
- **Terminal states must be guaranteed, not hoped for.** A busy state in the
  log with no terminal follow-up is the fingerprint of most "the agent ignored
  me" reports.

See the per-theme files for findings, questioned assumptions and suggestions:

- `restart-lifecycle.md`
- `installer-updater.md`
- `pwa-client.md`
- `voice-pipeline.md`
- `dispatch-states.md`
