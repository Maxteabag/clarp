"""The replay report's own guarantees, without running a Host."""
from __future__ import annotations

import json
import re
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
    # Builds are refused while any run is live (1); the finished run is removed, the live one kept.
    assert run.cmd_clean(type('A', (), {'scratch': str(tmp_path)})()) == 1
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


def _disposable_build(tmp_path, monkeypatch):
    """A real registered clean Git worktree under scratch/builds/<sha> of a throwaway repo."""
    import subprocess
    import run
    repo = tmp_path / 'repo'
    repo.mkdir()

    def git(*args):
        return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True,
                              text=True).stdout.strip()
    git('init', '-q')
    (repo / 'synthetic.txt').write_text('synthetic fixture\n')
    git('add', 'synthetic.txt')
    git('-c', 'user.name=t', '-c', 'user.email=t@localhost', 'commit', '-qm', 'fixture')
    sha = git('rev-parse', 'HEAD')
    scratch = tmp_path / 'scratch'
    build = scratch / 'builds' / sha
    build.parent.mkdir(parents=True)
    git('worktree', 'add', '-q', '--detach', str(build), sha)
    real_git = run.git
    monkeypatch.setattr(run, 'HARNESS', repo)
    monkeypatch.setattr(run, 'git', lambda *args, **kwargs: real_git(*args, cwd=kwargs.get('cwd', repo)))
    return run, scratch, build


def test_clean_does_not_remove_a_build_an_active_replay_uses(tmp_path, monkeypatch):
    # Reviewer B's control (review s0-harness-c06cb4992293): a live run's cached build survives clean.
    import subprocess
    from types import SimpleNamespace
    run, scratch, build = _disposable_build(tmp_path, monkeypatch)
    active = run.RunRoot(scratch)
    process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], cwd=build)
    try:
        assert run.cmd_clean(SimpleNamespace(scratch=str(scratch))) == 1
        assert process.poll() is None and build.exists()
    finally:
        process.terminate()
        process.wait(timeout=5)
        active.release(keep=False)


def test_clean_removes_a_build_once_no_run_holds_the_lease(tmp_path, monkeypatch):
    from types import SimpleNamespace
    run, scratch, build = _disposable_build(tmp_path, monkeypatch)
    run.RunRoot(scratch).release(keep=False)
    assert run.cmd_clean(SimpleNamespace(scratch=str(scratch))) == 0
    assert not build.exists()


def test_scenarios_have_unique_ids_and_known_bugs_name_a_register_id():
    import scenarios
    ids = [s['id'] for s in scenarios.SCENARIOS]
    assert len(ids) == len(set(ids))
    assert all(re.fullmatch(r'[A-Z]+-N?\d+', s['known_bug']) for s in scenarios.KNOWN_BUGS)
    assert scenarios.GATING and not any(s.get('known_bug') for s in scenarios.GATING)


def test_the_provider_proxy_injects_tool_items_after_turn_start(tmp_path):
    import subprocess
    proxy = pathlib.Path(__file__).resolve().parent / 'fake_provider.py'
    requests = [
        {'id': 1, 'method': 'initialize', 'params': {}},
        {'id': 2, 'method': 'thread/start', 'params': {}},
        {'id': 3, 'method': 'turn/start', 'params': {'input': [{'type': 'text', 'text': '[replay-tool] x'}]}},
    ]
    env = {'PATH': '/usr/bin:/bin', 'CLARP_QA_PROVIDER_ROOT': str(tmp_path), 'CLARP_QA_STREAM_DELAY': '0'}
    proc = subprocess.Popen([sys.executable, str(proxy), 'app-server'], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, text=True, env=env)
    for request in requests:
        proc.stdin.write(json.dumps(request) + '\n')
    proc.stdin.flush()
    methods = []
    for line in proc.stdout:
        event = json.loads(line)
        methods.append((event.get('method'), (event.get('params') or {}).get('item', {}).get('type')))
        if event.get('method') == 'turn/completed':
            break
    proc.stdin.close()
    proc.wait(timeout=10)
    started = methods.index(('turn/started', None))
    assert methods[started + 1:started + 6] == [
        ('item/started', 'reasoning'), ('item/completed', 'reasoning'),
        ('item/started', 'commandExecution'), ('item/commandExecution/outputDelta', None),
        ('item/completed', 'commandExecution')]


