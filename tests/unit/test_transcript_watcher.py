"""Tests for the per-agent transcript watcher.

The watcher tails a Claude Code transcript JSONL file. When a new
{type:"assistant"} entry with text content lands, it invokes a callback
with that text. Combined with the existing tts_queue.enqueue path, this
gives us text-input streaming — TTS starts as soon as Claude finishes a
text block, not after the whole turn.

Tests drive the watcher directly via tick() so we don't need real
inotify or a thread; the production scheduler will tick() in a loop.
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

_SERVER_DIR = pathlib.Path(__file__).resolve().parents[2] / "server"
sys.path.insert(0, str(_SERVER_DIR))


# ---- Helpers ------------------------------------------------------------


def _append(path: pathlib.Path, *entries: dict) -> None:
    """Append one or more JSONL entries to a transcript file."""
    with path.open("a") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def _assistant_text(text: str) -> dict:
    return {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": text}]},
    }


def _user(content: str) -> dict:
    return {"type": "user", "message": {"content": content}}


def _tool_use(name: str = "Bash") -> dict:
    return {
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "name": name}]},
    }


def _thinking() -> dict:
    """Claude's internal thinking blocks — we must NOT TTS these."""
    return {
        "type": "assistant",
        "message": {"content": [{"type": "thinking", "thinking": "secret"}]},
    }


# ---- Basic tail behavior -----------------------------------------------


def test_watcher_fires_callback_for_new_assistant_text(tmp_path):
    from lib.transcript_watcher import TranscriptWatcher
    tx = tmp_path / "t.jsonl"
    tx.touch()
    received: list[str] = []
    w = TranscriptWatcher(tx, on_text=received.append)
    w.tick()                                # no content yet
    assert received == []
    _append(tx, _user("hi"), _assistant_text("Hello there"))
    w.tick()
    assert received == ["Hello there"]


def test_watcher_fires_once_per_block_not_per_tick(tmp_path):
    """Once a block is consumed, ticking again should NOT re-emit it."""
    from lib.transcript_watcher import TranscriptWatcher
    tx = tmp_path / "t.jsonl"
    _append(tx, _assistant_text("just once"))
    received: list[str] = []
    w = TranscriptWatcher(tx, on_text=received.append)
    w.tick(); w.tick(); w.tick()
    assert received == ["just once"]


def test_watcher_streams_text_blocks_at_each_tool_boundary(tmp_path):
    """The realistic Claude Code pattern: text → tool → text → tool → text.
    Each text block should fire as soon as it lands."""
    from lib.transcript_watcher import TranscriptWatcher
    tx = tmp_path / "t.jsonl"
    received: list[str] = []
    w = TranscriptWatcher(tx, on_text=received.append)

    # Turn starts.
    _append(tx, _user("do the thing"))
    w.tick()
    assert received == []

    # First text block (mid-turn).
    _append(tx, _assistant_text("Let me check the build."))
    w.tick()
    assert received == ["Let me check the build."]

    # Tool call between text blocks.
    _append(tx, _tool_use("Bash"))
    w.tick()
    assert received == ["Let me check the build."]  # no new text

    # Second text block.
    _append(tx, _assistant_text("Build is green, all tests pass."))
    w.tick()
    assert received == ["Let me check the build.",
                        "Build is green, all tests pass."]


# ---- Filtering ----------------------------------------------------------


def test_watcher_skips_thinking_blocks(tmp_path):
    """Internal thinking content is private — must NOT be voiced."""
    from lib.transcript_watcher import TranscriptWatcher
    tx = tmp_path / "t.jsonl"
    _append(tx, _thinking(), _assistant_text("Said out loud."))
    received: list[str] = []
    w = TranscriptWatcher(tx, on_text=received.append)
    w.tick()
    assert received == ["Said out loud."]


def test_watcher_skips_user_entries(tmp_path):
    from lib.transcript_watcher import TranscriptWatcher
    tx = tmp_path / "t.jsonl"
    _append(tx, _user("user prompt"), _user("another prompt"))
    received: list[str] = []
    w = TranscriptWatcher(tx, on_text=received.append)
    w.tick()
    assert received == []


