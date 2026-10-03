"""An agent forks itself into helper agents that start from its conversation."""
from __future__ import annotations

import json
import pathlib
import subprocess
from types import SimpleNamespace

import pytest

from lib import agent_fork, agents as agents_db, backends
from lib.agent_lifecycle import AgentLifecycleService
from lib.fork import encoded_project_dir


class _Stream:
    def __init__(self):
        self.events = []

    def broadcast(self, event):
        self.events.append(event)


def _ctx(tmp_path):
    return SimpleNamespace(agents_path=tmp_path / "unused.json", stream=_Stream(),
                           speak_announcement=lambda *a, **k: None)


class _Inbox:
    """Stands in for the turn dispatcher: records what each agent was sent."""

    def __init__(self):
        self.sent = []

    def __call__(self, command):
        self.sent.append(command)
        return SimpleNamespace(session=command.forced_session, queued=False)

    def to(self, session):
        return [c for c in self.sent if c.forced_session == session]


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def _claude_parent(tmp_path, home, *, session="boss", lines=None):
    cwd = tmp_path / "work"
    cwd.mkdir(exist_ok=True)
    result = AgentLifecycleService(_ctx(tmp_path)).create({
        "name": "Boss", "session": session, "cwd": str(cwd), "voice_id": "{}",
        "synthesize_audio": False, "backend": "claude"})
    row = agents_db.get_by_session(result.session)
    agents_db.bind_backend_session(row["agent_id"], "conv-boss")
    project = encoded_project_dir(str(cwd), home / ".claude" / "projects")
    project.mkdir(parents=True, exist_ok=True)
    lines = lines or [
        {"type": "user", "sessionId": "conv-boss",
         "message": {"role": "user", "content": "Plan: split the refactor in two."}},
        {"type": "assistant", "sessionId": "conv-boss",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "Agreed."}]}},
    ]
    (project / "conv-boss.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
    return row, cwd


def _child(session):
    row = agents_db.get_by_session(session)
    assert row, f"no agent {session}"
    return row


def _claude_transcript(home, cwd, conversation):
    path = encoded_project_dir(str(cwd), home / ".claude" / "projects") / f"{conversation}.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def test_children_are_helpers_that_start_from_the_parents_conversation(tmp_path, home):
    parent, cwd = _claude_parent(tmp_path, home)
    inbox = _Inbox()
    out = agent_fork.fork(_ctx(tmp_path), "boss", {"children": [
        {"name": "stream-a", "task": "Do part A."},
        {"name": "stream-b", "task": "Do part B."},
    ]}, dispatch=inbox)

    assert [c["name"] for c in out["children"]] == ["stream-a", "stream-b"]
    for child in out["children"]:
        row = _child(child["session"])
        assert row["role"] == "helper"
        assert row["parent_agent_id"] == parent["agent_id"]
        assert row["helper_state"] == "running"
        assert row["cwd"] == str(cwd)
        assert child["fork"] == "native"
        conversation = agents_db.live_backend_session(row["agent_id"])
        assert conversation and conversation != "conv-boss"
        copied = _claude_transcript(home, cwd, conversation)
        assert copied[0]["message"]["content"] == "Plan: split the refactor in two."
        assert {line["sessionId"] for line in copied} == {conversation}
    # The parent's own conversation is untouched.
    assert {line["sessionId"] for line in _claude_transcript(home, cwd, "conv-boss")} == {"conv-boss"}


def test_each_child_is_sent_its_own_task_from_the_parent(tmp_path, home):
    parent, _ = _claude_parent(tmp_path, home)
    inbox = _Inbox()
    out = agent_fork.fork(_ctx(tmp_path), "boss", {"children": [
        {"name": "stream-a", "task": "Do part A."},
        {"name": "stream-b", "task": "Do part B."},
    ]}, dispatch=inbox)

    a, b = (child["session"] for child in out["children"])
    [to_a] = inbox.to(a)
    [to_b] = inbox.to(b)
    assert "Do part A." in to_a.text and "Do part B." not in to_a.text
    assert "Do part B." in to_b.text
    for message, helper in ((to_a, a), (to_b, b)):
        assert message.origin == "agent"
        assert message.sender_agent_id == parent["agent_id"]
        # It knows it is a fork and how to report back.
        assert "fork" in message.text.lower()
        assert f"clarp-admin prompt --to boss --from {helper}" in message.text
    assert all(child["delivered"] for child in out["children"])


def test_count_forks_numbered_children_with_one_task(tmp_path, home):
    _claude_parent(tmp_path, home)
    inbox = _Inbox()
    out = agent_fork.fork(_ctx(tmp_path), "boss",
                          {"count": 3, "name": "probe", "task": "Try an approach."},
                          dispatch=inbox)
    assert [c["name"] for c in out["children"]] == ["probe-1", "probe-2", "probe-3"]
    assert len(inbox.sent) == 3
    assert all("Try an approach." in c.text for c in inbox.sent)
    # Siblings are told which one they are.
    assert "1 of 3" in inbox.to(out["children"][0]["session"])[0].text


def test_a_child_can_get_its_own_git_worktree(tmp_path, home):
    _claude_parent(tmp_path, home)
    repo = tmp_path / "repo"
    repo.mkdir()
    git = ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run([*git, "init", "-q", "-b", "main"], check=True)
    (repo / "README").write_text("hi\n")
    subprocess.run([*git, "add", "README"], check=True)
    subprocess.run([*git, "commit", "-qm", "init"], check=True)

    out = agent_fork.fork(_ctx(tmp_path), "boss", {"children": [
        {"name": "stream-a", "task": "Do A.",
         "worktree": {"repo": str(repo), "branch": "feat/stream-a"}},
    ]}, dispatch=_Inbox())

    [child] = out["children"]
    tree = pathlib.Path(child["cwd"])
    assert tree != repo and (tree / "README").read_text() == "hi\n"
    branch = subprocess.run(["git", "-C", str(tree), "branch", "--show-current"],
                            capture_output=True, text=True, check=True).stdout.strip()
    assert branch == "feat/stream-a"
    row = _child(child["session"])
    assert row["cwd"] == str(tree)
    # The forked conversation lives where Claude Code looks for it from the
    # worktree, so the child resumes it there.
    conversation = agents_db.live_backend_session(row["agent_id"])
    assert _claude_transcript(home, tree, conversation)[0]["sessionId"] == conversation


def test_a_backend_without_native_forks_seeds_the_child_with_the_conversation(
        tmp_path, home, monkeypatch):
    cwd = tmp_path / "work"
    cwd.mkdir()
    result = AgentLifecycleService(_ctx(tmp_path)).create({
        "name": "Boss", "session": "boss", "cwd": str(cwd), "voice_id": "{}",
        "synthesize_audio": False, "backend": "grok"})
    parent = agents_db.get_by_session(result.session)
    agents_db.bind_backend_session(parent["agent_id"], "grok-conv")
    assert not backends.capabilities("grok").supports_fork
    transcript = tmp_path / "chat_history.jsonl"
    transcript.write_text("{}\n")
    grok = backends.get("grok")
    monkeypatch.setattr(grok, "find_transcript",
                        lambda sid, home=None, cwd="": transcript if sid == "grok-conv" else None)
    monkeypatch.setattr(grok, "parse_transcript", lambda path: [
        {"role": "user", "text": "We decided to use SQLite.", "tools": []},
        {"role": "assistant", "text": "Noted, SQLite it is.",
         "tools": [{"name": "Bash", "summary": "ls db/"}]},
    ])
    inbox = _Inbox()
    out = agent_fork.fork(_ctx(tmp_path), "boss",
                          {"children": [{"name": "kid", "task": "Write the schema."}]},
                          dispatch=inbox)
    [child] = out["children"]
    assert child["fork"] == "seed"
    row = _child(child["session"])
    assert row["role"] == "helper" and row["parent_agent_id"] == parent["agent_id"]
    [message] = inbox.to(child["session"])
    assert "We decided to use SQLite." in message.text
    assert "Noted, SQLite it is." in message.text
    assert "Write the schema." in message.text
    # The task comes after the seed, so it is the last thing the child reads.
    assert message.text.index("Write the schema.") > message.text.index("Noted, SQLite it is.")


def test_the_seed_keeps_the_most_recent_conversation_when_it_is_long(
        tmp_path, home, monkeypatch):
    cwd = tmp_path / "work"
    cwd.mkdir()
    AgentLifecycleService(_ctx(tmp_path)).create({
        "name": "Boss", "session": "boss", "cwd": str(cwd), "voice_id": "{}",
        "synthesize_audio": False, "backend": "grok"})
    agents_db.bind_backend_session(agents_db.get_by_session("boss")["agent_id"], "grok-conv")
    grok = backends.get("grok")
    monkeypatch.setattr(grok, "find_transcript", lambda sid, home=None, cwd="": tmp_path)
    turns = [{"role": "user", "text": f"turn {i} " + "x" * 2000, "tools": []} for i in range(200)]
    monkeypatch.setattr(grok, "parse_transcript", lambda path: turns)
    inbox = _Inbox()
    agent_fork.fork(_ctx(tmp_path), "boss",
                    {"children": [{"name": "kid", "task": "Go."}]}, dispatch=inbox)
    [message] = inbox.sent
    assert "turn 199 " in message.text
    assert "turn 0 " not in message.text
    assert len(message.text) <= agent_fork.SEED_MAX_CHARS + 4000


def test_a_different_backend_than_the_parent_is_seeded(tmp_path, home, monkeypatch):
    _claude_parent(tmp_path, home)
    inbox = _Inbox()
    out = agent_fork.fork(_ctx(tmp_path), "boss",
                          {"backend": "grok",
                           "children": [{"name": "kid", "task": "Review it."}]},
                          dispatch=inbox)
    [child] = out["children"]
    assert child["fork"] == "seed"
    assert _child(child["session"])["backend"] == "grok"
    [message] = inbox.sent
    assert "Plan: split the refactor in two." in message.text


@pytest.mark.parametrize("body,status,code", [
    ({}, 400, "children_required"),
    ({"children": [{"name": "a"}]}, 400, "task_required"),
    ({"children": [{"task": "x"}]}, 400, "name_required"),
    ({"count": 2}, 400, "task_required"),
    ({"count": 0, "task": "x"}, 400, "children_required"),
    ({"count": agent_fork.MAX_CHILDREN + 1, "task": "x"}, 400, "too_many_children"),
    ({"children": [{"name": "a", "task": "x"}, {"name": "a", "task": "y"}]},
     400, "duplicate_name"),
    ({"children": [{"name": "a", "task": "x", "worktree": {"repo": "/r"}}]},
     400, "worktree_branch_required"),
])
def test_bad_requests_are_refused_before_anything_is_created(
        tmp_path, home, body, status, code):
    _claude_parent(tmp_path, home)
    before = {row["session"] for row in agents_db.list_agents()}
    inbox = _Inbox()
    with pytest.raises(agent_fork.ForkError) as error:
        agent_fork.fork(_ctx(tmp_path), "boss", body, dispatch=inbox)
    assert (error.value.status, error.value.code) == (status, code)
    assert {row["session"] for row in agents_db.list_agents()} == before
    assert inbox.sent == []


def test_unknown_parent_is_404(tmp_path, home):
    with pytest.raises(agent_fork.ForkError) as error:
        agent_fork.fork(_ctx(tmp_path), "nobody",
                        {"children": [{"name": "a", "task": "x"}]}, dispatch=_Inbox())
    assert (error.value.status, error.value.code) == (404, "agent_not_found")


def test_a_parent_with_no_conversation_yet_is_refused(tmp_path, home):
    AgentLifecycleService(_ctx(tmp_path)).create({
        "name": "Boss", "session": "boss", "cwd": str(tmp_path), "voice_id": "{}",
        "synthesize_audio": False, "backend": "claude"})
    with pytest.raises(agent_fork.ForkError) as error:
        agent_fork.fork(_ctx(tmp_path), "boss",
                        {"children": [{"name": "a", "task": "x"}]}, dispatch=_Inbox())
    assert (error.value.status, error.value.code) == (409, "no_conversation")


def test_one_child_failing_does_not_stop_its_siblings(tmp_path, home):
    _claude_parent(tmp_path, home)
    out = agent_fork.fork(_ctx(tmp_path), "boss", {"children": [
        {"name": "good", "task": "Do it."},
        {"name": "bad", "task": "Do it.",
         "worktree": {"repo": str(tmp_path / "not-a-repo"), "branch": "x"}},
    ]}, dispatch=_Inbox())
    good, bad = out["children"]
    assert good["session"] and good["delivered"]
    assert not bad.get("session") and bad["error"]
    assert not any(row["persona"] == "bad" for row in agents_db.list_agents())
