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

## Uncommitted work needs a decision artifact

Never end a turn by silently leaving the work you produced uncommitted. If your
changes are still only in the working tree when you are ready to report, raise a
native question with the `clarp-decisions` skill's question helper and let the
user choose what happens to them:

```bash
clarp-agent-artifacts question "$CLAUDE_PWA_SESSION" \
  "Uncommitted: <short description>" \
  "<files changed> are still uncommitted on <branch>. What should I do with them?" \
  '[{"id":"commit","label":"Commit on this branch"},{"id":"branch_pr","label":"New branch and open a PR"},{"id":"leave","label":"Leave them uncommitted"}]' \
  --recommend commit --effort quick \
  --context "<what the change does and how it was verified>"
```

The helper accepts two or three options only, so offer the ones that actually
fit the situation; anything else (reverting, stashing, splitting the diff) is
reachable through the card's **Write my own answer**.

Scope it to the work of this turn. A shared working tree often carries unrelated
dirty files; name only what you touched, and say so in the context rather than
proposing to commit somebody else's work-in-progress. Report the change in your
answer as usual — the question rides alongside it and does not replace telling
the user what you did.

Do not block on the answer, do not resolve it yourself, and do not treat
silence, an expiry, or a discard as permission to commit or to revert. One
question per batch of related changes; check `clarp-agent-artifacts attention`
before adding another so you do not stack duplicates. Committing and pushing
still follow the usual rule that they happen when the user asks — the answer to
this question is that ask.

## Fleet-map vision and decisions

Before changing fleet-map behavior, authoring prompts, visuals, or generated-source
contracts, read [the vision and decision index](docs/architecture/fleet-map/README.md)
and the linked accepted ADRs. The current direction is creative but not disruptive:
extend established concepts; do not interpret ordinary novelty as a redesign request.
New explicit owner instructions take precedence. Record lasting changes to the vision
as a new ADR and update the index; do not silently rewrite the rationale to match code.