def test_watcher_handles_assistant_with_string_content(tmp_path):
    """Some Claude Code versions write content as a plain string, not a list."""
    from lib.transcript_watcher import TranscriptWatcher
    tx = tmp_path / "t.jsonl"
    _append(tx, {"type": "assistant",
                 "message": {"content": "plain string body"}})
    received: list[str] = []
    w = TranscriptWatcher(tx, on_text=received.append)
    w.tick()
    assert received == ["plain string body"]


def test_watcher_handles_multiple_text_chunks_in_one_entry(tmp_path):
    """Rare but legal: an assistant entry with multiple text items."""
    from lib.transcript_watcher import TranscriptWatcher
    tx = tmp_path / "t.jsonl"
    _append(tx, {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "first half"},
        {"type": "text", "text": "second half"},
    ]}})
    received: list[str] = []
    w = TranscriptWatcher(tx, on_text=received.append)
    w.tick()
    assert received == ["first half", "second half"]


# ---- Resilience ---------------------------------------------------------


def test_watcher_survives_partial_line_writes(tmp_path):
    """A JSONL line could be half-written when we read it — must not crash
    AND must consume the rest on the next tick when the line completes."""
    from lib.transcript_watcher import TranscriptWatcher
    tx = tmp_path / "t.jsonl"
    received: list[str] = []
    w = TranscriptWatcher(tx, on_text=received.append)
    # Write half a line — no newline yet.
    with tx.open("a") as f:
        f.write('{"type": "assistant", "message": {"content": [{"type": "te')
    w.tick()
    assert received == []
    # Complete the line.
    with tx.open("a") as f:
        f.write('xt", "text": "completed"}]}}\n')
    w.tick()
    assert received == ["completed"]


def test_watcher_skips_malformed_json_lines(tmp_path):
    from lib.transcript_watcher import TranscriptWatcher
    tx = tmp_path / "t.jsonl"
    with tx.open("a") as f:
        f.write("garbage not json\n")
    _append(tx, _assistant_text("after garbage"))
    received: list[str] = []
    w = TranscriptWatcher(tx, on_text=received.append)
    w.tick()
    assert received == ["after garbage"]


def test_watcher_handles_missing_file_gracefully(tmp_path):
    """File may not exist yet at watcher construction time — that's fine,
    tick() should be a no-op until it appears."""
    from lib.transcript_watcher import TranscriptWatcher
    nonexistent = tmp_path / "not-yet.jsonl"
    received: list[str] = []
    w = TranscriptWatcher(nonexistent, on_text=received.append)
    w.tick()       # must not raise
    assert received == []
    _append(nonexistent, _assistant_text("now it exists"))
    w.tick()
    assert received == ["now it exists"]


def test_watcher_skips_empty_text_blocks(tmp_path):
    from lib.transcript_watcher import TranscriptWatcher
    tx = tmp_path / "t.jsonl"
    _append(tx, _assistant_text(""), _assistant_text("   "),
            _assistant_text("real text"))
    received: list[str] = []
    w = TranscriptWatcher(tx, on_text=received.append)
    w.tick()
    assert received == ["real text"]


# ---- Resume / persistence -----------------------------------------------


def test_watcher_resumes_from_persisted_offset(tmp_path):
    """Production scenario: server restarts mid-turn. Watcher should not
    re-emit entries it already processed before the restart."""
    from lib.transcript_watcher import TranscriptWatcher
    tx = tmp_path / "t.jsonl"
    _append(tx, _assistant_text("first"))
    received1: list[str] = []
    w1 = TranscriptWatcher(tx, on_text=received1.append)
    w1.tick()
    assert received1 == ["first"]
    persisted_offset = w1.byte_offset

    # Simulate restart: new watcher constructed with the persisted offset,
    # then more text lands.
    _append(tx, _assistant_text("second"))
    received2: list[str] = []
    w2 = TranscriptWatcher(tx, on_text=received2.append,
                            start_offset=persisted_offset)
    w2.tick()
    assert received2 == ["second"]   # not "first" again


def test_watcher_seek_to_end_on_first_tick(tmp_path):
    """When `start_at_end=True`, the watcher ignores existing content on
    its first run — useful for a fresh agent picking up a transcript
    that already has historical content from before the watcher cared."""
    from lib.transcript_watcher import TranscriptWatcher
    tx = tmp_path / "t.jsonl"
    _append(tx, _assistant_text("historical 1"),
            _assistant_text("historical 2"))
    received: list[str] = []
    w = TranscriptWatcher(tx, on_text=received.append, start_at_end=True)
    w.tick()
    assert received == []
    _append(tx, _assistant_text("new content"))
    w.tick()
    assert received == ["new content"]


