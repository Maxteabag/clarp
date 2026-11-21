#!/usr/bin/env python3
"""Writes down what the Python Host makes of a tool's sign-in, as an app is told it.

A sign-in from an app (`POST /backend-auth/login`) runs the tool's own sign-in on the Host and shows the app what it
prints: cleaned of a terminal's signs and of anything that looks like a key, with the address to open, the code to
type and whether the tool waits for a code pasted back (`POST /backend-auth/login-code`). The rules are
`server/lib/backend_auth.py`'s own (`_clean_output`, `_failure_summary`, `_append_output`, `_task_value`,
`_driver`, `submit_login_code`), run here as they are on texts of this program's own; no tool is started.

    <the Python Host's interpreter> server-rs/tools/backend-auth-vectors.py

It runs in a process of its own whose every home, data, config and cache directory is a new empty directory (the
replay harness's own isolation), with no way out of the machine. The same file at every run. Nothing is logged.
"""
from __future__ import annotations

import itertools
import json
import pathlib
import runpy
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import scratch as tool_scratch

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[1]
OUT = HERE.parent / 'crates/clarp-kernel/tests/fixtures/backend-auth-vectors.json'
NOW = 1_790_000_000_000
ESC = '\x1b'

# What the tools print, made up here: Claude Code's browser sign-in with its prompt for the code, a device sign-in
# with its address and code in colour, and failures.
CLAUDE = ('Opening browser to sign in' + chr(0x2026) + '\n'
          "If the browser didn't open, visit: https://claude.example/oauth/authorize?code=true&state=s1.\n\n"
          'Paste code here if prompted > ')
DEVICE = ('Follow these steps to sign in with ChatGPT using device code authorization:\n\n'
          '1. Open this link in your browser and sign in to your account\n'
          f'   {ESC}[94mhttps://auth.example/codex/device{ESC}[0m\n\n'
          f'2. Enter this one-time code {ESC}[2m(expires in 15 minutes){ESC}[0m\n'
          f'   {ESC}[94mABCD-EFGH1{ESC}[0m\n')

