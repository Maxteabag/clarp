#!/usr/bin/env python3
"""Writes the table by which the kernel folds the case of a text as the Python Host's interpreter does.

Python compares names without regard to case by `str.casefold()`, which is not what Rust's lower-casing does: it
folds to more than one sign where a sign has no single small form, and knows the signs of one edition of Unicode,
that interpreter's. So the kernel folds by that interpreter's own table: every sign it folds to something else, and
what to.

    <the Python Host's interpreter> server-rs/tools/casefold-table.py

Writes crates/clarp-kernel/src/python/folds.rs; the same file at every run on the same interpreter.
"""
import pathlib
import sys
import unicodedata

REPO = pathlib.Path(__file__).resolve().parents[2]
OUT = REPO / 'server-rs' / 'crates' / 'clarp-kernel' / 'src' / 'python' / 'folds.rs'


def main() -> int:
    folds = [(sign, chr(sign).casefold()) for sign in range(0x110000) if chr(sign).casefold() != chr(sign)]
    lines = [
        '//! The signs Python folds to something else when it folds the case of a text (`str.casefold()`), and what to.',
        '//!',
        f'//! Written by `server-rs/tools/casefold-table.py` from the Python Host\'s interpreter (Unicode '
        f'{unicodedata.unidata_version}): do not',
        '//! change it by hand. In the order of the signs, so that one is found by halving.',
        '',
        f'pub(super) static FOLDS: [(u32, &str); {len(folds)}] = [',
    ]
    for sign, folded in folds:
        written = ''.join(f'\\u{{{ord(part):x}}}' for part in folded)
        lines.append(f'    (0x{sign:04x}, "{written}"),')
    lines.append('];')
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text('\n'.join(lines) + '\n', encoding='ascii')
    print(f'{OUT.relative_to(REPO)}: {len(folds)} signs, {sum(len(f) > 1 for _, f in folds)} of them to more than one, '
          f'Unicode {unicodedata.unidata_version}, Python {sys.version.split()[0]}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
