#!/usr/bin/env python3
"""Record the Python Host's managed skills (lib/managed_skills.py and the two routes of
lib/routes/providers.py) in stand-in homes, a stand-in release tree and a stand-in PATH.

Every case runs the reference's own functions on a scratch tree of its own: what a link in a
tool's skill folder reads as, the rows of GET /managed-skills, what POST /managed-skills
changes in each folder and in the chosen list, and how the two routes answer. Paths are
written with the scratch root as `/R`. Nothing of a real home is read or written."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scratch

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'server-rs/crates/clarp-kernel/tests/fixtures/managed-skills-vectors.json'

INTENTS = ('missing', 'healthy', 'outdated', 'dir', 'foreign')


class Tree:
    """One case's stand-in home, release tree and tool folders, under `base`."""

    def __init__(self, ms, base: Path, agy: bool, shared: str = ''):
        self.ms, self.base = ms, base
        self.share = base / 'share'
        self.release = self.share / 'releases/r1'
        self.older = self.share / 'releases/r0'
        self.home = base / 'home'
        self.claude = self.home / '.claude/skills'
        self.codex = self.home / '.codex/skills'
        self.agy = self.home / '.gemini/config/skills'
        (self.release / 'skills').mkdir(parents=True)
        (self.older / 'skills').mkdir(parents=True)
        (base / 'elsewhere').mkdir()
        self.claude.mkdir(parents=True)
        if shared == 'codex':
            self.codex.parent.mkdir(parents=True)
            self.codex.symlink_to(self.claude, target_is_directory=True)
        else:
            self.codex.mkdir(parents=True)
        if agy:
            self.agy.parent.mkdir(parents=True)
            if shared == 'agy':
                self.agy.symlink_to(self.claude, target_is_directory=True)
        ms.SHARE = self.share
        ms.RELEASE_ROOT = self.release
        ms.INSTALL_STATE = base / 'config/install.json'
        ms.CLAUDE_SKILLS, ms.CODEX_SKILLS, ms.AGY_SKILLS = self.claude, self.codex, self.agy

    def source(self, skill_id: str, present: bool = True) -> Path:
        source = self.release / 'skills' / skill_id
        if present:
            source.mkdir(parents=True, exist_ok=True)
            (source / 'SKILL.md').write_text('---\nname: x\n---\n')
        older = self.older / 'skills' / skill_id
        older.mkdir(parents=True, exist_ok=True)
        (older / 'SKILL.md').write_text('---\nname: x\n---\n')
        return source

    def put(self, folder: Path, skill_id: str, intent: str) -> None:
        at = folder / skill_id
        folder.mkdir(parents=True, exist_ok=True)
        if intent == 'missing' or at.exists() or at.is_symlink():
            return
        if intent == 'healthy':
            at.symlink_to(self.release / 'skills' / skill_id, target_is_directory=True)
        elif intent == 'outdated':
            at.symlink_to(self.older / 'skills' / skill_id, target_is_directory=True)
        elif intent == 'dir':
            at.mkdir()
            (at / 'SKILL.md').write_text('own\n')
        else:
            elsewhere = self.base / 'elsewhere' / skill_id
            elsewhere.mkdir(exist_ok=True)
            at.symlink_to(elsewhere, target_is_directory=True)

    def seen(self, folder: Path, skill_id: str) -> str:
        at = folder / skill_id
        if not at.exists() and not at.is_symlink():
            return 'missing'
        if not at.is_symlink():
            return 'dir'
        target = at.resolve()
        if target == (self.release / 'skills' / skill_id).resolve():
            return 'healthy'
        if target == (self.older / 'skills' / skill_id).resolve():
            return 'outdated'
        return 'foreign'

    def chosen(self, active):
        if active is not None:
            self.ms.INSTALL_STATE.parent.mkdir(parents=True, exist_ok=True)
            self.ms.INSTALL_STATE.write_text(json.dumps({'skills': active, 'channel': 'x'}))

    def manifest(self, value) -> None:
        path = self.release / 'skills/manifest.json'
        path.write_text(value if isinstance(value, str) else json.dumps(value))


def normal(root: Path, value):
    text = json.dumps(value)
    return json.loads(text.replace(json.dumps(str(root))[1:-1], '/R'))


