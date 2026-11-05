"""BACKENDS-08: Linux caps one argv string at 128 KiB (MAX_ARG_STRLEN). A
long prompt reaches an argv CLI through stdin or a prompt file; a backend
that has neither refuses it before admission, so nothing is stored."""
from __future__ import annotations

import errno
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
from lib.backend import long_prompt
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


# Reviewer A's probes on 74c467ec (7b8792fa), unchanged.
def test_prompt_file_permissions_and_normal_disposal(tmp_path, monkeypatch):
    real_mkdtemp = long_prompt.tempfile.mkdtemp
    monkeypatch.setattr(long_prompt.tempfile, 'mkdtemp',
                        lambda **kw: real_mkdtemp(dir=tmp_path, **kw))
    prompt = long_prompt.PromptFile('synthetic prompt')
    assert stat.S_IMODE(__import__('os').stat(prompt.dir).st_mode) == 0o700
    assert stat.S_IMODE(__import__('os').stat(prompt.path).st_mode) == 0o600
    with prompt.open() as source:
        assert source.read() == 'synthetic prompt'
    prompt.remove()
    assert not list(tmp_path.iterdir())


def test_a_failed_prompt_write_leaves_no_partial_conversation_file(tmp_path, monkeypatch):
    real_mkdtemp, real_fdopen = long_prompt.tempfile.mkdtemp, long_prompt.os.fdopen
    def mkdtemp(**kw):
        return real_mkdtemp(dir=tmp_path, **kw)
    class FullDisk:
        def __init__(self, fd, *a, **kw):
            self.file = real_fdopen(fd, *a, **kw)
        def __enter__(self):
            self.file.__enter__()
            return self
        def write(self, text):
            self.file.write(text[:4])
            self.file.flush()
            raise OSError(errno.ENOSPC, 'synthetic full disk after a partial write')
        def __exit__(self, *exc):
            return self.file.__exit__(*exc)
    with monkeypatch.context() as patch:
        patch.setattr(long_prompt.tempfile, 'mkdtemp', mkdtemp)
        patch.setattr(long_prompt.os, 'fdopen', FullDisk)
        with pytest.raises(OSError) as error:
            long_prompt.PromptFile('synthetic private prompt')
        assert error.value.errno == errno.ENOSPC
    assert not list(tmp_path.iterdir()), 'failed construction left a private prompt artifact without a cleanup owner'


def test_a_failed_stdin_open_leaves_no_prompt_file(tmp_path, monkeypatch):
    _recorder(tmp_path, monkeypatch, "codex")
    temp = tmp_path / "prompts"
    temp.mkdir()
    real_mkdtemp = long_prompt.tempfile.mkdtemp
    monkeypatch.setattr(long_prompt.tempfile, "mkdtemp",
                        lambda **kw: real_mkdtemp(dir=temp, **kw))

    def refuse(self):
        raise OSError(errno.EMFILE, "synthetic: no file descriptors left")
    monkeypatch.setattr(long_prompt.PromptFile, "open", refuse)
    agent_id = agents_db.create_agent(persona="Codex", voice_id="v", cwd=str(tmp_path),
                                      session="s-codex", backend="codex")
    with pytest.raises(OSError):
        by_id("codex").start_turn(text=BIG, cwd=tmp_path, session="s-codex", agent_id=agent_id,
                                  on_result=lambda *_a, **_k: None,
                                  on_error=lambda *_a, **_k: None, trace_id="t-codex")
    assert not list(temp.iterdir()), "a failed stdin open left the prompt file behind"


# Reviewer A's probe on 1ce34dc3 (b8516c80), unchanged.
def test_a_failed_fdopen_closes_the_still_owned_raw_descriptor(tmp_path, monkeypatch):
    import os
    real_mkdtemp, real_open = long_prompt.tempfile.mkdtemp, long_prompt.os.open
    captured = []
    def capture_open(*args, **kw):
        fd = real_open(*args, **kw)
        if str(args[0]).endswith('/prompt.txt'):
            captured.append(fd)
        return fd
    def no_wrapper(*args, **kw):
        raise OSError(errno.EMFILE, 'synthetic failure constructing the file wrapper')
    with monkeypatch.context() as patch:
        patch.setattr(long_prompt.tempfile, 'mkdtemp', lambda **kw: real_mkdtemp(dir=tmp_path, **kw))
        patch.setattr(long_prompt.os, 'open', capture_open)
        patch.setattr(long_prompt.os, 'fdopen', no_wrapper)
        with pytest.raises(OSError):
            long_prompt.PromptFile('synthetic prompt')
    assert len(captured) == 1
    try:
        with pytest.raises(OSError) as error:
            os.fstat(captured[0])
        assert error.value.errno == errno.EBADF
    finally:
        try:
            os.close(captured[0])
        except OSError:
            pass
    assert not list(tmp_path.iterdir())
