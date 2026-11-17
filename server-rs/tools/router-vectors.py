#!/usr/bin/env python3
"""Writes the vectors the Rust Host's router of spoken messages is tested against: its pure rules.

A message that is dictated hands-free and names no agent can be given to a model that says whom it
is for. The Python Host's router is `server/lib/orchestrator*.py`. This writes down what its pure
parts make of inputs chosen for them: the prompt a model is given for a packet (`_model_prompt`),
the JSON found in what a model answered (`_extract_json`), the decision read from it
(`parse_decision`) and what of a decision an app is told (`_public_decision`), when the router
runs at all (`should_run`), when it asks once more with every agent in view
(`_should_scan_broader`), and the names a provider goes by (`normalize_provider`). To
`crates/clarp-kernel/tests/fixtures/router-vectors.json`.

    "$(sed -n 1p ~/.local/share/clarp/current/SERVICE_PYTHON)" server-rs/tools/router-vectors.py

It runs in a process of its own whose every home, data, config and cache directory is a new
empty directory (the replay harness's own isolation, `tests/replay/host.py`), with no way out
of the machine. No Host is touched or started and no model is asked anything.
"""
from __future__ import annotations

import dataclasses
import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = HERE.parent / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'router-vectors.json'

ANSWERS = [
    '', '   ', 'no json here', '{"kind": "agent_message", "target_session": "mike"}',
    '  {"kind":"ignored","confidence":0.9}\n', '```json\n{"kind": "clarify", "spoken_text": "Who?"}\n```',
    '```\n{"kind": "clarify"}\n```', '```JSON\n{"kind": "control"}```', 'Here you go: {"kind": "status_query"} thanks',
    'first {"a": 1} then {"b": 2}', '[1, 2]', '"text"', '7', 'null', '{"kind": "agent_message", }',
    'x {"kind": "agent_message", "text_to_send": "say {hello}"} y', '{"kind": "agent_message"',
    '```json\n[1]\n```', '{"näme": "ä"}', '{"a": {"b": {"c": 1}}}',
]
DECISIONS = [
    {}, {'kind': 'agent_message', 'target_session': ' mike ', 'confidence': 0.91, 'addressing': True,
         'text_to_send': ' fix the build ', 'reason': ' named '},
    {'decision_kind': 'status_query', 'target_session': 'mike', 'status_text': ' busy '},
    {'kind': '', 'decision_kind': 'control', 'control_action': ' repeat_last '},
    {'kind': 'no-such-kind', 'confidence': 2}, {'kind': 'ignored', 'confidence': -1},
    {'kind': 'ignored', 'confidence': '0.5'}, {'kind': 'ignored', 'confidence': 'high'},
    {'kind': 'ignored', 'confidence': None}, {'kind': 'ignored', 'confidence': True},
    {'kind': 'ignored', 'confidence': [1]}, {'kind': 'ignored', 'confidence': 1e300},
    {'kind': 'agent_control', 'control_action': 'stop', 'control_payload': {'force': True}, 'target_session': 'mike'},
    {'kind': 'agent_control', 'control_payload': ['x']},
    {'kind': 'recipient_correction', 'mentioned_sessions': ['mike', 7, None], 'name_corrections': [{'heard': 'Mark', 'meant': 'Mike'}],
     'candidate_scores': [{'session': 'mike', 'score': 0.7}]},
    {'kind': 'clarify', 'mentioned_sessions': 'mike', 'name_corrections': {'a': 1}, 'candidate_scores': 'x',
     'spoken_text': ' Was that for Mike? ', 'phrase_key': ' clarify_target ', 'pending_id': ' p1 '},
    {'kind': 'ambiguous', 'addressing': 'yes', 'error': ' boom '}, {'kind': 'error', 'addressing': 0, 'target_session': 7},
    {'kind': ' agent_message ', 'text_to_send': 7, 'reason': None},
    {'kind': 'AGENT_MESSAGE'},
]
PACKETS = [
    {'utterance': 'fix the build', 'requested_session': 'mike', 'agents': []},
    {'utterance': 'für Zoë — bitte', 'fallback_request': True, 'agents': [{'session': 'zoe', 'persona': 'Zoë'}],
     'settings': {'confidence_threshold': 0.78}},
    {'utterance': 'x', 'fallback_request': 0}, {'utterance': 'x', 'fallback_request': 'yes', 'n': 1.5, 'none': None, 'flag': False},
]
PROVIDERS = [None, '', ' OpenAI ', 'openai', 'gpt', 'chatgpt', 'open-ai', 'typesafe', 'Jev', 'claude-code', 'claude', 'Claude',
             'codex', 'antigravity', 'agy', 'open-code', 'opencode', 'grok', 'Abacus', ' codex ']


