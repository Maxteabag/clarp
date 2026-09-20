"""Authenticated WebRTC call creation and server-owned Oracle sideband tools."""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import queue
import threading
import time
import uuid
from urllib.parse import urlparse

from . import agents, config, message_store, oracle_delegations
from .oracle_diagnostics import OracleJournal
from .oracle_webrtc import ConversationController

from .oracle_prompt import PROMPT  # noqa: E402  -- one prompt for every engine



def session_config(*, model, voice, fallback='', transcription=''):
    from .oracle_realtime import _ORACLE_TOOLS, _READ_MESSAGES_TOOL, _tool
    tools = [dict(t) for t in _ORACLE_TOOLS if t['name'] != 'get_agent_status']
    for tool in tools:
        if tool['name'] == 'cancel_agent':
            tool['description'] += ' Optional request replaces cancelled work atomically.'
            tool['parameters'] = {**tool['parameters'], 'properties': {
                **tool['parameters']['properties'], 'request': {'type': 'string', 'maxLength': 16000}}}
    tools = tools + [_READ_MESSAGES_TOOL, _tool(
        'investigate_with_oracle', 'Hand the full request to the configured Oracle contact. They can read files, investigate, use normal Clarp tools and organize other agents. Use for anything not addressed to a named agent.',
        {'request': {'type': 'string'}}, ['request'])]
    value = {'type': 'realtime', 'model': model, 'instructions': PROMPT,
             'output_modalities': ['audio'], 'max_output_tokens': 4096,
             'audio': {'input': {'noise_reduction': {'type': 'far_field'},
                               'turn_detection': {'type': 'semantic_vad', 'eagerness': 'low',
                                                  'create_response': True, 'interrupt_response': True}},
                       'output': {'voice': voice}}, 'tools': tools, 'tool_choice': 'auto'}
    if transcription:
        value['audio']['input']['transcription'] = {'model': transcription}
    return value


def validate_offer(sdp):
    if not isinstance(sdp, str) or len(sdp) > 128000 or not sdp.startswith('v=0') or 'm=audio ' not in sdp:
        raise ValueError('A bounded audio SDP offer is required')
    return sdp


