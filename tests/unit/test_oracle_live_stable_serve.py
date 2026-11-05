"""Stable Oracle transport lifecycle, using byte streams and an in-memory provider.

These protect admission-before-paid-audio, wire startup/commands, podcast
receipts and cleanup after failures. No HTTP server or listening socket runs.
"""
import io
import json
import queue
import threading
from types import SimpleNamespace

import pytest
import websocket

from lib import oracle_live_stable as mod, oracle_realtime, ws


class Provider:
    def __init__(self):
        self.events = queue.Queue()
        self.sent = []
        self.closed = False
        self.model = 'fixture-model'
        self.voice = 'fixture-voice'

    def send(self, raw):
        event = json.loads(raw)
        self.sent.append(event)
        if event['type'] == 'session.close':
            self.events.put(json.dumps({'type': 'session.closed'}))

    def recv(self):
        try:
            event = self.events.get(timeout=.05)
        except queue.Empty:
            raise websocket.WebSocketTimeoutException()
        if isinstance(event, Exception):
            raise event
        return event

    def close(self):
        self.closed = True
        self.events.put('')


class Connection:
    def __init__(self):
        self.timeouts = []
        self.shutdowns = []

    def settimeout(self, value):
        self.timeouts.append(value)

    def shutdown(self, how):
        self.shutdowns.append(how)


@pytest.fixture
def transport(monkeypatch):
    from lib import config, oracle_voice_provider
    provider = Provider()
    threads = []
    real_thread = threading.Thread

    def thread(*args, **kwargs):
        worker = real_thread(*args, **kwargs)
        threads.append(worker)
        return worker

    monkeypatch.setattr(mod.threading, 'Thread', thread)
    monkeypatch.setattr(mod.config, 'load', lambda: config.Config(
        openai_api_key='fixture', oracle_earcons=False))
    monkeypatch.setattr(oracle_voice_provider, 'effective', lambda cfg: 'openai')
    monkeypatch.setattr(mod, '_connect_voice', lambda *args: (provider, 'openai'))
    monkeypatch.setattr(mod.oracle_strategy, 'contact', lambda *args, **kwargs: '')
    monkeypatch.setattr(mod.oracle_voice_context, 'load', lambda contact: None)
    tools = SimpleNamespace(lock=threading.Lock(), delegations=set(), results=lambda: [],
                            execute=lambda *args: {'agents': []})
    monkeypatch.setattr(mod, 'AgentTools', lambda *args: tools)
    handler = SimpleNamespace(
        path='/oracle/v2', headers={'Upgrade': 'websocket', 'Connection': 'Upgrade',
        'Sec-WebSocket-Key': 'dGhlIHNhbXBsZSBub25jZQ=='}, rfile=io.BytesIO(),
        wfile=io.BytesIO(), connection=Connection(), ctx=SimpleNamespace(media_dir='/fixture'),
        _request_auth_validated=True, _request_device_scope='full',
        _request_principal='stable-serve-device', _stop_agent_session=lambda *a, **k: (None, None))
    errors = []
    monkeypatch.setattr(mod, '_send_http_error', lambda h, code, message: errors.append((code, message)))
    state = SimpleNamespace(provider=provider, handler=handler, errors=errors)
    yield state
    provider.events.put('')
    for worker in threads:
        worker.join(3)
        assert not worker.is_alive()
    assert handler._request_principal not in oracle_realtime._ACTIVE_PRINCIPALS
    assert handler._request_principal not in mod._STOP_HOOKS


def frames(handler):
    data = handler.wfile.getvalue()
    assert data.startswith(b'HTTP/1.1 101')
    stream = io.BytesIO(data.split(b'\r\n\r\n', 1)[1])
    result = []
    while header := stream.read(2):
        opcode, length = header[0] & 15, header[1] & 127
        if length == 126:
            length = int.from_bytes(stream.read(2), 'big')
        elif length == 127:
            length = int.from_bytes(stream.read(8), 'big')
        result.append((opcode, stream.read(length)))
    return result


