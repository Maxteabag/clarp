#!/usr/bin/env python3
"""Writes the vectors the Rust Host's schedules are tested against.

A schedule is a prompt an agent is sent at times a cron expression names, in UTC
(`server/lib/scheduler.py`): the owner or an agent makes one, turns it off and on, changes it and
removes it, and a runner sends what is due and moves each schedule on, so that none fires twice.

This runs that module itself and writes to `crates/clarp-kernel/tests/fixtures/schedule-vectors.json`
which expressions it takes and why not, when each runs next from moments made up here, and what
it answers through scripts of making, changing, running and removing, on a clock made up here.

    "$(sed -n 1p ~/.local/share/clarp/current/SERVICE_PYTHON)" server-rs/tools/schedule-vectors.py

It runs in a process of its own whose every home, data, config and cache directory is a new
empty directory (the replay harness's own isolation, `tests/replay/host.py`), with no way out
of the machine. The database it touches is a new one in that directory. No Host is touched or
started, and no agent is sent anything.
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
OUT = HERE.parent / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'schedule-vectors.json'

EXPRESSIONS = [
    '* * * * *', '*/5 * * * *', '0 * * * *', '0 0 * * *', '30 9 * * 1-5', '0 9,17 * * *', '15 10 1 * *',
    '0 0 29 2 *', '0 0 31 * *', '0 0 * * 0', '0 0 * * 7', '0 0 * * 6,7', '@hourly', '@daily', '@midnight', '@weekly',
    '@monthly', '  0 12 * * *  ', '0  12\t*  * *', '0-10/3 * * * *', '5/20 * * * *', '0 0 1-7 * 1', '59 23 31 12 *',
    '0 0 30 2 *', '0 0 31 4 *', '*/15 8-18 * 1,6,12 1-5', '0 22 * * 1-5', '1,1,1 * * * *', '0 0 * 2 *', '* * * * 1-7',
    '0 */6 * * *', ',5 * * * *', '5, * * * *', '50-70 * * * *', '*/100 * * * *', '+5 * * * *', '05 * * * *',
    '1_0 * * * *', ' 7 * * * *', '0 0 * * 0-7/2', '0 0 1,15 * *', '0 0 * 12 5', '10-20/5 3 * * *', '0 0 31 1,3 *',
    '0 12 29 2 1', '0 0 * * 3', '45 23 * * 6',
    # What is no expression, and why not.
    '', '   ', '* * * *', '* * * * * *', '60 * * * *', '* 24 * * *', '* * 0 * *', '* * 32 * *', '* * * 0 *', '* * * 13 *',
    '* * * * 8', 'a * * * *', '*/0 * * * *', '*/-1 * * * *', '1-a * * * *', 'a-1 * * * *', '5-1 * * * *', '@yearly',
    '@HOURLY', '*/x * * * *', '1.5 * * * *', '70-80 * * * *', '-5 * * * *', '5- * * * *', '*-5 * * * *', '1/2/3 * * * *',
    '*/ * * * *', '/5 * * * *', '1-2-3 * * * *', ', * * * *', '* * * * mon', '* * * jan *', '0x10 * * * *',
    '5 * * * * ', '٥ * * * *', '1 2 3 4 5 6', '0 0 0 0 0', '- * * * *', '*/5/2 * * * *', '3-1/1 * * * *',
]

# Moments a next run is asked from, as UTC milliseconds: an ordinary evening, the ends of a month,
# a year and a February before a leap day, the first moment there is, a Sunday midnight to the
# millisecond, and one just before a minute begins.
MOMENTS = [
    1_794_853_650_000,   # 2026-11-16 18:27:30
    1_796_083_199_999,   # 2026-11-30 23:59:59.999
    1_798_761_599_000,   # 2026-12-31 23:59:59
    1_835_395_140_000,   # 2028-02-28 23:59:00
    0,
    1_795_305_600_000,   # 2026-11-22 00:00:00, a Sunday
    1_794_853_679_999,   # 2026-11-16 18:27:59.999
    1_772_323_200_000,   # 2026-03-01 00:00:00
]

AGENTS = [
    {'agent_id': 'a-theo', 'session': 'theo-97e5', 'persona': 'Theo'},
    {'agent_id': 'a-mike', 'session': 'mike', 'persona': 'Mike'},
]
T0 = 1_794_853_650_000
MINUTE, HOUR, DAY = 60_000, 3_600_000, 86_400_000


def make(session, name, cron, prompt, **more):
    return {'do': 'create', 'session': session, 'name': name, 'cron': cron, 'prompt': prompt, **more}


def change(at, **more):
    return {'do': 'update', 'schedule': at, **more}


def remove(at):
    return {'do': 'delete', 'schedule': at}


def listed(**more):
    return {'do': 'list', **more}


def due():
    return {'do': 'due'}


def run():
    return {'do': 'run'}


def tick(ms):
    return {'do': 'tick', 'ms': ms}


SCRIPTS = [
    {'name': 'a schedule is made, listed, turned off and on, changed and removed',
     'steps': [listed(), make('theo-97e5', '  Morning report ', ' 0 9 * * 1-5 ', '  Write the morning report.  '), listed(),
               listed(session='theo-97e5'), listed(agent_id='a-theo'), listed(session='mike'), listed(agent_id='a-nobody'),
               make('a-mike', 'By the id', '*/5 * * * *', 'Look at the queue.'), make('mike', 'Off from the start', '@daily', 'Nothing.', enabled=False),
               listed(), listed(session='mike'), listed(agent_id='a-mike', session='theo-97e5'),
               change(0, enabled=False), change(0, enabled=False), change(0, enabled=True), tick(HOUR), change(0, enabled=True),
               change(0, name='  Evening report '), change(0, prompt=' Write the evening report. '), change(0, cron=' 0 18 * * * '),
               change(0, cron='nonsense'), change(0, cron='61 * * * *'), change(2, cron='@weekly'), change(2, enabled=True),
               change(2, cron='@monthly', enabled=False), change(2, name='Named while off', enabled=True),
               change('sched_nothing', enabled=True), change(1), remove(1), remove(1), remove('sched_nothing'), listed()]},
    {'name': 'what is no schedule is refused in words',
     'steps': [make('nobody-there', 'n', '* * * * *', 'p'), make('', 'n', '* * * * *', 'p'), make('theo-97e5', 'n', 'nonsense', 'p'),
               make('theo-97e5', 'n', '* * * *', 'p'), make('theo-97e5', 'n', '60 * * * *', 'p'), make('theo-97e5', 'n', '*/0 * * * *', 'p'),
               make('nobody-there', 'n', 'nonsense', 'p'), make('theo-97e5', '', '@hourly', ''), listed()]},
    {'name': 'what is due is run once and moved on',
     'steps': [make('theo-97e5', 'Every five', '*/5 * * * *', 'Five.'), make('mike', 'Hourly', '@hourly', 'Hour.'),
               make('mike', 'Off', '* * * * *', 'Never.', enabled=False), due(), run(), tick(2 * MINUTE), due(), tick(30_000), due(),
               run(), run(), due(), tick(5 * MINUTE), due(), tick(30 * MINUTE), due(), run(), due(), listed(),
               change(2, enabled=True), tick(MINUTE), due(), run(), remove(0), tick(DAY), due(), run(), run(), listed()]},
    {'name': 'a schedule whose time never comes has none, and runs never',
     'steps': [make('theo-97e5', 'Never', '0 0 30 2 *', 'Never.'), due(), tick(400 * DAY), due(), run(),
               make('theo-97e5', 'Leap', '0 0 29 2 *', 'Leap day.'), listed(), tick(500 * DAY), due(), run(), listed()]},
]


@tool_scratch.case('clarp-schedule-vectors-')
def run_all(root) -> dict:
    """Runs in a process of its own: what the Python Host's scheduler does."""
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    from lib import agents, scheduler
    from lib.db import conn

    def taken(expression):
        try:
            mins, hours, doms, months, dows = scheduler.parse_cron(expression)
        except ValueError as why:
            return {'expression': expression, 'refused': str(why)}
        return {'expression': expression, 'fields': [sorted(mins), sorted(hours), sorted(doms), sorted(months), sorted(dows)],
                'next': [scheduler.compute_next_run(expression, moment) for moment in MOMENTS]}

    # A schedule belongs to an agent the Host has: the agents are made in the new database, and the
    # ids it gave them are written here under the names this file knows them by.
    real = {agent['agent_id']: agents.create_agent(persona=agent['persona'], voice_id='', cwd=str(root),
                                                   session=agent['session'])
            for agent in AGENTS}
    named = {given: name for name, given in real.items()}

    def by_name(value):
        text = json.dumps(value)
        for given, name in named.items():
            text = text.replace(given, name)
        return json.loads(text)

    def played(script):
        with conn() as database:
            database.execute('DELETE FROM agent_schedules')
        state = {'now': T0, 'made': 0, 'ids': []}

        class Minted:
            def __init__(self, number):
                # A schedule's id is the first twelve signs of it.
                self.hex = f'{number:012x}' + '0' * 20

        def mint():
            state['made'] += 1
            return Minted(state['made'])

        scheduler.now_ms = lambda: state['now']
        scheduler.uuid.uuid4 = mint
        sent = []

        def which(step):
            at = step.get('schedule')
            return state['ids'][at] if isinstance(at, int) else at

        def act(step):
            do = step['do']
            if do == 'tick':
                state['now'] += step['ms']
                return None
            if do == 'create':
                more = {'enabled': step['enabled']} if 'enabled' in step else {}
                session = real.get(step['session'], step['session'])
                made = scheduler.create_schedule(session, step['name'], step['cron'], step['prompt'], **more)
                state['ids'].append(made['schedule_id'])
                return made
            if do == 'update':
                more = {key: step[key] for key in ('enabled', 'name', 'prompt') if key in step}
                if 'cron' in step:
                    more['cron_expression'] = step['cron']
                return scheduler.update_schedule(which(step), **more)
            if do == 'delete':
                return scheduler.delete_schedule(which(step))
            if do == 'list':
                agent_id = step.get('agent_id')
                return scheduler.list_schedules(session=step.get('session'), agent_id=real.get(agent_id, agent_id))
            if do == 'due':
                return scheduler.due_schedules(state['now'])
            if do == 'run':
                # The runner's own turn of the clock, with nothing else to look after.
                runner = scheduler.AgentScheduleRunner(lambda session, prompt: sent.append([session, prompt]))
                runner._stranded_parks = type('Nothing', (), {'tick': staticmethod(lambda now: None)})()
                return runner.tick()
            raise ValueError(do)

        rows = []
        for step in script['steps']:
            del sent[:]
            made = state['made']
            try:
                result, refused = act(step), None
            except ValueError as why:
                result, refused = None, str(why)
            row = {'step': dict(step), 'now': state['now'], 'result': result, 'refused': refused, 'sent': list(sent),
                   'minted': state['made'] - made}
            if 'schedule' in step:
                row['step']['schedule_id'] = which(step)
            rows.append(by_name(row))
        return {'name': script['name'], 'steps': rows}

    answered = {'moments': MOMENTS, 'expressions': [taken(expression) for expression in EXPRESSIONS], 'agents': AGENTS,
                'scripts': [played(script) for script in SCRIPTS]}
    shutil.rmtree(root, ignore_errors=True)
    return answered


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == '--run':
        print(json.dumps(run_all()))
        return 0
    ran = subprocess.run([sys.executable, '-I', __file__, '--run'], capture_output=True, text=True,
                         env=tool_scratch.child_env({'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}), check=False)
    if ran.returncode != 0:
        print(ran.stderr[-3000:], file=sys.stderr)
        return 1
    said = json.loads(ran.stdout.splitlines()[-1])
    out = ['{', ' "moments": ' + json.dumps(said['moments']) + ',', ' "agents": ' + json.dumps(said['agents']) + ',',
           ' "expressions": [']
    rows = said['expressions']
    out.extend('  ' + json.dumps(row, ensure_ascii=True) + (',' if at < len(rows) - 1 else '') for at, row in enumerate(rows))
    out.append(' ],')
    out.append(' "scripts": [')
    for at, script in enumerate(said['scripts']):
        out.append('  {"name": ' + json.dumps(script['name']) + ', "steps": [')
        rows = script['steps']
        out.extend('   ' + json.dumps(row, ensure_ascii=True) + (',' if index < len(rows) - 1 else '')
                   for index, row in enumerate(rows))
        out.append('  ]}' + (',' if at < len(said['scripts']) - 1 else ''))
    out.append(' ]')
    out.append('}')
    OUT.write_text('\n'.join(out) + '\n')
    taken = sum(1 for row in said['expressions'] if 'fields' in row)
    steps = sum(len(script['steps']) for script in said['scripts'])
    refused = sum(1 for script in said['scripts'] for row in script['steps'] if row['refused'])
    sent = sum(len(row['sent']) for script in said['scripts'] for row in script['steps'])
    print(f'{OUT.relative_to(REPO)}: {len(said["expressions"])} expressions ({taken} taken) from {len(said["moments"])} moments, '
          f'{len(said["scripts"])} scripts of {steps} steps ({refused} refused, {sent} prompts sent)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