def test_pairing_secrets_become_surrogates_and_keep_identity():
    code = 'clp_' + 'A' * 43
    token = 'cld_' + 'b-' * 24
    out = normalize.Normalizer({}).value({'code': code, 'uri': f'clarp://pair?code={code}', 'token': token})
    assert out == {'code': '<secret:1>', 'uri': 'clarp://pair?code=<secret:1>', 'token': '<secret:2>'}


def test_step_placeholders_fill_from_run_variables():
    import capture
    filled = capture.fill({'path': '/schedules/{schedule}', 'args': ['--url', 'http://127.0.0.1:{port}']},
                          {'schedule': 'sched_1', 'port': 4242})
    assert filled == {'path': '/schedules/sched_1', 'args': ['--url', 'http://127.0.0.1:4242']}
    # rf-readmodel RM-N3: a value an earlier step failed to save is a scenario failure with a reason.
    with pytest.raises(capture.StepFailed, match='nothing saved as limited_code'):
        capture.fill({'code': '{limited_code}'}, {'port': 4242})


def test_the_replay_host_refuses_network_but_not_loopback():
    import subprocess
    here = pathlib.Path(__file__).resolve().parent
    probe = (
        "import sys, socket; sys.path.insert(0, %r); import host; host.loopback_only()\n"
        "s = socket.socket(); s.settimeout(1)\n"
        "try:\n    s.connect(('192.0.2.1', 80)); print('connected')\n"
        "except ConnectionRefusedError as e: print('refused')\n"
        "print(socket.socket().connect_ex(('127.0.0.1', 9)) != 111 or 'loopback-tried')\n"
        "try:\n    socket.getaddrinfo('example.com', 443); print('resolved')\n"
        "except socket.gaierror: print('no-dns')\n") % str(here)
    out = subprocess.run([sys.executable, '-c', probe], capture_output=True, text=True, timeout=20).stdout.split()
    assert out[0] == 'refused' and out[2] == 'no-dns'


def test_cli_json_output_form_remains_observable(tmp_path, monkeypatch):
    # Reviewer B's control (review s0-corpus-aa744001): CLI stdout layout and key order are frozen.
    import capture
    from types import SimpleNamespace
    (tmp_path / 'config.toml').write_text('[tts]\nenabled = false\n')
    host = SimpleNamespace(root=tmp_path, build=tmp_path / 'build', python=sys.executable,
                           url='http://127.0.0.1:17991')
    outputs = iter(['{"a":1,"b":2}\n', '{\n  "b": 2,\n  "a": 1\n}\n', '{\n  "b": 2,\n  "a": 1\n}\n',
                    '{\n  "b": 2,\n  "a": 1\n}'])
    monkeypatch.setattr(capture.subprocess, 'run', lambda *a, **kw:
                        SimpleNamespace(returncode=0, stdout=next(outputs), stderr=''))
    first, second, third, fourth = (capture.run_cli(host, {'args': ['sessions']}) for _ in range(4))
    assert first != second and second == third and third != fourth
    assert second['stdout']['<form>'] == 'default-indent2'


def _live(lseq, *ops):
    return _frame({'type': 'live', 'session': 's', 'lseq': lseq, 'ops': list(ops)}, wire_id=None)


def test_live_appends_split_by_timing_compare_equal_but_text_and_lseq_gaps_do_not():
    up = {'op': 'upsert', 'id': 'i1', 'rev': 1}
    whole = [_live(1, up), _live(2, {'op': 'append', 'id': 'i1', 'field': 'text', 'rev': 2, 'chunk': 'abcdef'}),
             _live(3, {'op': 'done', 'id': 'i1', 'rev': 3})]
    split = [_live(1, up), _live(2, {'op': 'append', 'id': 'i1', 'field': 'text', 'rev': 2, 'chunk': 'ab'}),
             _live(3, {'op': 'append', 'id': 'i1', 'field': 'text', 'rev': 3, 'chunk': 'cdef'}),
             _live(4, {'op': 'done', 'id': 'i1', 'rev': 4})]
    assert not _report(_capture(sse=whole), _capture(sse=split)).items
    changed = [*split[:2], _live(3, {'op': 'append', 'id': 'i1', 'field': 'text', 'rev': 3, 'chunk': 'cdeX'}), split[3]]
    assert _report(_capture(sse=whole), _capture(sse=changed)).items
    other_item = [*split[:2], _live(3, {'op': 'append', 'id': 'i2', 'field': 'text', 'rev': 1, 'chunk': 'cdef'}),
                  split[3]]
    assert _report(_capture(sse=whole), _capture(sse=other_item)).items
    gap = [whole[0], whole[1], _live(5, {'op': 'done', 'id': 'i1', 'rev': 3})]
    assert any('live-contract' in d['locator'] for d in _report(_capture(sse=whole), _capture(sse=gap)).items)


