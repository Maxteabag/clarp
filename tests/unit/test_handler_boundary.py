"""Rule 8 of docs/architecture/state-and-boundaries.md: the handler is the
only HTTP surface (HTTP-06).

A library module takes a Principal and a Responder from http_utils; it never
calls the handler's private methods, reads its private request state or writes
to a socket's wfile. This scans server/lib with the AST for `<…handler…>._x`,
`getattr(<…handler…>, "_x")` and any `.wfile`, and counts them per module and
name. Ratchet mode: today's uses are the baseline, a new use or a higher count
fails, and a lower count fails until BASELINE is lowered to match, so the list
only shrinks. http_utils is the one sanctioned bridge, and lib/routes/ (slice
S3's route modules) is the handler layer by design.

Limits: this is a volume ratchet per (module, name), so replacing one use with
another of the same key in the same module passes; review catches such
relocations. The handler is recognised by name ("handler" in the variable or
attribute), so an alias with another name is not seen.
"""
from __future__ import annotations

import ast
import collections
import pathlib

LIB = pathlib.Path(__file__).resolve().parents[2] / "server" / "lib"
EXEMPT_FILES = {"lib/http_utils.py"}  # builds Principal and Responder from the handler
EXEMPT_PREFIXES = ("lib/routes/",)    # the route layer (S3) is the handler surface

_STREAM = "streams a response body straight to the socket; move behind a Responder stream"
_PRIVATE = "calls or reads handler internals; take a Principal/Responder instead"
BASELINE: dict[tuple[str, str], int] = {
    ("lib/agent_ring_live_socket.py", "wfile"): 7,
    ("lib/audio_growing.py", "wfile"): 6,
    ("lib/clip_delivery/hls.py", "wfile"): 1,
    ("lib/clip_stream.py", "wfile"): 9,
    ("lib/controller_actions_http.py", "_json"): 1,
    ("lib/controller_actions_http.py", "_json_error"): 8,
    ("lib/controller_actions_http.py", "_json_ok"): 3,
    ("lib/controller_actions_http.py", "_query"): 1,
    ("lib/controller_actions_http.py", "_read_json"): 1,
    ("lib/controller_actions_http.py", "_request_auth_validated"): 1,
    ("lib/controller_actions_http.py", "_request_is_local"): 1,
    ("lib/controller_actions_http.py", "_request_principal"): 2,
    ("lib/controller_actions_http.py", "_turn_caller"): 1,
    ("lib/oracle_live.py", "_stop_agent_session"): 1,
    ("lib/oracle_live.py", "wfile"): 6,
    ("lib/oracle_live_calls.py", "_stop_agent_session"): 1,
    ("lib/oracle_live_stable.py", "_stop_agent_session"): 1,
    ("lib/oracle_live_stable.py", "wfile"): 6,
    ("lib/oracle_realtime.py", "wfile"): 5,
    # Its own socketserver handler for the runtime RPC socket, not the HTTP one.
    ("lib/runtime_rpc_server.py", "wfile"): 4,
    ("lib/stt_stream.py", "wfile"): 6,
    ("lib/terminal_ws.py", "wfile"): 6,
}
REASONS = {"wfile": _STREAM}


def _is_handler(node: ast.AST) -> bool:
    name = node.id if isinstance(node, ast.Name) else getattr(node, "attr", "")
    return "handler" in name.lower()


def handler_uses(tree: ast.AST) -> collections.Counter[str]:
    found: collections.Counter[str] = collections.Counter()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            if node.attr == "wfile":
                found["wfile"] += 1
            elif node.attr.startswith("_") and not node.attr.startswith("__") and _is_handler(node.value):
                found[node.attr] += 1
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
              and node.func.id in ("getattr", "setattr", "hasattr") and len(node.args) >= 2
              and _is_handler(node.args[0]) and isinstance(node.args[1], ast.Constant)
              and str(node.args[1].value).startswith("_")):
            found[str(node.args[1].value)] += 1
    return found


def current() -> dict[tuple[str, str], int]:
    out = {}
    for path in sorted(LIB.rglob("*.py")):
        rel = path.relative_to(LIB.parent).as_posix()
        if rel in EXEMPT_FILES or rel.startswith(EXEMPT_PREFIXES):
            continue
        for name, count in handler_uses(ast.parse(path.read_text(), filename=str(path))).items():
            out[(rel, name)] = count
    return out


def test_library_modules_do_not_reach_into_the_handler():
    now = current()
    grew = [f"{rel}: {name} x{count} (baseline {BASELINE.get((rel, name), 0)}): "
            f"{REASONS.get(name, _PRIVATE)}"
            for (rel, name), count in sorted(now.items()) if count > BASELINE.get((rel, name), 0)]
    assert not grew, "new handler access from a library module (rule 8):\n" + "\n".join(grew)


def test_baseline_only_shrinks():
    now = current()
    lower = [f"{rel}: {name} baseline {count}, now {now.get((rel, name), 0)}"
             for (rel, name), count in sorted(BASELINE.items()) if now.get((rel, name), 0) < count]
    assert not lower, "lower BASELINE to match (the ratchet only goes down):\n" + "\n".join(lower)


def test_the_scanner_sees_every_form():
    tree = ast.parse(
        "def f(handler, self):\n"
        "    handler._json_error(400, 'x')\n"
        "    self.handler._query()\n"
        "    getattr(handler, '_request_principal', '')\n"
        "    handler.wfile.write(b'')\n"
        "    handler.send_response(200)\n"
        "    handler.__class__\n"
        "    other._private\n")
    assert handler_uses(tree) == {"_json_error": 1, "_query": 1, "_request_principal": 1, "wfile": 1}
