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
            raise s3_apply.Refusal(f'repoint requires an unstarted split part: {rel}')
        if not (work / rel).exists():
            text = show(root, base, rel)
            if text is None:
                raise s3_apply.Refusal(f'no baseline text for repoint {rel}')
            write(work, {rel: text})
        alternatives[rel] = (work / rel).read_text()
        write(work, split_apply.apply_repoint(work, entry))
    return alternatives


def validate(root: pathlib.Path, base: str, integration: str, found: list) -> None:
    """One reconstruction: all S9 splits, then repoints, then named methods."""
    splits, repoints = split_apply.load_recipes(root / 'tools/refactor/splits')
    with tempfile.TemporaryDirectory(prefix='s3-method-baseline-') as tmp:
        work = pathlib.Path(tmp)
        for directory in ('tools/refactor/splits', METHODS):
            if (root / directory).exists():
                shutil.copytree(root / directory, work / directory)
        checked, unavailable = _baseline_splits(root, base, integration, work, splits, found)
        alternatives = _baseline_repoints(root, base, work, repoints, unavailable)
        checked.update(alternatives)
        rewritten = set()
        for _, groups in found:
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
                    raise s3_apply.Refusal(f'no baseline text for {source}')
                write(work, {source: text})
            if prefix:
                write(work, s3_apply.apply(work, prefix))
                rewritten.add(source)
        for rel in sorted(checked):
            expected = (work / rel).read_text() if (work / rel).exists() else None
            actual = show(root, integration, rel)
            # D historically allows a repoint that has not yet been applied.
            # Once methods rewrite its source, only the ordered D->E result is valid.
            if rel not in rewritten and rel in alternatives and actual == alternatives[rel]:
                continue
            if actual != expected:
                raise s3_apply.Refusal(f'integration-only edit in {rel}')


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
