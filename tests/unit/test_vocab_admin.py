"""Vocabulary administration: profiles, packs, terms, and the live preview."""
from __future__ import annotations

from types import SimpleNamespace

from lib import vocab_store
from lib.context import ServerContext


def _agent_id(session: str) -> str:
    from lib import agents as agents_db
    return agents_db.get_by_session(session)["agent_id"]


def test_profile_detail_lists_packs_and_assignments_in_order(seed_agents):
    seed_agents({"rachel": {"name": "Rachel"}})
    agent = _agent_id("rachel")
    people = vocab_store.create_pack("people", priority=2.0, floor=2)
    vocab_store.add_term(people, "Knut Thomas", often_heard_as="Newt Thomas")
    project = vocab_store.create_pack("project")
    profile = vocab_store.create_profile("ECIT default")
    vocab_store.add_pack_to_profile(profile, project, 1)
    vocab_store.add_pack_to_profile(profile, people, 0)
    vocab_store.assign_profile(profile, agent_id=agent)

    detail = vocab_store.profile_detail(profile)
    assert [p["name"] for p in detail["packs"]] == ["people", "project"]
    assert detail["assignments"] == [{"agent_id": agent, "team_id": ""}]
    assert vocab_store.list_profiles() == [
        {"profile_id": profile, "name": "ECIT default", "packs": 2, "assignments": 1}]
    assert vocab_store.pack_term_counts() == {people: 1}
    terms = vocab_store.list_terms(people)
    assert terms[0]["text"] == "Knut Thomas" and terms[0]["often_heard_as"] == "Newt Thomas"

    assert vocab_store.assignments()[0]["profile_name"] == "ECIT default"
    vocab_store.remove_pack_from_profile(profile, project)
    vocab_store.unassign(agent_id=agent)
    detail = vocab_store.profile_detail(profile)
    assert [p["name"] for p in detail["packs"]] == ["people"]
    assert detail["assignments"] == []

    vocab_store.delete_profile(profile)
    assert vocab_store.profile_detail(profile) is None
    assert vocab_store.assignments() == []


def test_updates_edit_in_place_and_clamp_rarity():
    pack = vocab_store.create_pack("glossary")
    vocab_store.add_term(pack, "Fabric")
    term_id = vocab_store.list_terms(pack)[0]["term_id"]
    assert vocab_store.update_term(term_id, say_as="fab-rick", rarity=7)
    row = vocab_store.list_terms(pack)[0]
    assert (row["say_as"], row["rarity"]) == ("fab-rick", 1.0)
    assert vocab_store.update_pack(pack, name="Glossary", priority=1.7, floor=4)
    packs = {p["pack_id"]: p for p in vocab_store.list_packs()}
    assert (packs[pack]["name"], packs[pack]["priority"], packs[pack]["floor"]) == (
        "Glossary", 1.7, 4)
    assert not vocab_store.update_pack(pack)
    assert not vocab_store.update_term(term_id)


def _ctx(agent_id="agent-1", cwd=None):
    from lib import agents as agents_db
    stub = SimpleNamespace(
        stt=SimpleNamespace(provider="faster-whisper", model_name="small.en"),
        active_agent_names=lambda: ["Rachel", "Mike"],
    )
    stub.vocab_preview = ServerContext.__dict__["vocab_preview"].__get__(stub)
    stub._transcription_provider_model = (
        ServerContext.__dict__["_transcription_provider_model"].__get__(stub))
    stub._vocab_static_packs = ServerContext.__dict__["_vocab_static_packs"].__get__(stub)
    return stub


def test_preview_reflects_the_assigned_profile_without_recording_a_run(seed_agents):
    seed_agents({"rachel": {"name": "Rachel"}})
    people = vocab_store.create_pack("people", priority=2.0, floor=2)
    vocab_store.add_term(people, "Knut Thomas", rarity=0.9)
    vocab_store.add_term(people, "Oscar Lindqvist", rarity=0.9)
    profile = vocab_store.create_profile("ECIT")
    vocab_store.add_pack_to_profile(profile, people)
    vocab_store.assign_profile(profile, agent_id=_agent_id("rachel"))

    preview = _ctx().vocab_preview(session="rachel")
    assert preview["provider"] == "faster-whisper"
    assert preview["profile_id"] == profile
    included = {t["text"] for t in preview["included"]}
    assert {"Knut Thomas", "Oscar Lindqvist"} <= included
    assert any(p["pack"] == "people" and p["included"] == 2 for p in preview["packs"])
    assert preview["used"] <= preview["capacity"] == 223
    assert "Knut Thomas" in preview["payload"]
    assert vocab_store.recent_runs() == []


def test_preview_for_an_unknown_session_still_answers():
    preview = _ctx().vocab_preview(session="nobody", requested_model="deepgram:nova-3")
    assert preview["provider"] == "deepgram"
    assert preview["capacity"] == 50
    assert preview["profile_id"] is None