# ---- Multi-watcher coordination ----------------------------------------


def test_watcher_pool_distributes_to_correct_callback(tmp_path):
    """The production server runs one watcher per agent. WatcherPool keeps
    them straight: each tick, each agent's text goes only to its own
    callback."""
    from lib.transcript_watcher import TranscriptWatcher, WatcherPool
    tx_rachel = tmp_path / "rachel.jsonl"
    tx_mike   = tmp_path / "mike.jsonl"
    rachel: list[str] = []
    mike: list[str] = []
    pool = WatcherPool()
    pool.add("rachel-agent-id", TranscriptWatcher(
        tx_rachel, on_text=lambda t: rachel.append(t)))
    pool.add("mike-agent-id", TranscriptWatcher(
        tx_mike, on_text=lambda t: mike.append(t)))

    _append(tx_rachel, _assistant_text("Rachel says hi"))
    _append(tx_mike,   _assistant_text("Mike says hello"))
    pool.tick_all()

    assert rachel == ["Rachel says hi"]
    assert mike == ["Mike says hello"]


def test_watcher_pool_remove_stops_emitting(tmp_path):
    from lib.transcript_watcher import TranscriptWatcher, WatcherPool
    tx = tmp_path / "t.jsonl"
    received: list[str] = []
    pool = WatcherPool()
    pool.add("agent-1", TranscriptWatcher(tx, on_text=received.append))
    _append(tx, _assistant_text("first"))
    pool.tick_all()
    assert received == ["first"]
    pool.remove("agent-1")
    _append(tx, _assistant_text("after-removal"))
    pool.tick_all()
    assert received == ["first"]


def test_non_linux_dispatcher_polls_without_inotify(tmp_path, monkeypatch):
    import time
    from lib import transcript_watcher

    monkeypatch.setattr(transcript_watcher.sys, "platform", "darwin")
    tx = tmp_path / "poll.jsonl"
    received: list[str] = []
    pool = transcript_watcher.WatcherPool()
    pool.add("agent-1", transcript_watcher.TranscriptWatcher(
        tx, on_text=received.append))
    dispatcher = transcript_watcher.InotifyDispatcher(pool)
    dispatcher.watch("agent-1", tx)
    dispatcher.start()
    try:
        _append(tx, _assistant_text("macOS polling"))
        deadline = time.monotonic() + 1.5
        while not received and time.monotonic() < deadline:
            time.sleep(0.02)
        assert received == ["macOS polling"]
    finally:
        dispatcher.stop()


# ---- Marker (claimed-up-to-N) for hook coordination --------------------


def test_watcher_records_claimed_byte_offset_in_db(tmp_path, monkeypatch):
    """The hook needs to know what the watcher has already enqueued, so it
    doesn't double-speak the same text on Stop. We persist the watcher's
    byte_offset to the cursor_positions row keyed by backend_session_id.
    Hooks read that offset and skip earlier text.
    """
    from lib.transcript_watcher import TranscriptWatcher
    monkeypatch.setenv("CLAUDE_PWA_DB", str(tmp_path / "state.sqlite"))
    from lib import db
    db.reset_for_tests(tmp_path / "state.sqlite")
    tx = tmp_path / "t.jsonl"
    received: list[str] = []
    w = TranscriptWatcher(tx, on_text=received.append,
                          backend_session_id="cs-1",
                          persist_offset=True)
    _append(tx, _assistant_text("hello"))
    w.tick()
    # Persisted offset should equal the file size now.
    row = db.conn().execute(
        "SELECT position FROM cursor_positions WHERE backend_session_id = ?",
        ("cs-1",),
    ).fetchone()
    assert row is not None
    assert row["position"] == tx.stat().st_size


