"""What the router says when it refuses, and the clips it may play for it (issue 360, R75, R76)."""
from __future__ import annotations

import ast
import pathlib

import pytest

from lib import agent_lifecycle, agents, db, events, orchestrator
from tests.unit.test_orchestrator_held_and_unknown import effects, held, no_processes, router  # noqa: F401

# Written out, not imported: these files collect and fail by assertion on main too.
NAME_TAKEN = "I already have an agent by that name."
COMMAND_REFUSED = "I could not do that."
MAIN_KEYS = {  # the five keys the phrase cache knew before (issue 360, R76): unchanged
    "clarify_target": "Was that for me?",
    "ambiguous_target": "I am not sure who that was for.",
    "repeat_missing": "I do not have anything recent to replay.",
    "control_unknown": "I am not sure what control action you meant.",
    "agent_control_unknown": "I am not sure how to do that yet.",
}


def _command(action, target="", name=None, confidence=0.95):
    decision = {"kind": "agent_control", "control_action": action, "target_session": target,
                "confidence": confidence, "reason": action}
    if name is not None:
        decision["control_payload"] = {"name": name}
    return lambda packet: decision


# ---- R75: create with a name in use; a lifecycle refusal is said ----------------------------

@pytest.mark.parametrize("name", ["Antoni", "ANTONI", "antoni", "Antóni"])
def test_a_new_agent_never_takes_a_voice_name_already_in_use(effects, name):
    say, _ = router(_command("create", name=name), effects)
    assert say(f"make an agent called {name}").action == "clarify"
    assert effects["created"] == [] and effects["spoken"] == [NAME_TAKEN]
    assert held() == []


def test_an_archived_agents_name_is_in_use_too(effects):
    say, _ = router(_command("create", name="Antoni"), effects)
    db.conn().execute("UPDATE agents SET archived_at = 1 WHERE session = 'antoni'")
    say("make an agent called Antoni")
    assert effects["created"] == [] and effects["spoken"] == [NAME_TAKEN]


def test_a_new_name_is_still_created(effects):
    say, _ = router(_command("create", name="Nova"), effects)
    assert say("make an agent called Nova").action == "control"
    assert effects["created"] == [{"name": "Nova", "synthesize_audio": True}]


@pytest.mark.parametrize("action,target", [("create", ""), ("restart", "antoni")])
def test_a_lifecycle_refusal_is_said_never_a_silent_error(effects, monkeypatch, action, target):
    class Refusing:
        def __init__(self, _ctx):
            pass

        def create(self, payload):
            raise agent_lifecycle.AgentLifecycleError(409, "contact_occupied")
    monkeypatch.setattr(agent_lifecycle, "AgentLifecycleService", Refusing)
    say, _ = router(_command(action, target=target, name="Nova" if action == "create" else None), effects)
    outcome = say(f"{action} it")
    assert outcome.action == "clarify" and outcome.ok
    assert effects["spoken"] == [COMMAND_REFUSED]
    assert held() == []


# ---- the surviving mutant: a status question with two matching agents asks which ------------

@pytest.mark.parametrize("spoken", ["Sam", "SAM", "sam"])
def test_a_status_question_naming_two_agents_asks_which(effects, spoken):
    say, _ = router(lambda packet: {"kind": "status_query", "target_session": spoken,
                                    "confidence": 0.95, "status_text": "Sam is idle."}, effects)
    for session in ("sam-a", "sam-b"):
        agents.create_agent(persona="Sam", voice_id="", cwd="/tmp", session=session)
    assert say(f"what is {spoken} doing?").action == "clarify"
    assert effects["spoken"] == [orchestrator.WHICH_AGENT]


# ---- R76: one text per key; a clip plays only of the sentence said ------------------------------

def test_the_five_keys_the_cache_knew_are_unchanged():
    from lib import orchestrator_phrases
    assert {key: orchestrator_phrases.PHRASES[key] for key in MAIN_KEYS} == MAIN_KEYS


def test_every_sentence_the_router_speaks_itself_has_a_key_and_one_text():
    from lib import orchestrator_phrases
    texts = orchestrator_phrases.PHRASES.values()
    assert len(set(texts)) == len(orchestrator_phrases.PHRASES)
    for name in ("NO_SUCH_AGENT", "HELD_MESSAGE_GONE", "CANNOT_CHECK", "CANNOT_COMMAND", "WHICH_AGENT",
                 "UNSURE_COMMAND", "NO_VOICE_COMMANDS", "FORK_IN_APP", "NAME_TAKEN", "COMMAND_REFUSED"):
        assert getattr(orchestrator, name) in orchestrator_phrases.KEY_OF, name
    # No fixed sentence is spoken as a literal that the phrase table does not hold.
    source = ast.parse(pathlib.Path(orchestrator.__file__).read_text())
    spoken = [node.args[0].value for node in ast.walk(source)
              if isinstance(node, ast.Call) and getattr(node.func, "attr", "") in {"_speak_as_session",
                                                                                 "_speak_orchestrator"}
              and node.args and isinstance(node.args[0], ast.Constant)]
    assert spoken == [], spoken