# Reviewer B's controls (review s0-corpus-55526c31): changes that must stay differences.


def test_live_item_revision_gap_remains_a_difference():
    baseline = [_live(1, {"op": "upsert", "id": "i1", "rev": 1}),
                _live(2, {"op": "append", "id": "i1", "rev": 2, "field": "text", "chunk": "hello"})]
    broken = [baseline[0], _live(2, {"op": "append", "id": "i1", "rev": 9, "field": "text", "chunk": "hello"})]
    assert _report(_capture(sse=baseline), _capture(sse=broken)).items


def test_later_tool_output_lines_remain_observable():
    def frames(last):
        return [_live(1, {"op": "upsert", "id": "i1", "rev": 1}),
                _live(2, {"op": "append", "id": "i1", "rev": 2, "field": "tool.output", "lines": ["first"], "total_lines": 1}),
                _live(3, {"op": "append", "id": "i1", "rev": 3, "field": "tool.output", "lines": [last], "total_lines": 2})]
    assert _report(_capture(sse=frames("second")), _capture(sse=frames("CHANGED"))).items


def test_unrecognized_cli_json_layout_keeps_its_bytes(tmp_path, monkeypatch):
    import capture
    from types import SimpleNamespace
    (tmp_path / "config.toml").write_text('[tts]\nenabled = false\n')
    host = SimpleNamespace(root=tmp_path, build=tmp_path / "build", python=sys.executable,
                           url="http://127.0.0.1:17991")
    outputs = iter(['{"a": 1,"b":2}\n', '{"a":1, "b":2}\n'])
    monkeypatch.setattr(capture.subprocess, "run", lambda *a, **kw:
                        SimpleNamespace(returncode=0, stdout=next(outputs), stderr=""))
    assert capture.run_cli(host, {"args": ["sessions"]}) != capture.run_cli(host, {"args": ["sessions"]})


def test_a_malformed_live_sequence_stays_observable():
    baseline = [_live(1, {"op": "upsert", "id": "i1", "rev": 1}),
                _live(2, {"op": "done", "id": "i1", "rev": 2})]
    broken = [baseline[0], _live("broken", {"op": "done", "id": "i1", "rev": 2})]
    assert _report(_capture(sse=baseline), _capture(sse=broken)).items


def test_arbitrary_text_file_numbers_are_content():
    def files(text):
        return [{"path": "workspace/result.txt", "kind": "text", "bytes": len(text), "text": text}]
    assert _report(_capture(files=files("release number 1793692941")),
                   _capture(files=files("release number 1793692942"))).items


# Reviewer B's controls (review s0-corpus-9ec1349a): append metadata and merged-away envelopes.


def test_append_metadata_is_not_lost_when_chunks_merge():
    def frames(kind):
        return [_live(1, {"op": "upsert", "id": "i1", "rev": 1}),
                _live(2, {"op": "append", "id": "i1", "kind": "message", "rev": 2, "field": "text", "chunk": "ab"}),
                _live(3, {"op": "append", "id": "i1", "kind": kind, "rev": 3, "field": "text", "chunk": "cd"})]
    assert _report(_capture(sse=frames("message")), _capture(sse=frames("tool"))).items


def test_envelope_on_a_merged_away_frame_is_not_lost():
    def frames(changed):
        rows = [_live(1, {"op": "upsert", "id": "i1", "rev": 1}),
                _live(2, {"op": "append", "id": "i1", "rev": 2, "field": "text", "chunk": "ab"}),
                _live(3, {"op": "append", "id": "i1", "rev": 3, "field": "text", "chunk": "cd"})]
        if changed:
            rows[-1] = {**rows[-1], "event": "unexpected-event", "id": "999"}
        return rows
    assert _report(_capture(sse=frames(False)), _capture(sse=frames(True))).items