def link_states(ms, root: Path):
    """What `_link_status` says of one destination, with the facts it read."""
    cases = []
    names = ['nothing', 'empty-dir', 'file', 'abs-source', 'rel-source', 'via-current',
             'chain', 'source-gone', 'older', 'older-gone', 'older-other-name',
             'older-other-middle', 'older-deeper', 'releases-shallow', 'releases-itself',
             'elsewhere', 'elsewhere-gone', 'current-older', 'loop', 'other-host-older']
    for n, name in enumerate(names):
        base = root / f'l{n}'
        base.mkdir()
        tree = Tree(ms, base, agy=False)
        source = tree.source('s', present=name != 'source-gone')
        dest = tree.claude / 's'
        releases = tree.share / 'releases'
        target = {
            'abs-source': source, 'source-gone': source,
            'rel-source': Path(os.path.relpath(source, tree.claude)),
            'via-current': tree.share / 'current/skills/s',
            'chain': base / 'hop',
            'older': tree.older / 'skills/s',
            'older-gone': releases / 'r9/skills/s',
            'older-other-name': tree.older / 'skills/t',
            'older-other-middle': tree.older / 'tools/s',
            'older-deeper': tree.older / 'skills/s/inner',
            'releases-shallow': releases / 'skills/s',
            'releases-itself': releases,
            'elsewhere': base / 'elsewhere',
            'elsewhere-gone': base / 'gone/s',
            'current-older': tree.share / 'current/skills/s',
            'loop': base / 'loop-a',
            'other-host-older': base / 'other-share/releases/r0/skills/s',
        }.get(name)
        if name in ('via-current',):
            (tree.share / 'current').symlink_to(tree.release, target_is_directory=True)
        if name == 'current-older':
            (tree.share / 'current').symlink_to(tree.older, target_is_directory=True)
        if name == 'chain':
            (base / 'hop').symlink_to(source, target_is_directory=True)
        if name == 'older-deeper':
            (tree.older / 'skills/s/inner').mkdir()
        if name == 'older-other-middle':
            (tree.older / 'tools/s').mkdir(parents=True)
        if name == 'loop':
            (base / 'loop-a').symlink_to(base / 'loop-b')
            (base / 'loop-b').symlink_to(base / 'loop-a')
        if name == 'other-host-older':
            target.mkdir(parents=True)
        if name == 'empty-dir':
            dest.mkdir()
        elif name == 'file':
            dest.write_text('x')
        elif target is not None:
            dest.symlink_to(target, target_is_directory=True)
        try:
            state = ms._link_status(dest, source)
        except (OSError, RuntimeError) as exc:
            state = type(exc).__name__
        at = ('nothing' if not dest.exists() and not dest.is_symlink() else
              'thing' if not dest.is_symlink() else 'link')
        try:
            resolved = str(dest.resolve(strict=False)) if at == 'link' else None
        except (OSError, RuntimeError) as exc:
            resolved = type(exc).__name__
        cases.append(normal(root, {
            'case': name, 'at': at, 'resolved': resolved,
            'source': str(source.resolve()), 'releases': str(releases.resolve()),
            'name': 's', 'state': state}))
    return cases


def requirement_cases(ms, root: Path):
    bin_dir = root / 'bin'
    bin_dir.mkdir()
    for name in ('gh', 'himalaya', 'g'):
        (bin_dir / name).write_text('#!/bin/sh\n')
        (bin_dir / name).chmod(0o755)
    (bin_dir / 'plain').write_text('not run\n')
    os.environ['PATH'] = str(bin_dir)
    cases = []
    values = [None, [], '', 'gh', ['gh'], ['wacli'], ['wacli or himalaya'], ['wacli or nope'], ['wacli  or  nope'],
              [' or '], ['gh or'], ['  gh  '], ['gh', 'wacli'], ['wacli', 'nope or no'],
              ['GH'], [str(bin_dir / 'gh')], [str(bin_dir / 'plain')], ['plain'],
              ['gh  or  wacli'], ['wacli OR gh'], ['wacli or  or gh'], ['or'], ['gh orwacli'],
              [5], [None], [True], [False], [1.5], [['gh']], [{'gh': 1}], {'gh': 1}, {},
              0, False, 'g', ['', 'gh'], [chr(0xdf)], 5, True]
    for value in values:
        item = {'id': 'x'} if value is None else {'id': 'x', 'requires': value}
        if value is None:
            item = {'id': 'x'}
        tried = {}
        try:
            for expression in item.get('requires') or []:
                for part in str(expression).split(' or '):
                    if part.strip():
                        tried[part.strip()] = bool(ms.shutil.which(part.strip()))
            outcome = list(ms._requirements(item))
        except TypeError as exc:
            outcome = type(exc).__name__
        cases.append(normal(root, {'item': item, 'found': tried, 'outcome': outcome}))
    return cases


