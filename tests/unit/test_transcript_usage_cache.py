import json
import os
import time
from pathlib import Path

import pytest

from lib.claude_transcript import context_tokens_from_jsonl


def _settled(path):
    """Backdate the transcript as if its last write was an hour ago."""
    then = time.time_ns() - 3600 * 10**9
    os.utime(path, ns=(then, then))


def test_unchanged_transcript_is_not_reopened_and_replacement_invalidates(tmp_path, monkeypatch):
    path = tmp_path / 'session.jsonl'
    def content(tokens):
        return json.dumps({'type': 'assistant', 'message': {'usage': {'input_tokens': tokens}}}) + '\n'
    path.write_text(content(11))
    _settled(path)
    calls = []
    original = Path.open
    def observe(self, *args, **kwargs):
        if self == path:
            calls.append(args)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', observe)
    assert context_tokens_from_jsonl(path) == 11
    assert context_tokens_from_jsonl(path) == 11
    assert len(calls) == 1
    replacement = tmp_path / 'replacement.jsonl'
    replacement.write_text(content(22))
    replacement.replace(path)
    assert context_tokens_from_jsonl(path) == 22
    # RM-N4: ext4 takes timestamps from a coarse clock, so a same-size rewrite
    # inside one tick keeps every stat field the cache keys on. Freeze the
    # stat to make that tick deterministic.
    frozen = path.stat()
    path.write_text(content(33))
    original_stat = Path.stat
    def coarse_clock(self, *args, **kwargs):
        return frozen if self == path else original_stat(self, *args, **kwargs)
    monkeypatch.setattr(Path, 'stat', coarse_clock)
    assert context_tokens_from_jsonl(path) == 33


def test_a_fleet_larger_than_256_transcripts_is_answered_from_cache(tmp_path, monkeypatch):
    """One snapshot reads every bound agent's transcript in turn; the next
    snapshot must not reopen any of them while they are unchanged."""
    paths = []
    for i in range(600):
        path = tmp_path / f'session-{i}.jsonl'
        path.write_text(json.dumps({'type': 'assistant', 'message': {'usage': {'input_tokens': i}}}) + '\n')
        _settled(path)
        paths.append(path)
    for path in paths:
        context_tokens_from_jsonl(path)
    opened = []
    original = Path.open
    def observe(self, *args, **kwargs):
        opened.append(self)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', observe)
    assert [context_tokens_from_jsonl(path) for path in paths] == list(range(600))
    assert opened == []


def _usage(tokens):
    return json.dumps({'type': 'assistant', 'message': {'usage': {'input_tokens': tokens}}}) + '\n'


def _filler(size):
    line = json.dumps({'type': 'user', 'message': {'content': 'x' * 200}}) + '\n'
    return line * (size // len(line) + 1)


def test_the_latest_usage_is_found_without_reading_the_whole_tail(tmp_path, monkeypatch):
    """A cold Host start asks once per agent; reading 1 MB each took 38 s."""
    path = tmp_path / 'long.jsonl'
    path.write_text(_filler(2_000_000) + _usage(5) + _filler(1000) + _usage(7) + _filler(2000))
    read = []
    original = Path.open

    class Counted:
        def __init__(self, handle):
            self.handle = handle
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            self.handle.close()
        def __getattr__(self, name):
            return getattr(self.handle, name)
        def read(self, *args):
            data = self.handle.read(*args)
            read.append(len(data))
            return data

    def observe(self, *args, **kwargs):
        return Counted(original(self, *args, **kwargs))
    monkeypatch.setattr(Path, 'open', observe)
    assert context_tokens_from_jsonl(path) == 7
    assert sum(read) <= 128 * 1024


def test_usage_further_back_is_still_found_within_the_tail(tmp_path):
    path = tmp_path / 'sparse.jsonl'
    path.write_text(_filler(100_000) + _usage(9) + _filler(300_000))
    assert context_tokens_from_jsonl(path) == 9
    beyond = tmp_path / 'beyond.jsonl'
    beyond.write_text(_usage(9) + _filler(1_200_000))
    assert context_tokens_from_jsonl(beyond) is None


def _synthetic_usage(marker, tokens=0):
    record = {'type':'assistant','message':{
        'role':'assistant','model':'claude-real',
        'usage':{'input_tokens':tokens,'cache_read_input_tokens':0,'cache_creation_input_tokens':0}}}
    if marker == 'model':
        record['message']['model'] = '<synthetic>'
    elif marker == 'api':
        record['isApiErrorMessage'] = True
    else:
        record['message']['isApiErrorMessage'] = True
    return json.dumps(record) + '\n'


@pytest.mark.parametrize('marker', ['model','api','nested-api'])
@pytest.mark.parametrize('cache_mode', ['settled','racy'])
@pytest.mark.parametrize('shape,expected', [
    ('one-failure',160), ('several-failures',160), ('only-failures',None),
    ('failure-before-real',23), ('after-compact',23), ('synthetic-nonzero',160),
    ('real-zero',0), ('tail-cuts-failure-run',None), ('widen-through-failures',160),
])
def test_context_usage_keeps_the_last_real_answer(tmp_path, monkeypatch, marker, cache_mode, shape, expected):
    from lib import claude_transcript as transcript

    failed = _synthetic_usage(marker)
    real = _usage(160)
    tail_bytes = 1_000_000
    if shape == 'one-failure':
        content = real + failed
    elif shape == 'several-failures':
        content = real + failed * 4
    elif shape == 'only-failures':
        content = failed * 3
    elif shape == 'failure-before-real':
        content = failed + _usage(23)
    elif shape == 'after-compact':
        compact = json.dumps({'type':'system','subtype':'compact_boundary',
                              'compactMetadata':{'trigger':'manual','preTokens':160}}) + '\n'
        content = real + compact + failed + _usage(23) + failed
    elif shape == 'synthetic-nonzero':
        content = real + _synthetic_usage(marker,777)
    elif shape == 'real-zero':
        content = _usage(0) + _synthetic_usage(marker,777)
    elif shape == 'tail-cuts-failure-run':
        content = real + failed * 6
        tail_bytes = len(failed.encode()) * 4 + len(failed.encode()) // 2
    else:
        content = real + failed * 700  # beyond the first 64 KiB read
        assert len(content.encode()) > 64 * 1024
    path = tmp_path / 'context.jsonl'
    path.write_text(content)
    stamp = path.stat().st_mtime_ns
    age = transcript._RACY_NS + 1 if cache_mode == 'settled' else 1
    monkeypatch.setattr(transcript.time, 'time_ns', lambda: stamp + age)
    transcript._context_tokens_cached.cache_clear()
    opened = []
    original = Path.open
    def observe(self, *args, **kwargs):
        if self == path:
            opened.append(args)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', observe)
    assert context_tokens_from_jsonl(path, tail_bytes=tail_bytes) == expected
    assert context_tokens_from_jsonl(path, tail_bytes=tail_bytes) == expected
    assert len(opened) == (1 if cache_mode == 'settled' else 2)
