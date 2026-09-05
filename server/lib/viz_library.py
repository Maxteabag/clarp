"""Versioned fleet vocabulary. Atomic JSON, no database schema changes.

A writer holds a process-independent lock only while comparing and replacing
one snapshot. Model calls happen outside this lock. Every replacement names
its expected revision and records the explicit decision it supersedes.
"""
from __future__ import annotations

import copy
import fcntl
import json
import os
import pathlib
import re
import tempfile
from typing import Any

from . import viz_archetypes, xdg

IDENTIFIER = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._+:/@ -]{0,239}$')
EXE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$')
SHAPES = {'circle', 'box', 'diamond', 'hexagon', 'ring'}
_LAST_GOOD: dict[str, dict] = {}


def path() -> pathlib.Path:
    return xdg.data_dir() / 'viz_library.json'


def seed() -> dict:
    from .viz_normalize import EXE_RULES
    entities, rules = {}, {}
    for exe, (verb, kind) in EXE_RULES.items():
        canonical = {'pnpm': 'npm', 'yarn': 'npm', 'npx': 'npm',
                     'python': 'python3'}.get(exe, exe)
        target = f'{kind}:{canonical}' if kind in {'toolchain', 'script'} else kind
        base_kind = kind.split(':')[0]
        entities[target] = {'id': target, 'kind': base_kind,
                            'shape': 'box' if base_kind in {'repo', 'file', 'path'} else 'circle',
                            'icon': 'glyph:' + (canonical[:2].upper() if base_kind in {'toolchain','script'} else {'file':'F','path':'⌕','repo':'⑂','host':'▣'}.get(base_kind, '◈')),
                            'archetype': viz_archetypes.archetype_for(verb)}
        rules[exe] = {'exe': exe, 'verb': verb, 'kind': base_kind,
                      'target': target, 'archetype': viz_archetypes.archetype_for(verb)}
    return {'revision': 0, 'rules': rules, 'entities': entities,
            'archetypes': viz_archetypes.specs(), 'redirects': {}, 'decisions': []}


def load() -> dict:
    p = path()
    try:
        if p.stat().st_size > 4_000_000:
            raise ValueError('library too large')
        data = json.loads(p.read_text())
        if not all(isinstance(data.get(k), dict) for k in
                   ('rules', 'entities', 'archetypes', 'redirects')):
            raise ValueError('invalid library')
        if not isinstance(data.get('revision'), int) or not isinstance(data.get('decisions'), list):
            raise ValueError('invalid revision')
        _LAST_GOOD[str(p)] = copy.deepcopy(data)
        return data
    except FileNotFoundError:
        if str(p) in _LAST_GOOD:
            return copy.deepcopy(_LAST_GOOD[str(p)])
        return seed()
    except (OSError, ValueError, TypeError, AttributeError) as error:
        if str(p) in _LAST_GOOD:
            return copy.deepcopy(_LAST_GOOD[str(p)])
        raise ValueError('fleet library unreadable; preserve the last displayed map') from error


def resolve(target: str, library: dict) -> str:
    seen = set()
    while target in library['redirects']:
        if target in seen:
            raise ValueError('entity redirect cycle')
        seen.add(target)
        target = library['redirects'][target]
    return target


def _identifier(value: Any) -> bool:
    return isinstance(value, str) and bool(IDENTIFIER.fullmatch(value))


