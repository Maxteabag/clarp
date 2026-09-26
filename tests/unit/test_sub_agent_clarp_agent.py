"""``clarp-sub-agent start --clarp-agent`` and ``clarp-admin agent`` with the Host faked."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def sub(tmp_path, monkeypatch):
    module = _load("sub_agent_under_test", ROOT / "skills/clarp-sub-agents/scripts/sub_agent.py")
    monkeypatch.setattr(module, "DIR", tmp_path / "state")
    monkeypatch.setenv("CLARP_SESSION", "boss")
    module.calls = {"admin": [], "bg": [], "run": []}

    def fake_run(argv, **kwargs):
        module.calls["run"].append(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(module.shutil, "which", lambda exe: f"/usr/bin/{exe}")
    monkeypatch.setattr(module, "_bg", lambda *args: (
        module.calls["bg"].append(args), "bg1:1:job")[1])
    return module


def _start(sub, tmp_path, *extra):
    workdir = tmp_path / "wt"
    workdir.mkdir(exist_ok=True)
    prompt = tmp_path / "task.md"
    prompt.write_text("Refactor the parser.\n")
    return sub.main(["start", "stream-a", str(workdir), str(prompt), "--clarp-agent", *extra])


def test_start_creates_a_helper_prompts_it_and_launches_the_watcher(sub, tmp_path, monkeypatch):
    def admin(*args):
        sub.calls["admin"].append(args)
        return {"session": "stream-a-3f9c"} if args[:2] == ("agent", "create") else {"ok": True}
    monkeypatch.setattr(sub, "_admin", admin)

    assert _start(sub, tmp_path, "--backend", "codex", "--model", "gpt-5.5", "--title", "Parser") == 0

    create, prompt = sub.calls["admin"]
    assert create == ("agent", "create", "stream-a", "--parent", "boss", "--role", "helper",
                      "--cwd", str((tmp_path / "wt").resolve()), "--backend", "codex",
                      "--model", "gpt-5.5")
    assert prompt[:5] == ("prompt", "--to", "stream-a-3f9c", "--from", "boss")
    text = prompt[6]
    assert "Refactor the parser." in text
    assert "clarp-admin prompt --to boss --from stream-a-3f9c" in text
    assert (sub.DIR / "stream-a.session").read_text().strip() == "stream-a-3f9c"
    watcher = next(argv for argv in sub.calls["run"] if argv[0] == "systemd-run")
    assert watcher[-4:] == ["__watch", "stream-a", "stream-a-3f9c", "boss"]
    # Nothing ran a raw backend CLI.
    assert not any(argv[0] in {"claude", "codex"} for argv in sub.calls["run"])


def test_start_defaults_the_claude_model_and_needs_a_parent(sub, tmp_path, monkeypatch):
    monkeypatch.setattr(sub, "_admin", lambda *args: (
        sub.calls["admin"].append(args), {"session": "h-1"})[1])
    assert _start(sub, tmp_path) == 0
    assert sub.calls["admin"][0][-4:] == ("--backend", "claude", "--model", sub.DEFAULT_MODEL)
    monkeypatch.delenv("CLARP_SESSION")
    monkeypatch.delenv("CLAUDE_PWA_SESSION", raising=False)
    with pytest.raises(SystemExit):
        _start(sub, tmp_path)


def test_restart_with_the_same_name_reuses_the_live_helper(sub, tmp_path, monkeypatch):
    sub.DIR.mkdir(parents=True)
    (sub.DIR / "stream-a.session").write_text("stream-a-3f9c\n")

    def admin(*args):
        sub.calls["admin"].append(args)
        if args == ("agent", "helper-state", "stream-a-3f9c"):
            return {"role": "helper", "helper_state": "failed", "archived_at": None}
        return {"ok": True}
    monkeypatch.setattr(sub, "_admin", admin)

    assert _start(sub, tmp_path) == 0
    assert [a[:4] for a in sub.calls["admin"]] == [
        ("agent", "helper-state", "stream-a-3f9c"),
        ("agent", "helper-state", "stream-a-3f9c", "running"),
        ("prompt", "--to", "stream-a-3f9c", "--from")]


def test_start_reports_a_refused_create(sub, tmp_path, monkeypatch, capsys):
    def admin(*args):
        raise sub.AdminError("clarp-admin agent create: HTTP 409 contact_occupied")
    monkeypatch.setattr(sub, "_admin", admin)
    assert _start(sub, tmp_path) == 1
    assert "contact_occupied" in capsys.readouterr().err
    assert not any(argv[0] == "systemd-run" for argv in sub.calls["run"])


@pytest.mark.parametrize("final,ok,job_call", [
    ("reported", True, "job-finish"), ("done", True, "job-finish"),
    ("failed", False, "job-fail"), ("abandoned", False, "job-fail"),
])
def test_watcher_holds_the_job_until_the_helper_reports(sub, monkeypatch, final, ok, job_call):
    sub.DIR.mkdir(parents=True)
    (sub.DIR / "stream-a.title").write_text("Parser\n")
    states = iter(["running", "running", final])
    monkeypatch.setattr(sub, "_admin", lambda *args: {"helper_state": next(states)})
    sleeps = []
    rc = sub.watch("stream-a", "stream-a-3f9c", "boss", poll_sec=0, sleep=sleeps.append)
    assert rc == (0 if ok else 1)
    verbs = [call[1] for call in sub.calls["bg"]]
    assert verbs[:2] == ["job-upsert", "job-active"]
    assert sub.calls["bg"][0][2:] == ("sub-agent-stream-a", "sub-agent", "Parser", "stream-a-3f9c")
    assert verbs.count("job-heartbeat") == 2 and len(sleeps) == 2
    assert verbs[-1] == job_call
    assert (sub.DIR / "stream-a.exit").read_text().strip() == ("0" if ok else "1")


def test_watcher_survives_a_host_restart(sub, monkeypatch):
    sub.DIR.mkdir(parents=True)
    (sub.DIR / "stream-a.title").write_text("Parser\n")
    answers = iter([sub.AdminError("down"), sub.AdminError("down"), {"helper_state": "reported"}])

    def admin(*args):
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer
    monkeypatch.setattr(sub, "_admin", admin)
    assert sub.watch("stream-a", "h", "boss", poll_sec=0, sleep=lambda s: None) == 0


def test_systemd_mode_still_refuses_other_backends(sub, tmp_path):
    workdir = tmp_path / "wt"
    workdir.mkdir()
    prompt = tmp_path / "task.md"
    prompt.write_text("x\n")
    with pytest.raises(SystemExit, match="claude or codex"):
        sub.main(["start", "s", str(workdir), str(prompt), "--backend", "grok"])


# ---- clarp-admin agent ------------------------------------------------------

@pytest.fixture
def admin():
    return _load("agent_admin_under_test", ROOT / "bin/clarp-admin.py")


def test_admin_agent_create_posts_a_helper(admin, monkeypatch, tmp_path, capsys):
    sent = []
    monkeypatch.setattr(admin, "api_request", lambda method, path, body=None, **kw: (
        sent.append((method, path, body)), {"ok": True, "session": "h-1"})[1])
    args = admin.parser().parse_args([
        "agent", "create", "stream-a", "--parent", "boss", "--role", "helper",
        "--cwd", str(tmp_path), "--backend", "codex", "--model", "gpt-5.5"])
    assert args.func(args) == 0
    method, path, body = sent[0]
    assert (method, path) == ("POST", "/agents")
    assert body == {"name": "stream-a", "synthesize_audio": False, "cwd": str(tmp_path.resolve()),
                    "backend": "codex", "model": "gpt-5.5", "parent": "boss",
                    "role": "helper", "voice_id": "{}"}
    assert json.loads(capsys.readouterr().out)["session"] == "h-1"


def test_admin_helper_needs_a_parent(admin):
    args = admin.parser().parse_args(["agent", "create", "x", "--role", "helper"])
    with pytest.raises(SystemExit, match="--parent"):
        args.func(args)


def test_admin_helper_state_reads_and_marks(admin, monkeypatch):
    sent = []
    monkeypatch.setattr(admin, "api_request", lambda method, path, body=None, **kw: (
        sent.append((method, path, body)), {})[1])
    for argv in (["agent", "helper-state", "h-1"],
                 ["agent", "helper-state", "h-1", "done", "--from", "boss"]):
        args = admin.parser().parse_args(argv)
        assert args.func(args) == 0
    assert sent == [("GET", "/agent-helper-state?session=h-1", None),
                    ("POST", "/agent-helper-state", {"session": "h-1", "state": "done", "by": "boss"})]


def test_a_long_prompt_is_referenced_not_inlined(sub, tmp_path):
    prompt = tmp_path / "long.md"
    prompt.write_text("é" * (sub.INLINE_PROMPT_MAX // 2 + 1))
    text = sub._helper_message("n", "h-1", "boss", tmp_path, prompt)
    assert str(prompt) in text and "é" not in text


# ---- default (systemd) mode: a background process with a live log ----------

def test_render_turns_claude_stream_json_into_readable_lines(sub):
    text = json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "Reading the parser.\n"},
        {"type": "tool_use", "name": "Bash", "input": {"command": "pytest  -q tests"}},
    ]}})
    assert sub.render("claude", text + "\n") == ["Reading the parser.", "→ Bash: pytest -q tests"]
    assert sub.render("claude", json.dumps({"type": "system", "subtype": "init"})) == []
    assert sub.render("claude", json.dumps({"type": "result", "num_turns": 7})) == ["[done] 7 turns"]
    assert sub.render("claude", "plain error\n") == ["plain error"]
    assert sub.render("codex", "{not json but codex}\n") == ["{not json but codex}"]
    assert sub.render("codex", "\n") == []


def test_claude_worker_streams_json_so_the_log_is_live(sub):
    cmd = sub._command("claude", "", "do it")
    assert cmd[cmd.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in cmd


def test_run_registers_a_worker_job_with_its_log_and_streams_output(sub, tmp_path, monkeypatch, capsys):
    sub.DIR.mkdir(parents=True)
    (sub.DIR / "w.title").write_text("Worker W\n")
    (sub.DIR / "w.prompt.md").write_text("task\n")
    lines = [json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "Step one done"}]}}) + "\n"]

    class FakeProc:
        stdout = iter(lines)

        def wait(self):
            return 0
    monkeypatch.setattr(sub.subprocess, "Popen", lambda argv, **kw: FakeProc())

    assert sub.run("w", "claude", "", "boss") == 0

    bg = sub.calls["bg"]
    assert bg[0] == ("boss", "job-upsert", "sub-agent-w", "worker", "Worker W", "w")
    assert ("boss", "job-log", "bg1:1:job", str((sub.DIR / "w.log").resolve())) in bg
    assert bg[-1] == ("boss", "job-finish", "bg1:1:job")
    assert "Step one done" in capsys.readouterr().out