def test_a_real_frame_whose_wire_id_names_its_event_still_merges():
    def frames(split):
        rows = [_frame({'type': 'live', 'session': 's', 'lseq': 1, 'event_id': 11,
                        'ops': [{'op': 'append', 'id': 'i1', 'rev': 1, 'field': 'text', 'chunk': 'ab' if split else 'abcd'}]},
                       wire_id='11')]
        if split:
            rows.append(_frame({'type': 'live', 'session': 's', 'lseq': 2, 'event_id': 12,
                                'ops': [{'op': 'append', 'id': 'i1', 'rev': 2, 'field': 'text', 'chunk': 'cd'}]},
                               wire_id='12'))
        return rows
    assert not _report(_capture(sse=frames(False)), _capture(sse=frames(True))).items


# Reviewer B's controls (review s0-corpus-1e49c91e): clock and wire-id envelope cases.


def frames(clock=1793692941182, wire=None):
    return [_frame({"type":"live", "session":"s", "lseq":1, "event_id":11,
                    "server_now_ms":1793692941181,
                    "ops":[{"op":"append", "id":"i1", "rev":1, "field":"text", "chunk":"ab"}]}, wire_id=None),
            _frame({"type":"live", "session":"s", "lseq":2, "event_id":12,
                    "server_now_ms":clock,
                    "ops":[{"op":"append", "id":"i1", "rev":2, "field":"text", "chunk":"cd"}]}, wire_id=wire)]


def test_clock_schema_on_a_dropped_frame_remains_observable():
    assert _report(_capture(sse=frames()), _capture(sse=frames(clock="broken"))).items


def test_valid_wire_id_presence_on_a_dropped_frame_remains_observable():
    assert _report(_capture(sse=frames()), _capture(sse=frames(wire="12"))).items


def test_a_hex_id_that_reads_as_a_json_number_is_still_an_id():
    # rf-merge g2i-s3: a launch receipt's trace id '5533263533985e85' parsed as 5.5e+97.
    def settings(trace):
        return _capture(db={'schema': [], 'pk': {'settings': ['key']}, 'tables': {'settings': [
            {'key': 'launch-receipt-seq:agent0000000a:00000000000000000002', 'value': trace}]}})
    assert not _report(settings('5533263533985e85'), settings('51d510e6cbd138ab')).items
    assert _report(settings('5533263533985e85'), settings('{"state": "attempted"}')).items


def test_a_prepared_build_from_an_older_harness_is_refused(tmp_path, monkeypatch):
    # rf-readmodel RM-N3: builds prepared before bin/ was shipped were reused silently.
    import run
    monkeypatch.setattr(run, 'PREPARED', tmp_path)
    build = tmp_path / ('a' * 40)
    (build / 'server').mkdir(parents=True)
    (build / 'server' / 'x.py').write_text('x = 1\n')
    (build / run.STAMP).write_text(json.dumps({'sha': 'a' * 40, 'digest': run.tree_digest(build)}))
    with pytest.raises(run.BuildError, match='older harness'):
        run.prepared_build('a' * 12)


def test_a_path_field_from_the_previous_response_fills_its_placeholder():
    # queue-cancel: DELETE /turn-queue/{queue_id} with the id from the GET before it.
    import capture
    from types import SimpleNamespace
    sent = []
    host = SimpleNamespace(request=lambda method, path, *a, **kw: (sent.append(path), (200, {}, {}))[1])
    http = [{'body': {'items': [{'id': 'q 1'}]}}]
    capture.run_step(host, None, {'op': 'delete', 'path': '/turn-queue/{queue_id}',
                                  'from_last': {'queue_id': 'items.0.id'}}, http, [], {'port': 1})
    assert sent == ['/turn-queue/q%201']


def test_the_stored_event_log_gets_the_same_canonical_rules_as_the_stream():
    # rf-merge g2-mainint3: sse_events rows were rebuilt from raw payloads after canonicalizing.
    spawn = {'source': 'pwa', 'dispatch': 'codex', 'origin': 'user', 'trace_id': 't1', 'backend_session_id': ''}
    start = {'dispatch': 'codex', 'trace_id': 't1', 'origin': 'user'}

    def log(first, second):
        rows = [{'event_id': i + 1, 'ts': 1, 'type': 'agent-state', 'session': 'mike', 'agent_id': 'a',
                 'payload': json.dumps({'type': 'agent-state', 'status_text': '', 'session': 'mike',
                                        'kind': 'thinking', 'detail': d})} for i, d in enumerate((first, second))]
        return _capture(db={'schema': [], 'pk': {'sse_events': ['event_id']}, 'tables': {'sse_events': rows}})
    assert not _report(log(spawn, start), log(start, spawn)).items
    assert _report(log(spawn, start), log(start, {**spawn, 'source': 'x'})).items


