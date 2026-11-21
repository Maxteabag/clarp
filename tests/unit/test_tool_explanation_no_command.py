"""A shell tool's call explained without its command is never shaped or learned (R184).

Two over-broad learned rows came from such calls. One was a Bash call that carried only its
description: it was shaped as the tool's name plus one free-text slot, so every such call
shared it. The other was a live Bash item asked before its input arrived, whose command was
the tool's own name. Either way, one sentence written for one agent's call was then shown
for unrelated calls in other agents' chats. Every input here is synthetic; the model is the
`translate` hook, and nothing runs a command.
"""
import pytest

from lib import tool_explanation_learning as learning
from lib import tool_explanation_shapes as shapes
from lib import tool_explanation_templates as templates
from lib import tool_explanations as module
from lib.db import conn
from lib.tool_explanations import ToolExplanations, normalize_activity
from tests.unit.test_live_explanations import FakeService, _explainer, _tool_event
from tests.unit.test_tool_explanation_learning import (  # noqa: F401  (configured_explainer is autouse)
    bash, configured_explainer, decisions, item, settle, templated)

WITHOUT_COMMAND = [
    {"name": "Bash", "description": "Inspect how clips are announced and how the stand-in records requests"},
    {"name": "Bash", "description": "Read the job log"},
    {"name": "Bash", "summary": "Send a prompt to another agent"},
    {"name": "Bash", "command": "Bash"},
    {"name": "Bash", "command": "", "title": "Bash", "input": {"description": "Read the job log"}},
    {"kind": "command", "summary": "Bash"},
    {"name": "exec_command", "input": {}},
    {"name": "shell", "command": "shell"},
]


def _never(level, items):
    pytest.fail("the model must not be asked for a call without its command")


@pytest.mark.parametrize("activity", WITHOUT_COMMAND)
def test_a_shell_call_without_its_command_is_unexplained_with_no_model_and_no_learned_row(activity):
    assert shapes.lacks_command(normalize_activity(activity))
    assert shapes.split(normalize_activity(activity)) == []
    with ToolExplanations(translate=_never, debounce=.001) as service:
        [answer] = service.request(2, [item(activity)])["items"]
    assert answer == {"id": "1", "status": "failed", "reason": "unexplained"}
    assert conn().execute("SELECT count(*) FROM tool_explanation_learned").fetchone()[0] == 0
    assert decisions() == [("failed", "unexplained")]


def test_a_shell_call_with_its_command_is_shaped_as_before():
    for activity in (bash("tail -n 50 /var/tmp/job.log"), {"kind": "command", "summary": "git status"},
                     {"name": "bash -c 'ls'"}):
        assert not shapes.lacks_command(normalize_activity(activity))
        assert shapes.split(normalize_activity(activity))


def _plant(signature, text):
    learning.store(conn(), [{"signature": signature, "level": 2, "template_text": text, "producer": "llm",
                             "prompt_version": module.PROMPT_VERSION, "templates_version": templates.VERSION}], 0)


def test_the_two_known_over_broad_signatures_are_unreachable():
    """The signatures the code before R184 gave these calls, on this Host's key (the live rows
    `tool:Bash #04671e48…` and `sh:Bash #34c764f1…` are these two on their Host): rows planted
    under them are never served again, and no call can produce them."""
    described = shapes._tool_part(normalize_activity(WITHOUT_COMMAND[0]))
    named = shapes._shell_parts("Bash")[0]
    assert described.signature.startswith("tool:Bash #") and named.signature.startswith("sh:Bash #")
    assert shapes._tool_part(normalize_activity(WITHOUT_COMMAND[1])).signature == described.signature  # the breadth
    sentence = "Inspect how clips are announced, when the engine is queried, and how the stand-in records requests."
    _plant(described.signature, sentence)
    _plant(named.signature, sentence)
    with ToolExplanations(translate=_never, debounce=.001) as service:
        answers = service.request(2, [item(a, str(n)) for n, a in enumerate(WITHOUT_COMMAND)])["items"]
    assert {a["status"] for a in answers} == {"failed"} and all(sentence != a.get("text") for a in answers)
    hits = conn().execute("SELECT sum(hits) FROM tool_explanation_learned").fetchone()[0]
    assert hits == 0
    for activity in WITHOUT_COMMAND + [bash("ls"), bash("Bash"), {"kind": "command", "summary": "Bash"}]:
        assert all(part.signature not in {described.signature, named.signature}
                   for part in shapes.split(normalize_activity(activity)))


