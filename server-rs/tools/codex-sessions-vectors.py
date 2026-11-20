#!/usr/bin/env python3
"""The sessions Codex kept on a machine, as the Python Host lists them for whoever makes an agent
out of one (`server/lib/codex_transcript.py`, `list_sessions`, and `find_latest_jsonl` for the
rollout of one): the lists its own function gives for a made-up directory of rollouts and a
made-up index of threads (the table of Codex's `state_5.sqlite` that function reads), written to
`crates/clarp-backend/tests/fixtures/codex-sessions-vectors.json` with the files and the rows it
was given, so that the Rust Host's list is held to it. And the model a session last ran on, as the
Python Host reads it off a rollout (`server/lib/session_models.py`, `_read_model`).

It runs in a process of its own whose every home, data, config and cache directory is a new empty
directory (the replay harness's own isolation, `tests/replay/host.py`), with no way out to any
network.

    nice "$(sed -n 1p ~/.local/share/clarp/current/SERVICE_PYTHON)" server-rs/tools/codex-sessions-vectors.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import sqlite3
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import scratch as tool_scratch

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = HERE.parent / 'crates' / 'clarp-backend' / 'tests' / 'fixtures' / 'codex-sessions-vectors.json'

T0 = 1_800_000_000
# Where the made-up directory of rollouts stands, in what the index says of its files.
ROOT = '{ROOT}'


def meta(session, cwd, **more):
    return json.dumps({'timestamp': 't', 'type': 'session_meta', 'payload': {'id': session, 'cwd': cwd, **more}})


def asked(text):
    return json.dumps({'timestamp': 't', 'type': 'event_msg', 'payload': {'type': 'user_message', 'message': text}})


def said(role, text):
    kind = 'input_text' if role == 'user' else 'output_text'
    return json.dumps({'timestamp': 't', 'type': 'response_item',
                       'payload': {'type': 'message', 'role': role, 'content': [{'type': kind, 'text': text}]}})


def context(model):
    return json.dumps({'timestamp': 't', 'type': 'turn_context', 'payload': {'cwd': '/srv/alpha', 'model': model}})


def rollout(day, session, *lines):
    return f'2027/02/{day:02d}/rollout-2027-02-{day:02d}T10-00-00-{session}.jsonl', '\n'.join(lines) + '\n'


# The rollouts: where each lies under the directory, what it holds, and when it was last written.
FILES = [
    (*rollout(1, 'a1', meta('a1', '/srv/alpha'), context('gpt-old'), asked('› First of alpha'), said('assistant', 'Done.'),
              context('gpt-new'), asked('second')), T0 + 10),
    (*rollout(2, 'a2', '', 'not json', '[1]', meta('a2', '/srv/alpha/'), said('user', 'only as a message of the model'),
              asked('   › ›  Asked with marks   ')), T0 + 20),
    (*rollout(3, 'b1', meta('b1', '/srv/beta'), asked('First of beta')), T0 + 30),
    (*rollout(4, 'a3', meta('a3', '/srv/alpha'), asked('The index names another file for this one')), T0 + 40),
    (*rollout(5, 'none', asked('no meta at all'), said('assistant', 'x')), T0 + 50),
    (*rollout(6, 'a4', json.dumps({'type': 'session_meta', 'id': 'a4', 'cwd': '/srv/alpha'}), asked('Meta on the envelope')), T0 + 60),
    (*rollout(7, 'a5', meta('a5', '/srv/alpha')), T0 + 70),
    (*rollout(8, 'a6', meta('a6', '/srv/alpha'), asked('long ' + 'p' * 300)), T0 + 80),
    (*rollout(9, 'late', *([said('assistant', 'filler')] * 80), meta('late', '/srv/alpha'), asked('meta after eighty lines')), T0 + 90),
    (*rollout(10, 'a7', meta('a7', '/srv/alpha'), asked(7), asked(''), asked('The first that says something')), T0 + 100),
    (*rollout(11, 'nocwd', meta('nocwd', ''), asked('no directory')), T0 + 110),
    (*rollout(12, 'a8', meta('a8', '/srv/alpha'), asked('In the index, and newer there')), T0 + 5),
    (*rollout(13, 'a9', meta('a9', '/srv/alpha'), asked('In the index with no time in thousandths')), T0 + 6),
    (*rollout(14, 'g1', meta('g1', '/srv/gamma'), asked('[voice-mode] speak\n\n--- user message ---\nKept as it is')), T0 + 120),
    ('2027/02/15/notes.txt', 'no rollout\n', T0 + 130),
    ('2027/02/15/rollout-copy-of-a1.jsonl', meta('a1', '/srv/alpha') + '\n' + asked('A second file of a1, newer') + '\n', T0 + 140),
    ('rollout-at-the-top-t1.jsonl', meta('t1', '/srv/alpha') + '\n' + asked('At the top') + '\n', T0 + 150),
]

# The index of threads: what Codex's own table says of each (the columns that function reads).
THREADS = [
    dict(id='a1', cwd='/srv/alpha', name='Named by its user', title='Titled by Codex', preview='Preview of a1',
         first_user_message='First of alpha', updated_at=T0 + 11, updated_at_ms=(T0 + 11) * 1000 + 250,
         rollout_path='{ROOT}/2027/02/01/rollout-2027-02-01T10-00-00-a1.jsonl', has_user_event=1,
         recency_at_ms=(T0 + 11) * 1000, model='gpt-indexed'),
    dict(id='b1', cwd='/srv/beta', name=None, title='', preview='', first_user_message='', updated_at=T0 + 31,
         updated_at_ms=(T0 + 31) * 1000, rollout_path='{ROOT}/2027/02/03/rollout-2027-02-03T10-00-00-b1.jsonl',
         has_user_event=0, recency_at_ms=(T0 + 31) * 1000, model=None),
    dict(id='a3', cwd='/srv/alpha', name=None, title='Points at a file that is gone', preview='', first_user_message='x',
         updated_at=T0 + 41, updated_at_ms=(T0 + 41) * 1000, rollout_path='{ROOT}/gone/rollout-a3.jsonl', has_user_event=1,
         recency_at_ms=(T0 + 41) * 1000, model=''),
    dict(id='a8', cwd='/srv/alpha', name='', title='T' * 200, preview='', first_user_message='F' * 300,
         updated_at=T0 + 200, updated_at_ms=(T0 + 200) * 1000 + 999,
         rollout_path='{ROOT}/2027/02/12/rollout-2027-02-12T10-00-00-a8.jsonl', has_user_event=1,
         recency_at_ms=(T0 + 1) * 1000, model='gpt-a8'),
    dict(id='a9', cwd='/srv/alpha', name=None, title='Seconds only', preview='', first_user_message='only first',
         updated_at=T0 + 190, updated_at_ms=None, rollout_path='{ROOT}/2027/02/13/rollout-2027-02-13T10-00-00-a9.jsonl',
         has_user_event=0, recency_at_ms=(T0 + 300) * 1000, model=None),
    dict(id='nofile', cwd='/srv/alpha', name=None, title='No path at all', preview='p', first_user_message='f',
         updated_at=T0 + 500, updated_at_ms=(T0 + 500) * 1000, rollout_path='', has_user_event=1,
         recency_at_ms=(T0 + 500) * 1000, model=None),
    dict(id='', cwd='/srv/alpha', name=None, title='No id', preview='p', first_user_message='f', updated_at=T0 + 400,
         updated_at_ms=(T0 + 400) * 1000, rollout_path='{ROOT}/2027/02/07/rollout-2027-02-07T10-00-00-a5.jsonl',
         has_user_event=1, recency_at_ms=(T0 + 400) * 1000, model=None),
    dict(id='g1', cwd='/srv/gamma/', name=None, title='Its directory is written with a slash at its end', preview='',
         first_user_message='x', updated_at=T0 + 121, updated_at_ms=(T0 + 121) * 1000,
         rollout_path='{ROOT}/2027/02/14/rollout-2027-02-14T10-00-00-g1.jsonl', has_user_event=1,
         recency_at_ms=(T0 + 121) * 1000, model=None),
]
COLUMNS = ['id', 'cwd', 'name', 'title', 'preview', 'first_user_message', 'updated_at', 'updated_at_ms', 'rollout_path',
           'has_user_event', 'recency_at_ms', 'model']

# What is asked: (the directory, every directory or one, how many at most, with the index or without).
ASKED = [
    ('/srv/alpha', False, 100, True), ('/srv/alpha', False, 3, True), ('/srv/alpha/', False, 100, True),
    ('/srv/alpha', False, 1, True), ('/srv/alpha', False, 0, True), ('/srv/beta', False, 100, True),
    ('/srv/gamma', False, 100, True), ('/srv/nothing', False, 100, True), ('', False, 100, True), ('', True, 100, True),
    ('/srv/alpha', True, 100, True), ('', True, 4, True), ('/srv/alpha', False, 100, False), ('', True, 100, False),
    ('/srv/alpha', False, 2, False), ('.', False, 100, False),
]

# The rollout of a session, by its name.
FOUND = ['a1', 'a2', 'b1', 'none', 'nobody', '', 't1', 'a', '1']


@tool_scratch.case('clarp-codex-sessions-vectors-')
def run(root) -> dict:
    """Runs in a process of its own: the lists the Python Host gives for the files and rows above."""
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    from lib import codex_transcript, session_models

    sessions = root / 'sessions'
    for name, text, written in FILES:
        path = sessions / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        os.utime(path, (written, written))
    index = root / 'state_5.sqlite'
    with sqlite3.connect(index) as db:
        db.execute('CREATE TABLE threads (id TEXT, cwd TEXT, name TEXT, title TEXT, preview TEXT, first_user_message TEXT, '
                   'updated_at INTEGER, updated_at_ms INTEGER, rollout_path TEXT, has_user_event INTEGER, '
                   'recency_at_ms INTEGER, model TEXT)')
        for row in THREADS:
            values = [row[column].replace(ROOT, str(sessions)) if column == 'rollout_path' else row[column] for column in COLUMNS]
            db.execute(f'INSERT INTO threads VALUES ({", ".join("?" * len(COLUMNS))})', values)
    no_index = root / 'no-such-index.sqlite'
    broken = root / 'broken.sqlite'
    broken.write_text('this is no database\n' * 40)

    def named(listed):
        return json.loads(json.dumps(listed).replace(str(sessions), ROOT))

    answers = []
    for cwd, everywhere, limit, with_index in ASKED:
        listed = codex_transcript.list_sessions(cwd, limit, sessions_root=sessions, all_projects=everywhere,
                                                state_db=index if with_index else no_index)
        answers.append({'cwd': cwd, 'all_projects': everywhere, 'limit': limit, 'index': with_index, 'sessions': named(listed)})
    none = codex_transcript.list_sessions('/srv/alpha', 100, sessions_root=root / 'no-such-directory', state_db=index)
    with_broken = codex_transcript.list_sessions('/srv/alpha', 100, sessions_root=sessions, state_db=broken)
    found = []
    for session in FOUND:
        path = codex_transcript.find_latest_jsonl(session, sessions_root=sessions)
        found.append({'session': session, 'path': str(path.relative_to(sessions)) if path else None})

    def model_of(path):
        stat = path.stat()
        return session_models._read_model(str(path), stat.st_mtime_ns, stat.st_size)

    models = [{'path': name, 'model': model_of(sessions / name)} for name, _, _ in FILES if name.endswith('.jsonl')]
    return {'files': [{'path': name, 'text': text, 'mtime': written} for name, text, written in FILES],
            'threads': THREADS, 'asked': answers, 'without_sessions': none, 'with_a_broken_index': named(with_broken),
            'found': found, 'models': models}


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == '--run':
        print(json.dumps(run()))
        return 0
    ran = subprocess.run([sys.executable, '-I', __file__, '--run'], capture_output=True, text=True,
                         env=tool_scratch.child_env({'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}), check=False)
    if ran.returncode != 0:
        print(ran.stderr[-5000:], file=sys.stderr)
        return 1
    told = json.loads(ran.stdout.splitlines()[-1])
    out = ['{']
    for key in ('files', 'threads', 'asked', 'found', 'models'):
        out.append(f' "{key}": [')
        out.extend('  ' + json.dumps(row, ensure_ascii=True) + (',' if n < len(told[key]) - 1 else '')
                   for n, row in enumerate(told[key]))
        out.append(' ],')
    out.append(' "with_a_broken_index": ' + json.dumps(told['with_a_broken_index']) + ',')
    out.append(' "without_sessions": ' + json.dumps(told['without_sessions']))
    out.append('}')
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text('\n'.join(out) + '\n')
    print(f'{OUT.relative_to(REPO)}: {len(told["files"])} files, {len(told["threads"])} threads, {len(told["asked"])} lists: '
          + '; '.join(f'{"all:" if row["all_projects"] else ""}{row["cwd"] or "-"}/{row["limit"]}'
                      f'{"" if row["index"] else " no index"}: {[s["id"] for s in row["sessions"]]}' for row in told['asked']))
    print('with a broken index:', [s['id'] for s in told['with_a_broken_index']], '| found:',
          {row['session']: row['path'] for row in told['found']})
    print('models:', {row['path'].rsplit('-', 1)[-1]: row['model'] for row in told['models']})
    return 0


if __name__ == '__main__':
    sys.exit(main())
