"""The replay report's own guarantees, without running a Host."""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import diff  # noqa: E402
import normalize  # noqa: E402
import planted  # noqa: E402

SECRET = 'synthetic secret sentence that must never be printed'


def _capture(**over):
    base = {'scenario': 's', 'error': None, 'root': '/scratch/s', 'build': '/b',
            'http': [], 'sse': [], 'db': {'schema': [], 'tables': {}}, 'files': []}
    return {**base, **over}


def _report(a, b):
    report = diff.Report()
    diff.compare(normalize.normalize(a, '/h'), normalize.normalize(b, '/h'), report)
    return report


def test_generated_ids_and_clocks_normalize_away_but_keep_identity():
    n = normalize.Normalizer({'<ROOT>': '/scratch/s'})
    a = n.value({'agent_id': 'a1b2c3d4e5f60718', 'ts': 1793692941182, 'again': 'a1b2c3d4e5f60718',
                 'cwd': '/scratch/s/workspace', 'pid': 4242})
    assert a == {'agent_id': '<id:1>', 'ts': '<time>', 'again': '<id:1>',
                 'cwd': '<ROOT>/workspace', 'pid': '<pid>'}


def test_report_never_prints_message_text():
    a = _capture(sse=[{'type': 'transcript', 'text': SECRET}],
                 db={'schema': [], 'tables': {'messages': [{'message_id': 'u-1', 'text': SECRET}]}})
    b = _capture(sse=[{'type': 'transcript', 'text': SECRET + ' changed'}],
                 db={'schema': [], 'tables': {'messages': [{'message_id': 'u-1', 'text': 'other'}]}})
    report = _report(a, b)
    rendered = diff.render(report, set(), 'test')
    assert report.items
    assert 'synthetic' not in rendered and 'other' not in rendered


def test_order_swap_and_removed_field_are_differences():
    events = [{'type': 'agent-state', 'kind': 'done'}, {'type': 'agent-activity', 'state': 'idle'}]
    swapped = _report(_capture(sse=events), _capture(sse=events[::-1]))
    assert any(d['kind'].startswith('sequence-') for d in swapped.items)
    http = [{'step': 'send', 'method': 'POST', 'path': '/send', 'status': 200,
             'body': {'ok': True, 'queue_depth': 0}}]
    dropped = [{**http[0], 'body': {'ok': True}}]
    removed = _report(_capture(http=http), _capture(http=dropped))
    assert [d['kind'] for d in removed.items] == ['field-removed']


def test_identical_captures_have_no_difference_and_stable_ids():
    a = _capture(http=[{'step': 'x', 'method': 'GET', 'path': '/status', 'status': 200, 'body': {'busy': False}}])
    b = _capture(http=[{**a['http'][0], 'status': 500}])
    assert not _report(a, a).items
    assert [d['id'] for d in _report(a, b).items] == [d['id'] for d in _report(a, b).items]


def test_planted_change_refuses_a_stale_anchor(tmp_path):
    (tmp_path / 'server').mkdir()
    (tmp_path / 'server/x.py').write_text('a = 1\n')
    with pytest.raises(RuntimeError, match='anchor found 0 times'):
        planted.apply({'id': 't', 'file': 'server/x.py', 'find': 'b = 2', 'replace': ''}, tmp_path)


@pytest.mark.parametrize('change', planted.CHANGES, ids=lambda c: c['id'])
def test_every_planted_anchor_matches_this_tree(change, tmp_path):
    repo = pathlib.Path(__file__).resolve().parents[2]
    target = tmp_path / change['file']
    target.parent.mkdir(parents=True)
    target.write_text((repo / change['file']).read_text())
    planted.apply(change, tmp_path)
    assert target.read_text() != (repo / change['file']).read_text()