def test_a_clip_like_number_in_free_text_remains_content():
    # Reviewer B s0-voice-853f9037 F1: only a clip name, or a path or URL ending in one, is rewritten.
    left = _capture(http=[_http({'text': 'retain token 1800000000000__payload'})])
    right = _capture(http=[_http({'text': 'retain token 1800000000001__payload'})])
    assert _report(left, right).items


def _clip_files(name):
    return [{'path': f'cache/audio/{name}', 'kind': 'bytes', 'bytes': 3, 'digest': 'd'},
            {'path': f'cache/audio/{name}.json', 'kind': 'bytes', 'bytes': 3, 'digest': 'e'}]


def test_a_generated_clip_name_path_and_url_lose_only_their_creation_time():
    def clip(ms, session='rachel'):
        name = f'{ms}__{session}.mp3'
        return _capture(http=[_http({'name': name, 'url': f'/audio/{name}',
                                     'path': f'/scratch/s/cache/audio/{name}.json'})],
                        files=_clip_files(name))
    assert not _report(clip(1793692941182), clip(1793692941999)).items
    assert _report(clip(1793692941182), clip(1793692941182, 'bella')).items


def test_filename_shaped_http_text_is_still_content():
    # Reviewer B s0-voice-36b17ca5: a value shaped like a clip name, with no such clip, is content.
    left = _capture(http=[_http({'text': '1793692941182__rachel.mp3'})])
    right = _capture(http=[_http({'text': '1793692941999__rachel.mp3'})])
    assert _report(left, right).items


def test_only_the_runs_own_clips_are_rewritten():
    # A clip-shaped value that names no clip of this run stays content even when the run has a clip.
    def run(ms, other):
        name = f'{ms}__rachel.mp3'
        return _capture(http=[_http({'url': f'/audio/{name}', 'text': f'{other}__rachel.mp3'})],
                        files=_clip_files(name))
    assert not _report(run(1793692941182, 1700000000000), run(1793692941999, 1700000000000)).items
    assert _report(run(1793692941182, 1700000000000), run(1793692941999, 1700000000001)).items


def test_the_chunk_callback_plant_needs_its_stream_witness():
    # Reviewer B s0-voice-853f9037 F2: only the 'clip live stream' exchange counts.
    change = planted.BY_ID['on-chunk-signature']
    live = planted._http_index('voice-streamed-clip', 'clip live stream')

    def diff_at(locator, kind, scenario='voice-streamed-clip', channel='http'):
        return [{'scenario': scenario, 'channel': channel, 'kind': kind, 'locator': locator}]
    assert planted.caught(change, diff_at(f'[{live}:{live + 1}]->[{live}:{live + 1}]', 'sequence-replace'))
    assert planted.caught(change, diff_at(f'[{live}].body.<bytes>', 'changed'))
    assert not planted.caught(change, diff_at('[0].status', 'changed'))
    assert not planted.caught(change, diff_at(f'[0:{live + 1}]->[0:{live + 1}]', 'sequence-replace'))
    assert not planted.caught(change, diff_at(f'[{live + 1}].body.<bytes>', 'changed'))
    assert not planted.caught(change, diff_at('clips[1].producer_status', 'changed', channel='table'))
    assert not planted.caught(change, diff_at(f'[{live}:{live + 1}]->[{live}:{live + 1}]', 'sequence-replace',
                                              scenario='voice-clip-turn'))


def _reader(wire: bytes, on_close=None):
    import io
    import threading
    import time
    import capture

    class Response(io.BytesIO):
        def close(self):
            if on_close:
                on_close()
            super().close()
    reader = capture.SSE.__new__(capture.SSE)
    reader.frames, reader.errors, reader.ended = [], [], False
    reader.last_at = time.monotonic()
    reader._lock, reader._stop = threading.Lock(), threading.Event()
    reader._resp = Response(wire)
    reader._thread = threading.Thread(target=reader._read, daemon=True)
    return reader


