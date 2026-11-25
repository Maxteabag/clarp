# Clarp Host

This repository is the Clarp Host (in Rust, `server-rs/`), the web app, the
Rust desktop app and the Tauri desktop shell.
The iOS app is a separate private repo (`clarp-ios`). Do not document
or duplicate iOS build/signing steps here.

## The Python Host is retired

The Host is the Rust one in `server-rs/`. The Python Host (`server/`, its tests,
installer and Docker image) was retired and removed from the repository; git
history keeps it. Comments that name a `server/...` file say where a rule came
from; that file is no longer in the tree.

## Deploying

Do not deploy, update or restart the Clarp Host yourself
(`server-rs/tools/install-host`, `clarp-admin update`, restarting the
`clarp-host` unit). Push to main as described below and ask whoever gave you the
task to have it shipped. Peter can still deploy himself.

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
3. Run `server-rs/tools/build drop` from it first: a copy and target it built on
   the build machine are 5 to 14 GB, and that disk is shared. Where it built
   nothing, drop removes nothing.
4. Remove it with `git worktree remove <exact-path>` and then run
   `git worktree prune`.
5. Report the path that was removed.

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

## Tests run once, at the end of a piece of work

Peter, 21 Nov 2026: "Remove all tests from the pipeline. We will do one single
test at the very end when [the work is] 100% done ... Avoid making tests the
bottleneck in developing." On 10 Nov: "I don't want tests to run automatically.
Ever."

- While a change is being built, do not run test suites, gates, mutation or
  planted-rule runs, or CI checks on each change, push or pull request. Write
  the tests a change needs; do not run suites to gate it. A quick run of one
  test to understand a failure you are fixing is fine; it is not a gate.
- A change lands once it builds and a reviewer has read it. Review is by reading.
- No workflow starts by itself on a push or pull request; they run only when
  someone starts them ("Run workflow", or `gh workflow run`). Main's
  `desktop.yml` still publishes the desktop binary, and main's `host-release.yml`
  the Host's Linux builds (the `host-builds` pre-release that
  `server-rs/tools/install-from-release` installs): those are releases, not tests.
- When the owner of the work considers it complete, run the one final pass
  (`server-rs/tools/final-pass`, with the tests of what changed), fix what it
  finds, and only then install it on Peter's Hosts, which are live.
- Do not run the whole Rust suite locally: forty agents doing so overload the
  owner's laptops.

When you run a test command in the background through a wrapper such as
`pytest > log; echo "exit=$?" >> log`, the wrapper exits 0 whether or not the
suite failed, so a harness "completed (exit code 0)" notification says nothing
about the tests. Read the recorded
`exit=` line and grep the log for `^FAILED`, or run the suite in the
foreground. Two commits were pushed and deployed from a false green this way.

## Push to main

Never end a turn by silently leaving the work you produced uncommitted. Once
it builds and a reviewer has read it, push it to main yourself. Always. Do
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
who reviewed it. A passing test run is not itself permission to
skip verifying the change does what was asked; say plainly what you checked.

## Fleet-map vision and decisions

Before changing fleet-map behavior, authoring prompts, visuals, or generated-source
contracts, read [the vision and decision index](docs/architecture/fleet-map/README.md)
and the linked accepted ADRs. The current direction is creative but not disruptive:
extend established concepts; do not interpret ordinary novelty as a redesign request.
New explicit owner instructions take precedence. Record lasting changes to the vision
as a new ADR and update the index; do not silently rewrite the rationale to match code.

## Client contract

`server-rs/crates/clarp-host/src/edge.rs` carries `HOST_CONTRACT` and
`MIN_IOS_CONTRACT`, the integers the iOS app uses to decide whether it can
talk to this Host. When you add or change anything a client may depend on
(an endpoint, a response field, an SSE event, a feature in `FEATURES`), bump
`HOST_CONTRACT`, add a new feature to `FEATURES`, and add
a row at the top of `docs/compatibility.md`. Bump `MIN_IOS_CONTRACT` only when
old apps genuinely stop working; it puts a red banner in front of every user
below it. `server-rs/tools/tests/test_rust_contract.py` fails when the table and the
constants disagree; keep them in step in the same commit.

## A feature agents can use ships with its skill

Anything an agent could use (an endpoint, a CLI command, an artifact kind, a
catalog element, a setting an agent may change, an app that starts drawing
something) ships with the matching Clarp skill under `skills/` added or updated
in the same pull request, so agents know about it the day it lands. Nine release
skills once never reached any agent; this rule is what prevents a repeat.

