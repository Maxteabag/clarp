#!/usr/bin/env python3
"""Writes the vectors the Rust Host's contacts are tested against: who an agent is when nobody
named it (feature `auto_contact_minting`, contract 85).

An agent made with `auto_contact` takes the first free contact of its tool's pool, and a new one
is minted when the pool is empty; one made with `mint_contact` always takes a new one;
`POST /agent-assign` gives a running agent a contact. A minted contact is a persona of the
owner's with a curated given name of its tool's tier, a voice nobody speaks with and one of the
tier's ways of speaking. Nothing outside is asked for any of it (the picture the Python Host
asks OpenAI for afterwards is not built in the Rust Host: docs/differences.md).

This runs the Python Host's own functions (`server/lib/contact_minting.py`,
`contact_assignment.py`, `roster.py`, `policies/agent_spec.py`) on made-up rosters and writes
what they answered to `crates/clarp-kernel/tests/fixtures/contact-vectors.json`, with the
tiers' names and ways of speaking to `crates/clarp-kernel/src/contact-tiers.json`.

    "$(sed -n 1p ~/.local/share/clarp/current/SERVICE_PYTHON)" server-rs/tools/contact-vectors.py

It runs in a process of its own whose every home, data, config and cache directory is a new
empty directory (the replay harness's own isolation, `tests/replay/host.py`), with no way out of
the machine; the database it touches is a new one there. No Host is touched or started. The
agents of a roster stand in for the table they are kept in (the functions read them through
`agents.list_agents`); the personas are written to the new database as the Host writes them.
The same file at every run.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import shutil
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import scratch as tool_scratch

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = HERE.parent / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'contact-vectors.json'
TIERS = HERE.parent / 'crates' / 'clarp-kernel' / 'src' / 'contact-tiers.json'

BACKENDS = ['claude', 'codex', 'agy', 'gemini', 'grok', 'opencode', 'deepseek', 'muse', '', 'nobody', 'CODEX',
            ' codex ']
# Which tool a contact may run on: a name of the roster, a minted one, one with an initial or a
# number after it, one of no tier, and a row's own tier where it has one.
ALLOWED = [(name, backend, tier)
           for name in ['Mike', 'Clara', 'clara b', 'Soren 12', 'Nobody', 'Codex', 'Astra']
           for backend in ['claude', 'codex', 'agy', 'grok', 'deepseek', ' CODEX']
           for tier in ['', 'grok']]
PERSONALITIES = [('Clara', 'claude'), ('clara', 'claude'), (' Clara ', 'claude'), ('Clara B', 'claude'),
                 ('Soren', 'codex'), ('Soren 12', 'codex'), ('Zoë Å', 'grok'), ('Anyone', 'gemini'),
                 ('Anyone', 'deepseek'), ('Anyone', 'nobody'), ('', 'claude')]

# Whose picture is asked for, and of which tier: a tier given, none given (the name's, else the tool's).
PROMPTS = [('Clara', '', ''), ('clara', 'claude', ''), (' Clara ', '', 'codex'), ('Soren', '', ''), ('Soren 12', '', ''),
           ('Somebody', '', 'grok'), ('Somebody', '', 'agy'), ('Somebody', '', 'deepseek'), ('Somebody', '', 'nobody'),
           ('Somebody', 'GEMINI', ''), ('Somebody', 'nobody', 'codex'), ('Mike', '', 'codex'), ('Zoë Å', 'grok', '')]


# The life a contact's picture had before it is asked again: what each of its tries became, and when (minutes
# before the asking), then how many other pictures were begun in the last day and the day before.
ADMISSIONS = [
    {'name': 'never tried', 'tries': [], 'others_today': 0},
    {'name': 'one under way', 'tries': [('queued', 10)], 'others_today': 0},
    {'name': 'one made', 'tries': [('succeeded', 60)], 'others_today': 0},
    {'name': 'failed four minutes ago', 'tries': [('failed', 4)], 'others_today': 0},
    {'name': 'failed six minutes ago', 'tries': [('failed', 6)], 'others_today': 0},
    {'name': 'failed twice, the last 29 minutes ago', 'tries': [('failed', 120), ('failed', 29)], 'others_today': 0},
    {'name': 'failed twice, the last 31 minutes ago', 'tries': [('failed', 120), ('failed', 31)], 'others_today': 0},
    {'name': 'failed three times', 'tries': [('failed', 300), ('failed', 200), ('failed', 100)], 'others_today': 0},
    {'name': 'cancelled six minutes ago', 'tries': [('cancelled', 6)], 'others_today': 0},
    {'name': 'cancelled four minutes ago', 'tries': [('cancelled', 4)], 'others_today': 0},
    {'name': 'nineteen begun today', 'tries': [], 'others_today': 19},
    {'name': 'twenty begun today', 'tries': [], 'others_today': 20},
    {'name': 'twenty begun, but the day before', 'tries': [], 'others_today': 0, 'others_yesterday': 20},
    {'name': 'failed long ago, nineteen others today', 'tries': [('failed', 2000)], 'others_today': 19},
    {'name': 'failed an hour ago, nineteen others today', 'tries': [('failed', 60)], 'others_today': 19},
]


def admissions(avatars, db) -> list:
    """What the Python Host answers when a contact's picture is asked for, after each life in ADMISSIONS."""
    from lib import background_jobs
    minute = 60 * 1000
    asked_at = 1_950_000_000_000
    clock = {'now': asked_at}
    real_now = db.now_ms
    db.now_ms = lambda: clock['now']
    window, backoffs = avatars.DAILY_WINDOW_MS, avatars.RETRY_BACKOFF_MS
    out = []
    for number, case in enumerate(ADMISSIONS):
        kind = f'persona-avatar-case-{number}'
        job_id = f'persona-avatar-case-{number}'

        def enqueue(job, at):
            clock['now'] = at
            return background_jobs.enqueue_computer_capped(
                computer_id='computer-of-the-test', job_id=job, kind=kind, title='A picture', max_launches=10_000,
                window_ms=window, max_attempts=10_000, retry_backoffs_ms=(), now_ms=at)
        for n in range(case['others_today']):
            enqueue(f'{job_id}-other-{n}', asked_at - 60 * minute - n)
        for n in range(case.get('others_yesterday', 0)):
            enqueue(f'{job_id}-old-{n}', asked_at - 25 * 60 * minute - n)
        tries = case['tries']
        for at, (became, ago) in enumerate(tries):
            begun = asked_at - (ago + 1) * minute
            job, claimed = enqueue(job_id, begun)
            assert claimed, (case['name'], at)
            clock['now'] = asked_at - ago * minute
            if became in ('succeeded', 'failed'):
                background_jobs.finish(job_id, generation=job['generation'], status=became, reason='a test')
            elif became == 'cancelled':
                background_jobs.cancel(job_id)
        clock['now'] = asked_at
        row = db.conn().execute(
            """SELECT COUNT(*) AS n FROM background_job_events e JOIN background_jobs j ON j.job_id = e.job_id
                WHERE j.kind = ? AND e.status = 'queued' AND e.observed_at >= ?""",
            (kind, asked_at - window)).fetchone()
        before = background_jobs.get(job_id, reconcile=False)
        job, claimed = background_jobs.enqueue_computer_capped(
            computer_id='computer-of-the-test', job_id=job_id, kind=kind, title='A picture',
            max_launches=avatars.DEFAULT_DAILY_LIMIT, window_ms=window, max_attempts=avatars.DEFAULT_MAX_ATTEMPTS,
            retry_backoffs_ms=backoffs, now_ms=asked_at)
        out.append({
            'name': case['name'],
            'before': None if before is None else {
                'status': before['status'], 'generation': int(before['generation'] or 1),
                'ended_ago_ms': None if before.get('terminal_at') is None else asked_at - int(before['terminal_at'])},
            'begun_today': int(row['n']),
            'admitted': bool(claimed),
            'generation': int(job['generation']) if claimed else None,
        })
    db.now_ms = real_now
    return out


