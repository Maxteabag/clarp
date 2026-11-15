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
            {'op': 'wait_running'},
            {**SNAPSHOT, 'name': 'snapshot while the first turn runs'},
            send(SLOW + 'synthetic timed oscar', 'oscar', queue_if_busy=True),
            {**SNAPSHOT, 'name': 'snapshot after more was sent'},
            {'op': 'wait_reply', 'after': 'u-november'},
            {'op': 'wait_running'},
            {**SNAPSHOT, 'name': 'snapshot while the second turn runs'},
            {'op': 'wait_reply', 'after': 'u-oscar'},
            {'op': 'quiesce'},
            {**SNAPSHOT, 'name': 'snapshot at rest again'},
            LOG,
        ],
    },
    {
        'id': 'turn-timer-after-stop',
        'what': 'a turn that runs after one that was stopped is counted from its own start (issue 354)',
        'steps': [
            send(SLOW + 'synthetic timed papa', 'papa'),
            {'op': 'wait_running'},
            post('/stop', {'session': AGENT}),
            {'op': 'wait_busy', 'busy': False},
            {'op': 'quiesce', 'held': True},
            {**SNAPSHOT, 'name': 'snapshot after the stop'},
            send(SLOW + 'synthetic timed quebec', 'quebec'),
            {'op': 'wait_running'},
            {**SNAPSHOT, 'name': 'snapshot while the next turn runs'},
            {'op': 'wait_reply', 'after': 'u-quebec'},
            {'op': 'quiesce', 'held': True},
            {**SNAPSHOT, 'name': 'snapshot at rest'},
            LOG,
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
        'id': 'library',
        'what': 'what an agent made for its user: an artifact is made, read, listed, changed, pinned, archived '
                'and discarded, and what cannot be is refused',
        'steps': [
            post('/artifacts', {'session': AGENT, 'type': 'document', 'title': 'Synthetic notes',
                                'summary': 'What was found', 'artifact_id': 'notes-1',
                                'payload': {'content': '# Synthetic notes', 'kept': {'as': ['it', 'is']}}},
                 'make a document'),
            post('/artifacts', {'session': AGENT, 'type': 'data', 'title': 'Synthetic table', 'status': 'active',
                                'artifact_id': 'table-1', 'reference_id': 'ref-1',
                                'payload': {'columns': ['name', 'count'], 'rows': [['a', 1], ['b', 2.5]],
                                            'chart': {'kind': 'bar', 'value_column': 'count'}, 'progress': 0.5}},
                 'make a table'),
            post('/artifacts', {'session': AGENT, 'type': 'plan', 'title': 'Synthetic'}, 'a kind that is not made this way'),
            post('/artifacts', {'session': 'nobody', 'type': 'document', 'title': 'Synthetic',
                                'payload': {'content': 'x'}}, 'for nobody'),
            post('/artifacts', {'session': AGENT, 'type': 'document', 'title': 'Synthetic'}, 'with nothing to show'),
            post('/artifacts', {'session': AGENT, 'type': 'document', 'title': 'Synthetic again',
                                'artifact_id': 'notes-1', 'payload': {'content': 'x'}}, 'under an id that is taken'),
            post('/artifacts', {'session': AGENT, 'type': 'workflow_run', 'title': 'Synthetic run',
                                'payload': {'provider': 'github', 'run_id': '7', 'workflow_name': 'Tests',
                                            'run_url': 'https://example.org/o/r/actions/runs/7'}},
                 'a run that is not on GitHub'),
            {'op': 'post', 'path': '/artifacts', 'body': ['not', 'an', 'object'], 'name': 'with a body that is no object'},
            get('/artifacts/notes-1'),
            get('/artifacts/notes-1?representation=flat-v1', 'the document, flat'),
            get('/artifacts/no-such'),
            get(f'/artifacts?session={AGENT}'),
            get(f'/artifacts?session={AGENT}&representation=summary-v1&order=created', 'the library, summed up'),
            get('/artifacts?type=data&search=TABLE', 'the library, searched'),
            get('/artifacts?representation=other', 'the library, in a representation nobody knows'),
            get('/artifacts?limit=many', 'the library, with a limit that is no number'),
            post('/artifacts/table-1', {'payload_patch': {'progress': 1}, 'status': 'completed',
                                        'summary': 'All of it'}, 'change the table'),
            post('/artifacts/table-1', {'status': 'sleeping'}, 'a status nobody knows'),
            post('/artifacts/table-1', {'payload': {'columns': ['name'], 'rows': [['a', 'b']]}}, 'rows that do not fit'),
            post('/artifacts/no-such', {'status': 'ready'}, 'change what is not there'),
            post('/artifacts/notes-1/pin', {'pinned': True}),
            post('/artifacts/notes-1/pin', {'pinned': True}, 'pin it again'),
            post('/artifacts/notes-1/pin', {'pinned': 'yes'}, 'pin with no truth value'),
            post('/artifacts/no-such/pin', {'pinned': True}, 'pin what is not there'),
            get('/pinned-artifacts'),
            {'op': 'post', 'path': '/artifacts/notes-1/archive', 'body': {'archived': True},
             'body_from_last': {'expected_updated_at': 'artifacts.0.updated_at'}, 'name': 'archive it'},
            post('/artifacts/notes-1/archive', {'archived': False, 'expected_updated_at': 1}, 'restore it, as it once was'),
            post('/artifacts/notes-1/archive', {'archived': 'yes', 'expected_updated_at': 1}, 'archive with no truth value'),
            post('/artifacts/notes-1/archive', {'archived': True}, 'archive without saying when it was seen'),
            get('/artifacts/notes-1', 'the document, archived'),
            {'op': 'post', 'path': '/artifacts/notes-1/discard', 'body': {},
             'body_from_last': {'expected_updated_at': 'artifact.updated_at'}, 'name': 'discard it'},
            get('/artifacts/notes-1', 'the document, discarded'),
            get('/pinned-artifacts', 'nothing is pinned'),
            get(f'/artifacts?session={AGENT}', 'the library, after'),
        ],
    },
    {
        'id': 'approval',
        'what': 'an agent asks its user for an approval; it is in the inbox; the user gives it; the agent is told',
        'steps': [
            post('/decisions', {'session': AGENT, 'title': 'Synthetic deploy?',
                                'question': 'Deploy the synthetic thing?', 'context': 'Only the synthetic service.',
                                'yes_label': 'Deploy', 'no_label': 'Hold', 'reference_id': 'deploy-1',
                                'payload': {'version': '1.2'}}, 'ask'),
            get('/attention'),
            {'op': 'post', 'path': '/decisions/{decision}/resolve', 'from_last': {'decision': 'items.0.decision_id'},
             'body': {'choice': 'accepted', 'expected_revision': 2}, 'name': 'answer what was not seen'},
            get('/attention', 'the inbox, still'),
            {'op': 'post', 'path': '/decisions/{decision}/resolve', 'from_last': {'decision': 'items.0.decision_id'},
             'body': {'expected_revision': 1}, 'name': 'answer with nothing'},
            get('/attention', 'the inbox, again'),
            {'op': 'post', 'path': '/decisions/{decision}/resolve', 'from_last': {'decision': 'items.0.decision_id'},
             'body': {'choice': 'accepted', 'expected_revision': 1}, 'name': 'give the approval'},
            {'op': 'quiesce'},
            get('/attention', 'the inbox, empty'),
            get(f'/artifacts?session={AGENT}&type=decision'),
            LOG,
            SNAPSHOT,
        ],
    },
    {
        'id': 'question',
        'what': 'an agent asks a question with choices; the user chooses; the agent is told what was chosen',
        'steps': [
            post('/decisions', {'session': AGENT, 'title': 'Synthetic choice', 'question': 'Which synthetic way?',
                                'response_type': 'single_choice', 'recommended_option_id': 'slow',
                                'options': [{'id': 'slow', 'label': 'The slow way', 'description': 'Sure'},
                                            {'id': 'fast', 'label': 'The fast way'}],
                                'blocks_progress': True, 'priority_reason': 'Nothing else can go on',
                                'response_effort': 'quick'}, 'ask'),
            post('/decisions', {'session': AGENT, 'title': 'Synthetic', 'question': 'Which?',
                                'response_type': 'single_choice', 'options': [{'id': 'only', 'label': 'One'}]},
                 'ask with one choice'),
            post('/decisions', {'session': AGENT, 'title': 'Synthetic', 'question': 'Now?', 'urgency': 'time_sensitive'},
                 'ask urgently without a reason'),
            get('/attention', 'the inbox, as an app that knows only approvals asks'),
            get('/attention?decision_format=2'),
            {'op': 'post', 'path': '/decisions/{decision}/resolve', 'from_last': {'decision': 'items.0.decision_id'},
             'body': {'answer': {'option_id': 'fast'}, 'expected_revision': 1}, 'name': 'choose'},
            {'op': 'quiesce'},
            get('/attention?decision_format=2', 'the inbox, empty'),
            LOG,
        ],
    },
    {
        'id': 'requests-unanswered',
        'what': 'a request the user discards and one the agent takes back: neither is told to the agent',
        'steps': [
            post('/decisions', {'session': AGENT, 'title': 'Synthetic first', 'question': 'The first?'}, 'ask the first'),
            {'op': 'post', 'path': '/decisions/{decision}/dismiss', 'from_last': {'decision': 'artifact.decision.decision_id'},
             'body': {'expected_revision': 1}, 'name': 'discard it'},
            post('/decisions', {'session': AGENT, 'title': 'Synthetic second', 'question': 'The second?',
                                'response_type': 'text_input', 'input_hint': 'one_time_code'}, 'ask the second'),
            {'op': 'post', 'path': '/decisions/{decision}/withdraw', 'from_last': {'decision': 'artifact.decision.decision_id'},
             'body': {'session': 'someone-else'}, 'name': 'someone else takes it back'},
            get('/attention?decision_format=2'),
            {'op': 'post', 'path': '/decisions/{decision}/withdraw', 'from_last': {'decision': 'items.0.decision_id'},
             'body': {'session': AGENT}, 'name': 'the agent takes it back'},
            post('/decisions/no-such/dismiss', {'expected_revision': 1}, 'discard what is not there'),
            post('/decisions/no-such/resolve', {'choice': 'accepted', 'expected_revision': 1}, 'answer what is not there'),
            {'op': 'quiesce'},
            get('/attention?decision_format=2', 'the inbox, empty'),
            get(f'/artifacts?session={AGENT}&order=created'),
            LOG,
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
    {
        'id': 'who-is-told',
        'what': 'a turn that ends tells the user, without a push when the user asked at the desk; '
                'what another agent or a schedule began tells nobody',
        'steps': [
            send('synthetic told sierra', 'sierra', client='desktop'),
            {'op': 'wait_reply', 'after': 'u-sierra'},
            {'op': 'quiesce'},
            send('synthetic told tango', 'tango', origin='agent'),
            {'op': 'wait_reply', 'after': 'u-tango'},
            {'op': 'quiesce'},
            send('synthetic told uniform', 'uniform', origin='schedule'),
            {'op': 'wait_reply', 'after': 'u-uniform'},
            {'op': 'quiesce'},
            send('synthetic told victor', 'victor', origin='automation'),
            {'op': 'wait_reply', 'after': 'u-victor'},
            {'op': 'quiesce'},
            send('synthetic told whiskey', 'whiskey', origin='watcher'),
            {'op': 'wait_reply', 'after': 'u-whiskey'},
            {'op': 'quiesce'},
            # An origin nobody may set from outside is a person's.
            send('synthetic told xray', 'xray', origin='janitor'),
            {'op': 'wait_reply', 'after': 'u-xray'},
            {'op': 'quiesce'},
            LOG,
            SNAPSHOT,
        ],
    },
]

BY_ID = {scenario['id']: scenario for scenario in SCENARIOS}
