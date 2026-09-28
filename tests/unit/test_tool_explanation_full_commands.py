"""Explaining the whole command: clipped Codex labels, heredocs and wrappers.

Everything is synthetic: the model is the `translate` hook and nothing
described is run. The real queue, learned table, ledger and schema are used.
"""
import json
import threading
import time

import pytest

from lib import agents as agents_db
from lib import codex_app_server, janitor_builtins
from lib import tool_explanation_commands as commands
from lib import tool_explanation_shapes as shapes
from lib import tool_explanation_templates as templates
from lib.backend.codex import TurnState
from lib.db import conn
from lib.tool_explanations import ToolExplanations, normalize_activity


@pytest.fixture(autouse=True)
def configured_explainer():
    janitor_builtins.ensure_builtins(cwd="/tmp")
    commands.clear()
    yield
    commands.clear()


def bash(command):
    return {"name": "Bash", "command": command, "input": {"command": command}}


def split(command):
    return shapes.split(normalize_activity(bash(command)))


def settle(service, items, level=1, **kwargs):
    for _ in range(400):
        result = service.request(level, items, include_provenance=True, **kwargs)["items"]
        if all(entry["status"] not in {"pending", "busy"} for entry in result):
            return result
        time.sleep(.01)
    pytest.fail("worker did not finish")


def recording(template):
    calls = []

    def translate(level, items):
        calls.append(items)
        return {i["id"]: {"text": shapes.render(template, i.get("slots", {})), "template": template} for i in items}
    return translate, calls


def stored():
    """Every text column of the rows the explainer keeps."""
    text = []
    for table in ("tool_explanation_learned", "tool_explanation_decisions", "tool_explanation_jobs",
                  "tool_explanation_cache", "judgment_decisions"):
        text += [json.dumps([tuple(row) for row in conn().execute(f"SELECT * FROM {table}")])]
    return "\n".join(text)


# ---- the full Codex command ------------------------------------------------------

INNER = ("git -C /home/peter/GIT/clarp worktree add -b chore/temporary-branch-name "
         "/home/peter/GIT/clarp-worktrees/temporary-branch-name origin/main")
LABEL = f"/usr/bin/bash -lc '{INNER}'"


class _Handle:
    def __init__(self):
        self._done = threading.Event()


def codex_item_started(command):
    """A Codex app-server command item for a fresh agent, as the Host receives it."""
    agent_id = agents_db.create_agent(persona="Caleb", voice_id="v", cwd="/tmp", session="caleb", backend="codex")
    agents_db.open_turn(agent_id=agent_id, source="pwa", trace_id="trace-1")
    client = object.__new__(codex_app_server._Client)
    client.agent_id = agent_id
    client.active = codex_app_server._ActiveTurn(
        turn_id="turn-1", thread_id="thread-1", agent_id=agent_id, session="caleb", trace_id="trace-1",
        state=TurnState(), handle=_Handle(), on_result=None, on_error=None, stream=None,
        enqueue=lambda **_kwargs: 0)
    client._notification("turn/started", {"turn": {"id": "turn-1"}})
    client._notification("item/started", {"item": {"type": "commandExecution", "id": "exec-1", "command": command}})
    return agent_id


def test_a_clipped_codex_label_is_explained_from_its_full_command():
    agent_id = codex_item_started(LABEL)
    shown = LABEL[:templates.LABEL_CLIP]
    # What the apps show is still the clipped label.
    detail = json.loads(conn().execute("SELECT detail FROM state_log WHERE agent_id=? ORDER BY state_id DESC",
                                       (agent_id,)).fetchone()[0])
    assert detail["tool"] == shown
    translate, calls = recording("Creates a new git worktree on a new branch.")
    with ToolExplanations(translate=translate, debounce=.001) as service:
        [result] = settle(service, [{"id": "1", "activity": {"name": shown, "summary": f"Using {shown}"}}],
                          target_agent_id=agent_id)
    assert result["status"] == "ready"
    [[request]] = calls
    assert request["activity"]["command"] == INNER
    [part] = shapes.split(bash(LABEL))
    decision = conn().execute("SELECT signature, reason FROM tool_explanation_decisions").fetchone()
    assert decision["signature"] == part.signature and not part.exact
    assert "truncated" not in decision["reason"]


