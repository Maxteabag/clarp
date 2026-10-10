import json
from pathlib import Path

from lib.claude_transcript import context_tokens_from_jsonl


def test_unchanged_transcript_is_not_reopened_and_replacement_invalidates(tmp_path, monkeypatch):
    path = tmp_path / 'session.jsonl'
    def content(tokens):
        return json.dumps({'type': 'assistant', 'message': {'usage': {'input_tokens': tokens}}}) + '\n'
    path.write_text(content(11))
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
    path.write_text(content(33))
    assert context_tokens_from_jsonl(path) == 33


def test_a_fleet_larger_than_256_transcripts_is_answered_from_cache(tmp_path, monkeypatch):
    """One snapshot reads every bound agent's transcript in turn; the next
    snapshot must not reopen any of them while they are unchanged."""
    paths = []
    for i in range(600):
        path = tmp_path / f'session-{i}.jsonl'
        path.write_text(json.dumps({'type': 'assistant', 'message': {'usage': {'input_tokens': i}}}) + '\n')
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
