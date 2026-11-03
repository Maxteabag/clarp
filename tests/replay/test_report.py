"""The replay report's own guarantees, without running a Host."""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import diff  # noqa: E402
import normalize  # noqa: E402
import planted  # noqa: E402

SECRETS = ('synthetic secret sentence that must never be printed', 'zebrafish', '<private-message>')


def _capture(**over):
    base = {'scenario': 's', 'error': None, 'root': '/scratch/s', 'build': '/b',
            'http': [], 'sse': [], 'db': {'schema': [], 'pk': {}, 'tables': {}}, 'files': []}
    return {**base, **over}


def _report(a, b):
    report = diff.Report()
    diff.compare(normalize.normalize(a, '/h'), normalize.normalize(b, '/h'), report)
    return report


def _frame(data, event=None, has_id=True):
    return {'event': event, 'has_id': has_id, 'data': data}


def _http(body, status=200, headers=None):
    return {'step': 'send', 'method': 'POST', 'path': '/send', 'status': status,
            'headers': headers or {}, 'body': body}


def test_generated_ids_and_clocks_normalize_away_but_keep_identity():
    n = normalize.Normalizer({'<ROOT>': '/scratch/s'})
    out = n.value({'agent_id': 'a1b2c3d4e5f60718', 'ts': 1793692941182, 'again_id': 'a1b2c3d4e5f60718',
                   'cwd': '/scratch/s/workspace', 'pid': 4242, 'event_id': 17, 'next': {'event_id': 17}})
    assert out == {'agent_id': '<id:1>', 'ts': '<time>', 'again_id': '<id:1>', 'cwd': '<ROOT>/workspace',
                   'pid': '<pid>', 'event_id': '<eid:?1>', 'next': {'event_id': '<eid:?1>'}}


def test_settings_checksums_and_free_text_hex_stay_comparable():
    n = normalize.Normalizer({})
    assert n.value({'timeout_ms': 30000, 'heartbeat_interval_sec': 60}) == {
        'timeout_ms': 30000, 'heartbeat_interval_sec': 60}
    # Hex outside an id field is content, not a generated id.
    assert n.value({'checksum': 'deadbeefcafe0123'}) == {'checksum': 'deadbeefcafe0123'}
    a = _capture(http=[_http({'checksum': 'deadbeefcafe0123'})])
    b = _capture(http=[_http({'checksum': 'deadbeefcafe9999'})])
    assert [d['kind'] for d in _report(a, b).items] == ['changed']


@pytest.mark.parametrize('secret', SECRETS)
def test_report_never_prints_message_text_values_or_keys(secret):
    a = _capture(sse=[_frame({'type': 'transcript', 'text': secret, 'reason': secret, 'by': {secret: 1}})],
                 db={'schema': [], 'pk': {'messages': ['message_id']},
                     'tables': {'messages': [{'message_id': 'u-1', 'text': secret}]}})
    b = _capture(sse=[_frame({'type': 'transcript', 'text': secret + 'x', 'reason': 'other',
                              'by': {secret + 'x': 1}})],
                 db={'schema': [], 'pk': {'messages': ['message_id']},
                     'tables': {'messages': [{'message_id': 'u-1', 'text': 'other'}]}})
    report = _report(a, b)
    rendered = diff.render(report, set(), 'test')
    assert report.items
    assert secret not in rendered and 'other' not in rendered


def test_codes_from_the_server_source_do_print():
    a = _capture(sse=[_frame({'type': 'agent-state', 'kind': 'done'})])
    b = _capture(sse=[_frame({'type': 'agent-state', 'kind': 'thinking'})])
    rendered = diff.render(_report(a, b), set(), 'test')
    assert 'kind=done -> message agent-state kind=thinking' in rendered


def test_order_swap_removed_field_header_and_envelope_are_differences():
    events = [_frame({'type': 'agent-state', 'kind': 'done'}), _frame({'type': 'agent-activity', 'state': 'idle'})]
    # One producer (the state watcher) emits both: one lane, so a swap is a sequence difference.
    assert {d['kind'] for d in _report(_capture(sse=events), _capture(sse=events[::-1])).items} == {
        'sequence-insert', 'sequence-delete'}
    # Independent producers: their interleaving is not compared without calibration.
    live = _frame({'type': 'live', 'ops': []})
    assert not _report(_capture(sse=[events[0], live]), _capture(sse=[live, events[0]])).items
    removed = _report(_capture(http=[_http({'ok': True, 'queue_depth': 0})]),
                      _capture(http=[_http({'ok': True})]))
    assert [(d['kind'], d['locator']) for d in removed.items] == [('field-removed', '[0].body.queue_depth')]
    header = _report(_capture(http=[_http({}, headers={'cache-control': 'no-store'})]),
                     _capture(http=[_http({}, headers={'cache-control': 'no-cache'})]))
    assert [d['locator'] for d in header.items] == ['[0].headers.cache-control']
    envelope = _report(_capture(sse=[_frame({'type': 'x'})]), _capture(sse=[_frame({'type': 'x'}, has_id=False)]))
    assert [d['locator'].split(']', 1)[1] for d in envelope.items] == ['[0].has_id']


