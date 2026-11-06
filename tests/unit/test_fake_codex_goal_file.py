"""Real fake-provider persistence boundaries: readers and competing processes."""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

import pytest


PROVIDER = Path(__file__).resolve().parents[1] / "qa" / "fake_codex.py"
# Instrument only the disposable child. No fixture flag or production seam.
SHIM = r'''
import fcntl, inspect, json, os, pathlib, runpy, sys, tempfile, threading, time
source, mode, gate = sys.argv[1:]
gate = pathlib.Path(gate)
original_read = pathlib.Path.read_text
original_write = pathlib.Path.write_text
original_replace = os.replace
original_flock = fcntl.flock

def mark(name):
    original_write(gate / name, '1')

def hold():
    mark('ready')
    end = time.monotonic() + 6
    while not (gate / 'release').exists():
        if time.monotonic() > end:
            raise TimeoutError('test publication barrier unreleased')
        time.sleep(.005)

def caller(name):
    return any(frame.function == name for frame in inspect.stack())

reads = 0
def read(path, *a, **kw):
    global reads
    if path.name == 'fake-goals.json' and caller('load_goal') and mode.startswith('read-'):
        reads += 1
        mark('read-' + str(reads))
        if mode == 'read-io':
            raise PermissionError('injected read denial')
        if reads == 1 or mode == 'read-twice':
            return ''
    result = original_read(path, *a, **kw)
    if path.name == 'fake-goals.json' and caller('save_goal'):
        if mode == 'rmw-first':
            hold()
        elif mode == 'rmw-second':
            mark('read-current')
    return result
pathlib.Path.read_text = read

def write(path, data, *a, **kw):
    if mode == 'publication' and path.name == 'fake-goals.json' and threading.current_thread() is not threading.main_thread():
        with path.open('w') as stream:
            hold()  # baseline has truncated the canonical file here
            return stream.write(data)
    result = original_write(path, data, *a, **kw)
    if mode == 'publication' and path.name == 'fake-goals.json':
        original_write(gate / 'before', original_read(path))
    return result
pathlib.Path.write_text = write

def replace(src, dst, *a, **kw):
    if pathlib.Path(dst).name == 'fake-goals.json':
        if mode == 'publication' and threading.current_thread() is not threading.main_thread():
            hold()  # repaired fixture has only written a private temp file
        if mode == 'fail-replace':
            raise OSError('injected pre-replace failure')
    result = original_replace(src, dst, *a, **kw)
    if mode == 'publication' and pathlib.Path(dst).name == 'fake-goals.json' and threading.current_thread() is threading.main_thread():
        original_write(gate / 'before', original_read(pathlib.Path(dst)))
    return result
os.replace = replace

def flock(fd, operation):
    if mode == 'rmw-second' and operation == fcntl.LOCK_EX:
        mark('lock-attempt')
    return original_flock(fd, operation)
fcntl.flock = flock
if mode == 'fail-fsync':
    def fail_fsync(fd):
        raise OSError('injected pre-publication fsync failure')
    os.fsync = fail_fsync
if mode == 'fail-write':
    original_temp = tempfile.NamedTemporaryFile
    class BrokenTemp:
        def __init__(self, *a, **kw):
            self.stream = original_temp(*a, **kw)
        def __enter__(self):
            return self
        def __exit__(self, *a):
            self.stream.close()
        def __getattr__(self, name):
            return getattr(self.stream, name)
        def write(self, data):
            raise OSError('injected temporary write failure')
    tempfile.NamedTemporaryFile = BrokenTemp
def profile(frame, event, arg):
    if (event == 'call' and frame.f_code.co_name == 'save_goal'
            and mode == 'publication' and (gate / 'ready').exists()
            and threading.current_thread() is threading.main_thread()):
        mark('competing-writer')
sys.setprofile(profile)
sys.argv = [source, 'app-server']
runpy.run_path(source, run_name='__main__')
'''


