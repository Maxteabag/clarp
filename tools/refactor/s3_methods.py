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
    splits, _ = split_apply.load_recipes(root / 'tools/refactor/splits')
    split_paths = {entry['module'] for entry in splits}
    split_paths |= {part['module'] for entry in splits for part in entry['part']}
    if outputs & split_paths:
        raise s3_apply.Refusal('method output collides with a module split')
    return found


def affected(root: pathlib.Path, found: list) -> set[str]:
    """Original S9 modules whose regenerated parts are also method sources."""
    sources = {groups[0]['source_module'] for _, groups in found}
    splits, _ = split_apply.load_recipes(root / 'tools/refactor/splits')
    return {entry['module'] for entry in splits
            if sources & {entry['module'], *(p['module'] for p in entry['part'])}}


def show(root: pathlib.Path, ref: str, rel: str) -> str | None:
    result = subprocess.run(['git', '-C', str(root), 'show', f'{ref}:{rel}'],
                            capture_output=True, text=True)
    return result.stdout if result.returncode == 0 else None


def write(root: pathlib.Path, changes: dict[str, str]) -> None:
    for rel, text in changes.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def applied_prefix(root: pathlib.Path, integration: str, groups: list[dict]) -> list[dict]:
    exists = [show(root, integration, 'server/' + g['module']) is not None for g in groups]
    count = sum(exists)
    if exists != [True] * count + [False] * (len(groups) - count):
        raise s3_apply.Refusal('integration method outputs are not an ordered prefix')
    return groups[:count]


def compare_family(root: pathlib.Path, base: str, integration: str, work: pathlib.Path,
                   original: str, entry: dict | None, found: list) -> None:
    """Reconstruct one baseline family, then compare sources and generated files."""
    rel = 'server/' + original
    text = show(root, base, rel)
    if text is None:
        raise s3_apply.Refusal(f'no baseline text for {original}')
    write(work, {rel: text})
    family = {original}
    if entry:
        family |= {p['module'] for p in entry['part']}
    consumers = [(path, groups) for path, groups in found
                 if groups[0]['source_module'] in family]
    outputs = {'server/' + g['module'] for _, groups in consumers for g in groups}
    checked = {rel}
    if entry:
        parts = ['server/' + p['module'] for p in entry['part']]
        presence = [show(root, integration, p) is not None for p in parts]
        if not any(presence):
            # An unstarted split must be exactly the original baseline, with
            # no generated method modules hiding beside it.
            if show(root, integration, rel) != text or any(
                    show(root, integration, p) is not None for p in outputs):
                raise s3_apply.Refusal(f'integration-only edit before split {original}')
            return
        if not all(presence):
            raise s3_apply.Refusal(f'partial module split on integration: {original}')
        if show(root, base, parts[0]) is not None:
            raise s3_apply.Refusal(f'baseline is already split: {original}; needs explicit reconstruction')
        write(work, split_apply.apply_split(work, entry))
        checked.update(parts)
    for _, groups in consumers:
        prefix = applied_prefix(root, integration, groups)
        if prefix:
            write(work, s3_apply.apply(work, prefix))
        checked.update('server/' + g['module'] for g in groups)
    for path in sorted(checked):
        expected = (work / path).read_text() if (work / path).exists() else None
        if show(root, integration, path) != expected:
            raise s3_apply.Refusal(f'integration-only edit in {path}')


def validate(root: pathlib.Path, base: str, integration: str, found: list) -> None:
    """Validate affected S9 families and unsplit sources before taking main text."""
    splits, _ = split_apply.load_recipes(root / 'tools/refactor/splits')
    touched = affected(root, found)
    entries = {entry['module']: entry for entry in splits if entry['module'] in touched}
    covered = {p for entry in entries.values()
               for p in [entry['module'], *(part['module'] for part in entry['part'])]}
    standalone = {groups[0]['source_module'] for _, groups in found} - covered
    with tempfile.TemporaryDirectory(prefix='s3-method-baseline-') as tmp:
        for index, original in enumerate([*entries, *sorted(standalone)]):
            work = pathlib.Path(tmp, str(index))
            # Family import refusal reads only recipe data; no app is imported.
            for directory in ('tools/refactor/splits', METHODS):
                if (root / directory).exists():
                    shutil.copytree(root / directory, work / directory)
            compare_family(root, base, integration, work, original, entries.get(original), found)


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