def status_runs(ms, root: Path):
    runs = []
    packs = ['core', 'artifacts', None, 'personal', 'CORE']
    requires = [None, ['gh'], ['wacli']]
    for agy in (False, True):
        base = root / f'status-{int(agy)}'
        base.mkdir()
        tree = Tree(ms, base, agy=agy)
        items, active, present, n = [], [], [], 0
        for claude in INTENTS:
            for codex in INTENTS:
                for enabled in (False, True):
                    for has_source in (True, False):
                        for need in requires:
                            skill_id = f's{n}'
                            item = {'id': skill_id, 'description': f'd{n}'}
                            pack = packs[n % len(packs)]
                            if pack is not None:
                                item['pack'] = pack
                            if need is not None:
                                item['requires'] = need
                            if n % 7 == 3:
                                item['enabled'] = 'kept'
                                item['health'] = 'kept'
                            if n % 11 == 5:
                                item['default_enabled'] = False
                            tree.source(skill_id, present=has_source)
                            tree.put(tree.claude, skill_id, claude)
                            tree.put(tree.codex, skill_id, codex)
                            if agy:
                                tree.put(tree.agy, skill_id, INTENTS[n % len(INTENTS)])
                            if enabled:
                                active.append(skill_id)
                            if has_source:
                                present.append(skill_id)
                            items.append(item)
                            n += 1
        items.append({'id': 7, 'pack': 'core'})
        tree.source('7')
        active.append('7')
        present.append('7')
        active.append('gone-from-manifest')
        tree.manifest({'version': 1, 'skills': items})
        tree.chosen(active)
        found = {name: bool(ms.shutil.which(name)) for name in ('gh', 'wacli')}
        rows = ms.status()
        runs.append(normal(root, {'agy': agy, 'manifest': items, 'chosen': active,
                                  'present': present, 'found': found, 'rows': rows}))
    return runs


def manifests(ms, root: Path):
    cases = []
    values = [None, '', 'not json', '[]', '{}', '{"skills": {}}', '{"skills": null}',
              '{"skills": "x"}', '{"version": 1}', '{"skills": []}', '{"skills": [{"pack": "core"}]}',
              '{"skills": ["x"]}', '{"skills": [5]}', '{"skills": [{"id": null}]}',
              '{"skills": [{"id": "a"}], "version": 2}', '"skills"', 'null']
    for n, value in enumerate(values):
        base = root / f'm{n}'
        base.mkdir()
        tree = Tree(ms, base, agy=False)
        tree.source('a')
        if value is not None:
            tree.manifest(value)
        try:
            outcome = ['rows', [row['id'] for row in ms.status()]]
        except RuntimeError as exc:
            outcome = ['unavailable', str(exc)]
        except Exception as exc:  # noqa: BLE001 - the reference's own crash is the vector
            outcome = ['crash', type(exc).__name__]
        cases.append({'manifest': value, 'outcome': outcome})
    return cases


