"""Independent legacy client corpus for U09, distinct from server routing tests.

These expected payloads/results are specified from the accepted A2 wire matrix,
not generated from the new descriptor catalogue or from the codec under test.
"""
from __future__ import annotations

import json

import pytest

from lib.backends import GoalUnsupported
from lib.runtime_bridge import RuntimeClient, RuntimeProtocolError, RuntimeUnavailable, StopLease, encode_request
from lib.turn_dispatch import DispatchError, DispatchResult


# Frozen minimal command payload, including legacy omitted maintenance capability.
MINIMAL_SEND = {
    'text': 'synthetic', 'requested_session': '', 'trace_id': '',
    'synthesize_audio': True, 'forced_session': '', 'routed_by_orchestrator': False,
    'client_msg_id': '', 'origin': 'user', 'sender_agent_id': '',
    'prompt_admission': None, 'prompt_admission_id': '', 'queue_if_busy': False,
    'skip_admission': False, 'durable_queue_id': '', 'unheard_audio_sessions': [],
    'allow_paused_queue': False,
}


@pytest.mark.parametrize(('method', 'args', 'kwargs', 'wire_method', 'params', 'response', 'expected'), [
    ('dispatch', (), {'text': 'synthetic'}, 'dispatch', MINIMAL_SEND,
     {'ok': True, 'result': {'session': 's', 'backend': 'codex'}}, DispatchResult('s', 'codex')),
    ('dispatch_queued', ('q1',), {}, 'dispatch_queued', {'queue_id': 'q1'},
     {'ok': True, 'result': {'queued': True, 'queue_depth': '2'}}, DispatchResult('', '', True, 2)),
    ('recover_queued', (), {}, 'recover_queued', {}, {'ok': True, 'result': '3'}, 3),
    ('status', (), {}, 'status', {}, {'ok': True, 'result': []}, {}),
    ('status_within', (0.2,), {}, 'status', {}, {'ok': True, 'result': {'draining': True}}, {'draining': True}),
    ('release_leaked_slots', ('a1',), {}, 'release_leaked_slots', {'agent_id': 'a1'},
     {'ok': True, 'result': {2: 3}}, {'2': '3'}),
    ('repair_slot', ('a1',), {'actor': 'synthetic', 'reason': 'probe'}, 'repair_slot',
     {'agent_id': 'a1', 'actor': 'synthetic', 'reason': 'probe', 'actor_verified': False},
     {'ok': True, 'result': {'released_trace': 7, 'queue_recovered': '2', 'ignored': 1}},
     {'released_trace': '7', 'queue_recovered': 2}),
    ('interrupt', ('codex', 'a1'), {}, 'interrupt', {'backend': 'codex', 'agent_id': 'a1'},
     {'ok': True, 'result': '2'}, 2),
    ('interrupt_any', ('a1',), {}, 'interrupt_any', {'agent_id': 'a1'}, {'ok': True}, 0),
    ('steer', ('codex', 'a1', 'synthetic'), {'client_msg_id': 'c1'}, 'steer',
     {'backend': 'codex', 'agent_id': 'a1', 'text': 'synthetic', 'client_msg_id': 'c1', 'synthesize_audio': False},
     {'ok': True, 'result': 'accepted'}, True),
    ('begin_stop', ('a1', 'codex'), {'strict': True}, 'begin_stop',
     {'agent_id': 'a1', 'backend': 'codex', 'strict': True, 'hold': True},
     {'ok': True, 'result': {'lease_id': 5, 'terminated': '2'}}, StopLease('5', '', 2, 0)),
    ('begin_stop', ('a1', 'codex'), {'strict': False, 'hold': False, 'fence_user_receipts': False}, 'begin_stop',
     {'agent_id': 'a1', 'backend': 'codex', 'strict': False, 'hold': False, 'fence_user_receipts': False},
     {'ok': True, 'result': {}}, StopLease('', '', 0, 0)),
    ('finish_stop', ('lease1', {'z', 'a'}), {}, 'finish_stop',
     {'lease_id': 'lease1', 'cancelled_trace_ids': ['a', 'z']}, {'ok': True, 'result': True}, None),
    ('recover_janitor_quota', ('codex', 'owner', 4), {}, 'recover_janitor_quota',
     {'provider': 'codex', 'owner_id': 'owner', 'generation': 4, 'approval_id': ''},
     {'ok': False, 'result': None}, None),
    ('recover_janitor_quota', ('codex', 'owner', 4), {}, 'recover_janitor_quota',
     {'provider': 'codex', 'owner_id': 'owner', 'generation': 4, 'approval_id': ''},
     {'ok': False}, {'status': 'runtime_unavailable'}),
    ('cancel_janitor_run', ('run1',), {}, 'cancel_janitor_run', {'run_id': 'run1'},
     {'ok': True, 'result': [['cancelled', True]]}, {'cancelled': True}),
    ('release_agent', ('a1',), {}, 'release_agent', {'agent_id': 'a1'}, {'ok': True, 'result': '2'}, 2),
    ('goal', ('a1', 'get'), {}, 'goal', {'agent_id': 'a1', 'action': 'get', 'objective': ''},
     {'ok': True, 'result': ['opaque', 1]}, ['opaque', 1]),
    ('compact', ('s1',), {}, 'compact', {'session': 's1'}, {'ok': True, 'result': []}, {}),
    ('explanation_command', ('a1', 'label'), {}, 'explanation_command', {'agent_id': 'a1', 'label': 'label'},
     {'ok': True, 'result': 3}, ''),
    ('live_snapshot', (), {'agent_id': 'a1'}, 'live_snapshot', {'session': '', 'agent_id': 'a1'},
     {'ok': True, 'result': ['not-object']}, None),
    ('live_observe', ('a1', 'thinking', 'working', {'trace_id': 't1'}), {}, 'live_observe',
     {'agent_id': 'a1', 'event': 'thinking', 'to_state': 'working', 'detail': {'trace_id': 't1'}},
     {'ok': False, 'error': 'legacy ignored reply'}, None),
    ('live_patch', ('a1', 'i1', {'label': 'synthetic'}), {}, 'live_patch',
     {'agent_id': 'a1', 'item_id': 'i1', 'fields': {'label': 'synthetic'}},
     {'ok': False, 'error': 'legacy ignored reply'}, None),
])
def test_each_client_policy_against_independent_wire_and_result(method, args, kwargs, wire_method, params, response, expected, tmp_path, monkeypatch):
    calls = []
    def request(client, name, values=None, **options):
        calls.append((json.loads(encode_request(name, values)), client.timeout, options))
        return response
    monkeypatch.setattr(RuntimeClient, '_request', request)
    client = RuntimeClient(tmp_path / 'unused.sock', timeout=9)
    assert getattr(client, method)(*args, **kwargs) == expected
    assert len(calls) == 1
    envelope, timeout, options = calls[0]
    assert envelope == {'version': 1, 'method': wire_method, 'params': params}
    assert timeout == {'explanation_command': 0.5, 'live_observe': 2.0}.get(method, 9)
    if method == 'status_within':
        assert options == {'total_s': 0.2}
    elif method == 'status':
        assert options == {'timeout': None}
    else:
        assert options == {}


