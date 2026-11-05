"""Silently swallowed broad excepts, in ratchet mode (done target: none left).

A handler counts when it catches everything, and its body does nothing an
operator could see. Catching everything means a bare `except:`, or any form
naming Exception or BaseException, including a tuple or `builtins.Exception`.
Doing nothing visible means only `pass`, `continue`, a constant expression
(`...` or a docstring), or `return` of None, False, 0, "" or an empty
collection: an empty literal, or set(), frozenset(), list(), tuple() or
dict() called with no arguments (a call with any argument is not counted). The program survey counted 132 with a narrower rule (a bare
Name only, no `...`, no empty collections); this rule finds 136 in 64 modules
on main a78affb9.

Ratchet mode: the count per module is the baseline. More in a module fails.
Fewer fails until BASELINE is lowered, so the list only shrinks. A swallow
that logs, records or re-raises is not counted: fix one by logging it
(log_exception) or narrowing the exception type, then lower the baseline.
"""
from __future__ import annotations

import ast
import collections
import pathlib

SERVER = pathlib.Path(__file__).resolve().parents[2] / "server"
_BROAD = frozenset({"Exception", "BaseException"})
# Builtins whose no-argument call is an empty collection (set() has no literal).
_EMPTY_CALLS = frozenset({"set", "frozenset", "list", "tuple", "dict"})

BASELINE: dict[str, int] = {
    'lib/activity.py': 1,
    'lib/agent_ring_live.py': 2,
    'lib/agent_ring_live_socket.py': 5,
    'lib/agent_rings.py': 1,
    'lib/agy_transcript.py': 1,
    'lib/apns.py': 6,
    'lib/artifacts.py': 1,
    'lib/audio_stream.py': 3,
    'lib/backend/agy.py': 1,
    'lib/backend/agy_hooks.py': 1,
    'lib/backend/claude.py': 1,
    'lib/backend/grok.py': 1,
    'lib/backend/muse.py': 1,
    'lib/backend/opencode.py': 2,
    'lib/backend/opencode_serve.py': 1,
    'lib/cartesia_stt_ws.py': 1,
    'lib/cartesia_tts.py': 3,
    'lib/cartesia_ws.py': 3,
    'lib/clip_pacing.py': 2,
    'lib/clip_stream.py': 2,
    'lib/codex_app_server.py': 2,
    'lib/codex_transcript.py': 1,
    'lib/eleven_ws.py': 3,
    'lib/eventlog.py': 1,
    'lib/hook_runtime.py': 1,
    'lib/janitor_autonomy.py': 1,
    'lib/janitor_http.py': 1,
    'lib/live_activity.py': 2,
    'lib/live_explain.py': 1,
    'lib/live_hub.py': 3,
    'lib/live_pacing.py': 1,
    'lib/log.py': 2,
    'lib/maintenance.py': 2,
    'lib/message_writes.py': 1,
    'lib/oracle_calls.py': 1,
    'lib/oracle_calls_stable.py': 1,
    'lib/oracle_gemini_live.py': 2,
    'lib/oracle_live.py': 2,
    'lib/oracle_live_calls.py': 6,
    'lib/oracle_live_provider.py': 3,
    'lib/oracle_live_stable.py': 3,
    'lib/oracle_realtime.py': 2,
    'lib/oracle_webrtc.py': 1,
    'lib/provider_capabilities.py': 4,
    'lib/reconcile.py': 3,
    'lib/relay_connector.py': 2,
    'lib/runtime_bridge.py': 2,
    'lib/server_identity.py': 2,
    'lib/snapshot.py': 1,
    'lib/state_watcher.py': 1,
    'lib/stt.py': 2,
    'lib/stt_stream.py': 5,
    'lib/transcript_streamer.py': 1,
    'lib/transcript_watcher.py': 2,
    'lib/tts_engine.py': 3,
    'lib/tts_service.py': 1,  # S7: existing worker._emit moved here as emit_event.
    'lib/tts_worker.py': 1,
    'lib/turn_dispatch.py': 7,
    'lib/turn_lifecycle.py': 1,
    'lib/turn_slots.py': 2,
    'lib/viz_work.py': 3,
    'lib/vocab_store.py': 1,
    'lib/voice_humanness.py': 1,
    'lib/voice_preamble.py': 2,
    'server.py': 6,
}


def _catches_everything(node: ast.expr | None) -> bool:
    if node is None:
        return True
    names = node.elts if isinstance(node, ast.Tuple) else [node]
    return any((isinstance(n, ast.Name) and n.id in _BROAD)
               or (isinstance(n, ast.Attribute) and n.attr in _BROAD) for n in names)


