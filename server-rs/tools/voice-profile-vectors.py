#!/usr/bin/env python3
"""Writes the vectors the Rust Host's voice profile is tested against (the Python Host's PR 485).

An agent's row says what it sounds like and how it is heard, as the Host resolves them: the provider and voice
its next clip is spoken with and where that voice came from (its own, or a provider's one default), or why
nothing is heard; the engines a recording for it is tried with and the languages the first one is handed. The
Python Host makes that with `server/lib/voice_profile.py`, from the same resolver its speaker calls. This runs
that module's own functions (`speech`, `hearing`, `defaults`, `Projection`, `elevenlabs_id_usable`,
`not_spoken_words`) on made-up settings and agents and writes what they answered to
`crates/clarp-kernel/tests/fixtures/voice-profile-vectors.json`.

    server-rs/tools/reference run server-rs/tools/voice-profile-vectors.py

The view of the Host (`HostView`) is made field by field, never built: which keys there are, the names of
voices (a stand-in catalogue of this program's own), the transcription settings. Nothing reads a store, a key or
a cache, and nothing reaches a provider. It runs in a process of its own whose every home, data, config and
cache directory is a new empty directory (the replay harness's own isolation), with no way out of the machine.
The same file at every run. Nothing is logged.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import scratch as tool_scratch

REPO = pathlib.Path(__file__).resolve().parents[2]
OUT = REPO / 'server-rs' / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'voice-profile-vectors.json'

CARTESIA_UUID = 'a0000000-0000-4000-8000-00000000000a'
ADA_CARTESIA = 'c0000000-0000-4000-8000-0000000000ad'
ELEVEN_ID = 'nPczCjzI2devNBz1zQrb'
# The stored shapes of an agent's voice (the release owner's table 1.4), and a few more.
SHAPES = {
    'bare-cartesia-uuid': CARTESIA_UUID,
    'empty': '',
    'bare-eleven-id': ELEVEN_ID,
    'empty-map': '{}',
    'map-cartesia': json.dumps({'cartesia': CARTESIA_UUID}),
    'map-elevenlabs': json.dumps({'elevenlabs': ELEVEN_ID}),
    'map-gemini': json.dumps({'gemini': 'Charon'}),
    'map-google': json.dumps({'google': 'nb-NO-Chirp3-HD-Puck'}),
}
MORE_SHAPES = {
    'map-google-off-the-list': json.dumps({'google': 'xx-XX-Chirp3-HD-Nobody'}),
    'bare-google-voice': 'en-US-Chirp3-HD-Aoede',
    'bare-google-voice-padded': '  en-US-Chirp3-HD-Aoede ',
    'map-deepgram': json.dumps({'deepgram': 'aura-2-thalia-en'}),
    'map-everything': json.dumps({'cartesia': 'ca-own', 'elevenlabs': 'el-own', 'gemini': 'Fenrir',
                                  'google': 'nb-NO-Chirp3-HD-Puck', 'deepgram': 'aura-2-thalia-en'}),
    'eleven-with-space': 'two words',
    'eleven-uuid-shape': '12345678-1234-1234-1234-123456789012',
    'eleven-three-dashes': '12345678-1234-1234-123456789012345',
    'not-json': '{nope',
    'blank': '   ',
}
PROVIDERS = ('cartesia', 'elevenlabs', 'gemini', 'google', 'deepgram')
PERSONAS = ('Ada', 'Zed')          # Ada is in the Host's persona lists, Zed in none


def settings(provider='cartesia', **over) -> dict:
    """The owner's settings as the Python Host holds them once read: made up."""
    values = dict(tts_provider=provider, tts_fallback='none', cartesia_voices={'Ada': ADA_CARTESIA},
                  gemini_voices={'Ada': 'Puck'}, gemini_voice='Kore', deepgram_model='flux-haley-en',
                  tts_agent_overrides={}, whisper_provider='faster-whisper', whisper_model='small.en',
                  whisper_enabled=True)
    values.update(over)
    return values


ALL_KEYS = {'cartesia': True, 'elevenlabs': True, 'gemini': True, 'deepgram': True, 'google': True}


