#!/usr/bin/env python3
"""Writes the vectors the Rust Host's rules of speech out are tested against.

An agent says something aloud: the Host decides which engine speaks it and in which voice, and
what the clip is called. The Python Host does that with a handful of rules that are no engine's:
what an agent's stored voice means for each provider (`server/lib/voice.py`), which provider an
agent speaks with and which one is tried after it (`tts_mode.py`, and the owner's settings as
`config.py` reads them: `tts_override_for`), which of Gemini's and Cartesia's voices a persona
has (`gemini_voice_for`, `gemini_voice_for_cartesia`, `cartesia_voice_for` in `config.py`,
`_gemini_voice` in `tts_registry.py`), and what a clip's file is called (`tts_engine.py`). This
calls those functions for many made-up agents and settings and writes what they answered to
`crates/clarp-kernel/tests/fixtures/voice-vectors.json`.

    python3 server-rs/tools/voice-vectors.py

Nothing of a Host is touched or started. A file that leans on nothing of the server is run as
the module it is; from a file that does, each function is taken out by its name and called by
itself. The settings are made up here: no file of the owner's is read, and no voice is real.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import pathlib
import re
import sys
import types

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
LIB = REPO / 'server' / 'lib'
OUT = HERE.parent / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'voice-vectors.json'


def whole(path: pathlib.Path) -> dict:
    """A file that leans on nothing of the server, run as the module it is."""
    name = f'taken_{path.stem}'
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return vars(module)


def taken(path: pathlib.Path, names: list[str], given: dict | None = None, inside: str | None = None) -> dict:
    """The named functions and values of a file, by themselves. With `inside`, they are the
    methods of that class, taken out as plain functions that are handed what they call self."""
    body = ast.parse(path.read_text()).body
    if inside:
        body = next(node.body for node in body if isinstance(node, ast.ClassDef) and node.name == inside)
    wanted = []
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names:
            node.decorator_list = []
            wanted.append(node)
        elif isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in names for t in node.targets):
            wanted.append(node)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id in names:
            # What a value is said to be is not needed to have the value.
            wanted.append(ast.Assign(targets=[node.target], value=node.value))
    found = {node.name if hasattr(node, 'name') else node.targets[0].id for node in wanted}
    missing = set(names) - found
    assert not missing, f'{path.name} has no {sorted(missing)}'
    room = {'re': re, '__name__': 'taken', **(given or {})}
    exec(compile(ast.fix_missing_locations(ast.Module(body=wanted, type_ignores=[])), str(path), 'exec'), room)  # noqa: S102
    return room


def settings(methods: dict, *, provider='cartesia', fallback='none', overrides=None, gemini_voices=None,
             cartesia_voices=None, gemini_voice='', backend='gemini_api', vertex_voices=None):
    """The owner's settings as the Python Host holds them once read: made up, with the Host's
    own methods on them."""
    cfg = types.SimpleNamespace(
        tts_provider=provider, tts_fallback=fallback, gemini_voice=gemini_voice,
        gemini_backend=backend, gemini_vertex_voices=dict(vertex_voices or {}),
        tts_agent_overrides={str(name).strip().lower(): {str(k).strip().lower(): str(v).strip()
                                                       for k, v in entry.items() if str(v).strip()}
                             for name, entry in (overrides or {}).items()},
        gemini_voices=dict(gemini_voices or {}), cartesia_voices=dict(cartesia_voices or {}))
    for name in ('tts_override_for', 'gemini_voice_for', 'gemini_voice_for_cartesia', 'cartesia_voice_for',
                 'gemini_tts_voice'):
        setattr(cfg, name, types.MethodType(methods[name], cfg))
    return cfg


def stored_voices(voice: dict) -> dict:
    raws = [None, '', '   ', 'el-voice-1', '  el-voice-1  ', '{"elevenlabs": "el-1", "cartesia": "ca-1"}',
            '{"gemini": "Kore"}', '{"cartesia": ""}', '{"cartesia": null, "gemini": "Charon"}', '{not json',
            '{}', '[]', '["a"]', '{"Cartesia": "ca-upper"}', '{"cartesia": 7}', ' {"gemini":"Puck"} ',
            '{"google": "en-US-Chirp3-HD-Aoede", "elevenlabs": "el-2"}', '"quoted"', 'null']
    providers = ['elevenlabs', 'cartesia', 'gemini', 'google', 'deepgram', '']
    merges = [('cartesia', 'ca-new'), ('gemini', ' Kore '), ('elevenlabs', ''), (' Cartesia ', 'ca-case'),
              ('gemini', '   '), ('deepgram', 'aura-1')]
    return {
        'maps': [{'raw': raw, 'map': voice['voice_map'](raw)} for raw in raws],
        'resolved': [{'raw': raw, 'provider': provider, 'voice': voice['resolve_voice'](raw, provider)}
                     for raw in raws for provider in providers],
        'merged': [{'raw': raw, 'provider': provider, 'voice': given,
                    'merged': voice['merge_voice'](raw, provider, given)}
                   for raw in raws for provider, given in merges],
    }


AGENTS = [
    None, {}, {'persona': 'Maximus', 'session': 'maximus-6c37', 'name': 'Maximus'},
    {'persona': 'Ingrid', 'session': 'ingrid'}, {'persona': 'INGRID', 'session': 'other'},
    {'persona': 'Nobody', 'session': 'maximus-6c37'}, {'persona': ' Ingrid ', 'session': ''},
    {'name': 'Arnold', 'persona': 'Somebody Else', 'session': 'ingrid'}, {'persona': 'Claude-5628', 'session': 'claude-5628'},
]
OVERRIDES = {
    'Ingrid': {'provider': 'gemini', 'gemini_voice': 'Charon', 'cartesia_voice': ''},
    ' maximus-6c37 ': {'Provider': ' ElevenLabs ', 'elevenlabs_voice': 'el-max'},
    'Arnold': {'cartesia_voice': 'ca-arnold'},
}


def routes(mode: dict, methods: dict) -> list:
    out = []
    for provider in ('cartesia', 'gemini', 'elevenlabs', 'none'):
        for fallback in ('none', '', 'cartesia', 'gemini', 'elevenlabs'):
            for overrides in ({}, OVERRIDES):
                cfg = settings(methods, provider=provider, fallback=fallback, overrides=overrides)
                for agent in AGENTS:
                    route = mode['route'](cfg, agent)
                    out.append({'provider': provider, 'fallback': fallback, 'overrides': overrides, 'agent': agent,
                                'override': cfg.tts_override_for(agent),
                                'route': {'provider': route.provider, 'voice': route.voice,
                                          'fallback': route.fallback}})
    return out


def persona_voices(methods: dict, registry: dict) -> dict:
    gemini = {'Maximus': 'Charon', 'ingrid': 'Kore', 'Claude': 'Puck', 'Ünï': 'Aoede'}
    cartesia = {'Maximus': 'ca-max', 'Ingrid': 'ca-ingrid', 'Claude': 'ca-claude', 'Lonely': 'ca-lonely'}
    personas = [None, '', 'Maximus', 'maximus', 'MAXIMUS', 'Ingrid', 'Claude', 'Claude-5628', 'claude-ab12',
                'Claude-56289', 'Claude-XYZW', 'Nobody', 'Nobody-0000', 'ÜNÏ', 'Lonely', 'Maximus-12ab']
    cfg = settings(methods, gemini_voices=gemini, cartesia_voices=cartesia)
    out = {
        'gemini_voices': gemini, 'cartesia_voices': cartesia,
        'gemini_for': [{'persona': persona, 'voice': cfg.gemini_voice_for(persona)} for persona in personas],
        'cartesia_for': [{'persona': persona, 'voice': cfg.cartesia_voice_for(persona)} for persona in personas],
        'gemini_for_cartesia': [{'cartesia_voice': voice, 'voice': cfg.gemini_voice_for_cartesia(voice)}
                                for voice in (None, '', 'ca-max', 'ca-ingrid', 'ca-claude', 'ca-lonely', 'ca-none')],
    }
    # Which of Gemini's voices one clip is spoken in: what the agent's entry says, what the
    # agent has stored, what its persona has, what the owner of the Cartesia voice it speaks
    # with has, and the default only for an agent moved to Gemini on purpose.
    chosen = []
    for default in ('', 'Zephyr'):
        for overrides in ({}, {'Mover': {'provider': 'gemini'}}):
            cfg = settings(methods, gemini_voices=gemini, cartesia_voices=cartesia, overrides=overrides,
                           gemini_voice=default)
            for raw in (None, 'el-1', '{"gemini": "Fenrir"}', '{"cartesia": "ca-x"}'):
                for persona in ('Maximus', 'Nobody', 'Mover', 'Claude-5628'):
                    for route_voice in (None, 'Leda'):
                        for cartesia_voice in (None, 'ca-ingrid', 'ca-lonely'):
                            agent = {'persona': persona, 'session': persona.lower()}
                            voice = registry['_gemini_voice'](cfg, raw, persona, agent, route_voice, cartesia_voice)
                            chosen.append({'default': default, 'overrides': overrides, 'raw': raw, 'persona': persona,
                                           'route_voice': route_voice, 'cartesia_voice': cartesia_voice,
                                           'voice': voice})
    out['gemini_voice'] = chosen
    return out


def clip_voices(methods: dict, registry: dict, mode: dict) -> dict:
    """The voices one clip can be spoken in, by provider, as they are worked out once for the
    clip: the agent's entry for the engine it speaks with, what the agent has stored, what its
    persona has in the owner's lists. And a voice of Gemini's as each of Google's doors knows it."""
    gemini = {'Maximus': 'voice_api_max', 'Ingrid': 'Kore', 'Claude': 'Puck'}
    vertex = {'Maximus': 'voice_vertex_max', 'Ingrid': 'Kore-v', 'Lonely': 'Aoede'}
    cartesia = {'Maximus': 'ca-max', 'Claude': 'ca-claude'}
    overrides = {'Ingrid': {'provider': 'gemini', 'gemini_voice': 'Charon'},
                 'Arnold': {'provider': 'cartesia', 'cartesia_voice': 'ca-arnold'},
                 'Eleven': {'provider': 'elevenlabs', 'elevenlabs_voice': 'el-entry'}}
    doors = []
    for backend in ('gemini_api', 'vertex'):
        cfg = settings(methods, gemini_voices=gemini, vertex_voices=vertex, backend=backend)
        for voice in ('voice_api_max', 'voice_vertex_max', 'Kore', 'Kore-v', 'Puck', 'Aoede', 'Zephyr', ''):
            doors.append({'backend': backend, 'voice': voice, 'as_known': cfg.gemini_tts_voice(voice)})
    chosen = []
    for provider in ('cartesia', 'gemini', 'elevenlabs'):
        cfg = settings(methods, provider=provider, overrides=overrides, gemini_voices=gemini,
                       cartesia_voices=cartesia, vertex_voices=vertex)
        for raw in (None, 'el-plain', '{"cartesia": "ca-own", "gemini": "Fenrir"}', '{"elevenlabs": "el-own"}'):
            for persona in ('Maximus', 'Ingrid', 'Arnold', 'Eleven', 'Nobody', 'Claude-5628'):
                agent = {'persona': persona, 'session': persona.lower()}
                route = mode['route'](cfg, agent)
                made = registry['ProviderRegistry'](cfg, raw, agent, route)
                chosen.append({'provider': provider, 'raw': raw, 'persona': persona,
                               'route': {'provider': route.provider, 'voice': route.voice},
                               'cartesia_voice': made.cartesia_voice, 'eleven_voice': made.eleven_voice,
                               'by_provider': {name: made.voice_for(name)
                                               for name in ('cartesia', 'gemini', 'elevenlabs', 'deepgram')}})
    return {'gemini_voices': gemini, 'vertex_voices': vertex, 'cartesia_voices': cartesia, 'overrides': overrides,
            'doors': doors, 'chosen': chosen}