class Provider:
    def __init__(self, root, home, mode="normal"):
        self.gate = root
        root.mkdir()
        env = os.environ.copy()
        env.pop("CODEX_HOME", None)
        if home is not None:
            home.mkdir(exist_ok=True)
            env["CODEX_HOME"] = str(home)
        env["CLARP_QA_PROVIDER_ROOT"] = str(root)
        self.stderr = (root / "stderr").open("w+")
        self.proc = subprocess.Popen(
            [sys.executable, "-u", "-c", SHIM, str(PROVIDER), mode, str(root)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr,
            text=True, env=env,
        )
        self.events = queue.Queue()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        self.serial = 0

    def _read(self):
        for line in self.proc.stdout:
            self.events.put(json.loads(line))
        self.events.put(None)

    def send(self, method, **params):
        self.serial += 1
        self.proc.stdin.write(json.dumps({"id": self.serial, "method": method, "params": params}) + "\n")
        self.proc.stdin.flush()
        return self.serial

    def response(self, ident):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            item = self.events.get(timeout=max(.01, deadline - time.monotonic()))
            assert item is not None, self.error()
            if item.get("id") == ident:
                assert "error" not in item, item
                return item["result"]
        raise AssertionError("provider response deadline")

    def call(self, method, **params):
        return self.response(self.send(method, **params))

    def error(self):
        self.stderr.flush()
        return (self.gate / "stderr").read_text()

    def close(self):
        (self.gate / "release").touch()
        if self.proc.poll() is None:
            self.proc.stdin.close()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=3)
        self.reader.join(timeout=3)
        assert not self.reader.is_alive()
        self.proc.stdout.close()
        self.stderr.close()


@pytest.fixture
def providers():
    owned = []
    def start(*args, **kwargs):
        provider = Provider(*args, **kwargs)
        owned.append(provider)
        return provider
    yield start
    for provider in reversed(owned):
        provider.close()


def wait_file(path):
    deadline = time.monotonic() + 4
    while not path.exists():
        assert time.monotonic() < deadline, path.name
        time.sleep(.005)


def stored_goal(objective="preserved"):
    return {"objective": objective, "status": "paused", "tokens": 0, "created": 123, "updated": 123}


def test_background_publication_never_exposes_empty_json(tmp_path, providers):
    home = tmp_path / "home"
    home.mkdir()
    target = home / "fake-goals.json"
    previous = {"other": stored_goal()}
    target.write_text(json.dumps(previous))
    provider = providers(tmp_path / "publisher", home, "publication")
    provider.call("thread/goal/set", threadId="background", objective="synthetic", status="active")
    # The initial set is complete; the real background goal_turn is held mid-publication.
    before = json.loads((provider.gate / "before").read_text())
    wait_file(provider.gate / "ready")
    try:
        reader = subprocess.run(
            [sys.executable, "-c", "import json,sys; print(json.dumps(json.load(open(sys.argv[1]))))", str(target)],
            text=True, capture_output=True, timeout=3,
        )
        assert reader.returncode == 0, reader.stderr
        assert json.loads(reader.stdout) == before
        competing = provider.send("thread/goal/set", threadId="competing", objective="synthetic", status="paused")
        wait_file(provider.gate / "competing-writer")
    finally:
        (provider.gate / "release").touch()
    # A completion RPC proves the daemon publisher succeeded, rather than merely exiting.
    deadline = time.monotonic() + 4
    completed = replied = False
    while not (completed and replied):
        event = provider.events.get(timeout=max(.01, deadline - time.monotonic()))
        assert event is not None, provider.error()
        if event.get("method") == "turn/completed":
            completed = True
        if event.get("id") == competing:
            assert "error" not in event
            replied = True
        assert time.monotonic() < deadline
    current = json.loads(target.read_text())
    assert current["other"] == previous["other"]
    assert current["background"]["status"] == "complete"
    assert current["background"]["tokens"] == 7
    assert current["competing"]["status"] == "paused"
    assert not list(home.glob(".fake-goals-*.tmp"))
    assert "Traceback" not in provider.error()


