"""Characterization of the /vocab/* admin routes as server.py serves them today.

Written before the S3 move of the vocabulary handlers (REFACTOR.md): it pins
statuses, error messages and payloads of the HTTP layer, not vocab_store's
own rules, which tests/unit covers.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from tests.integration.test_server_di import (  # noqa: F401 - fixtures
    _model_catalog_never_probes_installed_clis, fake_ctx, running_server)


def _call(base: str, method: str, path: str, body=None, raw: bytes | None = None):
    data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
    request = urllib.request.Request(base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"null")


def test_packs_and_terms_round_trip(running_server):
    base, _ctx, _srv = running_server
    post = lambda path, body: _call(base, "POST", path, body)  # noqa: E731

    assert post("/vocab/packs", {}) == (400, {"error": "name is required"})
    assert post("/vocab/packs", {"name": "P", "kind": "odd"}) == (
        400, {"error": "kind must be static or dynamic"})
    assert _call(base, "POST", "/vocab/packs", raw=b"[1]") == (400, {"error": "bad json"})
    status, created = post("/vocab/packs", {
        "name": "Ships", "priority": 2, "floor": 1,
        "terms": ["Argo", {"text": "Nautilus", "rarity": 0.9, "say_as": "naw-ti-lus"}, 7]})
    assert status == 200 and created["ok"] is True
    pack_id = created["pack_id"]
    assert post("/vocab/packs", {"name": "Ships"}) == (
        409, {"error": "a pack with that name exists"})
    assert post("/vocab/packs", {"name": "Bad", "priority": "high"})[0] == 400

    status, listed = _call(base, "GET", "/vocab/packs")
    assert status == 200
    ships = next(p for p in listed["packs"] if p["pack_id"] == pack_id)
    assert ships["name"] == "Ships" and ships["terms"] == 2

    assert _call(base, "GET", "/vocab/terms") == (400, {"error": "pack_id is required"})
    status, terms = _call(base, "GET", f"/vocab/terms?pack_id={pack_id}")
    assert status == 200 and terms["pack_id"] == pack_id
    assert sorted(t["text"] for t in terms["terms"]) == ["Argo", "Nautilus"]

    assert post("/vocab/terms", {"pack_id": pack_id}) == (
        400, {"error": "pack_id and text are required"})
    assert post("/vocab/terms", {"pack_id": pack_id, "text": "Kon-Tiki"}) == (
        200, {"ok": True, "added": True})
    assert post("/vocab/terms", {"pack_id": pack_id, "text": "X", "rarity": "rare"})[0] == 400

    term_id = next(t["term_id"] for t in _call(
        base, "GET", f"/vocab/terms?pack_id={pack_id}")[1]["terms"] if t["text"] == "Argo")
    assert post("/vocab/terms/update", {}) == (400, {"error": "term_id is required"})
    assert post("/vocab/terms/update", {"term_id": term_id, "say_as": "ar-go"}) == (
        200, {"ok": True, "updated": True})
    assert post("/vocab/terms/update", {"term_id": term_id, "text": "Nautilus"}) == (
        409, {"error": "that term already exists in the pack"})
    assert post("/vocab/terms/delete", {"term_id": "x"}) == (400, {"error": "term_id is required"})
    assert post("/vocab/terms/delete", {"term_id": term_id}) == (200, {"ok": True})

    assert post("/vocab/packs/update", {}) == (400, {"error": "pack_id is required"})
    assert post("/vocab/packs/update", {"pack_id": pack_id, "enabled": False,
                                        "name": "Boats"}) == (200, {"ok": True})
    post("/vocab/packs", {"name": "Planes"})
    assert post("/vocab/packs/update", {"pack_id": pack_id, "name": "Planes"}) == (
        409, {"error": "a pack with that name exists"})
    assert post("/vocab/packs/delete", {}) == (400, {"error": "pack_id is required"})
    assert post("/vocab/packs/delete", {"pack_id": pack_id}) == (200, {"ok": True})
    assert all(p["pack_id"] != pack_id for p in _call(base, "GET", "/vocab/packs")[1]["packs"])


def test_profiles_assignments_and_runs(running_server):
    base, _ctx, _srv = running_server
    post = lambda path, body: _call(base, "POST", path, body)  # noqa: E731
    pack_id = post("/vocab/packs", {"name": "Crew"})[1]["pack_id"]

    assert post("/vocab/profiles", {}) == (400, {"error": "name is required"})
    status, created = post("/vocab/profiles", {"name": "Deck", "pack_ids": [pack_id]})
    assert status == 200 and created["ok"] is True
    profile_id = created["profile_id"]
    assert post("/vocab/profiles", {"name": "Deck"}) == (
        409, {"error": "a profile with that name exists"})

    status, profiles = _call(base, "GET", "/vocab/profiles")
    assert status == 200 and profile_id in [p["profile_id"] for p in profiles["profiles"]]
    assert _call(base, "GET", "/vocab/profile") == (404, {"error": "no such profile"})
    status, detail = _call(base, "GET", f"/vocab/profile?profile_id={profile_id}")
    assert status == 200 and detail["profile_id"] == profile_id

    assert post("/vocab/profiles/workspace", {}) == (400, {"error": "profile_id is required"})
    assert post("/vocab/profiles/workspace", {"profile_id": profile_id, "enabled": "yes"}) == (
        400, {"error": "enabled must be a boolean"})
    assert post("/vocab/profiles/workspace", {"profile_id": "nope", "enabled": True}) == (
        404, {"error": "no such profile"})
    assert post("/vocab/profiles/workspace", {"profile_id": profile_id, "enabled": True}) == (
        200, {"ok": True, "profile_id": profile_id, "harvests_workspace": True})

    assert post("/vocab/profiles/packs", {"profile_id": profile_id}) == (
        400, {"error": "profile_id and pack_id are required"})
    assert post("/vocab/profiles/packs", {"profile_id": profile_id, "pack_id": "nope"}) == (
        404, {"error": "no such profile or pack"})
    assert post("/vocab/profiles/packs", {"profile_id": profile_id, "pack_id": pack_id,
                                          "position": "first"})[0] == 400
    assert post("/vocab/profiles/packs/remove", {"profile_id": profile_id}) == (
        400, {"error": "profile_id and pack_id are required"})
    assert post("/vocab/profiles/packs/remove", {"profile_id": profile_id,
                                                 "pack_id": pack_id}) == (200, {"ok": True})
    assert post("/vocab/profiles/packs", {"profile_id": profile_id, "pack_id": pack_id}) == (
        200, {"ok": True})

    assert post("/vocab/assign", {}) == (400, {"error": "profile_id is required"})
    assert post("/vocab/assign", {"profile_id": profile_id})[0] == 400
    assert post("/vocab/assign", {"profile_id": "nope", "agent_id": "a1"}) == (
        404, {"error": "no such profile"})
    # An unknown agent fails the foreign key and reads as an unknown profile.
    assert post("/vocab/assign", {"profile_id": profile_id, "agent_id": "a1"}) == (
        404, {"error": "no such profile"})
    from lib import agents
    agent_id = agents.get_by_session("claude")["agent_id"]
    assert post("/vocab/assign", {"profile_id": profile_id, "agent_id": agent_id}) == (
        200, {"ok": True})
    status, assignments = _call(base, "GET", "/vocab/assignments")
    assert status == 200 and len(assignments["assignments"]) == 1
    assert post("/vocab/unassign", {}) == (400, {"error": "agent_id or team_id is required"})
    assert post("/vocab/unassign", {"agent_id": agent_id}) == (200, {"ok": True})
    assert _call(base, "GET", "/vocab/assignments") == (200, {"assignments": []})

    assert post("/vocab/profiles/delete", {}) == (400, {"error": "profile_id is required"})
    assert post("/vocab/profiles/delete", {"profile_id": profile_id}) == (200, {"ok": True})

    assert _call(base, "GET", "/vocab/runs?limit=x") == (200, {"runs": []})
    assert _call(base, "GET", "/vocab/run") == (404, {"error": "no run for that trace"})
    assert _call(base, "GET", "/vocab/run?trace_id=t1") == (404, {"error": "no run for that trace"})
    assert _call(base, "GET", "/transcription-audio?trace_id=t1") == (
        404, {"error": "no retained audio for that trace"})
    status, budgets = _call(base, "GET", "/vocab/budgets")
    assert status == 200 and set(budgets) == {"budgets", "engine", "models"}


@pytest.mark.parametrize("preview, expected", [
    (None, (503, {"error": "vocabulary preview unavailable"})),
    ("boom", (500, {"error": "vocabulary preview failed"})),
    ("echo", (200, {"session": "claude", "requested_model": "m", "delegated": True})),
])
def test_vocab_preview(fake_ctx, monkeypatch, preview, expected):
    from lib.context import ServerContext
    from tests.integration.test_server_di import _running_context_server

    def boom(**_kw):
        raise RuntimeError("no")

    def echo(**kw):
        return kw
    monkeypatch.setattr(ServerContext, "vocab_preview",
                        {"boom": staticmethod(boom), "echo": staticmethod(echo)}.get(preview))
    for base, _ctx, _srv in _running_context_server(fake_ctx):
        assert _call(base, "GET", "/vocab/preview?session=claude&model=m&delegated=1") == expected


def test_update_routes_reject_bad_json_and_bad_numbers(running_server):
    base, _ctx, _srv = running_server
    post = lambda path, body: _call(base, "POST", path, body)  # noqa: E731
    for path in ("/vocab/packs/update", "/vocab/terms/update"):
        assert _call(base, "POST", path, raw=b"[1]") == (400, {"error": "bad json"})
    pack_id = post("/vocab/packs", {"name": "Numbers", "terms": ["Argo"]})[1]["pack_id"]
    status, body = post("/vocab/packs/update", {"pack_id": pack_id, "priority": "high"})
    assert status == 400 and set(body) == {"error"}
    term_id = _call(base, "GET", f"/vocab/terms?pack_id={pack_id}")[1]["terms"][0]["term_id"]
    status, body = post("/vocab/terms/update", {"term_id": term_id, "rarity": "rare"})
    assert status == 400 and set(body) == {"error"}


@pytest.mark.parametrize("kept, audio_url", [(True, "/transcription-audio?trace_id=t1"), (False, None)])
def test_vocab_run_for_a_recorded_trace(running_server, monkeypatch, kept, audio_url):
    from lib import heard_audio, vocab_store
    base, _ctx, _srv = running_server
    monkeypatch.setattr(vocab_store, "run_for_trace",
                        lambda trace_id: {"run_id": 4, "trace_id": trace_id} if trace_id == "t1" else None)
    monkeypatch.setattr(heard_audio, "lookup",
                        lambda cache_dir, trace_id: (cache_dir / "a.wav", {}) if kept else None)
    assert _call(base, "GET", "/vocab/run?trace_id=t1") == (
        200, {"run_id": 4, "trace_id": "t1", "audio_url": audio_url})
