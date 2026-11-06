"""Pinned wire behavior and an inert first parse, below runtime execution."""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

import pytest

from lib import prompt_admissions, runtime_bridge, turn_command, turn_dispatch


@pytest.mark.parametrize(('raw', 'expected'), [
    (b'{"version":1,"method":" status "}',
     {'version': 1, 'method': ' status ', 'params': {}}),
    (b'{"version":true,"method":"status","ignored":1}',
     {'version': True, 'method': 'status', 'params': {}}),
    (b'{"version":1.0,"method":"status","params":{}}',
     {'version': 1.0, 'method': 'status', 'params': {}}),
])
def test_decode_keeps_legacy_method_and_version_language(raw, expected):
    assert runtime_bridge.decode_request(raw) == expected


def test_encoding_trims_method_and_preserves_nested_value_bytes():
    assert runtime_bridge.encode_request(' status ', {
        'path': pathlib.Path('/synthetic/home'), 'tuple': (1, False),
        'nested': {2: [None, 'x']},
    }) == (b'{"version":1,"method":"status","params":'
           b'{"path":"/synthetic/home","tuple":[1,false],"nested":{"2":[null,"x"]}}}\n')


@pytest.mark.parametrize(('raw', 'message'), [
    (b'\xff', 'invalid runtime request'),
    (b'[]', 'runtime request must be an object'),
    (b'{"method":"status"}', 'unsupported runtime protocol version: None'),
    (b'{"version":2,"method":"status"}', 'unsupported runtime protocol version: 2'),
    (b'{"version":1,"method":" "}', 'runtime method is required'),
    (b'{"version":1,"method":"status","params":null}', 'runtime params must be an object'),
])
def test_decode_failures_retain_exact_protocol_errors(raw, message):
    with pytest.raises(runtime_bridge.RuntimeProtocolError) as error:
        runtime_bridge.decode_request(raw)
    assert str(error.value) == message


@pytest.mark.parametrize(('params', 'message'), [
    ({'z': 1, 'a': 2, 'prompt_admission': 42}, 'unknown dispatch fields: a, z'),
    ({'prompt_admission': 42}, 'invalid prompt admission'),
    ({'prompt_admission': '{}'}, 'invalid prompt admission'),
    ({}, 'dispatch text is required'),
])
def test_command_failure_precedence_is_not_hardened(params, message):
    with pytest.raises(ValueError) as error:
        turn_command.DispatchCommand.from_wire(params)
    assert str(error.value) == message


def test_command_legacy_iterables_and_optional_capability_omission():
    command = turn_dispatch.DispatchCommand.from_wire({'text': '', 'unheard_audio_sessions': 'ab'})
    assert command.unheard_audio_sessions == ('a', 'b')
    assert command.to_wire()['unheard_audio_sessions'] == ['a', 'b']
    assert 'janitor_run_id' not in command.as_kwargs()
    assert 'janitor_run_id' not in command.to_wire()
    assert turn_dispatch.DispatchCommand(text='x', janitor_run_id='run-1').to_wire()['janitor_run_id'] == 'run-1'


def test_dispatch_response_retains_projection_and_error_identity(tmp_path, monkeypatch):
    client = runtime_bridge.RuntimeClient(tmp_path / 'unused.sock')
    monkeypatch.setattr(client, '_request', lambda *args, **kwargs: {
        'ok': True, 'result': {'session': 12, 'backend': None, 'queued': 'yes',
                            'queue_depth': '2', 'queue_revision': False,
                            'delivery': [], 'delivery_runtime': None}})
    result = client.dispatch(text='synthetic')
    assert result == turn_command.DispatchResult('12', '', True, 2, 0, None, None)
    monkeypatch.setattr(client, '_request', lambda *args, **kwargs: {'ok': False, 'status': 0, 'error': ''})
    with pytest.raises(turn_dispatch.DispatchError) as error:
        client.dispatch(text='synthetic')
    assert error.value.status == 503
    assert str(error.value) == 'agent runtime rejected the request'


def test_contract_first_parse_has_no_execution_or_database_dependency(tmp_path):
    # A fresh interpreter avoids conftest/import cache hiding a backedge. First
    # parse also matters: a lazy DB import would pass an import-only control.
    script = '''
import importlib.abc, sys
class NoExecution(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(('lib.db', 'lib.turn_', 'lib.runtime_bridge',
                                'lib.prompt_admissions', 'lib.backend', 'lib.config')):
            raise AssertionError('execution dependency: ' + fullname)
sys.meta_path.insert(0, NoExecution())
from lib.runtime_contract import DispatchCommand, PromptAdmission, encode_request
raw = {'admission_id':'padm-1','admission_version':1,
       'authenticated_at_admission':True,'cooperative_principal':'user',
       'origin':'user','channel':'chat','observed_at':2,
       'client_admission_id':'c1','trace_id':'t1','original_text':'synthetic'}
command = DispatchCommand.from_wire({'text':'synthetic','prompt_admission':raw})
assert isinstance(command.prompt_admission, PromptAdmission)
assert command.prompt_admission.principal_id == ''
assert command.prompt_admission.sender_agent_id == ''
assert command.prompt_admission.client == ''
assert encode_request('dispatch', command.to_wire()).endswith(b'\\n')
print('contract-first-parse-ok')
'''
    server = pathlib.Path(__file__).resolve().parents[2] / 'server'
    env = {**os.environ, 'HOME': str(tmp_path), 'CLAUDE_PWA_DB': str(tmp_path / 'must-not-exist.sqlite'),
           'PYTHONPATH': str(server), 'PYTHONDONTWRITEBYTECODE': '1'}
    result = subprocess.run([sys.executable, '-c', script], cwd=tmp_path,
                            env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'contract-first-parse-ok'
    assert not (tmp_path / 'must-not-exist.sqlite').exists()


def test_contract_compatibility_exports_keep_one_class_identity():
    from lib import runtime_contract as contract
    assert turn_dispatch.DispatchCommand is turn_command.DispatchCommand is contract.DispatchCommand
    assert turn_dispatch.DispatchResult is turn_command.DispatchResult is contract.DispatchResult
    assert turn_dispatch.DispatchError is turn_command.DispatchError is contract.DispatchError
    assert prompt_admissions.PromptAdmission is contract.PromptAdmission
    assert runtime_bridge.StopLease is contract.StopLease
    assert runtime_bridge.RuntimeProtocolError is contract.RuntimeProtocolError
    assert runtime_bridge.RuntimeUnavailable is contract.RuntimeUnavailable
