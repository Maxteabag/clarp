"""Paced live text (docs/live-items.md §3 pacing): the stored live row grows at
markdown-stable boundaries at most every interval, a burst always ends with
its last state (trailing flush), and the final text is never held back. The
transcript-updated wake-up is throttled the same way in both the HTTP and the
runtime process."""
from __future__ import annotations

from lib.audio_stream import AudioStream
from lib.live_pacing import LivePacer, TrailingThrottle, stable_prefix


class FakeClock:
    def __init__(self):
        self.now = 100.0
        self.timers: list[list] = []

    def __call__(self):
        return self.now

    def schedule(self, delay, fn):
        timer = [self.now + delay, fn, False]
        self.timers.append(timer)

        class _Handle:
            def cancel(self_inner):
                timer[2] = True
        return _Handle()

    def advance(self, seconds):
        self.now += seconds
        while True:
            due = [t for t in self.timers if not t[2] and t[0] <= self.now]
            if not due:
                return
            for timer in due:
                timer[2] = True
                timer[1]()


def test_stable_prefix_stops_at_closed_blocks():
    assert stable_prefix("Para one.\n\nPara tw") == "Para one.\n\n"
    assert stable_prefix("no boundary yet") == ""
    # Never inside an open code fence.
    assert stable_prefix("Intro.\n\n```py\nx = 1\n\ny = 2") == "Intro.\n\n"
    assert stable_prefix("```\ncode\n```\nafter") == "```\ncode\n```\n"
    # A new list item closes the previous one.
    assert stable_prefix("Steps:\n- one\n- tw") == "Steps:\n- one\n"


def _pacer(clock, writes, **kw):
    return LivePacer(writes.append, interval=0.25, hold=1.0,
                     clock=clock, schedule=clock.schedule, **kw)


def test_pacer_releases_whole_paragraphs_at_most_every_interval():
    clock, writes = FakeClock(), []
    pacer = _pacer(clock, writes)
    pacer.offer("First.\n\nSec")
    assert writes == ["First.\n\n"]
    clock.advance(0.1)
    pacer.offer("First.\n\nSecond one.\n\nThi")
    assert writes == ["First.\n\n"]
    clock.advance(0.2)
    assert writes == ["First.\n\n", "First.\n\nSecond one.\n\n"]


def test_a_long_paragraph_is_not_held_back_forever():
    clock, writes = FakeClock(), []
    pacer = _pacer(clock, writes)
    pacer.offer("A single long sentence that keeps go")
    assert writes == []
    clock.advance(1.0)
    assert writes == ["A single long sentence that keeps "]


def test_the_final_text_is_written_at_once_and_stops_the_timer():
    clock, writes = FakeClock(), []
    pacer = _pacer(clock, writes)
    pacer.offer("Hello wor")
    pacer.offer("Hello world.", final=True)
    assert writes == ["Hello world."]
    clock.advance(5)
    assert writes == ["Hello world."]


def _fire_due(clock, limit=50):
    """Fire due timers one at a time; how many fired (``limit`` = a loop)."""
    fired = 0
    while fired < limit:
        due = [t for t in clock.timers if not t[2] and t[0] <= clock.now]
        if not due:
            return fired
        due[0][2] = True
        due[0][1]()
        fired += 1
    return fired


def test_held_text_with_nothing_left_to_release_stops_the_timer():
    # "Done." has no line or word break, so once the hold has passed nothing
    # more can be released until new text arrives; the pacer must go quiet
    # instead of re-arming itself at zero delay forever (CI 2026-10-25: a
    # left-over pacer's timer loop slowed every later test on its worker).
    clock, writes = FakeClock(), []
    pacer = _pacer(clock, writes)
    pacer.offer("Done.")
    clock.now += 1.5
    assert _fire_due(clock) == 1
    assert [t for t in clock.timers if not t[2]] == []
    # Going quiet loses nothing: the final flush still writes the held text.
    assert writes == []
    pacer.flush()
    assert writes == ["Done."]


