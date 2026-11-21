"""A turn that printed is never judged silent because a line waits in a reader's buffer
(issue 491, part B; reviewer 4 on df7af42c).

A text stream reads ahead: while the drain handles one line (an init does database writes),
the next line, a tool call, can already be out of the pipe and in Python's buffer, where the
pipe's unread count does not see it and the drain has not counted it. ``takeover.read_lines``
reads from the pipe only once every line read so far is handled, and says when it waits;
``takeover.drained`` is true only then. The model: the real ``drained`` and ``note_output``
on a reader stalled on its first line, with the tool call written in the same burst.
"""
from __future__ import annotations

import json
import os
import threading
from types import SimpleNamespace

from lib import takeover
from lib.backend.claude import note_output

INIT = {"type": "system", "subtype": "init", "session_id": "s-1"}
TOOL = {"type": "assistant", "message": {"content": [
    {"type": "tool_use", "id": "tool-1", "name": "Bash", "input": {"command": "true"}}]}}


def _reader(stream, handle, stall, stalled, lines):
    for line in takeover.read_lines(stream, handle):
        ev = json.loads(line)
        note_output(handle, ev)
        lines.append(ev)
        if ev.get("subtype") == "init":
            stalled.set()
            stall.wait(5)                       # the init's database writes, slow


def _world():
    r, w = os.pipe()
    stream = os.fdopen(r, "r")
    handle = SimpleNamespace(proc=SimpleNamespace(stdout=stream), lines_read=0,
                             output_seen=False)
    return r, w, stream, handle


def test_a_tool_call_read_ahead_while_the_reader_is_busy_is_never_judged_silent():
    _r, w, stream, handle = _world()
    stall, stalled, lines = threading.Event(), threading.Event(), []
    os.write(w, (json.dumps(INIT) + "\n" + json.dumps(TOOL) + "\n").encode())   # one burst
    thread = threading.Thread(target=_reader, args=(stream, handle, stall, stalled, lines),
                              daemon=True)
    thread.start()
    assert stalled.wait(5)
    # The pipe may be empty and the count still: the tool call is not handled yet.
    assert takeover.drained(handle, bound_s=0.3) is False
    assert handle.output_seen is False
    stall.set()
    assert takeover.drained(handle, bound_s=2.0) is True
    assert handle.output_seen is True and len(lines) == 2   # judged: it printed
    os.close(w)
    thread.join(5)


def test_a_reader_that_only_saw_its_init_is_drained_when_it_waits():
    _r, w, stream, handle = _world()
    stall, stalled, lines = threading.Event(), threading.Event(), []
    stall.set()
    os.write(w, (json.dumps(INIT) + "\n").encode())
    thread = threading.Thread(target=_reader, args=(stream, handle, stall, stalled, lines),
                              daemon=True)
    thread.start()
    assert takeover.drained(handle, bound_s=2.0) is True
    assert handle.output_seen is False and handle.lines_read == 1
    os.close(w)
    thread.join(5)


def test_a_half_line_kept_back_is_not_drained():
    """A line the CLI was frozen in the middle of is output not yet seen: never drained."""
    _r, w, stream, handle = _world()
    stall, stalled, lines = threading.Event(), threading.Event(), []
    stall.set()
    os.write(w, (json.dumps(INIT) + "\n" + json.dumps(TOOL)[:20]).encode())
    thread = threading.Thread(target=_reader, args=(stream, handle, stall, stalled, lines),
                              daemon=True)
    thread.start()
    assert takeover.drained(handle, bound_s=0.4) is False
    os.close(w)
    thread.join(5)


def test_lines_come_out_whole_across_reads_and_at_the_end():
    r, w = os.pipe()
    stream = os.fdopen(r, "r")
    handle = SimpleNamespace(lines_read=0)
    payload = "é" * 40000 + "\n" + "last without newline"
    writer = threading.Thread(target=lambda: (os.write(w, payload.encode()), os.close(w)))
    writer.start()
    got = list(takeover.read_lines(stream, handle))
    writer.join(5)
    assert got == ["é" * 40000 + "\n", "last without newline"]
    assert handle.reader_waiting is False


def test_a_stand_in_stream_without_a_descriptor_is_read_as_it_is():
    assert list(takeover.read_lines(iter(["a\n", "b\n"]))) == ["a\n", "b\n"]