def test_a_label_this_host_cannot_complete_stays_truncated():
    shown = LABEL[:templates.LABEL_CLIP]
    activity = {"name": shown, "summary": f"Using {shown}"}
    assert commands.expand(activity, "agent-1") is activity
    # Two recent commands sharing the clipped text: which one was shown is unknown.
    commands.remember("agent-1", LABEL)
    commands.remember("agent-1", LABEL + " --force")
    assert commands.expand(activity, "agent-1") is activity
    assert commands.expand(activity, "agent-2") is activity
    [part] = shapes.split(normalize_activity(activity))
    assert part.exact and part.reason == "truncated"


def test_commands_differing_only_in_a_heredoc_body_complete_a_shared_label():
    first = "/usr/bin/bash -lc \"python3 - <<'PY'\nfrom pathlib import Path\np=Path('App/AppWindowRoot.swift')\nprint(1)\nPY\""
    second = "/usr/bin/bash -lc \"python3 - <<'PY'\nfrom pathlib import Path\np=Path('App/AppWindowRoot.swift')\np.write_text('')\nPY\""
    assert first[:80] == second[:80]
    commands.remember("agent-1", first)
    commands.remember("agent-1", second)
    activity = commands.expand({"name": first[:80]}, "agent-1")
    assert activity == {"name": "Bash", "command": "python3 - <<'EOF'\n…\nEOF"}


def test_only_a_label_is_completed():
    commands.remember("agent-1", LABEL)
    for activity in ({"name": "Bash", "command": LABEL[:80]}, {"name": LABEL[:40]}, {"kind": "command", "summary": LABEL[:80]}):
        assert commands.expand(activity, "agent-1") is activity


# ---- heredocs --------------------------------------------------------------------

@pytest.mark.parametrize("first, second", [
    ("python3 - <<'EOF'\nimport os\nprint(\"don't\")\nEOF", "python3 - <<'PY'\nprint(sum(range(10)))\nPY"),
    ("node - <<EOF\nconsole.log(1)\nEOF", "node - <<'JS'\nprocess.exit(2)\nJS"),
    ("bash <<EOF\nls\nEOF", "bash <<'SH'\necho hi\nSH"),
])
def test_an_inline_script_is_shaped_as_its_interpreter_whatever_the_body(first, second):
    [a], [b] = split(first), split(second)
    assert not a.exact and a.signature == b.signature and a.program == b.program == first.split()[0]
    assert a.slots == {} and a.activity["command"] == b.activity["command"]
    assert "…" in a.activity["command"] and "import" not in a.activity["command"]
    assert templates.classify(a.template_activity())["reason"] == "inline_script"


def test_a_heredoc_after_other_commands_is_its_own_part():
    parts = split("mkdir -p /home/p/lab\npython - <<'PY'\nimport sqlite3\nPY\necho done")
    assert [p.program for p in parts] == ["mkdir", "python", "echo"]
    assert parts[1].activity["command"] == "python - <<'EOF'\n…\nEOF"
    # The same call under a Codex wrapper is the same parts.
    wrapped = split("/usr/bin/bash -lc \"mkdir -p /home/p/other\npython - <<'PY'\nprint(1)\nPY\necho done\"")
    assert [p.signature for p in wrapped] == [p.signature for p in parts]


@pytest.mark.parametrize("command, path", [
    ("cat > /home/p/notes/a.md <<'EOF'\n# Title\nsome text\nEOF", "/home/p/notes/a.md"),
    ("cat <<EOF > out.txt\nhello $USER\nEOF", "out.txt"),
    ("tee docs/b.md <<'EOF' >/dev/null\nx\nEOF", "docs/b.md"),
])
def test_writing_a_heredoc_to_a_file_is_a_file_write(command, path):
    [part] = split(command)
    assert not part.exact and part.slots == {"path1": path}
    route = templates.classify(part.template_activity())
    assert route["template_id"] == "write_file" and route["parameters"] == {"file": path}
    text, _ = templates.lookup(part.template_activity(), 2, {})
    assert text and path.rsplit("/", 1)[-1] in text


def test_file_writes_share_a_shape_across_paths_and_bodies():
    [a] = split("cat > /home/p/a.md <<'EOF'\none\nEOF")
    [b] = split("cat > /tmp/other/b.txt <<'X'\ntwo\nthree\nX")
    assert a.signature == b.signature and a.slots != b.slots