def test_unrelated_bash_commands_never_share_an_explanation():
    translate, calls = templated("Shows the end of {path1_name}.")
    with ToolExplanations(translate=translate, debounce=.001) as service:
        first = settle(service, [item(bash("jobctl tail -n 50 /var/tmp/job.log"))], level=2)[0]
        other = service.request(2, [item(bash("agentmsg send --to someone --text hello"), "2")])["items"][0]
    assert first["text"] == "Shows the end of job.log." and first["source"] == "llm"
    assert other.get("text") != first["text"] and other["status"] in {"pending", "busy"}
    assert shapes.split(normalize_activity(bash("jobctl tail -n 50 /var/tmp/job.log")))[0].signature != \
        shapes.split(normalize_activity(bash("agentmsg send --to someone --text hello")))[0].signature


# ---- what a reusable template may carry --------------------------------------------------------

@pytest.mark.parametrize("fixed", [
    "Reads /home/someone/notes.txt.", "Opens ~/private.", "Runs the step for nadia-1374-a.",
    "Checks config.toml.", "Waits 30 seconds.", "Calls the my_helper routine."])
def test_a_template_whose_fixed_words_carry_a_value_is_kept_exact_only(fixed):
    template = fixed[:-1] + " for {text1}."
    text = shapes.render(template, {"text1": "x"})
    assert shapes.parameterise(text, template, {"text1": "x"}) == (None, "specific_literal")


def test_a_template_of_plain_words_stays_reusable():
    assert shapes.parameterise("Shows the latest commit.", "Shows the latest commit.", {"sha1": "abc1234"}) == \
        ("Shows the latest commit.", "")
    assert shapes.parameterise("Runs a well-known check on x.", "Runs a well-known check on {text1}.",
                               {"text1": "x"}) == ("Runs a well-known check on {text1}.", "")


def test_a_stored_sentence_naming_a_value_this_call_lacks_is_not_served():
    """A row stored before R184 whose fixed words name its source call's value is refused at the
    learned tier (`learning.foreign`), as Jev's candidates already were."""
    [part] = shapes.split(normalize_activity(bash("jobctl tail -n 50 /var/tmp/job.log")))
    _plant(part.signature, "Shows the end of {path1_name} in the nightly-build-7 run.")
    translate, _ = templated("Shows the end of {path1_name}.")
    with ToolExplanations(translate=translate, debounce=.001) as service:
        answer = service.request(2, [item(bash("jobctl tail -n 50 /var/tmp/other.log"))])["items"][0]
    assert answer.get("text") is None and answer["status"] in {"pending", "busy"}
    # Without the foreign value the same row is served, rendered for this call.
    conn().execute("DELETE FROM tool_explanation_learned")
    _plant(part.signature, "Shows the end of {path1_name}.")
    with ToolExplanations(translate=translate, debounce=.001) as service:
        served = service.request(2, [item(bash("jobctl tail -n 50 /var/tmp/other.log"))])["items"][0]
    assert served["status"] == "ready" and served["text"] == "Shows the end of other.log."


# ---- the Host's own caller ----------------------------------------------------------------------

def test_a_live_bash_item_is_not_asked_before_its_command_arrives():
    """A live item made when a Bash call starts names the tool as its command; the explainer is
    asked only once the real command is there."""
    service, patches = FakeService(["ready"]), []
    explainer = _explainer(service, patches)
    explainer.observe(_tool_event("Bash"))
    assert service.calls == [] and patches == []
    explainer.observe(_tool_event("tail -n 50 /var/tmp/job.log"))
    assert service.calls and service.calls[0][1][0]["activity"]["command"] == "tail -n 50 /var/tmp/job.log"