def change_cases(ms, root: Path):
    cases = []
    n = 0

    def one(pack, enabled, claude, codex, agy, *, skill='k', present=True, active=None,
            shared='', known=True, roots_made=True, twice=False):
        nonlocal n
        base = root / f'c{n}'
        n += 1
        base.mkdir()
        tree = Tree(ms, base, agy=agy is not None, shared=shared)
        items = [{'id': 'other', 'pack': 'core'}]
        if twice:
            items.append({'id': skill, 'pack': 'core', 'description': 'first of two'})
        if known:
            item = {'id': skill, 'description': 'desc'}
            if pack is not None:
                item['pack'] = pack
            items.append(item)
        tree.manifest({'skills': items})
        tree.source(skill, present=present)
        if not roots_made:
            for folder in (tree.claude, tree.codex):
                folder.rmdir()
        else:
            tree.put(tree.claude, skill, claude)
            if shared != 'codex':
                tree.put(tree.codex, skill, codex)
            if agy is not None and shared != 'agy':
                tree.put(tree.agy, skill, agy)
        tree.chosen(active)
        before = {'claude': tree.seen(tree.claude, skill), 'codex': tree.seen(tree.codex, skill)}
        if agy is not None:
            before['agy'] = tree.seen(tree.agy, skill)
        roots = [[name, str(path.resolve())] for name, path in
                 [('claude', tree.claude), ('codex', tree.codex)] +
                 ([('agy', tree.agy)] if ms.agy_usable(tree.agy) else [])]
        used = [name for name, _ in ms._roots()]
        try:
            result = ['ok', ms.set_enabled(skill, enabled)]
        except ValueError as exc:
            result = ['refused', str(exc)]
        after = {'claude': tree.seen(tree.claude, skill), 'codex': tree.seen(tree.codex, skill)}
        if agy is not None:
            after['agy'] = tree.seen(tree.agy, skill)
        state = json.loads(ms.INSTALL_STATE.read_text()) if ms.INSTALL_STATE.is_file() else None
        cases.append(normal(root, {
            'skill': skill, 'manifest': items, 'enabled': enabled,
            'present': present, 'shared': shared, 'chosen': active, 'before': before,
            'roots': roots, 'used': used,
            'destinations': {name: str(Path(path) / skill) for name, path in
                             [('claude', tree.claude), ('codex', tree.codex), ('agy', tree.agy)]},
            'result': result, 'after': after,
            'chosen_after': None if state is None else state.get('skills'),
            'state_kept': None if state is None else sorted(k for k in state if k != 'skills')}))

    for pack in ('core', 'artifacts'):
        for enabled in (True, False):
            for claude in INTENTS:
                for codex in INTENTS:
                    for agy in (None,) + INTENTS:
                        one(pack, enabled, claude, codex, agy, active=['zeta', 'k', 'alpha'] if
                            (n % 3 == 0) else (['k'] if n % 3 == 1 else None))
    for enabled in (True, False):
        one('artifacts', enabled, 'missing', 'missing', None, known=False)
        one('core', enabled, 'missing', 'missing', None, present=False)
        one('artifacts', enabled, 'healthy', 'healthy', None, present=False)
        one(None, enabled, 'healthy', 'missing', 'missing', active=['k', 'k', 'b'])
        one('CORE', enabled, 'outdated', 'missing', None)
        one('artifacts', enabled, 'missing', 'missing', None, roots_made=False)
        one('artifacts', enabled, 'healthy', 'missing', None, twice=True)
        for intent in INTENTS:
            one('artifacts', enabled, intent, intent, None, shared='codex')
            one('artifacts', enabled, intent, 'missing', intent, shared='agy')
            one('artifacts', enabled, 'missing', intent, intent, shared='agy')
    return cases


def routes(root: Path):
    from lib.routes import providers
    bodies = [None, {}, {'skill_id': 'x'}, {'enabled': True}, {'skill_id': '', 'enabled': True},
              {'skill_id': '  ', 'enabled': True}, {'skill_id': ' x ', 'enabled': True},
              {'skill_id': 'x', 'enabled': False}, {'skill_id': 5, 'enabled': True},
              {'skill_id': 0, 'enabled': True}, {'skill_id': False, 'enabled': True},
              {'skill_id': True, 'enabled': True}, {'skill_id': [], 'enabled': True},
              {'skill_id': [1, 'a'], 'enabled': True}, {'skill_id': {'a': 1}, 'enabled': True},
              {'skill_id': 1.5, 'enabled': True}, {'skill_id': {}, 'enabled': True}, {'skill_id': None, 'enabled': True},
              {'skill_id': 'x', 'enabled': 1}, {'skill_id': 'x', 'enabled': 'true'},
              {'skill_id': 'x', 'enabled': None}, {'skill_id': 'x', 'enabled': 0},
              {'skill_id': 'x'}, {'skill_id': '\tx\n', 'enabled': False},
              {'skill_id': chr(0xdf), 'enabled': True}]
    asked = []
    for body in bodies:
        answers, called = [], []
        handler = SimpleNamespace(
            _read_json=lambda: body,
            _json_error=lambda code, why: answers.append([code, {'error': why}]),
            _json_ok=lambda result: answers.append([200, result]))

        def set_enabled(skill_id, enabled):
            called.append([skill_id, enabled])
            return {'id': skill_id, 'enabled': enabled}
        with patch('lib.managed_skills.set_enabled', set_enabled):
            providers.ProviderRoutes._handle_managed_skills_update(handler)
        asked.append([body, called, answers[0]])
    failures = []
    for kind in ('value', 'os'):
        answers = []
        handler = SimpleNamespace(
            _read_json=lambda: {'skill_id': 'x', 'enabled': True},
            _json_error=lambda code, why: answers.append([code, {'error': why}]),
            _json_ok=lambda result: answers.append([200, result]))

        def refuse(skill_id, enabled, kind=kind):
            raise ValueError('refused words') if kind == 'value' else OSError('system words')
        with patch('lib.managed_skills.set_enabled', refuse), \
                patch.object(providers, 'log_exception', lambda *a, **k: None):
            providers.ProviderRoutes._handle_managed_skills_update(handler)
        failures.append([kind, answers[0]])
    reads = []
    for kind in ('rows', 'runtime', 'os'):
        answers = []
        handler = SimpleNamespace(
            _json_error=lambda code, why: answers.append([code, {'error': why}]),
            _json_ok=lambda result: answers.append([200, result]))

        def status(kind=kind):
            if kind == 'runtime':
                raise RuntimeError('Clarp skill manifest is unavailable')
            if kind == 'os':
                raise OSError('system words')
            return [{'id': 'a'}]
        with patch('lib.managed_skills.status', status), \
                patch.object(providers, 'log_exception', lambda *a, **k: None), \
                patch.dict(os.environ, {'CLARP_DEPLOYMENT_MODE': 'native'}):
            providers.ProviderRoutes._handle_managed_skills(handler)
        reads.append([kind, answers[0]])
    return {'asked': asked, 'failures': failures, 'reads': reads}


