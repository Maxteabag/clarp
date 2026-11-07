"""The synthetic provider goal file preserves identity and semantic comparison (812)."""
from __future__ import annotations

import copy
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import diff
import normalize


def capture(offset=0, *, two=False, path='provider/fake-goals.json'):
    agent = f'{offset + 1:032x}'
    thread = f'{offset + 10:08x}-1234-4567-89ab-123456789abc'
    clock = 1793990000000 + offset * 1000
    entry = {'objective': 'synthetic objective', 'status': 'complete', 'tokens': 7,
             'created': clock, 'updated': clock + 52}
    tables = {
        'agents': [{'agent_id': agent, 'session': 'rachel'}],
        'agent_goals': [{'agent_id': agent, 'session': 'rachel', 'backend': 'codex',
                         'objective': entry['objective'], 'status': 'complete'}],
        'runtimes': [{'runtime_id': 1, 'agent_id': agent, 'backend_session_id': thread}],
        'turns': [{'turn_id': 1, 'runtime_id': 1, 'agent_id': agent, 'trace_id': 'trace-a',
                   'outcome': 'completed'},
                  {'turn_id': 2, 'runtime_id': 1, 'agent_id': agent, 'trace_id': 'trace-b',
                   'outcome': 'completed'}],
        'messages': [{'message_id': 'user-a', 'agent_id': agent, 'backend_session_id': thread,
                      'trace_id': 'trace-a', 'role': 'user', 'text': 'synthetic request'},
                     {'message_id': 'reply-a', 'agent_id': agent, 'backend_session_id': thread,
                      'trace_id': 'trace-a', 'role': 'assistant', 'text': 'synthetic answer'},
                     {'message_id': 'reply-b', 'agent_id': agent, 'backend_session_id': thread,
                      'trace_id': 'trace-b', 'role': 'assistant', 'text': 'synthetic goal answer'}]}
    goals = {thread: entry}
    if two:
        other = capture(offset + 50)
        for name in tables:
            rows = copy.deepcopy(other['db']['tables'][name])
            for row in rows:
                if 'session' in row:
                    row['session'] = 'mike'
                if 'runtime_id' in row:
                    row['runtime_id'] += 10
                if 'turn_id' in row:
                    row['turn_id'] += 10
                if 'message_id' in row:
                    row['message_id'] += '-mike'
            tables[name] += rows
        goals.update(other['files'][0]['json'])
    pk = {'agents': ['agent_id'], 'agent_goals': ['agent_id'], 'runtimes': ['runtime_id'],
          'turns': ['turn_id'], 'messages': ['message_id']}
    return {'scenario': 'goal-turn', 'error': None, 'root': '/synthetic', 'build': '/build',
            'http': [], 'sse': [], 'db': {'schema': [], 'pk': pk, 'tables': tables},
            'files': [{'path': path, 'kind': 'json', 'bytes': 0, 'digest': None,
                       'text': None, 'json': goals}]}


def report(a, b):
    out = diff.Report()
    diff.compare(normalize.normalize(a, '/h'), normalize.normalize(b, '/h'), out)
    return out.items


@pytest.mark.parametrize('two', [False, True])
def test_generated_goal_identity_and_clocks_compare_equal_without_mutating_capture(two):
    a, b = capture(two=two), capture(100, two=two)
    if two:
        b['files'][0]['json'] = dict(reversed(list(b['files'][0]['json'].items())))
    original = copy.deepcopy((a, b))
    assert not report(a, b)
    assert (a, b) == original
    assert len(normalize.normalize(b, '/h')['files'][0]['json']) == (2 if two else 1)


@pytest.mark.parametrize('field,value', [('objective', 'changed objective'), ('tokens', 8),
                                        ('status', 'active'), ('extra', 1), ('tokens', '7'),
                                        ('created', None), ('created', True), ('created', 'clock'),
                                        ('updated', 12), ('updated', 1793989999999)])
def test_goal_payload_and_invalid_clocks_still_differ(field, value):
    a, b = capture(), capture()
    next(iter(b['files'][0]['json'].values()))[field] = value
    assert any(i['channel'] == 'files' for i in report(a, b))


@pytest.mark.parametrize('field', ['created', 'updated'])
def test_missing_goal_clock_is_not_invented(field):
    a, b = capture(), capture()
    del next(iter(b['files'][0]['json'].values()))[field]
    assert any(i['channel'] == 'files' for i in report(a, b))


@pytest.mark.parametrize('fault', ['unknown-key', 'foreign-runtime', 'wrong-goal-agent', 'duplicate-runtime',
                                  'duplicate-goal', 'extra-entry', 'missing-entry', 'same-objective-other-agent'])
