#!/usr/bin/env python3
"""The conversations Antigravity kept on a machine, as the Python Host lists them for whoever
makes an agent out of one (`server/lib/agy_transcript.py`, `list_sessions`, and
`find_latest_jsonl` for the transcript of one): the lists its own function gives for made-up
stores of agy (its catalogue of conversations `conversation_summaries.db`, the conversations
it still has under `conversations/`, their transcripts under `brain/`, and the older file that
names the last conversation of each directory), written to
`crates/clarp-backend/tests/fixtures/agy-sessions-vectors.json` with the files and the rows it
was given, so that the Rust Host's list is held to it.

It runs in a process of its own whose every home, data, config and cache directory is a new empty
directory (the replay harness's own isolation, `tests/replay/host.py`), with no way out to any
network.

    nice "$(sed -n 1p ~/.local/share/clarp/current/SERVICE_PYTHON)" server-rs/tools/agy-sessions-vectors.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = HERE.parent / 'crates' / 'clarp-backend' / 'tests' / 'fixtures' / 'agy-sessions-vectors.json'

T0 = 1_800_000_000


def asked(step, text):
    return json.dumps({'step_index': step, 'type': 'USER_INPUT', 'status': 'DONE',
                       'content': f'<USER_REQUEST>\n{text}\n</USER_REQUEST>'})


def said(step, text):
    return json.dumps({'step_index': step, 'type': 'PLANNER_RESPONSE', 'status': 'DONE', 'content': text})


def transcript(conversation):
    return f'brain/{conversation}/.system_generated/logs/transcript.jsonl'


LONG = 'P' * 300

# A store of agy: its files (where each lies, what it holds, when it was last written; a
# conversation agy still has is an empty file under `conversations/`), and the rows of its
# catalogue (None: the store has no catalogue; a text: a file that is no database).
ALPHA = 'file:///srv/alpha'
ROWS = [
    # conversation_id, title, preview, workspace_uris, nesting_depth, last_modified_time
    ('c-first', 'The first of alpha', 'What it began with', json.dumps([ALPHA]), 0, '2027-03-05 10:00:00+00:00'),
    ('c-newest', 'The newest of alpha', LONG, json.dumps([ALPHA]), 0, '2027-03-09 10:00:00+00:00'),
    ('c-helper', 'A helper within another', 'nested', json.dumps([ALPHA]), 1, '2027-03-10 10:00:00+00:00'),
    ('c-beta', None, None, json.dumps(['file:///srv/beta']), 0, '2027-03-07 10:00:00+00:00'),
    ('c-scratch', 'One question of the Host', 'x', json.dumps(['file:///tmp/clarp-model-fallback-x']), 0,
     '2027-03-08 10:00:00+00:00'),
    ('c-dream', 'A dream', 'x', json.dumps(['file:///var/tmp/clarp-dream-worktrees/run', ALPHA]), 0,
     '2027-03-08 11:00:00+00:00'),
    ('c-not-clarp', 'Under tmp, and not the Host\'s', 'x', json.dumps(['file:///tmp/other/clarp-x']), 0,
     '2027-03-04 09:00:00+00:00'),
    ('c-let-go', 'agy has it no more', 'x', json.dumps([ALPHA]), 0, '2027-03-06 10:00:00+00:00'),
    ('c-no-transcript', 'Nothing was written of it', 'x', json.dumps([ALPHA]), 0, '2027-03-06 11:00:00+00:00'),
    ('c-nowhere', 'Its directories are no JSON', 'x', 'not JSON', 0, '2027-03-06 12:00:00+00:00'),
    ('c-empty', 'It has no directory', 'x', '', 0, '2027-03-03 12:00:00+00:00'),
    ('c-spaced', 'A directory with a space', 'x', json.dumps(['file:///srv/with%20space/and%C3%A9']), 0,
     '2027-03-02 12:00:00+00:00'),
    ('c-two', 'Two directories', 'x', json.dumps(['file:///srv/gamma', ALPHA]), 0, '2027-03-01 12:00:00+00:00'),
    ('c-remote', 'A directory elsewhere, and things that are none',
     'x', json.dumps(['vscode-remote://host/srv/alpha', 7, None, 'file://host/srv/delta?x=1#frag']), 0,
     '2027-02-28 12:00:00+00:00'),
    ('c-list', 'Its directories are no list', 'x', json.dumps({'file:///srv/alpha': 1}), 0, '2027-02-27 12:00:00+00:00'),
    (7, 'A name that is a number', 'x', json.dumps([ALPHA]), 0, '2027-02-26 12:00:00+00:00'),
]
HELD = ['c-first', 'c-newest', 'c-helper', 'c-beta', 'c-scratch', 'c-dream', 'c-not-clarp', 'c-no-transcript',
        'c-nowhere', 'c-empty', 'c-spaced', 'c-two', 'c-remote', 'c-list', '7']
WRITTEN = ['c-first', 'c-newest', 'c-helper', 'c-beta', 'c-scratch', 'c-dream', 'c-not-clarp', 'c-let-go',
           'c-nowhere', 'c-empty', 'c-spaced', 'c-two', 'c-remote', 'c-list', '7']
LAST = {'/srv/alpha': 'c-first', '/srv/beta': 'c-beta', '/srv/none': '', '/srv/gone': 'c-nobody',
        '/srv/let-go': 'c-let-go', '/srv/silent': 'c-silent', '/srv/number': 7}


def files(catalogued):
    out = [(f'conversations/{name}.db', '', T0) for name in HELD]
    for number, name in enumerate(WRITTEN):
        lines = [asked(0, f'What {name} began with ' + (LONG if name == 'c-newest' else '')), said(1, 'An answer.')]
        out.append((transcript(name), '\n'.join(lines) + '\n', T0 + 100 * (number + 1)))
    # A transcript in which the user said nothing has nothing to show of itself.
    out.append((transcript('c-silent'), said(0, 'Only the agent spoke.') + '\n', T0 + 5))
    out.append(('cache/last_conversations.json', json.dumps(LAST), T0))
    if isinstance(catalogued, str):
        out.append(('conversation_summaries.db', catalogued, T0))
    return out


STORES = [
    ('catalogue', 'a store with agy\'s own catalogue of its conversations', ROWS, 'cache'),
    ('older', 'a store of an agy that kept no catalogue: the last conversation of each directory', None, 'cache'),
    ('broken', 'a catalogue that is no database: the older file is read', 'this is no database\n' * 40, 'cache'),
    ('no-cache', 'no catalogue, and the older file is no JSON', None, 'not JSON'),
    ('a-list', 'no catalogue, and the older file is a list', None, json.dumps(['c-first'])),
    ('bare', 'no catalogue and no older file', None, None),
]

# What is asked of each store: a directory ('' for every directory) and how many at most.
ASKED = [('/srv/alpha', 20), ('', 100), ('/srv/beta', 20), ('/srv/alpha', 1), ('', 3), ('/srv/alpha/', 20),
         ('/srv/with space/andé', 20), ('/srv/gamma', 20), ('/srv/delta', 20), ('/srv/none', 20),
         ('/srv/gone', 20), ('/srv/let-go', 20), ('/srv/silent', 20), ('/srv/number', 20), ('/nowhere', 20),
         ('/tmp/other/clarp-x', 20), ('', 0)]

# The transcript of a conversation, by its name.
FOUND = ['c-first', 'c-let-go', 'c-no-transcript', 'c-nobody', '', '7']


def run() -> dict:
    """Runs in a process of its own: the lists the Python Host gives for the stores above."""
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    root = pathlib.Path(tempfile.mkdtemp(prefix='clarp-agy-sessions-vectors-', dir='/var/tmp'))
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    from lib import agy_transcript

    stores = []
    for name, what, catalogued, cache in STORES:
        base = root / name
        written = [row for row in files(catalogued) if row[0] != 'cache/last_conversations.json' or cache == 'cache']
        if cache not in ('cache', None):
            written.append(('cache/last_conversations.json', cache, T0))
        for path, text, at in written:
            file = base / path
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(text)
            os.utime(file, (at, at))
        if isinstance(catalogued, list):
            with sqlite3.connect(base / 'conversation_summaries.db') as db:
                db.execute('CREATE TABLE conversation_summaries (conversation_id, title text, preview text, '
                           'step_count integer NOT NULL DEFAULT 0, last_modified_time datetime NOT NULL, '
                           'workspace_uris text NOT NULL, nesting_depth integer NOT NULL DEFAULT 0)')
                for conversation, title, preview, uris, depth, modified in catalogued:
                    db.execute('INSERT INTO conversation_summaries (conversation_id, title, preview, workspace_uris, '
                               'nesting_depth, last_modified_time) VALUES (?, ?, ?, ?, ?, ?)',
                               (conversation, title, preview, uris, depth, modified))
        os.environ['CLAUDE_PWA_AGY_HOME'] = str(base)
        answers = [{'cwd': cwd, 'limit': limit, 'sessions': agy_transcript.list_sessions(cwd, limit)}
                   for cwd, limit in ASKED]
        found = []
        for conversation in FOUND:
            path = agy_transcript.find_latest_jsonl(conversation)
            found.append({'conversation': conversation, 'path': str(path.relative_to(base)) if path else None,
                          'kept': agy_transcript.conversation_exists(conversation)})
        rows = None if not isinstance(catalogued, list) else [
            {'conversation_id': row[0], 'title': row[1], 'preview': row[2], 'workspace_uris': row[3],
             'nesting_depth': row[4], 'last_modified_time': row[5]} for row in catalogued]
        stores.append({'name': name, 'what': what,
                       'files': [{'path': path, 'text': text, 'mtime': at} for path, text, at in written],
                       'catalogue': rows, 'asked': answers, 'found': found})
    return {'stores': stores}


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == '--run':
        print(json.dumps(run()))
        return 0
    ran = subprocess.run([sys.executable, '-I', __file__, '--run'], capture_output=True, text=True,
                         env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}, check=False)
    if ran.returncode != 0:
        print(ran.stderr[-5000:], file=sys.stderr)
        return 1
    told = json.loads(ran.stdout.splitlines()[-1])
    out = ['{', ' "stores": [']
    stores = told['stores']
    for number, store in enumerate(stores):
        out.append('  {"name": ' + json.dumps(store['name']) + ', "what": ' + json.dumps(store['what']) + ',')
        out.append('   "catalogue": ' + ('null,' if store['catalogue'] is None else '['))
        if store['catalogue'] is not None:
            out.extend('    ' + json.dumps(row, ensure_ascii=True) + (',' if n < len(store['catalogue']) - 1 else '')
                       for n, row in enumerate(store['catalogue']))
            out.append('   ],')
        for key in ('files', 'asked', 'found'):
            out.append(f'   "{key}": [')
            out.extend('    ' + json.dumps(row, ensure_ascii=True) + (',' if n < len(store[key]) - 1 else '')
                       for n, row in enumerate(store[key]))
            out.append('   ]' + (',' if key != 'found' else ''))
        out.append('  }' + (',' if number < len(stores) - 1 else ''))
    out.append(' ]')
    out.append('}')
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text('\n'.join(out) + '\n')
    json.loads(OUT.read_text())
    for store in stores:
        print(f'{store["name"]}: ' + '; '.join(
            f'{row["cwd"] or "-"}/{row["limit"]}: {[s["id"] for s in row["sessions"]]}' for row in store['asked'][:6]))
    print(f'{OUT.relative_to(REPO)}: {len(stores)} stores, '
          f'{sum(len(store["asked"]) for store in stores)} lists, '
          f'{sum(len(row["sessions"]) for store in stores for row in store["asked"])} sessions listed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
