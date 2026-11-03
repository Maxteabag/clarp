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


def _frame(data, event=None, wire_id='1'):
    return {'event': event, 'id': wire_id, 'data': data}


def _http(body, status=200, headers=None):
    return {'step': 'send', 'method': 'POST', 'path': '/send', 'status': status,
            'headers': headers or {}, 'body': body}


def test_generated_ids_and_clocks_normalize_away_but_keep_identity():
    n = normalize.Normalizer({'<ROOT>': '/scratch/s'})
    n.in_stream = True
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
    events = [_frame({'type': 'agent-state', 'kind': 'done', 'status_text': ''}),
              _frame({'type': 'agent-activity', 'state': 'idle'})]
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
    envelope = _report(_capture(sse=[_frame({'type': 'x'})]), _capture(sse=[_frame({'type': 'x'}, wire_id=None)]))
    assert [d['locator'].split(']', 1)[1] for d in envelope.items] == ['[0].id']


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
    spawn = {'source': 'pwa', 'dispatch': 'codex', 'origin': 'user', 'trace_id': 't1', 'backend_session_id': ''}
    start = {'dispatch': 'codex', 'trace_id': 't1', 'origin': 'user'}
    assert not _report(_capture(db=rows(spawn, start)), _capture(db=rows(start, spawn))).items
    assert _report(_capture(db=rows(spawn, start)), _capture(db=rows(start, {**spawn, 'source': 'x'}))).items


def test_run_roots_are_exclusive_and_clean_spares_a_live_one(tmp_path):
    import run
    first, second = run.RunRoot(tmp_path), run.RunRoot(tmp_path)
    assert first.path != second.path
    assert run.live(first.path) and run.live(second.path)
    first.dir_for('run1'), first.dir_for('run1')
    second.release(keep=True)
    assert not run.live(second.path)
    assert run.cmd_clean(type('A', (), {'scratch': str(tmp_path)})()) == 0
    assert first.path.exists() and not second.path.exists()
    first.release(keep=False)
    assert not first.path.exists()


def test_a_scenario_root_that_exists_is_refused_never_deleted(tmp_path):
    import capture
    root = tmp_path / 's00'
    root.mkdir()
    (root / 'keep').write_text('another job')
    with pytest.raises(capture.StepFailed):
        capture.fresh_root(root)
    assert (root / 'keep').exists()


# Controls from review s0-harness-57d175ff (reviewer B): behaviour changes that
# must stay differences.

@pytest.mark.parametrize('left,right', [
    ({'text': '2026-01-01T00:00:00Z'}, {'text': '2026-02-01T00:00:00Z'}),
    ({'text': '{"x":1, "y":2}'}, {'text': '{"y":2,"x":1}'}),
    ({'max_bytes': 1_800_000_000}, {'max_bytes': 1_900_000_000}),
])
def test_content_is_not_clock_or_structured_column_noise(left, right):
    assert _report(_capture(http=[_http(left)]), _capture(http=[_http(right)])).items


def test_a_stored_json_column_keeps_its_form_and_key_order():
    def table(raw):
        return _capture(db={'schema': [], 'pk': {'state_log': ['state_id']}, 'tables': {'state_log': [
            {'state_id': 1, 'agent_id': 'a', 'kind': 'done', 'detail': raw}]}})
    assert not _report(table('{"a": 1, "b": 2}'), table('{"a": 1, "b": 2}')).items
    assert _report(table('{"a": 1, "b": 2}'), table('{"b": 2, "a": 1}')).items
    assert _report(table('{"a": 1, "b": 2}'), table('{"a":1,"b":2}')).items


def _parse_sse(wire: bytes):
    import capture
    import io
    import threading
    import time
    reader = capture.SSE.__new__(capture.SSE)
    reader.frames, reader.errors, reader.ended = [], [], False
    reader.last_at = time.monotonic()
    reader._lock, reader._stop = threading.Lock(), threading.Event()
    reader._resp = io.BytesIO(wire)
    reader._read()
    assert not reader.errors
    return reader.frames


def test_changed_sse_wire_id_is_a_difference_even_with_the_same_data():
    frame = 'id: {}\ndata: {{"type":"agent-state","event_id":7}}\n\n'
    same = _report(_capture(sse=_parse_sse(frame.format(7).encode())),
                   _capture(sse=_parse_sse(frame.format(7).encode())))
    moved = _report(_capture(sse=_parse_sse(frame.format(7).encode())),
                    _capture(sse=_parse_sse(frame.format(999).encode())))
    assert not same.items and moved.items


def test_wire_id_and_payload_reference_stay_aliased():
    wire = (b'id: 7\ndata: {"type":"agent-state","event_id":7}\n\n'
            b'id: 8\ndata: {"type":"live","event_id":8,"after_event_id":7}\n\n')
    out = normalize.normalize(_capture(sse=_parse_sse(wire)), '/h')['sse']
    assert out[0]['id'] == out[0]['data']['event_id'] == out[1]['data']['after_event_id']
    assert out[1]['id'] == out[1]['data']['event_id'] != out[0]['id']


@pytest.mark.parametrize('channel', ['db', 'sse'])
def test_done_details_are_not_the_documented_thinking_writer_race(channel):
    first, second = {'trace_id': 't1', 'reason': 'first'}, {'trace_id': 't1', 'reason': 'second'}

    def sample(details):
        if channel == 'db':
            rows = [{'state_id': i + 1, 'agent_id': 'a', 'kind': 'done', 'detail': json.dumps(d)}
                    for i, d in enumerate(details)]
            return _capture(db={'schema': [], 'pk': {'state_log': ['state_id']}, 'tables': {'state_log': rows}})
        return _capture(sse=[_frame({'type': 'agent-state', 'status_text': '', 'session': 's', 'kind': 'done',
                                     'detail': d}) for d in details])
    assert _report(sample([first, second]), sample([second, first])).items


def test_two_thinking_rows_that_are_not_the_spawn_start_pair_keep_their_order():
    a = {'trace_id': 't1', 'dispatch': 'codex', 'origin': 'user'}
    b = {'trace_id': 't1', 'dispatch': 'codex', 'origin': 'heartbeat'}

    def rows(details):
        return _capture(db={'schema': [], 'pk': {'state_log': ['state_id']}, 'tables': {'state_log': [
            {'state_id': i + 1, 'agent_id': 'a', 'kind': 'thinking', 'detail': json.dumps(d)}
            for i, d in enumerate(details)]}})
    assert _report(rows([a, b]), rows([b, a])).items
