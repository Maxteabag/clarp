"""Table-driven decisions for AgentSpec.parse (no IO: every lookup is a lambda)."""
import base64
from dataclasses import replace
from types import SimpleNamespace

import pytest

from lib.policies.agent_spec import AVATAR_MAX_BYTES, AgentSpec, RosterView, SpecError


class FakeBackends:
    def __init__(self):
        self.normalize_calls = []

    def normalize(self, name):
        self.normalize_calls.append(name)
        return (name or "claude").strip().lower()

    def get(self, name):
        return SimpleNamespace(resumable=name != "grok")

    def is_valid_model(self, backend, model):
        return model in {"", f"{backend}-big", f"{backend}-small"}

    def valid_efforts(self, backend):
        return {"low", "high"} if backend != "grok" else set()

    def by_id(self, backend):
        return SimpleNamespace(supports_mcp=backend != "grok",
                               effort_compatibility_unknown=backend == "agy")

    def label(self, backend):
        return backend.title()

    def capabilities(self, backend):
        return SimpleNamespace(supports_fork=backend == "claude")


class Personas(dict):
    pass


PERSONAS = Personas({"Rachel": {"voice_id": "el-rachel", "personality": "calm",
                                "avatar_symbol": "star", "avatar_path": "/a.png"}})
VIEW = RosterView(default_roster_voice="el-default", random_suffix=lambda: "beef",
                  existing_cwd=lambda raw: str(raw or "/home"),
                  launch_default=lambda backend: f"{backend}-big")


def parse(data, view=VIEW, *, janitor=False, backends=None):
    return AgentSpec.parse(data, backends=backends or FakeBackends(), roster=view,
                           personas=PERSONAS, janitor=janitor)


@pytest.mark.parametrize("data,view,status,message", [
    ({}, VIEW, 400, "name required"),
    ({"name": "X", "auto_contact": True, "anonymous": True}, VIEW, 400,
     "conflicting launch options"),
    ({"auto_contact": True}, VIEW, 409, "contact_pool_empty"),
    ({"name": "X", "anonymous": True, "replace_sid": "a"},
     replace(VIEW, agents={"a": {"name": "A"}}), 400, "anonymous launch must be fresh"),
    ({"name": "X", "avatar_base64": "!!"}, VIEW, 400, "invalid avatar"),
    ({"name": "X", "replace_sid": "gone"}, VIEW, 404, "no such agent to replace"),
    ({"name": "X", "replace_sid": "j"},
     replace(VIEW, agents={"j": {"name": "J"}},
             existing_agent=lambda s: {"is_janitor": 1}), 409, "janitor_managed"),
    ({"name": "rachel"}, replace(VIEW, agents={"r1": {"name": "Rachel"}}), 409,
     "contact_occupied"),
    ({"name": "X", "session": "s1"}, replace(VIEW, agents={"s1": {"name": "Y"}}), 409,
     "session_taken"),
    ({"name": "X", "model": 3}, VIEW, 400, "model must be a string or null"),
    ({"name": "X", "model": "gpt"}, VIEW, 400, "invalid model for backend"),
    ({"name": "X", "effort": 3}, VIEW, 400, "effort must be a string or null"),
    ({"name": "X", "effort": "max"}, VIEW, 400, "invalid effort for backend"),
    ({"name": "X", "mcp_servers": "a"}, VIEW, 400, "mcp_servers must be a list of names"),
    ({"name": "X", "backend": "grok", "mcp_servers": ["a"]}, VIEW, 400,
     "mcp servers unsupported for backend"),
    ({"name": "X", "mcp_servers": ["a"]}, VIEW, 400, "unknown mcp server"),
    ({"name": "X", "backend": "agy", "effort": "high"}, VIEW, 400,
     "AGY model-specific effort compatibility is unknown"),
    ({"name": "X", "voice_id": '{"cartesia": "c1"}'},
     replace(VIEW, agents={"o": {"name": "Other", "voice_id": '{"cartesia": "c1"}'}}),
     409, "voice_in_use"),
    ({"name": "X", "backend": "codex", "fork_session_id": "f"}, VIEW, 400, "fork_unsupported"),
    ({"name": "X", "backend": "grok", "open_existing": True, "resume_session_id": "r"},
     VIEW, 400, "resume_unsupported"),
    ({"name": "X", "open_existing": True, "resume_session_id": "r"}, VIEW, 404,
     "Session is no longer available in this directory"),
])
def test_refusals(data, view, status, message):
    result = parse(data, view)
    assert isinstance(result, SpecError)
    assert (result.status, result.message) == (status, message)