def _silent(stmt: ast.stmt) -> bool:
    if isinstance(stmt, (ast.Pass, ast.Continue)):
        return True
    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
        return True
    if isinstance(stmt, ast.Return):
        value = stmt.value
        if value is None:
            return True
        if isinstance(value, ast.Constant) and value.value in (None, False, 0, ""):
            return True
        if isinstance(value, (ast.List, ast.Tuple, ast.Set)) and not value.elts:
            return True
        if isinstance(value, ast.Dict) and not value.keys:
            return True
        if (isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id in _EMPTY_CALLS
                and not value.args and not value.keywords):
            return True
    return False


def swallowed(tree: ast.AST) -> list[int]:
    """Line numbers of silently swallowed broad excepts."""
    return [h.lineno for h in ast.walk(tree)
            if isinstance(h, ast.ExceptHandler) and _catches_everything(h.type)
            and all(_silent(s) for s in h.body)]


def current() -> dict[str, int]:
    from tests.patching import logical_split_tree_map
    counts: collections.Counter[str] = collections.Counter()
    paths = [path for path in sorted(SERVER.rglob("*.py")) if "tests" not in path.parts]
    # Only exact recipe-declared definitions keep their original module's
    # debt. Undeclared code in a generated part remains a physical owner.
    for origin, tree in logical_split_tree_map(paths, SERVER).items():
        found = swallowed(tree)
        if found:
            counts[origin] = len(found)
    return dict(counts)


def test_no_new_silently_swallowed_excepts():
    now = current()
    grew = {m: (BASELINE.get(m, 0), n) for m, n in sorted(now.items()) if n > BASELINE.get(m, 0)}
    assert not grew, ("a broad except that swallows silently was added (baseline, now); log it with "
                      f"log_exception or narrow the exception type: {grew}")


def test_baseline_only_shrinks():
    now = current()
    lower = {m: (n, now.get(m, 0)) for m, n in sorted(BASELINE.items()) if now.get(m, 0) < n}
    assert not lower, f"lower BASELINE to match (baseline, now): {lower}"


def test_the_scanner_sees_every_silent_form():
    tree = ast.parse(
        "def f():\n"
        "    try: pass\n"
        "    except: pass\n"
        "    try: pass\n"
        "    except Exception: return None\n"
        "    try: pass\n"
        "    except (OSError, Exception): ...\n"
        "    try: pass\n"
        "    except builtins.BaseException: return []\n"
        "    for x in y:\n"
        "        try: pass\n"
        "        except Exception: continue\n"
        "    try: pass\n"
        "    except Exception as exc: log_exception('x', exc)\n"
        "    try: pass\n"
        "    except OSError: pass\n"
        "    try: pass\n"
        "    except Exception: raise\n"
        "    try: pass\n"
        "    except Exception: return 1\n"
        "    try: pass\n"
        "    except Exception: return set()\n"
        "    try: pass\n"
        "    except Exception: return {}\n"
        "    try: pass\n"
        "    except Exception: return dict()\n"
        "    try: pass\n"
        "    except Exception: return {1}\n"
        "    try: pass\n"
        "    except Exception: return set(x)\n"
        "    try: pass\n"
        "    except Exception: return frozenset\n")
    assert len(swallowed(tree)) == 8


def test_declared_swallow_moves_with_its_source_but_new_code_stays_physical(tmp_path, monkeypatch):
    import sys
    import pytest
    from tests import patching
    root = tmp_path / 'server'
    (root / 'lib').mkdir(parents=True)
    (root / 'server.py').write_text('')
    body = 'def moved():\n    try: pass\n    except Exception: pass\n'
    original = root / 'lib/old.py'
    original.write_text(body)
    recipes = tmp_path / 'splits'
    recipes.mkdir()
    monkeypatch.setattr(patching, '_RECIPES', recipes)
    monkeypatch.setattr(sys.modules[__name__], 'SERVER', root)
    monkeypatch.setattr(sys.modules[__name__], 'BASELINE', {'lib/old.py': 1})
    assert current() == {'lib/old.py': 1}
    (recipes / 's4.toml').write_text(
        '[[split]]\nmodule="lib/old.py"\n'
        '[[split.part]]\nmodule="lib/part.py"\nnames=["moved"]\n')
    original.write_text('from .part import moved\n')
    part = root / 'lib/part.py'
    part.write_text(body)
    assert current() == {'lib/old.py': 1}
    added = '\ndef new():\n    try: pass\n    except Exception: return None\n'
    part.write_text(body + added)
    assert current() == {'lib/old.py': 1, 'lib/part.py': 1}
    with pytest.raises(AssertionError, match='lib/part.py'):
        test_no_new_silently_swallowed_excepts()
    part.write_text(body.replace('except Exception: pass', 'except Exception: raise') + added)
    with pytest.raises(AssertionError, match='lib/old.py'):
        test_baseline_only_shrinks()
