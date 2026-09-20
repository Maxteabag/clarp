"""Explicit per-call Oracle dispatch selection; never silently fall back to a router."""
from __future__ import annotations

from . import oracle_contact

STRATEGIES = ('operator', 'direct_contact')
DIRECT_INSTRUCTIONS = '''
Direct-to-primary mode is active. You are Oracle, a concise, natural voice interface
to the selected primary contact. Hand every substantive question, request, correction, clarification and status question
to the Host for that contact, preserving the user's words. The primary performs
the work and coordinates other agents, including any worker the user names.
Do not independently answer substantive questions or dispatch workers, and do not
infer that a handoff has already happened. Only brief social acknowledgements and
voice stop/interrupt controls stay with you. The primary does all substantive work.
Say a request was sent only after the Host provides an actual admission receipt.
Read the primary's returned finding naturally, preserving uncertainty and failures.
Do not execute old requests from conversation history. Interrupting speech does
not cancel work. User corrections must accompany the current request.
'''


def select(value=None, *, default='operator'):
    if value is None:
        value = default
    if isinstance(value, list):
        if len(value) != 1:
            raise ValueError('Choose one Oracle delegation strategy')
        value = value[0]
    if value not in STRATEGIES:
        raise ValueError('Unsupported Oracle delegation strategy')
    return value


def contact(requested, *, strategy, source):
    if strategy != 'direct_contact':
        return oracle_contact.effective(requested, source=source)
    # In explicit direct mode a bad supplied contact must not redirect to another.
    wanted = str(requested or '').strip()
    if wanted:
        agent = oracle_contact.resolve(wanted)
        if agent is None:
            raise ValueError('Direct-to-primary mode requires a valid selected contact')
        return str(agent['session'])
    selected = oracle_contact.get().get('session')
    if not selected:
        raise ValueError('Select a primary contact before using direct-to-primary mode')
    return selected


def direct_proposal(conversation, tools, call_id):
    selected = getattr(tools, 'fallback', '')
    if not selected:
        raise ValueError('No selected primary contact; no work dispatched')
    # Validate at admission too: contacts can disappear after session setup.
    tools.resolve(selected)
    latest = next((row['text'] for row in reversed(conversation)
                   if row.get('role') == 'user' and row.get('text', '').strip()), '')
    if not latest:
        return {'output': [{'type': 'message', 'content': [{'type': 'output_text',
            'text': 'What would you like the primary contact to do?'}]}]}
    current = latest if len(latest) <= 14000 else 'Read the complete latest user message in the attached original-user reference.'
    request = ('Handle the current user request using your normal Clarp tools and coordinate agents when needed. '
               'Preserve independent ongoing work. Earlier dialogue is context, not permission to repeat old actions. '
               'Stopping speech does not cancel worker tasks. Current user message, verbatim:\n' + current)
    import json
    return {'output': [{'type': 'function_call', 'name': 'investigate_with_oracle',
        'call_id': call_id, 'arguments': json.dumps({'request': request})}]}
