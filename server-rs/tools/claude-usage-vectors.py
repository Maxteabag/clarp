#!/usr/bin/env python3
"""What the Python Host shows an app of how much of a Claude account is used, and whether it
takes the account for used up: the provider as an app's usage screen reads it
(`server/lib/backend_usage.py`, `_structured_provider`, for Claude) and what an agent's row says
of its tool (`exhausted_backends`), from the one word that Host keeps of the account's two
windows.

That Host has the word from the provider, asked with the account's login
(`fetch_claude_usage`). Its own function is run here with a made-up answer in the provider's
place and a made-up login, one story at a time, each story in a store of its own, with a clock
the script holds; then it is asked as an app's request asks it. What it answers is written to
`crates/clarp-kernel/tests/fixtures/claude-usage-vectors.json`, so that what the Rust Host keeps
and shows of the same windows is held to it. (The Rust Host has the windows from Claude Code
itself, which says them as it runs; what is kept and shown of them is this.)

A story's steps: `said` (the provider's answer: each window with how much of it is used, in
percent, and when it begins anew; on the account the story names), `advance` (the clock goes on,
in seconds), `ended` (a turn of an agent of a tool ended, and how: a row of the Host's own log
of states, written here as that Host writes one), `asked` (an app asks, while the Host is on the
account the story names, or on none).

It runs in processes of its own, one for each story, whose every home, data, config and cache
directory is a new empty directory (the replay harness's own isolation,
`tests/replay/host.py`), with no way out to any network: the provider is never asked.

    nice "$(sed -n 1p ~/.local/share/clarp/current/SERVICE_PYTHON)" server-rs/tools/claude-usage-vectors.py
"""
from __future__ import annotations

import importlib.util
import io
import json
import pathlib
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = HERE.parent / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'claude-usage-vectors.json'

# The moment a story begins, in milliseconds, and the made-up accounts.
BEGAN = 1_900_000_000_000
ACCOUNT = 'claude-account-one'
OTHER = 'claude-account-other'
NO_LOGIN = 'no login'
HOUR = 3_600
FIVE_HOURS_ON = BEGAN // 1000 + 4 * HOUR
WEEK_ON = BEGAN // 1000 + 100 * HOUR


def said(five=None, week=None, on=ACCOUNT, **more):
    """The provider's answer: the two windows, each with what is used of it in percent."""
    answer = {}
    if five is not None:
        answer['five_hour'] = {'utilization': five, 'resets_at': more.pop('five_resets', FIVE_HOURS_ON)}
    if week is not None:
        answer['seven_day'] = {'utilization': week, 'resets_at': more.pop('week_resets', WEEK_ON)}
    return {'said': {**answer, **more}, 'on': None if on == NO_LOGIN else on}


def advance(seconds):
    return {'advance': seconds}


def ended(tool, how='done'):
    return {'ended': {'tool': tool, 'kind': how}}


def asked(on=ACCOUNT):
    return {'asked': True, 'active': None if on == NO_LOGIN else on}


ASKED = asked()