def run() -> dict:
    """Runs in a process of its own: what the Python Host's router makes of each."""
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    root = pathlib.Path(tempfile.mkdtemp(prefix='clarp-router-vectors-', dir='/var/tmp'))
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    from lib import orchestrator
    from lib.orchestrator_types import OrchestratorDecision, OrchestratorSettings

    def attempt(call):
        try:
            return {'is': call()}
        except Exception as refused:  # noqa: BLE001 - told by its kind and its words
            return {'refused': [type(refused).__name__, str(refused)[:200]]}

    def decision(raw):
        read = orchestrator.parse_decision(raw)
        whole = dataclasses.asdict(read)
        whole.pop('raw')
        return {'read': whole, 'told': orchestrator._public_decision(read)}

    settings = [OrchestratorSettings(enabled=on, hands_free_only=hands, fallback_only=fallback)
                for on in (True, False) for hands in (True, False) for fallback in (True, False)]
    runs = [{'enabled': s.enabled, 'hands_free_only': s.hands_free_only, 'fallback_only': s.fallback_only,
             'hands_free': hands_free, 'fallback_request': fallback_request,
             'is': orchestrator.should_run(s, hands_free=hands_free, fallback_request=fallback_request)}
            for s in settings for hands_free in (True, False) for fallback_request in (True, False)]
    broader = []
    for scope in ('focused', 'all', None):
        for kind in ('agent_message', 'status_query', 'control', 'agent_control', 'recipient_correction', 'clarify',
                     'ambiguous', 'ignored', 'error'):
            for target in ('', 'mike', 'anna', ' mike '):
                for confidence in (0.0, 0.57, 0.58, 0.84, 0.85, 1.0):
                    read = OrchestratorDecision(kind=kind, target_session=target, confidence=confidence)
                    packet = {'context_scope': scope} if scope else {}
                    broader.append([scope, kind, target, confidence,
                                    orchestrator._should_scan_broader(read, packet, 'mike')])
    return {
        'answers': [{'text': text, **attempt(lambda: orchestrator._extract_json(text))} for text in ANSWERS],
        'decisions': [{'raw': raw, **decision(raw)} for raw in DECISIONS],
        'prompts': [{'packet': packet, 'is': orchestrator._model_prompt(packet)} for packet in PACKETS],
        'runs': runs, 'broader': broader,
        'providers': [[name, orchestrator.normalize_provider(name)] for name in PROVIDERS],
        'defaults': dataclasses.asdict(OrchestratorSettings()),
    }


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == '--run':
        print(json.dumps(run()))
        return 0
    ran = subprocess.run([sys.executable, '-I', __file__, '--run'], capture_output=True, text=True,
                         env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}, check=False)
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
    refused = sum(1 for row in said['answers'] if 'refused' in row)
    print(f'{OUT.relative_to(REPO)}: {len(said["answers"])} answers ({refused} with no JSON in them), '
          f'{len(said["decisions"])} decisions, {len(said["prompts"])} prompts, {len(said["runs"])} cases of whether it runs, '
          f'{len(said["broader"])} of whether it asks again, {len(said["providers"])} names of providers')
    return 0


if __name__ == '__main__':
    sys.exit(main())
