"""BACKENDS-08: Linux caps one argv string at 128 KiB (MAX_ARG_STRLEN). A
long prompt reaches an argv CLI through stdin or a prompt file; a backend
that has neither refuses it before admission, so nothing is stored."""
from __future__ import annotations

import json
import os
import pathlib
import stat
import sys
import textwrap
import time
from types import SimpleNamespace

import pytest

from lib import agents as agents_db, backends, turn_dispatch as td
from lib.backend.registry import by_id

BIG = "long log line\n" * 15_000          # about 210 KB, well past 128 KiB


@pytest.fixture(autouse=True)
def _fake_clis_are_this_tests_own(fake_backend_cli, tmp_path):
    fake_backend_cli.dir(tmp_path)


def _recorder(tmp_path, monkeypatch, name: str) -> pathlib.Path:
    """A fake CLI that records its argv, its stdin and any --prompt-file."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    out = tmp_path / f"{name}.json"
    script = bin_dir / name
    script.write_text(textwrap.dedent(f"""\
        #!{sys.executable}
        import json, sys
        argv = sys.argv[1:]
        prompt_file = ""
        if "--prompt-file" in argv:
            with open(argv[argv.index("--prompt-file") + 1]) as fh:
                prompt_file = fh.read()
        record = {{"argv": argv, "stdin": sys.stdin.read(), "prompt_file": prompt_file}}
        with open({str(out)!r}, "w") as fh:
            json.dump(record, fh)
    """))
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(by_id(name), "required_binary", str(script))
    return out


def _run(tmp_path, name: str, text: str) -> dict:
    agent_id = agents_db.create_agent(persona=name.title(), voice_id="v", cwd=str(tmp_path),
                                      session=f"s-{name}", backend=name)
    handle = by_id(name).start_turn(
        text=text, cwd=tmp_path, session=f"s-{name}", agent_id=agent_id,
        on_result=lambda *_a, **_k: None, on_error=lambda *_a, **_k: None,
        trace_id=f"t-{name}")
    out = tmp_path / f"{name}.json"
    deadline = time.monotonic() + 10
    while not out.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert out.exists(), f"the fake {name} never ran"
    handle.proc.wait(10)
    if handle.drain_thread is not None:
        handle.drain_thread.join(10)
    return json.loads(out.read_text())


def test_a_long_codex_exec_prompt_goes_through_stdin(tmp_path, monkeypatch):
    _recorder(tmp_path, monkeypatch, "codex")
    record = _run(tmp_path, "codex", BIG)
    assert record["argv"][-1] == "-", record["argv"][-3:]
    assert BIG not in " ".join(record["argv"])
    assert BIG in record["stdin"]


@pytest.mark.parametrize("name", ["grok", "muse"])
def test_a_long_prompt_goes_through_a_prompt_file_that_is_removed_afterwards(tmp_path, monkeypatch, name):
    _recorder(tmp_path, monkeypatch, name)
    record = _run(tmp_path, name, BIG)
    argv = record["argv"]
    assert "--prompt-file" in argv and BIG not in " ".join(argv)
    assert BIG in record["prompt_file"]
    path = pathlib.Path(argv[argv.index("--prompt-file") + 1])
    assert not path.exists(), "the prompt file outlived its turn"


@pytest.mark.parametrize("name,flag", [("grok", "-p"), ("muse", "--")])
def test_a_short_prompt_keeps_the_argv_form(tmp_path, monkeypatch, name, flag):
    _recorder(tmp_path, monkeypatch, name)
    record = _run(tmp_path, name, "hello")
    argv = record["argv"]
    assert "--prompt-file" not in argv and flag in argv
    assert "hello" in argv[argv.index(flag) + 1]


class _Stream:
    def broadcast(self, event):
        pass


def _service(tmp_path):
    td.reset_for_tests()
    return td.TurnDispatchService(SimpleNamespace(default_session="anna", stream=_Stream(),
                                                  agents_path=tmp_path / "unused.json"),
                                  backend_registry=backends, home=tmp_path)


def _counts():
    con = agents_db.conn()
    return (con.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
            con.execute("SELECT COUNT(*) FROM prompt_admissions").fetchone()[0])


def test_a_message_too_long_for_agy_is_refused_before_admission(tmp_path, monkeypatch):
    agent_id = agents_db.create_agent(persona="Anna", voice_id="v", cwd=str(tmp_path),
                                      session="anna", backend="agy")
    agents_db.start_runtime(agent_id, "anna")
    spawned = []
    monkeypatch.setattr(by_id("agy"), "start_turn", lambda **kw: spawned.append(kw))
    service = _service(tmp_path)
    before = _counts()
    with pytest.raises(td.DispatchError) as error:
        service.dispatch(text=BIG, requested_session="anna", trace_id="t-big",
                         client_msg_id="cm-big", synthesize_audio=False)
    assert error.value.status == 413
    message = str(getattr(error.value, "message", "") or error.value)
    assert "KiB" in message and ("shorten" in message.lower() or "split" in message.lower()), message
    assert _counts() == before, "a refused message was stored"
    assert not spawned


def test_a_message_within_agys_limit_is_not_refused(tmp_path, monkeypatch):
    agent_id = agents_db.create_agent(persona="Anna", voice_id="v", cwd=str(tmp_path),
                                      session="anna", backend="agy")
    agents_db.start_runtime(agent_id, "anna")
    monkeypatch.setattr(by_id("agy"), "start_turn",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("control: reached the spawn")))
    service = _service(tmp_path)
    with pytest.raises(td.DispatchError) as error:
        service.dispatch(text="y" * 50_000, requested_session="anna", trace_id="t-ok",
                         client_msg_id="cm-ok", synthesize_audio=False)
    assert error.value.status != 413