STORIES = [
    ('nothing-seen', 'Nothing was said of the account yet', [ASKED]),
    ('both-windows', 'Both windows, asked at once, an hour on, and a second after that',
     [said(37.5, 82.0), ASKED, advance(HOUR), ASKED, advance(1), ASKED]),
    ('the-newer-word', 'A newer word takes the place of the older',
     [said(10.0, 40.0), advance(600), said(12.5, 40.5), ASKED]),
    ('only-five-hours', 'The five hours alone, then the week alone: the newer word is all there is',
     [said(20.0), ASKED, advance(60), said(None, 55.0), ASKED]),
    ('no-window', 'An answer with no window in it is no word', [said(), ASKED]),
    ('a-window-with-no-figure', 'A window that does not say how much is used is left out',
     [{'said': {'five_hour': {'utilization': None, 'resets_at': FIVE_HOURS_ON},
                'seven_day': {'utilization': 44.0, 'resets_at': WEEK_ON}}, 'on': ACCOUNT}, ASKED]),
    ('the-week-for-apps', 'The week that counts is the one for apps where the provider says one',
     [{'said': {'five_hour': {'utilization': 5.0, 'resets_at': FIVE_HOURS_ON},
                'seven_day_oauth_apps': {'utilization': 61.0, 'resets_at': WEEK_ON},
                'seven_day': {'utilization': 12.0, 'resets_at': WEEK_ON + 60}}, 'on': ACCOUNT}, ASKED]),
    ('past-its-reset', 'A window whose reset is past, by a word that is young',
     [said(60.0, 30.0, five_resets=BEGAN // 1000 + 300), ASKED, advance(301), ASKED]),
    ('no-reset', 'A window that does not say when it begins anew',
     [{'said': {'five_hour': {'utilization': 97.0}, 'seven_day': {'utilization': 44.0, 'resets_at': ''}},
       'on': ACCOUNT}, ASKED]),
    ('a-reset-in-words', 'A reset said as a moment in words',
     [{'said': {'five_hour': {'utilization': 9.0, 'resets_at': '2030-03-17T21:46:40+00:00'}}, 'on': ACCOUNT}, ASKED]),
    ('another-account', 'The Host is on another account than the one the word is of',
     [said(90.0, 10.0), ASKED, asked(OTHER), asked(NO_LOGIN), advance(60), said(20.0, 30.0, on=OTHER),
      asked(OTHER), asked(ACCOUNT), asked(NO_LOGIN)]),
    ('no-account-known', 'A word of an account nobody could tell',
     [said(50.0, 20.0, on=NO_LOGIN), asked(NO_LOGIN), asked(ACCOUNT), asked(OTHER)]),
    ('used-up', 'The five hours are used up',
     [said(100.0, 50.0), ASKED, advance(HOUR), ASKED, advance(1), ASKED]),
    ('the-week-is-used-up', 'The week is used up while the five hours are not', [said(12.0, 100.0), ASKED]),
    ('both-used-up', 'Both are used up: the later reset counts', [said(100.0, 100.0), ASKED]),
    ('over-all-of-it', 'More than all of a window is said to be used', [said(104.5, 50.0), ASKED]),
    ('nearly-used-up', 'Nearly all of a window is used', [said(99.9, 99.9), ASKED]),
    ('used-up-with-no-reset', 'Used up, and one of the two does not say when it begins anew',
     [{'said': {'five_hour': {'utilization': 100.0}, 'seven_day': {'utilization': 100.0, 'resets_at': WEEK_ON}},
       'on': ACCOUNT}, ASKED]),
    ('used-up-and-the-week-says-no-reset', 'Both used up, and it is the week that does not say when it begins anew',
     [{'said': {'five_hour': {'utilization': 100.0, 'resets_at': FIVE_HOURS_ON}, 'seven_day': {'utilization': 100.0}},
       'on': ACCOUNT}, ASKED]),
    ('used-up-and-grown-old-then-said-again', 'Used up, an hour and more on, and said again',
     [said(100.0, 50.0), advance(HOUR + 60), ASKED, said(100.0, 50.5), ASKED]),
    ('used-up-and-a-turn-completed-since', 'Used up, and a turn of Claude completed after that was said',
     [said(100.0, 50.0), ASKED, advance(30), ended('claude'), ASKED]),
    ('used-up-and-a-turn-completed-in-the-same-moment', 'A turn that completed in the moment it was said proves nothing',
     [said(100.0, 50.0), ended('claude'), ASKED, advance(1), ASKED]),
    ('used-up-and-a-turn-of-another-tool-completed', 'A turn of another tool says nothing of this account',
     [said(100.0, 50.0), advance(30), ended('codex'), ASKED]),
    ('used-up-and-a-turn-ended-otherwise', 'A turn that was cut off or failed says nothing of it either',
     [said(100.0, 50.0), advance(30), ended('claude', 'interrupted'), ended('claude', 'error'), ASKED]),
    ('used-up-again-after-a-turn-completed', 'Used up, a turn completed, and it is said to be used up again',
     [said(100.0, 50.0), advance(30), ended('claude'), ASKED, advance(30), said(100.0, 50.0), ASKED,
      advance(30), ended('claude'), ASKED]),
    ('used-up-and-another-account', 'Used up, and the Host is on another account',
     [said(100.0, 10.0), asked(OTHER), asked(ACCOUNT), asked(NO_LOGIN)]),
]


def told(story):
    """Runs in a process of its own: one story, from an empty store."""
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    root = pathlib.Path(tempfile.mkdtemp(prefix='clarp-claude-usage-vectors-', dir='/var/tmp'))
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    import urllib.request
    from lib import backend_usage as usage
    from lib import backend_usage_store as store
    from lib import db
    store_of_all = db.conn()
    # Which Host this is: one name for every story, so that the same stories give the same
    # file (a Host draws its name; every id of a window follows from it).
    store._computer_id = lambda: 'a-host'
    for tool in ('codex', 'claude'):
        store_of_all.execute(
            'INSERT INTO agents (agent_id, persona, voice_id, cwd, session, created_at, backend) VALUES (?,?,?,?,?,?,?)',
            (f'agent-{tool}', f'Of {tool}', '', '/', f'of-{tool}', BEGAN, tool))
    clock = [BEGAN]
    for module in list(sys.modules.values()):
        if getattr(module, '__name__', '').startswith('lib.') and hasattr(module, '_now_ms'):
            module._now_ms = lambda: clock[0]
    # The account the Host is on, and the provider's answer, are the story's word: no login is
    # read and nobody is asked.
    active, answer = [ACCOUNT], [{}]
    usage._read_claude_auth = lambda: ('a-token', 0, 'max')
    usage._active_claude_account_ref = lambda *, create=False: active[0]

    class Answer(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *gone):
            return False

    urllib.request.urlopen = lambda request, timeout=None: Answer(json.dumps(answer[0]).encode())
    name, what, steps = story
    done = []
    for step in steps:
        at = {'at': (clock[0] - BEGAN) // 1000}
        if 'advance' in step:
            clock[0] += step['advance'] * 1000
            continue
        if 'ended' in step:
            how = step['ended']
            store_of_all.execute('INSERT INTO state_log (agent_id, runtime_id, ts, kind, detail) VALUES (?,?,?,?,?)',
                                 (f"agent-{how['tool']}", None, clock[0], how['kind'], '{}'))
            done.append({**at, 'ended': how})
            continue
        if 'said' in step:
            active[0], answer[0] = step['on'], step['said']
            try:
                kept = usage.fetch_claude_usage()
                did = {'windows': kept['windows']}
            except RuntimeError as refused:
                did = {'refused': str(refused)}
            done.append({**at, 'said': step['said'], 'on': step['on'], **did})
            continue
        active[0] = step['active']
        now = clock[0]
        done.append({**at, 'asked': True, 'active': step['active'],
                     'provider': usage._structured_provider('claude', now),
                     'used_up': usage.exhausted_backends(now)})
    return {'name': name, 'what': what, 'steps': done}


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == '--story':
        story = next(story for story in STORIES if story[0] == sys.argv[2])
        sys.stdout.write('\nVECTORS ' + json.dumps(told(story)) + '\n')
        return 0
    stories = []
    for story in STORIES:
        run = subprocess.run([sys.executable, __file__, '--story', story[0]], capture_output=True, text=True)
        lines = [line for line in run.stdout.splitlines() if line.startswith('VECTORS ')]
        if run.returncode or not lines:
            sys.stderr.write(f'{story[0]}: the story did not run to its end\n{run.stderr[-1500:]}\n')
            return 1
        stories.append(json.loads(lines[-1][len('VECTORS '):]))
    text = json.dumps({'began_ms': BEGAN, 'stories': stories}, indent=1, ensure_ascii=False, sort_keys=True)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text + '\n')
    steps = sum(len(story['steps']) for story in stories)
    asked_n = sum(1 for story in stories for step in story['steps'] if step.get('asked'))
    used_up = sum(1 for story in stories for step in story['steps'] if step.get('asked') and step['used_up'])
    print(f'{len(stories)} stories, {steps} steps, {asked_n} askings, {used_up} of them used up -> {OUT.relative_to(REPO)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
