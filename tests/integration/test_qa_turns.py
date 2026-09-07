"""Real HTTP/dispatch/provider-process/SQLite round trips without credentials."""
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import urllib.request
import urllib.error

import pytest

ROOT = Path(__file__).resolve().parents[2]


class QAHost:
    def __init__(self, root):
        self.root = root
        self.process = None

    def start(self):
        previous = (self.root / 'host.json')
        if previous.exists():
            previous.unlink()
        self.log = (self.root.parent / f'{self.root.name}.log').open('a')
        self.process = subprocess.Popen(
            [sys.executable, str(ROOT / 'tests/qa/host.py'), '--state-dir', str(self.root)],
            stdout=self.log, stderr=subprocess.STDOUT)
        for _ in range(100):
            if previous.exists():
                self.url = json.loads(previous.read_text())['url']
                return self
            if self.process.poll() is not None:
                raise AssertionError(f'QA host exited: {self.process.returncode}')
            time.sleep(.05)
        raise AssertionError('QA host did not become ready')

    def stop(self):
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
            self.log.close()
            self.process = None

    def request(self, path, data=None):
        request = urllib.request.Request(
            self.url + path, data=json.dumps(data).encode() if data is not None else None,
            headers={'Authorization': 'Bearer qa-host-test', 'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.load(response)

    def wait_reply(self, session, text):
        for _ in range(120):
            result = self.request('/log?session=' + session)
            if any(row['role'] == 'assistant' and text in row['text'] for row in result['turns']):
                return result
            time.sleep(.05)
        raise AssertionError(f'no reply for {session}: {result}')


@pytest.fixture
def qa_host(tmp_path):
    host = QAHost(tmp_path / 'host')
    try:
        yield host.start()
    finally:
        host.stop()


def test_retry_after_lost_response_converges_by_message_identity(qa_host):
    payload = {'session': 'rachel', 'text': 'one durable message',
               'client_msg_id': 'retry-message', 'synthesize_audio': False}
    qa_host.request('/send', payload)
    qa_host.request('/send', payload)  # client did not observe the first acknowledgement
    transcript = qa_host.wait_reply('rachel', payload['text'])
    assert sum(row['id'] == 'u-retry-message' for row in transcript['turns']) == 1
    with sqlite3.connect(qa_host.root / 'state.sqlite') as db:
        assert db.execute("SELECT count(*) FROM messages WHERE message_id='u-retry-message'").fetchone()[0] == 1
    assert (qa_host.root / 'telemetry.sqlite').is_file()


def test_qa_host_refuses_real_provider_creation_and_dispatch(qa_host):
    with pytest.raises(urllib.error.HTTPError) as error:
        qa_host.request('/agents', {'name': 'Real', 'backend': 'claude', 'cwd': '/tmp'})
    assert error.value.code == 403
    # Even a malformed fixture cannot reach a real authenticated CLI.
    with sqlite3.connect(qa_host.root / 'state.sqlite') as db:
        db.execute("UPDATE agents SET backend='claude' WHERE session='rachel'")
    with pytest.raises(urllib.error.HTTPError) as error:
        qa_host.request('/send', {'session': 'rachel', 'text': 'must not execute',
                                 'client_msg_id': 'unsupported', 'synthesize_audio': False})
    assert error.value.code == 500
    assert b'QA host supports only' in error.value.read()


def test_two_hosts_do_not_share_session_identity(tmp_path):
    first, second = QAHost(tmp_path / 'first'), QAHost(tmp_path / 'second')
    try:
        first.start()
        second.start()
        for host, text in ((first, 'first host'), (second, 'second host')):
            host.request('/send', {'session': 'rachel', 'text': text,
                                  'client_msg_id': 'same-client-id', 'synthesize_audio': False})
        a = first.wait_reply('rachel', 'first host')
        b = second.wait_reply('rachel', 'second host')
        assert a['conversation_id'] != b['conversation_id']
        assert not any('second host' in row['text'] for row in a['turns'])
        assert not any('first host' in row['text'] for row in b['turns'])
    finally:
        first.stop()
        second.stop()


def test_durable_queue_survives_host_restart(qa_host):
    qa_host.request('/send', {'session': 'rachel', 'text': 'earlier conversation',
                             'client_msg_id': 'earlier', 'synthesize_audio': False})
    before = qa_host.wait_reply('rachel', 'earlier conversation')
    qa_host.request('/send', {'session': 'rachel', 'text': '[qa-slow] active',
                             'client_msg_id': 'active', 'synthesize_audio': False})
    queued = qa_host.request('/send', {'session': 'rachel', 'text': 'queued after restart',
                                     'client_msg_id': 'queued', 'queue_if_busy': True,
                                     'synthesize_audio': False})
    assert queued['queued'] is True
    qa_host.stop()
    qa_host.start()
    transcript = qa_host.wait_reply('rachel', 'queued after restart')
    assert transcript['conversation_id'] == before['conversation_id']
    assert any(row['text'] == 'earlier conversation' for row in transcript['turns'])
    assert sum(row['id'] == 'u-queued' for row in transcript['turns']) == 1
