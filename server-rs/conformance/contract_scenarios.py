"""The conformance corpus: what a client does, and nothing a client cannot see.

Every text here is synthetic. A scenario is a list of steps. The HTTP steps are
what is compared; the waits only watch for a state through the contract itself
(the snapshot and the conversation), the way a client would, so the same
scenario runs against any Host. The agent is `claire`, on the claude backend,
whose tool is the replay harness's deterministic `fake_claude.py`.
"""
from __future__ import annotations

AGENT = 'claire'


def send(text, msg_id, **extra):
    return {'op': 'post', 'path': '/send', 'name': f'send {msg_id}',
            'body': {'session': AGENT, 'text': text, 'client_msg_id': msg_id,
                     'synthesize_audio': False, **extra}}


def get(path, name=None):
    return {'op': 'get', 'path': path, **({'name': name} if name else {})}


def post(path, body, name=None):
    return {'op': 'post', 'path': path, 'body': body, **({'name': name} if name else {})}


LOG = get(f'/log?session={AGENT}')
SNAPSHOT = get('/agents/snapshot')
QUEUE = get(f'/turn-queue?session={AGENT}')
# The fake waits up to 3 s on this marker: a turn that is still running when the next step comes.
SLOW = '[qa-slow] '

SCENARIOS = [
    {
        'id': 'who-is-there',
        'what': 'what a client asks first: who the Host is and which agents it has',
        'steps': [
            get('/server-info'),
            SNAPSHOT,
            LOG,
        ],
    },
    {
        'id': 'text-turn',
        'what': 'one plain turn, then the conversation and the snapshot',
        'steps': [
            send('synthetic greeting alpha', 'alpha'),
            {'op': 'wait_reply', 'after': 'u-alpha'},
            {'op': 'quiesce'},
            LOG,
            SNAPSHOT,
        ],
    },
    {
        'id': 'retry-same-client-id',
        'what': 'a client resends after a lost acknowledgement: one message, one turn',
        'steps': [
            send('synthetic durable charlie', 'charlie'),
            {'op': 'wait_reply', 'after': 'u-charlie'},
            {'op': 'quiesce'},
            # The acknowledgement was lost; the client sends the same message again.
            {**send('synthetic durable charlie', 'charlie'), 'name': 'send charlie again'},
            {'op': 'quiesce'},
            LOG,
        ],
    },
    {
        'id': 'two-turns',
        'what': 'a second turn continues the conversation of the first',
        'steps': [
            send('synthetic first delta', 'delta'),
            {'op': 'wait_reply', 'after': 'u-delta'},
            {'op': 'quiesce'},
            send('synthetic second echo', 'echo'),
            {'op': 'wait_reply', 'after': 'u-echo'},
            {'op': 'quiesce'},
            LOG,
        ],
    },
    {
        'id': 'queue-while-busy',
        'what': 'a slow turn runs; a second send asks to wait, and runs after it',
        'steps': [
            send(SLOW + 'synthetic long task', 'slow'),
            {'op': 'wait_busy', 'busy': True},
            send('synthetic follow-up bravo', 'bravo', queue_if_busy=True),
            QUEUE,
            {'op': 'wait_reply', 'after': 'u-slow'},
            {'op': 'wait_reply', 'after': 'u-bravo'},
            {'op': 'quiesce'},
            LOG,
            QUEUE,
        ],
    },
    {
        'id': 'stop-mid-turn',
        'what': 'the user stops a turn while it runs',
        'steps': [
            send(SLOW + 'synthetic task to stop', 'tostop'),
            {'op': 'wait_busy', 'busy': True},
            post('/stop', {'session': AGENT}),
            {'op': 'wait_busy', 'busy': False},
            {'op': 'quiesce', 'held': True},
            LOG,
            SNAPSHOT,
            QUEUE,
        ],
    },
    {
        'id': 'stop-with-queue',
        'what': 'a Stop while a message waits: the turn ends, the queue is held, a resume runs it',
        'steps': [
            send(SLOW + 'synthetic task to stop', 'first'),
            {'op': 'wait_busy', 'busy': True},
            send('synthetic held golf', 'golf', queue_if_busy=True),
            post('/stop', {'session': AGENT}),
            {'op': 'wait_busy', 'busy': False},
            {'op': 'quiesce', 'held': True},
            QUEUE,
            SNAPSHOT,
            post('/turn-queue/resume', {'session': AGENT}),
            {'op': 'wait_reply', 'after': 'u-golf'},
            {'op': 'quiesce'},
            LOG,
            QUEUE,
        ],
    },
    {
        'id': 'send-while-busy',
        'what': 'a plain send while a turn runs takes its place',
        'steps': [
            send(SLOW + 'synthetic running task', 'hotel'),
            {'op': 'wait_busy', 'busy': True},
            send('synthetic replacement india', 'india'),
            {'op': 'wait_reply', 'after': 'u-india'},
            {'op': 'quiesce'},
            LOG,
            SNAPSHOT,
        ],
    },
    {
        'id': 'focus',
        'what': 'the open agent is chosen and the snapshot says so',
        'steps': [
            post('/select', {'session': AGENT}),
            SNAPSHOT,
        ],
    },
    {
        'id': 'nobody',
        'what': 'requests that name an agent the Host does not have',
        'steps': [
            get('/log?session=nobody'),
            post('/send', {'session': 'nobody', 'text': 'synthetic to nobody', 'client_msg_id': 'lost'}),
            post('/stop', {'session': 'nobody'}),
            post('/select', {'session': 'nobody'}),
            get('/turn-queue?session=nobody'),
        ],
    },
    {
        'id': 'log-pages',
        'what': 'a conversation read as a client reads it: the newest, what is older, what is newer',
        'steps': [
            send('synthetic page one', 'one'),
            {'op': 'wait_reply', 'after': 'u-one'},
            {'op': 'quiesce'},
            send('synthetic page two', 'two'),
            {'op': 'wait_reply', 'after': 'u-two'},
            {'op': 'quiesce'},
            {**get(f'/log?session={AGENT}&limit=2', 'log newest two'),
             'save': {'cursor': 'latest_revision'}},
            get(f'/log?session={AGENT}&limit=2&before=u-two', 'log before the second message'),
            get(f'/log?session={AGENT}&after_revision=0&limit=3', 'log first three by revision'),
            get(f'/log?session={AGENT}&after_revision={{cursor}}', 'log after the cursor'),
        ],
    },
]

BY_ID = {scenario['id']: scenario for scenario in SCENARIOS}
