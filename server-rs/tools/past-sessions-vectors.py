#!/usr/bin/env python3
"""The sessions Claude Code kept on a machine, as the Python Host lists them for whoever makes an
agent out of one (`server/lib/session_catalog.py`, `list_claude_sessions`): the list its own function
gives for a made-up directory of projects, written to
`crates/clarp-backend/tests/fixtures/past-sessions-vectors.json` with the files it was given, so
that the Rust Host's list is held to it file by file.

It runs in a process of its own whose every home, data, config and cache directory is a new empty
directory (the replay harness's own isolation, `tests/replay/host.py`), with no way out to any
network.

    nice "$(sed -n 1p ~/.local/share/clarp/current/SERVICE_PYTHON)" server-rs/tools/past-sessions-vectors.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import scratch as tool_scratch

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = HERE.parent / 'crates' / 'clarp-backend' / 'tests' / 'fixtures' / 'past-sessions-vectors.json'

T0 = 1_800_000_000
# Where the made-up directory of projects stands, in what the files say of themselves.
ROOT = '{ROOT}'


def line(**event):
    return json.dumps(event)


def user(text, cwd='/srv/alpha', **more):
    return line(type='user', cwd=cwd, message={'role': 'user', 'content': text}, **more)


def said(text, cwd='/srv/alpha'):
    return line(type='assistant', cwd=cwd, message={'role': 'assistant', 'content': [{'type': 'text', 'text': text}]})


# The files: (path under the directory of projects, its text, when it was last written).
FILES = [
    # A project with an index: what the index says is taken, and its transcripts are not opened.
    ('-srv-alpha/sessions-index.json', json.dumps({'version': 1, 'entries': [
        {'sessionId': 'a-named', 'fullPath': ROOT + '/-srv-alpha/a-named.jsonl', 'fileMtime': (T0 + 900) * 1000,
         'firstPrompt': 'p' * 300, 'customTitle': 'A name its user gave', 'summary': 'A summary', 'projectPath': '/srv/alpha'},
        {'sessionId': 'a-summed', 'fileMtime': T0 + 800, 'firstPrompt': '  what was asked first  ', 'summary': 's' * 200,
         'projectPath': '/srv/alpha'},
        {'sessionId': 'a-side', 'fileMtime': T0 + 990, 'isSidechain': True, 'projectPath': '/srv/alpha'},
        {'sessionId': 'a-no-file', 'fileMtime': T0 + 980, 'projectPath': '/srv/alpha'},
        {'sessionId': 'a-by-date', 'fileMtime': 0, 'modified': '2027-01-15T10:00:00Z', 'projectPath': '/srv/alpha'},
        {'sessionId': 'a-undated', 'modified': 'last week', 'projectPath': '/srv/alpha'},
        {'sessionId': 'a-elsewhere', 'fileMtime': T0 + 700, 'projectPath': '/srv/other', 'summary': 'Of another directory'},
        {'sessionId': 'a-named', 'fileMtime': T0 + 100, 'summary': 'An older word of the same session', 'projectPath': '/srv/alpha'},
        {'sessionId': 'a-moved', 'fullPath': ROOT + '/-srv-alpha/nowhere.jsonl', 'fileMtime': T0 + 600, 'projectPath': '/srv/alpha'},
        {'sessionId': 'a-pathless', 'fileMtime': T0 + 500},
        'no entry at all',
        {'sessionId': '', 'fileMtime': T0 + 995},
        {'fileMtime': T0 + 996},
    ]}), T0 + 1000),
    ('-srv-alpha/a-named.jsonl', user('never read: the index says what it is') + '\n', T0 + 900),
    ('-srv-alpha/a-summed.jsonl', user('never read') + '\n', T0 + 800),
    ('-srv-alpha/a-side.jsonl', user('a side chain') + '\n', T0 + 990),
    ('-srv-alpha/a-by-date.jsonl', user('by its date') + '\n', T0 + 10),
    ('-srv-alpha/a-undated.jsonl', user('undated') + '\n', T0 + 20),
    ('-srv-alpha/a-elsewhere.jsonl', user('of another directory', cwd='/srv/other') + '\n', T0 + 700),
    ('-srv-alpha/a-moved.jsonl', user('its file is where its name says') + '\n', T0 + 600),
    ('-srv-alpha/a-pathless.jsonl', user('no directory said') + '\n', T0 + 500),
    # Transcripts the index does not name are opened: where it ran, what was asked first, its title.
    ('-srv-alpha/a-read.jsonl', '\n'.join([
        line(type='summary', summary='not a title'),
        user('  the first thing asked  '),
        said('an answer'),
        user('the second thing asked'),
        line(type='ai-title', aiTitle='A title the tool made', cwd='/srv/alpha'),
    ]) + '\n', T0 + 950),
    ('-srv-alpha/a-renamed.jsonl', '\n'.join([
        user([{'type': 'tool_result', 'content': 'x'}, {'type': 'text', 'text': ' asked in parts '}]),
        line(type='ai-title', aiTitle='Made by the tool'),
        line(type='custom-title', customTitle='Given by its user'),
        line(type='custom-title', customTitle=''),
        'not JSON at all',
        '[1, 2, 3]',
    ]) + '\n', T0 + 940),
    ('-srv-alpha/a-of-another.jsonl', user('it ran in another directory', cwd='/srv/gamma') + '\n', T0 + 930),
    ('-srv-alpha/a-wordless.jsonl', '\n'.join([
        line(type='user', message={'role': 'user', 'content': [{'type': 'image'}]}),
        line(type='user', message={'role': 'user'}),
        line(type='user'),
        line(type='system', cwd='/srv/alpha'),
    ]) + '\n', T0 + 920),
    # Long: what is asked after line 120 is no preview, and a name given near the end is found.
    ('-srv-alpha/a-long.jsonl', '\n'.join(
        [line(type='system', cwd='/srv/alpha', n=n) for n in range(125)]
        + [user('asked after a hundred and twenty lines')]
        + [said('x' * 900) for _ in range(90)]
        + [line(type='custom-title', customTitle='Named at the end ' + 'n' * 200)]
    ) + '\n', T0 + 910),
    ('-srv-alpha/empty.jsonl', '', T0 + 905),
    ('-srv-alpha/notes.txt', 'no transcript', T0 + 999),
    # A project without an index.
    ('-srv-beta/b-new.jsonl', user('the newest of beta', cwd='/srv/beta') + '\n', T0 + 300),
    ('-srv-beta/b-mid.jsonl', user('the middle one of beta', cwd='/srv/beta') + '\n', T0 + 200),
    ('-srv-beta/b-old.jsonl', user('the oldest of beta', cwd='/srv/beta') + '\n', T0 + 100),
    ('-srv-beta/b-silent.jsonl', line(type='system') + '\n', T0 + 250),
    # A project whose index cannot be read, and one whose index is no object.
    ('-srv-delta/sessions-index.json', '{"entries": [', T0 + 50),
    ('-srv-delta/d-one.jsonl', user('delta, by its transcript', cwd='/srv/delta') + '\n', T0 + 40),
    ('-srv-epsilon/sessions-index.json', '[]', T0 + 50),
    ('-srv-epsilon/e-one.jsonl', user('epsilon, by its transcript', cwd='/srv/epsilon') + '\n', T0 + 30),
    # A directory whose path has more than letters, digits and slashes: Claude Code names its
    # project with a dash for every such sign.
    ('-srv-my-project-v2-0/m-one.jsonl', user('in a directory with a dot and an underscore', cwd='/srv/my_project/v2.0') + '\n', T0 + 60),
    ('stray.jsonl', user('in no project') + '\n', T0 + 70),
]

# What is asked: (the directory, every project or one, how many at most).
ASKED = [
    ('/srv/alpha', False, 100), ('/srv/alpha', False, 3), ('/srv/alpha/', False, 100), ('/srv/alpha', False, 0),
    ('/srv/beta', False, 100), ('/srv/beta', False, 2), ('/srv/delta', False, 100), ('/srv/epsilon', False, 100),
    ('/srv/my_project/v2.0', False, 100), ('/srv/nothing', False, 100), ('', False, 100),
    ('', True, 100), ('/srv/alpha', True, 100), ('', True, 4),
]


@tool_scratch.case('clarp-past-sessions-vectors-')
def run(root) -> dict:
    """Runs in a process of its own: the list the Python Host gives for the files above."""
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    from lib import session_catalog

    projects = root / 'projects'
    for name, text, written in FILES:
        path = projects / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text.replace(ROOT, str(projects)))
        os.utime(path, (written, written))
    answers = []
    for cwd, everywhere, limit in ASKED:
        listed = session_catalog.list_claude_sessions(cwd, all_projects=everywhere, limit=limit, projects_root=projects)
        answers.append({'cwd': cwd, 'all_projects': everywhere, 'limit': limit, 'sessions': listed})
    none = session_catalog.list_claude_sessions('/srv/alpha', projects_root=root / 'no-such-directory')
    return {'files': [{'path': name, 'text': text, 'mtime': written} for name, text, written in FILES],
            'asked': answers, 'without_projects': none}


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == '--run':
        print(json.dumps(run()))
        return 0
    ran = subprocess.run([sys.executable, '-I', __file__, '--run'], capture_output=True, text=True,
                         env=tool_scratch.child_env({'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}), check=False)
    if ran.returncode != 0:
        print(ran.stderr[-5000:], file=sys.stderr)
        return 1
    said = json.loads(ran.stdout.splitlines()[-1])
    out = ['{', ' "files": [']
    out.extend('  ' + json.dumps(row, ensure_ascii=True) + (',' if n < len(said['files']) - 1 else '')
               for n, row in enumerate(said['files']))
    out.append(' ],')
    out.append(' "asked": [')
    out.extend('  ' + json.dumps(row, ensure_ascii=True) + (',' if n < len(said['asked']) - 1 else '')
               for n, row in enumerate(said['asked']))
    out.append(' ],')
    out.append(' "without_projects": ' + json.dumps(said['without_projects']))
    out.append('}')
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text('\n'.join(out) + '\n')
    print(f'{OUT.relative_to(REPO)}: {len(said["files"])} files, {len(said["asked"])} lists; '
          + '; '.join(f'{row["cwd"] or "everywhere" if not row["all_projects"] else "all:" + row["cwd"]}'
                      f'/{row["limit"]}: {len(row["sessions"])}' for row in said['asked']))
    return 0


if __name__ == '__main__':
    sys.exit(main())