def test_watcher_can_hand_over_each_lines_own_timestamp(tmp_path):
    """The streamer reports how late it read a block (P3). The timestamp is the
    producer's, passed as written; the watcher neither parses nor trusts it."""
    from lib.transcript_watcher import TranscriptWatcher

    tx = tmp_path / "t.jsonl"
    tx.touch()
    received: list[tuple[str, object]] = []
    w = TranscriptWatcher(tx, on_text=lambda text, line_ts: received.append((text, line_ts)),
                          pass_line_timestamp=True)
    stamped = _assistant_text("first")
    stamped["timestamp"] = "2026-10-17T12:21:39.425Z"
    _append(tx, stamped, _assistant_text("second"))
    w.tick()
    assert received == [("first", "2026-10-17T12:21:39.425Z"), ("second", None)]


def test_a_non_object_json_line_does_not_discard_the_next_line(tmp_path):
    """Behaviour change (P3, 2d3ee12d): a valid JSON line that is not an object is
    skipped. Before, `entry.get` raised inside the tick and the following lines
    read in that tick were never voiced."""
    from lib.transcript_watcher import TranscriptWatcher

    tx = tmp_path / "t.jsonl"
    tx.touch()
    received: list[str] = []
    w = TranscriptWatcher(tx, on_text=received.append)
    with tx.open("a") as f:
        f.write("[1, 2]\n")
        f.write(json.dumps(_assistant_text("after the list")) + "\n")
    w.tick()
    assert received == ["after the list"]


def test_watcher_suppresses_synthetic_voice_text_but_advances_to_genuine_record(tmp_path):
    from lib.transcript_watcher import TranscriptWatcher
    path = tmp_path / 'synthetic.jsonl'
    path.touch()
    received = []
    watcher = TranscriptWatcher(path, on_text=received.append)
    synthetic = _assistant_text('<speak>SDK synthetic content</speak>')
    synthetic['message']['model'] = '<synthetic>'
    real = _assistant_text('<speak>Genuine spoken output</speak>')
    real['message']['model'] = 'claude-genuine'
    _append(path, synthetic, real)
    watcher.tick()
    watcher.tick()
    assert received == ['<speak>Genuine spoken output</speak>']
    assert watcher.byte_offset == path.stat().st_size


def test_concurrent_ticks_deliver_each_line_once_and_in_order(tmp_path, monkeypatch):
    import threading
    from lib.transcript_watcher import TranscriptWatcher

    path = tmp_path / 'concurrent.jsonl'
    _append(path, _assistant_text('first'), _assistant_text('second'))
    first_read, second_attempt, second_read = (threading.Event() for _ in range(3))
    seen, errors = [], []
    real_open = pathlib.Path.open

    class ReadWindow:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            self.stream.__enter__()
            return self
        def __exit__(self, *args):
            return self.stream.__exit__(*args)
        def seek(self, *args):
            return self.stream.seek(*args)
        def read(self, *args):
            if threading.current_thread().name == 'first-tick':
                first_read.set()
                assert second_attempt.wait(5)
                # Without serialization both readers have already selected
                # byte offset zero. With it the second read cannot enter yet.
                second_read.wait(1)
            else:
                second_read.set()
            return self.stream.read(*args)

    def open_window(selected, mode='r', *args, **kwargs):
        stream = real_open(selected, mode, *args, **kwargs)
        return ReadWindow(stream) if selected == path and mode == 'rb' else stream

    monkeypatch.setattr(pathlib.Path, 'open', open_window)
    watcher = TranscriptWatcher(path, on_text=seen.append)

    def tick(second=False):
        if second:
            second_attempt.set()
        try:
            watcher.tick()
        except BaseException as exc:
            errors.append(exc)

    first = threading.Thread(target=tick, name='first-tick')
    second = threading.Thread(target=lambda: tick(True), name='second-tick')
    first.start()
    try:
        assert first_read.wait(5)
        second.start()
    finally:
        second_attempt.set()
        first.join(5)
        if second.ident is not None:
            second.join(5)
    assert not first.is_alive() and not second.is_alive()
    assert not errors
    assert seen == ['first', 'second']