def test_goal_binding_requires_a_unique_positive_join(fault):
    a, b = capture(), capture()
    tables, goals = b['db']['tables'], b['files'][0]['json']
    key = next(iter(goals))
    if fault == 'unknown-key':
        goals['ffffffff-1234-4567-89ab-123456789abc'] = goals.pop(key)
    elif fault == 'foreign-runtime':
        tables['runtimes'][0]['backend_session_id'] = 'foreign-thread'
    elif fault in ('wrong-goal-agent', 'same-objective-other-agent'):
        tables['agent_goals'][0]['agent_id'] = 'foreign-agent'
    elif fault == 'duplicate-runtime':
        tables['runtimes'].append(copy.deepcopy(tables['runtimes'][0]))
    elif fault == 'duplicate-goal':
        tables['agent_goals'].append(copy.deepcopy(tables['agent_goals'][0]))
    elif fault == 'extra-entry':
        goals['ffffffff-1234-4567-89ab-123456789abc'] = copy.deepcopy(goals[key])
    else:
        goals.clear()
    items = report(a, b)
    assert items
    assert any(i['channel'] == 'files' for i in items)


@pytest.mark.parametrize('fault', ['remove-turn', 'failed-turn', 'message-backend', 'message-agent',
                                  'message-trace', 'message-content'])
def test_goal_adaptation_does_not_hide_turn_or_message_regressions(fault):
    a, b = capture(), capture()
    tables = b['db']['tables']
    if fault == 'remove-turn':
        tables['turns'].pop()
    elif fault == 'failed-turn':
        tables['turns'][1]['outcome'] = 'failed'
    else:
        field = {'message-backend': 'backend_session_id', 'message-agent': 'agent_id',
                 'message-trace': 'trace_id', 'message-content': 'text'}[fault]
        tables['messages'][1][field] = 'changed-source'
    items = report(a, b)
    assert any(i['channel'] == 'table' and i['locator'].startswith(
        'turns' if fault in ('remove-turn', 'failed-turn') else 'messages') for i in items)
    if fault in ('message-backend', 'message-agent', 'message-trace'):
        assert any(i['channel'] == 'files' for i in items)


@pytest.mark.parametrize('path', ['elsewhere/fake-goals.json', 'provider/other.json'])
def test_fixture_adaptation_does_not_normalize_other_paths(path):
    assert any(i['channel'] == 'files' for i in report(capture(path=path), capture(100, path=path)))


@pytest.mark.parametrize('channel', ['files', 'http', 'table', 'sse'])
def test_created_updated_clocks_outside_fixture_entry_remain_content(channel):
    a, b = capture(), capture()
    if channel == 'files':
        a['files'][0]['path'] = b['files'][0]['path'] = 'provider/other.json'
        next(iter(b['files'][0]['json'].values()))['created'] += 1000
    elif channel == 'http':
        a['http'] = [{'step': 'read', 'method': 'GET', 'path': '/goal', 'status': 200,
                      'body': {'created': 1793990000000}, 'headers': {}}]
        b['http'] = copy.deepcopy(a['http'])
        b['http'][0]['body']['created'] += 1000
    elif channel == 'table':
        a['db']['tables']['agent_goals'][0]['created'] = 1793990000000
        b['db']['tables']['agent_goals'][0]['created'] = 1793990001000
    else:
        a['sse'] = [{'event': None, 'id': None,
                     'data': {'type': 'goal-test', 'created': 1793990000000}}]
        b['sse'] = copy.deepcopy(a['sse'])
        b['sse'][0]['data']['created'] += 1000
    assert any(i['channel'] == channel for i in report(a, b))


def test_nested_unexpected_clock_and_arbitrary_map_keys_are_not_rewritten():
    a, b = capture(), capture()
    for raw, clock in ((a, 1793990000000), (b, 1793990001000)):
        next(iter(raw['files'][0]['json'].values()))['extra'] = {'created': clock}
    assert any(i['channel'] == 'files' for i in report(a, b))
    a, b = capture(), capture()
    a['files'][0].update(path='provider/other.json', json={'12345678-1234-4567-89ab-123456789abc': 1})
    b['files'][0].update(path='provider/other.json', json={'87654321-1234-4567-89ab-123456789abc': 1})
    assert any(i['channel'] == 'files' for i in report(a, b))


def test_two_existing_threads_cannot_be_swapped_or_collapsed_by_goal_objective():
    a, b = capture(two=True), capture(two=True)
    runtimes = b['db']['tables']['runtimes']
    runtimes[0]['backend_session_id'], runtimes[1]['backend_session_id'] = (
        runtimes[1]['backend_session_id'], runtimes[0]['backend_session_id'])
    assert any(i['channel'] == 'files' for i in report(a, b))
    a, b = capture(two=True), capture(two=True)
    # A UUID alias cannot overwrite a valid canonical owner.
    key = next(iter(b['files'][0]['json']))
    b['files'][0]['json'][key.upper()] = copy.deepcopy(b['files'][0]['json'][key])
    assert any(i['channel'] == 'files' for i in report(a, b))
