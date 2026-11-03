"""Route goldens: today's HTTP routing and auth answers of server.py, pinned.

Characterization for the server refactor (REFACTOR.md, slice S3). Every
method (GET, POST, PUT, DELETE, PATCH) is asked for every known path: the
exact route tables, a sample of every prefix/suffix branch, the library
routers server.py hands off to, and the fall-through. For each request the
golden records what the dispatch reaches for each principal (anonymous,
local without a token, limited device, full device, local administrator, and
a host with no admin token configured).

The golden records behaviour as it is, bugs included. Entries the review
flagged as pre-existing bugs (HTTP-01/02/03) are listed in KNOWN_ISSUES and
must stay visible in the golden until the fix lane fixes them.

Regenerate after an intended change with
    CLARP_UPDATE_ROUTE_GOLDENS=1 pytest tests/integration/test_route_goldens.py
and review the diff: every changed line is a routing or auth change.
"""
from __future__ import annotations

import json
import os
import pathlib

import pytest

from tests.integration import route_probe

GOLDEN_DIR = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "route_goldens"
DISPATCH_GOLDEN = GOLDEN_DIR / "dispatch.json"
UPDATE = os.environ.get("CLARP_UPDATE_ROUTE_GOLDENS") == "1"

# One sample per branch of the prefix/suffix chains in do_GET, do_POST,
# do_PUT, do_DELETE and do_PATCH, plus the library routers' own paths.
# Exact-table paths are added from the tables themselves.
DYNAMIC_SAMPLES = (
    # root static files, served outside the GET table
    "/manifest.json", "/styles.css", "/icon.png",
    # podcast history router (lib.podcast_history_http)
    "/podcast-history", "/podcast-history/sources", "/podcast-history/c1",
    "/podcast-history/c1/feedback",
    # janitor router (lib.janitor_http)
    "/janitors", "/janitor-triggers", "/janitor-policy", "/janitor-triggers/preview",
    "/janitor-triggers/t1", "/janitors/s1", "/janitors/s1/runs",
    "/janitors/s1/migration-state", "/janitors/s1/effective-model-chain",
    "/janitors/s1/pause", "/janitor-runs/r1/context", "/janitor-runs/r1/activity",
    "/janitor-runs/r1/review",
    # controller-action router (lib.controller_actions_http)
    "/controller-actions", "/controller-actions/audit", "/controller-actions/update",
    "/controller-actions/delete", "/controller-actions/mappings",
    "/controller-actions/a1/invoke",
    # teams
    "/teams/t1", "/teams/t1/messages", "/teams/t1/members", "/teams/t1/members/a1",
    # jobs, queue, transcription results, schedules
    "/background-jobs/j1", "/turn-queue/q1", "/turn-queue/q1/send",
    "/transcription-results/j1", "/schedules/s1",
    # artifacts
    "/artifacts/a1", "/artifacts/a%2F1", "/artifacts/a1/html",
    "/artifacts/a1/events-config", "/artifacts/a1/events", "/artifacts/a1/revisions",
    "/artifacts/a1/archive", "/artifacts/a1/discard", "/artifacts/a1/submit",
    "/artifacts/a1/pin",
    # decisions
    "/decisions/d1/resolve", "/decisions/d1/dismiss", "/decisions/d1/withdraw",
    "/decisions/d1/other",
    # agents and personas
    "/agents/a1", "/agents/a1/fork", "/personas/p1",
    # static, media and avatars
    "/static/app.js", "/static/../server.py", "/media/m1",
    "/agent-portraits/p1/content", "/notification-avatars/a1",
    "/viz/owner-avatar/owner", "/avatars/a1", "/persona-avatars/p1",
    "/audio/clip.mp3", "/audio/../secret",
    # streaming and terminals
    "/stt/stream", "/stt/streaming", "/server-terminal", "/terminal/s1",
    # clips
    "/clips/1/stream", "/clips/x/stream", "/clips/1/complete.mp3",
    "/clips/x/complete.mp3", "/clips/1/playlist.m3u8", "/clips/x/segment-1.aac",
    "/clips/1",
    # oracle gate without a matching route
    "/oracle", "/oracle/unknown",
    # query strings never take part in dispatch (HTTP-03 sample)
    "/past-sessions?scope=all", "/dirs?path=/",
    # fall-through
    "/unknown", "/devices/live-activity/x",
)

# Pre-existing bugs from the code review (http-shell.md at 09ee9279). The
# golden records them as they are; the fix lane changes them, not a move.
KNOWN_ISSUES = {
    "GET /terminal/s1": "HTTP-01: a limited device reaches the agent terminal",
    "PATCH /schedules/s1": "HTTP-02: do_PATCH skips the device scope gate",
    "GET /dirs": "HTTP-03: a limited device can browse host directories",
    "GET /dirs?path=/": "HTTP-03: a limited device can browse host directories",
    "GET /past-sessions": "HTTP-03: a limited device lists every project's sessions",
    "GET /past-sessions?scope=all": "HTTP-03: a limited device lists every project's sessions",
}

PRINCIPAL_ORDER = ("anonymous", "local_no_token", "limited", "full", "admin",
                   "open_local", "open_remote")


