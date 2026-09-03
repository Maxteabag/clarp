---
name: clarp-switch-agent-host
description: Move a running Clarp agent's work from one machine to another - rebuild its repo and uncommitted changes at an identical path, recreate the agent with matching persona/voice/avatar, hand it a written briefing, and retire the original. Use when an agent must continue on a different host, or when consolidating agents onto one machine.
allowed-tools:
  - Bash
  - Read
  - Write
---

# Switching an agent's host machine

Moves the *work*, not the session. The target gets a rebuilt environment, a
briefing, and a **dossier of the agent's own prior conversation**; a fresh agent
continues from there. Proven end-to-end on 2026-09-03 moving Arnold, Mike and OPUS
from `elitebook` to `xps15`, and OPUS back again the same evening.

The single biggest mistake is under-transferring context. A summary briefing gets
the environment right and the *work* wrong: the agent knows which repo it is in
but not the plan it designed, the constraints the user stated in their own words,
or the evidence behind its own half-finished decisions. Budget most of your effort
for step 5.

## Decide first: briefing or true resume?

| | briefing + dossier (default) | true resume |
|---|---|---|
| works on any machine | yes | only if paths are identical |
| app shows old chat | no | yes |
| agent *knows* old chat | yes, by reading it | yes, natively |
| cost | a few files and one long prompt | none |

A plain briefing with no dossier is a third option and it is almost always the
wrong one — see step 5.

**Never mix display and memory.** Importing history for display while running a
briefed session gives the user a scrollback the agent cannot actually remember.

True resume additionally requires the source session to be **stopped first** —
two Claude processes appending to one `.jsonl` corrupts it.

## 0. Survey the TARGET first

Do this before you capture anything. Most of a migration is discovering that the work
you were about to do is already done. Every line below has, at least once, turned a
planned step into a no-op.

```bash
# Is this a return trip? If the agent was moved off this host recently, its tree is
# probably still here — and probably byte-identical. Hash before you copy anything.
cd ~/GIT/REPO && git rev-parse HEAD && git status -sb | head -3
git diff HEAD | sha256sum; git diff --cached | sha256sum
git ls-files -o --exclude-standard -z | LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum

# Which identities does this host already hold? Do NOT copy a secret that is already here.
gh auth status                     # a non-active account is still usable
git -C ~/GIT/REPO config --get credential.helper
ls ~/.config/git/                  # repo-scoped credential stores

# Which Claude account? A different account is the whole reason a move helps.
python3 -c "import json,os;d=json.load(open(os.path.expanduser('~/.claude.json')));\
print((d.get('oauthAccount') or {}).get('emailAddress'))"

# Service dependencies, worktrees, and where a wanted branch already sits
docker ps --format '{{.Names}}\t{{.Status}}'
git -C ~/GIT/REPO worktree list
git -C ~/GIT/REPO branch -a --contains WANTED_SHA
```

Compare against the source and migrate **only the delta**. On the 2026-09-03 return trip
the delta was: one worktree path, one git credential pointer, one agent row, one dossier.
Zero bytes of repo content — the tree was already identical, hash-verified.

### Fast paths

- **Return trip, tree unchanged on both sides.** Skip steps 2 and 3 entirely. Hash-compare,
  then go straight to step 4.
- **Target already holds the identity.** If `gh auth status` lists the right account, point
  git at it (`gh auth switch -u ACCOUNT`, or a repo-scoped
  `credential.helper = !gh auth git-credential` plus `credential.username`) instead of
  copying a credential file across the network. Copying a secret between machines when the
  target already has it is wasted work and needless exposure. The failure that triggers
  this — `could not read Password for 'https://USER@github.com'` despite being logged in —
  means *wrong active account*, not *missing credential*.
- **Branch already present at the wanted commit.** Do not fetch or re-clone; just
  `git worktree add` it at the path the source used.
- **Same Claude account on both hosts.** Stop and tell the user: a session limit will
  follow the agent across the move. The move only buys quota if the accounts differ.

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

## 2. Capture (source, read-only) — skip if step 0 hashed identical

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

### Worktrees the agent actually used

Recreate them at the **same path**, because the agent's own transcript is full of
`cd <that path>` and it will follow its old muscle memory. If the branch is already
checked out somewhere else on the target, relocate that one first:

```bash
git worktree move OLD_PATH NEW_PATH        # fails "Invalid cross-device link" across
                                           # filesystems, e.g. /var/tmp -> $HOME
git worktree remove OLD_PATH && git worktree add NEW_PATH BRANCH   # the fallback
```

A clean worktree whose branch is in sync with origin carries no state at all — the only
thing to migrate is its *location*.

Clone to the **same path spelling** as the source (`~/GIT/clarp`, not `~/GIT/Clarp`) —
Claude's project directory key is derived from cwd. If the target already has that
repo on another branch with its own dirty files, clone to a separate path instead of
disturbing it.

## 4. Recreate the agent

`clarp-admin create-agent` may not exist on older builds. The API always does:

```bash
curl -X POST "$API/agents" -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"Mike","session":"mike-b71f","cwd":"/home/peter/GIT/REPO",
       "backend":"claude","model":"claude-opus-5","voice_id":"{...}",
       "avatar_base64":"..."}'
```

Accepted fields are `name` (the persona, required), `session`, `cwd`, `backend`, `model`,
`effort`, `voice_id`, `avatar_base64`, `replace_sid`, `fork_session_id`,
`synthesize_audio`. Read them off `AgentLifecycleService.create` if a build disagrees.

**Pass the source's `session` verbatim** — an explicit id is honored whenever it is unused
on the target, so the agent keeps its name in the app and in `clarp-admin prompt --to`.
It is refused only if that id already exists there (including soft-deleted), in which case
a `<persona>-<hex>` id is minted instead. Send `voice_id` and `avatar_base64` in the same
call: the server writes the avatar to a path keyed by the *new* `agent_id`, so copying the
source's `.jpg` by hand and patching `avatar_path` afterwards is unnecessary.

The token is `auth_token` in `~/.config/clarp/config.toml` — note that is the *clarp*
config dir; a stale `~/.config/claude-pwa/config.toml` may still exist with an empty token
and mislead you into thinking the API is unauthenticated.

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

## 5. Brief it — and ship its entire prior context with it

Two artifacts, not one. A briefing tells the agent where it is. A **dossier** tells it
who it was. Send both.

### 5a. The briefing

```bash
clarp-admin prompt --to SESSION --origin automation --text "$(cat briefing.md)"
```

It must contain: rebuilt environment and exact expected `git status`; what NOT to do
(don't re-clone, don't rebase, don't revert a co-tenant's changes); the long arc *and*
the current task; hard constraints stated verbatim ("open a PR, do not merge"); the
task-plan item list with per-item status, because **plans live in the server DB and are
per-host** — the agent must recreate its plan on the target; and **anything that could
not travel** — a Teams message, a browser tab, or the source machine's database
container is not in the repo, and the agent must be told to ask for it.

### 5b. The dossier — do not skip this

Transcripts are the only durable record of *why*. Extract them into readable files on
the target and tell the agent to read them. Do not paraphrase; the agent's own wording
and the user's own wording are the payload.

```bash
mkdir -p /var/tmp/<agent>-context
scp SOURCE:~/.claude/projects/<key>/<backend-session-id>.jsonl \
    /var/tmp/<agent>-context/source-session.jsonl
```

Then render, from every transcript that agent ever ran in this repo — the session on the
source *and* any earlier sessions still on the target:

| File | Content | Why |
|---|---|---|
| `01-<source>-full-conversation.md` | every user turn and assistant text block, in order, untruncated | the plan, the decisions, the user's exact asks |
| `02-<host>-user-turns.md` | only the user's turns, from the long pre-move session | every instruction, correction and constraint, with nothing of yours in between |
| `03-<host>-full-conversation.md` | both sides of the long pre-move session | greppable "why did I do that" |
| `04-<source>-tool-calls.md` | every `tool_use` input and its `tool_result`, results capped ~6 KB | the evidence: inventories, file reads, command output the conclusions rested on |

Render with a small script over the JSONL — `type` is `user`/`assistant`, and
`message.content` is either a string or a list of `text` / `tool_use` / `tool_result`
blocks. Cap tool results, never conversation text.

Write a `00-READ-ME-FIRST.md` index that states read order, marks which files to read
**fully** versus grep, points at any raw transcript left on the machine, and says plainly:
*this is not a suggestion, it is what you already decided and what the user already
approved.*