@pytest.mark.parametrize('query,message', [
    ('thread_id=a&thread_id=b', 'one Oracle context'),
    ('new_conversation=2', 'must be 0 or 1'),
    ('handoff=a&handoff=b', 'one handoff'),
    ('handoff_id=a&handoff_id=b', 'one handoff_id'),
    ('handoff=manual', 'must be hands_free'),
    ('handoff_id=invalid', 'Invalid handoff_id'),
    ('podcast_artifact=a&new_conversation=1', 'does not apply'),
    ('delegation_strategy=bad', 'delegation strategy'),
])
def test_invalid_context_never_opens_provider(transport, monkeypatch, query, message):
    monkeypatch.setattr(mod, '_connect_voice', lambda *args: pytest.fail('paid audio before validation'))
    transport.handler.path += '?' + query
    mod.serve(transport.handler)
    assert transport.errors[0][0] == 400
    assert message in transport.errors[0][1]


def test_context_lookup_failure_releases_claim_without_paid_audio(transport, monkeypatch):
    def invalid(*args, **kwargs):
        raise ValueError('unknown thread')
    monkeypatch.setattr(mod.oracle_memory, 'open_thread', invalid)
    monkeypatch.setattr(mod, '_connect_voice', lambda *args: pytest.fail('paid audio before memory'))
    mod.serve(transport.handler)
    assert transport.errors == [(400, 'unknown thread')]


def test_failed_provider_reports_502_and_releases_claim(transport, monkeypatch):
    def unavailable(*args):
        raise OSError('provider unavailable')
    monkeypatch.setattr(mod, '_connect_voice', unavailable)
    mod.serve(transport.handler)
    assert transport.errors == [(502, 'Oracle v2 upstream unavailable')]


@pytest.mark.parametrize('provider', ['openai', 'gemini'])
def test_wire_start_commands_pong_and_cleanup(transport, monkeypatch, provider):
    from lib import config, oracle_diagnostics
    journal = SimpleNamespace(session_id='fixture-journal', records=[], closed=False)
    journal.record = lambda *args: journal.records.append(args)
    def close():
        journal.closed = True
    journal.close = close
    monkeypatch.setattr(oracle_diagnostics, 'OracleJournal', lambda: journal)
    monkeypatch.setattr(mod.config, 'load', lambda: config.Config(
        openai_api_key='fixture', oracle_earcons=False, oracle_diagnostics=True))
    monkeypatch.setattr(mod, '_connect_voice', lambda *args: (transport.provider, provider))
    transport.handler.path += '?delegation_strategy=direct_contact&handoff=hands_free'
    incoming = iter([(ws.OP_PING, b'ping'), (ws.OP_TEXT, b'{"type":"unsupported"}'),
                     (ws.OP_TEXT, b'{"type":"session.close","handoff_id":"oh_123"}'), None])
    original_read = ws.read_frame
    monkeypatch.setattr(ws, 'read_frame', lambda stream: next(incoming))
    mod.serve(transport.handler)
    monkeypatch.setattr(ws, 'read_frame', original_read)
    output = frames(transport.handler)
    events = [json.loads(payload) for opcode, payload in output if opcode == ws.OP_TEXT]
    assert [e['type'] for e in events][:2] == ['oracle_v2.session', 'oracle_v2.routing_mode']
    assert any(e.get('message') == 'Unsupported Oracle v2 command' for e in events)
    assert (ws.OP_PONG, b'ping') in output
    assert transport.handler.connection.timeouts == [mod.CLIENT_IDLE_TIMEOUT]
    assert transport.provider.sent[0]['type'] == 'session.start'
    assert transport.provider.sent[-1] == {'type': 'session.close'}
    assert transport.provider.closed and not transport.errors
    assert journal.closed
    assert journal.records[0][0] == 'session.open'
    assert journal.records[0][1]['provider'] == provider
    assert journal.records[-1][0] == 'session.summary'
    if provider == 'gemini':
        assert journal.records[-1][1]['provider'] == 'gemini'


