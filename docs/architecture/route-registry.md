# Route registry seam for server.py (refactor slice S3)

Status: proposal for review (findings only). Author rf-routes (S3), 3 Nov 2026.
Inputs: `REFACTOR.md`, http-shell review section 3 (step 1 and step 4),
`tests/fixtures/route_goldens/` (dispatch and response goldens).

## 1. What the dispatch does today

Each `do_<METHOD>` runs a prelude of gates and then a first-match chain:

| Step | GET | POST | PUT | DELETE | PATCH |
|---|---|---|---|---|---|
| `_authorized` (public exact/prefix, local trust, bearer, cookie, local `?token=`) | yes | yes | yes | yes | yes |
| `_oracle_auth_missing` (`/oracle`, `/oracle/*` need a validated credential) | yes | yes | no | no | no |
| `_device_forbidden` (limited scope: full-only prefixes; non-GET allow-list) | yes | yes | yes | yes | **no** (HTTP-02) |
| `janitor_http` hand-off | yes | yes | no | yes | no |
| `controller_actions_http` hand-off | yes | yes | no | no | no |
| root static set, podcast-history (GET) | yes | — | — | — | — |
| exact table (`_GET_ROUTES` 104, `_POST_ROUTES` 124, `_PUT_ROUTES` 1) | yes | yes | yes | — | — |
| prefix/suffix chain, in source order | 27 branches | 10 | 1 | 11 | 1 |
| fall-through `404 not found` | yes | yes | yes | yes | yes |

Auth is decided by path prefix before routing, so it is not a property of a
route: the same gate answers unmatched paths too (limited `GET /x` is 404,
limited `POST /x` is 403). Some checks live inside handlers
(`/server-terminal`, `podcast_history_http`, controller-action `admin=True`,
background-job log redaction, `_reject_janitor_control` in 9 POST handlers).

Prefix matches are not segment-bound: `/artifacts/<rest>/html` takes `rest`
with its slashes, `DELETE /agents/<rest>` does not strip, `/terminal/a/b`
yields session `a/b`. Branch order matters (`/artifacts/x/events-config`
must win over `/artifacts/x/events`). The registry must reproduce both.

## 2. The seam

New file `server/lib/routes/registry.py` (new package `lib.routes`; not
`server/http/`, which would shadow the stdlib `http` because `server/` is on
`sys.path`):

```python
@dataclass(frozen=True)
class Route:
    method: str                  # GET POST PUT DELETE PATCH
    pattern: Pattern             # Exact("/status") | Affix("/artifacts/", "/html", arg=unquote_strip)
    handler: str                 # Handler method name, or "lib.<module>.<fn>"
    auth: Auth                   # public | limited | full | local_admin  (declared, see 3)
    janitor_guard: bool = False  # handler refuses janitor sessions (metadata until the fix lane)
    note: str = ""               # e.g. "HTTP-01" for a known pre-existing bug
```

`Affix(prefix, suffix="", arg=...)` matches `path.startswith(prefix) and
path.endswith(suffix)` and passes `arg(path[len(prefix):len(path)-len(suffix)])`,
which is exactly today's slicing (`strip("/")`, `unquote(...strip("/"))`,
raw, or `int` with the 400 fallback). Library routers become one route with
`Affix("/janitors/")` etc. and `handler="lib.janitor_http.handle"`.

`ROUTES: dict[method, tuple[Route, ...]]` is **ordered**: the dispatcher walks
it first-match, with the exact table folded in at the position it has today.
`do_<METHOD>` keeps its gate prelude unchanged and replaces the table lookup
and chain with `routes.dispatch(self, method, path)`. Nothing else changes in
the first seam commit.

Generation: a one-off script reads `_GET_ROUTES/_POST_ROUTES/_PUT_ROUTES`
and the chains (transcribed by hand, one Route per `if` branch, reviewed
against the dispatch golden) and writes the literal `ROUTES` table. The
script is not kept; the golden is the oracle.

## 3. Auth: declared, checked, then enforced

Two steps, so the seam commit changes no behaviour:

1. **Seam (move lane).** `auth` on each Route is the value the dispatch
   golden's `scope` column records today (computed from the gates, including
   the three wrong ones). A test asserts declared `auth` equals the golden
   scope for every route. The gates still decide; the declaration only
   documents. HTTP-01/02/03 routes carry `note="HTTP-0x"` and their real
   (wrong) scope `limited`.
2. **Fix lane (one commit and one test each).** The dispatcher enforces the
   declared `auth` after `_authorized`; then HTTP-01 (`/terminal/` → full,
   janitor refusal), HTTP-02 (PATCH gets the gate), HTTP-03 (`/dirs`,
   `/past-sessions` → full) are each a one-line declaration change plus the
   golden diff, registered in `differences.md`, with a `docs/compatibility.md`
   row and a `HOST_CONTRACT` bump decided by the contract owner. The
   unmatched-path behaviour (404 vs 403) stays as today unless Peter decides
   otherwise.

## 4. Parity test

`tests/integration/route_probe.py` already runs the real `do_<METHOD>` in
process, stubs every handler and library router, and records the first
target per (method, path, principal) for 1,360 requests and 7 principals.
The parity tests for the seam:

- `test_dispatch_golden` unchanged must pass on the seam commit (zero diffs:
  same target and arguments, same gate answer, for every path and principal).
- Registry completeness: every non-404 golden target is produced by exactly
  one Route; every Route's sample path resolves to its own handler (each
  Route carries no sample; the test takes it from the golden key).
- Declared auth equals the golden scope (section 3, step 1).
- `test_response_golden` (real socket, minimal request, status + body shape
  for admin and limited) must pass unchanged.

The probe proves it can fail: adding `/terminal/` to the full-only prefixes
turns one golden entry red.

## 5. Where new routes go from day one

- A new route is a `Route` in `server/lib/routes/<feature>.py`, collected into
  `ROUTES` by `server/lib/routes/registry.py`. No new entry in server.py's tables or
  chains: a ratchet test fails if `_GET_ROUTES`/`_POST_ROUTES`/`_PUT_ROUTES`
  or the `do_*` chains gain an entry (counts pinned, may only go down).
- Its handler is a function `(handler, **params)` in the feature module, so
  it never adds to `Handler`. It declares `auth` and `janitor_guard`, and the
  goldens are regenerated in the same commit.
- Contract rules are unchanged: a new route a client may use bumps
  `HOST_CONTRACT` and adds a `docs/compatibility.md` row.

## 6. How handlers move (after go/no-go 1)

Move-only commits move `Handler` methods, byte-identical, into feature mixin
classes in `server/lib/routes/<feature>.py` (`class VocabRoutes:` ...), and
`Handler` inherits them. Why mixins first rather than functions:

- The method bodies do not change at all, so the AST-identity check is a
  plain comparison of `FunctionDef` nodes and review is mechanical.
- `getattr(self, name)` dispatch, `self._json_ok` helpers and the library
  modules that call handler methods (`_stop_agent_session` from three oracle
  modules, `controller_actions_http`, HTTP-06) keep working unchanged.
- Tests that patch `server_module.Handler` attributes (4 sites) keep working;
  tests that patch server.py module globals used by a moved method (`log`,
  `TurnDispatchService`, `OrchestratorService`, `TTSWorker`,
  `_STT_ACTIVATION_LOCK`, `SERVER_TIMING`, `_deliver_decision_rows`,
  `eventlog`, `agents_db`) must follow the global; each move's map lists the
  test patch sites it retargets, in the same commit (test-only edit).

Each move ships a move map (old `server.py:Handler.name` → new
`lib/routes/<feature>.py:<Mixin>.name`) and the rebase helper REFACTOR.md asks for.
Turning mixin methods into `(req, ctx, resp)` functions and extracting
services (stop, delivery bracket, STT factory) is later, separate work
(http-shell step 5) with its own review.

Module globals used by moved code: mutable ones (`_OUTDATED_CLIENTS_NOTED`,
`_CARTESIA_PREVIEW_LOCKS`, `_STT_ACTIVATION_LOCK`) move with their only
users or are imported as objects (never rebound); import-time config
(`_CFG`, `PORT`, `BIND_ADDR`) stays in server.py and is read through
`server` only where it is today.

## 7. Order of moves (lowest risk first)

0. Seam: `server/lib/routes/registry.py` + dispatcher call, no handler moves.
1. Settings and ops (providers, update, pairing, devices, diagnostics, dirs,
   clog): 57 methods, ~830 lines, mostly parse-call-respond.
2. Vocabulary and transcription admin (not `_handle_transcribe`).
3. Artifacts, decisions, attention, media, upload.
4. Autonomy (heartbeat, dreaming, goals, schedules, teams, jobs).
5. Agents (create, fork, rename, llm, persona, avatars).
6. Calls and Oracle (thin, but three oracle modules call back into Handler).
7. Voice out (voices, previews, clips).
8. Send, select, orchestrator.
9. Turn control (stop, repair, queue): runtime-adjacent; after canary x3.
10. `/log` and the delivery bracket: last, coordinated with the runtime
    boundary owner (reviewer A).

Each step is one or more move-only commits (one feature group, at most
~800 lines each), each with G1, the AST-identity check, both route goldens
unchanged, and replay (G2) from go/no-go 1. Streaming routes (`/events`,
terminals, clip streams, HLS, growing audio, STT stream, Live sockets) move
last within their group and are checked by the response golden only for
their status line; byte-stream parity comes from the harness.

## 8. Coverage entry gate (G0)

server.py line coverage on the full suite is measured with pytest-cov
(`--cov=server`) and reported per feature group; the groups of step 1 to 3
must reach 85% before their move. Gaps are closed with characterization
tests against today's code, never by changing it.

## 9. Open questions for the lead

1. Should the unmatched-path gate answers (limited non-GET 403 before 404)
   stay forever, or be normalized in the fix lane? (Proposal: stay.)
2. Mixins as the move vehicle (section 6) instead of free functions: needs
   your agreement, since it shapes the AST-identity check the merge captain
   builds.
3. The review's step 7 rule-8 guard (no `handler._*` from `server/lib`)
   must exempt `lib.routes`, which is the handler layer by design.
