#!/usr/bin/env python3
"""How an explanation is put on the row of a tool while a turn is shown, as the Python Host does it
(`server/lib/live_explain.py`, `LiveExplainer`: it watches the events that show a turn, asks the
explainer about each new tool item at the Host's level, in rounds of at most eight items of an
agent, and patches what it learns into the item; an item that waits too long, falls off a full
backlog or outlives its turn is settled as skipped): scenes of events, rounds and moments, played
to that class with a stand-in for the explainer's service and a clock of the scene's own, written
to `crates/clarp-kernel/tests/fixtures/tool-explanation-rows-vectors.json` for the Rust Host's
watcher to be held to: what is patched into which item after every step, and what the service is
asked.

It runs in a process whose every home, data, config and cache directory is a new empty directory
(the replay harness's own isolation, `tests/replay/host.py`), with no way out to any network.
Nothing is explained by anybody: the stand-in answers by rule.

    nice "$(sed -n 1p ~/.local/share/clarp/current/SERVICE_PYTHON)" server-rs/tools/tool-explanation-rows-vectors.py
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
OUT = HERE.parent / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'tool-explanation-rows-vectors.json'


def tool(item, name='Bash', command=None, **more):
    """The op that shows a tool item, or shows it again."""
    shown = {'name': name, 'call_id': item, 'category': 'run', 'group': None, 'label': more.pop('label', command or name),
             'command': command, 'input_preview': more.pop('preview', {'command': command} if command else {}), **more}
    return {'op': 'upsert', 'kind': 'tool', 'id': item, 'item': {'status': 'running', 'tool': shown}}


def turn(turn_id, status='running'):
    return {'op': 'turn', 'conv': 'c', 'turn': {'turn_id': turn_id, 'status': status}}


def live(agent, *ops):
    return {'do': 'observe', 'event': {'type': 'live', 'agent_id': agent, 'ops': list(ops)}}


def at(seconds):
    return {'do': 'clock', 'seconds': seconds}


ROUND = {'do': 'round'}

# The stand-in for the explainer's service answers each item by the first word of its command:
#   ready ...     ready, with a sentence
#   wait-N ...    pending the first N times it is asked about, then ready
#   busy ...      busy every time
#   failed ...    failed, reason translator_failed
#   gone ...      cancelled
#   off ...       disabled
#   odd ...       a status nobody knows
#   empty ...     ready with no text
#   missing ...   not in the answer at all
# A scene's `service` may be `none` (there is no service) or `breaks` (asking it raises).
SCENES = [
    {'name': 'a tool item is announced as pending and settled when its answer comes',
     'steps': [live('a1', turn('t1'), tool('i1', command='ready ls')), ROUND, ROUND]},
    {'name': 'what is not a tool item, or names nothing, is passed over',
     'steps': [live('a1', turn('t1'), {'op': 'upsert', 'kind': 'message', 'id': 'm1', 'item': {'text': 'x'}},
                    {'op': 'append', 'kind': 'tool', 'id': 'i0'}, {'op': 'upsert', 'kind': 'tool', 'id': 'i1', 'item': {'tool': 'no table'}},
                    {'op': 'upsert', 'kind': 'tool', 'id': 'i2', 'item': {'tool': {'explain': {'status': 'ready'}}}},
                    {'op': 'upsert', 'kind': 'tool', 'id': 'i3', 'item': {}}, tool('i4', name='', command=None, label='only a label'),
                    tool('i5', name='Read', command=None, preview={'file_path': '/srv/a.rs', 'limit': 5})),
               {'do': 'observe', 'event': {'type': 'agent-state', 'agent_id': 'a1', 'ops': [tool('i9', command='ready x')]}},
               {'do': 'observe', 'event': {'type': 'live', 'agent_id': '', 'ops': [tool('i8', command='ready x')]}},
               {'do': 'observe', 'event': {'type': 'live', 'ops': [tool('i7', command='ready x')]}}, ROUND]},
    {'name': 'a tool shown again as it was is one item; shown with another command it is asked about anew',
     'steps': [live('a1', turn('t1'), tool('i1', command='wait-2 one')), live('a1', tool('i1', command='wait-2 one')), ROUND,
               live('a1', tool('i1', command='ready two')), ROUND, ROUND]},
    {'name': 'an answer that takes its time: asked again every round until it is there',
     'steps': [live('a1', turn('t1'), tool('i1', command='wait-3 x')), ROUND, at(0.7), ROUND, at(0.7), ROUND, at(0.7), ROUND, ROUND]},
    {'name': 'an item that waits too long is skipped',
     'steps': [live('a1', turn('t1'), tool('i1', command='busy x'), tool('i2', command='wait-99 y')), ROUND, at(44.9), ROUND, at(0.1), ROUND, ROUND]},
    {'name': 'when its turn ends an item has fifteen seconds more',
     'steps': [live('a1', turn('t1'), tool('i1', command='wait-99 x')), ROUND, at(10), live('a1', turn('t1', 'completed')), ROUND, at(14.9), ROUND,
               at(0.1), ROUND, ROUND]},
    {'name': 'a turn that ends late does not give an item more time than it had',
     'steps': [live('a1', turn('t1'), tool('i1', command='wait-99 x')), ROUND, at(40), live('a1', turn('t1', 'failed')), at(5), ROUND, ROUND]},
    {'name': 'a new turn takes the place of the items of the one before',
     'steps': [live('a1', turn('t1'), tool('i1', command='wait-99 x')), ROUND, live('a1', turn('t2'), tool('i2', command='ready y')), ROUND, ROUND,
               live('a1', turn('t2')), ROUND]},
    {'name': 'eight at once: those asked about keep their place, and the newest that wait go first',
     'steps': [live('a1', turn('t1'), *[tool(f'i{n}', command=f'wait-1 n{n}') for n in range(11)]), ROUND, ROUND, ROUND, ROUND]},
    {'name': 'a full backlog: the oldest is skipped for the newest',
     'steps': [live('a1', turn('t1'), *[tool(f'i{n}', command=f'wait-99 n{n}') for n in range(26)]), ROUND, at(46), ROUND]},
    {'name': 'two agents, each with its own round',
     'steps': [live('a1', turn('t1'), tool('i1', command='ready one')), live('a2', turn('t9'), tool('i1', command='wait-1 two')), ROUND, ROUND, ROUND]},
    {'name': 'what the service says of an item, in every way it can',
     'steps': [live('a1', turn('t1'), tool('i1', command='failed x'), tool('i2', command='gone x'), tool('i3', command='off x'),
                    tool('i4', command='odd x'), tool('i5', command='empty x'), tool('i6', command='missing x'), tool('i7', command='busy x')),
               ROUND, ROUND]},
    {'name': 'with explaining off nothing is watched', 'setting': {'enabled': False},
     'steps': [live('a1', turn('t1'), tool('i1', command='ready x')), ROUND]},
    {'name': 'for a reader who reads commands nothing is watched', 'setting': {'detail_level': 0},
     'steps': [live('a1', turn('t1'), tool('i1', command='ready x')), ROUND]},
    {'name': 'explaining turned off while an item waits',
     'steps': [live('a1', turn('t1'), tool('i1', command='wait-99 x')), ROUND, {'do': 'setting', 'change': {'enabled': False}}, ROUND, ROUND]},
    {'name': 'another reader chosen while an item waits',
     'steps': [live('a1', turn('t1'), tool('i1', command='wait-1 x')), ROUND, {'do': 'setting', 'change': {'detail_level': 4}}, ROUND, ROUND]},
    {'name': 'no service to ask', 'service': 'none', 'steps': [live('a1', turn('t1'), tool('i1', command='ready x')), ROUND, ROUND]},
    {'name': 'a service that breaks', 'service': 'breaks', 'steps': [live('a1', turn('t1'), tool('i1', command='ready x')), ROUND, ROUND]},
]


@tool_scratch.case('clarp-explainer-rows-')
def scene(root, number: int) -> dict:
    told = SCENES[number]
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    from lib import db, live_explain, tool_explanation_settings

    db.conn()
    if told.get('setting'):
        tool_explanation_settings.update(told['setting'])
    clock = {'now': 1000.0}
    patched, asked, seen = [], [], {}

    class Service:
        def request(self, level, items, *, cwd=None, release=None, target_agent_id=None):
            asked.append({'level': level, 'items': json.loads(json.dumps(items)), 'release': release, 'target': target_agent_id, 'cwd': cwd})
            if told.get('service') == 'breaks':
                raise RuntimeError('the stand-in for the service broke')
            answers = []
            for item in items:
                command = item['activity'].get('command') or ''
                word = command.split()[0] if command.split() else ''
                times = seen[item['id']] = seen.get(item['id'], 0) + 1
                if word == 'missing':
                    continue
                if word.startswith('wait-') and times <= int(word[5:]):
                    answers.append({'id': item['id'], 'status': 'pending'})
                elif word == 'busy':
                    answers.append({'id': item['id'], 'status': 'busy', 'reason': 'queue_full'})
                elif word == 'failed':
                    answers.append({'id': item['id'], 'status': 'failed', 'reason': 'translator_failed'})
                elif word == 'gone':
                    answers.append({'id': item['id'], 'status': 'cancelled'})
                elif word == 'off':
                    answers.append({'id': item['id'], 'status': 'disabled'})
                elif word == 'odd':
                    answers.append({'id': item['id'], 'status': 'unheard-of'})
                elif word == 'empty':
                    answers.append({'id': item['id'], 'status': 'ready', 'text': ''})
                else:
                    answers.append({'id': item['id'], 'status': 'ready', 'text': f'Explains {command}.', 'source': 'llm', 'cached': False})
            return {'model': 'stand-in', 'detail_level': level, 'items': answers}

    service = None if told.get('service') == 'none' else Service()
    explainer = live_explain.LiveExplainer(lambda: service, lambda agent, item, fields: patched.append([agent, item, json.loads(json.dumps(fields))]),
                                           clock=lambda: clock['now'])
    # The rounds are the scene's: no thread of its own.
    explainer._start = lambda: None
    steps = []
    for step in told['steps']:
        done = dict(step)
        before = (len(patched), len(asked))
        if step['do'] == 'observe':
            explainer.observe(json.loads(json.dumps(step['event'])))
        elif step['do'] == 'round':
            explainer._round()
        elif step['do'] == 'clock':
            clock['now'] += step['seconds']
        elif step['do'] == 'setting':
            tool_explanation_settings.update(step['change'])
        done['at_ms'] = round((clock['now'] - 1000.0) * 1000)
        done['patched'] = patched[before[0]:]
        done['asked'] = asked[before[1]:]
        done['work'] = explainer._has_work()
        steps.append(done)
    return {'name': told['name'], 'setting': {**{'enabled': True, 'detail_level': 2}, **(told.get('setting') or {})},
            'service': told.get('service') or 'rule', 'steps': steps}


def main() -> int:
    if len(sys.argv) > 2 and sys.argv[1] == '--scene':
        print(json.dumps(scene(int(sys.argv[2]))))
        return 0
    scenes = []
    for number, told in enumerate(SCENES):
        ran = subprocess.run([sys.executable, '-I', __file__, '--scene', str(number)], capture_output=True, text=True,
                             env=tool_scratch.child_env({'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}), check=False)
        if ran.returncode != 0:
            print(f'{told["name"]}: {ran.stderr[-3000:]}', file=sys.stderr)
            return 1
        scenes.append(json.loads(ran.stdout.splitlines()[-1]))
    out = ['{', ' "scenes": [']
    for n, done in enumerate(scenes):
        out.append('  {')
        out.extend(f'   {json.dumps(key)}: {json.dumps(done[key], ensure_ascii=True)},' for key in done if key != 'steps')
        out.append('   "steps": [')
        out.extend('    ' + json.dumps(step, ensure_ascii=True) + (',' if m < len(done['steps']) - 1 else '') for m, step in enumerate(done['steps']))
        out.append('   ]')
        out.append('  }' + (',' if n < len(scenes) - 1 else ''))
    out.append(' ]')
    out.append('}')
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text('\n'.join(out) + '\n')
    patches = [patch for done in scenes for step in done['steps'] for patch in step['patched']]
    by = {}
    for _, _, fields in patches:
        explain = fields['tool']['explain']
        key = explain['status'] + (':' + explain['reason'] if explain.get('reason') else '')
        by[key] = by.get(key, 0) + 1
    print(f'{OUT.relative_to(REPO)}: {len(scenes)} scenes, {sum(len(done["steps"]) for done in scenes)} steps, {len(patches)} patches {by}, '
          f'the service asked {sum(len(step["asked"]) for done in scenes for step in done["steps"])} times about '
          f'{sum(len(call["items"]) for done in scenes for step in done["steps"] for call in step["asked"])} items')
    return 0


if __name__ == '__main__':
    sys.exit(main())
