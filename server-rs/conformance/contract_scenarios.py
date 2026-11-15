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
# A helper the scenarios' agent makes, as the agents' own tool asks for one (`clarp-admin agent
# create`): a working identity, without a voice and without an announcement.
HELPER = 'helper-one'
MAKE_HELPER = {'name': 'Helper One', 'backend': 'claude', 'cwd': '{workspace}', 'synthesize_audio': False,
               'voice_id': '{}'}

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
            # A message that says nothing is refused before it is asked whom it is for.
            post('/send', {'session': 'nobody', 'text': '   ', 'client_msg_id': 'empty'}, 'send nothing to nobody'),
            post('/send', {'session': AGENT, 'text': '', 'client_msg_id': 'void'}, 'send nothing'),
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
    {
        'id': 'routine-turns',
        'what': "what a watcher or the Host's own routine began is marked, shown in its own words, "
                'left out of a chat that is read without automation, and never the last thing said',
        'steps': [
            send('synthetic routine alfa', 'alfa'),
            {'op': 'wait_reply', 'after': 'u-alfa'},
            {'op': 'quiesce'},
            {**LOG, 'name': 'log after the first turn', 'save': {'first': 'latest_revision'}},
            # A Host whose heartbeats are off takes none from outside either.
            send('synthetic routine bravo', 'bravo', origin='heartbeat'),
            # A dreaming run that found nothing to do says so with its token.
            send('synthetic DREAMING_OK charlie', 'charlie', origin='dreaming'),
            {'op': 'wait_reply', 'after': 'u-charlie'},
            {'op': 'quiesce'},
            send('synthetic routine delta', 'delta', origin='dreaming'),
            {'op': 'wait_reply', 'after': 'u-delta'},
            {'op': 'quiesce'},
            send('synthetic routine echo', 'echo', origin='watcher'),
            {'op': 'wait_reply', 'after': 'u-echo'},
            {'op': 'quiesce'},
            {**SNAPSHOT, 'name': 'snapshot after routine turns'},
            LOG,
            get(f'/log?session={AGENT}&include_automated=0', 'log without automation'),
            get(f'/log?session={AGENT}&include_automated=false&limit=1', 'log without automation, one row'),
            get(f'/log?session={AGENT}&include_automated=0&after_revision={{first}}',
                'log without automation, after the first turn'),
            send('synthetic routine foxtrot', 'foxtrot'),
            {'op': 'wait_reply', 'after': 'u-foxtrot'},
            {'op': 'quiesce'},
            get(f'/log?session={AGENT}&include_automated=0&limit=3', 'log without automation, three rows'),
            get(f'/log?session={AGENT}&include_automated=0&limit=3&before=u-foxtrot',
                'log without automation, before the last message'),
            {**SNAPSHOT, 'name': 'snapshot at the end'},
        ],
    },
    {
        'id': 'roster-edits',
        'what': 'the user changes who an agent is: whether it pages, its name, whether it is put away, '
                'its model and effort; a name is one agent\'s; and what is refused',
        'steps': [
            # Whether what it says pages the user.
            post('/agent-mute', {'session': AGENT}, 'mute without saying whether'),
            post('/agent-mute', {'muted': True}, 'mute without saying whom'),
            post('/agent-mute', {'session': 'nobody-there', 'muted': True}, 'mute an agent that is not there'),
            post('/agent-mute', {'session': AGENT, 'muted': True}, 'mute'),
            {**SNAPSHOT, 'name': 'snapshot of the muted agent'},
            send('synthetic roster alfa', 'ralfa'),
            {'op': 'wait_reply', 'after': 'u-ralfa'},
            {'op': 'quiesce'},
            # Muted is what is plainly true.
            post('/agent-mute', {'session': AGENT, 'muted': 'yes'}, 'mute with a word'),
            # Its name. The reference Host has an agent called Rachel, and so has the Rust Host here.
            post('/agent-rename', {'name': 'Nobody'}, 'rename without saying whom'),
            post('/agent-rename', {'session': AGENT, 'name': '   '}, 'rename to nothing'),
            post('/agent-rename', {'session': AGENT, 'name': 'n' * 61}, 'rename to too much'),
            post('/agent-rename', {'session': 'nobody-there', 'name': 'Nobody'},
                 'rename an agent that is not there'),
            post('/agent-rename', {'session': AGENT, 'name': 'RACHEL'}, 'rename to a name that is taken'),
            post('/agents', {'name': 'rachel', 'backend': 'codex'}, 'make an agent under a name that is taken'),
            post('/agent-rename', {'session': AGENT, 'name': '  Claire   the  Second '}, 'rename'),
            {**SNAPSHOT, 'name': 'snapshot of the renamed agent'},
            post('/agent-rename', {'session': AGENT, 'name': 'Claire the Second'}, 'rename to the name it has'),
            post('/agent-rename', {'session': AGENT, 'name': 'claire'}, 'rename to its first name in small letters'),
            post('/agent-rename', {'session': AGENT, 'name': 'Claire'}, 'rename back'),
            # Put away, and taken out again.
            post('/agent-archive', {'session': AGENT, 'archived': 'yes'}, 'put away with a word'),
            post('/agent-archive', {'archived': True}, 'put away without saying whom'),
            post('/agent-archive', {'session': 'nobody-there', 'archived': True},
                 'put away an agent that is not there'),
            post('/agent-archive', {'session': AGENT, 'archived': True}, 'put away'),
            {**SNAPSHOT, 'name': 'snapshot with the agent put away'},
            get('/agents/snapshot?include_archived=0', 'snapshot without the agents put away'),
            get('/agents/snapshot?include_archived=false', 'snapshot asked for in another word'),
            get('/agents/archived', 'the agents put away'),
            get('/agents/archived?limit=1', 'the agents put away, one to a page'),
            get('/agents/archived?limit=0', 'a page of none'),
            get('/agents/archived?limit=201', 'a page of too many'),
            get('/agents/archived?limit=07', 'a page with a size not in plain digits'),
            get('/agents/archived?cursor=zzz', 'a page after no place'),
            # An agent that is put away can still be written to, and stays put away.
            send('synthetic roster bravo', 'rbravo'),
            {'op': 'wait_reply', 'after': 'u-rbravo'},
            {'op': 'quiesce'},
            {**SNAPSHOT, 'name': 'snapshot after a turn of the agent put away'},
            post('/agent-archive', {'session': AGENT, 'archived': False}, 'take out again'),
            get('/agents/snapshot?include_archived=0', 'snapshot with nobody put away'),
            # Which model, and how much effort.
            post('/agent-llm', {'model': 'synthetic-model-one'}, 'tune without saying whom'),
            post('/agent-llm', {'session': 'nobody-there', 'model': 'synthetic-model-one'},
                 'tune an agent that is not there'),
            post('/agent-llm', {'session': AGENT, 'model': 5}, 'a model that is no text'),
            post('/agent-llm', {'session': AGENT, 'effort': ['high']}, 'an effort that is no text'),
            post('/agent-llm', {'session': AGENT, 'effort': 'extreme'}, 'an effort the tool has not'),
            post('/agent-llm', {'session': AGENT, 'model': ' synthetic-model-one ', 'effort': 'HIGH'}, 'tune'),
            {**SNAPSHOT, 'name': 'snapshot of the tuned agent'},
            send('synthetic roster charlie', 'rcharlie'),
            {'op': 'wait_reply', 'after': 'u-rcharlie'},
            {'op': 'quiesce'},
            {**SNAPSHOT, 'name': 'snapshot after a turn of the tuned agent'},
            # Only what a request has is changed; nothing said for one is the tool's own choice.
            post('/agent-llm', {'session': AGENT, 'effort': None}, 'the tool chooses its effort'),
            post('/agent-llm', {'session': AGENT, 'model': ''}, 'the tool chooses its model'),
            post('/agent-llm', {'session': AGENT}, 'tune nothing'),
            LOG,
            SNAPSHOT,
        ],
    },
    {
        'id': 'helpers',
        'what': "an agent makes a helper: who made it, how its work stands and moves, what its maker's "
                'row says of it, and what is refused',
        # The helper's row is compared too.
        'sessions': (AGENT, HELPER),
        'steps': [
            post('/agents', {**MAKE_HELPER, 'role': 'boss'}, 'make an agent in a role there is not'),
            post('/agents', {**MAKE_HELPER, 'role': 'helper'}, 'make a helper nobody made'),
            post('/agents', {**MAKE_HELPER, 'role': 'helper', 'parent': 'nobody-there'},
                 'make a helper of an agent that is not there'),
            post('/agents', {**MAKE_HELPER, 'role': 'helper', 'parent': AGENT, 'session': HELPER}, 'make a helper'),
            get(f'/agent-helper-state?session={HELPER}', 'who made the helper and how its work stands'),
            get(f'/agent-helper-state?session={AGENT}', 'the same of its maker'),
            get('/agent-helper-state?session=nobody-there', 'the same of an agent that is not there'),
            get('/agent-helper-state', 'the same of nobody'),
            {**SNAPSHOT, 'name': 'snapshot with a helper at work'},
            # The maker hands it work: it is working already, nothing moves.
            send('synthetic helper alfa', 'halfa', session=HELPER, origin='agent', sender=AGENT),
            {'op': 'wait_reply', 'after': 'u-halfa', 'session': HELPER},
            {'op': 'quiesce', 'session': HELPER},
            # The helper writes to its maker: it has reported.
            send('synthetic helper bravo', 'hbravo', origin='agent', sender=HELPER),
            {'op': 'wait_reply', 'after': 'u-hbravo'},
            {'op': 'quiesce'},
            get(f'/agent-helper-state?session={HELPER}', 'the helper after it reported'),
            {**SNAPSHOT, 'name': 'snapshot with a helper that reported'},
            # The maker writes back: more work.
            send('synthetic helper charlie', 'hcharlie', session=HELPER, origin='agent', sender=AGENT),
            {'op': 'wait_reply', 'after': 'u-hcharlie', 'session': HELPER},
            {'op': 'quiesce', 'session': HELPER},
            get(f'/agent-helper-state?session={HELPER}', 'the helper with more work'),
            # Marked by hand: by the agent that made it, or by the user.
            post('/agent-helper-state', {'session': HELPER}, 'mark without a state'),
            post('/agent-helper-state', {'session': HELPER, 'state': 'abandoned'},
                 'mark with a state nobody may set'),
            post('/agent-helper-state', {'session': 'nobody-there', 'state': 'done'},
                 'mark an agent that is not there'),
            post('/agent-helper-state', {'session': AGENT, 'state': 'done'}, 'mark an agent that is no helper'),
            post('/agent-helper-state', {'session': HELPER, 'state': 'done', 'by': 'rachel'},
                 'mark as an agent that did not make it'),
            post('/agent-helper-state', {'session': HELPER, 'state': 'failed', 'by': AGENT},
                 'the maker marks it failed'),
            post('/agent-helper-state', {'session': HELPER, 'state': 'failed'}, 'mark it failed again'),
            post('/agent-helper-state', {'session': HELPER, 'state': 'running'}, 'the user opens it again'),
            post('/agent-helper-state', {'session': HELPER, 'state': 'done'}, 'the user marks it done'),
            post('/agent-helper-state', {'session': HELPER, 'state': 'failed'}, 'mark what is done failed'),
            get(f'/agent-helper-state?session={HELPER}', 'the helper when it is done'),
            {**SNAPSHOT, 'name': 'snapshot with a helper that is done'},
            # The maker is put away: a helper that is not finished loses it.
            post('/agent-helper-state', {'session': HELPER, 'state': 'running'}, 'the user opens it once more'),
            post('/agent-archive', {'session': AGENT, 'archived': True}, 'put the maker away'),
            get(f'/agent-helper-state?session={HELPER}', 'the helper whose maker is put away'),
            get('/agents/snapshot?include_archived=0', 'snapshot without the maker'),
            post('/agent-archive', {'session': AGENT, 'archived': False}, 'take the maker out again'),
            LOG,
            get(f'/log?session={HELPER}', "the helper's conversation"),
            SNAPSHOT,
        ],
    },
    {
        'id': 'failed-turns',
        'what': 'a turn the tool could not finish, once because the account is out and once because '
                'the tool died: what a client is shown, and that the next turn runs',
        'steps': [
            # The fake waits before it fails, so that the turn is seen running first.
            send(SLOW + '[qa-usage-limit] synthetic work kilo', 'kilo'),
            {'op': 'wait_busy', 'busy': True},
            {'op': 'wait_busy', 'busy': False},
            {'op': 'quiesce'},
            {**LOG, 'name': 'log after the account was out'},
            {**SNAPSHOT, 'name': 'snapshot after the account was out'},
            send(SLOW + '[qa-crash] synthetic work lima', 'lima'),
            {'op': 'wait_busy', 'busy': True},
            {'op': 'wait_busy', 'busy': False},
            {'op': 'quiesce'},
            {**LOG, 'name': 'log after the tool died'},
            {**SNAPSHOT, 'name': 'snapshot after the tool died'},
            # And the agent works on.
            send('synthetic greeting mike', 'mike'),
            {'op': 'wait_reply', 'after': 'u-mike'},
            {'op': 'quiesce'},
            LOG,
            SNAPSHOT,
            # A scenario grows at its end: what was compared before keeps its place and its name.
            # A turn that had begun to answer when its account was out: where its end is told.
            send(SLOW + '[qa-says-first] [qa-usage-limit] synthetic work november', 'november'),
            {'op': 'wait_busy', 'busy': True},
            {'op': 'wait_busy', 'busy': False},
            {'op': 'quiesce'},
            {**LOG, 'name': 'log after a turn that had begun to answer'},
            {**SNAPSHOT, 'name': 'snapshot after a turn that had begun to answer'},
            # A turn that works, and then a tool that dies without a word of how full the
            # conversation is: what the gauge shows then.
            send('synthetic greeting oscar', 'oscar'),
            {'op': 'wait_reply', 'after': 'u-oscar'},
            {'op': 'quiesce'},
            send(SLOW + '[qa-crash] synthetic work papa', 'papa'),
            {'op': 'wait_busy', 'busy': True},
            {'op': 'wait_busy', 'busy': False},
            {'op': 'quiesce'},
            {**SNAPSHOT, 'name': 'snapshot after a tool died that had worked before'},
            # Every row is read once more: a row a client has read has a name it can be held to
            # wherever else it is named (a notification names the row it is about).
            {**LOG, 'name': 'log at the end'},
        ],
    },
]

