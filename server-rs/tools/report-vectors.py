#!/usr/bin/env python3
"""Writes the vectors the Rust Host's reading of an HTML report is tested against.

An agent hands its user a report as a page of HTML: an artifact of the kind `html_form` that
takes no answers (`read_only`). The page runs in a sandbox, and what it may connect to is
what the artifact says and nothing else. The Python Host's rules for that are the functions of
`server/lib/html_forms.py` that take a payload and answer: what origins a page may reach
(`connect_origins`), the policy a browser is given (`content_security_policy`), the page as
it is served (`render_html`), a payload with what a report may leave out filled in
(`normalize`), and whether a payload is a form or report at all (`validate_contract`). This
calls them for made-up payloads and writes what they said to
`crates/clarp-kernel/tests/fixtures/report-vectors.json`.

Run it with the Python the Host runs (`validate_contract` asks the `jsonschema` package of
the Host's own environment whether an answer schema is one):

    "$(sed -n 1p ~/.local/share/clarp/current/SERVICE_PYTHON)" server-rs/tools/report-vectors.py

Nothing of a Host is touched or started: functions that take a payload are called.
"""
from __future__ import annotations

import copy
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = HERE.parent / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'report-vectors.json'

ORIGINS = [
    [], ['https://example.org'], ['https://example.org:443'], ['https://example.org:8443'], ['https://EXAMPLE.org'],
    ['https://example.org', 'https://example.org:443', 'https://Example.ORG'], ['https://a.example.org', 'https://b.example.org'],
    ['https://example.org/'], ['https://example.org/path'], ['https://user@example.org'], ['https://user:secret@example.org'],
    ['http://example.org'], ['example.org'], ['https://'], ['https://example.org:'], ['https://example.org:0'],
    ['https://example.org:65535'], ['https://example.org:65536'], ['https://example.org:99999'], ['https://example.org:123456'],
    ['https://example.org:08443'], ['https://-example.org'], ['https://example-.org'], ['https://exa_mple.org'], ['https://example..org'],
    ['https://.example.org'], ['https://example.org.'], ['https://' + 'a' * 63 + '.org'], ['https://' + 'a' * 64 + '.org'],
    ['https://localhost'], ['https://1.2.3.4'], ['https://1.2.3.4:8443'], ['https://1.2.3'], ['https://1.2.3.4.5'], ['https://256.1.1.1'],
    ['https://01.2.3.4'], ['https://1.2.3.04'], ['https://0x7f.0.0.1'], ['https://127.0.0.0x1'], ['https://2130706433'],
    ['https://example.123'], ['https://123.example'], ['https://example.0x1f'], ['https://xn--bcher-kva.example'],
    ['https://bücher.example'], ['https://example.org '], [' https://example.org'], ['https://example.org\n'], ['https://[::1]'],
    ['https://example.org?x=1'], ['https://example.org#top'], ['https://*.example.org'], ["https://example.org 'unsafe-eval'"],
    ['https://example.org; script-src *'], ['https://' + 'a.' * 120 + 'org'], ['https://' + 'a.' * 130 + 'org'],
    ['https://a' * 9][0:1] * 9, ['https://o%d.example.org' % n for n in range(8)], ['https://o%d.example.org' % n for n in range(9)],
    [7], [None], ['https://example.org', 7], 'https://example.org', None, {'0': 'https://example.org'}, 7, True,
    ['https://example.org:443', 'https://example.org:444', 'https://example.org'],
]

PAGE = '<h1>Synthetic report</h1><p>All of it.</p>'
SCHEMA = {'type': 'object', 'properties': {}, 'additionalProperties': False}
A_FORM = {'type': 'object', 'properties': {'choice': {'type': 'string', 'enum': ['a', 'b']}}, 'required': ['choice']}


def report(**more):
    return {'content': PAGE, 'version': 'v1', 'read_only': True, **more}


def deep(levels: int):
    schema = {'type': 'object'}
    for _ in range(levels):
        schema = {'type': 'object', 'properties': {'inner': schema}}
    return schema


