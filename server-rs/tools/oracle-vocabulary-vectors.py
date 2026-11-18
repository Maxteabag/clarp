#!/usr/bin/env python3
"""Writes down which prepared vocabulary the Python Host takes for a call of the Oracle, and which it refuses.

An owner can prepare, for the Oracle's contact, the names and words that occur in what is said to it: a small file
that names the contact, the text the words were taken from (by its path and its digest) and the words. A call of the
Oracle that has that contact tells its voice the words; a file that is not as it must be refuses the call, in words.
The rule is `server/lib/oracle_voice_context.load`, run here as it is on files of this program's own.

For every case it is written down what the small file holds, what stands at the path it names (a text, or nothing
that can be read as one), and what came of it: what the voice is to be told from, or the words of the refusal, and
whether those words are the rule's own or the language's (for a file that is no JSON, a path that is no text, a text
that is not UTF-8), which another Host cannot say.

    <the Python Host's interpreter> server-rs/tools/oracle-vocabulary-vectors.py

It runs in a process of its own whose every home, data, config and cache directory is a new empty directory (the
replay harness's own isolation), with no way out of the machine. The same file at every run. Nothing is logged.
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[2]
OUT = REPO / 'server-rs' / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'oracle-vocabulary-vectors.json'
# Where the files of the cases stand while this runs: the same on every machine, since a case names its text by its
# path.
PLACE = pathlib.Path('/var/tmp/clarp-oracle-vocabulary-vectors')
TEXT = ('The pump at Drammen feeds the line to Stra' + chr(0xdf) + 'e 4. Theo keeps the valve; '
        'Mike keeps the gauge.\n' + 'a' * 200 + '\n')


def digest(text) -> str:
    raw = text if isinstance(text, bytes) else text.encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def case(name, change=None, *, primary='theo', text=TEXT, at='text.txt', raw=None, pad=None, **said):
    """A case: the small file as it is to be, changed by what the case says, and the text it names.
    `said` replaces fields of the small file; `change` does the same for its binding. `text`: what stands at the
    path (`None`: nothing; `bytes`: bytes that are no text; `...`: a directory). `raw`: the small file is these
    bytes, whatever else is said. `pad`: blanks are added to the small file up to so many bytes."""
    return {'name': name, 'primary': primary, 'text': text, 'at': at, 'raw': raw, 'pad': pad, 'said': said,
            'binding': change or {}}


LONG = 'a' * 160
CASES = [
    case('a vocabulary as it is to be'),
    case('other things in the file are passed over', more='things', terms=['Drammen', 'Theo']),
    case('the words are taken as they stand, in their order, also twice',
         terms=['valve', 'Drammen', 'valve', 'the pump at drammen', 'STRASSE 4', LONG]),
    case('a small file of the most bytes it may have', pad=16384),
    case('a small file that is a byte too large', pad=16385),
    case('a file that is no JSON', raw=b'{"version": 1, '),
    case('a file that is no text', raw=b'\xff\xfe{}'),
    case('a file that is a list', raw=b'[1]'),
    case('a file that is a word', raw=b'"vocabulary"'),
    case('a version that is another', version=2),
    case('a version that is a text', version='1'),
    case('a version with a point', version=1.0),
    case('a version that is a yes', version=True),
    case('no version', version='~drop'),
    case('a file of another contact', primary_session='mike'),
    case('a file that names its contact in other letters', primary_session='Theo'),
    case('a file that names no contact', primary_session='~drop'),
    case('a call that has no contact, and a file that names none', primary='', primary_session=''),
    case('a call of another contact', primary='mike'),
    case('no binding', prepared_context='~drop'),
    case('a binding that is nothing', prepared_context=None),
    case('a binding that is an empty list', prepared_context=[]),
    case('a binding that is a list', prepared_context=['text.txt']),
    case('a binding that is a text', prepared_context='text.txt'),
    case('a binding without a path', {'path': '~drop'}),
    case('a path that is a number', {'path': 7}),
    case('a path that is nothing', {'path': None}),
    case('a path from where the Host stands', {'path': 'text.txt'}),
    case('a path where nothing is', text=None),
    case('a path where a directory is', text=...),
    case('a text that is no UTF-8', text=b'Drammen \xff valve'),
    case('a digest of another text', {'sha256': digest('another text')}),
    case('a digest in large letters', {'sha256': digest(TEXT).upper()}),
    case('no digest', {'sha256': '~drop'}),
    case('a digest that is a number', {'sha256': 7}),
    case('words that are no list', terms='Drammen'),
    case('no words', terms='~drop'),
    case('an empty list of words', terms=[]),
    case('as many words as there may be', terms=['Drammen'] * 64),
    case('one word too many', terms=['Drammen'] * 65),
    case('a word that is a number', terms=['Drammen', 7]),
    case('a word that is nothing', terms=['Drammen', None]),
    case('a word that is empty', terms=['Drammen', '']),
    case('a word of one sign', terms=['4']),
    case('a word that is a sign too long', terms=['a' * 161]),
    case('a word of two lines, as the text has it', terms=['gauge.\naaa']),
    case('a word with a return in it, as the text has it', text='Drammen\rfeeds', terms=['Drammen\rfeeds']),
    case('the two halves of such a word', text='Drammen\rfeeds', terms=['Drammen', 'feeds']),
    case('a word the text has not', terms=['Drammen', 'Bergen']),
    case('a word the text has in other letters', terms=['DRAMMEN', 'stra' + chr(0xdf) + 'e', 'Strasse']),
    case('words of as many signs as there may be together', terms=['a' * 150] * 20),
    case('words of a sign too many together', terms=['a' * 150] * 19 + ['a' * 151]),
    case('a text with a mark of its order before it', text='﻿Drammen', terms=['Drammen']),
    case('an empty text', text='', terms=['Drammen']),
]


def run() -> dict:
    """Runs in a process of its own: what the Python Host's module does."""
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    root = pathlib.Path(tempfile.mkdtemp(prefix='clarp-oracle-vocabulary-vectors-', dir='/var/tmp'))
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    from lib import oracle_voice_context as rule

    shutil.rmtree(PLACE, ignore_errors=True)
    PLACE.mkdir()
    played = []
    try:
        for number, one in enumerate(CASES):
            folder = PLACE / f'case-{number}'
            folder.mkdir()
            where = folder / one['at']
            text = one['text']
            if text is ...:
                where.mkdir()
            elif isinstance(text, bytes):
                where.write_bytes(text)
            elif text is not None:
                where.write_bytes(text.encode('utf-8'))
            binding = {'path': str(where), 'sha256': digest(text) if isinstance(text, (str, bytes)) else digest('')}
            binding.update(one['binding'])
            held = {'version': 1, 'primary_session': 'theo', 'prepared_context': binding,
                    'terms': ['Drammen', 'Stra' + chr(0xdf) + 'e', 'valve']}
            held.update(one['said'])
            for part in (held, binding):
                for name in [name for name, value in part.items() if value == '~drop' and isinstance(value, str)]:
                    del part[name]
            small = one['raw'] if one['raw'] is not None else json.dumps(held).encode('utf-8')
            if one['pad']:
                small += b' ' * (one['pad'] - len(small))
            sidecar = folder / 'vocabulary.json'
            sidecar.write_bytes(small)
            # What a Host's reader finds at the path the file names: the text's bytes, or nothing where the path
            # is not whole, names no file, or names one too large to be a text of this kind.
            named = held.get('prepared_context') if isinstance(held.get('prepared_context'), dict) else {}
            path = named.get('path') if isinstance(named, dict) else None
            found = None
            if isinstance(path, str) and pathlib.Path(path).is_absolute() and pathlib.Path(path).is_file():
                found = base64.b64encode(pathlib.Path(path).read_bytes()).decode('ascii')
            done = {'name': one['name'], 'primary': one['primary'], 'file': base64.b64encode(small).decode('ascii'),
                    'bytes': len(small), 'asks_for': path if isinstance(path, str) else None, 'text': found}
            try:
                done['taken'] = rule.load(one['primary'], str(sidecar))
            except (ValueError, OSError, TypeError) as refused:
                own = type(refused) is ValueError
                done['refused'] = str(refused) if own else type(refused).__name__
                done['in_its_own_words'] = own
            played.append(done)
        nothing = [rule.load('theo', ''), rule.instructions(None)]
    finally:
        shutil.rmtree(PLACE, ignore_errors=True)
    return {'what': 'server/lib/oracle_voice_context.py: load', 'place': str(PLACE), 'cases': played,
            'without_a_file': nothing}


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == '--run':
        print(json.dumps(run()))
        return 0
    ran = subprocess.run([sys.executable, '-I', __file__, '--run'], capture_output=True, text=True,
                         env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}, check=False)
    if ran.returncode != 0:
        print(ran.stderr[-3000:], file=sys.stderr)
        return 1
    said = json.loads(ran.stdout.splitlines()[-1])
    lines = ['{']
    for name in ('what', 'place', 'without_a_file'):
        lines.append(f' {json.dumps(name)}: {json.dumps(said[name], ensure_ascii=True)},')
    lines.append(' "cases": [')
    lines.extend('  ' + json.dumps(one, ensure_ascii=True) + (',' if n < len(said['cases']) - 1 else '')
                 for n, one in enumerate(said['cases']))
    lines.append(' ]')
    lines.append('}')
    OUT.write_text('\n'.join(lines) + '\n', encoding='ascii')
    cases = said['cases']
    print(f'{OUT.relative_to(REPO)}: {len(cases)} cases, {sum(1 for one in cases if "taken" in one)} taken, '
          f'{sum(1 for one in cases if one.get("in_its_own_words"))} refused in the rule\'s words, '
          f'{sum(1 for one in cases if "refused" in one and not one["in_its_own_words"])} in the language\'s, '
          f'{OUT.stat().st_size} bytes')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