def test_a_frame_complete_before_the_cut_survives_its_slow_parse(monkeypatch):
    # Reviewer B s0-speed-3dc58a34: the cut must not drop a frame already being parsed.
    import threading
    import capture
    entered, release = threading.Event(), threading.Event()
    text = '{"type":"agent-state","tag":"complete-before-cut"}'
    original = capture.json.loads

    def delayed(value, *args, **kwargs):
        if value == text:
            entered.set()
            assert release.wait(5)
        return original(value, *args, **kwargs)
    monkeypatch.setattr(capture.json, 'loads', delayed)
    reader = _reader(('data: ' + text + '\n\n').encode(), on_close=release.set)
    reader._thread.start()
    try:
        assert entered.wait(2)
        frames = reader.close()
        assert not reader._thread.is_alive() and not reader.errors
        assert [f['data']['tag'] for f in frames] == ['complete-before-cut']
    finally:
        release.set()
        reader._thread.join(2)


def test_a_frame_completed_after_the_cut_is_not_captured():
    reader = _reader(b'data: {"type":"agent-state","n":1}\n\n')
    reader.freeze()
    reader._thread.start()
    assert reader.close() == [] and not reader.errors


def test_a_background_request_is_recorded_at_its_join():
    import threading
    import capture
    release = threading.Event()

    class StubHost:
        def request(self, method, path, body=None, raw=None, headers=None, token=None, timeout=10):
            assert release.wait(5)
            return 200, {}, {'ok': True, 'path': path}
    http, variables = [], {}
    step = {'op': 'post', 'path': '/send', 'name': 'send held', 'body': {}, 'background': 'held'}
    capture.run_step(StubHost(), None, step, http, [], variables)
    assert http == [] and 'held' in variables['<background>']   # still open, not recorded
    release.set()
    capture.run_step(StubHost(), None, {'op': 'join', 'name': 'held'}, http, [], variables)
    assert [(h['step'], h['status']) for h in http] == [('send held', 200)]
    assert not variables['<background>']


def test_joining_a_request_that_was_never_sent_fails():
    import capture
    with pytest.raises(capture.StepFailed):
        capture.run_step(None, None, {'op': 'join', 'name': 'missing'}, [], [], {})


def test_a_background_name_still_open_is_refused():
    # Reviewer B s0-codexpool-996077f8 F1.
    import threading
    import capture
    release = threading.Event()

    class StubHost:
        def request(self, *args, **kwargs):
            assert release.wait(5)
            return 200, {}, {'ok': True}
    variables = {}
    capture.run_step(StubHost(), None, {'op': 'get', 'path': '/a', 'background': 'same'}, [], [], variables)
    first = variables['<background>']['same'][1]
    try:
        with pytest.raises(capture.StepFailed):
            capture.run_step(StubHost(), None, {'op': 'get', 'path': '/b', 'background': 'same'}, [], [], variables)
        assert variables['<background>']['same'][1] is first
    finally:
        release.set()
        first.join(3)


def test_a_failed_capture_settles_its_background_requests(tmp_path, monkeypatch):
    # Reviewer B s0-codexpool-996077f8 F2: an unjoined request ends before capture returns.
    import threading
    import capture
    stopped, workers = threading.Event(), []

    class StubHost:
        def __init__(self, build, root, python):
            self.root, self.url = root, 'http://127.0.0.1:1'

        def start(self, config):
            pass

        def request(self, *args, **kwargs):
            workers.append(threading.current_thread())
            assert stopped.wait(5)
            return 200, {}, {'ok': True}

        def stop(self):
            stopped.set()

    class StubSSE:
        frames, errors, ended = [{'event': None, 'id': None, 'data': {}}], [], False

        def __init__(self, *args):
            pass

        def freeze(self):
            pass

        def close(self):
            return self.frames
    monkeypatch.setattr(capture, 'Host', StubHost)
    monkeypatch.setattr(capture, 'SSE', StubSSE)
    monkeypatch.setattr(capture, 'dump_db', lambda p: {'schema': [], 'pk': {}, 'tables': {}})
    monkeypatch.setattr(capture, 'list_files', lambda p: [])
    scenario = {'id': 'unjoined', 'steps': [{'op': 'get', 'path': '/held', 'background': 'held'}]}
    result = capture.capture(tmp_path / 'build', scenario, tmp_path / 'run')
    assert 'never joined' in result['error']
    assert workers and not workers[0].is_alive()
