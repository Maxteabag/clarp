#!/usr/bin/env python3
"""Enumerate named-class recipes and validate integration before regeneration.

This orchestrates s3_apply's existing byte mover; it never moves code itself.
The merge script must restore this file and methods/ from pinned main first.
"""
from __future__ import annotations

import argparse
import pathlib
import re
import shutil
import subprocess
import tempfile
import tomllib

import s3_apply
import split_apply


METHODS = 'tools/refactor/methods'


def recipes(root: pathlib.Path) -> list[tuple[pathlib.Path, list[dict]]]:
    found = []
    outputs, sources = set(), set()
    directory = root / METHODS
    for path in sorted(directory.glob('*.toml')):
        if not re.fullmatch(r'[A-Za-z0-9_-]+\.toml', path.name):
            raise s3_apply.Refusal(f'noncanonical method recipe {path.name}')
        groups = s3_apply.load_recipe(path)
        if not groups or s3_apply.load_helpers(path):
            raise s3_apply.Refusal(f'{path.name}: methods require groups and no helpers')
        source = groups[0]['source_module']
        # Reuse S9's canonical lib path and sibling-output validation.
        split_apply._check_split({'name': path.name, 'module': source,
            'part': [{'module': g['module'], 'names': g['members']} for g in groups]})
        key = (source, groups[0]['source_class'])
        if key == ('server.py', 'Handler') or key in sources:
            raise s3_apply.Refusal(f'{path.name}: duplicate or default S3 source class')
        sources.add(key)
        for group in groups:
            if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', group['name']):
                raise s3_apply.Refusal(f'{path.name}: noncanonical group name')
            if group['module'] in outputs:
                raise s3_apply.Refusal(f'duplicate method output {group["module"]}')
            outputs.add(group['module'])
        found.append((path, groups))
    source_paths = {source for source, _ in sources}
    if outputs & source_paths:
        raise s3_apply.Refusal('method output is also a method source')
    splits, repoints = split_apply.load_recipes(root / 'tools/refactor/splits')
    split_paths = {entry['module'] for entry in splits}
    split_paths |= {part['module'] for entry in splits for part in entry['part']}
    if outputs & split_paths:
        raise s3_apply.Refusal('method output collides with a module split')
    forbidden = {entry['file'] for entry in repoints}
    routes = root / 'tools/refactor/s3_moves.toml'
    if routes.exists():
        forbidden |= {g['module'] for g in tomllib.loads(routes.read_text()).get('group', [])}
    if outputs & forbidden:
        raise s3_apply.Refusal('method output collides with a repoint file or S3 mixin')
    return found


def affected(root: pathlib.Path, found: list) -> set[str]:
    """Original S9 modules whose regenerated parts are also method sources."""
    sources = {groups[0]['source_module'] for _, groups in found}
    splits, _ = split_apply.load_recipes(root / 'tools/refactor/splits')
    return {entry['module'] for entry in splits
            if sources & {entry['module'], *(p['module'] for p in entry['part'])}}


def _git(root: pathlib.Path, *args: str) -> bytes:
    """Raw Git bytes: object errors and line endings must stay distinguishable."""
    try:
        result = subprocess.run(['git', '-C', str(root), *args], capture_output=True)
    except OSError as exc:
        raise s3_apply.Refusal(f'Git data read failed: {exc}') from exc
    if result.returncode:
        detail = result.stderr.decode('utf-8', errors='replace').strip()
        raise s3_apply.Refusal(f'Git data read failed ({args[0]}): {detail}')
    return result.stdout


def _decode(data: bytes, label: str) -> str:
    """Decode parser/metadata input strictly, without newline translation."""
    try:
        return data.decode('utf-8')
    except UnicodeError as exc:
        raise s3_apply.Refusal(f'invalid UTF-8 Git data for {label}') from exc

