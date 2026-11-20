#!/usr/bin/env python3
"""Writes down what the Python Host keeps of a failed routing CLI's words (its PR 445, R85).

`server/lib/model_fallbacks.py`: echoes (whether a text shares a run of 24 characters with the prompt, escapes undone,
NFKC on both sides), _kept_line (the words as one line of at most 300 characters, or nothing when they echo), cli_failure
(the line, the category and the refusal, read from the raw words first; the backend and the exit status); and
goal_keeper.error_code over such failures. Also the code points the reference's unicodedata (Unicode 15.1.0) leaves
unassigned, which its NFKC leaves as written.

    <the Python Host's interpreter> server-rs/tools/routing-failure-vectors.py

It runs in a process of its own whose every home, data, config and cache directory is a new empty directory (the
replay harness's own isolation), with no way out of the machine. The same file at every run. Nothing is logged.
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
OUT = REPO / 'server-rs' / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'routing-failure-vectors.json'
TABLE = REPO / 'server-rs' / 'crates' / 'clarp-kernel' / 'src' / 'unassigned-15.1.json'

EN = 'Route this: please book a table for four at the harbour restaurant tonight, thank you.'
NO = 'Bestill et bord for fire på havnerestauranten i kveld, takk skal du ha. Ærlig talt, øyeblikkelig!'
FI = 'Find the file in the office and fix the final figures before Friday afternoon.'
EMOJI = 'Send the 🎉 party invitation to everyone on the guest list before noon tomorrow.'
SHORT = 'hi there'
OUTLINED = 'A' * 30


def fullwidth(text):
    return ''.join(chr(ord(c) + 0xFEE0) if '!' <= c <= '~' else ('　' if c == ' ' else c) for c in text)


def texts_for(prompt):
    import unicodedata
    yield 'nothing of it', 'rate limit reached for this account'
    for n in (23, 24, 25):
        yield f'a run of {n}', 'error: ' + prompt[5:5 + n] + ' :end'
    yield 'the whole prompt', prompt
    yield 'spaced as lines', prompt[:40].replace(' ', '\n\t  ')
    yield 'control characters', prompt[:40].replace(' ', '\x00\x1f\x7f')
    yield 'as JSON', json.dumps(prompt)
    yield 'as JSON, ASCII', json.dumps(prompt, ensure_ascii=True)
    yield 'as JSON, not ASCII', json.dumps(prompt, ensure_ascii=False)
    yield 'as a repr', repr(prompt)
    yield 'as ascii()', ascii(prompt)
    yield 'as unicode_escape', prompt.encode('unicode_escape').decode('ascii')
    yield 'as a bytes repr', str(prompt.encode())
    yield 'full-width', fullwidth(prompt)
    yield 'decomposed', unicodedata.normalize('NFD', prompt)
    yield 'with ligatures', prompt.replace('fi', 'ﬁ').replace('ff', 'ﬀ')
    yield 'prompt with ligatures, words plain', prompt


@tool_scratch.case('clarp-routing-failure-vectors-')
def run(root) -> dict:
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    import unicodedata
    from lib import goal_keeper, model_fallbacks as mf

    echoes = []
    prompts = {'en': EN, 'no': NO, 'fi': FI, 'emoji': EMOJI, 'short': SHORT, 'empty': ''}
    for name, prompt in prompts.items():
        for what, text in texts_for(prompt or EN):
            if what.startswith('prompt with ligatures'):
                prompt_as = prompt.replace('fi', 'ﬁ') if prompt else prompt
            else:
                prompt_as = prompt
            echoes.append({'what': f'{name}: {what}', 'text': text, 'prompt': prompt_as,
                           'echoes': mf.echoes(text, prompt_as)})
    # Escapes at their edges, one by one.
    edge = [
        ('\\U0001F389 above nothing', '\\U0001F389 Send the', EMOJI),
        ('\\U00110000 stays as written', 'x \\U00110000 ' + EN[:30], EN),
        ('\\U0010FFFF is a character', '\\U0010FFFF' + EN[:30], EN),
        ('\\xNN as a code point', 'P\\xe5 havnerestauranten i kveld, takk skal', 'På havnerestauranten i kveld, takk skal du ha'),
        ('\\xNN as UTF-8 bytes', "b'p\\xc3\\xa5 havnerestauranten i kveld, takk skal'", 'på havnerestauranten i kveld, takk skal du ha'),
        ('\\xNN as UTF-8 bytes, the run across them', "b'\\xc3\\xa5 havnerestauranten i kv'", 'på havnerestauranten i kveld, takk skal du ha'),
        ('invalid UTF-8 bytes', "b'\\xff\\xfe havnerestauranten i kveld, takk skal'", 'på havnerestauranten i kveld, takk skal du ha'),
        ('a lone high surrogate', '\\ud83d' + EN[:30], EN),
        ('a surrogate pair split', json.dumps(EMOJI), EMOJI),
        ('one-letter escapes', EN[:12] + '\\n' + EN[12:40].replace(' ', '\\t'), EN),
        ('quote escapes', '\\"' + EN[:30] + '\\\'' + '\\/', EN),
        ('a backslash escaped', EN[:20] + '\\\\' + EN[20:40], EN),
        ('outlined capitals of Unicode 16', '\U0001CCD6' * 30, OUTLINED),
        ('a prompt shorter than the run, inside', 'well hi there friend', SHORT),
        ('a prompt shorter than the run, not inside', 'well hi the re friend', SHORT),
        ('nothing to compare', 'anything at all', ''),
        ('white space only prompt', 'anything', '   \n\t '),
    ]
    for what, text, prompt in edge:
        echoes.append({'what': what, 'text': text, 'prompt': prompt, 'echoes': mf.echoes(text, prompt)})
    kept = []
    for what, words, prompt in [
        ('own words', 'rate limit reached', EN),
        ('own words on lines', '  error:\n\tunknown model\r\n  gpt-9  ', EN),
        ('long words', 'x' * 280 + ' unknown model ' + 'y' * 60, EN),
        ('an echo', 'Error: ' + EN, EN),
        ('an echo past the cut', 'z' * 299 + EN, EN),
        ('an echo just inside the cut', 'z' * 270 + EN, EN),
        ('nothing', '', EN),
        ('blanks', ' \n\t ', EN),
    ]:
        kept.append({'what': what, 'words': words, 'prompt': prompt, 'line': mf._kept_line(words, prompt)})
    failures = []
    outputs = [
        ('nothing', ''), ('blanks', ' \n '), ('own words', 'rate limit reached'),
        ('a refusal', 'Error: permission denied for this tool'),
        ('a refusal that echoes', 'permission denied: ' + EN),
        ('an echo', 'error processing: ' + EN),
        ('a usage limit', "You've hit your usage limit. Upgrade to Pro"),
        ('a connection', 'stream error: connection reset by peer'),
        ('an interruption', 'Request interrupted by user'),
        ('an interruption past the cut', 'x' * 300 + ' deliberately interrupted'),
        ('a plan', 'model is not supported when using Codex with a ChatGPT account'),
        ('a timeout', 'request timed out after 30s'),
        ('anything', 'something unexpected happened'),
    ]
    for backend in ('codex', 'claude', 'agy'):
        for what, output in outputs:
            for code in (1, 2, 137, -9, None):
                failure = mf.cli_failure(backend, output, prompt=EN, returncode=code)
                row = {'backend': backend, 'what': what, 'output': output, 'returncode': code,
                       'kind': type(failure).__name__, 'line': str(failure),
                       'category': getattr(failure, 'category', None),
                       'denied': getattr(failure, 'denied', None),
                       'provider_failure': mf._as_provider_failure(failure) is not None}
                if isinstance(failure, mf.ProviderFailure):
                    row['error_code'] = goal_keeper.error_code(failure)
                failures.append(row)
    codes = []
    for what, failure in [
        ('a plain provider failure', mf.ProviderFailure('boom')),
        ('a timeout', mf.ProviderFailure('timed out', 'timeout')),
        ('agy in its words', mf.ProviderFailure('agy exited 3: something')),
        ('agy negative', mf.ProviderFailure('agy exited -9')),
    ]:
        codes.append({'what': what, 'line': str(failure), 'category': failure.category,
                      'error_code': goal_keeper.error_code(failure)})
    unassigned, start = [], None
    for cp in range(0x110001):
        cn = cp <= 0x10FFFF and unicodedata.category(chr(cp)) == 'Cn'
        if cn and start is None:
            start = cp
        elif not cn and start is not None:
            unassigned.append([start, cp - 1])
            start = None
    return {'what': 'server/lib/model_fallbacks.py echoes, _kept_line, cli_failure, _as_provider_failure; '
                    'goal_keeper.error_code', 'unicode': unicodedata.unidata_version,
            'echo_run': mf.ECHO_RUN, 'kept_most': mf.KEPT_MOST,
            'echoes': echoes, 'kept': kept, 'failures': failures, 'codes': codes, 'unassigned': unassigned}


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == '--run':
        sys.stdout.write(json.dumps(run()) + '\n')
        return 0
    ran = subprocess.run([sys.executable, '-I', __file__, '--run'], capture_output=True, text=True,
                         env=tool_scratch.child_env({'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}), check=False)
    if ran.returncode != 0:
        print(ran.stderr[-3000:], file=sys.stderr)
        return 1
    said = json.loads(ran.stdout.splitlines()[-1])
    table = said.pop('unassigned')
    TABLE.write_text(json.dumps({'unicode': said['unicode'], 'unassigned': table}, separators=(',', ':')) + '\n',
                     encoding='ascii')
    lines = ['{']
    for key in ('what', 'unicode', 'echo_run', 'kept_most'):
        lines.append(f' {json.dumps(key)}: {json.dumps(said[key])},')
    parts = ('echoes', 'kept', 'failures', 'codes')
    for part in parts:
        rows = said[part]
        lines.append(f' "{part}": [')
        lines.extend('  ' + json.dumps(one, ensure_ascii=True) + (',' if n < len(rows) - 1 else '')
                     for n, one in enumerate(rows))
        lines.append(' ],' if part != parts[-1] else ' ]')
    lines.append('}')
    OUT.write_text('\n'.join(lines) + '\n', encoding='ascii')
    print(f'{OUT.relative_to(REPO)}: {len(said["echoes"])} echoes, {len(said["kept"])} kept, '
          f'{len(said["failures"])} failures, {len(said["codes"])} codes; {len(table)} unassigned ranges')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