PAYLOADS = [
    report(), report(answer_schema=dict(SCHEMA)), report(connect_origins=['https://example.org']),
    report(connect_origins=['https://EXAMPLE.org:443', 'https://example.org']), report(connect_origins=['http://example.org']),
    report(connect_origins='https://example.org'), report(connect_origins=[]), report(read_only='yes'), report(read_only=1),
    report(answer_schema=dict(A_FORM)), report(answer_schema={}), report(answer_schema={'type': 'object'}),
    report(answer_schema={'additionalProperties': False, 'properties': {}, 'type': 'object'}),
    report(answer_schema={**SCHEMA, 'title': 'Nothing'}), report(answer_schema=None), report(answer_schema='none'),
    report(content=''), report(content='   \n'), report(content=7), report(content=None), {'version': 'v1', 'read_only': True},
    report(version=''), report(version='v 1'), report(version='x' * 80), report(version='x' * 81), report(version=7),
    report(version='2026.11.15-rc_1'), report(version='v1/2'), report(version='é'), {'content': PAGE, 'read_only': True},
    report(content='<script>document.title = "x"</script><p>with a script</p>'),
    report(content='<p>æøå 日本 "quoted" &amp; <b>bold</b></p>'), report(title='ignored', extra={'kept': [1, 2]}),
    # What takes answers.
    {'content': PAGE, 'version': 'v1', 'answer_schema': dict(A_FORM)},
    {'content': PAGE, 'version': 'v1', 'read_only': False, 'answer_schema': dict(A_FORM)},
    {'content': PAGE, 'version': 'v1'}, {'content': PAGE, 'version': 'v1', 'answer_schema': {'type': 'array'}},
    {'content': PAGE, 'version': 'v1', 'answer_schema': ['object']}, {'content': PAGE, 'version': 'v1', 'answer_schema': dict(SCHEMA)},
    {'content': PAGE, 'version': 'v1', 'answer_schema': {'type': 'object', 'properties': {'a': {'$ref': '#/x'}}}},
    {'content': PAGE, 'version': 'v1', 'answer_schema': {'type': 'object', 'allOf': [{'$dynamicRef': '#x'}]}},
    {'content': PAGE, 'version': 'v1', 'answer_schema': {'type': 'object', 'properties': {'a': {'type': 'nothing'}}}},
    {'content': PAGE, 'version': 'v1', 'answer_schema': deep(28)}, {'content': PAGE, 'version': 'v1', 'answer_schema': deep(40)},
    {'content': PAGE, 'version': 'v1', 'read_only': None, 'answer_schema': dict(A_FORM)},
    {'content': '', 'version': '', 'read_only': 'x', 'answer_schema': 7, 'connect_origins': 7},
    {}, {'read_only': True},
]

NOT_PAYLOADS = [None, 7, 'text', [], [report()]]


def said(function, *args):
    """What a function answers, or the words it refuses with."""
    try:
        return {'result': function(*copy.deepcopy(args))}
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        return {'error': {'kind': type(error).__name__, 'message': str(error)}}


def main() -> int:
    sys.path.insert(0, str(REPO / 'server'))
    from lib import html_forms as forms

    def rendered(payload):
        page, policy = forms.render_html(payload)
        return {'page': page.decode(), 'policy': policy}

    def contract(payload):
        forms.validate_contract(payload)
        return 'ok'

    origins = [{'given': given, **said(forms.connect_origins, given)} for given in ORIGINS]
    payloads = []
    for payload in PAYLOADS:
        normal = said(forms.normalize, payload)
        entry = {'payload': payload, 'normalize': normal, 'policy': said(forms.content_security_policy, payload)}
        if isinstance(payload.get('content'), str):
            entry['rendered'] = said(rendered, payload)
        # A payload is held to the contract as the Host holds it: after what may be left out
        # was filled in.
        entry['contract'] = said(contract, normal['result']) if 'result' in normal else {'error': normal['error']}
        payloads.append(entry)
    others = [{'payload': payload, 'normalize': said(forms.normalize, payload)} for payload in NOT_PAYLOADS]
    vectors = {'about': 'Written by server-rs/tools/report-vectors.py from server/lib/html_forms.py. Do not edit by hand.',
               'python': '.'.join(str(part) for part in sys.version_info[:3]),
               'read_only_schema': forms.READ_ONLY_SCHEMA, 'origins': origins, 'payloads': payloads, 'others': others}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(vectors, ensure_ascii=False, indent=1) + '\n')
    refused = sum(1 for vector in origins if 'error' in vector)
    contracts = sum(1 for vector in payloads if 'error' in vector['contract'])
    print(f'{len(origins)} lists of origins ({refused} refused), {len(payloads)} payloads ({contracts} no contract), '
          f'{len(others)} that are no payload -> {OUT.relative_to(REPO)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
