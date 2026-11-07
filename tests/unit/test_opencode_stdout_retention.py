"""Finite synthetic stdout streams; no provider, listener or command content."""
import queue
import threading
import weakref
from types import SimpleNamespace

import pytest

from lib.backend import opencode_serve as serve

URL = 'http://127.0.0.1:12345'
ANNOUNCE = f'listening on {URL}/\n'


class Stream:
    def __init__(self, chunks):
        self.chunks = iter(chunks)
        self.pending = ''
        self.max_read = 0
        self.max_concat = 0
        self.reads = 0
    def __iter__(self):
        return self
    def __next__(self):
        line = self.readline()
        if not line:
            raise StopIteration
        return line
    def readline(self, size=-1):
        pieces, count = [], 0
        while size < 0 or count < size:
            if not self.pending:
                try:
                    self.pending = next(self.chunks)
                except StopIteration:
                    break
            newline = self.pending.find('\n')
            available = len(self.pending) if newline < 0 else newline + 1
            take = available if size < 0 else min(available, size - count)
            pieces.append(self.pending[:take])
            self.pending = self.pending[take:]
            count += take
            if pieces[-1].endswith('\n'):
                break
        owner = self
        class Text(str):
            def __add__(self, other):
                value = Text(str.__add__(self, other))
                owner.max_concat = max(owner.max_concat, len(value))
                return value
            def __radd__(self, other):
                value = Text(str.__add__(other, self))
                owner.max_concat = max(owner.max_concat, len(value))
                return value
            def __getitem__(self, key):
                return Text(str.__getitem__(self, key))
        line = Text(''.join(pieces))
        self.max_read = max(self.max_read, len(line))
        self.reads += 1
        return line


@pytest.fixture
def observe(monkeypatch):
    readers, roots, gates, timers = [], [], [], []
    timer_started = threading.Event()
    class ObservedQueue(queue.Queue):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            roots.append(weakref.ref(self))
    def reader(*a, **kw):
        thread = threading.Thread(*a, **kw)
        readers.append(thread)
        return thread
    class Timer:
        daemon = True
        def __init__(self, timeout, callback):
            self.callback = callback
            self.started = threading.Event()
            timers.append(self)
        def start(self):
            self.started.set()
            timer_started.set()
        def cancel(self):
            pass
    monkeypatch.setattr(serve, 'threading', SimpleNamespace(**{
        **vars(threading), 'Thread': reader, 'Timer': Timer}))
    monkeypatch.setattr(serve, 'queue', SimpleNamespace(Queue=ObservedQueue, Empty=queue.Empty))
    def gate():
        value = threading.Event()
        gates.append(value)
        return value
    def retained():
        count, size = 0, 0
        for ref in roots:
            q = ref()
            if q is not None:
                with q.mutex:
                    count += len(q.queue)
                    size += sum(len(item) for item in q.queue if isinstance(item, str))
        return count, size
    yield SimpleNamespace(readers=readers, gates=gates, timers=timers, gate=gate, retained=retained, timer_started=timer_started)
    for event in gates:
        event.set()
    # Drain any mutant's blocking notification slot so all stand-ins can end.
    for ref in roots:
        q = ref()
        if q is not None:
            while not q.empty():
                q.get_nowait()
    for thread in readers:
        thread.join(timeout=5)
        assert not thread.is_alive(), 'stdout producer failed to finish'


def proc(stream):
    return SimpleNamespace(stdout=stream, pid=123)


def test_post_url_burst_drains_without_retaining_lines(observe):
    release, consumed, eof = observe.gate(), threading.Event(), observe.gate()
    def chunks():
        yield ANNOUNCE
        assert release.wait(5)
        for i in range(256):
            yield f'{i:04d}' + 'x' * 4091 + '\n'
        consumed.set()
        assert eof.wait(5)
    stream = Stream(chunks())
    assert serve.wait_for_url(proc(stream)) == URL
    release.set()
    assert consumed.wait(5), 'bounded notification blocked the stdout producer'
    count, size = observe.retained()
    assert count <= 1 and size <= 8192, 'post-return stdout retained in abandoned queue'
    assert stream.max_read <= 4096


@pytest.mark.parametrize('chunks, expected', [
    (['noise\n', ANNOUNCE, 'listening on http://127.0.0.1:54321\n'], URL),
    ([ANNOUNCE], URL),
    ([ANNOUNCE.rstrip('\n')], URL),
    (['no url\n'], ''),
    ([], ''),
    (['listen', 'ing on http://127.', '0.0.1:12345/\n'], URL),
])
def test_startup_url_eof_noise_and_split_boundaries(observe, chunks, expected):
    assert serve.wait_for_url(proc(Stream(iter(chunks)))) == expected


@pytest.mark.parametrize('error', [OSError, ValueError])
def test_read_failure_returns_empty_and_reader_finishes(observe, error):
    def chunks():
        raise error('synthetic read failure')
        yield ''
    assert serve.wait_for_url(proc(Stream(chunks()))) == ''


def test_timeout_still_drains_a_late_url_and_burst(observe):
    release, consumed, eof = observe.gate(), threading.Event(), observe.gate()
    def chunks():
        assert release.wait(5)
        yield ANNOUNCE
        for _ in range(256):
            yield 'x' * 4095 + '\n'
        consumed.set()
        assert eof.wait(5)
    result = []
    owner = threading.Thread(target=lambda: result.append(serve.wait_for_url(proc(Stream(chunks())))))
    owner.start()
    try:
        # Timer construction/start are observable, not a sleep-derived timeout.
        # The reader is started first; wait for its first request via a gate.
        assert observe.timer_started.wait(5), 'startup timer did not arm'
        assert observe.timers[0].started.is_set()
        observe.timers[0].callback()
        owner.join(timeout=5)
        assert not owner.is_alive() and result == ['']
        release.set()
        assert consumed.wait(5)
        count, size = observe.retained()
        assert count <= 1 and size <= 8192
    finally:
        release.set()
        eof.set()
        owner.join(timeout=5)
        assert not owner.is_alive()


def test_newline_free_noise_has_bounded_reads_and_parse_storage(observe):
    def chunks():
        for _ in range(2048):
            yield 'x' * 4096
        yield 'listening on http://127.'
        yield '0.0.1:12345/\n'
    stream = Stream(chunks())
    assert serve.wait_for_url(proc(stream)) == URL
    assert stream.max_read <= 4096, 'newline-free stdout read was unbounded'
    assert stream.max_concat <= 32768, 'startup parse buffer grew without bound'


def test_two_readers_keep_urls_and_lifetimes_independent(observe):
    release_a, release_b = observe.gate(), observe.gate()
    done_a, done_b = threading.Event(), threading.Event()
    def chunks(url, release, done):
        yield f'listening on {url}/\n'
        assert release.wait(5)
        done.set()
    assert serve.wait_for_url(proc(Stream(chunks(URL, release_a, done_a)))) == URL
    second = 'http://127.0.0.1:54321'
    assert serve.wait_for_url(proc(Stream(chunks(second, release_b, done_b)))) == second
    release_b.set()
    assert done_b.wait(5) and not done_a.is_set()
    release_a.set()
    assert done_a.wait(5)


def test_oversized_url_is_ignored_not_truncated_and_later_announcement_works(observe):
    def chunks():
        yield 'listening on http://127.0.0.1/'
        for _ in range(4):
            yield 'x' * 4096
        yield '\n'
        yield ANNOUNCE
    assert serve.wait_for_url(proc(Stream(chunks()))) == URL