def test_the_generator_records_the_same_texts():
    script = (pathlib.Path(orchestrator.__file__).parents[2] / "scripts/generate_orchestrator_phrases.py").read_text()
    assert "from lib.orchestrator_phrases import PHRASES" in script and "PHRASES = {" not in script


def _cache(key, text, voice=""):
    db.conn().execute(
        "INSERT INTO orchestrator_phrase_cache (phrase_key, voice_id, session, text, audio_path, provider, "
        "model, generated_at, updated_at) VALUES (?, ?, '', ?, '/audio/clip.mp3', 'test', '', 1, 1)",
        (key, voice, text))


@pytest.fixture
def played(monkeypatch):
    clips = []
    monkeypatch.setattr(events, "broadcast", lambda stream, event: clips.append(event)
                        if event.get("cached_phrase") else None)
    return clips


def test_a_clip_of_the_sentence_itself_plays(effects, played):
    say, _ = router(_command("restart", target="zed"), effects)
    _cache("no_such_agent", orchestrator.NO_SUCH_AGENT)
    say("restart Zed")
    assert [clip["phrase_key"] for clip in played] == ["no_such_agent"] and effects["spoken"] == []


def test_a_clip_of_another_text_is_never_played_for_the_sentence(effects, played, monkeypatch):
    lines = []
    monkeypatch.setattr(orchestrator, "log", lambda event, detail="", **_: lines.append((event, detail)))
    say, _ = router(_command("restart", target="zed"), effects)
    _cache("no_such_agent", "Was that for me?")
    say("restart Zed")
    assert played == [] and effects["spoken"] == [orchestrator.NO_SUCH_AGENT]
    assert ("orchestratorPhraseMismatch", "key=no_such_agent") in lines


def test_a_key_with_no_clip_is_spoken(effects, played):
    say, _ = router(_command("restart", target="zed"), effects)
    say("restart Zed")
    assert played == [] and effects["spoken"] == [orchestrator.NO_SUCH_AGENT]


def test_the_refusal_is_recorded_under_its_own_key(effects):
    say, _ = router(_command("restart", target="zed"), effects)
    outcome = say("restart Zed")
    assert outcome.decision["phrase_key"] == "no_such_agent"


# ---- R80: one rule for every path: a clip plays only of the very words meant ---------------------

def test_the_question_about_another_agent_is_said_not_played_as_the_default_clip(effects, played, monkeypatch):
    """On main "Was that for Antoni?" went out under _as_clarify's default key and the cached
    clip "Was that for me?" played instead, in the voice of the agent spoken to; a yes then sent
    the message to Antoni."""
    lines = []
    monkeypatch.setattr(orchestrator, "log", lambda event, detail="", **_: lines.append((event, detail)))
    say, _ = router(lambda packet: {"kind": "agent_message", "target_session": "antoni",
                                    "confidence": 0.5, "reason": "unsure"}, effects)
    _cache("clarify_target", "Was that for me?")
    say("send the report")
    assert played == [] and effects["spoken"] == ["Was that for Antoni?"]
    assert ("orchestratorPhraseMismatch", "key=clarify_target") in lines


def test_the_cached_sentence_itself_still_plays_its_clip(effects, played):
    say, _ = router(lambda packet: {"kind": "clarify", "confidence": 0.9, "reason": "who",
                                    "phrase_key": "clarify_target", "spoken_text": "Was that for me?"}, effects)
    _cache("clarify_target", "Was that for me?")
    say("send the report")
    assert [clip["phrase_key"] for clip in played] == ["clarify_target"] and effects["spoken"] == []


def test_a_key_the_model_names_with_other_words_is_said(effects, played):
    say, _ = router(lambda packet: {"kind": "clarify", "confidence": 0.9, "reason": "which",
                                    "phrase_key": "repeat_missing", "spoken_text": "Which report did you mean?"},
                    effects)
    _cache("repeat_missing", "I do not have anything recent to replay.")
    say("send the report")
    assert played == [] and effects["spoken"] == ["Which report did you mean?"]


def test_no_caller_can_play_a_clip_without_the_sentence_it_must_be():
    import inspect
    parameter = inspect.signature(orchestrator.OrchestratorService._play_cached_phrase).parameters["expected_text"]
    assert parameter.default is inspect.Parameter.empty