- The skill says what exists now and where it works: which apps draw it, which
  Host contract or setting it needs. An agent must never publish something no
  app can show, or call something no Host has.
- A feature that is switched off, or that no app draws yet, is either left out
  of the skill or described as not usable yet, with what to use instead.
- When the feature lives in another repository (the iOS app), the skill change
  is its own pull request here, linked from the app's, and merged no later than
  the app's release. The app's reviewer refuses the app change without that link.
- A new skill gets its line in `skills/manifest.json` in the same change, so
  every Host links it for every backend.
- A reviewer refuses a change that adds or alters such a feature without the
  skill text. A purely internal change, one that adds or alters nothing an agent
  can call, publish, read or set, needs no skill.

## Every issue has an owner and a target date

Peter, 22 Nov 2026, after 30 real issues had lain unowned for weeks: "Hadde vi glemt disse
GitHub issuene? Hvorfor var de ute?" Found it, filed it, moved on is not allowed.

- When you file an issue, give it an owner and a target date in the same step: the label
  `owner:<agent session>` (the agent who will fix it, e.g. `owner:maximus-6c37`; all agents act
  as one GitHub account, so an assignee names no one) and a line `Target: YYYY-MM-DD` in the body.
  If you can't name an owner, it goes to the area's owner (the Rust Host, iOS, Android, the Rust
  desktop) and you tell them in the same turn.
- An issue that won't be done is closed at once, with a one-line reason; not left open.
- A missed target date is moved, with a comment that says why, or the issue is closed.
- A daily check messages about any open issue without an owner label, or past its target
  date, for more than 3 days: the owner, or for an unowned issue its area owner (below). The
  front desk hears only about issues no area owns. It is the `clarp-issue-owners` timer
  in the owner's dotfiles, running on the EliteBook at 09:30.
- Area owners, when no one else takes it: the Rust Host `owner:maximus-6c37`; iOS
  `owner:arnie-85ef`; Android `owner:ethan`; the Rust desktop `owner:jasper-050b`; everything
  else in this repository, and issues filed by a person rather than an agent, `owner:arnold`
  (the release owner), who triages them.
- Create the label if it doesn't exist yet (`gh label create owner:<session>`). An owner whose
  session is archived or gone hands the issue to the area owner; the release owner moves such
  labels when he finds them.

## Code describes itself, never other projects

Code, comments, tests, commit messages and docs say what Clarp does. They never name
another product or project as the source or model for it ("like X's pattern"). Write
the behaviour instead. Peter's rule of 23 Nov 2026.

## The web app is deprecated

Peter, 22 Nov 2026: "Web appen er egentlig deprecated." The browser web app (web/ and the
static it builds) gets no new features. It keeps working as it does today, his old browser link
included, until he says to remove it; fix it only when something that worked breaks. New client
work goes to the iOS app, the Android app and the Rust desktop app (desktop-rs/).

## The Host in Rust (`server-rs/`)

`server-rs/` is the rebuild of the Host in Rust (Peter's go of 14 Nov 2026). Read `server-rs/REBUILD.md` before you
touch it: it names the lead, the rules, and who owns which crate. Its Rust compiles on the ai-swarm build VM through
`server-rs/tools/build` (its default host since Peter's order of 25 Nov 2026: all heavy work runs on the VM, never on
his machines; the VM is untrusted, so it gets source only and never a secret; skill buildbox) and in the `Server (Rust)` workflow,
which runs only when started by hand. The
rule in the next section still holds on elitebook and laptopstudio, for `server-rs` too.

## The Rust desktop compiles only in GitHub Actions

Never run `cargo build`, `cargo test`, `cargo check`, `clippy` or
`desktop-rs/slint-app/tests/check.sh` (it builds) on the owner's machines
(laptopstudio included): parallel Rust builds there pushed the load to ~97 and
starved the live Host. Push your branch and start `.github/workflows/desktop-checks.yml` on it by hand
(`gh workflow run desktop-checks.yml -R Maxteabag/clarp --ref <branch>`; it no
longer starts on a push). It runs the unit tests, the headless checks (with one rerun for load-sensitive
ones) and a release build, and uploads `cargo-test-log`, `checks` (logs and
screenshots) and `clarp-slint-x86_64-linux` as artifacts
(`gh run download <run> -R Maxteabag/clarp -n checks`). Run a subset by hand
with `gh workflow run desktop-checks.yml -R Maxteabag/clarp --ref <branch> -f checks="live a2a"`.
Main's `desktop.yml` publishes the binary clarpd installs.