def _age_text(timestamp: str) -> str:
    """'2 minutes ago', '3 days ago' or '' for an unparseable timestamp."""
    from datetime import datetime, timezone
    try:
        then = datetime.fromisoformat(str(timestamp).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return ''
    seconds = max(0, (datetime.now(timezone.utc) - then).total_seconds())
    for unit, size in (('day', 86400), ('hour', 3600), ('minute', 60)):
        if seconds >= size:
            count = int(seconds // size)
            return f"{count} {unit}{'s' if count != 1 else ''} ago"
    return 'just now'


def _agent_status(agent: dict) -> dict:
    """What one agent is doing right now, for a spoken one-liner.

    Reads the latest recorded state and the newest assistant message so
    "what is Omar doing" has an answer instead of a silent receipt.
    """
    latest = agents.latest_state(agent['agent_id']) or {}
    kind = str(latest.get('kind') or 'idle')
    busy = kind in ('thinking', 'tool', 'spawned')
    detail = latest.get('detail') if isinstance(latest.get('detail'), dict) else {}
    summary = str(detail.get('summary') or detail.get('tool') or detail.get('message') or '')[:200]
    since = _age_text(datetime_from_ms(latest.get('ts')))
    rows = message_store.list_messages(agent_id=agent['agent_id'], limit=20, include_automated=False)
    last_said = next((r for r in reversed(rows) if r['role'] == 'assistant' and str(r['text']).strip()), None)
    return {
        'agent': agent['persona'], 'session': agent['session'],
        'state': 'working' if busy else kind, 'since': since,
        'current_step': summary,
        'last_said': str(last_said['text'])[:400] if last_said else '',
        'last_said_age': _age_text(last_said['timestamp']) if last_said else '',
        'note': 'Say what they are doing in one sentence; mention the age if it is more than an hour.',
    }


def datetime_from_ms(value) -> str:
    from datetime import datetime, timezone
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return ''


class AgentTools:
    def __init__(self, ctx, principal, fallback, stop):
        self.ctx, self.principal, self.fallback, self.stop = ctx, principal, fallback, stop
        self.delegations: set[str] = set()
        self.lock = threading.Lock()
        self.supersede = lambda agent: None

    def roster(self):
        return [a for a in agents.list_agents() if not a.get('archived_at') and not a.get('is_janitor')]

    def resolve(self, name):
        name = str(name or '').strip().casefold()
        found = [a for a in self.roster() if name in (a['session'].casefold(), a['persona'].casefold())]
        if len(found) != 1:
            raise ValueError('Unknown or ambiguous agent; use a session from list_agents')
        return found[0]

    def execute(self, name, arguments, call_id):
        if name == 'list_agents':
            return {'agents': [{'name': a['persona'], 'session': a['session'],
                                'backend': a['backend'], 'personality': str(a.get('personality') or '')[:500]}
                               for a in self.roster()], 'oracle_contact': self.fallback or None,
                    'note': 'These are the agents you can reach; oracle_contact is your main colleague.'}
        if name == 'investigate_with_oracle':
            if not self.fallback:
                return {'error': 'No Oracle contact selected; ask which contact should investigate'}
            agent = self.resolve(self.fallback)
        else:
            agent = self.resolve(arguments.get('agent'))
        if name == 'get_agent_status':
            return _agent_status(agent)
        if name == 'read_agent_messages':
            rows = message_store.list_messages(agent_id=agent['agent_id'], limit=20, include_automated=False)
            selected = [r for r in rows if r['role'] in ('user', 'assistant')][-10:]
            budget, output = 12000, []
            for r in reversed(selected):
                text = str(r['text'])[:min(2000, budget)]
                if not text: break
                budget -= len(text)
                output.append({'id': r['id'], 'role': r['role'], 'timestamp': r['timestamp'], 'text': text})
            newest = max((r['timestamp'] for r in output), default='')
            return {'agent': agent['persona'], 'messages': list(reversed(output)),
                    'newest_message_age': _age_text(newest),
                    'note': 'Summarize in one natural sentence and say how old the newest message is.'}
        if name == 'cancel_agent':
            follow = str(arguments.get('request') or '').strip()
            if len(follow) > 16000:
                raise ValueError('Request must contain 1 to 16000 characters')
            rows = oracle_delegations.cancel_for_session(agent['session'],
                stop=lambda: self.stop(agent['session']), owner_principal=self.principal)
            self.supersede(agent['session'])
            if follow:
                from . import turn_queue
                turn_queue.set_paused(agent['agent_id'], False)
                ident = 'rtc-' + hashlib.sha256(
                    (self.principal + ':follow:' + call_id).encode()).hexdigest()[:40]
                row = oracle_delegations.dispatch(
                    ctx=self.ctx, delegation_id=ident, session=agent['session'],
                    request_text=follow, authenticated_at_admission=True,
                    owner_principal=self.principal)
                with self.lock:
                    self.delegations.add(ident)
                return {'status': row['status'],
                        'note': 'Handed off, not finished. Tell the user what you asked and who is doing it.'}
            return {'cancelled': True, 'note': 'Confirm the cancellation in one sentence.'}
        if name not in ('delegate_to_agent', 'investigate_with_oracle'):
            raise ValueError('Unknown Oracle tool')
        request = str(arguments.get('request') or '').strip()
        if not request or len(request) > 16000:
            raise ValueError('Request must contain 1 to 16000 characters')
        ident = 'rtc-' + hashlib.sha256((self.principal + ':' + call_id).encode()).hexdigest()[:40]
        row = oracle_delegations.dispatch(ctx=self.ctx, delegation_id=ident,
            session=agent['session'], request_text=request, authenticated_at_admission=True,
            owner_principal=self.principal)
        with self.lock:
            self.delegations.add(ident)
        return {'status': row['status'], 'note': 'Handed off, not finished. Tell the user what you asked and who is doing it.'}

    def results(self):
        with self.lock:
            ids = tuple(self.delegations)
        # Query exact current IDs: historical rows cannot starve current results.
        return [r for ident in ids if (r := oracle_delegations.get(ident)) and r['status'] in oracle_delegations.TERMINAL]


class Sideband:
    def __init__(self, socket, tools, journal):
        self.socket, self.tools, self.journal = socket, tools, journal
        self.incoming = queue.Queue()
        self.stopped = threading.Event()
        self.seen = set()
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix='oracle-tool')
        self.controller = ConversationController(send=self.send,
            acknowledge=lambda ident: oracle_delegations.acknowledge(ident, owner_principal=tools.principal))
        tools.supersede = lambda session: self.incoming.put(('supersede', session))

    def send(self, value):
        if self.stopped.is_set(): return
        raw = json.dumps(value, separators=(',', ':'))
        if self.journal: self.journal.event('client', raw)
        self.socket.send(raw)

    def start(self):
        threading.Thread(target=self.read, daemon=True, name='oracle-sideband-read').start()
        threading.Thread(target=self.run, daemon=True, name='oracle-sideband-control').start()

    def read(self):
        try:
            while not self.stopped.is_set():
                raw = self.socket.recv()
                if not raw: break
                if self.journal: self.journal.event('server', raw)
                self.incoming.put(('event', json.loads(raw)))
        except Exception as exc:
            if self.journal: self.journal.record('sideband.failed', {'error_type': type(exc).__name__})
        finally:
            self.incoming.put(('close', None))

    def execute(self, event):
        try:
            arguments = json.loads(event.get('arguments') or '{}')
            if not isinstance(arguments, dict): raise ValueError('Tool arguments must be an object')
            output = self.tools.execute(event['name'], arguments, event['call_id'])
        except Exception as exc:
            output = {'error': str(exc)[:500]}
        speak = event.get('name') not in (
            'delegate_to_agent', 'investigate_with_oracle', 'cancel_agent')
        if isinstance(output, dict) and (output.get('need_choice') or output.get('error')):
            speak = True
        self.incoming.put(('tool', (event['call_id'], json.dumps(output), speak)))

    def run(self):
        next_poll = 0
        try:
            while not self.stopped.is_set():
                try: kind, value = self.incoming.get(timeout=.2)
                except queue.Empty: kind, value = '', None
                if kind == 'close': break
                if kind == 'event':
                    if value.get('type') == 'response.function_call_arguments.done':
                        call = value.get('call_id')
                        if call and call not in self.seen:
                            self.seen.add(call)
                            self.controller.tool_started()
                            self.pool.submit(self.execute, value)
                    self.controller.event(value)
                elif kind == 'supersede':
                    self.controller.drop_agent(value)
                elif kind == 'tool':
                    call_id, output, *rest = value
                    speak = rest[0] if rest else True
                    self.controller.tool_finished(call_id, output, speak=speak)
                if time.monotonic() >= next_poll:
                    next_poll = time.monotonic() + 1
                    for row in self.tools.results():
                        if row['status'] == 'cancelled':
                            continue
                        agent = agents.get_by_session(row['session'])
                        text = row['result_text'] if row['status'] == 'completed' else row['error'] or f"Work {row['status']}"
                        if self.journal and (row['result_message_id'] or row['delegation_id']) not in self.controller.results:
                            self.journal.record('result.ready', {'delegation_id': row['delegation_id'], 'result_message_id': row['result_message_id']})
                        self.controller.add_result(row['delegation_id'], row['result_message_id'] or row['delegation_id'],
                                                   row['session'], text)
                    self.controller.flush()
        finally:
            self.close()

    def close(self):
        if self.stopped.is_set(): return
        self.stopped.set()
        self.controller.close()
        self.pool.shutdown(wait=False, cancel_futures=True)
        try: self.socket.close()
        except Exception: pass
        if self.journal: self.journal.close()


_CALLS = {}
_LOCK = threading.Lock()


def create_call(*, ctx, principal, attempt_id, sdp, fallback, stop):
    import urllib.request
    import websocket
    validate_offer(sdp)
    attempt_id = oracle_delegations.normalize_id(attempt_id)
    tools = AgentTools(ctx, principal, fallback, stop)
    if fallback: tools.resolve(fallback)
    digest = hashlib.sha256(sdp.encode()).hexdigest()
    with _LOCK:
        previous = _CALLS.get(principal)
        if previous and previous['attempt'] == attempt_id:
            if previous['digest'] != digest: raise ValueError('Attempt reused with a different SDP')
            if previous.get('response'): return previous['response']
            raise ValueError('Previous call creation is pending or failed; start a new attempt')
        if previous and previous.get('sideband'): previous['sideband'].close()
        record = {'attempt': attempt_id, 'digest': digest}
        _CALLS[principal] = record
    cfg = config.load()
    key = cfg.openai_key()
    if not key: raise ValueError('Oracle requires an OpenAI key on this Host')
    contract = session_config(model=cfg.openai_realtime_model, voice=cfg.openai_realtime_voice,
        fallback=fallback, transcription=getattr(cfg, 'openai_realtime_transcription_model', '') if cfg.oracle_diagnostics else '')
    boundary = 'oracle-' + uuid.uuid4().hex
    body = b''
    for name, value in [('sdp', sdp), ('session', json.dumps(contract))]:
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n').encode()
    body += f'--{boundary}--\r\n'.encode()
    request = urllib.request.Request('https://api.openai.com/v1/realtime/calls', data=body,
        headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'multipart/form-data; boundary=' + boundary})
    with urllib.request.urlopen(request, timeout=25) as response:
        answer = response.read(256000).decode()
        call_id = urlparse(response.headers.get('Location', '')).path.rsplit('/', 1)[-1]
    if not call_id.startswith('rtc_'): raise ValueError('Provider omitted Realtime call identity')
    socket = websocket.create_connection('wss://api.openai.com/v1/realtime?call_id=' + call_id,
        header=['Authorization: Bearer ' + key], timeout=15, enable_multithread=True)
    socket.settimeout(None)
    journal = OracleJournal() if cfg.oracle_diagnostics else None
    if journal:
        journal.record('session.open', {'transport': 'webrtc', 'call_id': call_id, 'attempt_id': attempt_id})
        journal.event('client', json.dumps({'type': 'session.update', 'session': contract}))
    sideband = Sideband(socket, tools, journal)
    with _LOCK:
        if _CALLS.get(principal) is not record:
            sideband.close()
            raise ValueError('Oracle connection superseded')
        record['sideband'] = sideband
        record['response'] = {'sdp': answer, 'call_id': call_id, 'attempt_id': attempt_id}
    sideband.start()
    return record['response']


def close_call(principal, attempt_id):
    with _LOCK:
        record = _CALLS.get(principal)
        if record and record['attempt'] == attempt_id:
            _CALLS.pop(principal, None)
            if record.get('sideband'): record['sideband'].close()


def call_results(principal, attempt_id):
    with _LOCK:
        record = _CALLS.get(principal)
        sideband = record.get('sideband') if record and record['attempt'] == attempt_id else None
    if not sideband: return []
    with sideband.tools.lock:
        ids = tuple(sideband.tools.delegations)
    return [row for ident in ids if (row := oracle_delegations.get(ident))]
