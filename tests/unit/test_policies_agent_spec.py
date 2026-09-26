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

    def adapter_for(self, backend):
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


def test_auto_contact_takes_the_first_free_contact():
    view = replace(VIEW, contact_pool=lambda backend: ["Nora", "Ivy"])
    assert parse({"auto_contact": True}, view).persona == "Nora"


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