Then deliver it — and inline the smallest, densest file directly in the prompt so the
agent has the core before it opens anything:

```bash
{ echo "Read /var/tmp/<agent>-context/00-READ-ME-FIRST.md first, then 01 and 04 fully."
  echo; cat /var/tmp/<agent>-context/01-*-full-conversation.md
} > flood.md
clarp-admin prompt --to SESSION --origin automation --text "$(cat flood.md)"
```

A 25 KB inline prompt is fine. Multi-hundred-KB files go on disk with a read order.

### What under-transferring actually costs

On 2026-09-03 OPUS was moved back to `elitebook` with a briefing that carried the eight
task-plan item titles and the hard constraints. It resumed work correctly and looked
fine. It had in fact lost: a five-act demo structure with per-act timings, the user's
verbatim framing of what the demo was for, the seed specification (six services, planted
typos in three, a 3/3 ownership split, two API keys surfaced once at generation because
they are stored hashed), the skill specification, the sub-agent rehearsal rubric, and the
one open question that decided a whole act. None of that is recoverable from the repo —
it existed only in the transcript. Three follow-up prompts were needed to repair it, and
only after the user noticed.

The tell: the agent's first reply confirms the *environment* confidently and says nothing
specific about the *plan*.

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
- and cites something from the dossier it could only know by having read it — a per-act
  detail, a verbatim constraint, an open question. Environment-only confirmation means
  the context did not land
- `API Error: 529 Overloaded` is transient and unrelated to the migration — re-prompt
- source `git status` and target `git status` produce identical counts, hash-compared:
  `git diff HEAD | sha256sum`, `git diff --cached | sha256sum`, and untracked content via
  `git ls-files -o --exclude-standard -z | LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum`
  (without `LC_ALL=C` the two hosts' collation differs and the hashes disagree on identical trees)

## Environment gaps to check before declaring victory

- **APNs** — configured per server. A target without `[apns]` sends no push notifications.
  Check the key file actually exists at the configured `key_path`: a host migrated from the
  old `claude-pwa` layout can still point at `~/.config/claude-pwa/AuthKey_*.p8` while the
  file now lives in `~/.config/clarp/`. Also check `device_tokens` has a live row — a
  server with no token pushes to nobody.
- **TTS provider** — differing providers make the same persona sound different. Pass the
  source's whole `voice_id` JSON (it holds one id per provider), not just one of them.
- **Rate limits are per account, not per machine.** Compare `oauthAccount.emailAddress` in
  `~/.claude.json` on both hosts. Same account ⇒ the limit follows the agent and the move
  achieves nothing on that front; say so before doing the work.
- **Pairing** — the phone pairs with a *server*. Clarp peers are only cross-server
  message delivery (`POST /send`); they do not surface another host's agents in the app.
- **Task plans are per-server.** `task_plans` / `task_items` live in the host's
  `state.sqlite`. The plan does not travel. Put its item list and per-item status in the
  briefing and tell the agent to recreate it on the target.
- **Databases and containers do not travel.** A target running its own `mssql` / `azurite`
  container has *its own data*. Migrations and seed rows the agent applied on the source
  are not there. Tell it to re-verify rather than assume.
- **A co-tenant left behind means two diverging copies.** If another agent shares the
  source tree and is not moving, the two hosts now hold independent copies of the same
  uncommitted work. State that to both the user and the migrated agent, and tell the agent
  not to try to reconcile them.

## The whole thing, in order, when nothing goes wrong

1. Survey the **target** (step 0) — hashes, identities, accounts, containers, worktrees.
2. Survey the source; find the agent's real cwd from its transcript, not the `agents` row.
3. Diff the two surveys. Migrate only the delta.
4. Recreate missing worktrees at identical paths; point git at an identity the target
   already holds.
5. `POST /agents` once, full payload, reusing the source `session` id.
6. Build the dossier, write the briefing, send the briefing then the flood.
7. Retire the source (API if reachable, else SQLite after `cp -a`).
8. Verify the agent's first reply cites both the environment *and* the plan.

A return trip with an unchanged tree is roughly a dozen commands. If it is taking longer
than that, you are probably re-doing something step 0 would have shown you was already
done.