def test_relaunch_keeping_its_own_voice_is_not_a_clash():
    """An archived anonymous agent's fallback voice can equal an existing
    agent's. Relaunching that agent unchanged must not be refused, or its
    conversation cannot be restored through the lifecycle API."""
    view = replace(
        VIEW,
        agents={"adam": {"name": "Adam", "voice_id": '{"cartesia": "c1"}'},
                "anon": {"name": "Claude-ea5a", "voice_id": "{}"}},
        existing_agent=lambda s: {"backend": "claude"},
        cartesia_voice_for=lambda name: "c1" if name == "Claude-ea5a" else None)
    spec = parse({"name": "Adam", "replace_sid": "adam", "resume_session_id": "r"}, view)
    assert isinstance(spec, AgentSpec) and spec.voice_id == '{"cartesia": "c1"}'
    # Taking that voice for another agent is still a clash.
    clash = parse({"name": "Bea", "voice_id": '{"cartesia": "c1"}'}, view)
    assert isinstance(clash, SpecError) and clash.message == "voice_in_use"


def test_roster_contact_sharing_its_shipped_voice_is_not_a_clash():
    """Shipped roster contacts deliberately share Cartesia voices (Adam and
    Claude map to the same id in DEFAULT_CARTESIA_VOICES). Starting Adam
    while Claude is live must succeed with Adam's own shipped voice; only a
    custom pick of another agent's voice is a clash."""
    personas = Personas({
        "Adam": {"voice_id": '{"elevenlabs": "el-adam", "cartesia": "c1"}'},
        "Claude": {"voice_id": '{"cartesia": "c1"}'}})
    view = replace(
        VIEW,
        agents={"claude": {"name": "Claude", "voice_id": "c1-plain"}},
        cartesia_voice_for=lambda name: "c1" if name in ("Adam", "Claude") else None)

    def parse_as(data):
        return AgentSpec.parse(data, backends=FakeBackends(), roster=view,
                               personas=personas, janitor=False)

    # Voice defaulted from the persona definition.
    assert isinstance(parse_as({"name": "Adam"}), AgentSpec)
    # Same voice sent explicitly (client prefill) is still Adam's own voice.
    assert isinstance(
        parse_as({"name": "Adam", "voice_id": '{"cartesia": "c1"}'}), AgentSpec)
    # A custom-named agent taking that voice is still refused.
    clash = parse_as({"name": "Bea", "voice_id": '{"cartesia": "c1"}'})
    assert isinstance(clash, SpecError) and clash.message == "voice_in_use"


def test_backend_is_normalised_once():
    fake = FakeBackends()
    assert isinstance(parse({"name": "X", "backend": "Codex"}, backends=fake), AgentSpec)
    assert fake.normalize_calls == ["Codex"]


def test_fresh_create_defaults():
    spec = parse({"name": "Rachel"})
    assert (spec.persona, spec.backend, spec.cwd, spec.session) == (
        "Rachel", "claude", "/home", "")
    assert spec.voice_id == "el-rachel"             # persona definition voice
    assert spec.model == "claude-big" and spec.llm_update == {"model": "claude-big"}
    assert spec.presentation_update == {"avatar_symbol": "star", "personality": "calm",
                                        "avatar_path": "/a.png"}
    assert spec.synthesize_audio is True and spec.reopen is None


def test_voice_fallback_reaches_the_host_default():
    assert parse({"name": "Nobody"}).voice_id == "el-default"


def test_explicit_session_is_honoured_only_when_unused():
    assert parse({"name": "X", "session": "fresh!"}).session == "fresh"
    taken = replace(VIEW, session_exists=lambda s: True)
    assert parse({"name": "X", "session": "old"}, taken).session == ""
    refused = parse({"name": "X", "session": "old"}, taken, janitor=True)
    assert (refused.status, refused.message) == (409, "session_taken")


def test_anonymous_launch_mints_a_free_label():
    spec = parse({"name": "ignored", "anonymous": True, "session": "s", "backend": "codex"})
    assert spec.persona == "Codex-beef" and spec.voice_id == "{}"
    assert spec.session == "" and spec.synthesize_audio is False


