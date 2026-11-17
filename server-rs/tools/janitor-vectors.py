#!/usr/bin/env python3
"""Writes the vectors the Rust Host's book of janitors is tested against.

A janitor is an ordinary agent that is given a job of maintenance: a template (what it does),
a scope (the agents it watches), attachments (the triggers that start it) and options. The Python
Host keeps it in `server/lib/janitors.py` and `janitor_store.py`, and installs seven of its own
(`janitor_builtins.py`). This runs those modules themselves, on a database made for it, and writes
to `crates/clarp-kernel/tests/fixtures/janitor-vectors.json`:

- the catalogue: the templates and the triggers a Host offers;
- the first day: the janitors a Host installs for itself, as it shows them;
- what it makes of options, of a scope, of attachments and of how a job is run, each as it is
  taken or why it is refused;
- stories: a janitor made, changed, turned on and off and removed, step by step, with what the
  Host answered each time.

    "$(sed -n 1p ~/.local/share/clarp/current/SERVICE_PYTHON)" server-rs/tools/janitor-vectors.py

It runs in a process of its own whose every home, data, config and cache directory is a new
empty directory (the replay harness's own isolation, `tests/replay/host.py`), with no way out
of the machine. No Host is touched or started, no model is asked and nothing is run.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import re
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = HERE.parent / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'janitor-vectors.json'
# What the kernel carries of it: the catalogue, and how the Host's own janitors are installed.
CATALOGUE = HERE.parent / 'crates' / 'clarp-kernel' / 'src' / 'janitor-catalogue.json'

# The agents of the made-up Host: who they are, and the tool each runs on.
AGENTS = [('Worker', 'worker', 'claude'), ('Coder', 'coder', 'codex'), ('Other', 'other', 'claude'),
          ('Second', 'second', 'codex'), ('Third', 'third', 'claude'), ('Fourth', 'fourth', 'codex'),
          ('Gone', 'gone', 'claude')]
# The moment everything happens at, counted on by one second for each thing that is written.
NOW = 1_800_000_000_000

LONG = 'x' * 8000
OPTIONS = [
    # (template, what is given, what is kept already)
    ('tool-explainer', {}, None), ('tool-explainer', {'detail_level': 0}, None), ('tool-explainer', {'detail_level': 4}, None),
    ('tool-explainer', {'detail_level': 5}, None), ('tool-explainer', {'detail_level': -1}, None),
    ('tool-explainer', {'detail_level': '2'}, None), ('tool-explainer', {'detail_level': 2.0}, None),
    ('tool-explainer', {'detail_level': True}, None), ('tool-explainer', {'detail_level': None}, None),
    ('tool-explainer', {'explanation_sources': 2, 'detail_level': 3}, None), ('tool-explainer', {'explanation_sources': 3}, None),
    ('tool-explainer', {'unknown': 1}, None), ('tool-explainer', {'unknown': 1}, {'unknown': 1}),
    ('tool-explainer', {'unknown': 2}, {'unknown': 1}), ('tool-explainer', {'detail_level': 1}, {'unknown': [1, {'a': 2}]}),
    ('tool-explainer', {'unknown': [1, {'a': 2}]}, {'unknown': [1, {'a': 2}]}),
    ('tool-explainer', [], None), ('tool-explainer', 'detail', None), ('tool-explainer', None, None),
    ('tool-explainer', {'detail_level': 2}, {'detail_level': 9}), ('tool-explainer', {}, {'detail_level': 9}),
    ('scheduled-prompt', {'instructions': ''}, None), ('scheduled-prompt', {'instructions': 'Say hello.\n\tTwice.'}, None),
    ('scheduled-prompt', {'instructions': LONG}, None), ('scheduled-prompt', {'instructions': LONG + 'x'}, None),
    ('scheduled-prompt', {'instructions': 'a\x01b'}, None), ('scheduled-prompt', {'instructions': 'a\rb'}, None),
    ('scheduled-prompt', {'instructions': 7}, None), ('scheduled-prompt', {'instructions': 'ä' * 8000}, None),
    ('scheduled-prompt', {'max_interval_seconds': 0}, None), ('scheduled-prompt', {'max_interval_seconds': 604800}, None),
    ('scheduled-prompt', {'max_interval_seconds': 604801}, None), ('scheduled-prompt', {'max_interval_seconds': -1}, None),
    ('scheduled-prompt', {'max_interval_seconds': 1.5}, None), ('scheduled-prompt', {'max_interval_seconds': 3600.0}, None),
    ('scheduled-prompt', {'max_interval_seconds': True}, None), ('scheduled-prompt', {'max_interval_seconds': '60'}, None),
    ('message-delegator', {'confidence_threshold': 0.5}, None), ('message-delegator', {'confidence_threshold': 0.99}, None),
    ('message-delegator', {'confidence_threshold': 0.49}, None), ('message-delegator', {'confidence_threshold': 1}, None),
    ('message-delegator', {'confidence_threshold': '0.7'}, None), ('message-delegator', {'confidence_threshold': True}, None),
    ('message-delegator', {'timeout_ms': 250}, None), ('message-delegator', {'timeout_ms': 249}, None),
    ('message-delegator', {'timeout_ms': 60000.0}, None), ('message-delegator', {'fallback_only': False}, None),
    ('message-delegator', {'fallback_only': 0}, None), ('message-delegator', {'fallback_only': 'no'}, None),
    ('message-delegator', {'voice_id': 'v' * 160}, None), ('message-delegator', {'voice_id': 'v' * 161}, None),
    ('message-delegator', {'voice_id': 'a\nb'}, None), ('message-delegator', {'voice_id': ''}, None),
    ('message-delegator', {'voice_id': 7}, None),
    ('quota-monitor', {'recovery_mode': 'ask'}, None), ('quota-monitor', {'recovery_mode': 'other'}, None),
    ('quota-monitor', {'recovery_mode': 0}, None), ('quota-monitor', {'remaining_threshold': 100, 'interval_seconds': 60}, None),
    ('quota-monitor', {'remaining_threshold': 101}, None), ('quota-monitor', {'interval_seconds': 59}, None),
    ('account-hotseat', {'mode': 'notify', 'hotseat_command': '/usr/local/bin/hotseat', 'auto_repair_auth': True}, None),
    ('account-hotseat', {'codex_interval_seconds': 299}, None), ('account-hotseat', {'hotseat_command': 'a\tb'}, None),
    ('goal-keeper', {'max_goals_per_run': 6}, None), ('goal-keeper', {'max_goals_per_run': 7}, None),
    ('goal-keeper', {'max_goals_per_run': 0}, None), ('audio-bookkeeper', {}, None), ('audio-bookkeeper', {'x': 1}, None),
    ('label-auditor', {'interval_seconds': 900, 'autocorrect': True}, None), ('label-auditor', {'interval_seconds': 899}, None),
    ('heartbeat-decider', {'review_interval_seconds': 60, 'timeout_ms': 120000}, None),
    ('heartbeat-decider', {'timeout_ms': 999}, None), ('task-labels', {'max_interval_seconds': 86400}, None),
    ('no-such-job', {}, None), ('', {}, None),
]

# Attachments: (template, what is given). `{worker}` is the agent they are checked for.
A = 'attachment-one'
ATTACHMENTS = [
    ('task-labels', None), ('task-labels', []), ('task-labels', 'schedule'), ('task-labels', [1]), ('task-labels', [None]),
    ('task-labels', [{}]), ('task-labels', [{'attachment_id': A}]), ('task-labels', [{'attachment_id': A, 'trigger_id': 7}]),
    ('task-labels', [{'attachment_id': A, 'trigger_id': 'agent-work-completed'}]),
    ('task-labels', [{'attachment_id': A, 'trigger_id': 'agent-work-completed', 'trigger_version': 1}]),
    ('task-labels', [{'attachment_id': A, 'trigger_id': 'agent-work-completed', 'trigger_version': 2}]),
    ('task-labels', [{'attachment_id': A, 'trigger_id': 'agent-work-completed', 'trigger_version': True}]),
    ('task-labels', [{'attachment_id': A, 'trigger_id': 'agent-work-completed', 'trigger_version': '1'}]),
    ('task-labels', [{'attachment_id': A, 'trigger_id': 'agent-work-completed', 'trigger_version': 1.0}]),
    ('task-labels', [{'attachment_id': A, 'trigger_id': 'no-such-trigger'}]),
    ('task-labels', [{'attachment_id': A, 'trigger_id': 'routing-requested'}]),
    ('message-delegator', [{'attachment_id': A, 'trigger_id': 'routing-requested'}]),
    ('message-delegator', [{'attachment_id': A, 'trigger_id': 'routing-requested', 'config': {'max_targets': 1}}]),
    ('message-delegator', [{'attachment_id': A, 'trigger_id': 'schedule'}]),
    ('task-labels', [{'attachment_id': 7, 'trigger_id': 'schedule'}]),
    ('task-labels', [{'attachment_id': 'a' * 100, 'trigger_id': 'schedule'}]),
    ('task-labels', [{'attachment_id': 'a' * 101, 'trigger_id': 'schedule'}]),
    ('task-labels', [{'attachment_id': '', 'trigger_id': 'schedule'}]),
    ('task-labels', [{'trigger_id': 'schedule'}]),
    ('task-labels', [{'attachment_id': A, 'trigger_id': 'schedule'}, {'attachment_id': A, 'trigger_id': 'agent-work-completed'}]),
    ('task-labels', [{'attachment_id': f'a{n}', 'trigger_id': 'schedule'} for n in range(8)]),
    ('task-labels', [{'attachment_id': f'a{n}', 'trigger_id': 'schedule'} for n in range(9)]),
    ('task-labels', [{'attachment_id': A, 'trigger_id': 'schedule', 'config': []}]),
    ('task-labels', [{'attachment_id': A, 'trigger_id': 'schedule', 'config': None}]),
    ('task-labels', [{'attachment_id': A, 'trigger_id': 'schedule', 'config': {'cron': '30 8 * * 1-5', 'timezone': 'Europe/Oslo'}}]),
    ('task-labels', [{'attachment_id': A, 'trigger_id': 'schedule', 'config': {'cron': '30 8 * *', 'timezone': 'Europe/Oslo'}}]),
    ('task-labels', [{'attachment_id': A, 'trigger_id': 'schedule', 'config': {'cron': '61 8 * * *'}}]),
    ('task-labels', [{'attachment_id': A, 'trigger_id': 'schedule', 'config': {'cron': 7}}]),
    ('task-labels', [{'attachment_id': A, 'trigger_id': 'schedule', 'config': {'timezone': 'Europe/Nowhere'}}]),
    ('task-labels', [{'attachment_id': A, 'trigger_id': 'schedule', 'config': {'timezone': ''}}]),
    ('task-labels', [{'attachment_id': A, 'trigger_id': 'schedule', 'config': {'timezone': 7}}]),
    ('task-labels', [{'attachment_id': A, 'trigger_id': 'schedule', 'config': {'timezone': '../etc/passwd'}}]),
    ('task-labels', [{'attachment_id': A, 'trigger_id': 'schedule', 'config': {'timezone': '/usr/share/zoneinfo/UTC'}}]),
    ('task-labels', [{'attachment_id': A, 'trigger_id': 'schedule', 'config': {'timezone': 'europe/oslo'}}]),
    ('task-labels', [{'attachment_id': A, 'trigger_id': 'schedule', 'config': {'timezone': 'America/Argentina/Buenos_Aires'}}]),
    ('task-labels', [{'attachment_id': A, 'trigger_id': 'schedule', 'config': {'timezone': 'Etc/GMT+5', 'max_targets': 1}}]),
    ('task-labels', [{'attachment_id': A, 'trigger_id': 'schedule', 'config': {'max_targets': 0}}]),
    ('task-labels', [{'attachment_id': A, 'trigger_id': 'schedule', 'config': {'max_targets': 4}}]),
    ('task-labels', [{'attachment_id': A, 'trigger_id': 'schedule', 'config': {'max_targets': True}}]),
    ('task-labels', [{'attachment_id': A, 'trigger_id': 'schedule', 'config': {'max_targets': 2.0}}]),
    ('task-labels', [{'attachment_id': A, 'trigger_id': 'schedule', 'config': {'coalesce_seconds': 300, 'min_interval_seconds': 3600}}]),
    ('task-labels', [{'attachment_id': A, 'trigger_id': 'schedule', 'config': {'coalesce_seconds': 301}}]),
    ('task-labels', [{'attachment_id': A, 'trigger_id': 'schedule', 'config': {'min_interval_seconds': 3601}}]),
    ('task-labels', [{'attachment_id': A, 'trigger_id': 'schedule', 'config': {'interval_seconds': 900}}]),
    ('task-labels', [{'attachment_id': A, 'trigger_id': 'agent-work-completed', 'config': {'cron': '0 9 * * *'}}]),
    ('task-labels', [{'attachment_id': A, 'trigger_id': 'agent-work-completed', 'config': {'coalesce_seconds': 0, 'max_targets': 3}}]),
    ('task-labels', [{'attachment_id': A, 'trigger_id': 'active-interval'}]),
    ('task-labels', [{'attachment_id': A, 'trigger_id': 'active-interval',
                      'config': {'interval_seconds': 60, 'idle_timeout_seconds': 86400, 'run_on_resume': False}}]),
    ('task-labels', [{'attachment_id': A, 'trigger_id': 'active-interval', 'config': {'interval_seconds': 59}}]),
    ('task-labels', [{'attachment_id': A, 'trigger_id': 'active-interval', 'config': {'idle_timeout_seconds': 86401}}]),
    ('task-labels', [{'attachment_id': A, 'trigger_id': 'active-interval', 'config': {'interval_seconds': 900.0}}]),
    ('task-labels', [{'attachment_id': A, 'trigger_id': 'active-interval', 'config': {'run_on_resume': 1}}]),
    ('task-labels', [{'attachment_id': A, 'trigger_id': 'active-interval', 'config': {'cron': '0 9 * * *'}}]),
    ('task-labels', [{'attachment_id': A, 'trigger_id': 'schedule', 'enabled': False}]),
    ('task-labels', [{'attachment_id': A, 'trigger_id': 'schedule', 'enabled': 0}]),
    ('task-labels', [{'attachment_id': A, 'trigger_id': 'schedule', 'enabled': None}]),
    ('scheduled-prompt', [{'attachment_id': A, 'trigger_id': 'schedule'}]),
    ('scheduled-prompt', [{'attachment_id': A, 'trigger_id': 'agent-work-completed'}]),
    ('goal-keeper', [{'attachment_id': A, 'trigger_id': 'goal-activity-observed'}]),
    ('goal-keeper', [{'attachment_id': A, 'trigger_id': 'goal-activity-observed', 'config': {'coalesce_seconds': 5}}]),
    ('no-such-job', [{'attachment_id': A, 'trigger_id': 'schedule'}]),
]

# Scopes. `{worker}` and the like stand for the id of that agent; `{janitor}` for one that is a janitor.
SCOPES = [
    None, [], 'all', {}, {'agent_ids': []}, {'exclude_agent_ids': []}, {'agent_ids': [], 'exclude_agent_ids': [], 'more': 1},
    {'agent_ids': ['{worker}']}, {'agent_ids': ['{coder}', '{worker}', '{coder}'], 'exclude_agent_ids': ['{other}']},
    {'agent_ids': 'worker'}, {'agent_ids': [7]}, {'agent_ids': ['']}, {'agent_ids': ['  ']}, {'agent_ids': [None]},
    {'agent_ids': ['nobody-has-this-id']}, {'exclude_agent_ids': ['nobody-has-this-id']},
    {'agent_ids': ['{janitor}']}, {'exclude_agent_ids': ['{janitor}']}, {'agent_ids': ['{gone}']},
    {'agent_ids': ['{worker}'], 'exclude_agent_ids': ['{worker}']}, {'agent_ids': [' {worker} ']},
    {'agent_ids': ['worker']}, {'agent_ids': None}, {'agent_ids': {}},
]

EXECUTIONS = [
    (template, value, backend)
    for template in ('task-labels', 'scheduled-prompt', 'tool-explainer', 'audio-bookkeeper', 'message-delegator', 'goal-keeper')
    for backend in ('claude', 'codex')
    for value in (None, {}, [], 'ephemeral', {'executor': 'ephemeral'}, {'executor': 'managed'}, {'executor': 'deterministic'},
                  {'provider': 'claude'}, {'provider': 'codex'}, {'provider': 'openai'}, {'provider': 'local'},
                  {'provider': 'nobody'}, {'provider': 7}, {'isolated': True}, {'isolated': False}, {'isolated': 1},
                  {'executor': 'ephemeral', 'provider': 'codex', 'more': 1})
]


def run() -> dict:
    """Runs in a process of its own: what the Python Host's janitors make of each."""
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    root = pathlib.Path(tempfile.mkdtemp(prefix='clarp-janitor-vectors-', dir='/var/tmp'))
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    from lib import agents, avatar_urls, backends, db, janitor_builtins, janitors

    clock = {'now': NOW}

    def now_ms():
        clock['now'] += 1000
        return clock['now']
    db.now_ms = now_ms
    work = root / 'work'
    work.mkdir()
    ids = {}
    for persona, session, backend in AGENTS:
        ids[session] = agents.create_agent(persona=persona, voice_id='', cwd=str(work), session=session, backend=backend)
    agents.set_archived(ids['gone'], True)
    janitor_builtins.ensure_builtins(str(work))

    drawn: dict = {}

    def named(value):
        """A value with each made-up agent's id as its name, the made-up directory as one name,
        and each id the Host drew for an attachment as the number of its drawing."""
        text = json.dumps(value)
        for session, agent_id in ids.items():
            text = text.replace(agent_id, f'id-{session}')
        for found in re.findall(r'attachment-[0-9a-f]{32}', text):
            text = text.replace(found, drawn.setdefault(found, f'<drawn:{len(drawn) + 1}>'))
        return json.loads(text.replace(str(work), '/srv/work'))

    def filled(value):
        text = json.dumps(value)
        for session, agent_id in ids.items():
            text = text.replace('{' + session + '}', agent_id)
        return json.loads(text.replace('{janitor}', builtin['tool-explainer']))

    def attempt(call):
        try:
            return {'taken': named(call())}
        except janitors.JanitorError as refused:
            return {'refused': [refused.status, refused.code, named(str(refused))]}
        except (AttributeError, TypeError, ValueError, KeyError) as broke:
            # Not a refusal: the Python Host breaks on this input. Told as that, by its kind.
            return {'broke': type(broke).__name__}

    first_day = named(janitors.list_janitors())
    builtin = {row['builtin_role']: row['agent_id'] for row in first_day}
    routing = [adapter.id for adapter in backends.routing_adapters()]
    said = {
        'templates': janitors.templates(), 'triggers': janitors.trigger_definitions(),
        'turn_templates': sorted(janitors.TURN_TEMPLATES),
        # Who can run a job for a janitor: the tools that answer one request by itself, which of them
        # can do it shut off from everything, and the tool a provider's name stands for.
        'providers': {'routing': routing,
                      'isolating': [name for name in routing if backends.get(name).isolated_oneshot is not None],
                      'backend_of': {name: backends.for_provider(name) for name in ['openai', 'local', *routing]},
                      # The model a janitor that is made as a new agent gets where its request names none.
                      'default_models': {name: backends.by_id(name).janitor_default_model for name in routing},
                      # Every name a tool is known by: its own, and the others it goes by.
                      'names': {**{tool.id: tool.id for tool in backends._all()}, **dict(backends._ALIASES)}},
        'roles': list(janitor_builtins.ROLES), 'optional_roles': list(janitor_builtins.OPTIONAL_ROLES),
        # The id each of the Host's own janitors has, on every Host: made from its job's name.
        'own_ids': {row['builtin_role']: row['agent_id'] for row in first_day},
        'portraits': dict(avatar_urls._JANITOR_PORTRAITS), 'first_day': first_day,
        'agents': [{'agent_id': f'id-{session}', 'session': session, 'persona': persona, 'backend': backend,
                    'archived': session == 'gone'} for persona, session, backend in AGENTS],
    }
    said['options'] = [
        {'template': template, 'given': given, 'kept': kept,
         'patched': attempt(lambda: janitors._option_patch(template, given, kept)),
         'shown': attempt(lambda: janitors.option_values(template, kept))}
        for template, given, kept in OPTIONS]
    said['attachments'] = []
    for template, given in ATTACHMENTS:
        result = attempt(lambda: janitors._attachment_values(given, ids['worker'], template))
        # An attachment that names no id is given one the Host draws: told as that.
        for item in result.get('taken') or ():
            if item['attachment_id'].startswith('<drawn:'):
                item['attachment_id'] = '<drawn>'
        said['attachments'].append({'template': template, 'given': given, **result})
    said['scopes'] = [{'given': given, **attempt(lambda: janitors._scope(filled(given)))} for given in SCOPES]
    said['executions'] = [{'template': template, 'given': given, 'backend': backend,
                           **attempt(lambda: janitors._execution(template, given, backend))}
                          for template, given, backend in EXECUTIONS]

    # ---- stories: one janitor's life, step by step. Each step says what is asked (`ask`), of
    # which agent (by its session, or `{role}` for one of the Host's own by its id), and with
    # what; `{name}` in what is given stands for that agent's id.
    def step(what, ask, of='', **given):
        key = builtin[of[1:-1]] if of.startswith('{') else of
        args = filled(given)
        calls = {
            'get': lambda: janitors.get(key),
            'create': lambda: janitors.create(key, **args),
            'configure': lambda: janitors.configure(key, args.pop('expected_revision'), **args),
            'enabled': lambda: janitors.set_enabled(key, args['expected_revision'], args['enabled']),
            'remove': lambda: janitors.remove(key, args['expected_revision']),
            'reset': lambda: janitors.reset_defaults(key, args['expected_revision']),
            'adopt': lambda: janitors.adopt_options(key, args['expected_revision'], args['options']),
            'release': lambda: janitors.release(key, args.pop('expected_revision'), **args),
            'list': lambda: sorted((row['session'], row['enabled'], row['revision'], row['generation'])
                                   for row in janitors.list_janitors()),
        }
        return {'what': what, 'ask': ask, 'of': of, 'given': given, **attempt(calls[ask])}
    on, off = {'enabled': True}, {'enabled': False}
    only_other = {'agent_ids': ['{other}'], 'exclude_agent_ids': []}
    stories = [{'name': 'a scheduled task', 'steps': [
        step('read an agent that is no janitor', 'get', 'worker'),
        step('read an agent nobody has', 'get', 'nobody'),
        step('make of an agent nobody has', 'create', 'nobody'),
        step('make with a job nobody has', 'create', 'worker', template_id='no-such-job'),
        step('make of an agent that was put away', 'create', 'gone'),
        step('make', 'create', 'worker', template_id='scheduled-prompt'),
        step('make again', 'create', 'worker', template_id='task-labels'),
        step('turn on without its instructions', 'enabled', 'worker', expected_revision=1, **on),
        step('turn off what is off', 'enabled', 'worker', expected_revision=1, **off),
        step('turn off what is off, with an old revision', 'enabled', 'worker', expected_revision=7, **off),
        step('change with an old revision', 'configure', 'worker', expected_revision=7, options={'instructions': 'x'}),
        step('change with a revision that is yes', 'configure', 'worker', expected_revision=True,
             options={'instructions': 'x'}),
        step('change with options that are none', 'configure', 'worker', expected_revision=1, options=None),
        step('change to a tool nobody has', 'configure', 'worker', expected_revision=1, backend='abacus'),
        step('change to an effort its tool has not', 'configure', 'worker', expected_revision=1, effort='extreme'),
        step('give it its instructions', 'configure', 'worker', expected_revision=1,
             options={'instructions': 'Say hello.'}),
        step('turn on', 'enabled', 'worker', expected_revision=2, **on),
        step('turn on what is on', 'enabled', 'worker', expected_revision=3, **on),
        step('turn on with an old revision', 'enabled', 'worker', expected_revision=2, **on),
        step('change while it is on', 'configure', 'worker', expected_revision=3,
             attachments=[{'attachment_id': 'at-nine', 'trigger_id': 'schedule',
                           'config': {'cron': '0 9 * * *', 'timezone': 'Europe/Oslo'}}]),
        step('read it', 'get', 'worker'),
        step('turn on again', 'enabled', 'worker', expected_revision=4, **on),
        step('give it another job', 'configure', 'worker', expected_revision=5, template_id='task-labels'),
        step('turn on as that', 'enabled', 'worker', expected_revision=6, **on),
        step('give it a scope', 'configure', 'worker', expected_revision=7,
             scope={'agent_ids': ['{coder}'], 'exclude_agent_ids': []}),
        step('give it an effort', 'configure', 'worker', expected_revision=8, effort='high'),
        step('give it no trigger that is on', 'configure', 'worker', expected_revision=9,
             attachments=[{'attachment_id': 'off', 'trigger_id': 'schedule', 'enabled': False}]),
        step('turn on with no trigger on', 'enabled', 'worker', expected_revision=10, **on),
        step('give it back a trigger it had', 'configure', 'worker', expected_revision=10,
             attachments=[{'attachment_id': 'at-nine', 'trigger_id': 'schedule'}]),
        step('remove with an old revision', 'remove', 'worker', expected_revision=1),
        step('remove', 'remove', 'worker', expected_revision=10),
        step('read what was removed', 'get', 'worker'),
        step('turn on what was removed', 'enabled', 'worker', expected_revision=11, **on),
        step('make of what was removed', 'create', 'worker'),
        step('the list', 'list'),
    ]}, {'name': 'two that keep labels, and whose scope is whose', 'steps': [
        step('make one that keeps labels', 'create', 'coder'),
        step('make another', 'create', 'second'),
        step('turn the first on', 'enabled', 'coder', expected_revision=1, **on),
        step('turn the second on in the same scope', 'enabled', 'second', expected_revision=1, **on),
        step('give the second one agent to watch', 'configure', 'second', expected_revision=1, scope=only_other),
        step('turn it on there, where the first watches all', 'enabled', 'second', expected_revision=2, **on),
        step('have the first leave that agent out', 'configure', 'coder', expected_revision=2,
             scope={'agent_ids': [], 'exclude_agent_ids': ['{other}']}),
        step('turn the first on', 'enabled', 'coder', expected_revision=3, **on),
        step('turn the second on beside it', 'enabled', 'second', expected_revision=2, **on),
        step('give the first the agent the second has', 'configure', 'coder', expected_revision=4, scope=only_other),
        step('turn the first on there', 'enabled', 'coder', expected_revision=5, **on),
        step('a scheduled task beside them, made with what it is to do', 'create', 'third',
             template_id='scheduled-prompt', options={'instructions': 'Tidy.'},
             attachments=[{'attachment_id': 'nightly', 'trigger_id': 'schedule',
                           'config': {'cron': '15 3 * * *', 'timezone': 'Europe/Oslo'}}]),
        step('turn it on', 'enabled', 'third', expected_revision=1, **on),
        step('one made with a scope and a trigger of its own', 'create', 'fourth', scope=only_other,
             attachments=[{'attachment_id': 'while-used', 'trigger_id': 'active-interval',
                           'config': {'interval_seconds': 600}}],
             options={'max_interval_seconds': 3600}),
        step('one that takes a trigger another has', 'configure', 'fourth', expected_revision=1,
             attachments=[{'attachment_id': 'nightly', 'trigger_id': 'schedule'}]),
        step('one made with a scope nobody has', 'create', 'other', scope={'agent_ids': ['nobody-has-this-id']}),
        # Let go: an agent like any other again, with what it was as a janitor kept.
        step('let one go that is on', 'release', 'third', expected_revision=2),
        step('let one go with a revision nobody saw', 'release', 'fourth', expected_revision=9),
        step('let one go', 'release', 'fourth', expected_revision=1),
        step('let it go again', 'release', 'fourth', expected_revision=2),
        step('turn on what was let go', 'enabled', 'fourth', expected_revision=2, **on),
        step('read what was let go', 'get', 'fourth'),
        step('make a janitor of it again', 'create', 'fourth', template_id='scheduled-prompt'),
        step('let one go to a successor that is named without its revision', 'release', 'fourth', expected_revision=2,
             successor_session='coder'),
        step('one made with options that are none', 'create', 'other', options={'detail_level': 1}),
        step('the list', 'list'),
    ]}, {'name': "the Host's own janitors", 'steps': [
        step("read one of the Host's own", 'get', 'clarp-tool-explainer'),
        step('give it another job', 'configure', '{tool-explainer}', expected_revision=1, template_id='task-labels'),
        step('remove it', 'remove', '{tool-explainer}', expected_revision=1),
        step('turn it off', 'enabled', '{tool-explainer}', expected_revision=1, **off),
        step('set how it explains', 'configure', '{tool-explainer}', expected_revision=2, options={'detail_level': 3}),
        step('take over a choice when it has one', 'adopt', '{tool-explainer}', expected_revision=3,
             options={'detail_level': 1}),
        step('take over more than the one choice', 'adopt', '{tool-explainer}', expected_revision=3,
             options={'detail_level': 1, 'x': 2}),
        step('turn it on', 'enabled', '{tool-explainer}', expected_revision=3, **on),
        step('put it back as it came', 'reset', '{tool-explainer}', expected_revision=4),
        step('take over a choice for another', 'adopt', '{goal-keeper}', expected_revision=1,
             options={'detail_level': 1}),
        step('turn on the one that keeps goals', 'enabled', '{goal-keeper}', expected_revision=1, **on),
        step('give it fewer goals at once', 'configure', '{goal-keeper}', expected_revision=2,
             options={'max_goals_per_run': 2}),
        step('put it back as it came', 'reset', '{goal-keeper}', expected_revision=3),
        step("let one of the Host's own go", 'release', '{quota-monitor}', expected_revision=1),
        step('take over a choice where none was made', 'adopt', '{message-delegator}', expected_revision=1,
             options={'detail_level': 1}),
        step('the list at the end', 'list'),
    ]}]
    said['stories'] = stories
    return said


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
    parts = list(said)
    for at, part in enumerate(parts):
        rows = said[part]
        end = ',' if at < len(parts) - 1 else ''
        if part == 'stories':
            out.append(f' "{part}": [')
            for index, story in enumerate(rows):
                out.append('  {"name": ' + json.dumps(story['name']) + ', "steps": [')
                out.extend('   ' + json.dumps(row, ensure_ascii=True) + (',' if n < len(story['steps']) - 1 else '')
                           for n, row in enumerate(story['steps']))
                out.append('  ]}' + (',' if index < len(rows) - 1 else ''))
            out.append(' ]' + end)
        elif isinstance(rows, list) and rows and isinstance(rows[0], (dict, list)):
            out.append(f' "{part}": [')
            out.extend('  ' + json.dumps(row, ensure_ascii=True) + (',' if index < len(rows) - 1 else '')
                       for index, row in enumerate(rows))
            out.append(' ]' + end)
        else:
            out.append(f' "{part}": ' + json.dumps(rows, ensure_ascii=True) + end)
    out.append('}')
    OUT.write_text('\n'.join(out) + '\n')
    carried = {key: said[key] for key in ('templates', 'triggers', 'turn_templates', 'providers', 'roles',
                                          'optional_roles', 'own_ids', 'portraits')}
    CATALOGUE.write_text(json.dumps(carried, indent=1, ensure_ascii=True) + '\n')

    def count(rows):
        refused = sum(1 for row in rows if 'refused' in row or 'refused' in (row.get('patched') or {}))
        return f'{len(rows)} ({refused} refused)'
    steps = sum(len(story['steps']) for story in said['stories'])
    print(f'{OUT.relative_to(REPO)}: {len(said["templates"])} templates, {len(said["triggers"])} triggers, '
          f'{len(said["first_day"])} janitors of the Host\'s own; options {count(said["options"])}, '
          f'attachments {count(said["attachments"])}, scopes {count(said["scopes"])}, '
          f'executions {count(said["executions"])}; {len(said["stories"])} stories of {steps} steps')
    return 0


if __name__ == '__main__':
    sys.exit(main())