def view(made: dict) -> dict:
    """What a case says of the Host: its settings, its keys and the rest of its view."""
    return {'settings': made['settings'], 'keys': made.get('keys', ALL_KEYS),
            'google_refusal': made.get('google_refusal'), 'engine': made.get('engine', 'local'),
            'language': made.get('language', 'en'), 'agent_engines': made.get('agent_engines', {}),
            'agent_languages': made.get('agent_languages', {})}


@tool_scratch.case('clarp-voice-profile-vectors-')
def run(root) -> dict:
    """Runs in a process of its own: what the Python Host's module does."""
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    from lib import config, google_tts, stt_language, stt_providers, tts_providers, voice_profile
    from lib.voice_catalog import gemini_voices
    from lib.tts_worker import _GOOGLE_WORDS

    # The names of voices a Host has in memory: a stand-in catalogue, with a name that is empty
    # and one that is only space (a label is a real name or nothing).
    labels = {
        'cartesia': {ADA_CARTESIA: 'Ada of the list', 'ca-own': '  ', CARTESIA_UUID: 'Uuid voice'},
        'elevenlabs': {ELEVEN_ID: 'Brian', 'el-own': ''},
        'google': {voice: voice.rsplit('-', 1)[1] for voice in sorted(google_tts.VOICES)},
        'deepgram': {'flux-haley-en': 'Haley', 'aura-2-thalia-en': 'Thalia'},
    }

    def host_view(made: dict):
        seen = view(made)
        cfg = config.Config(**seen['settings'])
        # Labels for the stand-in voices come from this run's tables. The Gemini catalogue
        # is the reference's own picker, with this case's configuration.
        gemini = {vid: label for vid, label, _ in gemini_voices(cfg)}
        for persona, vertex in cfg.gemini_vertex_voices.items():
            if vertex and vertex not in gemini:
                gemini[vertex] = gemini.get(cfg.gemini_voices.get(persona, ''), f'{persona} (designed)')
        return voice_profile.HostView(
            cfg=cfg, keys=dict(seen['keys']), google_refusal=seen['google_refusal'], adapters={},
            labels={**labels, 'gemini': gemini}, engine=seen['engine'],
            language=stt_language.parse_language(seen['language']),
            agent_engines={str(k).strip().lower(): v for k, v in seen['agent_engines'].items()},
            agent_languages={str(k).strip().lower(): v for k, v in seen['agent_languages'].items()})

    def agent_of(persona, stored, *, name=None, session=None, agent_id='a1'):
        agent = {'agent_id': agent_id, 'persona': persona, 'session': session or persona.lower(),
                 'voice_id': stored}
        if name is not None:
            agent['name'] = name
        return agent

    # 1. What one agent sounds like: the table, every shape under every provider and fallback.
    speech = []

    def spoken(name, made, agent):
        shown = voice_profile.speech(host_view(made), agent)
        # What the speaker is handed for the providers it may try, before it asks whether
        # they can say it: the voice of the one resolver, with where it came from.
        handed = {}
        for provider in PROVIDERS:
            route = voice_profile.tts_mode.route(host_view(made).cfg, dict(agent))
            choice = voice_profile.provider_voice(host_view(made).cfg, agent.get('voice_id'), agent, route,
                                                  provider)
            handed[provider] = {'voice': choice.voice, 'origin': choice.origin}
        speech.append({'name': name, 'view': view(made), 'agent': agent, 'speech': shown, 'handed': handed})

    for provider in PROVIDERS:
        for fallback in ('none', 'elevenlabs', 'cartesia'):
            for shape, stored in {**SHAPES, **MORE_SHAPES}.items():
                for persona in PERSONAS:
                    spoken(f'table {provider} / {fallback} / {shape} / {persona}',
                           {'settings': settings(provider, tts_fallback=fallback)}, agent_of(persona, stored))
    # The owner's entry for one agent, speech off, keys missing, Google refused, an engine of nobody's.
    zed = agent_of('Zed', '{}')
    entry = {'zed': {'provider': 'gemini', 'gemini_voice': 'Leda'}}
    spoken('an entry moves the agent to gemini with its voice', {'settings': settings(tts_agent_overrides=entry)},
           zed)
    spoken('an entry moves the agent to gemini without a voice: the default, on purpose',
           {'settings': settings(tts_agent_overrides={'zed': {'provider': 'gemini'}})}, zed)
    spoken('an entry moves the agent to gemini without a voice, and the default is empty',
           {'settings': settings(tts_agent_overrides={'zed': {'provider': 'gemini'}}, gemini_voice='')}, zed)
    spoken('gemini for all without a voice of its own: silent', {'settings': settings('gemini')}, zed)
    spoken('an entry for another provider than the one it speaks with is not heard',
           {'settings': settings('cartesia', tts_agent_overrides={'zed': {'gemini_voice': 'Leda'}})}, zed)
    spoken('an entry by the session', {'settings': settings(
        'cartesia', tts_agent_overrides={'zed-1': {'cartesia_voice': 'ca-session'}})},
        agent_of('Zed', '{}', session='zed-1'))
    spoken('an entry by the name', {'settings': settings(
        'cartesia', tts_agent_overrides={'theo': {'cartesia_voice': 'ca-name'}})},
        agent_of('Zed', '{}', name='Theo', session='zed-2'))
    for over in ({'zed': {'provider': 'cartesia', 'cartesia_voice': 'ca-x'}}, {}):
        spoken(f'speech off for the Host, entry {bool(over)}',
               {'settings': settings('none', tts_agent_overrides=over, tts_fallback='cartesia')}, zed)
    spoken('an entry turns this agent off', {'settings': settings(
        'cartesia', tts_agent_overrides={'zed': {'provider': 'none'}}, tts_fallback='elevenlabs')}, zed)
    for provider in PROVIDERS:
        for fallback in ('none', 'cartesia', 'deepgram'):
            keys = dict(ALL_KEYS, **{provider: False})
            spoken(f'no key for {provider}, fallback {fallback}',
                   {'settings': settings(provider, tts_fallback=fallback), 'keys': keys},
                   agent_of('Ada', json.dumps({'elevenlabs': ELEVEN_ID, 'gemini': 'Charon'})))
    spoken('no key for anyone', {'settings': settings('cartesia', tts_fallback='deepgram'),
                                 'keys': {k: False for k in ALL_KEYS}}, agent_of('Ada', ''))
    for name, stored in (
        ('a bare Google voice on the allowlist', 'nb-NO-Chirp3-HD-Aoede'),
        ('a padded Google voice on the allowlist', '  nb-NO-Chirp3-HD-Aoede '),
    ):
        spoken(name, {'settings': settings('google')}, agent_of('Zed', stored))
    for refusal in ('google_id_collision', 'google_id_unverifiable'):
        spoken(f'google refused: {refusal}', {'settings': settings('google'), 'google_refusal': refusal},
               agent_of('Ada', '{}'))
    spoken('an engine nobody has', {'settings': settings('nobodys-engine')}, agent_of('Ada', '{}'))
    spoken('an engine nobody has, falling back', {'settings': settings('nobodys-engine', tts_fallback='cartesia')},
           agent_of('Ada', '{}'))
    spoken('deepgram with no model of the owner', {'settings': settings('deepgram', deepgram_model='')},
           agent_of('Zed', '{}'))
    spoken('deepgram with a model of the owner', {'settings': settings('deepgram', deepgram_model='aura-2-thalia-en')},
           agent_of('Zed', '{}'))
    spoken('gemini by the owner of its cartesia voice', {'settings': settings(
        'gemini', cartesia_voices={'Ada': ADA_CARTESIA, 'Bea': 'ca-bea'}, gemini_voices={'Bea': 'Aoede'})},
        agent_of('Zed', json.dumps({'cartesia': 'ca-bea'})))
    designed = {'settings': settings(
        'gemini', gemini_voices={'Ada': 'Puck', 'Other': 'voice_api_max'},
        gemini_vertex_voices={'Other': 'voice_vertex_max'})}
    spoken('gemini through the api names a voice of its own', designed,
           agent_of('Zed', json.dumps({'gemini': 'voice_api_max'})))
    spoken('gemini through Vertex keeps the designed label', designed,
           agent_of('Zed', json.dumps({'gemini': 'voice_vertex_max'})))
    spoken('an unregistered Gemini design is nameless', {'settings': settings('gemini')},
           agent_of('Zed', json.dumps({'gemini': 'voice_unregistered'})))

    # 2. Whether ElevenLabs can take a text as a voice id.
    texts = [None, '', ' ', ELEVEN_ID, ' ' + ELEVEN_ID + ' ', '{}', '{"a": 1}', ' {"a": 1}', 'two words',
             'tab\there', CARTESIA_UUID, '12345678-1234-1234-1234-123456789012', '1234567-1234-1234-1234-123456789012',
             '12345678-1234-1234-123456789012345', '123456781234123412341234567890123456', 'x' * 36,
             '12345678-1234-1234-1234-1234567890123', 'a-b-c-d-e', 'new\nline']
    usable = [{'text': text, 'usable': voice_profile.elevenlabs_id_usable(text)} for text in texts]

    # 3. How one agent is heard.
    hearing = []
    engines = ('local', 'google:chirp_3', 'elevenlabs:scribe_v2', 'deepgram:nova-3', 'faster-whisper:base',
               'cartesia:ink-2', '')
    own_engines = (None, 'google:chirp_3', 'elevenlabs:scribe_v2,google:chirp_3', 'faster-whisper:base.en',
                   ' deepgram:nova-3 , elevenlabs:scribe_v2 ', 'faster-whisper:large-v3,google:chirp_3')
    for engine in engines:
        for own in own_engines:
            for language in ('en', 'no,en', 'no', 'not a code'):
                for own_language in (None, 'no,en', 'pt'):
                    for whisper in (('small.en', True), ('large-v3', True), ('small.en', False)):
                        made = {'settings': settings(whisper_model=whisper[0], whisper_enabled=whisper[1]),
                                'engine': engine, 'language': language,
                                'agent_engines': {'Ada': own} if own else {},
                                'agent_languages': {'ADA': own_language} if own_language else {}}
                        agent = agent_of('Ada', '{}')
                        hearing.append({'view': view(made), 'agent': agent,
                                        'hearing': voice_profile.hearing(host_view(made), agent)})
    # An agent found by its name, by its persona and by its session; and by none.
    for name, persona, session in (('Theo', 'Zed', 'zed-1'), ('', 'Theo', 'zed-1'), ('', 'Zed', 'theo'),
                                   ('', 'Zed', 'zed-1'), (None, 'Zed', 'zed-1')):
        made = {'settings': settings(), 'engine': 'local', 'language': 'en',
                'agent_engines': {'theo': 'google:chirp_3'}, 'agent_languages': {'Theo': 'no,en'}}
        agent = agent_of(persona, '{}', name=name, session=session)
        hearing.append({'view': view(made), 'agent': agent, 'hearing': voice_profile.hearing(host_view(made), agent)})
    engine_labels = {}
    for item in hearing:
        for row in item['hearing']['engines']:
            engine_labels[row['id']] = row['label']

    # 4. What a new agent gets.
    defaults = []
    for provider in (*PROVIDERS, 'none'):
        for engine in ('local', 'google:chirp_3'):
            made = {'settings': settings(provider), 'engine': engine, 'language': 'no,en'}
            defaults.append({'view': view(made), 'defaults': voice_profile.defaults(host_view(made))})

    # 5. Who else speaks with the same voice: one pass over the agents of a snapshot.
    def row(agent_id, persona, stored, **more):
        return {'agent_id': agent_id, 'persona': persona, 'session': persona.lower() + '-' + agent_id,
                'voice_id': stored, **more}
    rosters = []
    for provider, agents in (
        ('cartesia', [row('a1', 'Ada', '{}'), row('a2', 'Ada', '{}'), row('a3', 'Ada', '{}'),
                      row('a4', 'Zed', '{}'), row('a5', 'Zed', json.dumps({'cartesia': ADA_CARTESIA})),
                      row('a6', 'Ada', '{}', archived_at=5), row('a7', 'Ada', '{}', is_janitor=1),
                      row('a8', 'Zed', json.dumps({'cartesia': 'ca-alone'}))]),
        # The release owner's rows: two agents on Google's one default voice share it.
        ('google', [row('g1', 'Zed', '{}'), row('g2', 'Ada', ''), row('g3', 'Zed', json.dumps({'google': 'nb-NO-Chirp3-HD-Puck'})),
                    row('g4', 'Zed', json.dumps({'google': 'xx-XX-Chirp3-HD-Nobody'})),
                    row('g5', 'Zed', '{}', archived_at=9)]),
        ('deepgram', [row('d1', 'Zed', '{}'), row('d2', 'Ada', '{}'), row('d3', 'Zed', json.dumps({'deepgram': 'aura-2-thalia-en'})),
                      row('d4', 'Zed', '{}', is_janitor=1)]),
        ('elevenlabs', [row('e1', 'Zed', ELEVEN_ID), row('e2', 'Zed', ELEVEN_ID), row('e3', 'Zed', '{}'),
                        row('e4', 'Zed', '{}')]),
        ('none', [row('n1', 'Zed', '{}'), row('n2', 'Zed', '{}')]),
    ):
        made = {'settings': settings(provider)}
        projection = voice_profile.Projection(host_view(made), agents)
        rosters.append({'view': view(made), 'agents': agents,
                        'profiles': [projection.for_row(agent) for agent in agents]})

    # 6. The words of a clip that was not spoken.
    reasons = ('no_voice', 'not_configured', 'provider_off', 'provider_failed', 'host_failed', 'unheard_of')
    words = [{'reason': reason, 'provider_label': label, 'name': name, 'partly': partly,
              'words': voice_profile.not_spoken_words(reason, provider_label=label, name=name, partly=partly)}
             for reason in reasons for label in ('', 'Cartesia Sonic') for name in ('', 'Ada')
             for partly in (False, True)]

    # The release owner's three rows, named.
    asked = {
        "deepgram's origin is default": next(item for item in speech
                                             if item['name'] == 'table deepgram / none / empty-map / Zed')['speech'],
        "two agents on google's default share it": [rosters[1]['profiles'][0]['speech'],
                                                   rosters[1]['profiles'][1]['speech']],
        'a google voice off the allowlist is no voice': next(
            item for item in speech if item['name'] == 'table google / none / map-google-off-the-list / Zed')['speech'],
    }
    return {
        'what': 'server/lib/voice_profile.py: speech, hearing, defaults, Projection, elevenlabs_id_usable, '
                'not_spoken_words',
        'labels': labels,
        'providers': [{'id': item['id'], 'name': item['name'], 'streaming': bool(item.get('streaming'))}
                      for item in tts_providers.CATALOG],
        'google_voices': sorted(google_tts.VOICES), 'google_default': google_tts.DEFAULT_VOICE,
        'language_set_providers': sorted(stt_providers.LANGUAGE_SET_PROVIDERS),
        'engine_labels': engine_labels,
        'google_reasons': voice_profile.GOOGLE_REASONS, 'google_words': _GOOGLE_WORDS,
        'speech': speech, 'usable': usable, 'hearing': hearing, 'defaults': defaults, 'rosters': rosters,
        'words': words, 'asked': asked,
    }


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
    # One case a line: the file is read by a test, and a line is a case to a reader of a change.
    lines = ['{']
    names = sorted(said)
    for n, name in enumerate(names):
        end = ',' if n < len(names) - 1 else ''
        value = said[name]
        if isinstance(value, list):
            lines.append(f' {json.dumps(name)}: [')
            lines.extend('  ' + json.dumps(one, sort_keys=True, ensure_ascii=True) + (',' if k < len(value) - 1 else '')
                         for k, one in enumerate(value))
            lines.append(' ]' + end)
        else:
            lines.append(f' {json.dumps(name)}: {json.dumps(value, sort_keys=True, ensure_ascii=True)}{end}')
    lines.append('}')
    OUT.write_text('\n'.join(lines) + '\n', encoding='ascii')
    counts = {name: len(said[name]) for name in ('speech', 'usable', 'hearing', 'defaults', 'rosters', 'words')}
    print(f'{OUT.relative_to(REPO)}: {counts}, {OUT.stat().st_size} bytes')
    return 0


if __name__ == '__main__':
    sys.exit(main())
