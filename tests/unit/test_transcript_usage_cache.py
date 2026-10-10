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