@pytest.mark.parametrize('revision', ['rev1', 'stale'])
def test_podcast_receipt_precedes_provider_and_finishes(transport, monkeypatch, revision):
    from lib import artifacts, podcast_history, podcast_live
    order = []
    artifact = {'type': 'audio', 'duration_ms': 120000, 'payload': {'podcast': {'revision': 'rev1'}}}
    monkeypatch.setattr(artifacts, 'get', lambda ident: artifact)
    monkeypatch.setattr(podcast_history, 'source_artifact', lambda ident: {'id': 'source'})
    monkeypatch.setattr(podcast_live, 'context_for', lambda *args, **kwargs: 'Synthetic episode')
    def create(**kwargs):
        order.append(('create', kwargs['position']))
        return 'history1'
    monkeypatch.setattr(podcast_history, 'create', create)
    monkeypatch.setattr(podcast_history, 'finish', lambda *args: order.append(('finish', args)))
    def connect(cfg, key, provider):
        order.append(('provider', provider))
        return transport.provider, 'openai'
    monkeypatch.setattr(mod, '_connect_voice', connect)
    transport.handler.path += '?podcast_artifact=episode&position=12&revision=' + revision
    mod.serve(transport.handler)
    if revision == 'stale':
        assert order == []
        assert transport.errors == [(400, 'Invalid podcast artifact, revision or playhead')]
    else:
        assert order == [('create', 12.0), ('provider', 'openai'), ('finish', ('history1', 'closed'))]
        events = [json.loads(payload) for opcode, payload in frames(transport.handler) if opcode == ws.OP_TEXT]
        assert events[0] == {'type': 'podcast.history', 'conversation_id': 'history1', 'saved': True, 'position': 12.0}
        assert transport.provider.sent[0]['session']['input'][0]['content'][0]['text'].endswith('Synthetic episode')
        assert transport.provider.closed


@pytest.mark.parametrize('ending', ['pump_failure', 'poll_failure', 'takeover', 'cleanup_failure'])
def test_disconnect_and_cleanup_failures_release_device(transport, monkeypatch, ending):
    errors = []
    checkpoint = []
    observed = threading.Event()
    monkeypatch.setattr(mod, 'log_exception', lambda code, exc, detail: errors.append((code, detail)))
    if ending == 'pump_failure':
        transport.provider.events.put(OSError('lost provider'))
    elif ending == 'poll_failure':
        def tick(self):
            observed.set()
            raise RuntimeError('poll failed')
        monkeypatch.setattr(mod.Conversation, 'tick', tick)
    elif ending == 'cleanup_failure':
        def fail_checkpoint(self):
            checkpoint.append(True)
            raise OSError('checkpoint failed')
        monkeypatch.setattr(mod.Conversation, 'checkpoint', fail_checkpoint)
        def fail_close():
            raise OSError('close failed')
        monkeypatch.setattr(transport.provider, 'close', fail_close)

    def read(stream):
        if ending == 'takeover':
            mod._STOP_HOOKS[transport.handler._request_principal][1]()
        elif ending == 'poll_failure':
            assert observed.wait(3), 'result poll never ran'
        return None
    monkeypatch.setattr(ws, 'read_frame', read)
    mod.serve(transport.handler)
    assert not transport.errors
    if ending == 'takeover':
        assert transport.handler.connection.shutdowns
        assert [e['type'] for e in transport.provider.sent] == ['session.start']
    elif ending == 'cleanup_failure':
        assert checkpoint == [True]
        assert errors == [('oracleV2Close', "checkpointing the call's end"),
                          ('oracleV2Close', 'closing the upstream')]
    elif ending == 'pump_failure':
        # The fixture joins the receive worker before restoring dependencies.
        for worker in threading.enumerate():
            if worker.name == 'oracle-v2-receive':
                worker.join(3)
        assert errors == [('oracleV2Pump', 'upstream receive failed')]
    else:
        assert observed.is_set()
        assert transport.handler.connection.shutdowns
