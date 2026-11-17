#!/usr/bin/env python3
"""The models an app's picker offers for a tool, as the Python Host finds them
(`server/lib/provider_capabilities.py`): what its own functions make of made-up answers of the
tools (Codex's list of models, the cache Claude Code keeps of the models an account may run, a
tool's version), and what it says of a tool that is there, is not there, or says nothing; written
to `crates/clarp-backend/tests/fixtures/model-catalogue-vectors.json`, so that the Rust Host's
catalogue is held to it. And what the Python Host declares of the two tools the Rust Host runs
(`server/lib/backend_catalogue.py`): how a tool is shown and the models it is known to have when
nothing can be asked, written to `crates/clarp-backend/src/catalogue.json`, which the Rust Host
reads as its own table.

It runs in a process of its own whose every home, data, config and cache directory is a new empty
directory (the replay harness's own isolation, `tests/replay/host.py`), with no way out to any
network. No tool is started: what a tool would answer is given by the stories below.

    nice "$(sed -n 1p ~/.local/share/clarp/current/SERVICE_PYTHON)" server-rs/tools/model-catalogue-vectors.py
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
OUT = HERE.parent / 'crates' / 'clarp-backend' / 'tests' / 'fixtures' / 'model-catalogue-vectors.json'
TABLE = HERE.parent / 'crates' / 'clarp-backend' / 'src' / 'catalogue.json'
TOOLS = ('claude', 'codex')
AT = '2027-01-15T08:00:00Z'


def level(effort, **more):
    return {'effort': effort, 'description': 'synthetic', **more}


def codex_model(slug, **more):
    return {'slug': slug, 'display_name': more.pop('display_name', slug.upper()), 'visibility': more.pop('visibility', 'list'),
            'default_reasoning_level': more.pop('default', 'medium'),
            'supported_reasoning_levels': more.pop('levels', [level('low'), level('medium'), level('high')]), **more}


CODEX_LIST = {'models': [
    codex_model('gpt-synthetic-sol', display_name='GPT Synthetic Sol', levels=[level('low'), level('medium'), level('high'), level('xhigh')]),
    codex_model('gpt-synthetic-luna', display_name='  GPT Synthetic Luna  ', default=' LOW '),
    codex_model('gpt-hidden', visibility='hide'),
    codex_model('gpt-synthetic-terra', display_name='', default=''),
]}

# What Codex may answer when it is asked for its models: a name, and the text.
CODEX_SAID = [
    ('a list as Codex writes one', json.dumps(CODEX_LIST)),
    ('nothing', ''),
    ('no JSON', 'error: not signed in'),
    ('a list and no object', '[1, 2]'),
    ('no models', '{"other": 1}'),
    ('models that are no list', '{"models": {"slug": "x"}}'),
    ('an empty list', '{"models": []}'),
    ('what is no model', json.dumps({'models': [1, 'text', None, [], {'slug': 7, 'visibility': 'list'},
                                               {'slug': '   ', 'visibility': 'list'}, {'visibility': 'list'},
                                               {'slug': 'not-shown'}, {'slug': 'not-listed', 'visibility': 'LIST'}]})),
    ('the same model twice', json.dumps({'models': [codex_model('gpt-twice', display_name='First'),
                                                    codex_model(' gpt-twice ', display_name='Second')]})),
    ('names', json.dumps({'models': [codex_model('gpt-a', display_name=7), codex_model('gpt-b', display_name=None),
                                     codex_model('gpt-c', display_name='   '), {'slug': 'gpt-d', 'visibility': 'list'}]})),
    ('efforts', json.dumps({'models': [
        codex_model('gpt-none-said', levels=None), codex_model('gpt-none-there', levels=[]),
        codex_model('gpt-all-malformed', levels=[1, 'low', {'effort': 7}, {'no': 'effort'}]),
        codex_model('gpt-some-malformed', levels=[1, level(' HIGH '), level('high'), level(''), level('low')]),
        codex_model('gpt-no-list', levels={'effort': 'low'}), codex_model('gpt-a-word', levels='low'),
        codex_model('gpt-default-number', default=3), codex_model('gpt-default-nothing', default=None),
        codex_model('gpt-default-blank', default='   '), codex_model('gpt-default-padded', default='  XHigh '),
    ]})),
]


def option(value, label='', description=None, **more):
    return {'value': value, 'label': label, **({'description': description} if description is not None else {}), **more}


# What Claude Code's own file may hold of the models an account can run: a name, and the text.
CLAUDE_KEPT = [
    ('as Claude Code writes it', json.dumps({'oauthAccount': {'accountUuid': 'synthetic'}, 'modelAccessCache': [],
                                             'additionalModelOptionsCache': [
                                                 option('claude-synthetic-9-9[1m]', 'Synthetic', 'Synthetic 9.9 · The most capable')]})),
    ('nothing', ''),
    ('no JSON', 'not json'),
    ('a list and no object', '[1]'),
    ('neither cache', '{"oauthAccount": {}}'),
    ('caches of words', json.dumps({'modelAccessCache': ['sonnet', ' opus ', 'claude-synthetic-1', 'cc-update-required-1', '', 'default',
                                                         'gpt-not-claudes', 'OPUS', 'haiku[1m]', 'fable[1M]', 'best', 'opusplan']})),
    ('a cache that is an object', json.dumps({'modelAccessCache': {'one': 'sonnet', 'two': option('claude-synthetic-2', 'Two'),
                                                                  'three': 7}})),
    ('a cache that is neither', json.dumps({'modelAccessCache': 'sonnet', 'additionalModelOptionsCache': 7})),
    ('how an entry names its model', json.dumps({'additionalModelOptionsCache': [
        {'id': 'claude-by-id'}, {'model': 'claude-by-model'}, {'value': '', 'id': 'claude-by-id-after-nothing'},
        {'value': 7}, {'value': None, 'model': ' claude-padded '}, {}, 7, None, ['sonnet']]})),
    ('how an entry is called', json.dumps({'additionalModelOptionsCache': [
        option('claude-a', 'Label'), {'value': 'claude-b', 'display_name': 'Display'}, {'value': 'claude-c', 'name': 'Name'},
        {'value': 'claude-d'}, {'value': 'claude-e', 'label': 7}, {'value': 'claude-f', 'label': '   '},
        option('claude-g', 'Short', 'Long 1.0 · and more · and more'), option('claude-h', 'Short', '   '),
        option('claude-i', 'Short', ' · nothing before the point'), option('claude-j', 'Short', 7),
        option('claude-k', '', 'Only a description')]})),
    ('the same model in both caches', json.dumps({'modelAccessCache': ['sonnet', option('claude-twice', 'First')],
                                                  'additionalModelOptionsCache': [option('claude-twice', 'Second'), 'sonnet',
                                                                                  option('opus', 'Opus by its option')]})),
]

# A tool on a machine: (a name, the tool, whether it is there, what it says of its version, what is
# asked of it for its models). The version: (exit code, what it printed, what it wrote aside) or
# None where starting it fails. The models: for Codex the same of `debug models`; for Claude Code
# the text of its file, or None where it has none.
VERSION = (0, '9.9.9 (Synthetic Code)\nmore lines\n', '')
STORIES = [
    ('codex, there and answering', 'codex', True, VERSION, (0, json.dumps(CODEX_LIST), '')),
    ('codex, not there', 'codex', False, None, None),
    ('codex, there, its models asked in vain', 'codex', True, VERSION, (1, json.dumps(CODEX_LIST), 'failed')),
    ('codex, there, it cannot be started for its models', 'codex', True, VERSION, None),
    ('codex, there, it lists nothing', 'codex', True, VERSION, (0, '{"models": []}', '')),
    ('codex, there, a model the Host knows and one it does not', 'codex', True, VERSION,
     (0, json.dumps({'models': [codex_model('gpt-6-luna', display_name='Luna as Codex calls it', levels=[level('low')]),
                                codex_model('gpt-brand-new')]}), '')),
    ('codex, its version on the other stream', 'codex', True, (0, '', 'codex-cli 9.9.9\n'), (0, json.dumps(CODEX_LIST), '')),
    ('codex, no version said', 'codex', True, (0, '  \n', ''), (0, json.dumps(CODEX_LIST), '')),
    ('codex, its version asked in vain', 'codex', True, (2, 'usage', ''), (0, json.dumps(CODEX_LIST), '')),
    ('codex, it cannot be started for its version', 'codex', True, None, (0, json.dumps(CODEX_LIST), '')),
    ('codex, a long version', 'codex', True, (0, 'v' * 300 + '\n', ''), (0, json.dumps(CODEX_LIST), '')),
    ('claude, there, with what an account may run', 'claude', True, VERSION, CLAUDE_KEPT[0][1]),
    ('claude, not there', 'claude', False, None, CLAUDE_KEPT[0][1]),
    ('claude, there, without its file', 'claude', True, VERSION, None),
    ('claude, there, its file says nothing of models', 'claude', True, VERSION, '{"oauthAccount": {}}'),
    ('claude, there, a model the Host knows under another name', 'claude', True, VERSION,
     json.dumps({'additionalModelOptionsCache': [option('sonnet', 'Sonnet as the account calls it'),
                                                 option('claude-brand-new-1', 'Brand new')]})),
]


def run() -> dict:
    """Runs in a process of its own: what the Python Host makes of the stories above."""
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    root = pathlib.Path(tempfile.mkdtemp(prefix='clarp-model-catalogue-vectors-', dir='/var/tmp'))
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    from lib import backend_catalogue
    from lib import provider_capabilities as found
    from lib.backend.registry import catalogue_fields

    table = {}
    for tool in TOOLS:
        definition = backend_catalogue.get(tool)
        adapter = found._adapter(tool)
        probe = definition.probe
        table[tool] = {
            'shown': catalogue_fields(tool),
            'program': adapter.required_binary,
            'efforts': list(adapter.efforts),
            'effort_scope': adapter.effort_scope,
            'models': [{key: row[key] for key in ('id', 'label', 'default_effort', 'supported_efforts')}
                       for row in found._fallback_models(tool, AT)],
            'asked': {'how': probe.operation, 'with': list(probe.argv), 'detail': probe.detail,
                      'source_kind': probe.source_kind, 'known_too': probe.append_fallback,
                      'within_s': probe.timeout},
        }

    codex = [{'name': name, 'said': said, 'models': found.parse_codex_models(said, observed_at=AT)}
             for name, said in CODEX_SAID]
    claude = [{'name': name, 'kept': kept, 'models': found.parse_claude_models(kept, observed_at=AT)}
              for name, kept in CLAUDE_KEPT]

    stories = []
    for name, tool, there, version, models in STORIES:
        kept = root / f'claude-{len(stories)}.json'
        if tool == 'claude' and models is not None:
            kept.write_text(models)
        found.claude_model_cache_path = lambda kept=kept: kept

        def answered(argv, timeout=None, version=version, models=models, tool=tool):
            what = version if argv[1:] == ['--version'] else (models if tool == 'codex' else None)
            if what is None:
                raise OSError('synthetic: it cannot be started')
            return subprocess.CompletedProcess(argv, what[0], stdout=what[1], stderr=what[2])

        row = found._discover_provider(tool, AT, resolve=lambda _tool, there=there, tool=tool: f'/synthetic/bin/{tool}' if there else None,
                                       run=answered)
        stories.append({'name': name, 'tool': tool, 'there': there,
                        'version': None if version is None else {'exit': version[0], 'out': version[1], 'err': version[2]},
                        'models': (None if models is None else {'exit': models[0], 'out': models[1]}) if tool == 'codex' else models,
                        'row': row})
    whole = found._build_catalog(wall_time=1_800_000_000.0, which=lambda _tool: None,
                                 run=lambda argv, timeout=None: (_ for _ in ()).throw(OSError('never asked')))
    said = found.endpoint_response.__wrapped__ if hasattr(found.endpoint_response, '__wrapped__') else None
    del said
    around = {key: value for key, value in whole.items() if key != 'providers'}
    around['observed_at_of'] = 1_800_000_000
    around['providers'] = list(whole['providers'])
    return {'table': table, 'codex': codex, 'claude': claude, 'stories': stories, 'whole': around,
            'note': {'freshness_scope': 'response_observation',
                     'freshness_note': ('Top-level freshness describes cache age only; inspect each '
                                        'provider and model source for evidence freshness.')}}


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
    TABLE.write_text(json.dumps(said['table'], indent=1, ensure_ascii=True) + '\n')
    out = ['{']
    for key in ('codex', 'claude', 'stories'):
        rows = said[key]
        out.append(f' "{key}": [')
        out.extend('  ' + json.dumps(row, ensure_ascii=True) + (',' if n < len(rows) - 1 else '') for n, row in enumerate(rows))
        out.append(' ],')
    out.append(' "whole": ' + json.dumps(said['whole'], ensure_ascii=True) + ',')
    out.append(' "note": ' + json.dumps(said['note'], ensure_ascii=True))
    out.append('}')
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text('\n'.join(out) + '\n')
    print(f'{TABLE.relative_to(REPO)}: ' + '; '.join(f'{tool}: {len(row["models"])} models known, efforts {row["efforts"]}'
                                                     for tool, row in said['table'].items()))
    print(f'{OUT.relative_to(REPO)}: {len(said["codex"])} answers of Codex ({sum(len(r["models"]) for r in said["codex"])} models), '
          f'{len(said["claude"])} files of Claude Code ({sum(len(r["models"]) for r in said["claude"])} models), '
          f'{len(said["stories"])} stories')
    for row in said['stories']:
        r = row['row']
        print(f'  {row["name"]}: installed={r["installed"]} availability={r["availability"]} version={r["cli_version"]!r:.40} '
              f'models={len(r["models"])} source={r["source"]["kind"]}')
    print('  the whole:', said['whole'])
    return 0


if __name__ == '__main__':
    sys.exit(main())
