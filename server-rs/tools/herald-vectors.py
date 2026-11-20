#!/usr/bin/env python3
"""Writes the vectors the Rust Host's herald is tested against.

When an agent says something aloud and its user is not with that agent (it is not the open
one, and not the one a message was just sent to), the Python Host does not play the reply. It
plays a short clip, "<name> here, ready for an update.", and holds the reply until the user
opens that agent, writes to it, or says yes and its name (`server/lib/herald.py`,
`server/lib/intent.py`). A reply of a few words is played at once.

This runs that manager itself, with a stream, a voice and a clock made up here, through
scripts of what happens (clips arrive, the user opens an agent, sends, speaks, time passes,
the settings change, the voice fails), and writes what the manager did after each step to
`crates/clarp-kernel/tests/fixtures/herald-vectors.json`; and the classifier of what a user
said, for utterances and names made up here.

    "$(sed -n 1p ~/.local/share/clarp/current/SERVICE_PYTHON)" server-rs/tools/herald-vectors.py

It runs in a process of its own whose every home, data, config and cache directory is a new
empty directory (the replay harness's own isolation, `tests/replay/host.py`), with no way out
of the machine. No Host is touched or started, and nothing is said by any engine.
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
OUT = HERE.parent / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'herald-vectors.json'

AGENTS = {'rachel': 'Rachel', 'mike': 'Mike', 'bella': 'Bella', 'anna-maria': 'Anna Maria', 'nameless': ''}


def clip(session, n, length=400):
    return {'do': 'clip', 'session': session, 'n': n, 'len': length}


FOCUS, OPEN, AWAIT, SAY, TICK = 'focus', 'open', 'awaiting', 'say', 'tick'


def step(do, value):
    return {'do': do, ('sid' if do in (FOCUS, OPEN, AWAIT) else 'text' if do == SAY else 's'): value}


SCRIPTS = [
    {'name': 'the open agent is heard at once, another raises its hand and is heard when it is opened',
     'steps': [step(FOCUS, 'rachel'), clip('rachel', 1), clip('mike', 2), clip('mike', 3), clip('rachel', 4),
               step(FOCUS, 'mike'), clip('mike', 5), clip('rachel', 6)]},
    {'name': 'for a minute after a message only the agent it went to is heard, even before the open one',
     'steps': [step(FOCUS, 'rachel'), step(AWAIT, 'mike'), clip('mike', 1), clip('rachel', 2), step(TICK, 59),
               clip('rachel', 3), step(TICK, 2), clip('rachel', 4), clip('mike', 5), step(FOCUS, 'rachel')]},
    {'name': 'a few words are said at once, and where that is turned off they are not',
     'steps': [step(FOCUS, 'rachel'), clip('mike', 1, 120), clip('mike', 2, 121), clip('bella', 3, 0),
               {'do': 'settings', 'short_reply_bypass_enabled': False}, clip('bella', 4, 5),
               {'do': 'settings', 'short_reply_bypass_enabled': True, 'speak_if_short_chars': 0}, clip('anna-maria', 5, 1),
               clip('anna-maria', 6, 0), {'do': 'settings', 'speak_if_short_chars': 2000}, clip('nameless', 7, 2000),
               clip('nameless', 8, 2001), clip('rachel', 9, None)]},
    {'name': 'a clip whose length nobody knows is no short one',
     'steps': [step(FOCUS, 'rachel'), clip('mike', 1, None), clip('mike', 2, 'long'), clip('bella', 3, '7')]},
    {'name': 'with the herald off everybody is heard at once, but what was held stays held',
     'steps': [step(FOCUS, 'rachel'), clip('mike', 1), {'do': 'settings', 'disabled': True}, clip('mike', 2), clip('bella', 3),
               {'do': 'settings', 'disabled': False}, clip('bella', 4), step(FOCUS, 'mike'), step(FOCUS, 'bella')]},
    {'name': 'a session nobody has, and no session',
     'steps': [step(FOCUS, 'rachel'), clip('stranger', 1), clip('', 2), clip('stranger', 3, 5)]},
    {'name': 'the user says yes and the name, or not now',
     'steps': [step(FOCUS, 'rachel'), clip('mike', 1), clip('mike', 2), step(SAY, 'not now'), step(SAY, 'Mike is slow today'),
               step(SAY, 'yes'), step(SAY, 'Yes, Mike!'), step(SAY, 'yes mike'), clip('mike', 3), clip('bella', 4),
               clip('mike', 5), step(SAY, 'later'), step(SAY, 'bella later'), step(SAY, 'go ahead Bella and Mike'),
               step(SAY, 'Bella?'), step(SAY, 'mike what is up')]},
    {'name': 'two raise their hands and the user answers both',
     'steps': [step(FOCUS, 'rachel'), clip('mike', 1), clip('bella', 2), step(SAY, 'not now'), step(SAY, 'ok Mike, Bella later'),
               step(SAY, 'bella go')]},
    {'name': 'a message to an agent lets what it held be heard',
     'steps': [step(FOCUS, 'rachel'), clip('mike', 1), clip('mike', 2), step(AWAIT, 'mike'), clip('mike', 3), clip('rachel', 4),
               step(AWAIT, ''), clip('rachel', 5)]},
    {'name': 'opening another agent ends the wait for the one a message went to',
     'steps': [step(FOCUS, 'rachel'), step(AWAIT, 'mike'), step(FOCUS, 'bella'), clip('mike', 1), clip('bella', 2),
               step(AWAIT, 'mike'), step(FOCUS, 'mike'), clip('mike', 3), step(FOCUS, ''), clip('mike', 4)]},
    {'name': 'the agent becomes the open one without a word to the herald (a message named it)',
     'steps': [step(FOCUS, 'rachel'), clip('mike', 1), step(OPEN, 'mike'), clip('mike', 2), clip('rachel', 3),
               step(OPEN, 'rachel'), clip('rachel', 4)]},
    {'name': 'the voice of the herald fails: the reply is heard as if there were no herald',
     'steps': [step(FOCUS, 'rachel'), {'do': 'voice', 'works': False}, clip('mike', 1), clip('mike', 2),
               {'do': 'voice', 'works': True}, clip('mike', 3), step(FOCUS, 'mike')]},
    {'name': 'the user opens the agent while its herald is being made',
     'steps': [step(FOCUS, 'rachel'), {**clip('mike', 1), 'during': step(FOCUS, 'mike')}, clip('mike', 2),
               {**clip('bella', 3), 'during': step(AWAIT, 'bella')}, clip('bella', 4)]},
    {'name': 'a second clip arrives while the herald of the first is being made',
     'steps': [step(FOCUS, 'rachel'), {**clip('mike', 1), 'during': clip('mike', 2)}, clip('mike', 3), step(FOCUS, 'mike')]},
    {'name': 'nobody is open',
     'steps': [clip('mike', 1), step(AWAIT, 'mike'), clip('mike', 2), step(TICK, 61), clip('mike', 3), step(SAY, 'yes mike'),
               clip('nameless', 4), step(SAY, 'yes'), step(FOCUS, 'nameless')]},
]

NAMES = ['Rachel', 'Bella']
SAID = [
    'yes rachel', 'Yes, Rachel.', 'rachel', 'Rachel?', 'rachel ?', 'rachel what\'s up', 'rachel what is it', 'rachel go',
    'rachel going home', 'go ahead rachel', 'goahead rachel', 'rachel go for it', 'ok', 'okay rachel', 'ok bella and rachel',
    'not now', 'not now rachel', 'rachel later', 'later', 'bella hold on', 'shh', 'shhhh rachel', 'be quiet bella',
    'rachel in a minute', 'in a sec', 'rachel in a second please', 'tell me rachel', 'rachel tell me about the weather',
    'yes rachel please check the branch now', 'rachel did you finish the build', 'I talked to rachel yesterday',
    'yes rachel and', 'yes rachel one two', 'yes rachel one two three', 'go ahead rachel but bella later',
    'bella later but go ahead rachel', 'rachel bella', 'yes', 'sure', 'hit me rachel', "i'm listening rachel",
    'im listening bella', "let's hear it rachel", 'let us hear you bella', "let's hear rachel", 'rachel shoot',
    'talk to me rachel', 'rachel talk', "what's up rachel", 'what is up', 'RACHEL!!! YES!!!', '  yes ,  rachel  ',
    'yes rachels', "rachel's turn yes", 'yes "rachel"', 'yes rachel?', 'no rachel', 'stop talking rachel', 'rachel wait',
    'wait', 'hush', 'quiet', '', '   ', 'yes rachel\nnot now bella', 'rachel yes bella yes', 'okay okay okay', 'ok ok rachel ok',
    'yup rachel', 'yep bella', 'yeah rachel yeah', 'rachel what is going on', 'rachel whats up', 'rachel what up',
    'yes rächel', 'yes rachel please', 'bella: go on', 'go on then bella dear', 'bella, hold on a moment',
]
ONE = ['Mike']
SAID_TO_ONE = ['not now', 'later mike', 'yes', 'mike', 'Mike?', 'wait a second', 'please be quiet now', 'quiet', 'mike yes',
               'hold on', 'ok mike', 'what is it mike', 'in a bit', 'not now I am driving', 'shh', 'hmm']
ODD = ['Anna Maria', 'C++', 'Zoë', 'o.k', '']
SAID_TO_ODD = ['yes anna maria', 'yes anna', 'anna maria?', 'yes c++', 'c++ go', 'yes zoë', 'zoë?', 'ok o.k', 'yes',
               'not now']


@tool_scratch.case('clarp-herald-vectors-')
def run(root) -> dict:
    """Runs in a process of its own: what the Python Host's herald and its classifier do."""
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    from lib import herald, intent

    def classified(names, said):
        rows = []
        for text in said:
            found = intent.classify_intent(text, list(names))
            rows.append({'text': text, 'grants': found.grants, 'declines': found.declines})
        return rows

    def played(script):
        heard: list = []
        state = {'focus': '', 'now': 1000.0, 'works': True, 'made': 0,
                 'settings': herald.HeraldSettings(), 'during': None}

        class Stream:
            def broadcast(self, event):
                heard.append(dict(event))

        class Voice:
            def synthesize_herald(self, text, voice_id, *, session=None):
                during, state['during'] = state['during'], None
                if during is not None:
                    act(during)
                if not state['works']:
                    raise RuntimeError('the voice is away')
                state['made'] += 1
                return pathlib.Path(f"/data/audio/herald-{state['made']}__{session}.mp3"), text, voice_id

        class Said(Voice):
            """The herald's words and voice are part of what is recorded."""
            def synthesize_herald(self, text, voice_id, *, session=None):
                path, text, voice_id = super().synthesize_herald(text, voice_id, session=session)
                heard.append({'type': 'herald-made', 'text': text, 'voice_id': voice_id, 'session': session})
                return path

        manager = herald.HeraldManager(
            stream=Stream(), tts=Said(),
            agents=lambda: {sid: {'name': name, 'voice_id': f'voice-{sid}'} for sid, name in AGENTS.items()},
            focus_session=lambda: state['focus'], clock=lambda: state['now'], settings=lambda: state['settings'])

        def act(one):
            do = one['do']
            if do == FOCUS:
                state['focus'] = one['sid']
                manager.set_focus(one['sid'])
            elif do == OPEN:
                state['focus'] = one['sid']
            elif do == AWAIT:
                manager.set_awaiting(one['sid'])
            elif do == TICK:
                state['now'] += one['s']
            elif do == 'settings':
                was = state['settings'].as_dict()
                was.update({key: value for key, value in one.items() if key != 'do'})
                state['settings'] = herald.HeraldSettings(**was)
            elif do == 'voice':
                state['works'] = one['works']
            elif do == SAY:
                found = manager.on_user_text(one['text'])
                return {'granted': found.granted, 'declined': found.declined}
            elif do == 'clip':
                session, n = one['session'], one['n']
                meta = {'clip_id': n, 'trace_id': f't{n}', 'persona': AGENTS.get(session, ''), 'agent_id': f'id-{session}',
                        'streamable': True, 'stream_url': f'/clips/{n}/stream', 'complete_url': f'/clips/{n}/complete.mp3'}
                if one['len'] is not None:
                    meta['text_len'] = one['len']
                state['during'] = one.get('during')
                found = manager.ingest_clip(session, url=f'/audio/{1000 + n}__{session}.mp3', ts=1000 + n, meta=meta)
                state['during'] = None
                return {'broadcast': found.broadcast, 'herald_emitted': found.herald_emitted}
            return None

        rows = []
        for one in script['steps']:
            del heard[:]
            result = act(one)
            held = {sid: manager.held_clips(sid) for sid in list(AGENTS) + ['stranger', '']}
            told = [dict(event) for event in heard]
            # Where a sentence lets more than one agent speak, the Python Host lets them speak
            # in the order of a set of their sessions, which is another from one start to the
            # next (reference defect R52). Written here in the order of the sessions' names,
            # so that the same steps give the same file: the order is no part of what is held.
            if isinstance(result, dict) and len(result.get('granted') or []) > 1:
                result = {**result, 'granted': sorted(result['granted'])}
                told.sort(key=lambda event: str(event.get('session') or ''))
            rows.append({'step': one, 'result': result, 'heard': told,
                         'pending': sorted(manager.pending_heralds()),
                         'held': {sid: urls for sid, urls in held.items() if urls}})
        return {'name': script['name'], 'steps': rows}

    answered = {
        'agents': AGENTS, 'words': herald.herald_text('Rachel'),
        'defaults': herald.HeraldSettings().as_dict(), 'awaiting_s': herald.AWAITING_TTL_SEC,
        'most_short': herald.MAX_SPEAK_IF_SHORT_CHARS, 'residual': intent.MAX_RESIDUAL_WORDS,
        'said': [{'names': NAMES, 'rows': classified(NAMES, SAID)}, {'names': ONE, 'rows': classified(ONE, SAID_TO_ONE)},
                 {'names': ODD, 'rows': classified(ODD, SAID_TO_ODD)}, {'names': [], 'rows': classified([], ['yes rachel'])}],
        'scripts': [played(script) for script in SCRIPTS],
    }
    shutil.rmtree(root, ignore_errors=True)
    return answered


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == '--run':
        print(json.dumps(run()))
        return 0
    ran = subprocess.run([sys.executable, '-I', __file__, '--run'], capture_output=True, text=True,
                         env=tool_scratch.child_env({'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}), check=False)
    if ran.returncode != 0:
        print(ran.stderr[-3000:], file=sys.stderr)
        return 1
    said = json.loads(ran.stdout.splitlines()[-1])
    out = ['{']
    for key in ('agents', 'words', 'defaults', 'awaiting_s', 'most_short', 'residual'):
        out.append(f' "{key}": ' + json.dumps(said[key], ensure_ascii=True) + ',')
    out.append(' "said": [')
    for at, room in enumerate(said['said']):
        out.append('  {"names": ' + json.dumps(room['names'], ensure_ascii=True) + ', "rows": [')
        rows = room['rows']
        out.extend('   ' + json.dumps(row, ensure_ascii=True) + (',' if index < len(rows) - 1 else '')
                   for index, row in enumerate(rows))
        out.append('  ]}' + (',' if at < len(said['said']) - 1 else ''))
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
    utterances = sum(len(room['rows']) for room in said['said'])
    signals = sum(1 for room in said['said'] for row in room['rows'] if row['grants'] or row['declines'])
    steps = sum(len(script['steps']) for script in said['scripts'])
    print(f'{OUT.relative_to(REPO)}: {utterances} utterances ({signals} with a yes or a no in them), '
          f'{len(said["scripts"])} scripts of {steps} steps')
    return 0


if __name__ == '__main__':
    sys.exit(main())
