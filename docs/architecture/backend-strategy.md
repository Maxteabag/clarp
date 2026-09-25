# Backends as strategies

Status: accepted 2026-09-25. Owner: Peter. This document is the contract for
the `refactor/backend-strategy` change and for every backend added after it.

## The rule

The host never spells a backend's name. It asks the backend object to do the
work. A backend is one class per CLI, holding the logic that is specific to
that CLI. Shared behaviour lives in a base class or a helper module the
backend classes call; it never lives in the host behind a flag.

Two consequences:

* No `if backend == X` anywhere outside `server/lib/backend/registry.py`.
  A guard test enforces this over `server/lib` and `server/server.py`.
* No boolean capability that gates a backend-named call. If the host would
  write `if adapter.does_x: _do_x_for_codex()`, the backend gets a method
  `do_x()` and the host calls it unconditionally; backends that have nothing
  to do implement it as a no-op.

Values that are genuinely data stay as attributes on the backend object:
`context_window`, `model_family`, `janitor_default_model`, `api_providers`,
`login_kind`, `effort_ui`, `effort_scope`, the fallback model table, the
required binary. A class full of one-line getters is the wrong shape.

## Layout

```
server/lib/backend/
  __init__.py     nothing but the package docstring
  base.py         Backend: the operations the host needs, with docstrings
  stream_json.py  StreamJsonBackend(Backend): spawn/drain/speak/live text
                  shared by the JSON-lines CLIs (today's runner_common)
  claude.py       ClaudeBackend(Backend)
  codex.py        CodexBackend(StreamJsonBackend)
  agy.py          AgyBackend(StreamJsonBackend)
  grok.py         GrokBackend(StreamJsonBackend)
  opencode.py     OpenCodeBackend(StreamJsonBackend)
  deepseek.py     DeepSeekBackend(OpenCodeBackend): composition over OpenCode
  registry.py     by_id(), for_agent(), all(), the only place ids are listed
server/lib/backends.py   facade: constants, normalize(), by_id(), for_agent()
```

`backends.py` stays the import path callers use; it re-exports from the
package. The `*_runner.py` and `*_transcript.py` modules keep their names
while callers migrate, then become the bodies of the classes (runners) or
stay as parsers the classes call (transcripts).

## What a Backend owns

Methods, each with a real body in the subclass or the shared base:

| Method | Notes |
|---|---|
| `spawn_turn(**spec) -> TurnHandle` | the runner |
| `interrupt(agent_id) -> int`, `active_handles(agent_id)` | process registry |
| `resume_target(session_id, cwd, home)` | transcript path for Claude, the id for the rest, `None` when a bound session has nothing to resume |
| `bind_new_session(agent_id, session) -> str` | Claude pre-mints a UUID; others return "" |
| `find_transcript(session_id, home)`, `parse_transcript(path)`, `list_sessions(cwd, limit, all_projects)` | transcript access |
| `terminal_argv(session_id) -> list[str]` | raises `Unsupported` when the CLI has no interactive mode |
| `on_credential_change()` | Codex recycles its app-server writers; others no-op |
| `recover_usage_limit(message) -> bool` | Codex reconnects; others return False |
| `account_pool() -> str` | "" when the CLI has no account switching |
| `default_model_effort(cfg) -> tuple[str, str]`, `is_valid_model(model) -> bool` | model policy |
| `recorded_model(session_id) -> str` | what the session actually ran |
| `compaction(session) -> CompactionStrategy` | how to compact |
| `wrap_turn_callback(fn)` | Claude serialises under the dispatch lock; others return `fn` |
| `arm_source_marker(session, trace_id, synthesize_audio)` | Claude writes the hook marker; others no-op |
| `classify_usage_limit(...)`, `quota_identity(window)` | usage events |
| `executable() -> str` | Claude reads the host setting |

Anything the host does that differs per backend and is not in this table is
a new method, not a new flag.

## Sharing

* `StreamJsonBackend` carries the subprocess contract, the drain thread, the
  `<speak>` extraction, the live assistant row cadence, state_log writes and
  transcript broadcasts. Subclasses override the event mapping.
* Small pure helpers may live in modules (`voice_preamble.py`,
  `process_registry.py`, `proc_util.py`). They take explicit arguments and
  never inspect a backend id.
* `DeepSeekBackend` subclasses `OpenCodeBackend` and pins the model. It is
  composition over an existing strategy, not a copy.
* No `isinstance` checks on backend classes outside the registry and tests.

## Migration slices

Each slice is one commit on the branch, gated by the full suite against the
clean baseline, behaviour-preserving unless a line here says otherwise.

1. Package, `Backend` base, registry, six subclasses whose methods delegate
   to today's runner modules and adapter callables. `backends.py` gains
   `by_id()`/`for_agent()` and keeps `adapter_for()`. Guard test extended to
   forbid new identity branches in the package's callers.
2. Credential change, terminal argv, usage-limit recovery, account pool,
   session binding and source marker move to methods; the corresponding
   adapter flags are deleted. The two `_recycle_codex_writers()` call sites
   become `backend.on_credential_change()`.
3. `runner_common.py` becomes `stream_json.py`; the five runner modules
   become the subclass bodies; the modules keep thin delegators for the
   tests that patch them. Each backend carries `runner`, the short name
   that prefixes its log events, drain threads and `dispatch` tags and
   names its `lib.<runner>_runner` module (DeepSeek's is OpenCode's, so it
   shares OpenCode's process registry). Until slice 5, `Backend._hook`
   and the `@hooked` methods let a monkeypatched runner-module global
   intercept the class body; the guard test keeps CLI names out of the
   package, so `configured_claude_bin` stays on `clarp_runner`.
4. Dispatch-side decisions (resume target, callback wrapping, usage
   classification, compaction, recorded model, transcript access) move to
   methods; the remaining behavioural adapter fields are deleted.
5. `BackendAdapter` is reduced to the data attributes and merged into
   `Backend`; `adapter_for()` becomes an alias of `by_id()`; the runner
   delegators that no caller uses are removed; the guard test bans
   `backends.X ==` and `in {backends.` outright.
