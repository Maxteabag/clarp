---
name: clarp-switch-agent-host
description: Move a running Clarp agent's work from one machine to another - rebuild its repo and uncommitted changes at an identical path, recreate the agent with matching persona/voice/avatar, hand it a written briefing, and retire the original. Use when an agent must continue on a different host, or when consolidating agents onto one machine.
allowed-tools:
  - Bash
  - Read
  - Write
---

# Switching an agent's host machine

Moves the *work*, not the session. The target gets a rebuilt environment and a
briefing; a fresh agent continues from there. Proven end-to-end on 2026-09-03
moving Arnold, Mike and OPUS from `elitebook` to `xps15`.

## Decide first: briefing or true resume?

| | briefing (default) | true resume |
|---|---|---|
| works on any machine | yes | only if paths are identical |
| app shows old chat | no | yes |
| agent *knows* old chat | summary only | everything |

**Never mix them.** Importing history for display while running a briefed session
gives the user a scrollback the agent cannot actually remember.

True resume additionally requires the source session to be **stopped first** —
two Claude processes appending to one `.jsonl` corrupts it.

## 1. Survey the source

```bash
DB=~/.local/share/clarp/state.sqlite
sqlite3 -json "file:$DB?immutable=1" "SELECT session,persona,backend,model,cwd,voice_id,
  personality,avatar_path FROM agents WHERE deleted_at IS NULL AND archived_at IS NULL;"
sqlite3 "file:$DB?immutable=1" "SELECT session,backend_session_id,ended_at FROM runtimes
  WHERE ended_at IS NULL;"
```

Find each agent's real position from its transcript — the recorded `cwd` may be a
parent directory, and agents wander between worktrees:

```bash
python3 - <<'PY'
import json
last=None
for line in open(TRANSCRIPT, errors="replace"):
    d=json.loads(line)
    if d.get("cwd"): last=(d["cwd"], d.get("gitBranch"), d.get("timestamp"))
print(last)
PY
```

### Lessons that changed the plan

- **Check what is actually live.** OPUS had touched 37 worktrees, but the newest was
  2 days stale and its last position was the main tree. None of them needed to move.
- **Branch names may not match directory names.** Worktree dir `751-shared-plain-library`
  held branch `worktree-751-shared-plain-library`. Grepping the wrong name produced a
  false "not pushed to origin" conclusion. Verify with:
  ```bash
  git for-each-ref --format='%(refname:short)|%(upstream:track)' refs/heads
  ```
- **Agents can share one working tree.** Two agents in the same cwd have one set of
  uncommitted changes. They migrate together or not at all — say so explicitly.
- **Don't copy the directory.** That repo was 18 GB; 16 GB was build artifacts and
  `.git` was 35 MB. Worktree `.git` files hold *absolute* gitdir paths and break on
  any path change. Everything committed is already on the remote.

## 2. Capture (source, read-only)

```bash
git -C "$R" diff HEAD > dirty.patch
git -C "$R" ls-files --others --exclude-standard -z > untracked.list0
tar -C "$R" --null -T untracked.list0 -czf untracked.tar.gz
git -C "$R" diff --cached --name-only -z > staged.list0     # ← do not skip
```

Record `remote`, `branch`, `base_sha` in a manifest. Typical total: well under 1 MB
of patch plus a few MB of untracked files.

## 3. Rebuild (target)

```bash
gh repo clone ORG/REPO ~/GIT/REPO          # or git clone
cd ~/GIT/REPO
git checkout BRANCH && git reset --hard BASE_SHA
git apply --whitespace=nowarn dirty.patch
tar -xzf untracked.tar.gz
xargs -0 -a staged.list0 git add --          # restore the index
```

Reset to `BASE_SHA` **before** applying, then rebase forward later if wanted.
Applying onto a moved `main` produces conflicts inside `git apply`, where a rejected
hunk is nearly invisible.

**Restoring the index matters.** Skip `staged.list0` and the counts silently diverge —
57 staged files became "30 changed + 92 untracked". Verify equality:

```bash
git diff --cached --shortstat            # must match source exactly
git ls-files --others --exclude-standard | wc -l
```

Clone to the **same path spelling** as the source (`~/GIT/clarp`, not `~/GIT/Clarp`) —
Claude's project directory key is derived from cwd. If the target already has that
repo on another branch with its own dirty files, clone to a separate path instead of
disturbing it.

## 4. Recreate the agent

`clarp-admin create-agent` may not exist on older builds. The API always does:

```bash
curl -X POST "$API/agents" -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"Mike","cwd":"/home/peter/GIT/REPO","backend":"claude","model":"claude-opus-5"}'
```

> **`POST /agents` is not a dry run.** A minimal `{"name":"X"}` probe *creates the
> agent* with a default cwd, and a second attempt then fails `409 contact_occupied`.
> Send the full payload first time, or `DELETE /agents/<session>` and redo it.

Personas carry voice and personality automatically. A persona that doesn't exist on
the target silently falls back to defaults — check and repair:

```bash
curl -X POST "$API/agent-voice" -H "Authorization: Bearer $TOKEN" \
  -d '{"session":"opus-7722","provider":"cartesia","voice_id":"..."}'
scp avatars/<hash>.jpg TARGET:~/.local/share/clarp/avatars/
sqlite3 "$DB" "UPDATE agents SET avatar_path='...' WHERE session='opus-7722';"
```

## 5. Brief it

Write a `BRIEFING.md` per agent and deliver it as the opening message:

```bash
clarp-admin prompt --to SESSION --origin automation --text "$(cat briefing.md)"
```

It must contain: rebuilt environment and exact expected `git status`; what NOT to do
(don't re-clone, don't rebase, don't revert a co-tenant's changes); the long arc *and*
the current task; hard constraints stated verbatim ("open a PR, do not merge"); and
**anything that could not travel** — a Teams message or a browser tab is not in the
repo, and the agent must be told to ask for it.

## 6. Retire the source

Check for duplicates before deleting anything:

```bash
sqlite3 "$DB" "SELECT name FROM paired_devices;"   # empty ⇒ nothing to deduplicate
```

If the API is unreachable (Clarp binds loopback; the tailnet port is firewalled to
the tailscale interface), go through SQLite — back it up first:

```bash
cp -a "$DB" state.sqlite.bak
NOW=$(python3 -c 'import time;print(int(time.time()*1000))')
sqlite3 "$DB" "UPDATE runtimes SET ended_at=$NOW WHERE ended_at IS NULL AND session IN (...);
               UPDATE agents   SET deleted_at=$NOW WHERE deleted_at IS NULL AND session IN (...);"
```

Reversible by clearing `deleted_at`.

## 7. Verify

- transcripts appearing under `~/.claude/projects/<key>/<runtime-id>.jsonl` and growing
- each agent's first reply confirms the repo state it was told to expect
- `API Error: 529 Overloaded` is transient and unrelated to the migration — re-prompt
- source `git status` and target `git status` produce identical counts

## Environment gaps to check before declaring victory

- **APNs** — configured per server. A target without `[apns]` sends no push notifications.
- **TTS provider** — differing providers make the same persona sound different.
- **Rate limits are per account, not per machine.** If the source hit a session limit,
  the target may hit it too.
- **Pairing** — the phone pairs with a *server*. Clarp peers are only cross-server
  message delivery (`POST /send`); they do not surface another host's agents in the app.
