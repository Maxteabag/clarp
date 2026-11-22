"""A shell call with no word, or whose command is only under input.cmd after a `cd`, never
fails the request it came in (R39, R40).

R39: a command with no word to run (a lone comment, `;`, `bash -c ''`) raised IndexError while
it was split, and the whole request, up to eight rows, failed. R40: a Bash call whose command is
only in `input.cmd`, explained as one exact part after a `cd`, raised KeyError('command') when its
template was looked up. Every input is synthetic; the model is the `translate` hook."""
import pytest

from lib import tool_explanation_shapes as shapes
from lib.tool_explanations import ToolExplanations, normalize_activity
from tests.unit.test_tool_explanation_learning import (  # noqa: F401  (configured_explainer is autouse)
    bash, configured_explainer, item, settle)


def _translate(level, items):
    return {i["id"]: {"text": "Lists the files.", "template": "Lists the files."} for i in items}


@pytest.mark.parametrize("command", ["# only a note", ";", "bash -c ''", " ; ; ", "sh -c '# nothing'"])
def test_a_command_with_no_word_is_split_and_its_request_answered(command):
    parts = shapes.split(normalize_activity(bash(command)))
    assert len(parts) == 1
    with ToolExplanations(translate=_translate, debounce=.001) as service:
        answers = settle(service, [item(bash(command), "1"), item(bash("ls -la /var/tmp"), "2")])
    assert [answer["id"] for answer in answers] == ["1", "2"]
    assert answers[1]["status"] == "ready"


def test_a_command_only_under_input_cmd_after_a_cd_is_answered():
    activity = {"name": "Bash", "input": {"cmd": "cd /var/tmp/work && ls -la"}}
    [part] = shapes.split(normalize_activity(activity))
    assert part.cwd == "/var/tmp/work"
    part.activity = normalize_activity(activity)        # what request() gives an exact single part
    same = shapes.split(normalize_activity(bash("cd /var/tmp/work && ls -la")))[0]
    same.activity = normalize_activity(bash("cd /var/tmp/work && ls -la"))
    # As the same call with its command under `command` reads (no KeyError).
    assert part.template_activity()["command"] == same.template_activity()["command"]
    with ToolExplanations(translate=_translate, debounce=.001) as service:
        answers = settle(service, [item(activity, "1"), item(bash("ls -la /var/tmp"), "2")])
    assert [answer["id"] for answer in answers] == ["1", "2"]
    assert all(answer["status"] == "ready" for answer in answers)
