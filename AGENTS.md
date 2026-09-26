# Clarp Host

This repository is the Clarp server, PWA, and Tauri desktop shell.
The iOS app is a separate private repo (`clarp-ios`). Do not document
or duplicate iOS build/signing steps here.

## Agent instruction files

`AGENTS.md` is the only real file. `CLAUDE.md` and `GEMINI.md` are symlinks to
it, because the CLIs that work in this repo look for different names: codex,
grok and opencode read `AGENTS.md`, claude reads `CLAUDE.md`, gemini reads
`GEMINI.md`, and agy reads either `AGENTS.md` or `GEMINI.md`. Keeping one file
under three names means every backend sees the same rules and none of them can
drift.

Edit `AGENTS.md`. If a tool rewrites `CLAUDE.md` or `GEMINI.md` as a regular
file, the copies have forked — restore the symlink rather than syncing by hand:

```bash
ln -sf AGENTS.md CLAUDE.md && ln -sf AGENTS.md GEMINI.md
```

## Worktree closeout

After a pull request or branch is merged, remove its worktree as part of the
same closeout:

1. Fetch `origin` and verify the worktree commit is contained in `origin/main`.
2. Confirm the worktree is clean and that no agent or process is actively using
   it. Check more than process working directories: inspect Docker container
   mounts plus the Compose `working_dir`/`config_files` labels, and inspect
   systemd unit `WorkingDirectory`/`ExecStart` paths. A stopped container with a
   restart policy still counts as an active dependency.
3. Remove it with `git worktree remove <exact-path>` and then run
   `git worktree prune`.
4. Report the path that was removed.

Never force-remove a dirty, unmerged, active, or ownership-uncertain worktree.
Never remove a worktree referenced by a container, service, timer, or other
restartable workload. Redeploy that workload from a permanent checkout first,
then verify that no runtime metadata still points at the worktree.
Before abandoning an unmerged worktree, clean its generated build artifacts
when safe so `.build` and similar caches do not accumulate indefinitely.

## git checkout

Check for uncommitted changes before running `git checkout` on a path. This is a
shared working tree; if the file is dirty, the checkout destroys work that
exists nowhere else.

## A test run is green only when you read its result

`make test` / `pytest` is the gate before pushing. When you run it in the
background through a wrapper such as `pytest > log; echo "exit=$?" >> log`,
the wrapper exits 0 whether or not the suite failed, so a harness "completed
(exit code 0)" notification says nothing about the tests. Read the recorded
`exit=` line and grep the log for `^FAILED`, or run the suite in the
foreground. Two commits were pushed and deployed from a false green this way.

`tests/unit/test_dashboard_projection.py` and `test_model_fallbacks.py` are
load-sensitive under `pytest-xdist`: confirm a failure by running that test
alone before treating it as a regression, and say which it was.

## Push to main

Never end a turn by silently leaving the work you produced uncommitted. Once
the relevant gates above have passed, push it to main yourself. Always. Do
not open a pull request and wait, and do not raise a question asking whether
to commit — commit and push is the default action, not something that needs
asking.

Work in an isolated worktree so a shared working tree's other dirty files
never ride along; stage and commit only the files this turn's task actually
touched. Rebase onto the current `origin/main` before pushing, and fast-forward
(`git merge --ff-only` or an explicit `git push origin SHA:main`) rather than
opening a merge commit. If `origin/main` moved and no longer fast-forwards
cleanly, rebase again rather than forcing.

Report the change in your answer as usual: what you pushed, the commit, and
the evidence that it passed. A passing test run is not itself permission to
skip verifying the change does what was asked; say plainly what you checked.

## Fleet-map vision and decisions

Before changing fleet-map behavior, authoring prompts, visuals, or generated-source
contracts, read [the vision and decision index](docs/architecture/fleet-map/README.md)
and the linked accepted ADRs. The current direction is creative but not disruptive:
extend established concepts; do not interpret ordinary novelty as a redesign request.
New explicit owner instructions take precedence. Record lasting changes to the vision
as a new ADR and update the index; do not silently rewrite the rationale to match code.

## Client contract

`server/lib/server_identity.py` carries `HOST_CONTRACT` and
`MIN_IOS_CONTRACT`, the integers the iOS app uses to decide whether it can
talk to this Host. When you add or change anything a client may depend on
(an endpoint, a response field, an SSE event, a feature in `FEATURES`), bump
`HOST_CONTRACT`, give a new feature its number in `FEATURE_CONTRACTS`, and add
a row at the top of `docs/compatibility.md`. Bump `MIN_IOS_CONTRACT` only when
old apps genuinely stop working; it puts a red banner in front of every user
below it. `tests/unit/test_client_contract.py` fails when the table and the
constants disagree; keep them in step in the same commit.

## Client contract

`server/lib/server_identity.py` carries `HOST_CONTRACT` and
`MIN_IOS_CONTRACT`, the integers the iOS app uses to decide whether it can
talk to this Host. When you add or change anything a client may depend on
(an endpoint, a response field, an SSE event, a feature in `FEATURES`), bump
`HOST_CONTRACT`, give a new feature its number in `FEATURE_CONTRACTS`, and add
a row at the top of `docs/compatibility.md`. Bump `MIN_IOS_CONTRACT` only when
old apps genuinely stop working; it puts a red banner in front of every user
below it. `tests/unit/test_client_contract.py` fails when the table and the
constants disagree; keep them in step in the same commit.
