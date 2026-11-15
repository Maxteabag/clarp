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
        # The Python Host answers this in two ways (15 Nov 2026, runs 43678950870 and
        # 43679413710): the reply is either the row it wrote while the turn ran, with the turn's
        # end on it, or the row it imported from the tool's transcript, without. Reported as a
        # defect of the reference; compared, not counted.
        'unsteady': True,
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
        'id': 'send-after-stop',
        'what': 'after a Stop the queue is held; the user writes again',
        'steps': [
            send(SLOW + 'synthetic task to stop', 'kilo'),
            {'op': 'wait_busy', 'busy': True},
            post('/stop', {'session': AGENT}),
            {'op': 'wait_busy', 'busy': False},
            {'op': 'quiesce', 'held': True},
            send('synthetic after the stop lima', 'lima'),
            {'op': 'quiesce', 'held': True},
            LOG,
            QUEUE,
            SNAPSHOT,
        ],
    },
    {
        'id': 'turn-timer',
        'what': 'what a list of agents counts a running turn from (issue 354): the agent is busy, and the '
                "turn's start stays what it is while more is sent and is another for the next turn",
        'steps': [
            {**SNAPSHOT, 'name': 'snapshot at rest'},
            send(SLOW + 'synthetic timed november', 'november'),
            {'op': 'wait_running', 'message': 'u-november'},
            {**SNAPSHOT, 'name': 'snapshot while the first turn runs'},
            send(SLOW + 'synthetic timed oscar', 'oscar', queue_if_busy=True),
            {**SNAPSHOT, 'name': 'snapshot after more was sent'},
            {'op': 'wait_reply', 'after': 'u-november'},
            {'op': 'wait_running', 'message': 'u-oscar'},
            {**SNAPSHOT, 'name': 'snapshot while the second turn runs'},
            {'op': 'wait_reply', 'after': 'u-oscar'},
            {'op': 'quiesce'},
            {**SNAPSHOT, 'name': 'snapshot at rest again'},
        ],
    },
    {
        'id': 'turn-timer-after-stop',
        'what': 'a turn that runs after one that was stopped is counted from its own start (issue 354)',
        'steps': [
            send(SLOW + 'synthetic timed papa', 'papa'),
            {'op': 'wait_running', 'message': 'u-papa'},
            post('/stop', {'session': AGENT}),
            {'op': 'wait_busy', 'busy': False},
            {'op': 'quiesce', 'held': True},
            {**SNAPSHOT, 'name': 'snapshot after the stop'},
            send(SLOW + 'synthetic timed quebec', 'quebec'),
            {'op': 'wait_running', 'message': 'u-quebec'},
            {**SNAPSHOT, 'name': 'snapshot while the next turn runs'},
            {'op': 'wait_reply', 'after': 'u-quebec'},
            {'op': 'quiesce', 'held': True},
            {**SNAPSHOT, 'name': 'snapshot at rest'},
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
        'id': 'voice-markup',
        'what': 'a reply with voice markup in it: what the conversation, the list of chats and the notification show',
        'steps': [
            # The fake answers with the message, so the reply carries the markup too. Nothing is
            # asked to be spoken: this is about what is shown.
            send('<speak>synthetic <vox>um,</vox> spoken part. <break time="300ms"/> [sigh] done.</speak> '
                 'and a written part <team>for the team</team>', 'mike'),
            {'op': 'wait_reply', 'after': 'u-mike'},
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
            {**get(f'/log?session={AGENT}', 'log after the first turn'), 'save': {'first': 'latest_revision'}},
            send('synthetic page two', 'two'),
            {'op': 'wait_reply', 'after': 'u-two'},
            {'op': 'quiesce'},
            {**get(f'/log?session={AGENT}&limit=2', 'log newest two'),
             'save': {'cursor': 'latest_revision'}},
            get(f'/log?session={AGENT}&limit=2&before=u-two', 'log before the second message'),
            get(f'/log?session={AGENT}&after_revision=0&limit=3', 'log first three by revision'),
            get(f'/log?session={AGENT}&after_revision={{cursor}}', 'log after the cursor'),
            # A client that was away since the first turn catches up, one row at a time.
            {**get(f'/log?session={AGENT}&after_revision={{first}}&limit=1', 'log one row after the first turn'),
             'save': {'next': 'latest_revision'}},
            get(f'/log?session={AGENT}&after_revision={{next}}', 'log the rest'),
        ],
    },
]

BY_ID = {scenario['id']: scenario for scenario in SCENARIOS}
