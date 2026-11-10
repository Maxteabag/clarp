"""Compare two normalized captures and report the differences safely.

A difference names its scenario, channel (http, sse, schema, table, files,
cli, run), a locator and a kind. The report never prints message text
(review s0-harness-7daeae37 finding 2):

- A value prints only if it is a number, boolean, null, a surrogate the
  normalizer made (``<id:3>``, ``<time>``), or a code: a string under a code
  key (``CODE_KEYS``) that is also a string literal in the server source the
  harness ships with (the codebook). Text a user or agent wrote is not in the
  source, so it prints as ``str[len]#hash8``.
- A key in a locator prints only if it is in the codebook (field names are
  code literals); any other key, such as a map keyed by user text, prints as
  ``k#hash8``. Event types in sequence signatures follow the value rule.
- Run errors print as the step index and op only.

A difference's id hashes its scenario, channel, locator, kind and the hashes
of both values (finding 5), so a registered intended difference matches only
that exact change; another change at the same place gets a new id.
"""
from __future__ import annotations

from collections import Counter
import difflib
import functools
import hashlib
import json
from pathlib import Path
import re

import normalize

HARNESS = Path(__file__).resolve().parents[2]
CODE_KEYS = frozenset({'type', 'kind', 'status', 'op', 'outcome', 'state', 'method', 'phase', 'role',
                       'action', 'mode', 'event', 'field', 'backend', 'error_code', 'code', 'tbl_name'})
CODE_SHAPE = re.compile(r'^[a-z][a-z0-9_./:-]{0,47}$')
SURROGATE = re.compile(r'^(<id:\d+>|<secret:\d+>|<eid:(\?\d+|(log:)?[a-z][a-z0-9_-]{0,40}#\d+@[0-9a-f]{6})>|<(time|duration|pid|port|json|bytes|sha256)>|<(ROOT|BUILD|HARNESS)>)$')
# Keys the capture itself creates (envelopes, contract header names).
HARNESS_KEYS = frozenset({'step', 'method', 'path', 'status', 'headers', 'body', 'event', 'id', 'data', '<form>', '<keys>',
                          'bytes', 'digest', 'json', 'kind', 'pk', 'rows', '<json>', '<bytes>', '<sha256>',
                          'content-type', 'location', 'content-range', 'accept-ranges', 'content-encoding',
                          'cache-control', 'content-disposition', 'etag', 'www-authenticate', 'retry-after',
                          'type', 'name', 'tbl_name', 'sql'})
LITERAL = re.compile(r'''["']([A-Za-z_][\w./:-]{0,47})["']''')


@functools.lru_cache(maxsize=1)
def codebook() -> frozenset[str]:
    """Every identifier-shaped string literal in the harness tree's server code."""
    words: set[str] = set()
    for path in (HARNESS / 'server').rglob('*.py'):
        words.update(LITERAL.findall(path.read_text(errors='replace')))
    return frozenset(words)