def _sample_paths(handler_cls) -> list[str]:
    paths = set(DYNAMIC_SAMPLES)
    for table in route_probe.route_tables(handler_cls).values():
        paths.update(table)
    return sorted(paths)


def _scope(answers: dict[str, str]) -> str:
    """Who gets past the gates: the least-trusted principal that does."""
    def reached(name: str) -> bool:
        target = answers[name]
        return not (target.startswith("error 401") or target.startswith("error 403")
                    or target.startswith("send 429"))
    if reached("anonymous"):
        return "public"
    if reached("limited"):
        return "limited"
    if reached("full"):
        return "full"
    if reached("admin"):
        return "local_admin"
    return "none"


@pytest.fixture(scope="module")
def server_module():
    return route_probe.load_server_module()


def _device_tokens() -> tuple[str, str]:
    from lib import device_pairing
    tokens = []
    for scope in ("limited", "full"):
        issued = device_pairing.issue(device_name=f"route golden {scope}", scope=scope)
        tokens.append(device_pairing.exchange(issued["code"])["token"])
    return tokens[0], tokens[1]


def build_dispatch_golden(server_module, monkeypatch, tmp_path) -> dict:
    (tmp_path / "static").mkdir(exist_ok=True)
    (tmp_path / "audio").mkdir(exist_ok=True)
    route_probe.patch_lib_targets(monkeypatch)
    probe = route_probe.make_probe_class(server_module.Handler)
    principals = route_probe.principals(*_device_tokens())
    routes = {}
    for path in _sample_paths(server_module.Handler):
        for method in route_probe.METHODS:
            answers = {
                name: route_probe.resolve(probe, tmp_path, method, path, *principals[name])
                for name in PRINCIPAL_ORDER
            }
            key = f"{method} {path}"
            entry = {"scope": _scope(answers), "target": answers["admin"]}
            # Only the principals whose answer differs from the administrator's.
            entry["others"] = {name: answers[name] for name in PRINCIPAL_ORDER
                               if answers[name] != answers["admin"]}
            if key in KNOWN_ISSUES:
                entry["known_issue"] = KNOWN_ISSUES[key]
            routes[key] = entry
    return {
        "_about": (
            "Generated by tests/integration/test_route_goldens.py. Per 'METHOD path': "
            "scope = least-trusted principal that passes the gates; target = what the "
            "local administrator reaches; others = what each principal whose answer "
            "differs reaches instead (anonymous, local_no_token, limited, full, "
            "open_local, open_remote; open_* = host without an admin token). "
            "'error 401/403 ...' and 'send 429' are gate refusals; 'send 404' is the "
            "fall-through. HEAD, OPTIONS and other methods have no do_ method and get "
            "the stdlib 501."
        ),
        "routes": routes,
    }


def _dump(golden: dict) -> str:
    """One route per line, so a diff of the golden reads as a list of routes."""
    lines = ["{", f' "_about": {json.dumps(golden["_about"])},', ' "routes": {']
    items = sorted(golden["routes"].items())
    for index, (key, entry) in enumerate(items):
        comma = "," if index < len(items) - 1 else ""
        lines.append(f"  {json.dumps(key)}: {json.dumps(entry, sort_keys=True)}{comma}")
    lines += [" }", "}"]
    return "\n".join(lines) + "\n"


def test_dispatch_golden(server_module, monkeypatch, tmp_path):
    actual = build_dispatch_golden(server_module, monkeypatch, tmp_path)
    if UPDATE or not DISPATCH_GOLDEN.exists():
        GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        DISPATCH_GOLDEN.write_text(_dump(actual))
        if not UPDATE:
            pytest.fail("route golden was missing and has been written; review and commit it")
    expected = json.loads(DISPATCH_GOLDEN.read_text())
    changed = sorted(
        key for key in set(expected["routes"]) | set(actual["routes"])
        if expected["routes"].get(key) != actual["routes"].get(key))
    assert not changed, (
        f"{len(changed)} route answers differ from the golden, first: "
        + json.dumps({k: {"golden": expected["routes"].get(k), "now": actual["routes"].get(k)}
                      for k in changed[:5]}, indent=1))


def test_every_table_route_is_in_the_golden(server_module):
    golden = json.loads(DISPATCH_GOLDEN.read_text())["routes"]
    for method, table in route_probe.route_tables(server_module.Handler).items():
        for path, name in table.items():
            assert golden[f"{method} {path}"]["target"] == f"{name}()", (method, path)


def test_known_issues_are_still_reproduced_by_the_golden():
    """Each pre-existing bug is visible in the golden: the limited device gets
    through. When the fix lane fixes one, it updates this list with the golden."""
    golden = json.loads(DISPATCH_GOLDEN.read_text())["routes"]
    for key in KNOWN_ISSUES:
        assert golden[key]["scope"] == "limited", key
        assert golden[key]["known_issue"] == KNOWN_ISSUES[key]


def test_unsupported_methods_get_the_stdlib_501(server_module):
    handler = server_module.Handler
    assert {m for m in ("HEAD", "OPTIONS", "TRACE", "CONNECT")
            if hasattr(handler, "do_" + m)} == set()
