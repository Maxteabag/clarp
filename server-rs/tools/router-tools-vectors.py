#!/usr/bin/env python3
"""Writes the vectors of the tools the Rust Host's router of spoken messages may ask, and of how it asks each.

The Python Host's router asks any tool of the Host that can answer one question on its own
(`backends.routing_adapters`): the names it knows a provider by (`orchestrator_model.normalize_provider`,
`orchestrator.is_routing_provider`), the providers an app is offered (`orchestrator.provider_options`), how each
tool is started for one question (`routing_cmd`, `routing_env`) and where its answer is in what it printed
(`routing_text`). This runs those functions of the Python Host, as they are, and writes what they answered to
`crates/clarp-kernel/tests/fixtures/router-tools-vectors.json`.

    "$(sed -n 1p ~/.local/share/clarp/current/SERVICE_PYTHON)" server-rs/tools/router-tools-vectors.py

It runs in a process of its own whose every home, data, config and cache directory is a new empty directory (the
replay harness's own isolation, `tests/replay/host.py`), with no way out of the machine. No tool is started: a
command is only built, and an answer only read. Whether a tool's program is on this machine (`installed`) is left
out, and so is where the private home Antigravity is given lies (`routing_env`: only its names are kept, with
what of its user's home is linked and copied there).
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
OUT = HERE.parent / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'router-tools-vectors.json'

# The question a command is built for: where it stands in the command is marked by these words.
PROMPT = 'QUESTION'
# Names a provider is given: each tool's own, its other names, the two that are no tool, and what is none.
NAMES = ['claude', 'Claude', ' claude ', 'CLAUDE-CODE', 'claude-code', 'codex', 'Codex', 'agy', 'antigravity',
         'Antigravity ', 'grok', 'opencode', 'open-code', 'opencode-ai', 'deepseek', 'deep-seek', 'muse', 'muse-code',
         'musecode', 'openai', 'OpenAI', 'gpt', 'GPT', 'typesafe', 'jev', 'Jev', 'gemini', 'abacus', 'claude code',
         'code', '', '  ', 'agy\t']
# Models a command is built with: none, a name, one with blanks around it, and blanks alone.
MODELS = ['', 'm-1', ' m-1 ', '  ']
# What each tool printed for one question, besides what other vectors hold already.
PRINTED = {
    'claude': ['{"kind": "ignored"}', '', '  {"kind": "ignored"}\n', 'not json at all'],
    'codex': [
        '\n'.join(json.dumps(event) for event in [
            {'type': 'thread.started', 'thread_id': 't-1'},
            {'type': 'item.completed', 'item': {'type': 'reasoning', 'text': 'thinking'}},
            {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': '{"kind": "ignored"}'}},
            {'type': 'turn.completed', 'usage': {}}]),
        '\n'.join(json.dumps(event) for event in [
            {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'first'}},
            {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': '   '}},
            {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'last'}}]),
        '\n'.join(json.dumps(event) for event in [
            {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'the answer'}},
            {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': ' \n '}}]),
        '\n'.join(json.dumps(event) for event in [
            {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 7}},
            {'type': 'item.started', 'item': {'type': 'agent_message', 'text': 'not over'}},
            {'type': 'item.completed', 'item': 'no object'}, ['not', 'an', 'event']]),
        'not json\n{"type": "item.completed", "item": {"type": "agent_message", "text": "after noise"}}\n{broken',
        '',
    ],
    'agy': ['{"kind": "ignored"}', '', 'plain words'],
}


def run() -> dict:
    """Runs in a process of its own: what the Python Host's router knows of its tools."""
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    root = pathlib.Path(tempfile.mkdtemp(prefix='clarp-router-tools-vectors-', dir='/var/tmp'))
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    from lib import backends, orchestrator
    from lib.orchestrator_model import normalize_provider

    providers = [{key: row[key] for key in ('id', 'label', 'detail', 'kind', 'catalog_backend', 'effort_options')}
                 for row in orchestrator.provider_options()]
    names = [{'name': name, 'provider': normalize_provider(name), 'routes': orchestrator.is_routing_provider(name)}
             for name in NAMES]
    tools = []
    for adapter in backends.routing_adapters():
        efforts = [''] + list(adapter.efforts) + [' ' + (adapter.efforts or ('high',))[0].title() + ' ', '  ']
        commands = []
        for model in MODELS:
            for effort in efforts:
                cmd = adapter.routing_cmd(PROMPT, model=model, effort=effort)
                commands.append({'model': model, 'effort': effort, 'program': cmd[0], 'args': cmd[1:]})
        texts = []
        for printed in PRINTED.get(adapter.id, []):
            try:
                texts.append({'printed': printed, 'answer': adapter.routing_text(printed)})
            except ValueError as refused:
                texts.append({'printed': printed, 'refused': str(refused)})
        # What of its user's home a tool asked in a home of its own is given there: linked, or copied.
        home = {'linked': list(getattr(adapter, '_ONESHOT_LINKED', ())),
                'copied': list(getattr(adapter, '_ONESHOT_COPIED', ()))}
        tools.append({'id': adapter.id, 'efforts': list(adapter.efforts), 'env': sorted(adapter.routing_env()),
                      'home': home, 'commands': commands, 'texts': texts})
    return {'prompt': PROMPT, 'providers': providers, 'names': names, 'tools': tools}


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == '--run':
        print(json.dumps(run()))
        return 0
    ran = subprocess.run([sys.executable, '-I', __file__, '--run'], capture_output=True, text=True,
                         env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}, check=False)
    if ran.returncode != 0:
        print(ran.stderr[-5000:], file=sys.stderr)
        return 1
    said = json.loads(ran.stdout.splitlines()[-1])
    out = ['{']
    parts = list(said)
    for index, part in enumerate(parts):
        end = ',' if index < len(parts) - 1 else ''
        if part == 'tools':
            out.append(f' "{part}": [')
            out.extend('  ' + json.dumps(tool, ensure_ascii=True) + (',' if n < len(said[part]) - 1 else '')
                       for n, tool in enumerate(said[part]))
            out.append(' ]' + end)
        else:
            out.append(f' "{part}": ' + json.dumps(said[part], ensure_ascii=True) + end)
    out.append('}')
    OUT.write_text('\n'.join(out) + '\n')
    commands = sum(len(tool['commands']) for tool in said['tools'])
    texts = sum(len(tool['texts']) for tool in said['tools'])
    print(f'{OUT.relative_to(REPO)}: {len(said["providers"])} providers, {len(said["names"])} names, '
          f'{len(said["tools"])} tools, {commands} commands, {texts} answers read')
    return 0


if __name__ == '__main__':
    sys.exit(main())