SCENARIOS.append({
    'id': 'retried-turns',
    'what': 'a connection that drops under a turn: the turn is tried again, and what a client is '
            'shown while it is and when it then works',
    # A turn whose connection drops every time is not here. The reference did not end such a turn
    # within the fifteen seconds the harness waits, in four recordings of four (runs 43741865162
    # and 43744222881: one "reconnecting", then nothing), and a scenario the reference does not
    # finish makes the whole run untrustworthy. What the Rust Host does then (three attempts,
    # then the turn says so) is held by its own test (`tests/host.rs`, a connection that drops).
    'steps': [
        # It drops once. The second process answers.
        send('[qa-drops:1] synthetic work quebec', 'quebec'),
        {'op': 'wait_reply', 'after': 'u-quebec'},
        {'op': 'quiesce'},
        {**LOG, 'name': 'log after a turn that was tried twice'},
        {**SNAPSHOT, 'name': 'snapshot after a turn that was tried twice'},
        # And the agent works on.
        send('synthetic greeting sierra', 'sierra'),
        {'op': 'wait_reply', 'after': 'u-sierra'},
        {'op': 'quiesce'},
        LOG,
        SNAPSHOT,
    ],
})

# A small picture in signs a body of text can carry. A Host reads of an image what its first
# bytes say: that it is a GIF, and how large it is (2 by 3, and 5 by 7).
GIF = 'GIF89a\x02\x00\x03\x00' + '\x00' * 6
OLD_GIF = 'GIF87a\x05\x00\x07\x00' + '\x00' * 6


