#!/usr/bin/env python3
"""Writes down what the Python Host says a compaction the user asked for did (its PR 483, R148).

When a compaction ends, the agent's row says what it did, from numbers (`last_compaction`): `compacted`,
`not_compacted` or `unknown`, with the sizes before and after and the reason in words. The rule for Claude Code is
`decide_claude` in `server/lib/compaction.py`, the shape of what is said is its `outcome`, and Claude Code's own record
of a compaction is read by `compact_boundary_tokens` and `compact_boundary_since` in `server/lib/claude_transcript.py`.
They run here as they are, over every branch: the size before (none, nothing, a number), Claude Code's record (none,
one without a number, one with), whether there was a conversation to read it in, and how the wait ended (it settled,
it was stopped, it ran out of time, it only waited); the record read from lines of every shape; and the newest of
several records.

    <the Python Host's interpreter> server-rs/tools/compaction-outcome-vectors.py

It runs in a process of its own whose every home, data, config and cache directory and whose database
(`CLAUDE_PWA_DB`) is a new empty place of its own, from its start (the replay harness's own isolation after it), with
no way out of the machine. The same file at every run. Nothing is logged.
"""
from __future__ import annotations

import importlib.util
import itertools
import json
import os
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import scratch as tool_scratch

REPO = pathlib.Path(__file__).resolve().parents[2]
OUT = REPO / 'server-rs' / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'compaction-outcome-vectors.json'

BEFORE = (None, 0, 1, 41_250)
PROOF = (None, 0, 1, 30_729)
ENDINGS = ('settled', 'stopped', 'cap', 'waited')

# What Claude Code's record may carry as its size before (`compactMetadata.preTokens`), and what else a line may be.
PRE = {
    'a number': 30_729, 'nothing': 0, 'one': 1, 'a large number': 2 ** 53, 'below nothing': -1,
    'a fraction': 1.5, 'a whole number written as a fraction': 30_729.0, 'true': True, 'false': False,
    'a text': '30729', 'null': None, 'a list': [30_729], 'an object': {'n': 30_729},
}


def record(**metadata) -> dict:
    return {'type': 'system', 'subtype': 'compact_boundary', 'compactMetadata': metadata}


def lines() -> list:
    """Each a line of a conversation as Claude Code writes it, and what it is."""
    found = [[f'its size before is {name}', json.dumps(record(preTokens=value))] for name, value in PRE.items()]
    found += [
        ['a record without a size', json.dumps(record())],
        ['a record whose metadata is no object', json.dumps({**record(), 'compactMetadata': [1]})],
        ['a record without metadata', json.dumps({'type': 'system', 'subtype': 'compact_boundary'})],
        ['a record with metadata null', json.dumps({**record(), 'compactMetadata': None})],
        ['the size after only', json.dumps(record(postTokens=8_957))],
        ['another kind of line', json.dumps({'type': 'system', 'subtype': 'init', 'note': 'compact_boundary'})],
        ['a line of the user', json.dumps({'type': 'user', 'subtype': 'compact_boundary'})],
        ['no line of JSON', '{"subtype": "compact_boundary"'],
        ['a list', json.dumps(['compact_boundary'])],
        ['an empty line', ''],
    ]
    return found


# Lines written after `/compact` was typed: the size of the newest record that has one, in order.
WRITTEN = {
    'nothing': [],
    'no record': [{'type': 'assistant'}, {'type': 'system', 'subtype': 'init'}],
    'one record': [{'type': 'assistant'}, record(preTokens=30_729, postTokens=8_957)],
    'two records: the newest counts': [record(preTokens=50_000), {'type': 'user'}, record(preTokens=30_729)],
    'a newer record without a size': [record(preTokens=30_729), record()],
    'an older record without a size': [record(), record(preTokens=30_729)],
}


