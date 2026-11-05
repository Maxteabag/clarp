# Clarp server refactor program — the brief

Read this whole file before you start. It is the single source of truth for every
agent in the program. The lead is **Hobbles** (Clarp session `hobbles-18b0`); the
owner is **Peter**. Ask the lead, never Peter directly. Only the lead edits this
file. Refresh it with `git fetch origin && git show origin/main:REFACTOR.md`.

Approved by Peter on 3 Nov 2026 ("Ok i trust your judgement. So. Can we get
started today?"): every recommended answer in the plan, starting today, plus the
lifecycle kit.

- Review behind the program: `/var/tmp/clarp-refactor-program/plan/review-report.html`
  and the area reports in `/var/tmp/clarp-refactor-program/reports/` (findings are
  cited by id, for example `TP-01`, at commit `09ee9279`).
- Plan: `/var/tmp/clarp-refactor-program/plan/plan.html`.
- Decision log (lead only, append-only): `/var/tmp/clarp-refactor-program/decision-log.md`.

## 1. Outcome and how we prove it

**Outcome.** The Clarp server restructured per the plan, with behaviour unchanged
except registered intended differences, and the 29 high findings fixed or
explicitly accepted by Peter.

**The oracle.** Main moves under every slice, so each slice's head is replayed
against **its own base commit on main**, with the same corpus on both builds. The
harness compares normalized HTTP responses, SSE event order, rows of the tables
the slice touches, files written and CLI output. Every difference is exactly one
of:

1. **Refactor bug** — fix it in the slice.
2. **Intended difference** — registered in `registers/differences.md`, checked by
   the area's reviewer, signed by the lead. Fix-lane changes are registered once.
3. **Pre-existing bug** — registered in `registers/bugs.md`, fixed in the fix
   lane, never inside a move slice.

The harness counts only once it has proven it can fail: replay of unchanged code
gives zero differences twice, **and** every planted change (SSE order, a missing
database write, a response field, a status transition) shows up as a difference.

**Done** (Peter's targets): zero unexplained replay differences; import layering
enforced in CI; no module over 1,500 lines; `server/server.py` at most 1,500 lines
with no business logic; no function over 150 lines; no silently swallowed broad
excepts; suite time, startup time and memory no worse; characterization tests on
the hot spots; every high finding fixed or explicitly accepted.

## 2. Hard rules

1. **Only the release owner restarts, updates or deploys the live Host**, inside
   the windows **12:00 and 18:00 Europe/Oslo**. Runtime-class releases only at
   18:00, at most one a day. Longest downtime 5 minutes. A failed health check
   rolls back automatically. Never run `make deploy`, `install.sh`,
   `make deploy-static`, `clarp-admin update` or `systemctl ... clarp*` yourself.
2. **Main and `refactor/integration` move through the merge captain** for
   program work. You commit on your own branch and never push.
3. **Refactor slices change no behaviour.** Fixes go through the fix lane, one
   commit and one test each. A move-only commit contains nothing but moves.
4. **Frozen surfaces** (changing one needs Peter's decision through the lead): the
   iOS contract (`contract/`, `HOST_CONTRACT`, `MIN_IOS_CONTRACT`);
   `state.sqlite` compatibility (expand then contract, one release compatible in
   both directions); HTTP, SSE and WebSocket routes and payloads; CLI commands
   and their outputs; environment variables and file paths; hook and skill
   interfaces. **Out of scope:** stable message ids, Oracle engine convergence.
   **Clients (Peter, 4 Nov):** the iOS app and the Rust desktop client are the clients
   that matter; the PWA (web version) is discarded for now. Do no PWA-only fixes or
   checks, and do not remove PWA routes or assets without a separate decision. A
   behaviour change's client check covers iOS and the Rust desktop (its code compiles
   only in GitHub Actions: never run cargo on Peter's machines).
   **Advance GO (lead, 4 Nov):** routine steps are settled by the agents under the standing
   rules R1-R7 in /var/tmp/clarp-refactor-program/registers/advance-go.md (re-pin carry-over,
   landing on SOURCE YES, G1 dispositions by rf-merge and rf-harness, flake register owned by
   rf-harness, backlog pull from registers/backlog.md, peer settlement). The lead decides
   behaviour changes, frozen surfaces, scope and claims, release GO and questions for Peter.
5. **No test suites, coverage, browsers or test servers on laptopstudio** (Peter's
   standing order of 2026-10-12; this machine runs his live Host). Locally, run at
   most one test file or node id with `-n 0` through
   `/var/tmp/clarp-refactor-program/bin/heavy` (it refuses everything else), and
   only tests that start no server, Host process or listening socket. Any test
   that starts an HTTP server (`build_server`, `ContextHTTPServer`, a running-server
   fixture) or a Host runs on heavy-remote, even a single file (3 Nov evening).
   Full suites, coverage and replay runs go to the buildbox through
   `/var/tmp/clarp-refactor-program/bin/heavy-remote` (announced 3 Nov; four job
   slots of `-n 8`; `--status`, `--forget`). It holds the last 5 NOK of Peter's
   100 NOK daily cap for releases: other jobs stop at `CLARP_REMOTE_RESERVE_NOK`
   (95), while jobs from `release-src/` worktrees or with `HEAVY_RELEASE=1` may use
   all 100. Only the release owner and the merge captain set `HEAVY_RELEASE=1`, for
   release canaries and release-candidate gates; nobody raises
   `CLARP_REMOTE_RESERVE_NOK` (4 Nov). The landing gate is the merge captain's
   GitHub Actions run on the exact commit. Never run `cargo` (repo rule).
6. **Data.** The replay corpus is scrubbed with stable surrogates and read only
   through the harness. Tools print structure, counts and codes, never message
   text. Never paste conversation content into messages, commits or reports. Read
   the live database only with `sqlite3 -readonly`. Never write to
   `~/.local/share/clarp` or `~/.config/clarp`.
7. **Peter's own agents come first.** When capacity is tight the lead pauses
   program agents.
8. **Ownership.** Edit only what section 6 gives you. Ask the lead before
   touching anything else. Run agents never edit this brief or files they were
   given to read.
9. **Never** `pkill -f` or `pgrep -f` (your own command line matches). Never
   force-push, never rewrite main, never commit secrets.
10. **Fail safe.** Unknown, partial or contradictory means stop and report. Never
    retry a write that may have landed; read back first. Postpone rather than
    improvise.

## 3. Decisions already made (do not reopen)

- Keep the stdlib HTTP server; no ASGI move.
- **Two tracks (Peter, 3 Nov evening; replaces "no long-lived refactor
  branch").** Bug fixes (the fix lane) and other agents' work land on main and
  go live as we go, in the 18:00 window, only when the release interrupts no
  running agent turn and can be rolled back. The restructuring (slices S1-S6,
  and anything else that is not a bug fix) lands on `refactor/integration`,
  which takes in main after every main landing. It goes live as **one big
  release** when everything is done and good. Tests-only, tools, harness and
  docs changes land on main.
- Claude authors (the fix lane included); Codex reviewers, a different model
  family.
- Seams before moves: new code goes into new files (a route registry, a
  turn-callback seam, harness scenario packs) before shared files move.
- Server-only slices first. Runtime-class slices (turn engine, backends) start
  only after the canary path has worked three times.
- **Lifecycle kit.** Long-lived things become explicit lifecycles: one owner,
  transitions as atomic compare-and-set writes
  (`UPDATE … SET state=? WHERE id=? AND state=?` in one transaction, zero rows
  means conflict), a deadline on every non-final state with a reaper, and
  decide-then-act (a pure decision under the lock, effects after commit). Not for
  request handling, parsers, importers or config.
- Unmerged branches are parked, not landed. Each slice publishes a move map (old
  path → new path, old import → new import) and a rebase helper.

## 4. Phases

| Phase | Content | Exit |
|---|---|---|
| F0 (today) | AUT-01 (Stop pauses goals and always records the stop), RTO-01 (voice claim released on early Oracle failure), BACKENDS-02 (one bad Codex message no longer kills the shared reader) | released in windows |
| 1 (days 1–3) | this brief; flakes quarantined; parked-branch manifest; route goldens; schema golden; migration rehearsal; lifecycle kit contract; harness S0; each slice's characterization tests | **Go/no-go 1** (lead, on the harness proof) |
| 2 | S3 route seam then moves; S5 read model; S1 layering (move-only); S2 guards (all on `refactor/integration`); F1 fixes with replay evidence (on main, released as we go); then S4 turn engine and S6 backends after canary ×3 | **Go/no-go 2** (lead, on the Phase 2 evidence) |
| 3 | deep work: lifecycle conversions, incremental transcript import, locks out of I/O, database behaviour (P) | lead's done check |
| 4 | the one big release: `refactor/integration` into main after a full replay against main, CI, and a canary soak on the buildbox, then a window with rollback ready; close-out: archive agents, remove worktrees, update ARCHITECTURE.md, delete this file | lead's release GO |

**Checkpoints (Peter, 3 Nov 11:3x):** "I dont want to approve for next stages. I just want a finshed refactored server". Both go/no-go checkpoints are the lead's decisions on the same evidence; no agent waits for Peter. Every gate in section 5 still applies.

**Go/no-go 1: GO (lead, 3 Nov 12:12)** on the harness proof at `57d175ff`: two
self-check runs, 9 scenarios, unchanged code 0/0/0 differences both times, all
six planted changes caught. G2 is binding from then on. Behaviour-relevant
landings start once a harness commit with reviewer B's SOURCE YES is on main, and
not before the 3 Nov 18:00 release is done. Tests- and docs-only landings
continue as before.

## 5. Gates

- **G0 entry (per slice):** characterization tests for the code the slice will
  move, written against the old code and committed first; line coverage of that
  code at least 85%, or a lead-signed exception.
- **G1 author:** the full suite (`heavy-remote uv run --frozen --group dev python -m pytest`) is green
  except the quarantined tests (read the result, not a wrapper's exit code);
  move-only commits pass the AST-identity check once it exists; the layer test
  once it exists.
  Keep the full pytest output of every G1: tee it to a log file before any grep,
  and cite the log path in packets and flake reports (a bare FAILED line cannot
  tell a deadline miss from a real weakness).
- **G2 harness** (binding since go/no-go 1): replay of head against its own base
  on main with a harness commit that has reviewer B's SOURCE YES: zero
  unexplained differences. On the laptop `python3 tests/replay/run.py prepare
  <base> <head> --prune` (git only), then `heavy-remote uv run --frozen --group
  dev python tests/replay/run.py --jobs 8 compare --base <base> --head <head>`;
  exit 0 passes. Register intended differences with the lead before the run.
- **G3 reviewer:** the area reviewer's first line `SOURCE YES <full sha>` on the
  exact commit. Reviewer A: the Codex pool, backends and accounts (narrowed on
  4 Nov). Reviewer A2 (from 4 Nov): turn engine and dispatch, runtime boundary,
  agents and identity, policies, database and maintenance. Reviewer C (from
  3 Nov 13:05): read model and transcripts (S5, RM-*), voice (VP-*), autonomy and goals (AUT-*, from 4 Nov). Reviewer
  B: everything else (HTTP layer, live calls and Oracle,
  harness and test infrastructure, release tooling).
- **G4 merge captain:** rebase onto the current target (`origin/main` for
  fixes, tests and tools; `refactor/integration` for slices); the rebased diff's
  `git patch-id --stable` equals the reviewed diff's, otherwise back to review;
  rerun G1 (and G2) on the rebased commit; fast-forward push.
- **G5 release owner:** tag `refactor-<yyyymmdd>-<n>` on main inside a window;
  backup; deploy; health check; automatic rollback on failure; entry in
  `registers/releases.md`.

## 6. Roles and ownership

| Role | Session | Model | Owns |
|---|---|---|---|
| Lead | `hobbles-18b0` | Claude | this brief, scope, decisions, registers, sign-offs |
| Merge captain | `rf-merge-f870` | Claude | landing on main, the gate script, the flake quarantine list, the parked-branch manifest |
| Release owner | `rf-release-d188` | Claude | windows, tags, backup, deploy, health check, rollback, break-glass, the canary node |
| Harness owner | `rf-harness-eeb2` | Claude | `tests/replay/` (new), the corpus tools, planted changes, diff report |
| Contract owner | `rf-contract-80af` | Claude | `server/lib/db_schema.py`, `server/lib/db_migrations.py`, `contract/`, `server/lib/server_identity.py` contract constants, the lifecycle kit (`server/lib/lifecycle.py`, `docs/architecture/lifecycles.md`), schema goldens |
| Fix-lane owner | `rf-fix-c4ab` | Claude | one fix per commit, in the files the lead names per fix |
| Fix-lane author 2 | `rf-fix2-56f3` | Claude | one fix per commit (voice, Oracle, autonomy findings), in the files the lead names |
| Fix-lane author 3 | `rf-fix3-9875` | Claude | one fix per commit, pulled from `registers/backlog.md` (B and C areas) |
| Slice owner S9 (module splits) | `rf-fix3-9875` | Claude | the module-split tool (recipe + generator + facade + ast_identity) and the move-only splits of unowned oversized modules on `refactor/integration` |
| Slice owner S3 | `rf-routesc-352a` (was `rf-routes-0f54`) | Codex | route goldens now; the route seam and route modules after go/no-go 1 |
| Slice owner S5 | `rf-readmodelc-cb42` (was `rf-readmodel-12f8`) | Codex | read-model goldens now; the read model after go/no-go 1 |
| Compute owner | `rf-computec-d8ca` (was `rf-compute-aa87`) | Codex | `/var/tmp/clarp-refactor-program/bin/heavy-remote`, the buildbox ledger |
| Reviewer A (core) | `rf-review-core-bbc1` | Codex | verdicts only |
| Reviewer B (rest) | `rf-review-rest-a79f` | Codex | verdicts only |
| Reviewer C (read model, voice) | `rf-review-readmodel-300e` | Codex | verdicts only |
| Reviewer A2 (turn engine, database) | `rf-review-core2-cf19` | Claude | verdicts only |
| Slice owner S4 (turn engine) | `rf-s4c-8be3` (was `rf-s4-ca35`) | Codex | the turn engine restructure on `refactor/integration` (recipe + generator + facade, regenerated from main's text); G0 tests on main |
| Slice owner S6 (backends) | `rf-s6c-0876` (was `rf-s6-8970`) | Codex | the backends restructure on `refactor/integration` (codex_app_server, the Codex pool, backend modules), same method; G0 tests on main |
| Reviewer A3 (turn engine restructure, Phase 3) | `rf-review-core3-7399` | Claude | verdicts only (S4 and the Phase 3 lifecycle conversions; A2 keeps fixes on main, A reviews S6) |

Reviewers never edit sources, never run jobs against the live Host and never
grant more than the exact commit they reviewed.

### Claims

Files a slice has claimed. Agents outside the program must not edit them; ask
the lead (`hobbles-18b0`). A claim is short-lived and ends when its slice
commits land.

Order rule (5 Nov): on a file that a regenerated split owns (S3, S4, S6, S9), the move-only split lands first. Hand-written edits to that file on `refactor/integration` (Phase 3 conversions, S7 steps) come after it and travel as replay patches stored in main's tree, so regeneration never drops them. A file nobody regenerates may be converted at any time under a claim, with its functions in the forward-port watch.

| Since | Slice (owner) | Claimed | Until |
|---|---|---|---|
| 4 Nov 10:0x | S9 (`rf-fix3-9875`) | on `refactor/integration` only: `server/lib/agent_live_tools.py` (pilot), then `agent_rings.py`, `dreaming.py`, `background_jobs.py`, `orchestrator.py`, each claimed when its cut starts; main's fixes keep flowing because the splits are regenerated from main's text at every merge | the big release |
| 4 Nov 11:5x | S9 (`rf-fix3-9875`) | cut 1b on `refactor/integration` only: `server/lib/agent_rings.py` split along its section headers into `agent_rings/` modules (store, voip, openings, host identity) with `agent_rings.py` as the facade; regenerated from main's text at every merge (RTO-04 (a) on main flows through) | the big release |
| 5 Nov 01:0x | S9 (`rf-fix3-9875`) | cut 1c on `refactor/integration` only: `server/lib/dreaming.py` split along its section headers into modules with `dreaming.py` as the facade; regenerated from main's text at every merge (AUT-04/06/06b, AUT-11 and AUT-12 on main flow through) | the big release |
| 4 Nov 10:4x | S7 (`rf-fix2-56f3`) | on `refactor/integration` only: `server/lib/tts_providers.py` (the `_wav_to_mp3` definition, which stays as a facade re-export), new `server/lib/audio_codec.py`, and the one lazy import of it in `server/lib/custom_tts_adapters.py`; regenerated from main's text at every merge with S9's split tool | the big release |
| 4 Nov 10:5x | S7 (`rf-fix2-56f3`) | on `refactor/integration` only: `server/lib/clip_delivery/raw_pcm.py` (RawPcmSession), `server/lib/clip_delivery/chunked_file.py` (ChunkedFileSession), new `server/lib/clip_delivery/file_session.py`; behaviour-preserving refactor (G0 table plus G2 at zero), not regenerated; no pending main fix touches these files (lead checked VP-01, VP-06 and the voice batch) | the big release |
| 4 Nov 11:3x | S7 (`rf-fix2-56f3`) | on `refactor/integration` only: `server/lib/voice_markup.py` (new `extract_speak_blocks`), `server/lib/transcript_streamer.py` (its `<speak>` regex and the loop at `_on_text`), `server/lib/message_audio.py` (its `<speak>` regex); behaviour-preserving, each reader keeps its mode; no pending main fix touches these files (lead checked VP-01, VP-06, AUT-07, RTO-07) | the big release |
| 5 Nov 06:5x | S7 (`rf-fix2-56f3`) | step 6 on `refactor/integration` only: `server/lib/transcription_pipeline.py` (`transcribe` and `_run_call_steps`) and new `server/lib/stt_router.py`; behaviour-preserving refactor (720-row engine-plan golden table and G2 at zero), NOT regenerated: rf-fix2 forward-ports every later main change to these functions, and rf-merge's main->integration merge stops when main changed them without the forward-port | the big release |
| 5 Nov 07:1x | S7 (`rf-fix2-56f3`) | step 1e on `refactor/integration` only: `server/lib/tts_queue.py` (delete `recent_synth_count`, zero callers; VP-18 dead code); rf-merge's forward-port watch stops a merge if main touches or calls it | the big release |
| 5 Nov 07:1x | Phase 3 (`rf-contract-80af`) | the goals lifecycle conversion on `refactor/integration` only: `server/lib/task_plans.py`; hand-written, so its converted functions join rf-merge's forward-port watch | the big release |
| 5 Nov 07:2x | Phase 3 (`rf-contract-80af`) | the queue lifecycle conversion on `refactor/integration` only: `server/lib/turn_queue.py` (S4 does not split it); hand-written, so its converted functions join rf-merge's forward-port watch, and TP-N3's later change to turn_queue.py on main is forward-ported | the big release |
| 5 Nov 07:2x | S6 (`rf-s6-8970`) | cut 1 on `refactor/integration` only: `server/lib/backend_usage.py` split into a facade plus store, claude, agy and codex parts (S9's split_apply), regenerated from main's text at every merge | the big release |
| 5 Nov 07:3x | S4 (`rf-s4c-8be3`) | on `refactor/integration` only: `server/lib/turn_dispatch.py` split into turn_dispatch_state, turn_command and turn_stop (module level) plus seven TurnDispatchService mixin modules (docs/refactor/s4-turn-engine.md), regenerated from main's text at every merge; the later extraction of the three functions over 150 lines is hand-written on integration and joins the forward-port watch | the big release |
| 3 Nov 12:17 | S5 (`rf-readmodel-12f8`) | `server/lib/message_writes.py` import section (`store_transcript_turns`, `_ImportWriter`, `_store_transcript_turns_txn` and its helpers, `_attach_authority_tools`, `stamp_turn`); `server/lib/message_store.py` facade re-exports; `tests/readmodel/` | the S5 extraction commits land; `rf-fix-c4ab`'s RM-N1 commit may edit the upsert and twin path |

## 7. How to work

- Your worktree is `~/GIT/clarp-worktrees/refactor-<role>` on branch
  `refactor/<role>`, created by the lead. Make further branches inside it if your
  role needs them. One worktree per agent.
- Commit WIP after every meaningful step (conventional commits). You may be
  restarted and must resume from your commits.
- Report to the lead with
  `clarp-admin prompt --to hobbles-18b0 --from <your session> --text "..."`:
  branch, head SHA, done, next, blockers. Report a blocker or a CRITICAL at once.
- Ask for review with a packet: branch, base SHA, candidate SHA, `git patch-id
  --stable` of the diff, gate result, what changed and why, open questions.
  Reviewers answer on the first line: `SOURCE YES <sha>`, `CHANGES NEEDED <sha>;
  <n> findings` or `HOLD <sha>`.
- A reviewed commit goes to the merge captain with its SOURCE YES line. The
  release owner ships what is on main in the next window.
- **Pace.** Never wait for a person or a clock: ask the lead, then do the next
  independent task. Keep turns short: never wait inside a turn for more than
  about 10 minutes. Start CI runs, heavy-remote suites, canaries and other long
  jobs with the clarp-background-jobs launcher
  (`~/.claude/skills/clarp-background-jobs/scripts/run_detached_job.sh --goal
  <your goal> ...`, which wakes you when the job ends) or schedule a wake-up
  (`clarp-admin prompt --to <you> --from <you> --delay 10m --text "..."`), then
  end the turn. Never end a turn on "still running" without one of the two.
- **Inbox.** Clarp hands a busy agent one queued message per turn, so after a
  long turn the rest wait, sometimes for hours. Run
  `/var/tmp/clarp-refactor-program/bin/inbox <your session>` at the start of
  every turn and before any decision, landing or ship. It prints every waiting
  message and is read-only: the messages still arrive later as turns, so note
  "handled via inbox" on one you already handled. Never delete queue rows: each
  delete scans the whole messages table under the writer lock (QUEUE-N2).
- **Messages.** One message per decision. Check whether your question was
  already answered before you send it. Never reverse an in-flight decision
  unless asked. No acknowledgements. Address agents by session slug.
- **After any Host restart:** check your worktree (clean or WIP-committed), rerun
  your gate, run your inbox, then continue.

## 8. Registers

In `/var/tmp/clarp-refactor-program/registers/` (the lead keeps them; send
entries to the lead): `differences.md`, `bugs.md`, `releases.md` (release
owner writes), `parked-branches.json` (merge captain writes).
