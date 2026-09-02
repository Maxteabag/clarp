# Clarp Host

This repository is the Clarp server, PWA, and Tauri desktop shell.
The iOS app is a separate private repo (`clarp-ios`). Do not document
or duplicate iOS build/signing steps here.

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
