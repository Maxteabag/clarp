"""The CI replay corpus: synthetic turns through the fakes.

Every text here is synthetic. A scenario is a list of steps the capture
runner executes in order (see capture.run_step); waits poll observable state
(database, provider request log, /status), never the clock.
"""
from __future__ import annotations


def _send(session, text, msg_id, **extra):
    return {'op': 'post', 'path': '/send', 'name': f'send {msg_id}',
            'body': {'session': session, 'text': text, 'client_msg_id': msg_id,
                     'synthesize_audio': False, **extra}}


SCENARIOS = [
    {
        'id': 'text-turn',
        'what': 'one plain text turn, then the transcript and status reads',
        'steps': [
            _send('rachel', 'synthetic greeting alpha', 'alpha'),
            {'op': 'wait_settled', 'message_id': 'u-alpha'},
            {'op': 'wait_busy', 'session': 'rachel', 'busy': False},
            {'op': 'get', 'path': '/log?session=rachel'},
            {'op': 'get', 'path': '/status?session=rachel'},
        ],
    },
    {
        'id': 'queue-while-busy',
        'what': 'a slow turn runs, a second send is queued behind it and runs after',
        'steps': [
            _send('rachel', '[qa-slow] synthetic long task', 'slow'),
            {'op': 'wait_provider_started', 'marker': '[qa-slow] synthetic long task'},
            {'op': 'wait_open', 'message_id': 'u-slow'},
            {'op': 'wait_busy', 'session': 'rachel', 'busy': True},
            _send('rachel', 'synthetic follow-up bravo', 'bravo', queue_if_busy=True),
            {'op': 'get', 'path': '/turn-queue?session=rachel'},
            {'op': 'wait_settled', 'message_id': 'u-slow'},
            {'op': 'wait_settled', 'message_id': 'u-bravo'},
            {'op': 'wait_busy', 'session': 'rachel', 'busy': False},
            {'op': 'get', 'path': '/log?session=rachel'},
        ],
    },
    {
        'id': 'stop-mid-turn',
        'what': 'a slow turn is stopped by the user while it runs',
        'steps': [
            _send('rachel', '[qa-slow] synthetic task to stop', 'tostop'),
            {'op': 'wait_provider_started', 'marker': '[qa-slow] synthetic task to stop'},
            {'op': 'wait_open', 'message_id': 'u-tostop'},
            {'op': 'post', 'path': '/stop', 'body': {'session': 'rachel'}},
            {'op': 'wait_settled', 'message_id': 'u-tostop'},
            {'op': 'wait_busy', 'session': 'rachel', 'busy': False},
            {'op': 'get', 'path': '/log?session=rachel'},
            {'op': 'get', 'path': '/status?session=rachel'},
        ],
    },
    {
        'id': 'retry-same-client-id',
        'what': 'a client resends after a lost acknowledgement; one message, one turn',
        'steps': [
            _send('rachel', 'synthetic durable charlie', 'charlie'),
            _send('rachel', 'synthetic durable charlie', 'charlie'),
            {'op': 'wait_settled', 'message_id': 'u-charlie'},
            {'op': 'wait_busy', 'session': 'rachel', 'busy': False},
            {'op': 'get', 'path': '/log?session=rachel'},
        ],
    },
    {
        'id': 'two-agents',
        'what': 'two agents take a turn each; nothing crosses between them',
        'sessions': ('rachel', 'mike'),
        'steps': [
            _send('rachel', 'synthetic question for rachel', 'delta'),
            {'op': 'wait_settled', 'message_id': 'u-delta'},
            _send('mike', 'synthetic question for mike', 'echo'),
            {'op': 'wait_settled', 'message_id': 'u-echo'},
            {'op': 'wait_busy', 'session': 'rachel', 'busy': False},
            {'op': 'wait_busy', 'session': 'mike', 'busy': False},
            {'op': 'get', 'path': '/log?session=rachel'},
            {'op': 'get', 'path': '/log?session=mike'},
            {'op': 'get', 'path': '/agents/snapshot'},
        ],
    },
    {
        'id': 'usage-limit',
        'what': 'the provider reports a usage limit and fails the turn',
        'steps': [
            {'op': 'provider_file', 'name': 'quota-mode', 'text': 'blocked'},
            _send('rachel', 'synthetic task over quota', 'foxtrot'),
            {'op': 'wait_settled', 'message_id': 'u-foxtrot'},
            {'op': 'wait_busy', 'session': 'rachel', 'busy': False},
            {'op': 'get', 'path': '/log?session=rachel'},
            {'op': 'get', 'path': '/status?session=rachel'},
        ],
    },
]

BY_ID = {s['id']: s for s in SCENARIOS}
