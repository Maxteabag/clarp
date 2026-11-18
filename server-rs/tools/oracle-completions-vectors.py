#!/usr/bin/env python3
"""Writes down what the Python Host shows the Oracle's voice of what agents finished for the user.

Between two turns of a call the Oracle's voice is told, one thing at a time, of what waits for the user: the
requests of agents, and what an agent finished that the user was told of and did not go on about. This is the second
of the two: which finished turns are shown, in which order, and which of them are too old to bring up.

The rule is `server/lib/oracle_attention.pending_completion_notifications`, run here as it is on a database of this
program's own. For every case it is written down what the Host has of each turn (the notification it kept, the
answer and the message that began it as its conversations have them, and whether the user or the Oracle wrote to
that conversation since: by that Host's own query) and what the rule shows of them. And what the end of a turn is told
under (`user_notifications._notification_id`): of the agent and the moment, so that it is the same when told again.

    <the Python Host's interpreter> server-rs/tools/oracle-completions-vectors.py

It runs in a process of its own whose every home, data, config and cache directory is a new empty directory (the
replay harness's own isolation), with no way out of the machine. The same file at every run. Nothing is logged.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[2]
OUT = REPO / 'server-rs' / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'oracle-completions-vectors.json'
NOW = 1_800_000_000_000
MINUTE, HOUR = 60_000, 3_600_000


def turn(ident, ago=MINUTE, *, told=None, answer=None, asked=None, since=()):
    """A turn that ended so long ago, as the Host has it: the notification it kept (`told`), the answer and the
    message that began the turn in the agent's conversation (`answer`, `asked`; `False`: there is no such row), and
    what was written to that conversation since (`since`). Every turn is of an agent of its own, unless it says
    otherwise."""
    done = NOW - ago
    agent, tool = f'a-{ident}', f'tool-{ident}'
    told = {'notification_id': f'n-{ident}', 'agent_id': agent, 'session': f'session-{ident}', 'persona': f'Agent {ident}',
            'backend_session_id': tool, 'trace_id': f't-{ident}', 'done_ts': done, 'source_message_id': f's-{ident}',
            'cause_message_id': f'c-{ident}', 'origin': 'user', 'notify': 1, 'push': 1, 'badge': 1, 'unread': 1,
            'preview': '', 'reason': 'text-reply', 'created_at': done, 'updated_at': done, 'muted': 0, **(told or {})}
    rows = []
    if answer is not False:
        rows.append({'message_id': told['source_message_id'] or f's-{ident}', 'agent_id': agent, 'backend_session_id': tool,
                     'role': 'assistant', 'text': f'The answer of {ident}.', 'origin': 'user', 'updated_at': done,
                     **(answer or {})})
    if asked is not False:
        rows.append({'message_id': told['cause_message_id'] or f'c-{ident}', 'agent_id': agent, 'backend_session_id': tool,
                     'role': 'user', 'text': f'The question of {ident}.', 'origin': 'user', 'updated_at': done - 5000,
                     **(asked or {})})
    for number, later in enumerate(since):
        rows.append({'message_id': f'l-{ident}-{number}', 'agent_id': agent, 'backend_session_id': tool, 'role': 'user',
                     'text': 'Later.', 'origin': 'user', 'updated_at': done + 1000, **later})
    return {'told': told, 'rows': rows}


def case(name, *turns, asks=((20, 0),)):
    """A Host with these turns, asked for so many of them, so long after now."""
    return {'name': name, 'turns': list(turns), 'asks': [{'limit': limit, 'later': later} for limit, later in asks]}


CASES = [
    case('a turn the user was told of is shown, with the agent, the message and the answer it is of',
         turn('plain'), turn('oracle', 2 * MINUTE, told={'origin': 'oracle'}, asked={'origin': 'oracle'}),
         turn('unnamed', 3 * MINUTE, told={'persona': ''}), turn('blanks', 4 * MINUTE, told={'origin': ' user '},
                                                                 answer={'text': '  An answer with blanks around it.  '})),
    case('what the user was not told of, or has read, is not shown',
         turn('not-told', told={'notify': 0}), turn('read', 2 * MINUTE, told={'unread': 0}),
         turn('no-conversation', 5 * MINUTE, told={'backend_session_id': ''}, answer={'backend_session_id': ''},
              asked={'backend_session_id': ''}),
         turn('still-shown', 6 * MINUTE)),
    # A row that has no name can be there (one of them: the name is what rows are told apart by), and is the answer or
    # the message of no turn.
    case('a turn that names no answer is not shown, though an answer without a name is there',
         turn('no-answer-named', told={'source_message_id': ''}, answer={'message_id': ''}),
         turn('no-answer-named-and-none-there', 2 * MINUTE, told={'source_message_id': ''}, answer=False),
         turn('still-shown', 3 * MINUTE)),
    case('a turn that names no message is not shown, though a message without a name is there',
         turn('no-message-named', told={'cause_message_id': ''}, asked={'message_id': ''}),
         turn('no-message-named-and-none-there', 2 * MINUTE, told={'cause_message_id': ''}, asked=False),
         turn('still-shown', 3 * MINUTE)),
    case('a turn whose answer or message the conversation does not have as it is named is not shown',
         turn('answer-gone', answer=False), turn('answer-of-the-user', 2 * MINUTE, answer={'role': 'user'}),
         turn('answer-of-another', 3 * MINUTE, answer={'agent_id': 'a-other'}),
         turn('answer-elsewhere', 4 * MINUTE, answer={'backend_session_id': 'tool-other'}),
         turn('answer-nowhere', 5 * MINUTE, answer={'backend_session_id': None}),
         turn('message-gone', 6 * MINUTE, asked=False), turn('message-of-the-agent', 7 * MINUTE, asked={'role': 'assistant'}),
         turn('message-of-another', 8 * MINUTE, asked={'agent_id': 'a-other'}),
         turn('message-elsewhere', 9 * MINUTE, asked={'backend_session_id': 'tool-other'}),
         turn('message-nowhere', 10 * MINUTE, asked={'backend_session_id': None}),
         turn('still-shown', 11 * MINUTE)),
    case('only what the user or the Oracle asked for is shown',
         turn('of-an-agent', told={'origin': 'agent'}), turn('of-a-schedule', 2 * MINUTE, told={'origin': 'schedule'}),
         turn('of-a-watcher', 3 * MINUTE, told={'origin': 'watcher'}), turn('of-nobody', 4 * MINUTE, told={'origin': ''}),
         turn('asked-by-an-agent', 5 * MINUTE, asked={'origin': 'agent'}),
         turn('asked-by-nobody-said', 6 * MINUTE, asked={'origin': ''}),
         turn('asked-by-nobody-said-of-an-agent', 7 * MINUTE, told={'origin': 'agent'}, asked={'origin': ''}),
         turn('asked-by-the-oracle', 8 * MINUTE, asked={'origin': 'oracle'}),
         turn('asked-with-blanks', 9 * MINUTE, asked={'origin': ' oracle '}),
         turn('asked-by-blanks', 10 * MINUTE, asked={'origin': '  '})),
    case('a turn the user or the Oracle wrote after is not shown; what anybody else wrote after it changes nothing',
         turn('gone-on', since=[{}]), turn('gone-on-by-the-oracle', 2 * MINUTE, since=[{'origin': 'oracle'}]),
         turn('an-agent-wrote', 3 * MINUTE, since=[{'origin': 'agent'}]),
         turn('the-agent-went-on', 4 * MINUTE, since=[{'role': 'assistant'}]),
         turn('written-elsewhere', 5 * MINUTE, since=[{'backend_session_id': 'tool-other'}]),
         turn('written-to-another', 6 * MINUTE, since=[{'agent_id': 'a-other'}]),
         turn('written-at-that-moment', 7 * MINUTE, since=[{'updated_at': NOW - 7 * MINUTE}]),
         turn('written-a-moment-after', 8 * MINUTE, since=[{'updated_at': NOW - 8 * MINUTE + 1}]),
         turn('written-before', 9 * MINUTE, since=[{'updated_at': NOW - 10 * MINUTE}]),
         # A message says when it was said: that, and not when it was written down, is its moment.
         turn('said-before-written-after', 10 * MINUTE,
              since=[{'timestamp': '2027-01-15T07:40:00+00:00', 'updated_at': NOW}]),
         turn('said-after', 11 * MINUTE, since=[{'timestamp': '2027-01-15T07:50:00+00:00', 'updated_at': NOW - HOUR}])),
    case('an answer that says nothing is not shown',
         turn('nothing', answer={'text': ''}), turn('blanks', 2 * MINUTE, answer={'text': '   \n '}),
         turn('something', 3 * MINUTE, answer={'text': ' . '})),
    case('what is older than a day is not shown, and what is older than a quarter of an hour is said to be old',
         turn('new', 1), turn('a-quarter', 15 * MINUTE), turn('a-quarter-and-more', 15 * MINUTE + 1),
         turn('a-day', 24 * HOUR), turn('a-day-and-more', 24 * HOUR + 1), turn('to-come', -5 * MINUTE),
         asks=((20, 0), (20, 1), (20, 14 * MINUTE), (20, 24 * HOUR))),
    case('the newest are shown first, and of two that ended at one moment the one named last',
         *[turn(f'{number:02}', (7 * number) % 31 * MINUTE + MINUTE) for number in range(30)],
         turn('twin-a', 40 * MINUTE), turn('twin-b', 40 * MINUTE), turn('twin-Z', 40 * MINUTE),
         asks=((20, 0), (1, 0), (2, 0), (25, 0), (40, 0), (0, 0), (-1, 0), (True, 0), (2.5, 0), ('7', 0), (None, 0))),
    # The fourth is looked at for one that is asked for, and the ninth only for three.
    case('four turns are looked at for each that is asked for, and no more',
         *[turn(f'not-shown-{number}', (number + 1) * MINUTE, told={'origin': 'agent'}) for number in range(3)],
         turn('the-fourth', 4 * MINUTE),
         *[turn(f'not-shown-{number}', (number + 2) * MINUTE, told={'origin': 'agent'}) for number in range(3, 7)],
         turn('the-ninth', 9 * MINUTE),
         asks=((1, 0), (2, 0), (3, 0))),
    case('no more than eighty turns are looked at, however many are asked for',
         *[turn(f'not-shown-{number:02}', (number + 1) * 1000, told={'origin': 'agent'}) for number in range(79)],
         turn('the-eightieth', 80 * 1000), turn('the-eighty-first', 81 * 1000),
         asks=((20, 0), (21, 0), (50, 0))),
    case('a Host that told its user of nothing shows nothing', asks=((20, 0), (1, 0))),
]


# What the end of a turn is told under: of which agent, ended at which moment.
NAMED = [('a-plain', NOW), ('a-plain', NOW + 1), ('a-other', NOW), ('', 0), ('an agent with blanks ', 1),
         (chr(0xe9) + '-agent', 1_799_999_999_999), ('a' * 40, -5)]


def run() -> dict:
    """Runs in a process of its own: what the Python Host's module does."""
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    root = pathlib.Path(tempfile.mkdtemp(prefix='clarp-oracle-completions-vectors-', dir='/var/tmp'))
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    from lib import db, oracle_attention, user_notifications
    from lib.message_writes import _message_activity_sql

    def put(table, row):
        names = ', '.join(row)
        db.conn().execute(f'INSERT INTO {table} ({names}) VALUES ({", ".join("?" for _ in row)})', tuple(row.values()))

    def played(one):
        for table in ('user_notifications', 'messages'):
            db.conn().execute(f'DELETE FROM {table}')
        seq = 0
        for made in one['turns']:
            put('user_notifications', made['told'])
            for row in made['rows']:
                seq += 1
                put('messages', {'seq': seq, 'timestamp': None, **row})
        # What the Host has of every turn it kept a notification of: as a Host of another kind would hand it over.
        had = []
        for told in db.conn().execute('SELECT * FROM user_notifications ORDER BY rowid'):
            told = dict(told)
            rows = {}
            for name, key in (('answer', 'source_message_id'), ('asked', 'cause_message_id')):
                row = db.conn().execute('SELECT role, agent_id, backend_session_id, origin, text FROM messages WHERE message_id=?',
                                        (told[key],)).fetchone()
                rows[name] = dict(row) if row else None
            since = db.conn().execute(
                f"""SELECT 1 FROM messages WHERE agent_id=? AND backend_session_id=? AND role='user'
                    AND {_message_activity_sql()}>? AND origin IN ('user','oracle') LIMIT 1""",
                (told['agent_id'], told['backend_session_id'], told['done_ts'])).fetchone()
            had.append({name: told[name] for name in ('notification_id', 'agent_id', 'session', 'persona', 'backend_session_id',
                                                      'done_ts', 'source_message_id', 'cause_message_id', 'origin')}
                       | {'notify': told['notify'] == 1, 'unread': told['unread'] == 1, **rows,
                          'written_to_since': since is not None})
        asks = []
        for ask in one['asks']:
            answered = dict(ask)
            try:
                if ask['limit'] is None:
                    answered['shown'] = oracle_attention.pending_completion_notifications(now_ms_value=NOW + ask['later'])
                else:
                    answered['shown'] = oracle_attention.pending_completion_notifications(now_ms_value=NOW + ask['later'],
                                                                                         limit=ask['limit'])
            except ValueError as refused:
                answered['raises'] = str(refused)
            asks.append(answered)
        return {'name': one['name'], 'had': had, 'asks': asks}

    named = [{'agent_id': agent, 'done_ts': done, 'id': user_notifications._notification_id(agent, done)}
             for agent, done in NAMED]
    return {'what': 'server/lib/oracle_attention.py: pending_completion_notifications', 'now': NOW,
            'most': oracle_attention.MAX_DECISIONS, 'named': named, 'cases': [played(one) for one in CASES]}


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == '--run':
        print(json.dumps(run()))
        return 0
    ran = subprocess.run([sys.executable, '-I', __file__, '--run'], capture_output=True, text=True,
                         env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}, check=False)
    if ran.returncode != 0:
        print(ran.stderr[-3000:], file=sys.stderr)
        return 1
    said = json.loads(ran.stdout.splitlines()[-1])
    out = ['{']
    for name in ('what', 'now', 'most'):
        out.append(f' {json.dumps(name)}: {json.dumps(said[name], ensure_ascii=True)},')
    out.append(' "named": [')
    out.extend('  ' + json.dumps(row, ensure_ascii=True) + (',' if n < len(said['named']) - 1 else '')
               for n, row in enumerate(said['named']))
    out.append(' ],')
    out.append(' "cases": [')
    for at, one in enumerate(said['cases']):
        out.append('  {"name": ' + json.dumps(one['name'], ensure_ascii=True) + ',')
        out.append('   "had": [')
        out.extend('    ' + json.dumps(row, ensure_ascii=True) + (',' if n < len(one['had']) - 1 else '')
                   for n, row in enumerate(one['had']))
        out.append('   ],')
        out.append('   "asks": [')
        out.extend('    ' + json.dumps(row, ensure_ascii=True) + (',' if n < len(one['asks']) - 1 else '')
                   for n, row in enumerate(one['asks']))
        out.append('   ]}' + (',' if at < len(said['cases']) - 1 else ''))
    out.append(' ]')
    out.append('}')
    OUT.write_text('\n'.join(out) + '\n', encoding='ascii')
    asks = [ask for one in said['cases'] for ask in one['asks']]
    print(f'{OUT.relative_to(REPO)}: {len(said["cases"])} cases, {sum(len(one["had"]) for one in said["cases"])} turns, '
          f'{len(asks)} askings, {sum(len(ask.get("shown", [])) for ask in asks)} shown, '
          f'{sum(1 for ask in asks if "raises" in ask)} refused, {OUT.stat().st_size} bytes')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