def test_auto_contact_takes_the_first_free_contact_or_mints_when_exhausted():
    view = replace(VIEW, contact_pool=lambda backend: ["Nora", "Ivy"],
                   mint_contact=lambda backend: "Clara")
    assert parse({"auto_contact": True}, view).persona == "Nora"

    exhausted = replace(VIEW, contact_pool=lambda backend: [],
                        mint_contact=lambda backend: f"Minted-{backend}")
    assert parse({"auto_contact": True, "backend": "grok"}, exhausted).persona == "Minted-grok"
    assert parse({"mint_contact": True, "backend": "claude"}, view).persona == "Clara"
    conflict = parse({"auto_contact": True, "mint_contact": True}, view)
    assert isinstance(conflict, SpecError) and conflict.message == "conflicting launch options"


def test_open_existing_returns_the_owner():
    owner = {"session": "s", "persona": "P", "voice_id": "v", "backend": "claude"}
    view = replace(VIEW, resume_owner=lambda sid: owner)
    spec = parse({"open_existing": True, "resume_session_id": "r"}, view)
    assert spec.reopen == owner and spec.backend == "claude"


def test_relaunch_inherits_and_clears_incompatible_pins():
    view = replace(
        VIEW, agents={"a": {"name": "A", "voice_id": "v", "backend": "claude", "cwd": "/repo"}},
        existing_agent=lambda s: {"backend": "claude", "model": "claude-big", "effort": "high"})
    same = parse({"name": "A", "replace_sid": "a"}, view)
    assert (same.persona, same.voice_id, same.cwd, same.session) == ("A", "v", "/repo", "a")
    assert (same.model, same.effort, same.llm_update) == ("claude-big", "high", {})
    assert same.presentation_update == {}
    moved = parse({"name": "A", "replace_sid": "a", "backend": "grok"}, view)
    assert moved.llm_update == {"model": "grok-big", "effort": ""}
    assert moved.effort == ""


def test_janitor_skips_voice_and_model_defaults():
    spec = parse({"name": "Sweeper"}, janitor=True)
    assert spec.voice_id == "" and spec.model == "" and spec.synthesize_audio is False


def test_avatar_size_limit_and_mcp_dedup():
    big = base64.b64encode(b"x" * (AVATAR_MAX_BYTES + 1)).decode()
    assert parse({"name": "X", "avatar_base64": big}).detail == "avatar is too large"
    view = replace(VIEW, global_mcp_servers={"a", "b"})
    assert parse({"name": "X", "mcp_servers": [" a", "a", "b", ""]}, view).mcp_servers == (
        "a", "b")


def test_tier_mismatch_is_a_recommendation_not_a_refusal():
    spec = parse({"name": "Rachel", "backend": "grok"})
    assert isinstance(spec, AgentSpec)


def test_avatar_and_explicit_pins_survive_create_without_mutating_request():
    data = {"name": "Rachel", "avatar_base64": base64.b64encode(b"avatar").decode(),
            "model": " claude-small ", "effort": " HIGH ",
            "avatar_symbol": " badge ", "personality": " focused ",
            "creation_request_id": "request-1"}
    original = dict(data)
    spec = parse(data)
    assert isinstance(spec, AgentSpec)
    assert spec.avatar_raw == b"avatar"
    assert (spec.model, spec.effort) == ("claude-small", "high")
    assert spec.llm_update == {"model": "claude-small", "effort": "high"}
    assert spec.presentation_update == {"avatar_symbol": "badge", "personality": "focused",
                                        "avatar_path": "/a.png"}
    assert spec.creation_request_id == "request-1"
    assert data == original


def test_listed_resume_uses_recorded_model_and_keeps_requested_directory():
    view = replace(VIEW, resume_listed=lambda backend, cwd, sid: (
        backend, cwd, sid) == ("claude", "/repo", "saved"),
        recorded_model=lambda backend, sid: "claude-small")
    spec = parse({"name": "X", "open_existing": True, "resume_session_id": "saved",
                  "cwd": "/repo"}, view)
    assert isinstance(spec, AgentSpec)
    assert (spec.cwd, spec.resume_session_id, spec.model, spec.reopen) == (
        "/repo", "saved", "claude-small", None)
    assert spec.llm_update == {"model": "claude-small"}


def test_relaunch_presentation_updates_only_requested_fields():
    view = replace(VIEW, agents={"r": {"name": "Rachel", "backend": "claude"}},
                   existing_agent=lambda sid: {"backend": "claude"})
    spec = parse({"name": "Rachel", "replace_sid": "r", "personality": " new "}, view)
    assert isinstance(spec, AgentSpec)
    assert spec.presentation_update == {"personality": "new"}


def test_lineage_refusal_propagates_without_creating_spec():
    result = parse({"name": "X", "role": "helper"})
    assert isinstance(result, SpecError)
    assert (result.status, result.message, result.detail) == (
        400, "helper_requires_parent", "A helper needs a parent session.")