def clip_names(engine: dict) -> dict:
    sessions = [None, '', 'maximus-6c37', 'has space', 'ünï', 'a/b', '../etc', 'UPPER.case_1-2', '***', 'a' * 80, ' pad ']
    made = [{'session': session, 'now_ms': now,
             'name': engine['make_clip_filename'](session, now_ms=now)}
            for session in sessions for now in (1794800000000, 0, 7)]
    names = ['1794800000000.mp3', '1794800000000__maximus-6c37.mp3', '1794800000000__a.b_c-d.mp3',
             '179480000.mp3', '1794800000000__.mp3', '1794800000000__has space.mp3', '1794800000000.wav',
             'x1794800000000.mp3', '1794800000000__a__b.mp3', '1794800000000__ünï.mp3', '', '1794800000000.mp3.json',
             '17948000000001234__s.mp3', '1794800000000__anon.mp3', '1794800000000__a\n.mp3']
    parsed = []
    for name in names:
        ts, session = engine['parse_clip_filename'](name)
        parsed.append({'name': name, 'ts': ts, 'session': session})
    return {'made': made, 'parsed': parsed}


def main() -> int:
    voice = whole(LIB / 'voice.py')
    mode = whole(LIB / 'tts_mode.py')
    methods = taken(LIB / 'config.py',
                    ['tts_override_for', 'gemini_voice_for', 'gemini_voice_for_cartesia', 'cartesia_voice_for',
                     'gemini_tts_voice'],
                    taken(LIB / 'config.py', ['_ANONYMOUS_SUFFIX']), inside='Config')
    registry = taken(LIB / 'tts_registry.py', ['_gemini_voice', 'ProviderRegistry'],
                     {'resolve_voice': voice['resolve_voice'], 'GEMINI': voice['GEMINI'],
                      'CARTESIA': voice['CARTESIA'], 'ELEVENLABS': voice['ELEVENLABS']})
    built_in = taken(LIB / 'config.py', ['DEFAULT_CARTESIA_VOICES'])['DEFAULT_CARTESIA_VOICES']
    engine = taken(LIB / 'tts_engine.py', ['_FN_RE', 'make_clip_filename', 'parse_clip_filename'],
                   {'_now_ms': lambda: 0})
    vectors = {
        'written_by': f'server-rs/tools/voice-vectors.py with Python {sys.version_info.major}.{sys.version_info.minor}',
        'stored_voices': stored_voices(voice),
        'routes': routes(mode, methods),
        'persona_voices': persona_voices(methods, registry),
        'clip_voices': clip_voices(methods, registry, mode),
        # The voices of Cartesia's the Python Host has for the personas it ships with: an owner's
        # own list adds to them and never takes their place.
        'built_in_cartesia_voices': [[persona, voice] for persona, voice in built_in.items()],
        'clip_names': clip_names(engine),
    }
    OUT.write_text(json.dumps(vectors, indent=1, ensure_ascii=False, sort_keys=True) + '\n')
    counts = {'maps': len(vectors['stored_voices']['maps']), 'resolved': len(vectors['stored_voices']['resolved']),
              'merged': len(vectors['stored_voices']['merged']), 'routes': len(vectors['routes']),
              'gemini_voice': len(vectors['persona_voices']['gemini_voice']),
              'clip_names': len(vectors['clip_names']['made']) + len(vectors['clip_names']['parsed'])}
    print(f'wrote {OUT.relative_to(REPO)}: {counts}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
