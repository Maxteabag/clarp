#!/usr/bin/env python3
"""What the Python Host shows an app of how much of a Codex account is used, from what Codex
itself says of its limits: the windows of the provider as an app's usage screen reads them
(`server/lib/backend_usage.py`, `_structured_provider`), the row of the tool beside them
(`backend_usage_store._response_row`), and the limit events told along the way, as its own
keeping of such records makes them (`backend_usage_codex.capture_codex_rate_limits`).

Its own code is given made-up records over a made-up day, one story at a time, each story in a
store of its own, with a clock the script holds, and is asked after every step as an app's
request asks it. What it answers is written to
`crates/clarp-kernel/tests/fixtures/codex-usage-vectors.json`, so that what the Rust Host shows
of the same records is held to it. With each record is written what the Python Host's reader
makes of it (`_codex_snapshot_from_payload`: its windows), which is what the Rust Host's keeper
is given too.

A record is given as Codex writes one into a conversation's rollout with a count of tokens
(`rate_limits`: `primary` and `secondary`, each with `used_percent`, `window_minutes` and
`resets_at`), under the name its usage endpoint gives it, and as an observation that says
nothing of what it leaves out (not as a whole reading of the account): that is what a record in
a rollout is. The account is a made-up one: the login is never read.

A story's steps: `record` (a record of limits is seen, under the login it is seen on),
`advance` (the clock goes on, in seconds), `asked` (an app asks: limits whose window began anew
are closed first, as a request closes them). A login is two names, neither of them anybody's:
which login it is (another after every renewal of it) and whose account. What an app is shown
depends on the login the Host is on when it asks (`_active_codex_identity`, which is given the
story's word for it here, since no login is ever read): `active` on an asking, or nothing where
the Host has no login.

With every asking is written whether the Python Host takes the tool's account for used up
(`backend_usage.exhausted_backends`: what an agent's row says of its tool, `backend_quota`). That
depends on the turns that completed since a window was last heard of, so a story also says where
a turn of an agent ended (`ended`: of which tool, and how; a row of the Host's own log of states,
written here as that Host writes one).

A story may also say that a turn of Codex failed for a usage limit (`failed_for_a_limit`): the Python Host
then records a limit of which nothing more is known (`record_classified_usage_limit`), on the
login the story says the Host is on. Such a limit ends there when the provider is asked and says
that all is well (`_resolve_unknown_terminals`). The Rust Host asks no provider: it ends such a
limit when a turn of Codex completes, and so that function is called here where a story says that
a turn of Codex completed, on the login the Host is on then. What it answers is written with the
step.

It runs in processes of its own, one for each story, whose every home, data, config and cache
directory is a new empty directory (the replay harness's own isolation,
`tests/replay/host.py`), with no way out to any network.

    nice "$(sed -n 1p ~/.local/share/clarp/current/SERVICE_PYTHON)" server-rs/tools/codex-usage-vectors.py
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = HERE.parent / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'codex-usage-vectors.json'

# The moment a story begins, in milliseconds, and the made-up account its records are of.
BEGAN = 1_900_000_000_000
IDENTITY = ['generation-one', 'account-one']
# What is said of where a record came from.
SEEN_IN = 'rollout/token_count'
HOUR = 3_600
FIVE_HOURS_ON = BEGAN // 1000 + 4 * HOUR          # the five-hour window begins anew in four hours
WEEK_ON = BEGAN // 1000 + 100 * HOUR


def window(used, minutes, resets_at):
    return {'used_percent': used, 'window_minutes': minutes, 'resets_at': resets_at}


# Logins: the one most stories are told on, the same account after its login was renewed (and
# renewed again, with nothing seen since), another account, an account nothing was seen of, and
# a login whose account nobody could tell.
RENEWED = ['generation-two', 'account-one']
RENEWED_AGAIN = ['generation-three', 'account-one']
OTHER = ['generation-other', 'account-other']
NOT_SEEN = ['generation-nine', 'account-nine']
NO_ACCOUNT = ['generation-bare', None]
NO_LOGIN = 'no login'


def record(five=None, week=None, on=None, **more):
    """A record of limits as a rollout has it: the five-hour window first, the week second."""
    return {**({} if on is None else {'identity': on}), 'record': {
        'limit_id': 'codex', 'limit_name': None,
        'primary': None if five is None else window(five, 300, more.pop('five_resets', FIVE_HOURS_ON)),
        'secondary': None if week is None else window(week, 10_080, more.pop('week_resets', WEEK_ON)),
        'credits': {'has_credits': False, 'unlimited': False, 'balance': None},
        'individual_limit': None, 'spend_control_reached': None, 'plan_type': 'pro',
        'rate_limit_reached_type': None, **more}}


def advance(seconds):
    return {'advance': seconds}


def ended(tool, how='done', on=None):
    """A turn of an agent of this tool ended now, this way (while the Host is on this login)."""
    return {'ended': {'tool': tool, 'kind': how}, **({} if on is None else {'active': None if on == NO_LOGIN else on})}


def refused(on=None):
    """A turn of Codex failed for a usage limit (while the Host is on this login)."""
    return {'failed_for_a_limit': True, **({} if on is None else {'active': None if on == NO_LOGIN else on})}


ASKED = {'asked': True}


def asked(on):
    """An app asks while the Host is on this login (or on none)."""
    return {'asked': True, 'active': None if on == NO_LOGIN else on}


STORIES = [
    ('nothing-seen', 'Nothing was seen of the account yet', [ASKED]),
    ('both-windows', 'A record with both windows, asked at once, a quarter of an hour on, and an hour on',
     [record(37.5, 82.0), ASKED, advance(14 * 60), ASKED, advance(2 * 60), ASKED, advance(50 * 60), ASKED]),
    ('the-newer-record', 'A newer record takes the place of the older',
     [record(10.0, 40.0), advance(600), record(12.5, 40.5), ASKED]),
    ('only-five-hours', 'A record with the five-hour window alone, then one with the week alone',
     [record(20.0), ASKED, advance(60), record(None, 55.0), ASKED]),
    ('no-window', 'A record with no window in it is no record', [record(), ASKED]),
    ('past-its-reset', 'A window whose reset is past is stale, however young the record',
     [record(60.0, 30.0, five_resets=BEGAN // 1000 + 300), ASKED, advance(301), ASKED]),
    ('the-warning', 'Four fifths of the five-hour window are used: a warning; it is told once',
     [record(79.9, 10.0), ASKED, advance(60), record(80.0, 10.0), ASKED, advance(60), record(91.0, 10.5), ASKED]),
    ('the-limit', 'The five-hour window is used up: a hard limit, and it is over when the window begins anew',
     [record(85.0, 10.0), advance(60), record(100.0, 11.0), ASKED, advance(60), record(100.0, 11.0), ASKED,
      advance(4 * HOUR), ASKED, advance(60), record(1.0, 11.5, five_resets=FIVE_HOURS_ON + 5 * HOUR), ASKED]),
    ('straight-to-the-limit', 'Used up at the first record that is seen',
     [record(100.0, 50.0), ASKED]),
    ('the-week-is-used-up', 'The week is used up while the five hours are not',
     [record(12.0, 100.0), ASKED]),
    ('back-under', 'Under the warning again before the window began anew (another account took its place)',
     [record(90.0, 10.0), ASKED, advance(60), record(5.0, 10.0), ASKED]),
    ('another-window', 'The five-hour window began anew between two records',
     [record(95.0, 10.0), ASKED, advance(4 * HOUR + 60),
      record(2.0, 10.5, five_resets=FIVE_HOURS_ON + 5 * HOUR), ASKED]),
    ('odd-lengths', 'Windows of lengths nobody has a name for',
     [{'record': {'primary': window(33.0, 60, FIVE_HOURS_ON), 'secondary': window(44.0, 43_200, WEEK_ON),
                  'plan_type': 'team'}}, ASKED]),
    ('no-lengths', 'Windows that do not say how long they are',
     [{'record': {'primary': {'used_percent': 33.0, 'resets_at': FIVE_HOURS_ON},
                  'secondary': {'used_percent': 44.0, 'resets_at': WEEK_ON}}}, ASKED]),
    ('no-reset', 'A window that does not say when it begins anew',
     [{'record': {'primary': {'used_percent': 97.0, 'window_minutes': 300},
                  'secondary': {'used_percent': 44.0, 'window_minutes': 10_080}}}, ASKED,
      advance(60), {'record': {'primary': {'used_percent': 100.0, 'window_minutes': 300}}}, ASKED]),
    ('a-quarter-of-an-hour-exactly', 'What was said a quarter of an hour ago is fresh; a second on it is not',
     [record(37.5, 82.0), advance(15 * 60), ASKED, advance(1), ASKED]),
    ('at-its-reset-exactly', 'A window is stale from the second it begins anew',
     [record(60.0, 30.0, five_resets=BEGAN // 1000 + 300), advance(299), ASKED, advance(1), ASKED]),
    ('the-limit-at-its-reset-exactly', 'A limit is no longer known from the second its window begins anew',
     [record(100.0, 11.0, five_resets=BEGAN // 1000 + 300), advance(299), ASKED, advance(1), ASKED,
      advance(1), ASKED]),
    ('a-warning-grown-old', 'A warning nobody has heard of for a quarter of an hour is not known to stand',
     [record(85.0, 10.0), ASKED, advance(16 * 60), ASKED, advance(60), record(86.0, 10.0), ASKED]),
    ('limited-again-after-recovering', 'Back under the warning and over it again: another stretch',
     [record(85.0, 10.0), advance(60), record(5.0, 10.0), advance(60), record(90.0, 10.5), ASKED,
      advance(60), record(100.0, 11.0), ASKED]),
    ('still-used-up-in-the-next-window', 'Used up, and used up again in the window after it, with nobody asking between',
     [record(100.0, 10.0), ASKED, advance(4 * HOUR + 60),
      record(100.0, 10.5, five_resets=FIVE_HOURS_ON + 5 * HOUR), ASKED]),
    ('used-up-again-after-the-reset', 'Used up, asked after the window began anew, and used up again',
     [record(100.0, 10.0), advance(4 * HOUR + 60), ASKED, advance(60),
      record(100.0, 10.5, five_resets=FIVE_HOURS_ON + 5 * HOUR), ASKED]),
    ('another-login', 'Another account is logged in while a warning stands, and the first comes back',
     [record(90.0, 10.0), ASKED, advance(60), record(20.0, 30.0, on=OTHER), asked(OTHER), asked(IDENTITY),
      asked(NO_LOGIN), asked(NOT_SEEN), advance(60), record(91.0, 10.5), asked(IDENTITY), asked(OTHER),
      asked(NO_LOGIN)]),
    ('the-login-renewed', 'The login of the same account is renewed while a warning stands',
     [record(90.0, 10.0), ASKED, advance(60), asked(RENEWED), advance(60), record(92.0, 10.0, on=RENEWED),
      asked(RENEWED), asked(IDENTITY), asked(RENEWED_AGAIN), asked(NOT_SEEN), asked(NO_LOGIN)]),
    ('renewed-and-only-the-week-seen', 'After a renewal only the week is seen: the five hours of the login before are not shown',
     [record(40.0, 10.0), advance(60), record(None, 10.5, on=RENEWED), asked(RENEWED), asked(NO_LOGIN)]),
    ('no-account-known', 'A login whose account nobody could tell',
     [record(85.0, 10.0, on=NO_ACCOUNT), asked(NO_ACCOUNT), advance(60), record(5.0, 10.0, on=NO_ACCOUNT),
      asked(NO_ACCOUNT), asked(['generation-bare', 'account-one']), asked(IDENTITY), asked(NO_LOGIN)]),
    ('an-account-and-a-login-without-one', 'Records of an account, then of a login whose account nobody could tell',
     [record(85.0, 10.0), advance(60), record(30.0, 20.0, on=NO_ACCOUNT), asked(IDENTITY), asked(NO_ACCOUNT),
      asked(['generation-bare', 'account-one']), asked(NO_LOGIN)]),
    ('a-window-that-does-not-say-its-use', 'While a warning stands, a later window says when it begins anew and not how much is used',
     [record(85.0, 10.0), advance(60),
      {'record': {'primary': {'used_percent': None, 'window_minutes': 300, 'resets_at': FIVE_HOURS_ON + 5 * HOUR}}},
      ASKED, advance(4 * HOUR), ASKED]),
    ('the-reset-moves', 'A window says another second for its reset, and the first again (Codex does)',
     [record(50.0, 10.0), advance(60),
      record(50.5, 10.0, five_resets=FIVE_HOURS_ON + 1, week_resets=WEEK_ON + 30), ASKED, advance(60),
      record(51.0, 10.0), ASKED]),
    ('recovered-and-the-reset-moves-back', 'A warning is over; the window says another second for its reset, and the first again',
     [record(85.0, 10.0), advance(60), record(5.0, 10.0), advance(60),
      record(6.0, 10.0, five_resets=FIVE_HOURS_ON + 1), ASKED, advance(60), record(7.0, 10.0), ASKED]),
    ('a-warning-and-the-reset-moves', 'A warning stands while the window says another second for its reset',
     [record(85.0, 10.0), advance(60), record(86.0, 10.0, five_resets=FIVE_HOURS_ON + 1), ASKED, advance(60),
      record(87.0, 10.0), ASKED, advance(4 * HOUR), ASKED]),
    ('only-a-week', 'An account whose only window is its week, said first',
     [{'record': {'limit_id': 'codex', 'primary': window(64.0, 10_080, WEEK_ON), 'secondary': None,
                  'plan_type': 'pro'}}, ASKED, advance(16 * 60), ASKED, advance(60),
      {'record': {'limit_id': 'codex', 'primary': window(64.5, 10_080, WEEK_ON + 45), 'secondary': None,
                  'plan_type': 'pro'}}, ASKED]),
    ('only-a-week-and-it-is-used-up', 'The only window is the week, and it is used up',
     [{'record': {'limit_id': 'codex', 'primary': window(100.0, 10_080, WEEK_ON), 'secondary': None,
                  'plan_type': 'pro'}}, ASKED]),
    ('used-up-and-a-turn-completed-since', 'Used up, and a turn of the tool completed after that was said',
     [record(100.0, 50.0), ASKED, advance(30), ended('codex'), ASKED]),
    ('used-up-and-a-turn-completed-in-the-same-moment', 'A turn that completed in the moment it was said proves nothing',
     [record(100.0, 50.0), ended('codex'), ASKED, advance(1), ASKED]),
    ('used-up-and-a-turn-of-another-tool-completed', 'A turn of another tool says nothing of this account',
     [record(100.0, 50.0), advance(30), ended('claude'), ASKED]),
    ('used-up-and-a-turn-ended-otherwise', 'A turn that was cut off or failed says nothing of it either',
     [record(100.0, 50.0), advance(30), ended('codex', 'interrupted'), ended('codex', 'error'), ASKED]),
    ('used-up-again-after-a-turn-completed', 'Used up, a turn completed, and it is said to be used up again',
     [record(100.0, 50.0), advance(30), ended('codex'), ASKED, advance(30), record(100.0, 50.0), ASKED]),
    ('both-used-up', 'The five hours and the week are both used up: the later reset counts',
     [record(100.0, 100.0), ASKED]),
    ('both-used-up-and-one-said-again-after-a-turn', 'Both used up, a turn completed, and only the week is said again',
     [record(100.0, 100.0), advance(30), ended('codex'), ASKED, advance(30), record(None, 100.0), ASKED]),
    ('used-up-and-grown-old', 'Used up, and nobody has heard of it for a quarter of an hour',
     [record(100.0, 50.0), advance(15 * 60), ASKED, advance(1), ASKED]),
    ('used-up-until-its-reset', 'Used up until the window begins anew',
     [record(100.0, 50.0, five_resets=BEGAN // 1000 + 300), advance(299), ASKED, advance(1), ASKED]),
    ('used-up-and-another-account-logged-in', 'Used up, and the Host is on another account',
     [record(100.0, 10.0), asked(OTHER), asked(IDENTITY), asked(RENEWED), asked(NO_LOGIN)]),
    ('over-all-of-it', 'More than all of a window is said to be used',
     [record(104.5, 50.0), ASKED]),
    ('used-up-and-said-again-in-the-moment-a-turn-completed', 'What is said in the moment a turn completed still counts',
     [record(100.0, 50.0), advance(30), ended('codex'), record(100.0, 50.0), ASKED]),
    ('both-used-up-and-one-said-again-in-the-moment-a-turn-completed',
     'Both used up, a turn completed, and the week is said again in that very moment: the week still counts',
     [record(100.0, 100.0), advance(30), ended('codex'), record(None, 100.0), ASKED]),
    ('used-up-twice-and-a-turn-completed-after-each', 'Used up, a turn completed, used up again, and a turn completed again',
     [record(100.0, 50.0), advance(30), ended('codex'), advance(30), record(100.0, 50.0), ASKED, advance(30),
      ended('codex'), ASKED]),
    ('refused', 'A turn fails for a usage limit, and nothing more is known',
     [refused(), ASKED, advance(15 * 60), ASKED, advance(1), ASKED]),
    ('refused-twice', 'A second turn fails the same way',
     [refused(), ASKED, advance(60), refused(), ASKED]),
    ('refused-and-a-turn-completed', 'A turn fails for a usage limit, a turn completes, and one fails again',
     [refused(), ASKED, advance(60), ended('codex'), ASKED, advance(60), refused(), ASKED]),
    ('refused-with-windows-known', 'A turn fails for a usage limit though the windows say there is room',
     [record(50.0, 20.0), advance(60), refused(), ASKED, advance(60), ended('codex'), ASKED]),
    ('refused-while-the-five-hours-are-used-up', 'A turn fails for a usage limit while the five hours are said to be used up',
     [record(100.0, 20.0), advance(60), refused(), ASKED, advance(60), ended('codex'), ASKED]),
    ('refused-and-a-record-under-the-warning', 'After a turn failed for a usage limit, a record says there is room',
     [refused(), advance(60), record(10.0, 10.0), ASKED]),
    ('refused-on-no-login', 'A turn fails for a usage limit while the Host is on no login it can read',
     [refused(on=NO_LOGIN), asked(NO_LOGIN), asked(IDENTITY), advance(60), ended('codex', on=IDENTITY),
      asked(NO_LOGIN), asked(IDENTITY)]),
    ('refused-and-another-login', 'A turn fails for a usage limit, and another account is logged in',
     [refused(), advance(60), record(20.0, 30.0, on=OTHER), asked(OTHER), asked(IDENTITY)]),
    ('refused-after-another-login', 'A warning stands, another account is logged in, and a turn of it fails for a usage limit',
     [record(90.0, 10.0), advance(60), refused(on=OTHER), asked(OTHER), asked(IDENTITY)]),
    ('refused-and-a-turn-of-another-login-completed', 'A turn fails for a usage limit, and a turn completes on another account',
     [refused(), advance(60), ended('codex', on=OTHER), asked(IDENTITY), asked(OTHER)]),
    ('refused-and-the-login-renewed', 'A turn fails for a usage limit, the login is renewed, and a turn completes',
     [refused(), advance(60), asked(RENEWED), ended('codex', on=RENEWED), asked(RENEWED)]),
    ('used-up-with-one-reset-unknown', 'Both used up, and one does not say when it begins anew',
     [{'record': {'primary': {'used_percent': 100.0, 'window_minutes': 300},
                  'secondary': window(100.0, 10_080, WEEK_ON)}}, ASKED]),
]


def told(story):
    """Runs in a process of its own: one story, from an empty store."""
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    root = pathlib.Path(tempfile.mkdtemp(prefix='clarp-codex-usage-vectors-', dir='/var/tmp'))
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    from lib import backend_usage as usage
    from lib import backend_usage_codex as codex
    from lib import backend_usage_store as store
    from lib import db
    store_of_all = db.conn()
    # Which Host this is, and the ids it draws: the same in every run, so that the same
    # stories give the same file (a Host draws its name, and an id for every stretch and every
    # event; here they are numbered in the order they are drawn).
    import uuid
    store._computer_id = lambda: 'a-host'
    drawn = iter(range(1, 1_000_000))
    uuid.uuid4 = lambda: uuid.UUID(int=next(drawn))
    # An agent of each tool, for the turns a story says ended.
    for tool in ('codex', 'claude'):
        store_of_all.execute(
            'INSERT INTO agents (agent_id, persona, voice_id, cwd, session, created_at, backend) VALUES (?,?,?,?,?,?,?)',
            (f'agent-{tool}', f'Of {tool}', '', '/', f'of-{tool}', BEGAN, tool))
    clock = [BEGAN]
    # The login the Host is on, as the story says it: no login is read.
    active = [tuple(IDENTITY)]
    usage._active_codex_identity = lambda *, create=False: active[0]

    # And so is the login a failed turn is recorded on: read by the Host's own reader, which
    # is given the story's word.
    def read_login(codex_home=None):
        if active[0] is None:
            raise RuntimeError('codex auth tokens missing')
        return 'a-token', 'an-account'
    codex._read_codex_auth = read_login
    codex._codex_identity = lambda token, account: active[0]
    # Every module of the Host that took the clock's reading by name is given this clock.
    for module in list(sys.modules.values()):
        if getattr(module, '__name__', '').startswith('lib.') and hasattr(module, '_now_ms'):
            module._now_ms = lambda: clock[0]
    name, what, steps = story
    done = []
    for step in steps:
        at = {'at': (clock[0] - BEGAN) // 1000}
        if 'advance' in step:
            clock[0] += step['advance'] * 1000
            continue
        if 'ended' in step or 'failed_for_a_limit' in step:
            if 'active' in step:
                active[0] = None if step['active'] is None else tuple(step['active'])
            at['active'] = None if active[0] is None else list(active[0])
        if 'ended' in step:
            how = step['ended']
            store_of_all.execute('INSERT INTO state_log (agent_id, runtime_id, ts, kind, detail) VALUES (?,?,?,?,?)',
                                 (f"agent-{how['tool']}", None, clock[0], how['kind'], '{}'))
            over = []
            if how['tool'] == 'codex' and how['kind'] == 'done':
                generation, account = active[0] or ('auth-unknown', None)
                over = store._resolve_unknown_terminals(
                    provider_instance_id=store._provider_instance_id('codex'), auth_generation_id=generation,
                    account_scope_ref=account, observed_at=clock[0])
            done.append({**at, 'ended': how, 'limit_events': over})
            continue
        if 'failed_for_a_limit' in step:
            limit = codex.record_classified_usage_limit('codex')
            its_own = None if limit is None else {key: value for key, value in limit.items() if not key.startswith('_')}
            new = bool(limit and limit.get('_new'))
            # As they are told: what ended under the login before first, then the limit where it is new.
            told_of = list((limit or {}).get('_additional_events') or []) + ([its_own] if new else [])
            done.append({**at, 'failed_for_a_limit': True, 'limit': its_own, 'new': new, 'limit_events': told_of})
            continue
        if 'record' in step:
            seen = step['record']
            identity = step.get('identity', IDENTITY)
            # The Host is on the login it sees a record on, until a story says otherwise.
            active[0] = tuple(identity)
            at['identity'] = identity
            payload = {'rate_limit': seen, 'plan_type': seen.get('plan_type')}
            try:
                at['snapshot'] = codex._codex_snapshot_from_payload(json.loads(json.dumps(payload)))
            except RuntimeError:
                at['snapshot'] = None
            try:
                kept = codex.capture_codex_rate_limits(payload, source_detail=SEEN_IN, identity=tuple(identity))
                did = {'limit_events': kept.get('limit_events') or []}
            except RuntimeError as refused:
                did = {'refused': str(refused)}
            done.append({**at, 'record': seen, **did})
            continue
        now = clock[0]
        if 'active' in step:
            active[0] = None if step['active'] is None else tuple(step['active'])
        closed = usage._reconcile_expired_limits(now)
        done.append({**at, 'asked': True, 'active': None if active[0] is None else list(active[0]),
                     'limit_events': closed,
                     'provider': usage._structured_provider('codex', now),
                     'row': store._response_row('codex', now),
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
    vectors = {'began_ms': BEGAN, 'identity': IDENTITY, 'seen_in': SEEN_IN, 'stories': stories}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(vectors, indent=1, ensure_ascii=False, sort_keys=True) + '\n')
    steps = sum(len(story['steps']) for story in stories)
    asked = sum(1 for story in stories for step in story['steps'] if step.get('asked'))
    events = sum(len(step.get('limit_events') or []) for story in stories for step in story['steps'])
    print(f'{len(stories)} stories, {steps} steps, {asked} askings, {events} limit events -> {OUT.relative_to(REPO)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
