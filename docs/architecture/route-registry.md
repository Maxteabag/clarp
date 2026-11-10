# Route registry seam for server.py (refactor slice S3)

Status: proposal, revision 2 (answers reviewer B's five design findings on
c705bbaa and the lead's decisions of 3 Nov). Not approved; no seam code exists.
Author rf-routes (S3), 3 Nov 2026.
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

Prefix matches are not segment-bound and the branches' bodies differ in what
they pass: `/artifacts/<rest>/html` takes `rest` with its slashes and decodes
once (`%252F` arrives as `%2F`), `DELETE /agents/<rest>` neither strips nor
decodes, `/terminal/a/b` yields session `a/b`, `DELETE /teams/t/members/a/b`
splits on the first `/members/` into `('t', 'a/b')`, `/clips/x/stream` answers
400 before any target, and `/clips/<id>/<file>` needs `ctx.audio_dir`. Order
matters where matchers overlap: the generic `/artifacts/` branch must come
after the suffix branches (moving it first changes 8 golden entries), and
`POST /turn-queue/resume/send` reaches `_handle_turn_queue_send('resume')`,
not the exact `/turn-queue/resume`.

## 2. The seam: predicate plus verbatim adapter

New package `server/lib/routes/` (lead decision: inside `lib/`, because
`install.sh` copies only `server.py`, `runtime.py` and `lib/`, and a top-level
`server/http/` would also shadow the stdlib `http` on `sys.path`). The seam
file is `server/lib/routes/registry.py`:

```python
@dataclass(frozen=True)
class Route:
    method: str                      # GET POST PUT DELETE PATCH
    match: Callable[[str], bool]     # pure predicate on the bare path (no query)
    adapter: Callable[[Handler, str], object]  # today's branch body, verbatim
    admission: str                   # dispatch admission today: public|limited|full|local_admin
    name: str                        # stable id used by the goldens and the move map
    inner_checks: tuple[str, ...] = ()  # authorization the handler itself still does
    janitor_guard: bool = False      # handler calls _reject_janitor_control (metadata)
    note: str = ""                   # e.g. "HTTP-01"

NOT_HANDLED = object()   # adapter result: keep walking, as today's fall-through
```

Matching and invocation are separate (finding 1):

- `match` is exactly today's `if` condition as a pure function of the bare
  path, e.g. `lambda p: p.startswith("/teams/") and "/members/" in p`, or for
  a library router the router's own predicate (`janitor_http.handles`, which
  also accepts the exact `/janitors`, `/janitor-triggers`, `/janitor-policy`).
  It never converts, decodes or responds.
- `adapter(handler, path)` is today's branch body moved verbatim with `self`
  renamed `handler` (the only edit; the AST check allows exactly that rename
  in adapters). It does the slicing, `unquote`, `strip`, `int()` with its
  400, the containment checks (`/static/`, `/audio/`), reads `handler.ctx`,
  and calls the target with today's arguments: `(handler, "GET")` for library
  routers, two stripped arguments for the team member split, `(handler,
  ctx.audio_dir, clip_id, filename)` for HLS. Errors therefore happen at the
  same point in the same order as today.
- A branch that can fall through today (`/decisions/<id>/<other>` when no
  action suffix matches) returns `NOT_HANDLED`, and the walk continues to the
  next Route, as the `if` chain does now.
- Exact-table entries are generated Routes `match=lambda p: p == "/status"`,
  `adapter=lambda h, _p: getattr(h, "_handle_status")()`; the dispatcher keeps
  a dict for them at the chain position the table has today, so lookup stays
  O(1) and the order is unchanged.

`ROUTES: dict[method, tuple[Route, ...]]` is ordered first-match.
`do_<METHOD>` keeps its gate prelude byte-identical (lead decision: the
unmatched-path answers, limited `GET /x` 404 and limited non-GET 403, stay
exactly as today in the seam and every move) and replaces the hand-offs,
table lookup and chain with `registry.dispatch(self, method, path)`, which
ends in today's `self._send(404, b"not found")`.

The table is written by hand from the source, one Route per `if` branch and
per hand-off, in source order; the dispatch golden is the oracle, not a
generator script.

## 3. Admission: declared, checked, then enforced

`admission` names **dispatch admission**: who gets past today's gates to the
route. It is not the effective authorization of the request; handlers keep
their own checks, listed in `inner_checks` and never removed by a later
enforcement change: `/server-terminal` (validated credential),
`podcast_history_http` (full scope), controller actions (`admin=True`
changes), background-job detail (log hidden from limited devices), and
`_reject_janitor_control` in 9 POST handlers (finding 2).

One router can serve several admission levels today (finding 2): `GET
/janitors` and `/janitor-policy` admit limited devices, `GET
/janitor-runs/r1/context` only full; `POST /controller-actions/a1/invoke`
admits limited, `POST /controller-actions` only full. So a router gets one
Route per admission boundary, all with the same adapter, and their predicates
together equal the router's own predicate, in an order that keeps the first
match identical:

| Method | Route name | match (in this order) | admission (golden) |
|---|---|---|---|
| GET | `janitor.runs` | `handles(p) and p.startswith("/janitor-runs/")` | full |
| GET | `janitor.read` | `handles(p)` | limited |
| POST, DELETE | `janitor.write` | `handles(p)` | full |
| GET | `controller.read` | `handles(p)` | limited |
| POST | `controller.invoke` | `handles(p) and limited_device_allowed(p, "POST")` | limited |
| POST | `controller.manage` | `handles(p)` | full |

The boundaries are today's gate rules (`/janitor-runs/` is a full-only
prefix; `limited_device_allowed` admits only `/controller-actions/<id>/invoke`),
so the split adds no new rule.

Steps, so the seam changes no behaviour:

1. **Seam (move lane).** For every golden key, the declared `admission` of
   the first matching Route equals the golden `scope`, including the wrong
   ones (HTTP-01/02/03, `note` set). The gates still decide; the declaration
   is documentation checked by a test.
2. **Fix lane (one commit and one test each).** The dispatcher enforces the
   declaration after `_authorized`; HTTP-01, -02 and -03 then become
   declaration changes with their golden diff, `differences.md` entry,
   `docs/compatibility.md` row and a contract decision by the contract owner.
   The enforcement never relaxes an `inner_checks` entry.

## 4. Parity

The oracle is `tests/integration/route_probe.py`: it runs the real
`do_<METHOD>` in process with every handler and library router stubbed and
records the first target with its exact arguments, or the inline response,
per (method, path, principal). The dispatch golden now holds 1,620 method x
path entries for 7 principals, including edge witnesses (finding 5): nested
and empty remainders (`/terminal/a/b`, `/terminal/`, `/terminal//s1/`),
encoded and double-encoded slashes (`/artifacts/a%2Fb/html`,
`/artifacts/a%252Fb`, `DELETE /agents/a%2F1`), the infix split
(`/teams/t1/members/a1/b`), prefix boundaries (`/janitorsx`,
`/controller-actionsx`, `/podcast-historyx`, `/oracle-x`, `/static`),
overlaps (`/turn-queue/resume/send`, `/artifacts/a1/events-config/events`,
`/clips/1/a/b`, `/clips/%31/stream`) and query separation
(`/send?path=/teams/t1/members`).

Seam acceptance:

- `test_dispatch_golden` passes unchanged: for every key the **first selected
  route and its exact invocation or inline response** are identical
  (completeness is defined this way, not as one handler per Route, since a
  router serves many keys).
- Every Route is the first match for at least one golden key (no dead Route).
- Declared admission equals the golden scope (section 3).
- `test_response_golden` passes unchanged (status, type, full body shape for
  admin and limited, over a real socket; minimal requests only, not value
  parity, not stream bytes).

Proven able to fail (planted on the old code, reverted): `/terminal/` added to
the full-only prefixes (1 entry red); a binding change, terminal session
`.split("/")[0]` (1 red, `/terminal/a/b`); a precedence change, generic
`/artifacts/` before the suffix branches (8 red). A plant must clear
`server/__pycache__`: a revert of the same size within the same second
reuses the planted bytecode.

## 5. Where new routes go from day one

- A new route is a `Route` in `server/lib/routes/<feature>.py` with its
  handler as a function `(handler, ...)` there; it never adds to `Handler`.
  It declares `admission`, `inner_checks` and `janitor_guard`, and the goldens
  are regenerated in the same commit.
- A ratchet test pins the sizes of `_GET_ROUTES`, `_POST_ROUTES`,
  `_PUT_ROUTES` and the number of `if` branches in each `do_*` method; they
  may only go down.
- Layering (lead decision): `lib.routes` is the HTTP adapter layer and is
  exempt from the rule-8 guard (no `handler._*` from `server/lib`), by name
  and only it; no other module in `server/lib` may import `lib.routes`, and
  `lib.routes` never imports `server` or `__main__`.
- Contract rules are unchanged: a route a client may use bumps
  `HOST_CONTRACT` and adds a `docs/compatibility.md` row.

## 6. How handlers move (Phase 2, after go/no-go 1)

Lead decision: `Handler` methods move byte-identical into feature mixin
classes in `server/lib/routes/<feature>.py`, and `Handler` inherits them.
The AST-identity check compares each moved function by name: identical
arguments, decorators and body; only the containing class and module change.
The move map records old qualified name -> new `module:Class.name`. Turning
mixin methods into `(req, ctx, resp)` functions is later, separate work with
its own review.

### Binding contract (finding 3)

AST identity does not prove that a moved function's global names still
resolve to the same objects. Each move therefore needs:

1. **Inventory.** For each function to move, its global loads
   (`LOAD_GLOBAL` names via `dis`, minus builtins), classified: imported
   module or function; immutable constant; **mutable or rebound state**
   (assigned at runtime, `global` statements, or patched by tests on the
   server module: `log`, `TurnDispatchService`, `OrchestratorService`,
   `TTSWorker`, `_STT_ACTIVATION_LOCK`, `SERVER_TIMING`,
   `_deliver_decision_rows`, `eventlog`, `agents_db`, `BIND_ADDR`); and
   **composition-root state** (`_CFG`, `PORT`, `BIND_ADDR`, `_runtime_paths`).
