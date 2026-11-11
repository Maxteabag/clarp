"""Shared Oracle session configuration, status/transcript readers, and AgentTools."""
from __future__ import annotations

import hashlib
import threading

from . import agents, message_store, oracle_delegations

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


TRANSCRIPT_DEFAULT, TRANSCRIPT_MAX = 10, 30
TRANSCRIPT_MESSAGE_CHARS, TRANSCRIPT_BUDGET = 6000, 16000


def _recent_messages(agent: dict, *, limit: int, message_chars: int, budget: int, mark_cuts: bool):
    """The newest non-empty user and assistant messages, oldest first.

    Shared by read_agent_messages (a short spoken summary on the v1 tool
    contract, which never carried cut markers) and read_agent_transcript
    (sized for a verbatim read, every cut marked). Neither prompts the agent.
    Returns (messages, older_or_cut) where older_or_cut says the window or
    the budget left messages out.
    """
    rows = message_store.list_messages(agent_id=agent['agent_id'], limit=limit * 2, include_automated=False)
    # A tool-only or empty turn at the head must not hide the older history
    # from Oracle; only an exhausted budget stops the walk.
    chat = [r for r in rows if r['role'] in ('user', 'assistant') and str(r['text'])]
    selected = chat[-limit:]
    cut = len(chat) > len(selected)
    output = []
    for r in reversed(selected):
        if budget <= 0:
            cut = True
            break
        full = str(r['text'])
        text = full[:min(message_chars, budget)]
        budget -= len(text)
        message = {'id': r['id'], 'role': r['role'], 'timestamp': r['timestamp'], 'text': text}
        if mark_cuts and len(text) < len(full):
            message['truncated'] = True
        output.append(message)
    return list(reversed(output)), cut


def _agent_transcript(agent: dict, limit=None) -> dict:
    """read_agent_messages sized for a verbatim read; the caller relays it losslessly in parts."""
    if type(limit) is not int or limit < 1:
        limit = TRANSCRIPT_DEFAULT
    messages, cut = _recent_messages(agent, limit=min(limit, TRANSCRIPT_MAX),
                                     message_chars=TRANSCRIPT_MESSAGE_CHARS, budget=TRANSCRIPT_BUDGET,
                                     mark_cuts=True)
    newest = max((r['timestamp'] for r in messages), default='')
    return {'agent': agent['persona'], 'session': agent['session'], 'messages': messages,
            'newest_message_age': _age_text(newest), 'truncated': cut,
            'note': 'Read without prompting the agent. Read it word for word when the user asks for their words.'}


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
            messages, _ = _recent_messages(agent, limit=10, message_chars=2000, budget=12000, mark_cuts=False)
            newest = max((r['timestamp'] for r in messages), default='')
            return {'agent': agent['persona'], 'messages': messages,
                    'newest_message_age': _age_text(newest),
                    'note': 'Summarize in one natural sentence and say how old the newest message is.'}
        if name == 'read_agent_transcript':
            return _agent_transcript(agent, arguments.get('limit'))
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
                return {'status': row['status'], 'operation_id': ident, 'session': agent['session'],
                        'note': 'Handed off, not finished.'}
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
        return {'status': row['status'], 'operation_id': ident, 'session': agent['session'], 'note': 'Handed off, not finished.'}

    def results(self):
        with self.lock:
            ids = tuple(self.delegations)
        # Query exact current IDs: historical rows cannot starve current results.
        return [r for ident in ids if (r := oracle_delegations.get(ident)) and r['status'] in oracle_delegations.TERMINAL]
