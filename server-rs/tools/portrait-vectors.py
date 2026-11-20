#!/usr/bin/env python3
"""Writes the vectors the Rust Host's rules for an agent's picture are tested against.

An agent is shown with a face: its own picture when it chose one, else the picture the Host
ships for its name, else the one for its kind of tool; and, for a few names, a picture drawn
for the family of model it runs on. The Python Host decides which in `server/lib/avatar_urls.py`
and `model_avatars.py`. This calls them over a made-up directory of pictures and writes what
they answered to `crates/clarp-kernel/tests/fixtures/portrait-vectors.json`.

    python3 server-rs/tools/portrait-vectors.py

Nothing of a Host is touched or started. The pictures are a few bytes each, made here.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import shutil
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import scratch as tool_scratch

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = HERE.parent / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'portrait-vectors.json'

# The pictures the made-up Host ships: under avatars/ and avatars/models/.
SHIPPED = ['claire.png', 'rachel.png', 'mike-7.png', 'default-claude.png', 'default-codex.png', 'default-gemini.png',
           'default-grok.png', 'models/rachel.opus.png', 'models/rachel.codex.png', 'models/rachel.gemini.png',
           'models/rachel.fable.png', 'models/claire.sonnet.png', 'models/mike-7.deepseek.png']

NAMES = ['Claire', 'claire', 'CLAIRE', 'Rachel', 'Mike 7', 'mike-7', 'Mike_7', 'Nobody', '', '   ', 'Zoë', 'İstanbul',
         'ECIT Rachel', 'rachel!', 'Rachel-2', 'r.a.c.h.e.l', '../claire', 'claire/../../x', 'ČLAIRE', 'ß', 'ＣＬＡＩＲＥ',
         'Koko 🎧', 'clarp-memory-expert', '123', '-', '_']
BACKENDS = ['claude', 'codex', 'agy', 'grok', 'deepseek', 'muse', 'opencode', 'gemini', 'scripted', '', ' Codex ', 'AGY',
            'unknown']
MODELS = ['', 'claude-opus-4-6-thinking', 'claude-fable-5-2', 'claude-sonnet-5-5', 'claude-haiku-5-5', 'gpt-6-codex',
          'gemini-3-pro', 'grok-5', 'deepseek-chat', 'OPUS', '  Sonnet  ', 'gpt-6-astra', 'opus-and-sonnet',
          'sonnet-then-opus', 'codex-gemini', 'fable', 'gemini-grok-codex', 'o4-mini', 'haiku']
IDENTITIES = ['a1b2c3d4e5f60718', 'claire.png', 'mike 7.png', 'ü/ö?&=#%.png', 'A-Z_a-z.0-9~', '', ' ', 'été', '+', "it's", '日本']


@tool_scratch.case('clarp-portrait-vectors-')
def main(root) -> int:
    sys.path.insert(0, str(REPO / 'server'))
    from lib import avatar_urls, backends, model_avatars

    try:
        versions = {}
        for name in SHIPPED:
            path = root / 'avatars' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'a made-up picture: ' + name.encode())
            versions[name] = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
        faces = [{'persona': persona, 'backend': backend,
                  'url': avatar_urls.static_persona_avatar_url(persona, backend, static_root=root)}
                 for persona in NAMES for backend in BACKENDS]
        on_models = [{'persona': persona, 'backend': backend, 'model': model, 'default_model': default,
                      'url': model_avatars.url_for(persona, backend, model, root=root / 'avatars' / 'models',
                                                   default_model=default)}
                     for persona in ('Rachel', 'Claire', 'Mike 7', 'Nobody', '')
                     for backend in ('claude', 'codex', 'agy', 'grok', 'deepseek', 'unknown')
                     for model in MODELS
                     for default in ('', 'gemini-3-pro')]
        vectors = {
            'about': 'Written by server-rs/tools/portrait-vectors.py from server/lib/avatar_urls.py and '
                     'model_avatars.py. Do not edit by hand.',
            'shipped': versions,
            'slugs': [{'name': name, 'slug': avatar_urls.persona_avatar_slug(name), 'model_slug': model_avatars.slug(name)}
                      for name in NAMES],
            'families': [{'model': model, 'family': model_avatars.family_for_model(model)} for model in MODELS],
            'tools': [{'backend': backend, 'family': backends.by_id(backend).model_family}
                      for backend in ('claude', 'codex', 'agy', 'grok', 'deepseek', 'muse', 'opencode', 'unknown', '')],
            'faces': faces,
            'on_models': on_models,
            'addresses': [{'prefix': prefix, 'identity': identity,
                           'url': avatar_urls.versioned_avatar_url(prefix, identity, str(root / 'avatars' / 'claire.png'))}
                          for prefix in ('/avatars', '/static/avatars/', '/static/avatars//') for identity in IDENTITIES],
        }
    finally:
        shutil.rmtree(root, ignore_errors=True)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    # One case on a line: a change to one case is a change to one line.
    out = ['{']
    for at, (name, value) in enumerate(vectors.items()):
        comma = '' if at == len(vectors) - 1 else ','
        if isinstance(value, list):
            out.append(f' {json.dumps(name)}: [')
            out.extend('  ' + json.dumps(case, ensure_ascii=False) + (',' if place < len(value) - 1 else '')
                       for place, case in enumerate(value))
            out.append(' ]' + comma)
        else:
            out.append(f' {json.dumps(name)}: {json.dumps(value, ensure_ascii=False)}{comma}')
    out.append('}')
    OUT.write_text('\n'.join(out) + '\n')
    shown = sum(1 for face in faces if face['url'])
    drawn = sum(1 for face in on_models if face['url'])
    print(f'{OUT.relative_to(REPO)}: {len(faces)} faces ({shown} with a picture), {len(on_models)} on models '
          f'({drawn} with a picture), {len(vectors["addresses"])} addresses; {OUT.stat().st_size} bytes')
    return 0


if __name__ == '__main__':
    sys.exit(main())