def test_after_going_quiet_a_later_offer_still_writes_the_held_text():
    clock, writes = FakeClock(), []
    pacer = _pacer(clock, writes)
    pacer.offer("Done.")
    clock.now += 1.5
    _fire_due(clock)
    pacer.offer("Done. More words")
    assert writes == ["Done. More "]
    pacer.offer("Done. More words.", final=True)
    assert writes == ["Done. More ", "Done. More words."]


def test_a_released_line_waits_one_hold_for_more_then_goes_quiet():
    clock, writes = FakeClock(), []
    pacer = _pacer(clock, writes)
    pacer.offer("A single long sentence that keeps go")
    clock.advance(1.0)
    assert writes == ["A single long sentence that keeps "]
    clock.now += 5
    assert _fire_due(clock) <= 1
    assert [t for t in clock.timers if not t[2]] == []
    assert writes == ["A single long sentence that keeps "]


def test_throttle_delivers_the_first_and_the_last_of_a_burst():
    clock, sent = FakeClock(), []
    throttle = TrailingThrottle(0.25, clock=clock, schedule=clock.schedule)
    for n in range(50):
        throttle.submit("arnold", n, sent.append)
    throttle.submit("yuki", "y", sent.append)
    assert sent == [0, "y"]
    clock.advance(0.25)
    assert sent == [0, "y", 49]
    clock.advance(1)
    assert sent == [0, "y", 49]


def test_audio_stream_never_drops_the_last_transcript_wake(tmp_path):
    clock = FakeClock()
    stream = AudioStream(tmp_path, transcript_event_min_interval_sec=0.25,
                         monotonic=clock, schedule=clock.schedule)
    q = stream.subscribe()
    for _ in range(20):
        stream.broadcast({"type": "transcript-updated", "session": "arnold"})
    assert q.qsize() == 1
    clock.advance(0.25)
    assert q.qsize() == 2


def test_runtime_process_throttles_transcript_wakes_before_storing_them():
    from lib import agents as agents_db
    from lib.runtime_events import RuntimeEventStream

    clock = FakeClock()
    stream = RuntimeEventStream(clock=clock, schedule=clock.schedule)
    before = len(agents_db.events_after(0))
    for _ in range(30):
        stream.broadcast({"type": "transcript-updated", "agent_id": "a", "session": "s"})
    assert len(agents_db.events_after(0)) - before == 1
    clock.advance(0.25)
    assert len(agents_db.events_after(0)) - before == 2


def test_codex_live_row_catches_up_after_a_burst_without_another_delta():
    import threading
    import time as _time

    from lib import agents as agents_db
    from lib import codex_app_server
    from lib.backend.codex import TurnState

    class _Handle:
        def __init__(self):
            self._done = threading.Event()

    agent_id = agents_db.create_agent(
        persona="Caleb", voice_id="v", cwd="/tmp", session="caleb", backend="codex")
    agents_db.open_turn(agent_id=agent_id, source="pwa", trace_id="trace-1")
    client = object.__new__(codex_app_server._Client)
    client.agent_id = agent_id
    client.active = codex_app_server._ActiveTurn(
        turn_id="turn-1", thread_id="thread-1", agent_id=agent_id, session="caleb",
        trace_id="trace-1", state=TurnState(live_backend_session_id="thread-1"),
        handle=_Handle(), on_result=None, on_error=None, stream=None,
        enqueue=lambda **_kwargs: 0)

    def live_text():
        row = agents_db.conn().execute(
            "SELECT text FROM messages WHERE agent_id = ? AND source_file LIKE 'live:%'",
            (agent_id,)).fetchone()
        return row["text"] if row else ""

    client._notification("item/agentMessage/delta", {"delta": "Hello.\n\nWor"})
    client._notification("item/agentMessage/delta", {"delta": "ld.\n\nMore"})
    assert "World." not in live_text()
    deadline = _time.monotonic() + 2.0
    while "World." not in live_text() and _time.monotonic() < deadline:
        _time.sleep(0.05)
    assert "World." in live_text()
    assert "More" not in live_text()