TEXTS = [
    '', ' ', '\n\n', 'plain', '  padded both  \n', 'Signed in.\n', CLAUDE, DEVICE, CLAUDE + 'abc\n',
    CLAUDE + 'abc\nOAuth error: invalid code', CLAUDE + 'abc\nPaste code here if prompted > Error: bad code\n',
    'Paste code here if prompted > ', 'paste the CODE >  \t', 'Enter your code>', 'ENTER code > x',
    'please paste code > ', 'pa' + chr(0x17f) + 'te code >', 'enter code', 'enter > code', 'enter code >\nmore',
    'enter code >> and > ', 'Enter code: > > ', 'code > enter', 'say: enter code >',
    # A terminal's signs: colours, titles closed both ways and left open, a lone escape, controls.
    f'{ESC}[1;32mgreen{ESC}[0m', f'{ESC}]0;title{chr(7)}after', f'{ESC}]8;;https://login.example/x{ESC}\\link{ESC}]8;;{ESC}\\',
    f'before{ESC}]open title and the rest', f'a{ESC}[', f'a{ESC}[?25l b{ESC}[2K', f'{ESC}(Bx', f'{ESC}Mx', f'{ESC}x',
    f'{ESC}', f'a{ESC}[12', f'a{ESC}[1;2 q', 'a\rb\x00c\x07d\x7fe\x1cf\tg', 'bell\x07', f'{ESC}]{ESC}]x{chr(7)}y',
    f'{ESC}]a{ESC}]b{chr(7)}c', f'{ESC}]x\ny{chr(7)}z', f'title{ESC}]open\nnext line',
    # What looks like a key, and what does not quite.
    'key sk-abcdefghijkl end', 'key sk-abcdefghijk end', 'SK-ABCDEFGHIJKLMNOP', 'sk_abcdefghijklmnop',
    'Bearer abcdefghijkl', 'bearer\tabc.def-ghi_jk', 'BEARER  abcdefghijk', 'Bearer\nabcdefghijkl',
    'Bearerabcdefghijkl', 'auth: Bearer sk-abcdefghijklmn', 'sk-' + 'a' * 11 + '.' + 'b' * 12,
    chr(0x17f) + 'k-abcdefghijkl', 's' + chr(0x212a) + '-abcdefghijkl', 'sk-abc' + chr(0x130) + chr(0x131) + 'defghijk',
    'sk-abcdefghijk' + chr(0xe9), 'bearer' + chr(0xa0) + 'abcdefghijklm', 'bearer' + chr(0x2003) + 'abcdefghijklm',
    'sk-abcdefghijkl-sk-mnopqrstuvwx', 'x' * 30 + 'sk-' + 'q' * 40,
    # Addresses: only one about signing in is shown, without the stops after it.
    'visit https://example.com/home then https://example.com/login?x=1).',
    'https://auth.example/a,.)', 'https://example.com/AUTH', 'http://login.example/x', 'https://login',
    'https://...', 'https://x' + ESC + 'login', 'https://a.example/oauth\nhttps://b.example/login',
    'https://a' + chr(0x2028) + 'login', 'go to https://auth.example/' + chr(0xe9) + ' now',
    # Device codes, where they begin and end.
    'ABCD-EFGHI', 'code ABCD-EFGHI.', 'XABCD-EFGHI', 'ABCD-EFGHIJ', 'abcd-efghi', 'AB1D-12345', 'ABCD-EFGH',
    'ABC-DEFGHI', 'aABCD-EFGHIb', '-ABCD-EFGHI-', 'ABCDE-FGHIJ ABCD-EFGHI', 'ABCD-EFGHI ZZZZ-YYYYY',
    'A' + chr(0xc9) + 'CD-EFGHI', chr(0xc9) + 'ABCD-EFGHI', 'ABCDXEFGHI code',
    # Lines and their ends, as Python splits them.
    'first\nlast', 'first\nlast\n\n  \n', 'one' + chr(0x85) + 'two', 'one' + chr(0x2028) + 'two', 'a\r\nb',
    'one two', 'one\x0btwo\x0cthree', '\n  \t\n', 'only' + chr(0x3000),
    # Long texts: what is kept of them, at the bounds from both sides.
    'x' * 7999, 'y' * 8000, 'z' * 8001, 'w' * 9000 + '\n' + 'v' * 10, '  ' + 'u' * 7999 + '  ',
    'e' * 499, 'f' * 500, 'g' * 501, 'h' * 700 + '\n', 'Paste code here > ' + 'i' * 600,
    'n' * 7990 + 'sk-' + 'k' * 20, 'sk-' + 'k' * 20 + 'm' * 7990, chr(0x1f600) * 8001,
]