def _pin(root: pathlib.Path, ref: str) -> str:
    commit = _decode(_git(root, 'rev-parse', '--verify', f'{ref}^{{commit}}'), ref).strip()
    if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', commit):
        raise s3_apply.Refusal(f'Git did not resolve a full commit for {ref!r}')
    return commit


def show(root: pathlib.Path, ref: str, rel: str) -> bytes | None:
    if not split_apply._canonical(rel):
        raise s3_apply.Refusal(f'noncanonical Git data path {rel!r}')
    listing = _decode(_git(root, 'ls-tree', '-z', ref, '--', rel), rel)
    if not listing:
        return None
    entries = listing.rstrip('\0').split('\0')
    if len(entries) != 1:
        raise s3_apply.Refusal(f'ambiguous Git data path {rel!r}')
    metadata, path = entries[0].split('\t', 1)
    mode, kind, blob = metadata.split()
    if path != rel or kind != 'blob' or mode not in {'100644', '100755'}:
        raise s3_apply.Refusal(f'Git data path is not a plain file: {rel}')
    return _git(root, 'cat-file', 'blob', blob)


def _historical(root: pathlib.Path, base: str, target: pathlib.Path):
    """Load only pinned recipe data, never historical Python/tooling."""
    folders = ('tools/refactor/splits', METHODS)
    listing = _decode(_git(root, 'ls-tree', '-r', '-z', base, '--',
                           *folders, 'tools/refactor/s3_moves.toml'), base)
    for entry in listing.rstrip('\0').split('\0') if listing else []:
        metadata, rel = entry.split('\t', 1)
        if not rel.endswith('.toml'):
            continue
        mode, kind, blob = metadata.split()
        if (not split_apply._canonical(rel) or kind != 'blob'
                or mode not in {'100644', '100755'}
                or pathlib.PurePosixPath(rel).parent.as_posix()
                   not in {*folders, 'tools/refactor'}):
            raise s3_apply.Refusal(f'noncanonical historical recipe {rel!r}')
        write(target, {rel: _git(root, 'cat-file', 'blob', blob)})
        print(f's3_methods: historical recipe {base} {rel} {blob}')
    try:
        found = recipes(target)
        splits, repoints = split_apply.load_recipes(target / 'tools/refactor/splits')
    except (KeyError, TypeError, ValueError) as exc:
        raise s3_apply.Refusal(f'invalid historical recipe data at {base}: {exc}') from exc
    return splits, repoints, found


def _retained_order(old, new, label):
    positions = {name: i for i, name in enumerate(new)}
    if any(name not in positions for name in old):
        raise s3_apply.Refusal(f'historical {label} retired or renamed')
    indices = [positions[name] for name in old]
    if indices != sorted(indices):
        raise s3_apply.Refusal(f'historical {label} reordered')


def _evolution(old_splits, old_repoints, old_found, splits, repoints, found):
    _retained_order([e['name'] for e in old_splits],
                    [e['name'] for e in splits], 'split order')
    new_splits = {e['name']: e for e in splits}
    for old in old_splits:
        new = new_splits[old['name']]
        if any(old[k] != new[k] for k in ('module', 'recipe')):
            raise s3_apply.Refusal(f'historical split source/recipe topology changed: {old["name"]}')
        _retained_order([p['module'] for p in old['part']],
                        [p['module'] for p in new['part']], f'{old["name"]} parts')
        parts = {p['module']: p for p in new['part']}
        for part in old['part']:
            _retained_order(part['names'], parts[part['module']]['names'],
                            f'{old["name"]} members in {part["module"]}')
    _retained_order([e['name'] for e in old_repoints],
                    [e['name'] for e in repoints], 'repoint order')
    new_repoints = {e['name']: e for e in repoints}
    for old in old_repoints:
        new = new_repoints[old['name']]
        if any(old[k] != new[k] for k in ('file', 'from_module', 'to_module', 'recipe')):
            raise s3_apply.Refusal(f'historical repoint recipe topology changed: {old["name"]}')
        _retained_order(old['names'], new['names'], f'{old["name"]} repoint members')
    new_methods = {path.name: groups for path, groups in found}
    _retained_order([p.name for p, _ in old_found],
                    [p.name for p, _ in found], 'method recipe order')
    for path, old in old_found:
        new = new_methods[path.name]
        if any(old[0][k] != new[0][k] for k in ('source_module', 'source_class')):
            raise s3_apply.Refusal(f'historical method source topology changed: {path.name}')
        _retained_order([g['name'] for g in old], [g['name'] for g in new],
                        f'{path.name} group order')
        groups = {g['name']: g for g in new}
        for group in old:
            after = groups[group['name']]
            if any(group[k] != after[k] for k in ('module', 'class')):
                raise s3_apply.Refusal(f'historical method output topology changed: {group["name"]}')
            _retained_order(group['members'], after['members'],
                            f'{group["name"]} members')


