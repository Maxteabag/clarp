#!/usr/bin/env python3
"""Writes the vectors the Rust Host's words to an agent's tool are tested against.

Beside the user's own words, the Python Host tells a tool three things: who the agent is (its
name and how it speaks: `--append-system-prompt` for Claude Code), what holds for every turn
that comes through the Host (the guidance to use the Host's own tools, and whose turn this
is), and, for a turn whose answer is spoken, how to speak (which part is read aloud, how human
it sounds at the agent's level, how much it says while it works). Claude Code is told the
second and third by a hook with each prompt (`plugin/hooks/pwa_source_flag.py`); the other
tools at the head of the prompt (`server/lib/voice_preamble.py`).

This runs those functions themselves, for agents and settings made up here, and writes what
they said to `crates/clarp-kernel/tests/fixtures/told-vectors.json`, with the texts the Host carries as
data (`crates/clarp-kernel/src/told-texts.json`): the guidance, the two ways the rule of speaking is put,
the four ways of sounding human, and the four of saying what one is doing.

    "$(sed -n 1p ~/.local/share/clarp/current/SERVICE_PYTHON)" server-rs/tools/told-vectors.py

Each case runs in a process of its own whose every home, data, config and cache directory is a
new empty directory (the replay harness's own isolation, `tests/replay/host.py`), with no way
out of the machine. The database a case touches is a new one in that directory. No Host is
touched or started.
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
OUT = HERE.parent / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'told-vectors.json'
TEXTS = HERE.parent / 'crates' / 'clarp-kernel' / 'src' / 'told-texts.json'

GEMINI = '[tts]\nprovider = "gemini"\nfallback = "cartesia"\n'
CARTESIA = '[tts]\nprovider = "cartesia"\n'

# An agent as the Python Host has it in its table, and the settings of the room it is in.
CASES = [
    {'name': 'a persona that is built in, on a Host that speaks with Gemini', 'config': GEMINI,
     'agent': {'persona': 'Mike', 'session': 'mike-1a2b', 'personality': '', 'voice_verbosity': 0}},
    {'name': 'a personality of its own, and the engine that reads tags aloud', 'config': CARTESIA,
     'agent': {'persona': 'Nova', 'session': 'nova', 'voice_verbosity': 2,
               'personality': '  Personality: brisk, dry, exact.  '}},
    {'name': 'a name no persona has', 'config': '',
     'agent': {'persona': 'Codex-8194', 'session': 'codex-8194', 'personality': '', 'voice_verbosity': 1}},
    {'name': 'personalities switched off', 'config': GEMINI, 'personalities': False,
     'agent': {'persona': 'Rachel', 'session': 'rachel', 'personality': 'Personality: its own.',
               'voice_verbosity': 3}},
    {'name': 'a level of its own by its name, another for the Host', 'config': GEMINI,
     'humanness': {'default': 2, 'agents': {'CLAIRE': 9, 'somebody else': 0}},
     'agent': {'persona': 'Claire', 'session': 'claire-77', 'personality': '', 'voice_verbosity': 0}},
    {'name': 'a level of its own by its session', 'config': CARTESIA,
     'humanness': {'default': 7, 'agents': {'zed-01': 0}},
     'agent': {'persona': 'Zed', 'session': 'Zed-01', 'personality': '', 'voice_verbosity': 9}},
    {'name': 'an engine of its own', 'config': CARTESIA + '[tts.agents.astra]\nprovider = "gemini"\n',
     'humanness': {'default': 4},
     'agent': {'persona': 'Astra', 'session': 'astra', 'personality': '', 'voice_verbosity': -3}},
    {'name': 'nobody: a turn without an agent', 'config': '',
     'agent': {'persona': '', 'session': '', 'personality': '', 'voice_verbosity': 0}},
    {'name': 'a session without a name', 'config': '',
     'agent': {'persona': '  ', 'session': ' lone-session ', 'personality': '', 'voice_verbosity': 0}},
]

# What is said to the setting of how human voices sound (`POST /voice-humanness`), one after
# another in one room, each with what the setting is afterwards.
CHANGES = [
    None, [], 'seven', {}, {'default': 7}, {'default': 11}, {'default': '7'}, {'default': True}, {'default': None},
    {'default': 0, 'agents': {'Mike': 3, '  RACHEL ': 10}}, {'agents': {'mike': None}}, {'agents': {'rachel': ''}},
    {'agents': {'': 4}}, {'agents': {'zed': 12}}, {'agents': {'zed': '4'}}, {'agents': ['zed']}, {'agents': None},
    {'default': 4.0}, {'agents': {'a': 1, 'B': 2, 'c': False}}, {'agents': {'a': 1, 'B': 2, 'c': 0}},
    {'default': 99, 'agents': {'d': 5}}, {'unknown': 1},
]

# How human a voice sounds, at every level a setting can have and at some it cannot.
LEVELS = [-4, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 250, '7', 'much', None, 4.9, True]
# How much is said while working.
NARRATION = [-1, 0, 1, 2, 3, 4, '2', 'much', None, 2.9, True]
# What a user wrote, with what a tool that is told at the head of its prompt is given for it,
# and what is taken back off when the conversation is read.
WRITTEN = ['Hello there.', '', 'two\nlines', '--- user message ---', '[voice-mode] not a head\n\n--- user message ---\nx']


@tool_scratch.case('clarp-told-vectors-')
def one(root, case: dict) -> dict:
    """Runs in a process of its own: what the Python Host tells the tool of one agent."""
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    (root / 'scenario-config.toml').write_text(case['config'])
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    sys.path.insert(0, str(REPO / 'plugin' / 'hooks'))
    from lib import agents as agents_db, clarp_guidance, personalities, turn_command, voice_humanness, \
        voice_preamble, voice_verbosity
    import pwa_source_flag as hook
    agent = dict(case['agent'])
    # The one thing made up: the table of agents has this agent and no other.
    agents_db.get_by_session = lambda session: dict(agent) if session and session == agent['session'] else None
    if 'personalities' in case:
        personalities.update_settings({'enabled': case['personalities']})
    if 'humanness' in case:
        voice_humanness.update(case['humanness'])
    persona, session = agent['persona'], agent['session']
    told = {
        'who': voice_preamble.persona_identity_instruction(persona, session),
        'whose': clarp_guidance.identity_line(persona, session),
        'level': voice_humanness.level_for(agent),
        'gemini': voice_humanness.speaks_with_gemini(agent),
        'narration': voice_verbosity.narration_clause(agent['voice_verbosity']),
        # Claude Code, with each prompt, by its hook.
        'hook': {'silent': hook._build_additional_context(app_dispatched=True, voiced=False, agent=agent),
                 'spoken': hook._build_additional_context(app_dispatched=True, voiced=True, agent=agent)},
        # The other tools, in the words before the user's.
        'head': {'silent': voice_preamble.app_turn_instructions(voice=False, session=session),
                 'spoken': voice_preamble.app_turn_instructions(voice=True, session=session)},
        'prompt': {'silent': voice_preamble.apply_voice_preamble('Hello there.', voice=False, persona=persona,
                                                                 session=session),
                   'spoken': voice_preamble.apply_voice_preamble('Hello there.', voice=True, persona=persona,
                                                                 session=session)},
    }
    shared = {
        'guidance': clarp_guidance.CLARP_SKILLS_GUIDANCE,
        'levels': [{'level': level, 'plain': voice_humanness.guidance(level),
                    'gemini': voice_humanness.guidance(level, gemini=True)} for level in LEVELS],
        'narration': [{'level': level, 'clause': voice_verbosity.narration_clause(level)} for level in NARRATION],
        'options': voice_verbosity.options(),
        # What is put before the user's words when the app says its user began to speak before
        # hearing what the agent last said aloud.
        'unheard': {'heard': turn_command._with_delivery_context('Hello there.'),
                    'unheard': turn_command._with_delivery_context('Hello there.', unheard_audio=True)},
        'written': [{'text': text, 'given': voice_preamble.apply_voice_preamble(text, voice=False),
                     'read': voice_preamble.strip_voice_preamble(voice_preamble.apply_voice_preamble(text, voice=True)),
                     'read_plain': voice_preamble.strip_voice_preamble(text),
                     'read_quoted': voice_preamble.strip_voice_preamble(
                         '"' + voice_preamble.apply_voice_preamble(text, voice=False) + '"')}
                    for text in WRITTEN],
    }
    shutil.rmtree(root, ignore_errors=True)
    return {'told': told, 'shared': shared}


@tool_scratch.case('clarp-told-vectors-')
def changes(root) -> dict:
    """Runs in a process of its own: the setting of how human voices sound, changed step by step."""
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    sys.path.insert(0, str(REPO / 'plugin' / 'hooks'))
    from lib import voice_humanness, voice_preamble, voice_verbosity
    import pwa_source_flag as hook
    done = [{'given': '<nothing yet>', 'answered': voice_humanness.get(), 'refused': None, 'then': voice_humanness.get()}]
    for given in CHANGES:
        try:
            answered, refused = voice_humanness.update(given), None
        except ValueError as error:
            answered, refused = None, str(error)
        done.append({'given': given, 'answered': answered, 'refused': refused, 'then': voice_humanness.get()})
    texts = {
        'head': voice_preamble._VOICE_PREAMBLE_HEAD, 'split': voice_preamble._VOICE_PREAMBLE_SPLIT,
        'speak_by_hook': hook._SPEAK_INSTRUCTIONS, 'speak_at_head': voice_preamble._VOICE_INSTRUCTION,
        'neutral': voice_humanness._NEUTRAL, 'light': voice_humanness._LIGHT,
        'conversational': voice_humanness._CONVERSATIONAL, 'human': voice_humanness._HUMAN,
        'gemini_tags': voice_humanness._GEMINI_TAGS, 'tail': voice_humanness._TAIL,
        'narration': [voice_verbosity._CLAUSES[level] for level in range(4)],
        'narration_labels': [voice_verbosity.LABELS[level] for level in range(4)],
    }
    shutil.rmtree(root, ignore_errors=True)
    return {'changes': done, 'texts': texts}


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == '--case':
        print(json.dumps(one(CASES[int(sys.argv[2])])))
        return 0
    if len(sys.argv) > 1 and sys.argv[1] == '--changes':
        print(json.dumps(changes()))
        return 0
    cases, shared = [], None
    for index, case in enumerate(CASES):
        ran = subprocess.run([sys.executable, '-I', __file__, '--case', str(index)], capture_output=True, text=True,
                             env=tool_scratch.child_env({'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}), check=False)
        if ran.returncode != 0:
            print(ran.stderr[-3000:], file=sys.stderr)
            return 1
        answered = json.loads(ran.stdout.splitlines()[-1])
        assert shared in (None, answered['shared']), 'what does not depend on an agent is the same in every room'
        shared = answered['shared']
        cases.append({**case, 'told': answered['told']})
    ran = subprocess.run([sys.executable, '-I', __file__, '--changes'], capture_output=True, text=True,
                         env=tool_scratch.child_env({'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}), check=False)
    if ran.returncode != 0:
        print(ran.stderr[-3000:], file=sys.stderr)
        return 1
    changed = json.loads(ran.stdout.splitlines()[-1])
    shared['changes'] = changed['changes']
    texts = {'guidance': shared['guidance'], **changed['texts']}
    out = ['{', ' "about": "Written by server-rs/tools/told-vectors.py from plugin/hooks/pwa_source_flag.py and '
                'server/lib/voice_preamble.py, voice_humanness.py, voice_verbosity.py, clarp_guidance.py. Do not edit '
                'by hand.",']
    for key in ('guidance', 'options', 'unheard'):
        out.append(f' "{key}": ' + json.dumps(shared[key], ensure_ascii=True) + ',')
    for key in ('levels', 'narration', 'written', 'changes'):
        out.append(f' "{key}": [')
        rows = shared[key]
        out.extend('  ' + json.dumps(row, ensure_ascii=True) + (',' if at < len(rows) - 1 else '')
                   for at, row in enumerate(rows))
        out.append(' ],')
    out.append(' "cases": [')
    out.extend('  ' + json.dumps(case, ensure_ascii=True) + (',' if at < len(cases) - 1 else '')
               for at, case in enumerate(cases))
    out.append(' ]')
    out.append('}')
    OUT.write_text('\n'.join(out) + '\n')
    TEXTS.write_text(json.dumps(texts, indent=1, ensure_ascii=True) + '\n')
    print(f'wrote {OUT.relative_to(REPO)}: {len(cases)} agents, {len(shared["levels"])} levels, '
          f'{len(shared["narration"])} narration levels, {len(shared["written"])} texts, '
          f'{len(shared["changes"])} changes; {OUT.stat().st_size} bytes; and {TEXTS.relative_to(REPO)}: '
          f'{TEXTS.stat().st_size} bytes')
    return 0


if __name__ == '__main__':
    sys.exit(main())