def test_same_size_file_change_is_a_difference():
    f = {'path': 'audio/a.mp3', 'bytes': 4, 'kind': 'bytes', 'digest': 'aa'}
    assert _report(_capture(files=[f]), _capture(files=[{**f, 'digest': 'bb'}])).items


def test_rows_pair_by_primary_key_and_count_duplicates():
    rows = [{'turn_id': 1, 'outcome': 'interrupted'}, {'turn_id': 2, 'outcome': 'completed'}]
    a = _capture(db={'schema': [], 'pk': {'turns': ['turn_id']}, 'tables': {'turns': rows}})
    b = _capture(db={'schema': [], 'pk': {'turns': ['turn_id']},
                     'tables': {'turns': [rows[1], {**rows[0], 'outcome': 'failed'}]}})
    assert [(d['locator'], d['kind']) for d in _report(a, b).items] == [('turns[pk=1].outcome', 'changed')]
    dup = _capture(db={'schema': [], 'pk': {'t': []}, 'tables': {'t': [{'v': 1}, {'v': 1}]}})
    one = _capture(db={'schema': [], 'pk': {'t': []}, 'tables': {'t': [{'v': 1}]}})
    assert {d['kind'] for d in _report(dup, one).items} == {'changed', 'row-removed'}


def test_intended_id_binds_the_values_not_just_the_place():
    a = _capture(http=[_http({}, status=200)])
    first = _report(a, _capture(http=[_http({}, status=403)])).items
    other = _report(a, _capture(http=[_http({}, status=500)])).items
    assert first[0]['locator'] == other[0]['locator'] and first[0]['id'] != other[0]['id']


def test_planted_change_refuses_a_stale_anchor(tmp_path):
    (tmp_path / 'server').mkdir()
    (tmp_path / 'server/x.py').write_text('a = 1\n')
    with pytest.raises(RuntimeError, match='anchor found 0 times'):
        planted.apply({'id': 't', 'file': 'server/x.py', 'find': 'b = 2', 'replace': ''}, tmp_path)


def test_a_planted_change_counts_only_its_own_observable():
    change = planted.BY_ID['response-field']
    unrelated = [{'channel': 'http', 'kind': 'changed', 'locator': '[0].status'}]
    assert not planted.caught(change, unrelated)
    assert planted.caught(change, [{'channel': 'http', 'kind': 'field-removed',
                                    'locator': '[2].body.queue_depth'}])


@pytest.mark.parametrize('change', planted.CHANGES, ids=lambda c: c['id'])
def test_every_planted_anchor_matches_this_tree(change, tmp_path):
    repo = pathlib.Path(__file__).resolve().parents[2]
    target = tmp_path / change['file']
    target.parent.mkdir(parents=True)
    target.write_text((repo / change['file']).read_text())
    planted.apply(change, tmp_path)
    assert target.read_text() != (repo / change['file']).read_text()


def test_cross_lane_order_counts_only_where_calibration_agrees():
    state = _frame({'type': 'agent-state', 'kind': 'done'})
    live = _frame({'type': 'live', 'ops': []})
    note = _frame({'type': 'user-notification', 'push': True})
    norm = lambda frames: normalize.normalize(_capture(sse=frames), '/h')
    run1, run2 = norm([state, live, note]), norm([live, state, note])
    head = norm([note, live, state])
    report = diff.Report()
    diff.compare(run1, head, report, [run1, run2])
    # state/live disagreed in calibration; both moving behind the notification did not.
    assert [(d['kind'], d['head'].split(':')[0]) for d in report.items] == [
        ('order-changed', 'after 1'), ('order-changed', 'after 1')]
    quiet = diff.Report()
    diff.compare(run1, run2, quiet, [run1, run2])
    assert not quiet.items


def test_event_ids_are_named_by_lane_position_and_keep_aliases():
    frames = [_frame({'type': 'agent-state', 'event_id': 41}), _frame({'type': 'live', 'event_id': 40,
                                                                       'ref_event_id': 41})]
    out = normalize.normalize(_capture(sse=frames), '/h')['sse']
    assert out[0]['data']['event_id'] == out[1]['data']['ref_event_id']
    assert out[0]['data']['event_id'].startswith('<eid:agent-state#1@')


def test_racing_writers_of_one_transition_compare_equal_but_a_changed_detail_does_not():
    def rows(first, second):
        return {'schema': [], 'pk': {'state_log': ['state_id']}, 'tables': {'state_log': [
            {'state_id': 1, 'agent_id': 'a', 'kind': 'thinking', 'detail': json.dumps(first)},
            {'state_id': 2, 'agent_id': 'a', 'kind': 'thinking', 'detail': json.dumps(second)}]}}
    spawn = {'trace_id': 't1', 'dispatch': 'codex', 'source': 'pwa'}
    start = {'trace_id': 't1', 'dispatch': 'codex'}
    assert not _report(_capture(db=rows(spawn, start)), _capture(db=rows(start, spawn))).items
    assert _report(_capture(db=rows(spawn, start)), _capture(db=rows(start, {**spawn, 'source': 'x'}))).items