def _paths(splits, repoints, found):
    originals = {'server/' + e['module'] for e in splits}
    originals |= {'server/' + e['file'] for e in repoints}
    originals |= {'server/' + groups[0]['source_module'] for _, groups in found}
    outputs = {'server/' + p['module'] for e in splits for p in e['part']}
    outputs |= {'server/' + g['module'] for _, groups in found for g in groups}
    return originals, outputs

def write(root: pathlib.Path, changes: dict[str, str | bytes]) -> None:
    for rel, text in changes.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text if isinstance(text, bytes) else text.encode('utf-8'))


def applied_prefix(root: pathlib.Path, integration: str, groups: list[dict]) -> list[dict]:
    exists = [show(root, integration, 'server/' + g['module']) is not None for g in groups]
    count = sum(exists)
    if exists != [True] * count + [False] * (len(groups) - count):
        raise s3_apply.Refusal('integration method outputs are not an ordered prefix')
    return groups[:count]


def _baseline_splits(root, base, integration, work, splits, found):
    checked, unavailable = set(), set()
    for entry in splits:
        old = entry['module']
        rels = ['server/' + old] + ['server/' + p['module'] for p in entry['part']]
        text = show(root, base, rels[0])
        if text is None:
            raise s3_apply.Refusal(f'no baseline text for {old}')
        write(work, {rels[0]: text})
        checked.update(rels)
        present = [show(root, integration, p) is not None for p in rels[1:]]
        if not any(present):
            if any(groups[0]['source_module'] == old for _, groups in found):
                raise s3_apply.Refusal(f'method source is an unstarted split old module: {old}')
            unavailable.update(rels[1:])
            continue
        if not all(present):
            raise s3_apply.Refusal(f'partial module split on integration: {old}')
        if show(root, base, rels[1]) is not None:
            for rel in rels[1:]:
                text = show(root, base, rel)
                if text is None:
                    raise s3_apply.Refusal(f'partial baseline split: {old}')
                write(work, {rel: text})
        else:
            write(work, split_apply.apply_split(work, entry))
    return checked, unavailable


def _baseline_repoints(root, base, work, repoints, unavailable):
    alternatives = {}
    for entry in repoints:
        rel = 'server/' + entry['file']
        if rel in unavailable:
            # Integration has not applied this split yet. Its original text
            # and absent parts are checked above; D will create the part before
            # executing its repoint, so there is no part text to compare now.
            continue
        if not (work / rel).exists():
            text = show(root, base, rel)
            if text is None:
                raise s3_apply.Refusal(f'no baseline text for repoint {rel}')
            write(work, {rel: text})
        alternatives[rel] = (work / rel).read_bytes()
        write(work, split_apply.apply_repoint(work, entry))
    return alternatives