def test_shared_writers_merge_and_clear_without_losing_other_goals(tmp_path, providers):
    home = tmp_path / "home"
    home.mkdir()
    target = home / "fake-goals.json"
    seed = {"untouched": stored_goal()}
    target.write_text(json.dumps(seed))
    first = providers(tmp_path / "first", home, "rmw-first")
    second = providers(tmp_path / "second", home, "rmw-second")
    a = first.send("thread/goal/set", threadId="one", objective="first", status="paused")
    wait_file(first.gate / "ready")
    b = second.send("thread/goal/set", threadId="two", objective="second", status="paused")
    # Normal fence blocks before read; a lock-omission variant reads stale state.
    deadline = time.monotonic() + 4
    while not any((second.gate / name).exists() for name in ("lock-attempt", "read-current")):
        assert time.monotonic() < deadline
        time.sleep(.005)
    try:
        if (second.gate / "read-current").exists():
            second.response(b)
            b = None
        else:
            assert not (second.gate / "read-current").exists()
    finally:
        (first.gate / "release").touch()
    first.response(a)
    if b is not None:
        second.response(b)
    stored = json.loads(target.read_text())
    assert set(stored) == {"untouched", "one", "two"}
    assert stored["untouched"] == seed["untouched"]
    assert stored["one"]["objective"] == "first"
    assert stored["two"]["objective"] == "second"
    other = stored["two"]
    first.call("thread/goal/clear", threadId="one")
    assert json.loads(target.read_text()) == {"untouched": seed["untouched"], "two": other}


@pytest.mark.parametrize("mode", ["read-once", "read-twice", "read-io"])
def test_goal_read_decode_retry_is_once_logged_and_not_io_retry(tmp_path, providers, mode):
    home = tmp_path / "home"
    home.mkdir()
    goal = stored_goal()
    (home / "fake-goals.json").write_text(json.dumps({"one": goal}))
    provider = providers(tmp_path / "reader", home, mode)
    request = provider.send("thread/goal/get", threadId="one")
    if mode == "read-once":
        assert provider.response(request)["goal"]["objective"] == goal["objective"]
    else:
        assert provider.proc.wait(timeout=4) != 0
    error = provider.error()
    assert "preserved" not in error
    assert (provider.gate / "read-1").exists()
    assert (provider.gate / "read-2").exists() == (mode != "read-io")
    assert not (provider.gate / "read-3").exists()
    assert error.count("retrying goal-file JSON decode once") == (mode != "read-io")
    if mode == "read-twice":
        assert "JSONDecodeError" in error
    if mode == "read-io":
        assert "PermissionError" in error


@pytest.mark.parametrize("mode", ["fail-write", "fail-fsync", "fail-replace"])
def test_failed_publication_keeps_old_file_cleans_temp_and_releases_fence(tmp_path, providers, mode):
    home = tmp_path / "home"
    home.mkdir()
    target = home / "fake-goals.json"
    original = json.dumps({"old": stored_goal()})
    target.write_text(original)
    broken = providers(tmp_path / "broken", home, mode)
    broken.send("thread/goal/set", threadId="new", objective="synthetic", status="paused")
    assert broken.proc.wait(timeout=4) != 0
    assert "injected" in broken.error()
    assert target.read_text() == original
    assert not list(home.glob(".fake-goals-*.tmp"))
    writer = providers(tmp_path / "next", home)
    writer.call("thread/goal/set", threadId="new", objective="synthetic", status="paused")
    assert set(json.loads(target.read_text())) == {"old", "new"}


def test_corrupt_writer_state_is_not_replaced_with_empty_map(tmp_path, providers):
    home = tmp_path / "home"
    home.mkdir()
    target = home / "fake-goals.json"
    target.write_text('{"damaged":')
    provider = providers(tmp_path / "writer", home)
    provider.send("thread/goal/set", threadId="new", objective="synthetic", status="paused")
    assert provider.proc.wait(timeout=4) != 0
    assert "JSONDecodeError" in provider.error()
    assert target.read_text() == '{"damaged":'


@pytest.mark.parametrize("has_home", [True, False])
def test_first_create_and_no_home_keep_goal_protocol(tmp_path, providers, has_home):
    home = tmp_path / "home" if has_home else None
    provider = providers(tmp_path / "provider", home)
    result = provider.call("thread/goal/set", threadId="one", objective="synthetic", status="paused")
    assert result["goal"]["status"] == "paused"
    assert provider.call("thread/goal/get", threadId="one")["goal"] == result["goal"]
    assert provider.call("thread/goal/clear", threadId="one") == {"cleared": True}
    if has_home:
        assert json.loads((home / "fake-goals.json").read_text()) == {}