2. **Preparatory commit, separate from the move** (lead decisions, 3 Nov; composition-root state now goes on the server object, see section 10:
   S3 owns composition-root state that moved handlers read).
   - **Composition-root state** (`_CFG`, `PORT`, `BIND_ADDR`,
     `_runtime_paths`) is exposed on the server object: `ContextHTTPServer`
     carries the very object server.py holds (`self.boot_config = _CFG`),
     never a fresh `config.load()` (that would turn the boot snapshot into live
     config, see HTTP-12). The handler is edited **in server.py** to read it
     from `self.server`, and a test asserts identity under each load alias, so
     the later move is byte-identical. New composition-root state goes into
     `ServerContext` unless the same constraint applies; no further server
     attributes without the lead (decision A, 3 Nov, replacing the earlier
     ServerContext accessor).
   - **Mutable module state** only moving code uses (`_OUTDATED_CLIENTS_NOTED`,
     `_CARTESIA_PREVIEW_LOCKS`) moves to a leaf owner module under
     `lib.routes`, and server.py binds the same object from it.
   - Test patch sites that rebind such a name move to the new owner in the
     same commit (test-only edits). The commit is reviewed as a
     behaviour-neutral rebinding, with both route goldens and a replay run.
   - A moved module never imports server.py. A name that fits neither route
     keeps its function in server.py for now.
3. **Move commit.** Pure move. A test loads server.py under the production
   path name and under each test alias (`claude_pwa_server`,
   `clarp_route_probe_server`) and asserts, for every global loaded by a
   moved function, that the name in the mixin module **is** the object the
   server module holds. A second test imports every `lib.routes` module in a
   fresh interpreter and asserts that no `server` or `__main__`-from-server.py
   module gets loaded. `lib.routes` never imports `server`, so
   there is no cycle and no second composition root.
4. Mutable state that only the moved code uses (`_OUTDATED_CLIENTS_NOTED`,
   `_CARTESIA_PREVIEW_LOCKS`) moves with it in the preparatory commit.

The oracle modules that call `handler._stop_agent_session` and
`controller_actions_http` keep working because the method stays on
`Handler` through inheritance; the move map lists them.

## 7. Order of moves (lowest risk first)

0. Seam: `lib/routes/registry.py` and the dispatcher call; no handler moves.
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
10. `/log` and the delivery bracket: last, with the runtime boundary owner
    (reviewer A).

Each group: characterization and G0 first, then the preparatory binding
commit if any, then move-only commits of at most ~800 lines, each with G1,
the AST-identity and binding-identity checks, both route goldens unchanged
and replay (G2). Streaming routes (`/events`, terminals, clip streams, HLS,
growing audio, STT stream, Live sockets) move last within their group; the
response golden pins only their status line, byte-stream parity comes from
the harness.

## 8. Coverage entry gate (G0), for every group

Every group that moves, 1 to 10 alike, enters only with characterization
tests committed first and line coverage of exactly the functions it moves
(and their adapters) at or above 85% on the full suite, measured with
`--cov=server` on the buildbox (`heavy-remote`), or with an exception the
lead has signed that names the uncovered functions and why (finding 4).
G1 and the AST and binding checks do not replace G0. Gaps are closed with
characterization tests against today's code, never by changing it.

## 9. Decided and open

Decided by the lead on 3 Nov: unmatched-path gate answers stay as today;
mixins are the move vehicle with the by-name AST identity check; the package
is `server/lib/routes`, exempt from rule 8 by name, imported by nothing else
in `server/lib`.

Decided by the lead on 3 Nov: S3 owns composition-root state that moved
handlers read; it reaches them through the server object (`srv.boot_config`,
identity-tested), never through `config.load()` and never by importing
server.py (section 6, step 2; section 10).

Open: none.

## 10. Move map

Old location -> new location, recorded as each commit lands. Old imports keep
working unless a row says otherwise.

| Commit | Old | New | Kind |
|---|---|---|---|
| seam | `server.py:Handler.do_GET/POST/PUT/DELETE/PATCH` route chains (after the gate prelude) | `lib/routes/registry.py:GET/POST/PUT/DELETE/PATCH` Routes + `dispatch()`; route tables stay on `Handler` | move (adapters verbatim, `self` -> `handler`) |
| rebinding | `_handle_diagnostics_health` reads module global `_CFG.delivery` | reads `self.server.boot_config.delivery` | composition-root state on the server object (lead decision A, 3 Nov): `ContextHTTPServer.__init__` sets `self.boot_config = _CFG`, the same object, so `build_server`'s server, directly built servers and the LAN listener subclass all carry it; never `config.load()`; no further server attributes without the lead |

Layer rules enforced by `tests/integration/test_route_registry.py`:
`lib.routes` never imports `server` or `__main__`; no module in `server/lib`
outside `lib.routes` imports it.
