#!/usr/bin/env python3
"""What the Python Host tells an app of the desk of a machine's account seats
(`server/lib/hotseat_desk.py` and `hotseat_desk_reading.py`, the answer of `GET /hotseat/desk`),
for stories of what the owner's tool answered and when an app asked.

A story is run through that Host's own desk (`HotseatDesk`), from an empty store, with the clock
held at each step's moment:

  read      a live read of the tool (`refresh`): the three askings (`tui --snapshot`,
            `list --json`, `bridge work`) are answered by what the step says, or fail as it says;
            what the read came to (`ok`, `failed`, `throttled`) is the vector
  asked     an app asks (`payload`), with or without `refresh=1`; the body is the vector, and
            with it what `GET /hotseat/status` says at that moment (a live read hands the tool's
            list to the status store)
  restart   the Host is started anew: what the process knew is gone, what is stored stays
  seats-*   what the switcher's own read, failure or run left in the status store (as in
            `seats-vectors.py`): what a desk that was never read falls back on

Beside the stories there are two tables: what kind of window a label of the tool's is
(`_window_meta`), and the command that takes a native session up again, by the name of its tool
(`_resume_command`).

It runs in processes of its own, one for each story, whose every home, data, config and cache
directory is a new empty directory (the replay harness's own isolation, `tests/replay/host.py`),
with no way out to any network. The owner's tool is never run: what it would answer is in the
stories, made up. What the Python Host answered is written, with the stories, to
`crates/clarp-kernel/tests/fixtures/desk-vectors.json`.

    nice "$(sed -n 1p ~/.local/share/clarp/current/SERVICE_PYTHON)" server-rs/tools/desk-vectors.py
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import scratch as tool_scratch

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = HERE.parent / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'desk-vectors.json'

T0 = 1_800_000_000_000
S0 = T0 / 1000
MINUTE = 60_000
HOUR_S = 3600


# ---- what the tool answers, made up -----------------------------------------------------------

def window(label, used, reset=S0 + 4 * HOUR_S):
    return {'label': label, 'used': used, 'reset': reset}


def seat(provider, alias, windows=(), **more):
    """An account as `hotseat tui --snapshot` reports it."""
    return {'provider': provider, 'alias': alias, 'display_alias': alias, 'aliases': [alias],
            'email': f'{alias}@example.invalid', 'plan': 'Team 5×', 'rate_limit_tier': 'default_claude_max_5x',
            'workspace': 'An Org', 'active': False, 'saved': True, 'can_switch': True, 'can_launch': True,
            'limited': False, 'cached': False, 'warning': '', 'profile_notes': [], 'reset_credits': None,
            'reset_credits_error': '', 'windows': list(windows), 'error': '', 'checked_at': S0,
            'no_reset_reason': '', **more}


def tui(*accounts, sessions=3, errors=(), **more):
    return {'generated_at': S0, 'sessions': sessions, 'errors': list(errors), 'accounts': list(accounts), **more}


def claude_row(alias, *, active=False, **more):
    """A row of an account of Claude in `hotseat list --json`."""
    usage = {'status': 'allowed', 'available': True, 'limited': False, 'used_5h': 0.25, 'reset_5h': S0 + 4 * HOUR_S,
             'used_7d': 0.4, 'reset_7d': S0 + 4 * HOUR_S, 'scoped': [], 'blocked_models': [],
             'extra_usage_enabled': False, 'extra_usage_reason': 'out_of_credits'}
    usage.update(more.pop('usage', {}))
    return {'alias': alias, 'email': f'{alias}@example.invalid', 'org': 'An Org', 'plan': 'team', 'plan_label': 'Team 5×',
            'rate_limit_tier': 'default_claude_max_5x', 'is_active': active, 'access_expires_at': T0 + 6 * 3600 * 1000,
            'refresh_expires_at': T0 + 25 * 86400 * 1000, 'access_hours_left': 6.0, 'signin_days_left': 25.0,
            'signin_due_soon': False, 'error': None, 'usage': usage, **more}


def codex_row(alias, windows=(), *, active=False, **more):
    """A row of an account of Codex in the `codex` part of `hotseat list --json`."""
    usage = {'usable': True, 'blocked': False, 'error': None, 'worst_used': 0.1, 'windows': list(windows)}
    usage.update(more.pop('usage', {}))
    return {'provider': 'codex', 'alias': alias, 'is_active': active, 'saved': True, 'email': f'{alias}@example.invalid',
            'plan': 'plus', 'account_id': f'ws-{alias}', 'token_expires_at': int(S0 + 4 * HOUR_S),
            'token_hours_left': 4.0, 'api_key_present': False, 'usage': usage, **more}


def listed(claude=(), codex=(), clarp=None, **more):
    said = {'generated_at': S0, 'accounts': list(claude),
            'codex': {'quota_read': True, 'quota_age_s': 0, 'accounts': list(codex)}, **more}
    if clarp is not None:
        said['extensions'] = {'clarp': clarp}
    return said


def stopped(item_id, **more):
    return {'kind': 'clarp', 'cause': 'usage_limit', 'pending': 0, 'id': item_id, 'name': item_id.capitalize(),
            'backend': 'claude', 'model': 'claude-fable-4-7', 'cwd': '/work/repo', 'last_message': 'Hit usage limit',
            'stopped_at': S0 - 120, 'readiness': {'ready': True, 'hard': None, 'warning': 'ai13 could serve it'}, **more}


def work(item_id, **more):
    return {'id': item_id, 'provider': 'codex', 'title': 'Fix parser', 'state': 'failed', 'updated': S0 - 60,
            'cwd': '/work/repo', 'live': False, 'can_resume': True, 'can_reboot': False, 'reason': '', **more}


def says(answer):
    return {'says': answer}


def read(at, tui_said, list_said=None, work_said=None, **more):
    """A live read at a moment: each of the three askings answers what is given (a dict), or
    fails as a `{'fails': ...}` or `{'printed': ..., 'exit': ...}` says."""
    def one(said, empty):
        if said is None:
            return says(empty)
        return said if set(said) & {'fails', 'printed'} else says(said)
    return {'op': 'read', 'at': at, 'tui': one(tui_said, tui()), 'list': one(list_said, listed()),
            'work': one(work_said, {'items': []}), **more}


def asked(at, *, installed=True, refresh=None):
    """An app asks at a moment; with `refresh`, a live read is asked for first (a `read` step's
    answers)."""
    step = {'op': 'asked', 'at': at, 'installed': installed, 'refresh': refresh is not None}
    if refresh is not None:
        step.update({name: refresh[name] for name in ('tui', 'list', 'work')})
    return step


def seats_read(provider, accounts, at=T0, **more):
    return {'op': 'seats-read', 'provider': provider, 'received_at': at, 'payload': {'accounts': accounts, **more}}


def seats_did(provider, decision, at, *, mode='automatic', switched=True, floor=25, cadence=300):
    return {'op': 'seats-action', 'provider': provider, 'decision': decision, 'mode': mode, 'switched': switched,
            'now': at, 'floor_pct': floor, 'cadence_s': cadence, 'next_due_at': at + cadence * 1000}


def story(name, steps, *, enabled=False):
    return {'story': name, 'janitor_enabled': enabled, 'steps': steps}


FAILS = {'fails': 'timeout'}
MISSING = {'fails': 'missing'}
FIVE = 'All models · 5-hour'
WEEK = 'All models · weekly'
FABLE = 'Fable · weekly'
CODEX_WEEK = 'codex primary weekly'

# The made-up answers of that Host's own test of its desk (tests/unit/test_hotseat_desk.py,
# _sample_payloads), in this script's words.
SAMPLE_TUI = tui(
    seat('claude', 'ai13', [window(FIVE, 0.25), window(WEEK, 0.4), window(FABLE, 0.6)], active=True),
    seat('claude', 'ai8', profile_notes=['Access token expired; sign-in needed'], checked_at=0,
         error='Access token expired; refresh token revoked, sign-in needed'),
    seat('codex', 'codex-main', [window(CODEX_WEEK, 0.1)], active=True, plan='self_serve_business_prolite',
         rate_limit_tier='', workspace='ws-123', reset_credits=2,
         profile_notes=['OpenAI reports no separate Spark allowance for this account.']))
SAMPLE_LIST = listed(
    claude=[claude_row('ai13', active=True, usage={'scoped': [{'model': 'Fable', 'used': 0.6, 'reset': S0 + 4 * HOUR_S,
                                                                 'exhausted': False}]}),
            {'alias': 'ai8', 'email': 'ai8@example.invalid', 'org': 'An Org', 'plan': 'team', 'plan_label': 'Team 5×',
             'is_active': False, 'error': 'Access token expired', 'error_code': 'auth_error', 'usage': None}],
    codex=[codex_row('codex-main', [window(CODEX_WEEK, 0.1)], active=True, account_id='ws-123')],
    clarp={'agents': {'total': 12, 'live': 2, 'claude_backed': 10, 'by_backend': {'claude': 10, 'codex': 2},
                      'live_by_account': {'ai13': 2},
                      'agents': [{'session': 'alpha', 'would_use': 'ai13'}, {'session': 'beta', 'would_use': 'ai13'},
                                 {'session': 'gamma', 'would_use': 'ai8'}]},
           'stopped': [stopped('koko')]})
SAMPLE_WORK = {'items': [work('thread-001')]}
SAMPLE = read(T0, SAMPLE_TUI, SAMPLE_LIST, SAMPLE_WORK)


def later(step, at):
    return {**step, 'at': at}


STORIES = [
    story('the-sample-read-then-asked', [
        asked(T0 - 1000, installed=False),
        SAMPLE,
        asked(T0 + 1000),
        asked(T0 + 30_000, refresh=later(SAMPLE, T0 + 30_000)),
        asked(T0 + 61_000, refresh=read(T0 + 61_000, tui(
            seat('claude', 'ai13', [window(FIVE, 0.5), window(WEEK, 0.45)], active=True, checked_at=S0 + 61),
            sessions=4), SAMPLE_LIST, SAMPLE_WORK)),
        asked(T0 + 61_000 + 10 * MINUTE),
        asked(T0 + 61_001 + 10 * MINUTE),
    ]),
    story('the-sample-with-the-switcher-on', [SAMPLE, asked(T0 + 1000), asked(T0 + 20 * MINUTE)], enabled=True),
    story('the-states-of-an-account', [
        read(T0, tui(
            seat('claude', 'well', [window(FIVE, 0.25), window(WEEK, 0.4)], active=True),
            seat('claude', 'out-of-five', [window(FIVE, 1.0), window(WEEK, 0.4)]),
            seat('claude', 'out-with-extra', [window(FIVE, 1.0), window(WEEK, 0.4)]),
            seat('claude', 'a-model-out', [window(FIVE, 0.2), window(FABLE, 1.0), window('Opus · 5-hour', 1.0)]),
            seat('claude', 'said-limited', [window(FIVE, 0.2)], limited=True),
            seat('claude', 'login-gone', [window(FIVE, 0.2)], error='Access token expired; refresh token revoked, sign-in needed',
                 checked_at=0),
            seat('claude', 'said-no', [window(FIVE, 0.2)], error='the provider said no'),
            seat('claude', 'said-no-typed', [window(FIVE, 0.2)], error='HTTP 429'),
            seat('claude', 'typed-login', [window(FIVE, 0.2)]),
            seat('claude', 'typed-other', [window(FIVE, 0.2)]),
            seat('claude', 'nothing-known'),
            seat('claude', 'no-number', [window(FIVE, None), window(WEEK, 'half')]),
            seat('claude', 'kept-reading', [window(FIVE, 0.3)], cached=True),
            seat('claude', 'token-word-only', [window(FIVE, 0.2)], error='a token of something'),
            seat('claude', 'Token Revoked', [window(FIVE, 0.2)], error='TOKEN REVOKED'),
            seat('claude', 'token-expired', [window(FIVE, 0.2)], error='Token expired'),
            seat('claude', 'token-sign-in', [window(FIVE, 0.2)], error='token: sign-in needed'),
            seat('claude', 'expired-and-no-token', [window(FIVE, 0.2)], error='the session expired'),
            seat('claude', 'out-with-extra-said-oddly', [window(FIVE, 1.0), window(WEEK, 0.4)]),
            seat('claude', 'limited-with-extra', [window(FIVE, 0.2)], limited=True),
            seat('claude', 'typed-oddly', [window(FIVE, 0.2)], error='HTTP 500'),
            seat('claude', 'typed-with-nothing', [window(FIVE, 0.2)], error='HTTP 500'),
        ), listed(claude=[
            claude_row('well', active=True),
            claude_row('out-with-extra', usage={'extra_usage_enabled': True, 'extra_usage_reason': ''}),
            claude_row('a-model-out', usage={'blocked_models': ['fable', ' ', 7, 'opus']}),
            claude_row('said-no-typed', error='HTTP 429', error_code='rate_limited'),
            claude_row('typed-login', error_code='auth_error'),
            claude_row('typed-other', error_code='rate_limited'),
            claude_row('out-with-extra-said-oddly', usage={'extra_usage_enabled': 'yes'}),
            claude_row('limited-with-extra', usage={'extra_usage_enabled': True}),
            claude_row('typed-oddly', error_code=7),
            claude_row('typed-with-nothing', error_code=''),
        ])),
        asked(T0 + 1000),
    ]),
    story('the-windows-of-codex-and-of-a-tool-without-a-reading', [
        read(T0, tui(
            seat('codex', 'main', [window(CODEX_WEEK, 0.1), window('codex primary 5-hour', 0.2),
                                   window('codex primary 30-day', 0.3), window('codex spark weekly', 0.4),
                                   window('codex spark 5h', 1.0), window('gpt-5 weekly', 1.0)], active=True, reset_credits=2),
            seat('codex', 'blocked-by-the-tool', [window(CODEX_WEEK, 0.2)]),
            seat('codex', 'thirty-full', [window('codex primary 30-day', 1.0)], no_reset_reason=' no credits '),
            seat('codex', 'credits-yes', [window(CODEX_WEEK, 0.2)], reset_credits=True),
            seat('codex', 'credits-text', [window(CODEX_WEEK, 0.2)], reset_credits='2', reset_credits_error='could not be read'),
            seat('codex', 'credits-none', [window(CODEX_WEEK, 0.2)], reset_credits=0),
            seat('codex', 'credits-and-a-half', [window(CODEX_WEEK, 0.2)], reset_credits=2.5),
            seat('codex', 'from-the-list', [window(CODEX_WEEK, 0.2)], email='', plan='', workspace='', rate_limit_tier=''),
            seat('muse', 'meta', [window('All models · 5-hour', 0.5), window('A window with a name longer than four and twenty', 1.0),
                                  window('', 0.1)], active=True, plan='', rate_limit_tier=''),
        ), listed(codex=[
            codex_row('main', [window(CODEX_WEEK, 0.1)], active=True, plan='Plus', token_hours_left=3.14159,
                      api_key_present=True),
            codex_row('blocked-by-the-tool', usage={'blocked': True}),
            {'provider': 'codex', 'alias': 'credits-yes', 'usage': None},
            codex_row('from-the-list', plan='Pro', account_id='ws-listed', email='listed@example.invalid'),
        ])),
        asked(T0 + 1000),
    ]),
    story('what-is-said-oddly-of-an-account', [
        read(T0, tui(
            seat(' Claude ', ' spaced ', [window(' All models · 5-hour ', 1.2), window(WEEK, 1.6), window(FABLE, -0.1),
                                          window('Sonnet · weekly', True), window('Haiku · 5h', 0.33335),
                                          window('5-hour', 0.5, reset=0), window('weekly', 0.5, reset=-5),
                                          'not a window', window(None, 0.5, reset=None)], active=1),
            seat('claude', ''), seat('', 'nameless-tool'), 'not an account', None,
            {'provider': 'claude', 'alias': 'bare'},
            {'provider': 'claude', 'alias': 'saved-not', 'saved': None, 'aliases': [7, 'saved-not', 'other'],
             'display_alias': '', 'profile_notes': ['a note', ' ', 3], 'checked_at': 'yesterday', 'email': None,
             'plan': None, 'workspace': None, 'rate_limit_tier': None, 'warning': None, 'windows': None},
            seat('claude', 'from-the-list', email='', plan='', workspace='', rate_limit_tier=''),
            seat('claude', 'twice-in-the-list', plan=''),
            seat('claude', 'hours-and-days'),
            seat('claude', 'no-usage'),
            seat('codex', 'no-row'),
            seat('codex', 'seconds', checked_at=S0 + 0.0005),
        ), listed(claude=[
            claude_row('spaced', signin_days_left=3.0),
            claude_row('from-the-list', plan='TEAM', plan_label='Team 20×', rate_limit_tier='a-tier', org='Another Org',
                       email='listed@example.invalid'),
            claude_row('twice-in-the-list', plan_label='First'),
            claude_row('twice-in-the-list', plan_label='Second'),
            'not a row', {'alias': 7}, {'email': 'no-alias@example.invalid'},
            claude_row('hours-and-days', access_hours_left=6.126, signin_days_left=25.06, signin_due_soon=1,
                       access_expires_at=T0 + 0.6, refresh_expires_at=0, error_code=7,
                       usage={'extra_usage_enabled': None, 'extra_usage_reason': None}),
            {'alias': 'no-usage', 'usage': None, 'signin_due_soon': None},
        ], codex=[
            codex_row('seconds', token_expires_at=S0 + 0.0015, token_hours_left=True, plan=None, account_id=None,
                      email=None),
        ])),
        asked(T0 + 1000),
    ]),
    story('the-list-says-less', [
        read(T0, tui(seat('claude', 'ai13', [window(FIVE, 0.25)], active=True),
                     seat('codex', 'main', [window(CODEX_WEEK, 0.1)], active=True)),
             {'generated_at': S0, 'accounts': [claude_row('ai13', active=True)]}),
        asked(T0 + 1000),
        read(T0 + 61_000, tui(seat('claude', 'ai13', [window(FIVE, 0.25)], active=True, checked_at=S0 + 61),
                              seat('codex', 'main', [window(CODEX_WEEK, 0.1)], active=True, checked_at=S0 + 61)),
             {'generated_at': S0, 'accounts': None, 'codex': 'nothing', 'extensions': 'none'}),
        asked(T0 + 62_000),
        read(T0 + 122_000, tui(seat('claude', 'ai13', [window(FIVE, 0.25)], active=True, checked_at=S0 + 122)),
             {'generated_at': S0, 'extensions': {'clarp': 'none'}}),
        asked(T0 + 123_000),
        read(T0 + 183_000, tui(seat('claude', 'ai13', [window(FIVE, 0.25)], active=True, checked_at=S0 + 183)), {}),
        asked(T0 + 184_000),
    ], enabled=True),
    story('the-agents-and-the-stopped-work', [
        read(T0, tui(seat('claude', 'ai13', [window(FIVE, 0.25)], active=True), seat('claude', 'ai8'),
                     seat('codex', 'main', [window(CODEX_WEEK, 0.1)], active=True)),
             listed(claude=[claude_row('ai13', active=True)], clarp={
                 'agents': {'total': 12.9, 'live': None, 'claude_backed': True,
                            'by_backend': {'claude': 10, 'codex': True, 'agy': 1.5, 'grok': 'two'},
                            'live_by_account': {'ai13': 3, 'ai8': False, 'main': '1'},
                            'agents': [{'session': 'alpha', 'would_use': 'ai13'}, {'session': 'beta', 'would_use': 'main'},
                                       {'session': 'gamma', 'would_use': 'ai13'}, {'session': 'delta', 'would_use': ''},
                                       {'session': 'eps', 'would_use': 7}, 'not an agent', {'session': 'zeta'}]},
                 'stopped': [
                     stopped('koko'),
                     stopped('a-native-claude', kind='native', backend='claude', name=None),
                     stopped('a-native-codex', kind='native', backend='codex', cause='  waiting_for_account  '),
                     stopped('a-native-other', kind='native', backend='agy'),
                     stopped('a-native-nameless', kind='native', backend=None),
                     stopped('of-another-kind', kind='other', cause=7),
                     stopped('', name='No id'), stopped('   ', name='Blank id'), 'not an item',
                     stopped(' spaced-id ', readiness='ready', pending=None, stopped_at=0, model=None, cwd=None,
                             last_message=None, account='ai13'),
                     stopped('not-ready', readiness={'ready': 0, 'hard': 'No account has quota', 'warning': ''}),
                     stopped('ready-oddly', readiness={'ready': 'yes', 'hard': 7, 'warning': ['x']}, pending=3, cause=''),
                     stopped('no-readiness', readiness=None, kind=None),
                 ]}),
             {'items': [
                 work('thread-001'), work('thread-live', live=True), work('thread-done', can_resume=False, state=None),
                 work('thread-claude', provider='claude'), work('thread-other', provider='agy'),
                 work('thread-nobody', provider=None, updated=0, title=None, cwd=None, reason=None, can_reboot=1),
                 work(''), 'not an item', work('  spaced  '),
                 work('thread-numbered', title=7, state=3.5, reason=True),
             ]}),
        asked(T0 + 1000),
    ]),
    story('more-work-than-is-shown', [
        read(T0, tui(), None, {'items': ['not an item', work(''), *[work(f'thread-{n:03d}') for n in range(45)]]}),
        asked(T0 + 1000),
        read(T0 + 61_000, tui(), listed(clarp={'agents': 'none', 'stopped': 'none'}), {'items': None}),
        asked(T0 + 62_000),
        read(T0 + 122_000, tui(), listed(clarp={}), {}),
        asked(T0 + 123_000),
    ]),
    story('what-the-snapshot-says-of-itself', [
        read(T0, tui(seat('claude', 'ai13', [window(FIVE, 0.25)], active=True), sessions=True,
                     errors=['Muse: not signed in', ' ', 7, 'Codex: quota could not be read'])),
        asked(T0 + 1000),
        read(T0 + 61_000, {'accounts': [seat('claude', 'ai13', [window(FIVE, 0.25)], checked_at=S0 + 61)], 'sessions': '4',
                           'errors': None}),
        asked(T0 + 62_000),
        read(T0 + 122_000, {'sessions': 7.0}),
        asked(T0 + 123_000),
    ]),
    story('a-read-that-fails-and-how-soon-the-next-may-be', [
        read(T0, FAILS),
        asked(T0 + 1000),
        read(T0 + 5000, SAMPLE_TUI),
        read(T0 + 9999, SAMPLE_TUI),
        read(T0 + 10_000, {'printed': '', 'exit': 1}),
        asked(T0 + 11_000),
        read(T0 + 20_000, {'printed': 'not json', 'exit': 0}),
        asked(T0 + 21_000),
        read(T0 + 30_000, {'printed': '[1]', 'exit': 0}),
        asked(T0 + 31_000),
        read(T0 + 40_000, MISSING),
        asked(T0 + 41_000, installed=False),
        read(T0 + 50_000, SAMPLE_TUI, SAMPLE_LIST, SAMPLE_WORK),
        asked(T0 + 51_000),
        read(T0 + 60_000, SAMPLE_TUI),
        read(T0 + 109_999, SAMPLE_TUI),
        read(T0 + 110_000, FAILS, SAMPLE_LIST, SAMPLE_WORK),
        asked(T0 + 111_000),
        read(T0 + 119_999, SAMPLE_TUI),
        read(T0 + 120_000, {'printed': '{"accounts": [}', 'exit': 0}),
        asked(T0 + 121_000),
        read(T0 + 130_000, SAMPLE_TUI, SAMPLE_LIST, SAMPLE_WORK),
        asked(T0 + 131_000),
    ], enabled=True),
    story('a-part-of-a-read-fails', [
        read(T0, SAMPLE_TUI, FAILS, SAMPLE_WORK),
        asked(T0 + 1000),
        asked(T0 + 61_000, refresh=read(T0 + 61_000, SAMPLE_TUI, SAMPLE_LIST, {'printed': 'no', 'exit': 3})),
        asked(T0 + 122_000, refresh=read(T0 + 122_000, SAMPLE_TUI, {'printed': '"a text"', 'exit': 0}, MISSING)),
        asked(T0 + 183_000, refresh=read(T0 + 183_000, SAMPLE_TUI, SAMPLE_LIST, SAMPLE_WORK)),
        asked(T0 + 244_000, refresh=read(T0 + 244_000, FAILS, FAILS, FAILS)),
        asked(T0 + 250_000, refresh=read(T0 + 250_000, SAMPLE_TUI, SAMPLE_LIST, SAMPLE_WORK)),
        asked(T0 + 254_000, refresh=read(T0 + 254_000, SAMPLE_TUI, SAMPLE_LIST, SAMPLE_WORK)),
    ], enabled=True),
    story('the-tool-prints-nothing', [
        read(T0, {'printed': '', 'exit': 0}, {'printed': '', 'exit': 0}, {'printed': '', 'exit': 0}),
        asked(T0 + 1000),
        read(T0 + 61_000, {'printed': '  {"sessions": 2}\n', 'exit': 0}, {'printed': 'null', 'exit': 0},
             {'printed': '7', 'exit': 0}),
        asked(T0 + 62_000),
    ]),
    story('the-host-is-started-anew', [
        SAMPLE,
        {'op': 'restart', 'at': T0 + 20_000},
        asked(T0 + 30_000, refresh=later(SAMPLE, T0 + 30_000)),
        asked(T0 + 59_999, refresh=later(SAMPLE, T0 + 59_999)),
        asked(T0 + 60_000, refresh=later(SAMPLE, T0 + 60_000)),
        read(T0 + 120_000, FAILS),
        read(T0 + 125_000, SAMPLE_TUI),
        {'op': 'restart', 'at': T0 + 126_000},
        asked(T0 + 127_000),
        read(T0 + 128_000, FAILS),
        asked(T0 + 129_000),
        read(T0 + 130_000, SAMPLE_TUI),
        {'op': 'restart', 'at': T0 + 131_000},
        read(T0 + 132_000, SAMPLE_TUI),
        asked(T0 + 133_000),
    ]),
    story('a-failed-first-read-on-a-host-started-anew', [
        read(T0, FAILS),
        {'op': 'restart', 'at': T0 + 1000},
        asked(T0 + 2000),
        read(T0 + 3000, SAMPLE_TUI, SAMPLE_LIST, SAMPLE_WORK),
        asked(T0 + 4000),
    ]),
    story('when-an-account-was-last-known-good', [
        read(T0, tui(seat('claude', 'ai13', [window(FIVE, 0.25)], active=True),
                     seat('codex', 'ai13', [window(CODEX_WEEK, 0.1)], checked_at=S0 - 30),
                     seat('claude', 'never-good', error='the provider said no', checked_at=0),
                     seat('claude', 'twice', checked_at=S0 - 50), seat('claude', 'twice', checked_at=S0 - 40))),
        asked(T0 + 1000),
        read(T0 + 61_000, tui(seat('claude', 'ai13', [window(FIVE, 0.9)], active=True, error='the provider said no',
                                   checked_at=0),
                              seat('codex', 'ai13', [window(CODEX_WEEK, 0.2)], checked_at=S0 + 61),
                              seat('claude', 'never-good', error='the provider said no', checked_at=0),
                              seat('claude', 'twice', error='HTTP 500', checked_at=0),
                              seat('claude', 'new-and-failing', error='HTTP 500', checked_at=S0 + 61))),
        asked(T0 + 62_000),
        read(T0 + 122_000, tui(seat('claude', 'ai13', [window(FIVE, 0.9)], active=True, error='the provider said no',
                                    checked_at=0),
                               seat('codex', 'ai13', error='HTTP 500', checked_at=0))),
        asked(T0 + 123_000),
        read(T0 + 183_000, tui(seat('claude', 'ai13', [window(FIVE, 0.5)], active=True, checked_at=S0 + 183))),
        asked(T0 + 184_000),
    ]),
    story('a-window-begins-anew-while-nobody-reads', [
        read(T0, tui(
            seat('claude', 'out', [window(FIVE, 1.0, reset=S0 + 300), window(WEEK, 0.4, reset=S0 + 86_400)], limited=True),
            seat('claude', 'out-twice', [window(FIVE, 1.0, reset=S0 + 300), window(WEEK, 1.0, reset=S0 + 86_400)]),
            seat('claude', 'a-model-began-anew', [window(FIVE, 1.0, reset=S0 + 86_400), window(FABLE, 1.0, reset=S0 + 300)]),
            seat('codex', 'no-reset', [window(CODEX_WEEK, 1.0, reset=S0 + 300)], no_reset_reason='spend cap'),
            seat('claude', 'well', [window(FIVE, 0.3, reset=S0 + 300)], active=True),
            seat('claude', 'checked-after-its-reset', [window(FIVE, 1.0, reset=S0 - 60)]),
            seat('claude', 'checked-at-its-reset', [window(FIVE, 1.0, reset=S0)]),
            seat('claude', 'nobody-knows-when', [window(FIVE, 1.0, reset=S0 + 300)], checked_at=0),
            seat('claude', 'old-check', [window(FIVE, 0.5, reset=S0 + 86_400)], checked_at=S0 - 601),
            seat('claude', 'old-check-at-the-edge', [window(FIVE, 0.5, reset=S0 + 86_400)], checked_at=S0 - 300),
        )),
        asked(T0),
        asked(T0 + 299_999),
        asked(T0 + 300_000),
        asked(T0 + 300_001),
        asked(T0 + 86_400_000),
    ]),
    story('a-desk-that-was-never-read-says-what-the-switcher-read', [
        seats_read('claude', [claude_row('work', active=True, plan='max', plan_label='Max 5×'),
                              claude_row('sam@example.invalid', usage={'used_5h': 1.0, 'limited': True}),
                              claude_row('gone', error='Access token expired', error_code='auth_error')]),
        seats_read('codex', [codex_row('main', [window(CODEX_WEEK, 0.4, reset=S0 + 86_400)], active=True)], at=T0 + 5000),
        seats_did('claude', {'action': 'noop', 'active': 'work', 'remaining': 75}, T0 + 6000),
        asked(T0 + 7000),
        asked(T0 + 7000, installed=False),
        asked(T0 + 30 * MINUTE),
        SAMPLE | {'at': T0 + 31 * MINUTE},
        asked(T0 + 31 * MINUTE + 1000),
    ], enabled=True),
    story('a-desk-that-was-never-read-and-a-switcher-that-is-off', [
        seats_read('claude', [claude_row('work', active=True)]),
        asked(T0 + 1000),
        {'op': 'seats-failure', 'provider': 'codex', 'error': 'timeout', 'now': T0 + 2000},
        asked(T0 + 3000),
    ]),
]

# The labels the tool gives its windows, and labels like them: what kind each is taken for.
LABELS = [
    FIVE, WEEK, FABLE, 'Opus · weekly', 'Sonnet · weekly', 'Haiku · weekly', 'Opus · 5-hour', 'Fable · 5h',
    'ALL MODELS · 5-HOUR', 'all models · weekly', 'All models · monthly', 'All models', '5-hour', '5h', 'weekly',
    'Fable 5h', 'Fable weekly', 'fable', 'Opus and Sonnet · weekly', 'Design · weekly', 'A model · 5-hour',
    CODEX_WEEK, 'codex primary 5-hour', 'codex primary 30-day', 'codex secondary weekly', 'codex spark weekly',
    'codex spark 5h', 'codex spark 5-hour', 'Codex · Primary · Weekly', 'codex', 'codex primary', 'codex daily',
    'gpt-5 weekly', 'gpt-5 5-hour', '30-day', 'spark', 'Spark 30-day', 'claude weekly', 'claude 5h', 'muse weekly',
    'codex·primary·weekly', 'codex·weekly', 'codex·primary weekly', 'codex 30-day weekly',
    'muse', '', '   ', '  codex primary weekly  ', 'A window with a name longer than four and twenty signs',
    'Ünïcödé · Wïndöw · Náme · Lönger · Thán · Föur', 'codex 5h 30-day weekly',
]
TOOLS = ['claude', 'codex', 'muse', 'agy']

# The names a session's tool is given by: which of them has a command that takes a session up.
NAMES = ['claude', 'codex', 'agy', 'gemini', 'grok', 'opencode', 'echo', 'muse', '', 'Claude', 'CODEX', ' claude',
         'claude-code', 'antigravity', 'openai', 'anthropic']


@tool_scratch.case('clarp-desk-vectors-')
def told(root, name: str) -> dict:
    """Runs in a process of its own: one story, from an empty store."""
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    from lib import db
    from lib import hotseat_desk as desk
    from lib import hotseat_status as status
    db.conn()
    if name == '--tables':
        return {
            'windows': [{'tool': tool, 'label': label, 'is': list(desk._window_meta(tool, label))}
                        for tool in TOOLS for label in LABELS],
            'commands': [{'tool': tool, 'command': desk._resume_command(tool, 'a-session')} for tool in NAMES],
        }
    scenario = next(one for one in STORIES if one['story'] == name)
    clock = {'at': 0}
    desk.now_ms = lambda: clock['at']
    status.now_ms = lambda: clock['at']
    status.janitor_enabled = lambda: scenario['janitor_enabled']

    def runner_of(step):
        asking = {('tui', '--snapshot'): 'tui', ('list', '--json'): 'list', ('bridge', 'work'): 'work'}

        def run(argv, **_):
            said = step[asking[tuple(argv[1:])]]
            if said.get('fails') == 'timeout':
                raise subprocess.TimeoutExpired(argv, 45)
            if said.get('fails') == 'missing':
                raise FileNotFoundError(2, 'No such file or directory', argv[0])
            printed = json.dumps(said['says']) if 'says' in said else said['printed']
            return subprocess.CompletedProcess(argv, said.get('exit', 0), stdout=printed, stderr='it said why\n')
        return run

    errors = {'timeout': lambda: subprocess.TimeoutExpired(['hotseat'], 60), 'cli_failed': lambda: RuntimeError('exit 1'),
              'parse_failed': lambda: ValueError('bad json'), 'provider_error': lambda: KeyError('x')}
    the_desk = desk.HotseatDesk()
    steps = []
    for step in scenario['steps']:
        step = dict(step)
        clock['at'] = step.get('at', clock['at'])
        if step['op'] == 'read':
            step['outcome'] = the_desk.refresh(command='hotseat', runner=runner_of(step))
        elif step['op'] == 'asked':
            which = (lambda _command: '/usr/bin/hotseat') if step['installed'] else (lambda _command: None)
            step['body'] = the_desk.payload(refresh=step['refresh'], command='hotseat', which=which,
                                            runner=runner_of(step) if step['refresh'] else None)
            step['status'] = status.payload(served_at=clock['at'])
        elif step['op'] == 'restart':
            the_desk = desk.HotseatDesk()
        elif step['op'] == 'seats-read':
            status.record_read(step['provider'], step['payload'], step['received_at'])
        elif step['op'] == 'seats-action':
            status.record_action(step['provider'], step['decision'], mode=step['mode'], switched=step['switched'],
                                 now=step['now'], floor_pct=step['floor_pct'], cadence_s=step['cadence_s'],
                                 next_due_at=step['next_due_at'])
        elif step['op'] == 'seats-failure':
            status.record_failure(step['provider'], errors[step['error']](), now=step['now'])
        else:
            raise SystemExit(f'a step of no kind: {step["op"]}')
        steps.append(step)
    return {**scenario, 'steps': steps}


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == '--story':
        sys.stdout.write('\nVECTORS ' + json.dumps(told(sys.argv[2])) + '\n')
        return 0
    names = [one['story'] for one in STORIES]
    if len(set(names)) != len(names):
        sys.stderr.write('two stories of one name\n')
        return 1
    answered = {}
    for name in [*names, '--tables']:
        run = subprocess.run([sys.executable, __file__, '--story', name], capture_output=True, text=True)
        lines = [line for line in run.stdout.splitlines() if line.startswith('VECTORS ')]
        if run.returncode or not lines:
            sys.stderr.write(f'{name}: it did not run to its end\n{run.stderr[-3000:]}\n')
            return 1
        answered[name] = json.loads(lines[-1][len('VECTORS '):])
    tables = answered.pop('--tables')
    stories = [answered[name] for name in names]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({'stories': stories, **tables}, indent=1, ensure_ascii=False, sort_keys=True) + '\n')
    steps = [step for one in stories for step in one['steps']]
    reads = [step for step in steps if step['op'] == 'read' or step.get('refresh')]
    bodies = [step['body'] for step in steps if step['op'] == 'asked']
    print(f'{len(stories)} stories, {len(steps)} steps, {len(reads)} reads asked for, {len(bodies)} askings, '
          f'{sum(len(body["accounts"]) for body in bodies)} accounts served, {len(tables["windows"])} labels, '
          f'{len(tables["commands"])} names -> {OUT.relative_to(REPO)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