def validate(root: pathlib.Path, base: str, integration: str, found: list) -> None:
    """Validate history with its own data; never weaken current generation."""
    base, integration = _pin(root, base), _pin(root, integration)
    splits, repoints = split_apply.load_recipes(root / 'tools/refactor/splits')
    for entry in splits:
        old = entry['module']
        present = [show(root, integration, 'server/' + p['module']) is not None
                   for p in entry['part']]
        if not any(present) and any(g[0]['source_module'] == old for _, g in found):
            raise s3_apply.Refusal(f'method source is an unstarted split old module: {old}')
    with tempfile.TemporaryDirectory(prefix='s3-method-baseline-') as tmp:
        data, work = pathlib.Path(tmp, 'recipes'), pathlib.Path(tmp, 'source')
        data.mkdir()
        old_splits, old_repoints, old_found = _historical(root, base, data)
        _evolution(old_splits, old_repoints, old_found, splits, repoints, found)
        shutil.copytree(data, work)
        checked, unavailable = _baseline_splits(
            root, base, integration, work, old_splits, old_found)
        alternatives = _baseline_repoints(
            root, base, work, old_repoints, unavailable)
        checked.update(alternatives)
        rewritten = set()
        for _, groups in old_found:
            source = 'server/' + groups[0]['source_module']
            prefix = applied_prefix(root, integration, groups)
            checked.add(source)
            checked.update('server/' + g['module'] for g in groups)
            if source in unavailable:
                if prefix:
                    raise s3_apply.Refusal(f'method outputs precede their module split: {source}')
                continue
            if not (work / source).exists():
                text = show(root, base, source)
                if text is None:
                    raise s3_apply.Refusal(f'no historical baseline text for {source}')
                write(work, {source: text})
            for count in range(1, len(prefix) + 1):
                write(work, s3_apply.apply(work, prefix[:count]))
            if prefix:
                rewritten.add(source)
        old_originals, old_outputs = _paths(old_splits, old_repoints, old_found)
        originals, outputs = _paths(splits, repoints, found)
        checked.update(old_originals | old_outputs | originals | outputs)
        # A newly declared output is historically absent, not an optional old
        # output. Never bless an applied-looking family without its old recipe.
        for rel in outputs - old_outputs:
            if show(root, base, rel) is not None or (work / rel).exists():
                raise s3_apply.Refusal(f'new output lacks proven historical absence: {rel}')
        # First introductions retain any original historical source, including
        # a source already reconstructed as a part of another historical split.
        for rel in originals:
            if not (work / rel).exists() and rel not in unavailable:
                text = show(root, base, rel)
                if text is not None:
                    write(work, {rel: text})
        labels = {}
        for entry in splits:
            applied = any(show(root, integration, 'server/' + p['module']) is not None
                          for p in entry['part'])
            label = f'split {entry["name"]}' + ('' if applied else ', not yet applied')
            for rel in [entry['module'], *(p['module'] for p in entry['part'])]:
                labels['server/' + rel] = label
        for rel in sorted(checked):
            expected = (work / rel).read_bytes() if (work / rel).exists() else None
            actual = show(root, integration, rel)
            if rel not in rewritten and rel in alternatives and actual == alternatives[rel]:
                continue
            if actual != expected:
                suffix = f' ({labels[rel]})' if rel in labels else ''
                raise s3_apply.Refusal(f'integration-only edit in {rel}{suffix}')

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=pathlib.Path, required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--list', action='store_true')
    action.add_argument('--affected', action='store_true')
    action.add_argument('--validate', nargs=2, metavar=('BASE', 'INTEGRATION'))
    args = parser.parse_args()
    try:
        found = recipes(args.root)
        if args.list:
            for path, groups in found:
                for group in groups:
                    source = group['source_module']
                    move = f"{source}::{group['source_class']}={group['module']}::{group['class']}"
                    print(path.name, group['name'], source, group['module'], move)
        elif args.affected:
            print('\n'.join(sorted(affected(args.root, found))))
        else:
            validate(args.root, *args.validate, found)
    except s3_apply.Refusal as exc:
        print(f's3_methods: REFUSED: {exc}')
        return 11
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