def test_slow_watcher_callback_does_not_block_another_watcher(tmp_path):
    import threading
    from lib.transcript_watcher import TranscriptWatcher

    a, b = tmp_path / 'a.jsonl', tmp_path / 'b.jsonl'
    _append(a, _assistant_text('alpha'))
    _append(b, _assistant_text('beta'))
    entered, release, b_done = (threading.Event() for _ in range(3))
    seen, errors = [], []

    def slow(text):
        entered.set()
        assert release.wait(10)
        seen.append(text)

    def run(watcher, done=None):
        try:
            watcher.tick()
        except BaseException as exc:
            errors.append(exc)
        finally:
            if done:
                done.set()

    first = threading.Thread(target=run, args=(TranscriptWatcher(a, on_text=slow),))
    second = threading.Thread(target=run, args=(TranscriptWatcher(b, on_text=seen.append), b_done))
    first.start()
    try:
        assert entered.wait(5)
        second.start()
        assert b_done.wait(2), 'watcher B progresses while watcher A holds its callback'
        assert seen == ['beta']
    finally:
        release.set()
        first.join(5)
        if second.ident is not None:
            second.join(5)
    assert not first.is_alive() and not second.is_alive()
    assert not errors
    assert seen == ['beta', 'alpha']


def test_reentrant_tick_from_callback_does_not_deadlock_or_duplicate(tmp_path):
    import threading
    from lib.transcript_watcher import TranscriptWatcher

    path = tmp_path / 'reentrant.jsonl'
    _append(path, _assistant_text('first'), _assistant_text('second'))
    seen, inner, errors = [], [], []

    def callback(text):
        seen.append(text)
        inner.append(watcher.tick())

    watcher = TranscriptWatcher(path, on_text=callback)
    def tick():
        try:
            watcher.tick()
        except BaseException as exc:
            errors.append(exc)
    worker = threading.Thread(target=tick, daemon=True)
    worker.start()
    worker.join(5)
    assert not worker.is_alive(), 'same-thread callback re-entry must not deadlock'
    assert not errors
    assert seen == ['first', 'second'] and inner == [0, 0]


def test_idle_retry_cannot_rewind_a_newer_catchup_cursor(tmp_path,monkeypatch):
    import sqlite3
    import threading
    from lib import db
    from lib.transcript_watcher import TranscriptWatcher
    path=tmp_path/'transcript.jsonl'
    def line(text):return json.dumps({'type':'assistant','message':{'content':[{'type':'text','text':text}]}})+'\n'
    path.write_text(line('first'))
    real_conn=db.conn;fail_first=[True]
    old_ready=threading.Event();release_old=threading.Event();new_done=threading.Event();errors=[];seen=[]
    class Gate:
        def __init__(self,con):self.con=con
        def __getattr__(self,name):return getattr(self.con,name)
        def execute(self,sql,parameters=()):
            if 'INSERT INTO cursor_positions' in ' '.join(str(sql).split()):
                if fail_first[0]:
                    fail_first[0]=False
                    raise sqlite3.OperationalError('synthetic database is locked')
                if threading.current_thread().name=='review-idle-retry':
                    old_ready.set()
                    assert release_old.wait(5)
            return self.con.execute(sql,parameters)
    monkeypatch.setattr(db,'conn',lambda:Gate(real_conn()))
    watcher=TranscriptWatcher(path,on_text=seen.append,backend_session_id='review-session',persist_offset=True)
    assert watcher.tick()==1 and watcher.save_pending
    with path.open('a') as stream:stream.write(line('second'))
    def run(method,done=None):
        try:method()
        except BaseException as exc:errors.append(type(exc).__name__)
        finally:
            if done:done.set()
    retry=threading.Thread(target=lambda:run(watcher.retry_save),name='review-idle-retry')
    tick=threading.Thread(target=lambda:run(watcher.tick,new_done),name='review-bind-catchup')
    retry.start()
    try:
        assert old_ready.wait(5)
        # Decide before starting the tick: a repaired retry may own the same
        # lock, in which case release its DB gate before waiting for the tick.
        lock=getattr(watcher,'_tick_lock',None)
        free=lock is None or lock.acquire(blocking=False)
        if free and lock is not None:lock.release()
        tick.start()
        if free:assert new_done.wait(5)
    finally:
        release_old.set()
        retry.join(5)
        if tick.ident is not None:tick.join(5)
    assert not retry.is_alive() and not tick.is_alive() and errors==[]
    assert seen==['first','second']
    stored=real_conn().execute('SELECT position FROM cursor_positions WHERE backend_session_id=?',('review-session',)).fetchone()[0]
    assert stored==watcher.byte_offset==path.stat().st_size, 'an older idle retry must not overwrite the cursor saved before catchup delivery'
    assert not watcher.save_pending