@tool_scratch.case('clarp-compaction-outcome-vectors-')
def run(root) -> dict:
    """Runs in a process of its own: what the Python Host's functions say."""
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    # Whatever the Host's code asks of its store or its home is asked of this run's own.
    for key in ('CLAUDE_PWA_DB', 'HOME', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_CACHE_HOME', 'XDG_STATE_HOME'):
        assert os.environ[key].startswith(str(root) + os.sep), key
    sys.path.insert(0, str(REPO / 'server'))
    from lib import claude_transcript, compaction

    shapes = [
        {'kind': kind, 'before': before, 'after': after, 'after_is_estimate': estimate, 'detail': detail,
         'said': compaction.outcome(kind, before=before, after=after, after_is_estimate=estimate, detail=detail)}
        for kind in (compaction.COMPACTED, compaction.NOT_COMPACTED, compaction.UNKNOWN)
        for before, after, estimate, detail in ((None, None, False, ''), (41_250, 8_957, True, 'why'),
                                                (41_250, None, True, ''), (None, 8_957, False, 'why'))]
    decided = [
        {'before': before, 'proof': proof, 'transcript': transcript, 'ending': ending,
         'said': compaction.decide_claude(before, proof, transcript=transcript, ending=ending)}
        for before, proof, transcript, ending in itertools.product(BEFORE, PROOF, (True, False), ENDINGS)]
    read = [{'line': name, 'text': text, 'pre_tokens': claude_transcript.compact_boundary_tokens(text.encode())}
            for name, text in lines()]
    newest = []
    for name, written in WRITTEN.items():
        path = root / 'conversation.jsonl'
        path.write_text('{"type": "user"}\n' + ''.join(json.dumps(line) + '\n' for line in written))
        offset = len('{"type": "user"}\n')
        newest.append({'written': name, 'lines': written,
                       'pre_tokens': claude_transcript.compact_boundary_since(path, offset)})
    return {'what': 'server/lib/compaction.py (outcome, decide_claude), server/lib/claude_transcript.py '
                    '(compact_boundary_tokens, compact_boundary_since)',
            'outcomes': [compaction.COMPACTED, compaction.NOT_COMPACTED, compaction.UNKNOWN],
            'hard_cap_sec': compaction._HARD_CAP_SEC,
            'shapes': shapes, 'decided': decided, 'read': read, 'newest': newest}


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == '--run':
        print(json.dumps(run()))
        return 0
    # The process starts with a home and a store of its own too, before it sets its run's own.
    with tool_scratch.directory('clarp-compaction-outcome-home-') as place:
        own = {key: str(pathlib.Path(place) / name) for key, name in (
            ('HOME', 'home'), ('CLAUDE_PWA_DB', 'state.sqlite'), ('XDG_CONFIG_HOME', 'xdg-config'),
            ('XDG_DATA_HOME', 'xdg-data'), ('XDG_CACHE_HOME', 'xdg-cache'), ('XDG_STATE_HOME', 'xdg-state'))}
        env = tool_scratch.child_env({'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', **own})
        ran = subprocess.run([sys.executable, '-I', __file__, '--run'], capture_output=True, text=True,
                             env=env, check=False)
    if ran.returncode != 0:
        print(ran.stderr[-3000:], file=sys.stderr)
        return 1
    said = json.loads(ran.stdout.splitlines()[-1])
    lines_out = ['{']
    for name in ('what', 'outcomes', 'hard_cap_sec'):
        lines_out.append(f' {json.dumps(name)}: {json.dumps(said[name], ensure_ascii=True)},')
    lists = ('shapes', 'decided', 'read', 'newest')
    for at, name in enumerate(lists):
        lines_out.append(f' {json.dumps(name)}: [')
        lines_out.extend('  ' + json.dumps(case, ensure_ascii=True) + (',' if n < len(said[name]) - 1 else '')
                         for n, case in enumerate(said[name]))
        lines_out.append(' ]' + (',' if at < len(lists) - 1 else ''))
    lines_out.append('}')
    OUT.write_text('\n'.join(lines_out) + '\n', encoding='ascii')
    print(f'{OUT.relative_to(REPO)}: {len(said["shapes"])} shapes, {len(said["decided"])} decisions, '
          f'{len(said["read"])} lines read, {len(said["newest"])} conversations, {OUT.stat().st_size} bytes')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