def taken_names(pool, initials):
    """Every name of a pool, and every name with an initial after it."""
    return list(pool) + [f'{name} {initial}' for initial in initials for name in pool]


def cases_of(roster) -> dict:
    claude = roster.contact_tier_spec('claude').minted_names
    initials = 'BCDEFGHJKLMNPQRSTVWXYZ'
    names = [
        {'backend': 'claude', 'occupied': [], 'existing': []},
        {'backend': 'claude', 'occupied': ['CLARA', 'ethan'], 'existing': []},
        {'backend': 'claude', 'occupied': [], 'existing': ['Clara', ' maya ']},
        {'backend': 'claude', 'occupied': [], 'existing': list(claude)},
        {'backend': 'claude', 'occupied': ['Clara B'], 'existing': list(claude)},
        {'backend': 'claude', 'occupied': [], 'existing': taken_names(claude, initials)},
        {'backend': 'claude', 'occupied': ['Clara 2'], 'existing': taken_names(claude, initials) + ['ethan 2']},
        {'backend': 'codex', 'occupied': [], 'existing': []},
        {'backend': 'opencode', 'occupied': ['Soren'], 'existing': []},
        {'backend': 'agy', 'occupied': [], 'existing': []},
        {'backend': 'grok', 'occupied': [], 'existing': []},
        {'backend': 'deepseek', 'occupied': [], 'existing': []},
        {'backend': 'muse', 'occupied': [], 'existing': []},
        {'backend': 'nobody', 'occupied': [], 'existing': ['', '  ']},
    ]
    # Personas of the owner on top of the built-in ones, and agents that wear some of them.
    voice = lambda cartesia: json.dumps({'cartesia': cartesia}, separators=(',', ':'))
    own = [
        {'name': 'Clara', 'voice_id': voice('v-clara')},
        {'name': 'Kit', 'voice_id': voice('v-kit')},
        {'name': 'Soren', 'voice_id': voice('v-soren')},
        {'name': 'Plain', 'voice_id': 'v-plain'},
        {'name': 'Muted', 'voice_id': '{}'},
        {'name': 'Tama', 'voice_id': voice('v-taken')},
    ]
    agent = lambda agent_id, persona, voice_id, backend='claude', janitor=0: {
        'agent_id': agent_id, 'session': agent_id + '-s', 'persona': persona, 'voice_id': voice_id,
        'backend': backend, 'is_janitor': janitor}
    rosters = [
        {'name': 'nobody at work', 'agents': []},
        {'name': 'some worn, one voice spoken by another',
         'agents': [agent('a1', 'Mike', '{}'), agent('a2', 'clara', voice('v-x')),
                    agent('a3', 'Someone', voice('v-taken'), 'codex'), agent('a4', 'Kit', voice('v-kit'))]},
        {'name': 'every claude contact worn',
         'agents': [agent(f'b{n}', name, '{}') for n, name in enumerate(
             ['Mike', 'Rachel', 'Clara', 'Kit', 'Plain', 'Muted', 'Tama', 'Marcus', 'Caleb', 'Nadia', 'Theo',
              'Ida', 'Nora', 'Zoe', 'Leo', 'Ada', 'Sam', 'Max', 'Eve', 'Ben'])]},
        {'name': 'a janitor among them', 'agents': [agent('j1', 'Quota keeper', '{}', 'codex', 1)]},
    ]
    launches = []
    for data in [{'mint_contact': True}, {'auto_contact': True}, {'mint_contact': True, 'auto_contact': True},
                 {'auto_contact': True, 'anonymous': True}, {'mint_contact': True, 'anonymous': 'yes'},
                 {'auto_contact': True, 'anonymous': 0}, {'mint_contact': True, 'replace_sid': ' '},
                 {'mint_contact': 'true'}, {'auto_contact': 1}, {'auto_contact': False, 'name': 'Given'},
                 {'mint_contact': True, 'name': 'Given', 'session': 'given-session'}, {'name': 'Given'}]:
        for pool in [[], ['Kit', 'Clara']]:
            for minted in [None, '', 'Ethan']:
                for janitor in [False, True]:
                    launches.append({'data': data, 'pool': pool, 'minted': minted, 'janitor': janitor})
    assigns = [
        {'mode': 'nonsense'}, {'mode': 'auto', 'session': 'nobody-s'}, {'mode': 'auto', 'session': 'j1-s'},
        {'mode': 'options'}, {'mode': 'auto'}, {'mode': 'auto', 'session': 'a2-s'}, {'mode': 'auto', 'session': 'a4-s'},
        {'mode': 'choose', 'name': 'kit'},
        {'mode': 'choose', 'name': 'Mike'}, {'mode': 'choose', 'name': 'Nobody'}, {'mode': 'choose', 'name': 'Soren'},
        {'mode': 'create', 'name': '  New   Name '}, {'mode': 'create', 'name': ''}, {'mode': 'create', 'name': 'x' * 61},
        {'mode': 'create', 'name': 'MIKE'}, {'mode': 'create', 'name': 'kit'}, {'mode': 'create', 'name': None},
    ]
    return {'names': names, 'own': own, 'rosters': rosters, 'launches': launches, 'assigns': assigns}


