"""Compare two normalized captures and report the differences safely.

A difference names its scenario, channel (http, sse, schema, table, files,
cli), a locator and a kind. The report never prints message text: a value is
shown only when its key is a code key (type, kind, status, ...) and it looks
like a code; anything else is shown as its type, length and a short hash.
Every difference gets a stable id so an intended difference can be
registered once and matched on later runs.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re

CODE_KEYS = {'type', 'kind', 'status', 'op', 'outcome', 'state', 'method', 'path', 'phase', 'reason',
             'code', 'role', 'source', 'origin', 'action', 'event', 'field', 'name', 'backend',
             'step', 'queued', 'busy', 'error_code', 'mode', 'tbl_name', 'session'}
CODE_VALUE = re.compile(r'^[\w./:<>?=&-]{1,48}$')


def safe(value, key: str = '') -> str:
    if value is None or isinstance(value, bool) or isinstance(value, (int, float)):
        return json.dumps(value)
    if isinstance(value, str) and (key in CODE_KEYS or value.startswith('<')) and CODE_VALUE.match(value):
        return value
    blob = json.dumps(value, sort_keys=True)
    kind = type(value).__name__
    size = len(value) if isinstance(value, (str, list, dict)) else len(blob)
    return f'{kind}[{size}]#{hashlib.sha1(blob.encode()).hexdigest()[:8]}'


class Report:
    def __init__(self):
        self.items: list[dict] = []

    def add(self, scenario, channel, locator, kind, a=None, b=None, key=''):
        ident = hashlib.sha1(f'{scenario}|{channel}|{locator}|{kind}'.encode()).hexdigest()[:10]
        self.items.append({'id': ident, 'scenario': scenario, 'channel': channel, 'locator': locator,
                           'kind': kind, 'base': safe(a, key) if a is not None or kind == 'changed' else None,
                           'head': safe(b, key) if b is not None or kind == 'changed' else None})


def _deep(report, scenario, channel, locator, a, b, key=''):
    if type(a) is not type(b):
        report.add(scenario, channel, locator, 'changed', a, b, key)
    elif isinstance(a, dict):
        for k in sorted(set(a) | set(b)):
            loc = f'{locator}.{k}'
            if k not in b:
                report.add(scenario, channel, loc, 'field-removed', a[k], None, k)
            elif k not in a:
                report.add(scenario, channel, loc, 'field-added', None, b[k], k)
            else:
                _deep(report, scenario, channel, loc, a[k], b[k], k)
    elif isinstance(a, list):
        if len(a) != len(b):
            report.add(scenario, channel, f'{locator}#len', 'changed', len(a), len(b))
        for i, (x, y) in enumerate(zip(a, b)):
            _deep(report, scenario, channel, f'{locator}[{i}]', x, y, key)
    elif a != b:
        report.add(scenario, channel, locator, 'changed', a, b, key)


def _signature(event) -> str:
    if not isinstance(event, dict):
        return safe(event)
    parts = [str(event.get('type', '?'))]
    for k in ('kind', 'status', 'state', 'phase', 'action'):
        if isinstance(event.get(k), str):
            parts.append(f'{k}={event[k]}')
    if isinstance(event.get('ops'), list):
        parts.append('ops=' + ','.join(str(o.get('op')) for o in event['ops'] if isinstance(o, dict)))
    return ' '.join(parts)


def _sequence(report, scenario, channel, a, b):
    """Align two event sequences by signature; an insertion, removal or reorder
    shows as a sequence difference, a matched pair is compared field by field."""
    sa, sb = [_signature(e) for e in a], [_signature(e) for e in b]
    matcher = difflib.SequenceMatcher(a=sa, b=sb, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == 'equal':
            for i, j in zip(range(i1, i2), range(j1, j2)):
                _deep(report, scenario, channel, f'[{i}:{sa[i].split()[0]}]', a[i], b[j])
        else:
            report.add(scenario, channel, f'[{i1}:{i2}]->[{j1}:{j2}]', f'sequence-{tag}',
                       ' | '.join(sa[i1:i2]) or None, ' | '.join(sb[j1:j2]) or None, 'type')


def _rows(report, scenario, table, a, b):
    ka = [json.dumps(r, sort_keys=True) for r in a]
    kb = [json.dumps(r, sort_keys=True) for r in b]
    only_a = [r for r, k in zip(a, ka) if k not in kb or ka.count(k) > kb.count(k)]
    only_b = [r for r, k in zip(b, kb) if k not in ka or kb.count(k) > ka.count(k)]
    if len(a) != len(b):
        report.add(scenario, 'table', f'{table}#rows', 'changed', len(a), len(b))
    # Pair leftover rows by their first column (usually the primary key).
    def first(r):
        return json.dumps(next(iter(r.values()), None)) if r else ''
    by_key = {first(r): r for r in only_b}
    for r in only_a:
        other = by_key.pop(first(r), None)
        if other is None:
            report.add(scenario, 'table', f'{table}[{safe(first(r))}]', 'row-removed', r, None)
        else:
            _deep(report, scenario, 'table', f'{table}[{safe(first(r))}]', r, other)
    for k, r in by_key.items():
        report.add(scenario, 'table', f'{table}[{safe(k)}]', 'row-added', None, r)


def compare(a: dict, b: dict, report: Report):
    s = a['scenario']
    if a['error'] or b['error']:
        if bool(a['error']) != bool(b['error']) or a['error'] != b['error']:
            report.add(s, 'run', 'error', 'changed', a['error'] and 'failed', b['error'] and 'failed')
    _sequence(report, s, 'http', a['http'], b['http'])
    _sequence(report, s, 'sse', a['sse'], b['sse'])
    _deep(report, s, 'schema', 'sqlite_master', a['schema'], b['schema'])
    for table in sorted(set(a['tables']) | set(b['tables'])):
        _rows(report, s, table, a['tables'].get(table, []), b['tables'].get(table, []))
    _deep(report, s, 'files', 'files', a['files'], b['files'])
    _deep(report, s, 'cli', 'cli', a['cli'], b['cli'])


def render(report: Report, intended: set[str], title: str) -> str:
    lines = [f'# Replay difference report: {title}', '']
    unexplained = [d for d in report.items if d['id'] not in intended]
    by_scenario: dict[str, int] = {}
    for d in report.items:
        by_scenario[d['scenario']] = by_scenario.get(d['scenario'], 0) + 1
    lines.append(f'differences: {len(report.items)}  registered intended: '
                 f'{len(report.items) - len(unexplained)}  unexplained: {len(unexplained)}')
    for scen, count in sorted(by_scenario.items()):
        lines.append(f'  {scen}: {count}')
    if report.items:
        lines += ['', 'id          class        scenario / channel / locator / kind: base -> head']
    for d in report.items:
        cls = 'intended' if d['id'] in intended else 'UNEXPLAINED'
        lines.append(f"{d['id']}  {cls:<11}  {d['scenario']} / {d['channel']} / {d['locator']} / "
                     f"{d['kind']}: {d['base']} -> {d['head']}")
    return '\n'.join(lines) + '\n'
