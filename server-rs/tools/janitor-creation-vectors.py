#!/usr/bin/env python3
"""Writes the vectors the Rust Host's making of a new agent as a janitor is tested against.

An app makes a janitor that is no agent yet with one request: a name, a tool, where it works, its
job, and an id of the request (`POST /janitors` with `name`, `backend`, `cwd` and `request_id`, no
`session`). The Python Host reserves a session for it first (`janitors.begin_creation`), makes the
agent as a janitor, gives it the job, and keeps what it answered (`complete_creation`): the same
request, sent again, makes nothing and is answered with what was answered before; the same id with
other settings is refused. This calls those functions on a database made for it and writes what
they answered to `crates/clarp-kernel/tests/fixtures/janitor-creation-vectors.json`.

    "$(sed -n 1p ~/.local/share/clarp/current/SERVICE_PYTHON)" server-rs/tools/janitor-creation-vectors.py

It runs in a process of its own whose every home, data, config and cache directory is a new
empty directory (the replay harness's own isolation, `tests/replay/host.py`), with no way out
of the machine. No Host is touched or started.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import re
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import scratch as tool_scratch

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = HERE.parent / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'janitor-creation-vectors.json'

NOW = 1_800_000_000_000
GOOD = {'name': 'Night Sweeper', 'backend': 'codex', 'cwd': '{work}'}
NIGHTLY = [{'attachment_id': 'made-nightly', 'trigger_id': 'schedule', 'config': {'cron': '15 3 * * *', 'timezone': 'Europe/Oslo'}}]
# One request each: the id it comes under, and what it says. `{work}` is a directory that is there.
BEGINS = [
    (None, GOOD), ('', GOOD), (7, GOOD), ('two words', GOOD), ('tab\there', GOOD), ('x' * 160, GOOD), ('x' * 161, GOOD),
    ('r-list', ['name']), ('r-voice', {**GOOD, 'voice_id': 'v'}), ('r-session', {**GOOD, 'session': ''}),
    ('r-no-name', {'backend': 'codex'}), ('r-empty-name', {**GOOD, 'name': '  '}), ('r-long-name', {**GOOD, 'name': 'n' * 101}),
    ('r-name-7', {**GOOD, 'name': 7}), ('r-hundred', {**GOOD, 'name': ' ' + 'n' * 100 + ' '}),
    ('r-abacus', {**GOOD, 'backend': 'abacus'}), ('r-backend-7', {**GOOD, 'backend': 7}), ('r-backend-none', {**GOOD, 'backend': None}),
    ('r-no-backend', {'name': 'Plain'}), ('r-cwd-7', {**GOOD, 'cwd': 7}), ('r-cwd-none', {**GOOD, 'cwd': None}),
    ('r-cwd-gone', {**GOOD, 'cwd': '{work}/not-there'}), ('r-cwd-empty', {**GOOD, 'cwd': '  '}),
    ('r-model-7', {**GOOD, 'model': 7}), ('r-effort-7', {**GOOD, 'effort': 7}),
    ('r-codex', GOOD), ('r-codex-model', {**GOOD, 'model': 'gpt-6-astra'}), ('r-codex-own', {**GOOD, 'model': 'gpt-6-sol'}),
    ('r-codex-no-model', {**GOOD, 'model': ''}), ('r-codex-none-model', {**GOOD, 'model': None}),
    ('r-codex-effort', {**GOOD, 'effort': ' HIGH '}), ('r-codex-no-effort', {**GOOD, 'effort': ''}),
    ('r-codex-extreme', {**GOOD, 'effort': 'extreme'}), ('r-codex-spaced', {**GOOD, 'model': '  gpt-6-sol  '}),
    ('r-claude', {**GOOD, 'backend': 'claude'}), ('r-claude-effort', {**GOOD, 'backend': 'claude', 'effort': 'high'}),
    ('r-no-job', {**GOOD, 'template_id': 'no-such-job'}), ('r-scheduled', {**GOOD, 'template_id': 'scheduled-prompt'}),
    ('r-options', {**GOOD, 'options': {'detail_level': 1}}), ('r-attachments', {**GOOD, 'attachments': [{'trigger_id': 'no-such'}]}),
    ('r-scope', {**GOOD, 'scope': {'agent_ids': ['nobody-has-this-id']}}), ('r-execution', {**GOOD, 'execution': {'provider': 'nobody'}}),
    ('r-local', {**GOOD, 'template_id': 'audio-bookkeeper', 'model': 'gpt-6-sol'}),
    ('r-local-plain', {**GOOD, 'template_id': 'audio-bookkeeper', 'model': '', 'effort': ''}),
    ('r-names', {**GOOD, 'name': '  Näme With Spaces & Signs!  '}), ('r-signs', {**GOOD, 'name': '!!!'}),
    ('r-long-slug', {**GOOD, 'name': 'a.b_c-' + 'Word' * 12}), ('r-tool-explainer', {**GOOD, 'template_id': 'tool-explainer'}),
]


@tool_scratch.case('clarp-janitor-creation-vectors-')
def run(root) -> dict:
    """Runs in a process of its own: what the Python Host makes of each request."""
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    from lib import agents, backends, db, janitors

    db.now_ms = lambda: NOW
    work = root / 'work'
    work.mkdir()
    home = str(pathlib.Path.home())
    made: dict = {}

    def named(value):
        """A value with the made-up directories and agents by their names, and what the Host drew for a
        session as that."""
        text = json.dumps(value)
        for agent_id, name in made.items():
            text = text.replace(agent_id, name)
        text = re.sub(r'-[0-9a-f]{12}(?=["\\])', '-<drawn>', text)
        return json.loads(text.replace(str(work), '/srv/work').replace(home, '/srv/home'))

    def filled(value):
        return json.loads(json.dumps(value).replace('{work}', str(work)))

    def attempt(call):
        try:
            return {'taken': named(call())}
        except janitors.JanitorError as refused:
            return {'refused': [refused.status, refused.code, named(str(refused))]}
        except Exception as broke:  # noqa: BLE001 - told by its kind: the Python Host breaks on this
            return {'broke': type(broke).__name__, 'said': named(str(broke))[:200]}

    begins = [{'request_id': request_id, 'payload': payload,
               **attempt(lambda: janitors.begin_creation(request_id, filled(payload)))}
              for request_id, payload in BEGINS]

    # One janitor made from its first request to its last answer, and what comes of the request
    # sent again at every point on the way.
    story = []
    asked = {'name': 'Made Whole', 'backend': 'codex', 'cwd': '{work}', 'template_id': 'scheduled-prompt',
             'attachments': NIGHTLY, 'options': {'instructions': 'Sweep.'}}
    fields = {key: asked[key] for key in ('template_id', 'attachments', 'options')}

    def step(what, do, **given):
        calls = {
            'begin': lambda: janitors.begin_creation(given['request_id'], filled(given['payload'])),
            'complete': lambda: janitors.complete_creation(given['request_id'], reserved['session']),
            'complete-elsewhere': lambda: janitors.complete_creation(given['request_id'], 'another-session'),
        }
        story.append({'what': what, 'do': do, 'given': given, **attempt(calls[do])})
        return story[-1]

    reserved: dict = {}
    first = janitors.begin_creation('story-1', filled(asked))
    reserved.update(first)
    story.append({'what': 'the first asking: a session is kept for it', 'do': 'begin',
                  'given': {'request_id': 'story-1', 'payload': asked}, 'taken': named(first)})
    step('asked again before anything was made', 'begin', request_id='story-1', payload=asked)
    step('the same id with other settings', 'begin', request_id='story-1', payload={**asked, 'name': 'Another'})
    step('finished before its agent was made', 'complete', request_id='story-1')
    identity = first['identity']
    agent_id = janitors.create_reserved_agent('story-1', persona=identity['name'], voice_id='', cwd=identity['cwd'],
                                              session=first['session'], backend=identity['backend'],
                                              model=identity['model'], effort=identity['effort'])
    made[agent_id] = 'id-made'
    row = agents.get_by_agent_id(agent_id)
    story.append({'what': 'its agent is made, as a janitor', 'do': 'made', 'given': {'request_id': 'story-1'},
                  'taken': named({'agent_id': agent_id, 'session': row['session'], 'persona': row['persona'],
                                  'backend': row['backend'], 'model': row['model'], 'effort': row['effort'],
                                  'cwd': row['cwd'], 'is_janitor': bool(row['is_janitor'])})})
    step('asked again when its agent is there', 'begin', request_id='story-1', payload=asked)
    janitors.create(first['session'], **filled(fields))
    story.append({'what': 'it is given its job', 'do': 'job', 'given': {'request_id': 'story-1', 'fields': fields},
                  'taken': named(janitors.get(first['session']))})
    step('finished for another session', 'complete-elsewhere', request_id='story-1')
    step('finished for a request nobody made', 'complete', request_id='story-nobody')
    step('finished', 'complete', request_id='story-1')
    step('asked again when it is finished', 'begin', request_id='story-1', payload=asked)
    step('finished again', 'complete', request_id='story-1')
    step('the same id with other settings, when it is finished', 'begin', request_id='story-1',
         payload={**asked, 'options': {'instructions': 'Other.'}})

    known = sorted(name for name in ('claude', 'codex', 'agy', 'grok', 'opencode') if backends.is_valid(name))
    return {'begins': begins, 'story': story,
            'default_models': {name: backends.by_id(name).janitor_default_model for name in known},
            'default_backend': backends.normalize('codex')}


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == '--run':
        print(json.dumps(run()))
        return 0
    ran = subprocess.run([sys.executable, '-I', __file__, '--run'], capture_output=True, text=True,
                         env=tool_scratch.child_env({'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}), check=False)
    if ran.returncode != 0:
        print(ran.stderr[-4000:], file=sys.stderr)
        return 1
    said = json.loads(ran.stdout.splitlines()[-1])
    out = ['{']
    parts = list(said)
    for index, part in enumerate(parts):
        rows = said[part]
        end = ',' if index < len(parts) - 1 else ''
        if isinstance(rows, list):
            out.append(f' "{part}": [')
            out.extend('  ' + json.dumps(row, ensure_ascii=True) + (',' if n < len(rows) - 1 else '')
                       for n, row in enumerate(rows))
            out.append(' ]' + end)
        else:
            out.append(f' "{part}": ' + json.dumps(rows, ensure_ascii=True) + end)
    out.append('}')
    OUT.write_text('\n'.join(out) + '\n')
    refused = sum(1 for row in said['begins'] if 'refused' in row)
    broke = [row['request_id'] for row in said['begins'] if 'broke' in row]
    print(f'{OUT.relative_to(REPO)}: {len(said["begins"])} first askings ({refused} refused, {len(broke)} that break the Python Host: '
          f'{broke}), a story of {len(said["story"])} steps; default models {said["default_models"]}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