@pytest.mark.parametrize(('method', 'args', 'kwargs', 'response', 'kind', 'message'), [
    ('dispatch', (), {'text': 'synthetic'}, {}, DispatchError, 'agent runtime rejected the request'),
    ('dispatch_queued', ('q1',), {}, {'ok': False, 'status': 409, 'error': 'queued refusal'}, DispatchError, 'queued refusal'),
    ('recover_queued', (), {}, {}, RuntimeUnavailable, 'agent runtime recovery failed'),
    ('status', (), {}, {}, RuntimeUnavailable, 'agent runtime status unavailable'),
    ('release_leaked_slots', ('a1',), {}, {}, RuntimeProtocolError, 'runtime leak release failed'),
    ('repair_slot', ('a1',), {'actor': 'a', 'reason': 'r'}, {}, RuntimeProtocolError, 'runtime slot repair failed'),
    ('begin_stop', ('a1', 'codex'), {'strict': True}, {}, DispatchError, 'runtime stop failed'),
    ('finish_stop', ('lease1',), {}, {}, RuntimeProtocolError, 'runtime stop lease failed'),
    ('cancel_janitor_run', ('run1',), {}, {}, DispatchError, 'Maintenance cancellation failed'),
    ('release_agent', ('a1',), {}, {}, RuntimeProtocolError, 'runtime release failed'),
    ('goal', ('a1', 'get'), {}, {'ok': False, 'status': 501, 'error': 'unsupported'}, GoalUnsupported, 'unsupported'),
    ('goal', ('a1', 'get'), {}, {}, RuntimeError, 'runtime goal request failed'),
    ('compact', ('s1',), {}, {}, RuntimeProtocolError, 'runtime compaction failed'),
    ('live_snapshot', (), {}, {}, RuntimeProtocolError, 'runtime live snapshot failed'),
])
def test_method_failures_keep_distinct_exception_and_fallback(method, args, kwargs, response, kind, message, tmp_path, monkeypatch):
    monkeypatch.setattr(RuntimeClient, '_request', lambda *args, **kwargs: response)
    with pytest.raises(kind) as error:
        getattr(RuntimeClient(tmp_path / 'unused.sock'), method)(*args, **kwargs)
    assert type(error.value) is kind
    assert str(error.value) == message
    if kind is DispatchError:
        expected_status = 409 if method == 'dispatch_queued' else 503 if method == 'dispatch' else 502
        assert error.value.status == expected_status


@pytest.mark.parametrize(('method', 'args', 'expected'), [
    ('interrupt', ('codex', 'a1'), 0),
    ('interrupt_any', ('a1',), 0),
    ('steer', ('codex', 'a1', 'synthetic'), False),
    ('explanation_command', ('a1', 'label'), ''),
    ('ping', (), False),
])
def test_legacy_nonraising_rejections_remain_distinct(method, args, expected, tmp_path, monkeypatch):
    monkeypatch.setattr(RuntimeClient, '_request', lambda *args, **kwargs: {'ok': False, 'status': 503, 'error': 'synthetic refusal'})
    assert getattr(RuntimeClient(tmp_path / 'unused.sock'), method)(*args) == expected