def distinct(ms, root: Path):
    """Which tool folders `skill_roots` writes, where some are the same folder."""
    cases = []
    for n, (codex_shared, agy_kind) in enumerate(
            [(c, a) for c in (False, True) for a in ('absent', 'own', 'shared', 'dangling', 'to-codex')]):
        base = root / f'd{n}'
        base.mkdir()
        tree = Tree(ms, base, agy=agy_kind != 'absent', shared='codex' if codex_shared else '')
        if agy_kind == 'own':
            tree.agy.mkdir()
        elif agy_kind == 'shared':
            tree.agy.symlink_to(tree.claude, target_is_directory=True)
        elif agy_kind == 'dangling':
            tree.agy.symlink_to(base / 'nowhere', target_is_directory=True)
        elif agy_kind == 'to-codex':
            tree.agy.symlink_to(tree.codex, target_is_directory=True)
        usable = ms.agy_usable(tree.agy)
        facts = {'parent_is_dir': tree.agy.parent.is_dir(), 'is_link': tree.agy.is_symlink(),
                 'is_dir': tree.agy.is_dir()}
        offered = [['claude', str(tree.claude.resolve())], ['codex', str(tree.codex.resolve())]]
        if usable:
            offered.append(['agy', str(tree.agy.resolve())])
        kept = [name for name, _ in ms.skill_roots(tree.claude, tree.codex, tree.agy)]
        cases.append(normal(root, {'agy': agy_kind, 'codex_shared': codex_shared,
                                   'facts': facts, 'usable': usable, 'offered': offered,
                                   'kept': kept}))
    return cases


def record(root: Path):
    spec = importlib.util.spec_from_file_location('replay_host', ROOT / 'tests/replay/host.py')
    replay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(replay)
    replay.isolate(root)
    replay.loopback_only()
    home = root / 'home'
    os.environ['CLARP_INSTALL_STATE'] = str(root / 'config/install.json')
    os.environ['CLARP_CLAUDE_SKILLS'] = str(home / '.claude/skills')
    os.environ['CLARP_CODEX_SKILLS'] = str(home / '.codex/skills')
    os.environ['CLARP_AGY_SKILLS'] = str(home / '.gemini/config/skills')
    sys.path.insert(0, str(ROOT / 'server'))
    from lib import managed_skills as ms
    work = root / 'cases'

    def sub(name):
        (work / name).mkdir(parents=True)
        return work / name
    return {
        'link_states': link_states(ms, sub('links')),
        'requirements': requirement_cases(ms, sub('requirements')),
        'distinct': distinct(ms, sub('distinct')),
        'manifests': manifests(ms, sub('manifests')),
        'status': status_runs(ms, sub('status')),
        'changes': change_cases(ms, sub('changes')),
        'routes': routes(root),
    }

if __name__ == '__main__':
    if '--record' in sys.argv:
        with scratch.directory() as area:
            data = record(Path(area))
            print(json.dumps(normal(Path(area), data)))
    else:
        env = scratch.child_env({'PATH': os.defpath, 'LANG': 'C.UTF-8', 'TZ': 'UTC'})
        data = json.loads(subprocess.check_output([sys.executable, '-I', __file__, '--record'], env=env))
        OUT.write_text(json.dumps(data, indent=1, ensure_ascii=True) + '\n', encoding='ascii')
        print({key: len(rows) for key, rows in data.items()})