def _hash(value) -> str:
    return hashlib.sha1(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()[:8]


def safe(value, key: str = '') -> str:
    if value is None or isinstance(value, (bool, int, float)):
        return json.dumps(value)
    if isinstance(value, str):
        if SURROGATE.match(value):
            return value
        if key in CODE_KEYS and CODE_SHAPE.match(value) and value in codebook():
            return value
    size = len(value) if isinstance(value, (str, list, dict)) else 0
    return f'{type(value).__name__}[{size}]#{_hash(value)}'


def safe_key(key: str) -> str:
    return key if key in HARNESS_KEYS or key in codebook() or SURROGATE.match(key) else f'k#{_hash(key)}'


class Report:
    def __init__(self):
        self.items: list[dict] = []

    def add(self, scenario, channel, locator, kind, a=None, b=None, key=''):
        ident = hashlib.sha1(f'{scenario}|{channel}|{locator}|{kind}|{_hash(a)}|{_hash(b)}'.encode()).hexdigest()[:12]
        self.items.append({'id': ident, 'scenario': scenario, 'channel': channel, 'locator': locator,
                           'kind': kind, 'base': safe(a, key), 'head': safe(b, key)})


def _deep(report, scenario, channel, locator, a, b, key=''):
    if type(a) is not type(b):
        report.add(scenario, channel, locator, 'changed', a, b, key)
    elif isinstance(a, dict):
        for k in sorted(set(a) | set(b)):
            loc = f'{locator}.{safe_key(k)}'
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


def _signature(item) -> str:
    """A safe, comparable summary of an HTTP exchange or SSE frame."""
    if not isinstance(item, dict):
        return safe(item)
    if 'method' in item:
        return f"{item['method']} {_path_code(item['path'])} {item['status']}"
    data = item.get('data') if isinstance(item.get('data'), dict) else {}
    parts = [safe(item.get('event'), 'event') if item.get('event') else 'message', safe(data.get('type'), 'type')]
    for k in ('kind', 'status', 'state', 'phase', 'action'):
        if isinstance(data.get(k), str):
            parts.append(f'{k}={safe(data[k], k)}')
    if isinstance(data.get('ops'), list):
        parts.append('ops=' + ','.join(safe(o.get('op'), 'op') for o in data['ops'] if isinstance(o, dict)))
    return ' '.join(parts)


def _path_code(path: str) -> str:
    """Request paths are the harness's own scenario text; print the route shape."""
    return '/'.join(p if p in codebook() or not p else f'k#{_hash(p)}' for p in path.split('/'))


def _sequence(report, scenario, channel, a, b, prefix=''):
    """Align two sequences by signature; an insertion, removal or reorder shows
    as a sequence difference, a matched pair is compared field by field."""
    sa, sb = [_signature(e) for e in a], [_signature(e) for e in b]
    matcher = difflib.SequenceMatcher(a=sa, b=sb, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == 'equal':
            for i, j in zip(range(i1, i2), range(j1, j2)):
                _deep(report, scenario, channel, f'{prefix}[{i}]', a[i], b[j])
        else:
            report.add(scenario, channel, f'{prefix}[{i1}:{i2}]->[{j1}:{j2}]', f'sequence-{tag}',
                       {'<signatures>': sa[i1:i2]}, {'<signatures>': sb[j1:j2]})
            # The signatures are already safe; show them so a reorder is readable.
            report.items[-1]['base'] = ' | '.join(sa[i1:i2]) or '-'
            report.items[-1]['head'] = ' | '.join(sb[j1:j2]) or '-'


def _lane_label(name: str) -> str:
    kind, _, rest = name.partition('#')
    return f"{safe(kind, 'type')}@{rest.split('@')[1]}"


def stable_order(runs: list[list[dict]]) -> set[tuple[str, str]]:
    """Pairs (x, y) of lane positions where x came before y in every run."""
    pairs = None
    for frames in runs:
        names = normalize.lane_names(frames)
        here = {(x, y) for i, x in enumerate(names) for y in names[i + 1:]}
        pairs = here if pairs is None else pairs & here
    return pairs or set()


def _stream(report, scenario, channel, a, b, calibration=None):
    """An interleaved event stream: each producer lane is compared as an exact
    sequence; across lanes, only orders every calibration run agreed on count."""
    names_a, names_b = normalize.lane_names(a), normalize.lane_names(b)
    lanes: dict[str, tuple[list, list]] = {}
    for name, frame in zip(names_a, a):
        lanes.setdefault(name.split('@')[1], ([], []))[0].append((name, frame))
    for name, frame in zip(names_b, b):
        lanes.setdefault(name.split('@')[1], ([], []))[1].append((name, frame))
    for lane, (la, lb) in sorted(lanes.items()):
        label = _lane_label((la or lb)[0][0])
        _sequence(report, scenario, channel, [f for _, f in la], [f for _, f in lb], prefix=f'lane[{label}]')
    if not calibration:
        return
    position = {name: i for i, name in enumerate(names_b)}
    moved: dict[str, list[str]] = {}
    for x, y in stable_order(calibration):
        if x in position and y in position and position[y] < position[x]:
            moved.setdefault(x, []).append(y)
    for x, partners in sorted(moved.items()):
        report.add(scenario, channel, f'order[{_lane_label(x)}#{x.split("#")[1].split("@")[0]}]', 'order-changed',
                   None, sorted(partners))
        report.items[-1]['base'] = 'before'
        report.items[-1]['head'] = f'after {len(partners)}: ' + ', '.join(
            f'{_lane_label(p)}#{p.split("#")[1].split("@")[0]}' for p in sorted(partners)[:6])


def _rows(report, scenario, table, a, b):
    """Rows as a multiset (SELECT order is not a contract); leftovers paired by primary key."""
    pk = a.get('pk') or b.get('pk') or []
    ra, rb = a.get('rows', []), b.get('rows', [])
    ca = Counter(json.dumps(r, sort_keys=True) for r in ra)
    cb = Counter(json.dumps(r, sort_keys=True) for r in rb)
    if len(ra) != len(rb):
        report.add(scenario, 'table', f'{table}#rows', 'changed', len(ra), len(rb))
    only_a = [json.loads(k) for k in (ca - cb).elements()]
    only_b = [json.loads(k) for k in (cb - ca).elements()]

    def key(row):
        return json.dumps([row.get(c) for c in pk]) if pk else None
    pending: dict[str, list] = {}
    for row in only_b:
        if key(row) is not None:
            pending.setdefault(key(row), []).append(row)
    for row in only_a:
        match = pending.get(key(row)) if key(row) is not None else None
        if match:
            _deep(report, scenario, 'table', f"{table}[pk={','.join(safe(row.get(c)) for c in pk)}]", row, match.pop(0))
        else:
            report.add(scenario, 'table', f'{table}[row]', 'row-removed', row, None)
    for rows in pending.values():
        for row in rows:
            report.add(scenario, 'table', f'{table}[row]', 'row-added', None, row)
    for row in only_b:
        if key(row) is None:
            report.add(scenario, 'table', f'{table}[row]', 'row-added', None, row)


def compare(a: dict, b: dict, report: Report, calibration: list[dict] | None = None):
    """Each producer lane of a stream is compared as an exact sequence. Order
    across lanes (independent threads) is not a contract and is compared only
    when ``calibration`` (normalized base captures) is given: a diagnostic
    that reports orders every calibration run agreed on and ``b`` broke."""
    s = a['scenario']
    if a['error'] or b['error']:
        report.add(s, 'run', 'error', 'run-failed', 'failed' if a['error'] else 'ok',
                   'failed' if b['error'] else 'ok')
        return
    _sequence(report, s, 'http', a['http'], b['http'])
    _stream(report, s, 'sse', a['sse'], b['sse'], calibration and [c['sse'] for c in calibration])
    _stream(report, s, 'sse_log', a.get('sse_log', []), b.get('sse_log', []),
            calibration and [c.get('sse_log', []) for c in calibration])
    _deep(report, s, 'schema', 'sqlite_master', a['schema'], b['schema'])
    _deep(report, s, 'sse', 'live-contract', a.get('live', {}), b.get('live', {}))
    for table in sorted(set(a['tables']) | set(b['tables'])):
        _rows(report, s, table, a['tables'].get(table, {}), b['tables'].get(table, {}))
    _files(report, s, a['files'], b['files'])
    _deep(report, s, 'cli', 'cli', a['cli'], b['cli'])


def _files(report, scenario, a, b):
    fa = {f['path']: f for f in a}
    fb = {f['path']: f for f in b}
    for path in sorted(set(fa) | set(fb)):
        loc = f'files[{_hash(path)}]'
        if path not in fb:  # paths are scratch-relative and may carry scenario text: hashed
            report.add(scenario, 'files', loc, 'file-removed', fa[path]['bytes'], None)
        elif path not in fa:
            report.add(scenario, 'files', loc, 'file-added', None, fb[path]['bytes'])
        else:
            _deep(report, scenario, 'files', loc, fa[path], fb[path])


def shapes(report: Report, top: int = 40) -> list[str]:
    """Differences grouped by shape (indices and hashes folded), most frequent first."""
    fold = re.compile(r'\[\d+(:\d+)?\]|\[\d+:\d+\]->\[\d+:\d+\]|#[0-9a-f]{8}|pk=[^\]]*')
    counts = Counter(('*', d['channel'], fold.sub('[*]', d['locator']), d['kind'])
                     for d in report.items)
    return [f'{n:6d}  {ch} / {loc} / {kind}' for (_, ch, loc, kind), n in counts.most_common(top)]


def render(report: Report, intended: set[str], title: str, errors=()) -> str:
    lines = [f'# Replay difference report: {title}', '']
    unexplained = [d for d in report.items if d['id'] not in intended]
    by_scenario = Counter(d['scenario'] for d in report.items)
    lines.append(f'differences: {len(report.items)}  registered intended: '
                 f'{len(report.items) - len(unexplained)}  unexplained: {len(unexplained)}')
    for scen, count in sorted(by_scenario.items()):
        lines.append(f'  {scen}: {count}')
    for line in errors:
        lines.append(f'RUN ERROR {line}')
    if report.items:
        lines += ['', 'id            class        scenario / channel / locator / kind: base -> head']
    for d in report.items:
        cls = 'intended' if d['id'] in intended else 'UNEXPLAINED'
        lines.append(f"{d['id']}  {cls:<11}  {d['scenario']} / {d['channel']} / {d['locator']} / "
                     f"{d['kind']}: {d['base']} -> {d['head']}")
    return '\n'.join(lines) + '\n'