def vectors():
    harness = runpy.run_path(str(ROOT / 'tests/replay/host.py'))
    with tool_scratch.directory() as directory:
        harness['isolate'](pathlib.Path(directory))
        harness['loopback_only']()
        sys.path.insert(0, str(ROOT / 'server'))
        from lib import backend_auth as auth
        from lib import backends
        auth._now_ms = lambda: NOW

        texts = []
        for raw in TEXTS:
            texts.append(dict(raw=raw, clean=auth._clean_output(raw), summary=auth._failure_summary(raw)))

        appended = []
        for before, more in [('', 'a'), ('a' * 65535, 'b'), ('a' * 65536, 'b'), ('c' * 70000, 'd'),
                             ('x' * 65530, 'yyyyyyyyyy'), (chr(0xe9) * 65536, chr(0x1f600))]:
            out = before
            for character in more:
                out = auth._append_output(out, character)
            appended.append(dict(before=len(before), first=before[:1], more=more, kept=len(out),
                                 head=out[:3], tail=out[-3:]))

        tasks = []
        samples = [raw for raw in TEXTS if len(raw) < 400]
        for raw, state in itertools.product(samples, ('idle', 'running', 'complete', 'failed')):
            clean = auth._clean_output(raw)
            variants = [(0, '', ''), (NOW - 5000, 'it broke', clean), (7, '', clean + 'x')]
            # Whether the code was answered is asked of a running sign-in alone.
            for started, error, submitted in variants if state == 'running' else variants[:2]:
                tasks.append(dict(backend='codex', state=state, raw=raw, started_at=started, error=error,
                                  submitted=submitted,
                                  task=auth._task_value('codex', state, raw, started_at=started or None,
                                                        error=error, submitted_output=submitted)))

        names = []
        for name in ('claude', 'codex', 'grok', 'agy', 'antigravity', 'opencode', 'open-code', 'deepseek', 'muse',
                     'muse-code', 'scripted', '', 'Claude', 'claude ', 'gemini'):
            driver = auth._driver(name)
            names.append(dict(name=name, driver=driver is not None,
                              login=list(driver.login_argv) if driver else [],
                              logout=list(driver.logout_argv) if driver else [],
                              status=list(driver.status_argv) if driver else [],
                              listed=driver.listed_by_default if driver else False))
        order = [dict(id=a.id, label=a.label, login_kind=a.login_kind, supports_auth=a.supports_auth)
                 for a in backends.all_adapters()]

        codes = _codes(auth)
        return dict(now=NOW, limits=dict(timeout_seconds=auth._LOGIN_TIMEOUT_SECONDS,
                                         raw_output=auth._RAW_OUTPUT_LIMIT, code=auth._MAX_CODE_LENGTH),
                    texts=texts, appended=appended, tasks=tasks, names=names, order=order, codes=codes)


class _Stdin:
    def __init__(self, closed=False, fails=False):
        self.closed, self.fails, self.written = closed, fails, ''

    def write(self, text):
        if self.fails:
            raise OSError('the pipe is gone')
        self.written += text

    def flush(self):
        return None


class _Process:
    def __init__(self, stdin):
        self.stdin = stdin


def _codes(auth):
    """`submit_login_code` for every way a code can come and every state a sign-in can be in."""
    rows = []
    prompt = 'Paste code here if prompted > '
    states = [
        ('none', None, None),
        ('running-asking', 'running', prompt),
        ('running-not-asking', 'running', 'Opening browser'),
        ('complete-asking', 'complete', prompt),
        ('failed', 'failed', 'Error'),
    ]
    stdins = [('open', lambda: _Stdin()), ('no-process', None), ('no-stdin', lambda: None),
              ('closed', lambda: _Stdin(closed=True)), ('fails', lambda: _Stdin(fails=True))]
    codes = ['abc#def', '  spaced  ', '', '   ', 'a' * 512, 'a' * 513, ' ' + 'a' * 512 + ' ', 'a\nb', 'a\rb',
             'trailing\n', chr(0xe9) * 512, chr(0xe9) * 513, '\tcode\t']
    for backend in ('claude', 'codex', 'grok', 'agy', 'scripted'):
        # The code is looked at before the sign-in: every code against one waiting sign-in, and one code
        # against every state.
        cases = list(itertools.product(states, stdins, ['abc']))
        if backend == 'claude':
            cases += [(states[1], stdins[0], code) for code in codes]
        for (name, state, output), (stdin_name, stdin), code in cases:
            auth._tasks.clear()
            auth._processes.clear()
            auth._code_submitted.clear()
            made = None
            if state is not None:
                auth._tasks[backend] = auth._task_value(backend, state, output, started_at=NOW - 1000)
            if stdin is not None:
                made = stdin()
                auth._processes[backend] = _Process(made)
            row = dict(backend=backend, state=name, stdin=stdin_name, code=code)
            try:
                task = auth.submit_login_code(backend, code)
                row.update(outcome='sent', written=made.written, task=task)
            except ValueError as error:
                row.update(outcome='refused', error=str(error))
            except RuntimeError as error:
                row.update(outcome='conflict', error=str(error))
            rows.append(row)
    auth._tasks.clear()
    auth._processes.clear()
    auth._code_submitted.clear()
    return rows


def main():
    text = json.dumps(vectors(), indent=1, ensure_ascii=True, sort_keys=True) + '\n'
    OUT.write_text(text)


if __name__ == '__main__':
    main()
