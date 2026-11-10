"""In-process dispatch probe for server.py's HTTP routes.

Runs the real `Handler.do_<METHOD>` methods (auth gate, oracle gate, device
scope gate, janitor and controller-action hand-offs, the exact route tables
and the prefix chains) without a socket, and records the first thing each
request reaches instead of running it: a handler method, a library router or
an inline response. Nothing behind the dispatch runs, so the probe is cheap,
deterministic and has no side effects.

The route goldens (test_route_goldens.py) use it to pin today's routing and
auth answers; the route-registry seam reuses it as its parity oracle.
"""
from __future__ import annotations

import email.message
import importlib.util
import pathlib
from types import SimpleNamespace

SERVER_DIR = pathlib.Path(__file__).resolve().parents[2] / "server"

METHODS = ("GET", "POST", "PUT", "DELETE", "PATCH")

# Handler methods the dispatch chains call as a route target. Any method
# named in a route table is added at probe time.
_TARGET_METHODS = (
    "_send_root_static", "_send_app_asset", "_send_file", "_send_complete_clip",
)

# Library entry points the dispatch chains hand a request to.
LIB_TARGETS = (
    ("janitor_http", "handle"),
    ("controller_actions_http", "handle"),
    ("podcast_history_http", "handle"),
    ("viz_owner_avatar", "portrait"),
    ("html_report_revisions", "revisions"),
    ("audio_growing", "serve_growing"),
    ("stt_stream", "serve_stt_stream"),
    ("terminal_ws", "serve_server_terminal"),
    ("terminal_ws", "serve_terminal"),
    ("clip_stream", "serve_clip_stream"),
    ("clip_delivery.hls", "serve_hls_artifact"),
)

ADMIN_TOKEN = "route-golden-admin-token"
REMOTE_PEER = "100.64.0.7"
LOCAL_PEER = "127.0.0.1"


class Resolved(Exception):
    def __init__(self, target: str):
        super().__init__(target)
        self.target = target


def load_server_module():
    spec = importlib.util.spec_from_file_location(
        "clarp_route_probe_server", SERVER_DIR / "server.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _arg(value) -> str:
    if isinstance(value, (str, int)):
        return repr(value)
    return type(value).__name__


def _record(name: str):
    def recorder(self, *args, **kwargs):
        shown = [_arg(a) for a in args]
        shown += [f"{k}={_arg(v)}" for k, v in sorted(kwargs.items())]
        raise Resolved(f"{name}({', '.join(shown)})")
    recorder.__name__ = name
    return recorder


def _record_lib(qualified: str):
    def recorder(*args, **kwargs):
        # Library routers receive the handler first; show the other args.
        shown = [_arg(a) for a in args[1:]] if args and hasattr(args[0], "do_GET") else [
            _arg(a) for a in args]
        raise Resolved(f"lib.{qualified}({', '.join(shown)})")
    return recorder


def route_tables(handler_cls) -> dict[str, dict[str, str]]:
    return {
        "GET": dict(handler_cls._GET_ROUTES),
        "POST": dict(handler_cls._POST_ROUTES),
        "PUT": dict(handler_cls._PUT_ROUTES),
    }


def make_probe_class(handler_cls):
    names = set(_TARGET_METHODS)
    for table in route_tables(handler_cls).values():
        names.update(table.values())
    names.update(n for n in dir(handler_cls) if n.startswith("_handle_"))
    attrs = {name: _record(name) for name in names}

    def _send(self, code, body=b"", *args, **kwargs):
        raise Resolved(f"send {int(code)}")

    def _json_error(self, code, message):
        raise Resolved(f"error {int(code)} {message}")

    def _json_ok(self, payload):
        raise Resolved("json_ok")

    attrs.update(_send=_send, _json_error=_json_error, _json_ok=_json_ok)
    return type("RouteProbe", (handler_cls,), attrs)


def patch_lib_targets(monkeypatch) -> None:
    import importlib
    for module_name, attr in LIB_TARGETS:
        module = importlib.import_module(f"lib.{module_name}")
        monkeypatch.setattr(module, attr, _record_lib(f"{module_name}.{attr}"))


# A principal is (peer address, headers, admin token configured on the host).
# Device tokens are filled in by `principals()`.
def principals(limited_token: str, full_token: str) -> dict[str, tuple[str, dict, str]]:
    return {
        "anonymous": (REMOTE_PEER, {}, ADMIN_TOKEN),
        "local_no_token": (LOCAL_PEER, {}, ADMIN_TOKEN),
        "limited": (REMOTE_PEER, {"Authorization": f"Bearer {limited_token}"}, ADMIN_TOKEN),
        "full": (REMOTE_PEER, {"Authorization": f"Bearer {full_token}"}, ADMIN_TOKEN),
        "admin": (LOCAL_PEER, {"Authorization": f"Bearer {ADMIN_TOKEN}"}, ADMIN_TOKEN),
        # A host with no admin token configured trusts local requests only.
        "open_local": (LOCAL_PEER, {}, ""),
        "open_remote": (REMOTE_PEER, {}, ""),
    }


def resolve(probe_cls, ctx_root: pathlib.Path, method: str, path: str,
            peer: str, headers: dict, auth_token: str) -> str:
    """The first target `METHOD path` reaches for this principal."""
    from lib.request_security import FailureLimiter

    handler = probe_cls.__new__(probe_cls)
    message = email.message.Message()
    for key, value in headers.items():
        message[key] = value
    handler.headers = message
    handler.path = path
    handler.command = method
    handler.client_address = (peer, 50000)
    handler.server = SimpleNamespace(
        ctx=SimpleNamespace(
            auth_token=auth_token,
            static=ctx_root / "static",
            audio_dir=ctx_root / "audio",
            clip_broker=SimpleNamespace(),
        ),
        local_tls=False,
        # Fresh per request: a shared limiter turns the 21st 401 into a 429.
        auth_failures=FailureLimiter(),
    )
    try:
        getattr(handler, "do_" + method)()
    except Resolved as resolved:
        return resolved.target
    return "returned without a response"