def validate_design(design: dict, library: dict) -> None:
    """Mechanical limits on data and drawing primitives, never on new meaning."""
    if not isinstance(design, dict) or len(json.dumps(design, allow_nan=False)) > 32000:
        raise ValueError('invalid or oversized design')
    entity, rule = design.get('entity', {}), design.get('rule', {})
    if not isinstance(entity, dict) or not isinstance(rule, dict):
        raise ValueError('entity and rule must be objects')
    if not _identifier(entity.get('id')) or not _identifier(entity.get('kind')):
        raise ValueError('invalid entity identity')
    if entity['id'].startswith('repo:') and entity['id'].split(':', 1)[1] in {'null', 'None', 'undefined'}:
        raise ValueError('invented repository')
    if entity.get('shape', 'circle') not in SHAPES:
        raise ValueError('unknown drawing primitive; use logic for a custom shape')
    icon = entity.get('icon', 'glyph:?')
    if not isinstance(icon, str) or not icon.startswith('glyph:') or len(icon) > 30:
        raise ValueError('icon must be a short glyph; service marks are supplied by the client')
    if not isinstance(rule.get('exe'), str) or not EXE.fullmatch(rule['exe']):
        raise ValueError('invalid executable')
    if not _identifier(rule.get('verb')):
        raise ValueError('invalid verb')
    if rule.get('sub') and not EXE.fullmatch(rule['sub']):
        raise ValueError('invalid subcommand')
    if rule.get('target', entity['id']) != entity['id']:
        raise ValueError('rule must reference the designed entity')
    archetype = design.get('archetype', entity.get('archetype', 'pulse'))
    if not _identifier(archetype):
        raise ValueError('invalid archetype name')
    spec = design.get('spec', library['archetypes'].get(archetype))
    if not isinstance(spec, dict):
        raise ValueError('new archetype needs a spec')
    for field, low, high in [('travel', .001, .2), ('decay', 0, 1),
                             ('weight', .1, 5), ('trail', 0, 1)]:
        v = spec.get(field)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not low <= v <= high:
            raise ValueError('invalid archetype ' + field)
    if not isinstance(spec.get('persist'), bool):
        raise ValueError('persist must be boolean')
    logic = entity.get('logic')
    if logic is not None and (not isinstance(logic, list) or len(logic) > 64):
        raise ValueError('logic must contain at most 64 drawing commands')
    merges = design.get('merge', [])
    if not isinstance(merges, list) or any(x not in library['entities'] or x == entity['id'] for x in merges):
        raise ValueError('merge sources must be existing distinct entities')


def apply(design: dict, expected_revision: int, supersede: str | None = None) -> dict:
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = load()
        if current['revision'] != expected_revision:
            raise ValueError('library changed; triage against its new revision')
        validate_design(design, current)
        updated = copy.deepcopy(current)
        rule = dict(design['rule'])
        key = rule['exe'] + (':' + rule['sub'] if rule.get('sub') else '')
        prior = next((d for d in reversed(current['decisions']) if key in d['keys']), None)
        if key in current['rules'] and not prior and supersede != 'seed:' + key:
            raise ValueError('seed decision is stable; explicitly supersede seed:' + key)
        if prior and supersede != prior['id']:
            raise ValueError('live decision is stable; explicitly name the decision to supersede')
        seed_key = supersede.removeprefix('seed:') if supersede and supersede.startswith('seed:') else ''
        valid_seed = (seed_key in current['rules'] and seed_key.split(':', 1)[0] == rule['exe']
                      and not current['rules'][seed_key].get('authored'))
        if supersede and not valid_seed and not any(d['id'] == supersede for d in current['decisions']):
            raise ValueError('unknown superseded decision')
        entity = dict(design['entity'])
        eid = entity['id']
        archetype = design.get('archetype', entity.get('archetype', 'pulse'))
        entity.update(shape=entity.get('shape', 'circle'), icon=entity.get('icon', 'glyph:?'),
                      archetype=archetype)
        if eid in current['entities'] and current['entities'][eid] != entity and not supersede:
            raise ValueError('changing a live entity requires explicit supersession')
        spec = design.get('spec', current['archetypes'].get(archetype))
        if archetype in current['archetypes'] and spec != current['archetypes'][archetype] and not supersede:
            raise ValueError('changing a live archetype requires explicit supersession')
        if design.get('merge') and not supersede:
            raise ValueError('merging live entities requires explicit supersession')
        updated['entities'][eid] = entity
        updated['archetypes'][archetype] = spec
        rule.update(target=eid, kind=entity['kind'], archetype=archetype, authored=True)
        if supersede:
            previous = next((d for d in current['decisions'] if d['id'] == supersede), None)
            for old_key in previous['keys'] if previous else ([seed_key] if valid_seed else []):
                if old_key != key and old_key.split(':', 1)[0] == rule['exe']:
                    updated['rules'].pop(old_key, None)
        updated['rules'][key] = rule
        for source in design.get('merge', []):
            updated['redirects'][source] = eid
        for source in updated['redirects']:
            resolve(source, updated)
        updated['revision'] += 1
        updated['decisions'].append({'id': f"decision:{updated['revision']}", 'keys': [key],
                                     'supersedes': supersede, 'notes': str(design.get('notes', ''))[:1000],
                                     'design': design})
        blob = json.dumps(updated, indent=2, allow_nan=False)
        if len(blob.encode()) > 4_000_000:
            raise ValueError('library capacity reached')
        fd, tmp = tempfile.mkstemp(dir=p.parent, prefix='.viz-library-')
        try:
            with os.fdopen(fd, 'w') as out:
                out.write(blob)
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp, p)
            _LAST_GOOD[str(p)] = copy.deepcopy(updated)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return updated