def publish(text, declared, name, caption='', session=AGENT, step=None):
    """An agent's tool publishes a file: the file is the body as it is, what it is called is in
    the headers, written with %XX as the tool writes it."""
    headers = {'X-Session': session, 'X-File-Name': name, 'X-Caption': caption}
    return {'op': 'post', 'path': '/media', 'raw': {'bytes': text, 'content_type': declared},
            'headers': headers, 'name': step or f'publish {name}'}


SCENARIOS.append({
    'id': 'media',
    'what': 'a file an agent publishes: it is taken or refused, an image is in the agent\'s gallery, and what '
            'was taken is fetched whole and in parts',
    'steps': [
        {**publish(GIF, 'image/gif', 'tiny picture.gif', 'A%20tiny%20picture%20%5Bone%5D'),
         'save': {'gif': 'asset.asset_id'}},
        {**publish('%PDF-1.4\n1 0 obj\n', 'application/pdf', 'report.pdf'), 'save': {'pdf': 'asset.asset_id'}},
        # What is declared as nothing in particular is read by its name.
        {**publish('# Synthetic notes\n\nSome text.\n', 'application/octet-stream', 'notes.md'),
         'save': {'notes': 'asset.asset_id'}},
        # And an image by its first bytes, whatever it is declared as and called.
        publish(OLD_GIF, 'application/octet-stream', '', step='publish a picture that says nothing of itself'),
        publish('#!/bin/sh\necho synthetic\n', 'application/x-sh', 'run.sh', step='publish a program'),
        publish('not a pdf', 'application/pdf', 'fake.pdf', step='publish what is not what it says it is'),
        publish('', 'image/gif', 'empty.gif', step='publish nothing'),
        publish(GIF, 'image/gif', 'tiny.gif', session='nobody', step='publish for nobody'),
        get(f'/media?session={AGENT}', 'the gallery'),
        get(f'/media?session={AGENT}&limit=1', 'the gallery, one at most'),
        get(f'/media?session={AGENT}&limit=many', 'the gallery, with a limit that is no number'),
        get('/media?session=nobody', 'the gallery of nobody'),
        get('/media/{gif}', 'fetch the picture'),
        {**get('/media/{gif}', 'fetch a part of the picture'), 'headers': {'Range': 'bytes=0-5'}},
        {**get('/media/{gif}', 'fetch the end of the picture'), 'headers': {'Range': 'bytes=-4'}},
        {**get('/media/{gif}', 'fetch past the end of the picture'), 'headers': {'Range': 'bytes=100-'}},
        {**get('/media/{gif}', 'fetch a part nobody can read as one'), 'headers': {'Range': 'bytes=4-2'}},
        get('/media/{pdf}', 'fetch the report'),
        get('/media/{notes}', 'fetch the notes'),
        get('/media/asset_no_such_file', 'fetch what is not there'),
    ],
})

BY_ID = {scenario['id']: scenario for scenario in SCENARIOS}