@tool_scratch.case('clarp-contact-vectors-')
def child(root) -> dict:
    """Runs in a process of its own: what the Python Host's functions answer."""
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    from lib import agents, backends, contact_assignment, contact_minting, db, persona_avatar_generation, personas
    from lib import roster
    from lib.agent_lifecycle import AgentLifecycleError
    from lib.policies.agent_spec import AgentSpec, NeedsContact, RosterView, SpecError
    # Every write a millisecond after the last, from the first: the built-in personas, written
    # within a millisecond or two of each other at a real start, list in either order there
    # (an accident the Rust Host does not copy: they are in the roster's order), and here in the
    # order they are written, which is the roster's; the owner's own after them, in theirs.
    clock = iter(range(1_900_000_000_000, 2_000_000_000_000))
    db.now_ms = lambda: next(clock)
    cases = cases_of(roster)
    tiers = [{'id': spec.id, 'minted_names': list(spec.minted_names), 'personalities': list(spec.personalities),
              'portrait_template': spec.portrait_template, 'portrait_varieties': list(spec.portrait_varieties)}
             for spec in roster.CONTACT_TIER_SPECS]
    told: dict = {'tiers': [{'backend': b, 'tier': roster.backend_to_tier(b)} for b in BACKENDS]}
    told['tier_backends'] = dict(roster.TIER_BACKENDS)
    told['allowed'] = [{'name': n, 'backend': b, 'tier': t, 'answer': list(roster.validate_contact_backend(n, b, t))}
                       for n, b, t in ALLOWED]
    told['personalities'] = [{'name': n, 'tier': t, 'personality': contact_minting.default_personality(n, t)}
                             for n, t in PERSONALITIES]
    # What the provider of pictures is asked for a contact's picture: its tier's own words with one of its kinds of
    # face, by the name (`persona_avatar_generation.prompt_for_persona`). Nothing is asked here.
    from lib import persona_avatar_generation
    # The bounds of the making of pictures, as the Python Host has them (its defaults: no config here).
    told['picture_bounds'] = {
        'daily_limit': persona_avatar_generation.DEFAULT_DAILY_LIMIT,
        'daily_window_ms': persona_avatar_generation.DAILY_WINDOW_MS,
        'max_attempts': persona_avatar_generation.DEFAULT_MAX_ATTEMPTS,
        'retry_backoff_ms': list(persona_avatar_generation.RETRY_BACKOFF_MS),
        'heartbeat_timeout_ms': persona_avatar_generation.HEARTBEAT_TIMEOUT_MS,
        'answer_most': persona_avatar_generation.MAX_RESPONSE_BYTES,
        'made_most': persona_avatar_generation.MAX_GENERATED_BYTES,
        'kept_most': persona_avatar_generation.AVATAR_MAX_BYTES,
        'model': persona_avatar_generation.MODEL,
    }
    # Whether a picture's job is begun (`background_jobs.enqueue_computer_capped`, as the picture asks it): each
    # case is a kind and a contact of its own on the same database, on a clock the case sets.
    told['admissions'] = admissions(persona_avatar_generation, db)
    told['prompts'] = [{'name': n, 'tier': t, 'backend': b,
                        'prompt': persona_avatar_generation.prompt_for_persona(n, tier=t, backend=b)}
                       for n, t, b in PROMPTS]
    personas.ensure_builtins()
    told['names'] = []
    for case in cases['names']:
        try:
            name, tier = contact_minting.pick_given_name(case['backend'], occupied_names=case['occupied'],
                                                         existing_names=case['existing'])
            told['names'].append({**case, 'name': name, 'tier': tier})
        except RuntimeError as error:
            told['names'].append({**case, 'refused': str(error)})
    # The owner's own personas: the order they are listed in is the order they were made.
    for made in cases['own']:
        personas.create(name=made['name'], voice_id=made['voice_id'])
    listed = [{'name': row['name'], 'voice_id': row['voice_id'], 'tier': row.get('tier', '') or ''}
              for row in personas.list_all()]
    told['personas'] = listed
    told['pools'], told['assigns'] = [], []
    for roster_case in cases['rosters']:
        rows = roster_case['agents']
        agents.list_agents = lambda rows=rows: [dict(row) for row in rows]
        pools = {backend: [p['name'] for p in contact_assignment.available_contacts(backend, exclude)]
                 for backend in ['claude', 'codex', 'agy', 'grok', 'deepseek']
                 for exclude in ['']}
        told['pools'].append({'roster': roster_case['name'], 'agents': rows, 'pools': pools,
                              'excluding_a2': [p['name'] for p in contact_assignment.available_contacts('claude', 'a2')]})
        # What a running agent is assigned: the reference's own function, with the agents
        # standing in, a minted contact standing in for the mint (held above), and what it
        # would write recorded instead of written.
        for asked in cases['assigns']:
            written, made, minted = [], [], []
            agents.get_by_session = lambda session, rows=rows: next(
                (dict(row) for row in rows if row['session'] == session), None)
            agents.update_agent = lambda agent_id, **fields: written.append({'agent_id': agent_id, **fields})
            contact_minting.mint_contact = lambda backend, **more: minted.append(backend) or {
                'name': 'Minted', 'voice_id': '{"cartesia":"v-minted"}', 'personality': 'Minted words'}
            persona_avatar_generation.start_best_effort = lambda *said, **more: None
            real_create = personas.create
            personas.create = lambda **fields: made.append(fields) or {'name': fields['name'], 'voice_id': '',
                                                                        'personality': ''}

            real_conn = db.conn

            class Conn:
                """The connection as it is, but for the transaction the function opens around its
                writes, which are recorded above instead."""
                def execute(self, statement, *said):
                    return None if statement == 'BEGIN IMMEDIATE' else real_conn().execute(statement, *said)

                def commit(self):
                    return None

                def rollback(self):
                    return None
            db.conn = lambda: Conn()
            session = asked.get('session', 'a1-s' if rows else 'nobody-s')
            try:
                answer = contact_assignment.assign_contact(session, asked['mode'], asked.get('name', ''))
                refused = None
            except AgentLifecycleError as error:
                answer, refused = None, {'status': error.status, 'body': error.response()}
            except ValueError as error:
                answer, refused = None, {'status': 409, 'body': {'error': str(error)}}
            finally:
                db.conn, personas.create = real_conn, real_create
            told['assigns'].append({'roster': roster_case['name'], 'asked': {**asked, 'session': session},
                                    'answer': answer, 'refused': refused, 'written': written, 'made': made,
                                    'minted': minted})
    told['launches'] = []
    for launch in cases['launches']:
        view = RosterView(contact_pool=lambda backend, pool=launch['pool']: list(pool),
                          minted_contact=launch['minted'], existing_cwd=lambda raw: '/work')
        parsed = AgentSpec.parse({'backend': 'claude', **launch['data']}, backends=backends, roster=view,
                                 personas=personas, janitor=launch['janitor'])
        if isinstance(parsed, NeedsContact):
            outcome = {'mint': parsed.backend}
        elif isinstance(parsed, SpecError):
            outcome = {'refused': [parsed.status, parsed.message, parsed.detail]}
        else:
            outcome = {'name': parsed.persona, 'session': parsed.session}
        told['launches'].append({**launch, 'outcome': outcome})
    shutil.rmtree(root, ignore_errors=True)
    return {'told': told, 'tiers': tiers}


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == '--child':
        print(json.dumps(child(), ensure_ascii=False))
        return 0
    ran = subprocess.run([sys.executable, '-I', __file__, '--child'], capture_output=True, text=True,
                         env=tool_scratch.child_env({'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}), check=False)
    if ran.returncode != 0:
        print(ran.stderr[-3000:], file=sys.stderr)
        return 1
    answered = json.loads(ran.stdout.splitlines()[-1])
    told = answered['told']
    out = ['{', ' "about": "Written by server-rs/tools/contact-vectors.py from server/lib/contact_minting.py, '
                'contact_assignment.py, roster.py and policies/agent_spec.py. Do not edit by hand.",']
    keys = list(told)
    for at, key in enumerate(keys):
        value = told[key]
        if isinstance(value, list):
            out.append(f' "{key}": [')
            out.extend('  ' + json.dumps(row, ensure_ascii=False) + (',' if n < len(value) - 1 else '')
                       for n, row in enumerate(value))
            out.append(' ]' + (',' if at < len(keys) - 1 else ''))
        else:
            out.append(f' "{key}": ' + json.dumps(value, ensure_ascii=False) + (',' if at < len(keys) - 1 else ''))
    out.append('}')
    OUT.write_text('\n'.join(out) + '\n')
    TIERS.write_text(json.dumps(answered['tiers'], indent=1, ensure_ascii=False) + '\n')
    counts = {key: len(value) for key, value in told.items() if isinstance(value, list)}
    print(f'wrote {OUT.relative_to(REPO)}: {counts}; and {TIERS.relative_to(REPO)} ({len(answered["tiers"])} tiers)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