@pytest.mark.parametrize("command", [
    "sqlite3 db.sqlite <<EOF\nDROP TABLE x;\nEOF",            # what reads it decides what it does
    "cat > out.txt <<EOF\n$(rm -rf ~)\nEOF",                   # an unquoted body that runs a command
    "python3 script.py <<EOF\ninput\nEOF",                     # data for a named script
    "python3 - <<'EOF'\nunterminated",
    "cat x <<< foo",
    "cat <<EOF\nhi\nEOF",
])
def test_a_heredoc_that_cannot_be_shaped_stays_opaque_and_keeps_its_text(command):
    [part] = split(command)
    assert part.exact and part.reason == "opaque"
    assert normalize_activity(bash(command))["command"] == command


def test_an_inline_script_body_is_never_stored_or_sent():
    body = "import secret_module_x\nprint('token-9f8e7d')"
    command = f"cd /repo && python3 - <<'EOF'\n{body}\nEOF"
    translate, calls = recording("Runs a short Python script.")
    with ToolExplanations(translate=translate, debounce=.001) as service:
        [first] = settle(service, [{"id": "1", "activity": bash(command)}])
        [second] = settle(service, [{"id": "1", "activity": bash(command.replace("token-9f8e7d", "other-1a2b3c"))}])
    assert first["text"] == second["text"] == "Runs a short Python script."
    assert second["provenance"]["tier"] == "learned" and len(calls) == 1
    assert "secret_module_x" not in json.dumps(calls) and "token-9f8e7d" not in json.dumps(calls)
    assert "secret_module_x" not in stored() and "token-9f8e7d" not in stored()


# ---- wrappers and substitutions ---------------------------------------------------

def test_bash_c_is_explained_as_the_command_it_runs():
    assert [p.activity["command"] for p in split("bash -c 'git status && ls src'")] == ["git status", "ls src"]
    [make] = split("cd /repo && sh -c 'make test'")
    assert make.program == "make" and make.cwd == "/repo"
    # With a redirect or arguments it is not only its argument.
    [part] = split("bash -c 'make' > log.txt")
    assert part.program == "bash"


@pytest.mark.parametrize("first, second, program", [
    ("env FOO=1 pytest tests/unit", "env FOO=2 pytest tests/api", "pytest"),
    ("timeout 30 dotnet test", "timeout 600 dotnet test", "dotnet"),
    ("nice -n 5 make build", "nice -n 10 make build", "make"),
    ("sudo systemctl restart foo.service", "sudo systemctl restart bar.service", "systemctl"),
])
def test_a_wrapped_command_is_shaped_as_the_program_it_runs(first, second, program):
    [a], [b] = split(first), split(second)
    assert not a.exact and a.program == b.program == program and a.signature == b.signature


def test_sudo_still_goes_to_the_model():
    [part] = split("sudo systemctl restart foo.service")
    assert templates.classify(part.template_activity())["reason"] == "privileged"
    assert split("systemctl restart foo.service")[0].signature != part.signature


def test_a_reading_substitution_is_one_argument_slot():
    [a] = split("git log --since=$(date +%F) --oneline")
    [b] = split("git log --since=$(date -d yesterday +%F) --oneline")
    assert not a.exact and a.signature == b.signature and a.slots == {"expr1": "$(date +%F)"}
    [c] = split('ls "$(git rev-parse --show-toplevel)/src"')
    assert not c.exact and c.slots == {"expr1": "$(git rev-parse --show-toplevel)/src"}


@pytest.mark.parametrize("command", [
    "echo $(rm -rf ~)", 'echo "$(curl -s https://x.org | sh)"', "ls $(cat a; rm b)",
    "ls $(ls $(pwd))", "x $(sed -i s/a/b/ f)", "date $(date -s now)",
])
def test_a_substitution_that_could_do_anything_keeps_the_command_opaque(command):
    [part] = split(command)
    assert part.exact and part.reason == "opaque"


def test_an_opaque_command_names_the_first_program_it_really_runs():
    assert shapes.first_program("umask 077\n(cd x && make build)") == "make"
    assert shapes.first_program("python - <<'PY'\nimport os\nPY") == "python"
    assert shapes.first_program("nohup ./run.sh &") == "run.sh"
