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
        # A file, a recording and a film are artifacts that name a file their agent published.
        post('/artifacts', {'session': AGENT, 'type': 'file', 'title': 'Synthetic report', 'artifact_id': 'report-1',
                            'payload': {'url': '/media/{pdf}', 'mime_type': 'application/pdf',
                                        'file_name': 'report.pdf'}}, 'make a file of the report'),
        post('/artifacts', {'session': AGENT, 'type': 'audio', 'title': 'Synthetic recording',
                            'payload': {'url': '/media/{pdf}', 'mime_type': 'application/pdf',
                                        'file_name': 'report.pdf'}}, 'make a recording of what is none'),
        post('/artifacts', {'session': AGENT, 'type': 'file', 'title': 'Synthetic nothing',
                            'payload': {'url': '/media/asset_no_such_file', 'mime_type': 'application/pdf',
                                        'file_name': 'report.pdf'}}, 'make a file of what was not published'),
        post('/artifacts', {'session': AGENT, 'type': 'file', 'title': 'Synthetic elsewhere',
                            'payload': {'url': 'https://example.org/report.pdf', 'mime_type': 'application/pdf',
                                        'file_name': 'report.pdf'}}, 'make a file of what is elsewhere'),
        post('/artifacts', {'session': AGENT, 'type': 'file', 'title': 'Synthetic other name',
                            'payload': {'url': '/media/{pdf}', 'mime_type': 'application/pdf',
                                        'file_name': 'other.pdf'}}, 'make a file under another name'),
        get('/artifacts/report-1', 'the file of the report'),
    ],
})

PAGE_ONE = '<h1>Synthetic report</h1><p>Day one.</p>'
PAGE_TWO = '<h1>Synthetic report</h1><p>Day two, with a script.</p><script>document.title = "two"</script>'


def report(page, version, name, **more):
    """An agent publishes a report: a page of HTML that takes no answers, under an id of its own."""
    payload = {'content': page, 'version': version, 'read_only': True}
    return post('/artifacts', {'session': AGENT, 'type': 'html_form', 'title': 'Synthetic report',
                               'artifact_id': 'report-1', 'payload': payload, **more}, name)


SCENARIOS.append({
    'id': 'reports',
    'what': 'a report, a page of HTML an agent hands its user: it is published, read whole and in a summary, '
            'served to a browser, revised, and what it was is read back version by version',
    'steps': [
        report(PAGE_ONE, 'v1', 'publish a report', summary='Day one'),
        get('/artifacts/report-1', 'the report'),
        get('/artifacts/report-1?representation=flat-v1', 'the report, flat'),
        get(f'/artifacts?session={AGENT}&representation=summary-v1', 'the library, summed up'),
        get(f'/artifacts?session={AGENT}', 'the library'),
        get('/artifacts/report-1/html', 'the page as a browser is given it'),
        get('/artifacts/report-1/revisions', 'what the report was'),
        # Published again under its id: a revision, which says the version it follows.
        report(PAGE_TWO, 'v2', 'publish it again without the version it follows', summary='Day two'),
        report(PAGE_TWO, 'v2', 'publish it again after a version it does not have', summary='Day two',
               expected_version='v0'),
        report(PAGE_TWO, 'v2', 'publish it again', summary='Day two', expected_version='v1'),
        report(PAGE_ONE, 'v2', 'publish another page under a version it had', expected_version='v2'),
        # Changed in place: its page, what it may reach, what is said beside it.
        post('/artifacts/report-1', {'payload_patch': {'content': '<p>Day three.</p>', 'version': 'v3',
                                                       'connect_origins': ['https://EXAMPLE.org:443']},
                                     'summary': 'Day three'}, 'change the page'),
        post('/artifacts/report-1', {'payload_patch': {'version': 'v1'}}, 'back to a version that was another page'),
        post('/artifacts/report-1', {'payload_patch': {'read_only': False}}, 'a report that would take answers'),
        post('/artifacts/report-1', {'payload_patch': {'connect_origins': ['https://example.org/path'], 'version': 'v4'}},
             'a page that would reach what is not an origin'),
        post('/artifacts/report-1', {'status': 'completed'}, 'complete it'),
        get('/artifacts/report-1', 'the report, after'),
        get('/artifacts/report-1/revisions', 'what the report was, after'),
        get('/artifacts/report-1/revisions?version=v1', 'its first version'),
        get('/artifacts/report-1/revisions?version=nothing', 'a version it never had'),
        get('/artifacts/report-1/revisions?limit=1&offset=1', 'one of its versions'),
        get('/artifacts/report-1/revisions?limit=many', 'its versions, with a limit that is no number'),
        get('/artifacts/no-such/revisions', 'the versions of nothing'),
        get('/artifacts/report-1/html', 'the page as a browser is given it, after'),
        get('/artifacts/no-such/html', 'the page of nothing'),
        get(f'/artifacts?session={AGENT}&representation=summary-v1', 'the library, summed up, after'),
        # What is no report.
        post('/artifacts', {'session': AGENT, 'type': 'html_form', 'title': 'Synthetic',
                            'payload': {'content': PAGE_ONE, 'version': 'v 1', 'read_only': True}},
             'a version that is no name'),
        post('/artifacts', {'session': AGENT, 'type': 'html_form', 'title': 'Synthetic',
                            'payload': {'content': '', 'version': 'v1', 'read_only': True}}, 'a report without a page'),
        post('/artifacts', {'session': AGENT, 'type': 'html_form', 'title': 'Synthetic',
                            'payload': {'content': PAGE_ONE, 'version': 'v1', 'read_only': True,
                                        'answer_schema': {'type': 'object', 'properties': {'a': {'type': 'string'}}}}},
             'a report that declares answers'),
    ],
})

FORM_PAGE = '<form><label>When <select name="when"><option>now</option><option>later</option></select></label></form>'
FORM_SCHEMA = {'type': 'object', 'properties': {
    'when': {'type': 'string', 'enum': ['now', 'later']}, 'budget': {'type': 'number', 'minimum': 0},
    'notes': {'type': 'string', 'maxLength': 40}, 'steps': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 3}},
    'required': ['when'], 'additionalProperties': False}
# The ids a client mints for what it sends: the same on both Hosts.
ANSWERS_ONE = '0a1b2c3d-0000-4000-8000-000000000001'
ANSWERS_TWO = '0a1b2c3d-0000-4000-8000-000000000002'


def form(artifact_id, name, schema=None, **more):
    """An agent makes a form: a page that asks its user for answers, with what an answer is."""
    payload = {'content': FORM_PAGE, 'version': '1', 'answer_schema': FORM_SCHEMA if schema is None else schema}
    return post('/artifacts', {'session': AGENT, 'type': 'html_form', 'title': 'Synthetic plan: it\'s "here"',
                               'artifact_id': artifact_id, 'summary': 'What it asks', 'payload': payload, **more}, name)


def answers(artifact_id, submission_id, given, name, version='1', **more):
    """A user sends answers to a form, under an id the app chose."""
    return post(f'/artifacts/{artifact_id}/submit',
                {'submission_id': submission_id, 'version': version, 'answers': given, **more}, name)


SCENARIOS.append({
    'id': 'forms',
    'what': 'a form, a page that asks its user for answers: it is made once and never changed, shown whole and in a '
            'summary, sent answers that fit its schema and answers that do not, and its agent is told what its user '
            'answered, once',
    'steps': [
        form('form-1', 'make a form'),
        form('form-1', 'make it again'),
        get('/artifacts/form-1', 'the form'),
        get(f'/artifacts?session={AGENT}&representation=summary-v1', 'the library, summed up'),
        get('/artifacts/form-1/html', 'the page as a browser is given it'),
        get('/artifacts/form-1/revisions', 'the versions of a form'),
        post('/artifacts/form-1', {'payload_patch': {'version': '2'}}, 'change the form'),
        post('/artifacts/form-1', {'summary': 'What it asks, said better'}, 'change what is said of it'),
        # What a form is not.
        form('form-2', 'a schema that is none', schema={'type': 'object', 'properties': {'a': {'type': 'text'}}}),
        form('form-2', 'a schema that points elsewhere',
             schema={'type': 'object', 'properties': {'a': {'$ref': '#/$defs/a'}}}),
        form('form-2', 'a schema of something that is no object', schema={'type': 'string'}),
        # What is no submission, and what the form does not take.
        post('/artifacts/form-1/submit', {'submission_id': 'short', 'version': '1', 'answers': {}},
             'answers under an id that is none'),
        post('/artifacts/form-1/submit', {'submission_id': ANSWERS_ONE, 'version': '1', 'answers': ['now']},
             'answers that are a list'),
        answers('form-1', ANSWERS_ONE, {'when': 'now'}, 'answers with a wish that is no yes or no', synthesize_audio='yes'),
        answers('form-1', ANSWERS_ONE, {}, 'answers without what is required'),
        answers('form-1', ANSWERS_ONE, {'when': 'soon'}, 'an answer that is not among the choices'),
        answers('form-1', ANSWERS_ONE, {'when': 'now', 'budget': -1}, 'a number below what is allowed'),
        answers('form-1', ANSWERS_ONE, {'when': 'now', 'budget': '10'}, 'a number that is text'),
        answers('form-1', ANSWERS_ONE, {'when': 'now', 'notes': 'n' * 41}, 'a text that is too long'),
        answers('form-1', ANSWERS_ONE, {'when': 'now', 'steps': ['a', 'b', 'c', 'd']}, 'a list that is too long'),
        answers('form-1', ANSWERS_ONE, {'when': 'now', 'steps': ['a', 2]}, 'a list with what is no text'),
        answers('form-1', ANSWERS_ONE, {'when': 'now', 'colour': 'red', 'also': 1}, 'answers nobody asked for'),
        answers('form-1', ANSWERS_ONE, {'when': 'now'}, 'answers to another version', version='2'),
        answers('no-such-form', ANSWERS_ONE, {'when': 'now'}, 'answers to nothing'),
        post('/artifacts', {'session': AGENT, 'type': 'html_form', 'title': 'Synthetic report', 'artifact_id': 'report-1',
                            'payload': {'content': PAGE_ONE, 'version': 'v1', 'read_only': True}}, 'publish a report'),
        answers('report-1', ANSWERS_ONE, {}, 'answers to a report', version='v1'),
        LOG,
        # Answers that fit: a receipt, and the agent is told them as what its user said.
        answers('form-1', ANSWERS_ONE, {'when': 'now', 'budget': 12.5, 'notes': 'synthetic, "quoted" æøå',
                                        'steps': ['a', 'b']}, 'answers that fit', synthesize_audio=False),
        {'op': 'wait_reply', 'after': f'u-html-form-{ANSWERS_ONE}'},
        {'op': 'quiesce'},
        LOG,
        answers('form-1', ANSWERS_ONE, {'steps': ['a', 'b'], 'notes': 'synthetic, "quoted" æøå', 'budget': 12.5,
                                        'when': 'now'}, 'the same answers again, in another order'),
        answers('form-1', ANSWERS_ONE, {'when': 'later'}, 'other answers under the same id'),
        answers('form-1', ANSWERS_ONE, {'when': 'now', 'budget': 12.5, 'notes': 'synthetic, "quoted" æøå',
                                        'steps': ['a', 'b']}, 'the same answers with another wish', synthesize_audio=True),
        answers('no-such-form', ANSWERS_ONE, {'when': 'now'}, 'a used id, for a form that is not there'),
        # A form that is over takes no more; what it took is still answered.
        post('/artifacts/form-1', {'status': 'completed'}, 'complete the form'),
        answers('form-1', ANSWERS_TWO, {'when': 'later'}, 'answers to a form that is over'),
        answers('form-1', ANSWERS_ONE, {'when': 'now', 'budget': 12.5, 'notes': 'synthetic, "quoted" æøå',
                                        'steps': ['a', 'b']}, 'the first answers once more'),
        {'op': 'quiesce'},
        LOG,
        get('/artifacts/form-1', 'the form, after'),
    ],
})

def made(kind, title, status, name, **payload):
    """An agent makes something for its user."""
    return post('/artifacts', {'session': AGENT, 'type': kind, 'title': title, 'status': status, 'payload': payload}, name)


UPDATES = '/attention/inbox'
SCENARIOS.append({
    'id': 'updates',
    'what': 'Updates, what agents made that their user may want to look at: what failed first, then what is ready, '
            'then what is in the making, a page at a time, with a cursor that goes on from where a page ended as '
            'long as nothing that is shown has changed',
    'steps': [
        get(UPDATES, 'nothing was made yet'),
        made('document', 'Synthetic notes', 'ready', 'make notes', content='# Synthetic notes'),
        made('data', 'Synthetic table', 'active', 'make a table', columns=['a'], rows=[[1]]),
        made('code_change', 'Synthetic change', 'failed', 'make a change that failed', repository='owner/repo'),
        made('release', 'Synthetic draft', 'draft', 'make a draft', version='0.1'),
        made('directory', 'Synthetic folder', 'cancelled', 'make what is called off', root='workspace',
             relative_path='src'),
        made('workflow_run', 'Synthetic run', 'active', 'make a run of a workflow', provider='github', run_id='42',
             run_url='https://github.com/owner/repo/actions/runs/42', workflow_name='Tests'),
        post('/artifacts', {'session': AGENT, 'type': 'html_form', 'title': 'Synthetic report', 'artifact_id': 'report-1',
                            'payload': {'content': PAGE_ONE, 'version': 'v1', 'read_only': True}}, 'publish a report'),
        # (The request stands between two things that are ready: of two made in the same
        # thousandth of a second, which comes first is the order of their ids, each Host's own.)
        post('/decisions', {'session': AGENT, 'title': 'Synthetic approval', 'question': 'Proceed with the synthetic step?'},
             'ask for an approval'),
        made('research', 'Synthetic research', 'completed', 'make research that is done', content='Synthetic findings',
             sources=[{'title': 'A source', 'url': 'https://example.org/a'}]),
        get(UPDATES, 'the list'),
        get(f'{UPDATES}?representation=summary-v1', 'the list, summed up'),
        get(f'{UPDATES}?representation=flat-v1', 'the list, flat'),
        # A page at a time.
        get(f'{UPDATES}?limit=2', 'the first page'),
        {'op': 'get', 'path': UPDATES + '?limit=2&cursor={cursor}', 'from_last': {'cursor': 'next_cursor'},
         'name': 'the second page'},
        {'op': 'get', 'path': UPDATES + '?limit=2&cursor={cursor}', 'from_last': {'cursor': 'next_cursor'},
         'name': 'the third page'},
        get(f'{UPDATES}?limit=0', 'a page of nothing is a page of one'),
        get(f'{UPDATES}?limit=1000', 'a page of a thousand'),
        # What is no query.
        get(f'{UPDATES}?limit=many', 'a limit that is no number'),
        get(f'{UPDATES}?representation=nothing', 'a representation there is none of'),
        get(f'{UPDATES}?cursor=not-a-cursor', 'what is no cursor'),
        get(f'{UPDATES}?cursor=eyJvZmZzZXQiOiAwfQ==', 'a cursor without the list it is of'),
        get(f'{UPDATES}?cursor=eyJyZXZpc2lvbiI6ICJ4IiwgIm9mZnNldCI6IDB9', 'a cursor of a list that never was'),
        # What a user does with what is shown changes the list: a cursor of before is stale.
        # (`save` keeps something of an answer for a later step under a name of the scenario's own.)
        {**get(f'{UPDATES}?limit=2', 'the first page, before a change'), 'save': {'before': 'next_cursor'}},
        get(UPDATES + '?limit=2&cursor={before}', 'the second page, with nothing changed'),
        post('/artifacts/report-1/pin', {'pinned': True}, 'pin the report'),
        get(UPDATES + '?limit=2&cursor={before}', 'the second page, with a cursor of before the pin'),
        get(UPDATES, 'the list, with the report pinned'),
        post('/artifacts/report-1', {'status': 'failed'}, 'the report fails'),
        get(UPDATES, 'the list, with the report first'),
        post('/artifacts/report-1', {'status': 'expired'}, 'the report runs out'),
        get(UPDATES, 'the list, without the report'),
    ],
})

# What an app says of itself: one running copy of it (an id it made), counting what it reports.
ACTIVITY = {'instance_id': '0a1b2c3d-0000-4000-8000-0000000000aa', 'sequence': 1, 'foreground': True, 'input_age_ms': 0}


def activity(name, sent=True, **changed):
    """An app reports whether it is in front. It says when it sent the report."""
    step = post('/application-activity', {**ACTIVITY, **changed}, name)
    return {**step, 'body_now': ['sent_at_ms']} if sent else step


def attached(text, name, upload_id='synthetic-upload-1'):
    """An app hands over a file its user attached to what they say: the file is the body, what
    it is called and the id the app gave it are in the headers."""
    headers = {'X-Session': AGENT, 'X-File-Name': 'synthetic%20note', 'X-Upload-ID': upload_id}
    return {'op': 'post', 'path': '/upload', 'raw': {'bytes': text, 'content_type': 'text/markdown'},
            'headers': headers, 'name': name}


SCENARIOS.append({
    'id': 'app-asks',
    'what': 'what an app asks of any Host, whatever the Host says it has: whether it is up, which agent its user '
            'is looking at, whether the app is in front, the lines of its own log, and the lists its screens are '
            'made of',
    'steps': [
        get('/status', 'is the Host up'),
        get(f'/status?session={AGENT}', 'is the agent busy'),
        get('/status?session=nobody-here', 'is nobody busy'),
        post('/focus', {'session': AGENT}, 'the user looks at the agent'),
        SNAPSHOT,
        post('/focus', {}, 'the user looks at nothing'),
        post('/focus', {'session': 'nobody-here'}, 'the user looks at nobody'),
        activity('the app is in front'),
        activity('the same report again'),
        activity('the app is behind', sequence=2, foreground=False),
        activity('a report of long ago', sent=False, sequence=3, sent_at_ms=1),
        activity('a report of what has no id', instance_id='not-an-id'),
        activity('a report without its place', sequence=0),
        activity('a report that says neither front nor behind', sequence=4, foreground='yes'),
        activity('a report with an input that is no number', sequence=5, input_age_ms='now'),
        post('/application-activity', ['front'], 'a report that is a list'),
        post('/clog', {'event': 'synthetic-event', 'detail': 'a synthetic line'}, 'a line of the log of the app'),
        post('/clog', {'events': [{'event': 'synthetic-one', 'detail': {'k': 1}}, {'event': 'synthetic-two'}]},
             'lines of the log of the app'),
        get('/teams', 'the teams'),
        get('/janitors', 'the janitors'),
        get('/automation-settings', 'what the Host does by itself'),
        get('/transcription-capabilities', 'what the Host transcribes with'),
        get('/transcription-providers', 'who transcribes'),
        get('/voice-catalog', 'the voices'),
        get('/agent-model-options', 'the models an agent can be given'),
        get('/controller-actions', 'what a button of a controller can run'),
        get('/controller-actions/audit', 'what was defined and run'),
        get('/controller-actions/audit?limit=many', 'an audit of no number of rows'),
        # The faces agents are shown with: the pictures a Host ships. The snapshot above says
        # where each agent's is; here one is fetched as anybody may fetch it.
        get('/static/avatars/rachel.png', 'the picture of a name'),
        {**get('/static/avatars/rachel.png', 'a part of the picture'), 'headers': {'Range': 'bytes=0-7'}},
        {**get('/static/avatars/rachel.png', 'the picture, for one who has it'), 'headers': {'If-None-Match': '*'}},
        get('/static/avatars/default-claude.png', 'the picture of a kind of tool'),
        get('/static/avatars/nobody-of-the-kind.png', 'a picture that is not there'),
        # What a user attaches: kept once under the id the app gave it, and answered with where.
        attached('synthetic attachment one', 'the user attaches a file'),
        attached('synthetic attachment one', 'the same file again'),
        attached('synthetic attachment two', 'another file under the same id'),
        attached('synthetic attachment one', 'a file under an id that is none', upload_id='two words'),
        attached('', 'a file of nothing', upload_id='synthetic-upload-2'),
        # Asked last: a step that is put in before others moves the places the registered
        # differences of this scenario are named by.
        post('/crash', {'crashDiagnostics': [{'synthetic': 'a stack'}], 'hangDiagnostics': []},
             "what a phone's system told an app of itself"),
        post('/crash', [1, 2], 'a report that is no object'),
    ],
})

SCENARIOS.append({
    'id': 'tool-turns',
    'what': 'a turn in which the agent uses its tools (a command, a file read and a search at once, an edit, a '
            'command that fails, a tool no Host knows by name): what the conversation shows of each use, whole and '
            'in the short form an app asks for, and the details of one card when it is opened',
    'steps': [
        send('[qa-tools] synthetic work with tools', 'tools-1'),
        {'op': 'wait_reply', 'after': 'u-tools-1'},
        {'op': 'quiesce'},
        LOG,
        SNAPSHOT,
        get(f'/log?session={AGENT}&include_tool_details=0', 'the conversation without the details of its tools'),
        {'op': 'get', 'path': '/message-tool-details?session=' + AGENT + '&message_id={row}',
         'from_last': {'row': 'turns.1.id'}, 'name': 'the details of the first card'},
        get(f'/message-tool-details?session={AGENT}&message_id=nothing-of-the-kind', 'the details of nothing'),
        get(f'/message-tool-details?session={AGENT}', 'the details of no message'),
        get('/message-tool-details?session=nobody-here&message_id=u-tools-1', 'the details of what nobody has'),
        # A second turn with tools: its cards are its own.
        send('[qa-tools] more synthetic work', 'tools-2'),
        {'op': 'wait_reply', 'after': 'u-tools-2'},
        {'op': 'quiesce'},
        get(f'/log?session={AGENT}&include_tool_details=0', 'both turns without the details of their tools'),
        LOG,
    ],
})

# What the reference Host's stand-in for an engine that listens hears in every clip
# (`StubSTT` in tests/replay/host.py). The Rust Host's worker is told to hear the same.
HEARD = 'replay voice prompt'


def spoken(name, clip='synthetic-audio-bytes-one', **headers):
    """A clip of speech handed to the Host: made-up bytes, with what the app says of them."""
    return {'op': 'post', 'path': '/transcribe', 'name': name,
            'raw': {'content_type': 'audio/webm', 'bytes': clip}, 'headers': headers}


SPEECH_SETTINGS = '/transcription-providers'
UTTERANCE = {'X-Transcription-ID': 'utt-one', 'X-Utterance-ID': 'utt-one', 'X-Transcription-Session': AGENT,
             'X-Hands-Free': '1'}

SCENARIOS.append({
    'id': 'speech-in',
    'what': 'a person speaks: a clip becomes text and the text is sent as their message under the trace the clip '
            'was given; the same request again, another clip under the same name, a clip nobody named, a name '
            'that is none, an engine nobody installed, and what the Host says it listens with',
    'steps': [
        spoken('a clip becomes text', **UTTERANCE),
        {**send(HEARD, 'spoken-1', utterance_id='utt-one'), 'name': 'what was said is sent',
         'body_from_last': {'trace_id': 'trace_id'}},
        {'op': 'wait_reply', 'after': 'u-spoken-1'},
        {'op': 'quiesce'},
        LOG,
        SNAPSHOT,
        spoken('the same request again', **UTTERANCE),
        spoken('another clip under the same name', clip='synthetic-audio-bytes-two', **UTTERANCE),
        spoken('the same clip, not said hands-free', **{**UTTERANCE, 'X-Hands-Free': '0'}),
        spoken('a clip nobody named'),
        spoken('a clip under another name', **{**UTTERANCE, 'X-Transcription-ID': 'utt-two'}),
        spoken('a name that is none', **{'X-Transcription-ID': 'has space'}),
        spoken('an engine nobody installed', **{'X-Transcription-Model': 'nothing-of-the-kind'}),
        spoken('the engine of the Host by its plain name', **{'X-Transcription-Model': 'server-default'}),
        get('/transcription-capabilities', 'what the Host listens with'),
        get('/transcription-providers', 'who listens'),
        # The app has sent what was said and will not ask for the answer again.
        {'op': 'delete', 'path': '/transcription-results/utt-one', 'name': 'the answer is no longer wanted'},
        spoken('the same name for what is said next', clip='synthetic-audio-bytes-two', **UTTERANCE),
        {'op': 'delete', 'path': '/transcription-results/has%20space', 'name': 'forget under a name that is none'},
        {'op': 'delete', 'path': '/transcription-results/utt-one', 'name': 'forgotten once more'},
        # The owner's settings of speech in, made from an app: what is refused and in which
        # words, and an agent's own language and engines, given and taken away again. No clip
        # is spoken while the agent has engines of its own: the reference Host's stand-in for
        # an engine is none of them.
        post(SPEECH_SETTINGS, {'language': 'english'}, 'a language that is none'),
        post(SPEECH_SETTINGS, {'agent_languages': {AGENT: 'no,en,pt,sv'}}, 'four languages for one agent'),
        post(SPEECH_SETTINGS, {'agent_engines': {AGENT: 'nothing:of-the-kind'}}, 'an engine nobody knows'),
        post(SPEECH_SETTINGS, {'agent_engines': {'': 'google:chirp_3'}}, 'engines for nobody'),
        post(SPEECH_SETTINGS, {'agent_engines': [AGENT]}, 'engines that are a list'),
        post(SPEECH_SETTINGS, {'engine': 3}, 'an engine that is a number'),
        post(SPEECH_SETTINGS, {'turn_taking': 'sometimes'}, 'a way of taking turns that is none'),
        post(SPEECH_SETTINGS, {'agent_languages': {AGENT.title(): 'NO,en'},
                               'agent_engines': {AGENT.title(): ' elevenlabs:scribe_v2 , google:chirp_3 '}},
             'an agent gets a language and engines of its own'),
        get(SPEECH_SETTINGS, 'the settings with the agent in them'),
        post(SPEECH_SETTINGS, {'agent_languages': {AGENT: None}, 'agent_engines': {AGENT: ''}},
             'the agent is heard like every agent again'),
    ],
})

def ack(name, **said):
    return post('/clips/ack', said, name)


SCENARIOS.append({
    'id': 'speech-out',
    'what': 'the routes of speech out when nothing was said aloud: no clip an app missed, what an app may not '
            'say of a clip, what is no clip and no file, and the clips of a reply that has none. (The '
            "reference says nothing aloud in this harness: its fake Claude writes no transcript. A clip's own "
            'routes are held by the Rust Host\'s tests.)',
    'steps': [
        get(f'/clips/recoverable?session={AGENT}', 'no clip was missed'),
        get('/clips/recoverable', 'no clip of anybody was missed'),
        ack('a status that is none', clip_id=1, status='played'),
        ack('a number that is none', clip_id='x', status='play-ok'),
        ack('a number and a status that are none', clip_id='x', status='played'),
        ack('a clip nobody has', clip_id=999999, status='play-ok'),
        ack('a clip nobody has, by its number as a text', clip_id='999999', status='queued'),
        ack('a clip nobody has, by its address', url='http://synthetic/audio/1794800000000__claire.mp3',
            status='play-start'),
        ack('nothing named', status='play-start'),
        get('/clips/abc/stream', 'a number that is none, as a stream'),
        get('/clips/abc/complete.mp3', 'a number that is none, whole'),
        get('/clips/999999/complete.mp3', 'a clip nobody has, whole'),
        get('/audio/nothing.mp3', 'a file that is not there'),
        get(f'/clips/message?session={AGENT}', 'the clips of a reply nobody names'),
        get(f'/clips/message?session={AGENT}&message_id=nothing-of-the-kind', 'the clips of a reply nobody has'),
    ],
})

VOICE = '/agent-voice'
# One of the ElevenLabs voices both Hosts list for the old settings screen: a voice an agent
# can be given on a Host where nothing speaks.
A_VOICE = 'nPczCjzI2devNBz1zQrb'

SCENARIOS.append({
    'id': 'voices',
    'what': "what an app's voice picker reads and sets on a Host where nothing speaks: the catalogue for an "
            'agent, an agent given a voice, a voice another agent has, what is no agent and no voice, a preview '
            'that cannot be made, and a report of speech that is no report',
    'steps': [
        get(f'/voice-catalog?for={AGENT}', 'the catalogue for an agent'),
        get(f'/voices?for={AGENT}', 'the voices of the old settings screen'),
        post(VOICE, {'session': AGENT}, 'a voice that is not named'),
        post(VOICE, {'voice_id': A_VOICE}, 'a voice for nobody'),
        post(VOICE, {'session': 'nobody-of-the-kind', 'voice_id': A_VOICE}, 'a voice for an agent nobody has'),
        post(VOICE, {'session': AGENT, 'provider': 'google', 'voice_id': 'Kore'}, 'a voice Google does not have'),
        post(VOICE, {'session': AGENT, 'provider': 'ElevenLabs', 'voice_id': A_VOICE}, 'an agent is given a voice'),
        get(f'/voice-catalog?for={AGENT}', 'the catalogue with the voice as this agent\'s'),
        get('/voice-catalog?for=rachel', 'the catalogue for another agent: the voice is taken'),
        post(VOICE, {'session': 'rachel', 'provider': 'elevenlabs', 'voice_id': A_VOICE}, 'a voice another agent has'),
        get('/voice-preview?provider=elevenlabs&id=' + A_VOICE, 'a preview on a Host where nothing speaks'),
        get('/voice-preview?provider=nothing&id=nothing', 'a preview of nothing'),
        post('/voice-events', {'events': 'none'}, 'a report of speech that is no list'),
        post('/voice-events', {'events': [{'event': 'tick'}] * 501}, 'a report that is too long'),
    ],
})

# Two agents made for it: one named as a persona every Host has built in, with the way of
# speaking that persona has, and one with a way of speaking of its own.
NOVA, DRY = 'nova', 'dry-one'
MAKE_TOLD = {'backend': 'claude', 'cwd': '{workspace}', 'synthesize_audio': False, 'voice_id': '{}'}
WHO = '[qa-who] '
SPEAKING, HUMAN, NARRATION = '/personalities/settings', '/voice-humanness', '/agent-voice-verbosity'


def who(text, msg_id, session):
    """A message the stand-in answers with who it was told the agent is, and the wait for it."""
    return [send(WHO + text, msg_id, session=session),
            {'op': 'wait_reply', 'after': f'u-{msg_id}', 'session': session},
            {'op': 'quiesce', 'session': session}]


SCENARIOS.append({
    'id': 'agent-is-told',
    'what': "who an agent's tool is told the agent is (its name, and how it speaks), under another name and "
            "with the ways of speaking turned off; and the owner's settings of how agents speak: whether they "
            'are told a way of speaking, how human a voice sounds, how much an agent says while it works',
    'sessions': (AGENT, NOVA, DRY),
    'steps': [
        *who('synthetic told alfa', 'whoalfa', AGENT),
        post('/agents', {**MAKE_TOLD, 'name': 'Nova', 'session': NOVA}, 'make an agent named as a persona'),
        *who('synthetic told bravo', 'whobravo', NOVA),
        get(f'/voices?for={NOVA}', 'what the persona of an agent is like, for the old settings screen'),
        post('/agents', {**MAKE_TOLD, 'name': 'Dry One', 'session': DRY,
                         'personality': '  Personality: synthetic and dry.  '},
             'make an agent with a way of speaking of its own'),
        *who('synthetic told charlie', 'whocharlie', DRY),
        # Under another name it speaks as it did.
        post('/agent-rename', {'session': NOVA, 'name': 'Novalis'}, 'another name for the agent'),
        *who('synthetic told delta', 'whodelta', NOVA),
        # Whether agents are told how they speak at all.
        get(SPEAKING, 'whether agents are told how they speak'),
        post(SPEAKING, [True], 'a change that is no object'),
        post(SPEAKING, {}, 'a change of nothing'),
        post(SPEAKING, {'enabled': 0}, 'the ways of speaking turned off'),
        get(SPEAKING, 'turned off'),
        *who('synthetic told echo', 'whoecho', NOVA),
        *who('synthetic told foxtrot', 'whofoxtrot', DRY),
        post(SPEAKING, {'enabled': 'yes'}, 'turned on by a word'),
        *who('synthetic told golf', 'whogolf', DRY),
        # How human a voice sounds: the Host's level, and levels of single agents.
        get(HUMAN, 'how human voices sound'),
        post(HUMAN, {'default': 7, 'agents': {' CLAIRE ': 2, 'nova': 10}}, 'a level for the Host and for two agents'),
        post(HUMAN, {'default': 11}, 'a level there is not'),
        post(HUMAN, {'default': '7'}, 'a level that is a text'),
        post(HUMAN, {'default': True}, 'a level that is a truth'),
        post(HUMAN, {'agents': {'': 4}}, 'a level for nobody'),
        post(HUMAN, {'agents': {'dry-one': 12}}, 'a level an agent cannot have'),
        post(HUMAN, {'agents': ['claire']}, 'agents that are no table'),
        post(HUMAN, ['seven'], 'a change that is no object'),
        post(HUMAN, {'agents': {'claire': None, 'nova': ''}}, 'two agents follow the Host again'),
        post(HUMAN, {}, 'a change of nothing'),
        get(HUMAN, 'how human voices sound afterwards'),
        # How much an agent says while it works.
        post(NARRATION, {'voice_verbosity': 1}, 'a level for nobody'),
        post(NARRATION, {'session': AGENT}, 'no level'),
        post(NARRATION, {'session': AGENT, 'voice_verbosity': '2'}, 'a level that is a text'),
        post(NARRATION, {'session': AGENT, 'voice_verbosity': True}, 'a level that is a truth'),
        post(NARRATION, {'session': AGENT, 'voice_verbosity': 2.5}, 'a level that is no whole number'),
        post(NARRATION, {'session': AGENT, 'voice_verbosity': 4}, 'a level there is not'),
        post(NARRATION, {'session': AGENT, 'voice_verbosity': -1}, 'a level below the least'),
        post(NARRATION, {'session': 'nobody-there', 'voice_verbosity': 1}, 'an agent that is not there'),
        post(NARRATION, {'session': AGENT, 'voice_verbosity': 3}, 'an agent says all it does'),
        post(NARRATION, {'session': AGENT, 'voice_verbosity': 0}, 'and is quiet again'),
        get(f'/log?session={NOVA}', 'the conversation of the agent named as a persona'),
        get(f'/log?session={DRY}', 'the conversation of the agent with its own way of speaking'),
        LOG,
        SNAPSHOT,
    ],
})

PERSONAS, CHANGED = '/personas', '/personas/update'
# Two pictures: "a made-up picture" and "another picture", as an app hands them over.
PICTURE, ANOTHER = 'YSBtYWRlLXVwIHBpY3R1cmU=', 'YW5vdGhlciBwaWN0dXJl'
# Nothing makes a picture for a persona in these rooms: on the Python Host that is asked for.
UNDRAWN = {'generate_avatar': False}

# An agent made as a persona in these rooms: nothing of it is said that its persona can say.
MAKE_AS = {'backend': 'claude', 'cwd': '{workspace}', 'synthesize_audio': False}

SCENARIOS.append({
    'id': 'personas',
    'what': 'the owner makes a persona, changes it and removes it; a persona is given a voice nobody has when its '
            'maker names none; it has a picture of its own; of a persona that is built in only the look changes; '
            'an agent made as a persona takes on what the persona has, and keeps it; and what is refused',
    'sessions': (AGENT, 'pictured', 'voiced'),
    'steps': [
        {**post(PERSONAS, {'name': '  Scout  ', 'avatar_symbol': 'star', **UNDRAWN,
                           'personality': 'Personality: synthetic and curious.'}, 'a persona is made'),
         'save': {'scout': 'persona.id'}},
        post(PERSONAS, {'name': 'scout', **UNDRAWN}, 'a name a persona of the owner has'),
        post(PERSONAS, {'name': 'MIKE', **UNDRAWN}, 'a name a built-in persona has'),
        post(PERSONAS, {'name': '   ', **UNDRAWN}, 'no name'),
        post(PERSONAS, {'name': 'x' * 61, **UNDRAWN}, 'too long a name'),
        post(PERSONAS, ['Scout'], 'a persona that is no object'),
        {**post(PERSONAS, {'name': 'Pictured', 'voice_id': '{}', 'avatar_base64': PICTURE, **UNDRAWN},
                'a persona with a picture, and the word for no voice'),
         'save': {'pictured': 'persona.id', 'face': 'persona.avatar_url'}},
        post(PERSONAS, {'name': 'Voiced', 'voice_id': '  plain-synthetic-voice  ', 'avatar_symbol': 's' * 70,
                        'personality': 'p' * 4100, **UNDRAWN}, 'a persona with a voice, and too much of the rest'),
        get('{face}', 'the picture of a persona'),
        {**get('/persona-avatars/{pictured}', 'a part of the picture'), 'headers': {'Range': 'bytes=2-8'}},
        get('/persona-avatars/{scout}', 'the picture of a persona that has a symbol'),
        get('/persona-avatars/persona-nobody-of-the-kind', 'the picture of nobody'),
        {**SNAPSHOT, 'name': 'snapshot with three personas of the owner'},
        # The owner changes one of his own.
        post(CHANGED, {'original_name': 'scout', 'name': 'Scout Two', 'voice_id': ''},
             'another name, and the voice it had'),
        post(CHANGED, {'original_name': 'Scout', 'name': 'Scout', 'voice_id': ''}, 'a persona that is not there'),
        post(CHANGED, {'original_name': 'Scout Two', 'name': '', 'voice_id': ''}, 'a change to no name'),
        post(CHANGED, {'original_name': 'Scout Two', 'name': 'pictured', 'voice_id': ''},
             'a change to a name another persona has'),
        post(CHANGED, {'original_name': 'Scout Two', 'name': 'Rachel', 'voice_id': ''},
             'a change to a name a built-in persona has'),
        post(CHANGED, {'original_name': 'Pictured', 'name': 'Pictured', 'voice_id': '{"cartesia":"synthetic-chosen"}',
                       'avatar_symbol': 'moon', 'personality': 'Personality: synthetic and slow.',
                       'avatar_base64': ANOTHER}, 'another voice, another picture, a symbol and a way of speaking'),
        get('/persona-avatars/{pictured}', 'the other picture'),
        post(CHANGED, ['Scout Two'], 'a change that is no object'),
        # Of a persona that is built in only the look changes, whatever else is said.
        post(CHANGED, {'original_name': 'mike', 'name': 'Michael', 'voice_id': 'another', 'avatar_symbol': 'bolt',
                       'personality': 'Personality: another.'}, 'a built-in persona is given a symbol'),
        post(CHANGED, {'original_name': 'Rachel', 'name': 'Rachel', 'voice_id': '', 'avatar_base64': PICTURE},
             'a built-in persona is given a picture'),
        get('/persona-avatars/builtin-rachel', 'the picture of a built-in persona'),
        get('/persona-avatars/builtin-mike', 'the picture of a built-in persona that has a symbol'),
        # An agent made as a persona takes on what the persona has: its voice, its way of
        # speaking, its symbol, its picture. What the request says of the agent comes first.
        {**post('/agents', {**MAKE_AS, 'name': 'Pictured', 'session': 'pictured'},
                'an agent made as a persona of the owner'), 'save': {'pictured_agent': 'agent.agent_id'}},
        get('/avatars/{pictured_agent}', 'the picture an agent has from its persona'),
        get('/avatars/pictured', 'the same, by the name of its session'),
        {**get('/avatars/pictured', 'a part of it'), 'headers': {'Range': 'bytes=-7'}},
        get('/avatars/nobody-of-the-kind', 'the picture of an agent that is not there'),
        get(f'/avatars/{AGENT}', 'the picture of an agent that has none of its own'),
        *who('synthetic told as a persona', 'whopictured', 'pictured'),
        post('/agents', {**MAKE_AS, 'name': 'Voiced', 'session': 'voiced', 'voice_id': '{}', 'avatar_symbol': 'leaf',
                         'personality': '  Personality: synthetic and its own.  ', 'avatar_base64': PICTURE},
             'an agent made as a persona, with a voice, a symbol, a way of speaking and a picture of its own'),
        get('/avatars/voiced', 'the picture an agent was given'),
        *who('synthetic told its own way', 'whovoiced', 'voiced'),
        # The owner removes one of his own, and nothing else. An agent that was made as it stays
        # who it is.
        {'op': 'delete', 'path': '/personas/Pictured', 'name': 'the persona an agent was made as is removed'},
        get('/avatars/pictured', 'the picture of the agent afterwards'),
        {'op': 'delete', 'path': '/personas/Scout%20Two', 'name': 'a persona is removed'},
        {'op': 'delete', 'path': '/personas/Scout%20Two', 'name': 'a persona that was removed'},
        {'op': 'delete', 'path': '/personas/Mike', 'name': 'a built-in persona is not removed'},
        {'op': 'delete', 'path': '/personas/nobody-of-the-kind', 'name': 'nobody is not removed'},
        # The conversations of the two agents: read, so that the rows their own rows of the
        # list name are compared as rows and not by how each Host spells an id.
        get('/log?session=pictured', 'the conversation of the agent made as a persona'),
        get('/log?session=voiced', 'the conversation of the agent with what is its own'),
        {**SNAPSHOT, 'name': 'snapshot afterwards'},
    ],
})

SCENARIOS.append({
    'id': 'app-screens',
    'what': "what an app's screens read of what a Host may have nothing of: an agent's heartbeat, schedules and "
            'old goal, the goal ledger, pictures that could be made, the herald, judgments, diagnostics, the '
            "janitors' policy, where the owner is, Cartesia's old picker, podcasts, the vocabulary, calls, the "
            'Oracle; and what cannot be read as asked',
    'steps': [
        get(f'/agent-heartbeat/status?session={AGENT}', "an agent's heartbeat"),
        get('/agent-heartbeat/status?session=nobody-there', 'the heartbeat of an agent that is not there'),
        get('/agent-heartbeat/status', 'the heartbeat of nobody'),
        get('/heartbeat/settings', 'how the heartbeat is set'),
        post('/agent-heartbeat', {'session': AGENT, 'heartbeat_enabled': False}, 'a heartbeat that is off is turned off'),
        post('/agent-heartbeat', {'heartbeat_enabled': False}, 'a heartbeat for nobody'),
        post('/agent-heartbeat', {'session': AGENT}, 'a heartbeat that is neither'),
        post('/agent-heartbeat', {'session': AGENT, 'heartbeat_enabled': 'no'}, 'a heartbeat that is a word'),
        post('/agent-heartbeat', {'session': 'nobody-there', 'heartbeat_enabled': False},
             'the heartbeat of an agent that is not there, turned off'),
        get(f'/agent-schedules?session={AGENT}', "an agent's schedules"),
        get(f'/agent-goal?session={AGENT}', 'the goal an agent works toward by itself'),
        get('/agent-goal?session=nobody-there', 'the goal of an agent that is not there'),
        get('/agent-goal', 'the goal of nobody'),
        get('/goal-ledger?plan_id=nothing&limit=5', 'the ledger of a goal nobody has'),
        get('/goal-ledger', 'the ledger of no goal'),
        get('/goal-ledger?plan_id=nothing&limit=many', 'a ledger asked for by a word'),
        get(f'/agent-portrait-generation?session={AGENT}', 'whether a picture could be made'),
        get('/avatar-settings', 'how pictures are set'),
        get('/herald/settings', 'how the herald is set'),
        get('/judgments/settings', 'how judgments are set'),
        get('/diagnostics/settings', 'how diagnostics are set'),
        get('/janitor-policy', "the janitors' policy"),
        get('/location', 'where the owner is'),
        get('/cartesia-voices', "Cartesia's old picker"),
        get('/tts/providers', 'the engines that speak, for the old settings screen'),
        get('/podcast-history', 'the podcasts'),
        get('/podcast-history/sources', 'what podcasts were made of'),
        get('/vocab/packs', 'the packs of the vocabulary'),
        get('/vocab/terms?pack_id=nothing', 'the terms of a pack that is not there'),
        get('/vocab/terms', 'the terms of no pack'),
        get('/vocab/profiles', 'the profiles of the vocabulary'),
        get('/vocab/profile?profile_id=nothing', 'a profile that is not there'),
        get('/vocab/assignments', 'who has which profile'),
        get(f'/vocab/runs?session={AGENT}&limit=5', 'the runs of the vocabulary'),
        get('/calls', 'the call the owner is in'),
        get('/calls/ring', 'whether an agent rings'),
        get('/oracle/status', 'what the Oracle can do'),
        get('/oracle/contact', 'whom the Oracle is put through to'),
        get('/oracle/delegations', 'what the Oracle handed on'),
        get('/oracle/calls/status', 'what came of it'),
        # Asked last, so that no place a difference is named by moves.
        get('/hotseat/status', 'how the account seats stand, as they were last read'),
        get('/hotseat/desk', 'the desk of the account seats'),
        get('/janitors/nobody-of-the-kind', 'a janitor that is not there'),
        get('/janitors/nobody-of-the-kind/runs?limit=5', 'the runs of a janitor that is not there'),
    ],
})

EXPLAINED = '/tool-explanations/settings'

SCENARIOS.append({
    'id': 'live-items',
    'what': 'a turn as it is shown while it runs (docs/live-items.md), to a connection that asked for this agent: '
            'what it thinks, what it says, a file it reads and an edit, each an item that appears, grows and '
            'settles; the whole view before any turn and when the turn is over; and what is shown of nobody. '
            'Nothing explains a tool here: the scenario turns that off first, so that both Hosts show a turn '
            'by itself',
    # The events are listened to as an app that follows this agent does.
    'live': True,
    'steps': [
        post(EXPLAINED, {'enabled': 'no'}, 'a setting that is no yes or no'),
        post(EXPLAINED, {'detail_level': 9}, 'a level nobody has'),
        post(EXPLAINED, {'enabled': False, 'detail_level': 3}, 'tools are not explained, and for whom they would be'),
        get(EXPLAINED, 'whether tools are explained'),
        get(f'/live?session={AGENT}', 'what is shown before any turn'),
        get('/live?session=nobody-of-the-kind', 'what is shown of nobody'),
        get('/live', 'what is shown of no session'),
        send('[qa-stream] synthetic work, shown as it is done', 'shown-1'),
        {'op': 'wait_reply', 'after': 'u-shown-1'},
        {'op': 'quiesce'},
        get(f'/live?session={AGENT}', 'what is shown when the turn is over'),
        LOG,
        SNAPSHOT,
        # One turn, and the first of its conversation. What the Python Host's conversation
        # shows of a streamed turn that follows another is not the same from run to run
        # (defect R6), and its names would shift what is compared here; that a new turn
        # replaces the items of the one before is held by the recorded streams of the
        # contract and by this Host's own tests.
        # Asked last, so that no place a registered difference is named by moves: tools are
        # asked to be explained where nothing explains them.
        post('/tool-explanations', {'session': AGENT, 'detail_level': 3,
                                    'items': [{'id': 'tool-1', 'name': 'Bash'}, 'not a thing', {'name': 'no id'}]},
             'tools are asked to be explained where nothing explains'),
        post('/tool-explanations', {'session': 'nobody-of-the-kind', 'items': []}, 'explained for nobody'),
        post('/tool-explanations', {'items': []}, 'explained for no session'),
        post('/tool-explanations', [1], 'asked with something that is no object'),
    ],
})

def dictated(text, msg_id, to, **extra):
    """A message dictated hands-free that names the scenario's agent, and the wait for its answer
    where the Host took it to."""
    return [send(text, msg_id, hands_free=True, **extra),
            {'op': 'wait_reply', 'after': f'u-{msg_id}', 'session': to},
            {'op': 'quiesce', 'session': to}]


SCENARIOS.append({
    'id': 'spoken-names',
    'what': 'whom a message is for that was dictated hands-free: the agent whose name it begins with, by the '
            'name as it is written or by one much like it, without the address; without a name the agent that '
            'is open; the agent the app names when the app says so; and what a Host answers whose router of '
            'spoken messages is off',
    'sessions': (AGENT, NOVA),
    'steps': [
        post('/agents', {**MAKE_TOLD, 'name': 'Nova', 'session': NOVA}, 'make a second agent'),
        post('/select', {'session': AGENT}, 'the first agent is open'),
        *dictated('Hey Nova, synthetic word alfa', 'namealfa', NOVA),
        SNAPSHOT,
        # Without a name it stays with the agent that is open, whoever the app names.
        *dictated('synthetic word bravo', 'namebravo', NOVA),
        # The app says whom it is for: a name changes nothing.
        *dictated('Nova, synthetic word charlie', 'namecharlie', AGENT, force_session=True),
        SNAPSHOT,
        # A name much like one, as a transcript writes it.
        *dictated('Novah: synthetic word delta', 'namedelta', NOVA),
        # A name alone is not taken off.
        *dictated('Nova?', 'nameecho', NOVA),
        # What was typed is for the agent it names.
        send('Nova, synthetic word foxtrot', 'namefoxtrot'),
        {'op': 'wait_reply', 'after': 'u-namefoxtrot'},
        {'op': 'quiesce'},
        # The app could not tell and asks the Host's router, which is off: nothing is sent.
        post('/orchestrator/route-delegation',
             {'session': AGENT, 'text': 'synthetic word golf for somebody', 'client_msg_id': 'namegolf',
              'synthesize_audio': False, 'hands_free': True, 'force_session': False, 'orchestrator_fallback': True},
             'the router is asked whom a message is for'),
        # The same path with the agent chosen is a send.
        post('/orchestrator/route-delegation',
             {'session': NOVA, 'text': 'Claire, synthetic word hotel', 'client_msg_id': 'namehotel',
              'synthesize_audio': False, 'hands_free': True, 'force_session': True},
             'the router is not asked: the app chose'),
        {'op': 'wait_reply', 'after': 'u-namehotel', 'session': NOVA},
        {'op': 'quiesce', 'session': NOVA},
        get(f'/log?session={NOVA}', 'the conversation of the agent that was named'),
        LOG,
        SNAPSHOT,
    ],
})

PLACE = '/location'

SCENARIOS.append({
    'id': 'places',
    'what': "where the owner's phone is: the app says it for a session, the agent's tools read the last place "
            'said, a newer one takes its place; what is no place is refused in words; and an agent asks for it',
    'steps': [
        get(f'{PLACE}?session={AGENT}', 'nobody said where'),
        get(PLACE, 'where, for no session'),
        post(PLACE, {'session': AGENT, 'lat': 59.91, 'lng': '10.75', 'accuracy': 12}, 'the app says where its phone is'),
        get(f'{PLACE}?session={AGENT}', 'the last place said'),
        get(f'{PLACE}?session=nobody', "another session's place"),
        post(PLACE, {'lat': 1, 'lng': 2}, 'a place for nobody'),
        post(PLACE, {'session': AGENT}, 'no place'),
        post(PLACE, {'session': AGENT, 'lat': 'north', 'lng': 2}, 'a word for a number'),
        post(PLACE, {'session': AGENT, 'lat': None, 'lng': 2}, 'nothing for a number'),
        post(PLACE, {'session': AGENT, 'lat': 91, 'lng': 2}, 'north of the pole'),
        post(PLACE, {'session': AGENT, 'lat': 1, 'lng': -181}, 'west of the date line'),
        post(PLACE, {'session': AGENT, 'lat': 1, 'lng': 2, 'accuracy': -5}, 'less than exact'),
        post(PLACE, {'session': AGENT, 'lat': 1, 'lng': 2, 'accuracy': 'about ten'}, 'a word for how exact'),
        post(PLACE, {'session': AGENT, 'lat': True, 'lng': ' -180 '}, 'a truth and a padded text for numbers'),
        post(PLACE, {'session': f' {AGENT} ', 'lat': -90, 'lng': 180}, 'a newer place, at the edges'),
        get(f'{PLACE}?session={AGENT}', 'the newer place'),
        post(f'{PLACE}/request', {'session': AGENT}, 'an agent asks where its user is'),
        post(f'{PLACE}/request', {}, 'nobody asks'),
    ],
})

HERALD = '/herald/settings'

SCENARIOS.append({
    'id': 'herald-settings',
    'what': "how the owner sets the herald (an agent whose user is with another raises its hand before it is "
            "heard): on unless turned off, how short a reply is that is said at once, and whether short replies "
            "are; and what is no setting. (What the herald does with a clip is held by the Rust Host's tests "
            "against the Python Host's own manager: the reference says nothing aloud in this harness.)",
    'steps': [
        get(HERALD, 'the herald as it comes'),
        post(HERALD, {'disabled': True}, 'the herald turned off'),
        post(HERALD, {'speak_if_short_chars': 0, 'short_reply_bypass_enabled': False}, 'no reply is short'),
        get(HERALD, 'the herald as it was set'),
        post(HERALD, {}, 'a change of nothing'),
        post(HERALD, [], 'a change that is no object'),
        post(HERALD, {'disabled': 'yes'}, 'a word for a truth'),
        post(HERALD, {'disabled': 1}, 'a number for a truth'),
        post(HERALD, {'speak_if_short_chars': True}, 'a truth for a number'),
        post(HERALD, {'speak_if_short_chars': 12.5}, 'a number that is not whole'),
        post(HERALD, {'speak_if_short_chars': '12'}, 'a text for a number'),
        post(HERALD, {'speak_if_short_chars': -1}, 'fewer signs than none'),
        post(HERALD, {'speak_if_short_chars': 2001}, 'more signs than may be'),
        post(HERALD, {'short_reply_bypass_enabled': None}, 'nothing for a truth'),
        post(HERALD, {'disabled': False, 'speak_if_short_chars': 9999}, 'one right and one wrong: nothing changes'),
        post(HERALD, {'speak_if_short_chars': 2000, 'unknown': 1}, 'the most signs, and something nobody knows'),
        post(HERALD, {'disabled': False, 'short_reply_bypass_enabled': True, 'speak_if_short_chars': 120},
             'the herald as it came'),
        get(HERALD, 'the herald at the end'),
    ],
})

PHONE = 'device_synthetic'


def in_call(text, msg_id, session, to=None, **extra):
    """A message of the call for one of its agents, and the wait for the answer of the agent the
    Host took it to (`to`, when that is another)."""
    to = to or session
    return [post('/send', {'session': session, 'text': text, 'client_msg_id': msg_id, 'synthesize_audio': False,
                           'hands_free': True, 'force_session': True, 'call_id': '{call}', **extra}, f'send {msg_id}'),
            {'op': 'wait_reply', 'after': f'u-{msg_id}', 'session': to},
            {'op': 'quiesce', 'session': to}]


SCENARIOS.append({
    'id': 'calls',
    'what': "a call: the Host's record of a hands-free conversation with several agents, for the phone that "
            'owns it. It starts, an agent is put on hold and taken off, the floor moves, one is called '
            'instead of another, a third is asked in by a name as it is said and leaves; a message of the '
            'call goes to the agent it names when that one can take speech and to the floor when not; the '
            'call ends, and a message of a call that is over is refused. What is no request is refused in '
            "words. Each change is told to the apps, and noted in the agents' conversations.",
    'sessions': (AGENT, NOVA),
    'steps': [
        get('/calls', 'no call is up'),
        post('/calls', {'agents': [AGENT]}, 'a call for no phone'),
        post('/calls', {'agents': AGENT, 'principal': PHONE}, 'agents that are no list'),
        post('/calls', {'agents': [], 'principal': PHONE}, 'a call with nobody'),
        post('/calls', {'agents': ['nobody-there'], 'principal': PHONE}, 'a call with somebody the Host has not'),
        post('/calls/add', {'agent': AGENT}, 'no call to ask anybody into'),
        post('/calls/end', {}, 'no call to end'),
        # Both agents have a conversation before the call: a notice is written where one is.
        post('/agents', {**MAKE_TOLD, 'name': 'Nova', 'session': NOVA}, 'make a second agent'),
        send('synthetic word before the call', 'callzero'),
        {'op': 'wait_reply', 'after': 'u-callzero'},
        {'op': 'quiesce'},
        send('synthetic word before the call', 'callzeronova', session=NOVA),
        {'op': 'wait_reply', 'after': 'u-callzeronova', 'session': NOVA},
        {'op': 'quiesce', 'session': NOVA},
        {**post('/calls', {'agents': ['Claire', 'nova'], 'principal': PHONE, 'request_id': 'synthetic-call'},
                'a call starts'), 'save': {'call': 'call.call_id'}},
        post('/calls', {'agents': ['nova'], 'principal': PHONE, 'request_id': 'synthetic-call'},
             'the same request again'),
        get('/calls', 'the call that is up'),
        get(f'/calls?principal={PHONE}', "the phone's call"),
        get('/calls?principal=device_other', "another phone's call"),
        # A message of the call for the one who was only asked in: it is in, and has the floor.
        *in_call('synthetic call word alfa', 'callalfa', NOVA),
        get('/calls', 'the floor went to the one who was spoken to'),
        post('/calls/hold', {'agent': 'Nova'}, 'one is put on hold'),
        post('/calls/hold', {'agent': 'nova'}, 'on hold already'),
        # On hold it takes no speech: what is said to it goes to who has the floor.
        *in_call('synthetic call word bravo', 'callbravo', NOVA, to=AGENT),
        post('/calls/resume', {'agent': 'novah'}, 'back, by a name as it is said'),
        post('/calls/resume', {'agent': 'nova'}, 'not on hold'),
        post('/calls/switch', {'agent': 'nova'}, 'the floor goes to the other'),
        post('/calls/transfer', {'agent': 'claire'}, 'the one on the floor is held and the first is called'),
        post('/calls/remove', {'agent': 'somebody-else'}, 'a name nobody has'),
        post('/calls/add', {'agent': ''}, 'nobody is asked in'),
        post('/calls/add', ['claire'], 'a change that is no object'),
        post('/calls/dance', {'agent': 'claire'}, 'what is no change'),
        post('/calls/hold', {'agent': 'claire'}, 'the other is put on hold too'),
        post('/send', {'session': AGENT, 'text': 'synthetic call word charlie', 'client_msg_id': 'callcharlie',
                       'synthesize_audio': False, 'call_id': '{call}'}, 'a message when nobody can take speech'),
        post('/send', {'session': AGENT, 'text': '  ', 'client_msg_id': 'callnothing', 'call_id': '{call}'},
             'a message of the call that says nothing'),
        post('/calls/remove', {'agent': 'nova'}, 'one leaves'),
        post('/calls/remove', {'agent': 'claire'}, 'the last one leaves: the call is over'),
        get('/calls', 'no call is up any more'),
        post('/send', {'session': AGENT, 'text': 'synthetic call word delta', 'client_msg_id': 'calldelta',
                       'synthesize_audio': False, 'call_id': '{call}'}, 'a message of a call that is over'),
        post('/calls/end', {'call_id': '{call}', 'principal': PHONE}, 'the call that is over is ended'),
        # A second call takes the place of a first one that is up.
        post('/calls', {'agents': ['nova'], 'principal': PHONE}, 'another call'),
        post('/calls', {'agents': ['claire', 'Nova']}, 'a call that takes its place, for the phone of the newest'),
        post('/calls/end', {}, 'it ends'),
        get(f'/log?session={NOVA}', 'the conversation of the second agent'),
        LOG,
        SNAPSHOT,
    ],
})

def rings(agent, number):
    return {'agent': agent, 'reason': f'  synthetic   reason {number} ', 'opening_line': f'synthetic opening {number}'}


SCENARIOS.append({
    'id': 'rings',
    'what': 'a ring: an agent calls its owner. The owner\'s tools ring for an agent by its name, with why and '
            'with the line it opens with; while it rings nobody else does; the answer starts a call with '
            'the agent on the floor and hands back the line to play first, the same again for who answered '
            'and a refusal for anybody else; a message of that call is answered. A ring is declined, and '
            'one that is over is not answered. An agent waits before it rings again. The owner turns rings '
            'off and on and reads the trail; a phone leaves its token for calls. What is no ring is refused '
            "in words. Each change is told to the apps, and noted in the agent's conversation.",
    'sessions': (AGENT, NOVA),
    'steps': [
        get('/calls/ring', 'nobody rings'),
        get('/calls/ring/settings', "the owner's switches, and an empty trail"),
        post('/calls/ring', {}, 'a ring that names nobody'),
        post('/calls/ring', ['claire'], 'a ring that is no object'),
        post('/calls/ring', rings('nobody-there', 'zero'), 'a ring for an agent the Host has not'),
        post('/calls/ring', {'agent': AGENT, 'opening_line': 'synthetic opening'}, 'a ring without a reason'),
        post('/calls/ring', {'agent': AGENT, 'reason': 'synthetic reason'}, 'a ring without an opening line'),
        post('/calls/ring', {**rings(AGENT, 'zero'), 'opening_mode': 'sing'}, 'an opening nobody knows'),
        post('/calls/ring', {**rings(AGENT, 'zero'), 'conversation_mode': 'telepathy'}, 'a way to talk nobody knows'),
        post('/calls/ring', {**rings(AGENT, 'zero'), 'event_context': {'source': 'synthetic'}},
             'what only a live voice call is told'),
        post('/calls/ring/answer', {'ring_id': 'rng_nothing'}, 'an answer to no ring'),
        post('/calls/ring/decline', {'ring_id': 'rng_nothing'}, 'no ring is declined'),
        post('/calls/ring/cancel', {}, 'no ring is taken back'),
        get('/calls/ring?ring_id=rng_nothing', 'a ring that is none'),
        # Both agents have a conversation before they ring: a notice is written where one is.
        post('/agents', {**MAKE_TOLD, 'name': 'Nova', 'session': NOVA}, 'make a second agent'),
        send('synthetic word before the ring', 'ringzero'),
        {'op': 'wait_reply', 'after': 'u-ringzero'},
        {'op': 'quiesce'},
        send('synthetic word before the ring', 'ringzeronova', session=NOVA),
        {'op': 'wait_reply', 'after': 'u-ringzeronova', 'session': NOVA},
        {'op': 'quiesce', 'session': NOVA},
        {**post('/calls/ring', rings('Claire', 'one'), 'an agent rings'), 'save': {'ring': 'ring.ring_id'}},
        get('/calls/ring', 'the ring that rings'),
        get('/calls/ring?ring_id={ring}', 'the ring by its id'),
        post('/calls/ring', rings('nova', 'two'), 'another rings meanwhile'),
        post('/calls/ring/answer', {'ring_id': '{ring}', 'conversation_mode': 'telepathy'},
             'answered in a way nobody knows'),
        post('/calls/ring/answer', {'ring_id': '{ring}', 'conversation_mode': 'agent_live_v1'},
             'answered as a live voice call, which it is not'),
        post('/calls/ring/answer', {'ring_id': '{ring}'}, 'answered before any phone made a call'),
        post('/calls', {'agents': ['nova'], 'principal': PHONE}, 'a phone makes a call'),
        post('/calls/end', {'principal': PHONE}, 'and ends it'),
        {**post('/calls/ring/answer', {'ring_id': '{ring}'}, 'the ring is answered'), 'save': {'call': 'call.call_id'}},
        post('/calls/ring/answer', {'ring_id': '{ring}', 'opening_mode': 'say'}, 'answered again, by the same'),
        get('/calls', 'the call the answer started'),
        get('/calls/ring', 'nobody rings now'),
        get('/calls/ring?ring_id={ring}', 'the ring that was answered'),
        post('/calls/ring/decline', {'ring_id': '{ring}'}, 'declined after it was answered'),
        post('/calls/ring/cancel', {'ring_id': '{ring}'}, 'taken back after it was answered'),
        # What the user says in the call the ring became goes to the agent that rang.
        *in_call('synthetic ring word alfa', 'ringalfa', AGENT),
        post('/calls/end', {'principal': PHONE}, 'the call ends'),
        post('/calls/ring', rings('claire', 'three'), 'the same agent rings again too soon'),
        {**post('/calls/ring', rings('Nova', 'four'), 'another agent rings'), 'save': {'second': 'ring.ring_id'}},
        post('/calls/ring/decline', {'ring_id': '{second}'}, 'the ring is declined'),
        post('/calls/ring/decline', {'ring_id': '{second}'}, 'declined already'),
        post('/calls/ring/answer', {'ring_id': '{second}'}, 'answered after it was declined'),
        post('/calls/ring/cancel', {'ring_id': '{second}'}, 'taken back after it was declined'),
        post('/calls/ring/settings', {'enabled': False}, 'the owner turns rings off'),
        post('/calls/ring', rings('nova', 'five'), 'a ring while rings are off'),
        post('/calls/ring/settings', {'enabled': 'yes'}, 'a switch that is no switch'),
        post('/calls/ring/settings', ['enabled'], 'switches that are no object'),
        post('/calls/ring/settings', {'enabled': True, 'show_reason': True}, 'on again, and the reason shown'),
        get('/calls/ring/settings', 'the switches and the trail'),
        post('/calls/ring/voip-token', {'token': 'zz'}, 'a token that is none'),
        post('/calls/ring/voip-token', {'token': 'ab' * 32, 'environment': 'sandbox'}, 'a phone leaves its token'),
        post('/calls/ring/voip-token', {'token': 'ab' * 32, 'supported_conversation_modes': ['telepathy']},
             'with a way to talk nobody knows'),
        post('/calls/ring/voip-token', {'token': ''}, 'and takes it back'),
        get(f'/log?session={NOVA}', 'the conversation of the second agent'),
        LOG,
        SNAPSHOT,
    ],
})


ACTIVITY = '/devices/live-activity'

SCENARIOS.append({
    'id': 'activities',
    'what': "the activity a phone shows the fleet's work in on its lock screen: the phone leaves the token "
            'its pushes go to, under a name or under the beginning of the token; the same name again takes '
            'the place of what was there; it is taken back when the activity ended on the phone, by its name '
            'or by its token. What is no token, no kind and no side of the push service is refused in words.',
    'steps': [
        post(ACTIVITY, {}, 'an activity without a token'),
        post(ACTIVITY, ['ab' * 20], 'an activity that is no object'),
        post(ACTIVITY, {'token': 'not hex at all'}, 'a token that is none'),
        post(ACTIVITY, {'token': 'ab' * 201}, 'a token that is too long'),
        post(ACTIVITY, {'token': 'ab' * 20, 'kind': 'something-else'}, 'a kind nobody knows'),
        post(ACTIVITY, {'token': 'ab' * 20, 'environment': 'moon'}, 'a side of the push service nobody knows'),
        post(ACTIVITY, {'token': 'AB' * 20, 'activity_id': 'act-1', 'kind': 'agents-working', 'environment': 'sandbox'},
             'a phone leaves an activity'),
        post(ACTIVITY, {'token': 'cd' * 20, 'activity_id': 'act-1'}, 'the activity has another token now'),
        post(ACTIVITY, {'token': ' ' + 'ef' * 20 + ' ', 'kind': 'agents', 'environment': 'development'},
             'an activity without a name'),
        post(ACTIVITY, {'token': 'ef' * 20, 'activity_id': '  act-2  '}, 'the same token under a name'),
        post(ACTIVITY, {'platform': 'fcm', 'kind': 'agents-working'}, 'the same for a phone of another kind, by nobody\'s phone'),
        post(ACTIVITY, {'activity_id': 'act-1', 'ended': True}, 'the activity ended on the phone'),
        post(ACTIVITY, {'activity_id': 'act-1', 'ended': True}, 'ended already'),
        post(ACTIVITY, {'ended': True}, 'an end that names nothing'),
        {'op': 'delete', 'path': ACTIVITY + '?token=' + 'EF' * 20, 'name': 'taken back by its token'},
        {'op': 'delete', 'path': ACTIVITY + '?activity_id=act-2', 'name': 'taken back already'},
        {'op': 'delete', 'path': ACTIVITY, 'name': 'nothing is named'},
        {'op': 'delete', 'path': ACTIVITY + '?platform=fcm', 'name': 'the same for a phone of another kind'},
    ],
})

SCHEDULES = '/agent-schedules'


def scheduled(name, cron, prompt, **more):
    return {'session': AGENT, 'name': name, 'cron_expression': cron, 'prompt': prompt, **more}


SCENARIOS.append({
    'id': 'schedules',
    'what': 'the prompts an agent is sent at set times: a schedule is made for an agent by its session, with '
            'a name, a cron expression and what to send; made off; listed for one agent and for all; turned '
            'off and on; changed and removed, by its id in the request and in the address. What is no '
            'schedule and no expression is refused in words. No schedule of the scenario comes due while it '
            'runs: when an expression runs next is held by its own vectors.',
    'steps': [
        get(SCHEDULES, 'no schedules'),
        get(f'{SCHEDULES}?session={AGENT}', 'none of the agent'),
        post(SCHEDULES, {}, 'a schedule that says nothing'),
        post(SCHEDULES, ['x'], 'a schedule that is no object'),
        post(SCHEDULES, {'session': AGENT, 'name': 'n', 'cron': '0 0 1 1 *'}, 'a schedule without a prompt'),
        post(SCHEDULES, {**scheduled('n', '0 0 1 1 *', 'p'), 'session': 'nobody-there'}, 'a schedule for nobody'),
        post(SCHEDULES, scheduled('n', 'nonsense', 'p'), 'an expression that is none'),
        post(SCHEDULES, scheduled('n', '0 0 1 1', 'p'), 'an expression of four fields'),
        post(SCHEDULES, scheduled('n', '61 0 1 1 *', 'p'), 'a minute there is none of'),
        post(SCHEDULES, scheduled('n', '*/0 0 1 1 *', 'p'), 'a step of nothing'),
        post(SCHEDULES, scheduled('n', 'a 0 1 1 *', 'p'), 'a field that is no number'),
        {**post(SCHEDULES, scheduled('  Synthetic new year ', ' 0 0 1 1 * ', '  synthetic prompt one  '),
                'a schedule is made'), 'save': {'first': 'schedule.schedule_id'}},
        {**post(SCHEDULES, {**scheduled('Synthetic off', '@monthly', 'synthetic prompt two'), 'enabled': False},
                'a schedule is made off'), 'save': {'second': 'schedule.schedule_id'}},
        post(SCHEDULES, {'session': AGENT, 'name': 'Synthetic short', 'cron': '30 4 29 2 *', 'prompt': 'synthetic prompt three',
                         'enabled': 0}, 'made by the short word for its expression, off by a number'),
        get(SCHEDULES, 'the schedules'),
        get(f'{SCHEDULES}?session={AGENT}', 'the schedules of the agent'),
        get(f'{SCHEDULES}?session=nobody-there', 'the schedules of nobody'),
        post(f'{SCHEDULES}/toggle', {'schedule_id': '{second}', 'enabled': True}, 'turned on'),
        post(f'{SCHEDULES}/toggle', {'schedule_id': '{second}', 'enabled': True}, 'on already'),
        post(f'{SCHEDULES}/toggle', {'schedule_id': '{first}', 'enabled': False}, 'turned off'),
        post(f'{SCHEDULES}/toggle', {'schedule_id': '{first}'}, 'a switch that says neither'),
        post(f'{SCHEDULES}/toggle', {'schedule_id': '{first}', 'enabled': 'yes'}, 'a switch that is no switch'),
        post(f'{SCHEDULES}/toggle', {'schedule_id': 'sched_nothing', 'enabled': True}, 'a switch for no schedule'),
        post(f'{SCHEDULES}/toggle', ['x'], 'a switch that is no object'),
        {'op': 'patch', 'path': '/schedules/{first}', 'body': {'name': '  Synthetic first day ', 'enabled': True},
         'name': 'given another name, and on'},
        {'op': 'patch', 'path': '/schedules/{first}', 'body': {'cron_expression': '15 3 1 1 *', 'prompt': ' synthetic prompt four '},
         'name': 'given another expression and prompt'},
        {'op': 'patch', 'path': '/schedules/{first}', 'body': {'cron': 'nonsense'}, 'name': 'given an expression that is none'},
        {'op': 'patch', 'path': '/schedules/{first}', 'body': {'name': '', 'prompt': ''}, 'name': 'given nothing'},
        {'op': 'patch', 'path': '/schedules/sched_nothing', 'body': {'name': 'x'}, 'name': 'a change of no schedule'},
        {'op': 'patch', 'path': '/schedules/{first}', 'body': ['x'], 'name': 'a change that is no object'},
        get(SCHEDULES, 'the schedules as they are now'),
        post(f'{SCHEDULES}/delete', {}, 'nothing is removed'),
        post(f'{SCHEDULES}/delete', {'schedule_id': '{second}'}, 'a schedule is removed'),
        post(f'{SCHEDULES}/delete', {'schedule_id': '{second}'}, 'removed already'),
        {'op': 'delete', 'path': '/schedules/{first}', 'name': 'a schedule is removed by its address'},
        {'op': 'delete', 'path': '/schedules/{first}', 'name': 'removed already, by its address'},
        {'op': 'delete', 'path': SCHEDULES, 'name': 'what is no route'},
        get(SCHEDULES, 'the schedule that is left'),
    ],
})

# The token the replay harness gives its Hosts (`tests/replay/capture.py`): made up, like every
# text here. A step that carries headers of its own may say who asks otherwise, or not at all.
TOKEN = 'replay-host-token'
NOBODY = {'Authorization': ''}


SCENARIOS.append({
    'id': 'the-apps-own-ids',
    'what': "a message is filed under the id its app gave it: an id that comes with the mark of a message, as "
            "the phone's ids do, is kept as it is, and one that comes without, as the desk's do, gets the mark; "
            'the queue and the news of a turn and of the queue name each message as its app named it',
    'steps': [
        send(SLOW + 'synthetic ids alfa', 'u-phone-alfa'),
        {'op': 'wait_busy', 'busy': True},
        send('synthetic ids bravo', 'u-phone-bravo', queue_if_busy=True),
        send('synthetic ids charlie', 'desk-charlie', queue_if_busy=True),
        {**QUEUE, 'name': 'the queue names each as its app named it'},
        {'op': 'wait_reply', 'after': 'u-phone-alfa'},
        {'op': 'wait_reply', 'after': 'u-phone-bravo'},
        {'op': 'wait_reply', 'after': 'u-desk-charlie'},
        {'op': 'quiesce'},
        {**LOG, 'name': 'the conversation: each row under the id its app looks for'},
        {**QUEUE, 'name': 'the queue afterwards'},
        {**SNAPSHOT, 'name': 'the snapshot afterwards'},
    ],
})


def by_hand(queue_id, name):
    return {'op': 'post', 'path': f'/turn-queue/{queue_id}/send', 'body': {}, 'name': name}


def taken_back(queue_id, name):
    return {'op': 'delete', 'path': f'/turn-queue/{queue_id}', 'name': name}


SCENARIOS.append({
    'id': 'the-queue-by-hand',
    'what': 'what waits is taken back or sent now by its user: by the id its app gave it; nothing is sent past '
            'a turn that runs; one that is sent by hand runs although the queue is held, and the rest stays '
            'held; and a hold that holds nothing any more is over without a resume',
    'steps': [
        send(SLOW + 'synthetic queue first', 'q-first'),
        {'op': 'wait_busy', 'busy': True},
        send('synthetic queue golf', 'q-golf', queue_if_busy=True),
        send('synthetic queue hotel', 'u-q-hotel', queue_if_busy=True),
        send('synthetic queue india', 'q-india', queue_if_busy=True),
        send('synthetic queue juliett', 'q-juliett', queue_if_busy=True),
        {**QUEUE, 'name': 'four wait'},
        # Not asked here: one of them sent by hand while the turn runs. Both Hosts refuse it
        # in the same words (409), and the Python Host's count of the queue's changes moves by
        # two with the refusal, which every number after it would show (docs/differences.md).
        by_hand('nothing-there', 'what is not there, sent by hand'),
        taken_back('nothing-there', 'what is not there, taken back'),
        taken_back('u-q-hotel', 'one is taken back, by the id its app gave it'),
        taken_back('u-q-hotel', 'taken back a second time'),
        {**QUEUE, 'name': 'three wait'},
        post('/stop', {'session': AGENT}),
        {'op': 'wait_busy', 'busy': False},
        {'op': 'quiesce', 'held': True},
        {**QUEUE, 'name': 'three are held'},
        by_hand('q-india', 'one is sent by hand past the hold, and not the first'),
        {'op': 'wait_reply', 'after': 'u-q-india'},
        {'op': 'quiesce', 'held': True},
        {**QUEUE, 'name': 'two are still held'},
        {**SNAPSHOT, 'name': 'the snapshot while two are held'},
        taken_back('q-golf', 'one more is taken back'),
        {**QUEUE, 'name': 'one is still held'},
        by_hand('q-juliett', 'the last is sent by hand'),
        {'op': 'wait_reply', 'after': 'u-q-juliett'},
        {'op': 'quiesce'},
        {**QUEUE, 'name': 'nothing waits, and nothing is held'},
        # A hold that holds one thing ends when that thing is taken back.
        send(SLOW + 'synthetic queue kilo', 'q-kilo'),
        {'op': 'wait_busy', 'busy': True},
        send('synthetic queue lima', 'q-lima', queue_if_busy=True),
        post('/stop', {'session': AGENT}),
        {'op': 'wait_busy', 'busy': False},
        {'op': 'quiesce', 'held': True},
        {**QUEUE, 'name': 'one is held'},
        taken_back('q-lima', 'the one thing a hold held is taken back'),
        {**QUEUE, 'name': 'the hold is over'},
        {**LOG, 'name': 'the conversation: nothing that was taken back is in it'},
        {**SNAPSHOT, 'name': 'the snapshot afterwards'},
    ],
})


def reworded(queue_id, words, name):
    return {'op': 'put', 'path': f'/turn-queue/{queue_id}', 'body': {'text': words}, 'name': name}


SCENARIOS.append({
    'id': 'the-queue-reworded',
    'what': 'the words of a waiting message are changed by its user: it keeps its id and its place, its turn '
            'answers the words it has when it starts, also when they were changed while a hold held it; and '
            'what does not wait has no words to change',
    'steps': [
        send(SLOW + 'synthetic reword first', 'r-first'),
        {'op': 'wait_busy', 'busy': True},
        send('synthetic reword golf', 'r-golf', queue_if_busy=True),
        send('synthetic reword hotel', 'u-r-hotel', queue_if_busy=True),
        reworded('r-golf', '   ', 'words that are none'),
        {'op': 'put', 'path': '/turn-queue/r-golf', 'body': {}, 'name': 'no words'},
        reworded('nothing-there', '   ', 'no words for what is not there'),
        reworded('nothing-there', 'synthetic reword nothing', 'what is not there is reworded'),
        reworded('r-golf', '  synthetic reword golf, in other words  ', 'one is reworded'),
        reworded('u-r-hotel', 'synthetic reword hotel, as the phone has it now', "one of the phone's is reworded"),
        {**QUEUE, 'name': 'the queue with the new words'},
        post('/stop', {'session': AGENT}),
        {'op': 'wait_busy', 'busy': False},
        {'op': 'quiesce', 'held': True},
        reworded('r-golf', 'synthetic reword golf, as it was held', 'reworded while a hold holds it'),
        {**QUEUE, 'name': 'held, with the new words'},
        post('/turn-queue/resume', {'session': AGENT}),
        {'op': 'wait_reply', 'after': 'u-r-golf'},
        {'op': 'wait_reply', 'after': 'u-r-hotel'},
        {'op': 'quiesce'},
        reworded('r-golf', 'synthetic reword too late', 'reworded after it ran'),
        {**LOG, 'name': 'the conversation: each message with the words it had when its turn started'},
        {**QUEUE, 'name': 'the queue afterwards'},
        {**SNAPSHOT, 'name': 'the snapshot afterwards'},
    ],
})


def picture(width):
    """A tiny picture that is no other tiny picture: a GIF this many dots wide."""
    return 'GIF89a' + chr(width) + '\x00\x03\x00' + '\x00' * 6


def portrait(change, name, **more):
    return {'op': 'post', 'path': '/agent-portraits', 'body': {'session': AGENT, **change}, 'name': name, **more}


PORTRAITS = get(f'/agent-portraits?session={AGENT}')

SCENARIOS.append({
    'id': 'agent-portraits',
    'what': 'the portraits an agent keeps: pictures it published are kept as portraits, three at most and each '
            'once; one of them is made the picture the agent wears, and is then its face wherever it is shown; '
            'and what is asked for that is not there is refused in words',
    'steps': [
        {**PORTRAITS, 'name': 'the portraits of an agent that wears no picture of its own'},
        get('/agent-portraits?session=nobody-of-the-kind', 'the portraits of nobody'),
        {**publish(picture(2), 'image/gif', 'one.gif'), 'save': {'one': 'asset.asset_id'}},
        {**publish(picture(3), 'image/gif', 'two.gif'), 'save': {'two': 'asset.asset_id'}},
        {**publish(picture(4), 'image/gif', 'three.gif'), 'save': {'three': 'asset.asset_id'}},
        {**publish(picture(5), 'image/gif', 'four.gif'), 'save': {'four': 'asset.asset_id'}},
        portrait({'action': 'add_media_asset', 'asset_id': '{one}'}, 'a picture it published is kept',
                 save={'first': 'portraits.0.portrait_id'}),
        portrait({'action': 'add_media_asset', 'asset_id': '{one}'}, 'kept a second time, it is kept once'),
        portrait({'action': 'add_media_asset', 'asset_id': '{two}'}, 'a second is kept'),
        portrait({'action': 'add_media_asset', 'asset_id': '{three}'}, 'a third is kept'),
        portrait({'action': 'add_media_asset', 'asset_id': '{four}'}, 'a fourth is one too many'),
        portrait({'action': 'add_media_asset', 'asset_id': 'asset_nothing_of_the_kind'}, 'what it never published'),
        portrait({'action': 'add_media_asset'}, 'nothing named to keep'),
        portrait({'action': 'select_primary', 'portrait_id': 'portrait_nothing_of_the_kind'}, 'a portrait it has not'),
        portrait({'action': 'paint'}, 'a change nobody knows'),
        post('/agent-portraits', {'session': 'nobody-of-the-kind', 'action': 'select_primary', 'portrait_id': 'x'},
             'a portrait chosen for nobody'),
        post('/agent-portraits', {'session': 'nobody-of-the-kind', 'action': 'paint'},
             'a change nobody knows, for nobody'),
        portrait({'action': 'select_primary', 'portrait_id': '{first}'}, 'one of them is made the picture it wears'),
        {**PORTRAITS, 'name': 'the portraits afterwards: the one it wears first'},
        get('/agent-portraits/{first}/content', 'the picture of the portrait it wears'),
        get('/agent-portraits/portrait_nothing_of_the_kind/content', 'the picture of a portrait nobody has'),
        {**SNAPSHOT, 'name': 'the snapshot: the agent wears the picture it was given'},
    ],
})


def settings(change, name):
    return post('/heartbeat/settings', change, name)


HEARTBEAT = get(f'/agent-heartbeat/status?session={AGENT}')

SCENARIOS.append({
    'id': 'the-heartbeat',
    'what': "the heartbeat as an app's settings and an agent's profile read and change it: the owner's settings, "
            "changed whole or not at all, and why not in words; an agent's switch; its cadence before anybody "
            'spoke with it, after, and when it is off. On a Host whose heartbeats are off, as both are here, so '
            'that nobody is woken while it is read: what an agent is sent and what its answer counts as is held '
            'by vectors (crates/clarp-kernel/tests/heartbeat_vectors.rs) and by the Host\'s own tests',
    'steps': [
        get('/heartbeat/settings', 'the settings as a Host comes'),
        settings({'heartbeat_interval_sec': 600, 'heartbeat_backoff_strategy': ' linear ',
                  'heartbeat_backoff_cap_sec': 7200, 'heartbeat_dormant_after_noops': 2}, 'all four are changed'),
        settings({'heartbeat_dormant_after_noops': 0}, 'one is changed and the others stay'),
        settings({}, 'nothing is changed'),
        settings({'heartbeat_interval_sec': 59}, 'an interval too short'),
        settings({'heartbeat_interval_sec': 86401, 'heartbeat_backoff_cap_sec': 86401}, 'an interval too long'),
        settings({'heartbeat_interval_sec': 7201}, 'an interval longer than the cap that stands'),
        settings({'heartbeat_interval_sec': '600'}, 'an interval that is a word'),
        settings({'heartbeat_interval_sec': True}, 'an interval that is yes'),
        settings({'heartbeat_interval_sec': 600.0}, 'an interval with a fraction'),
        settings({'heartbeat_interval_sec': None}, 'an interval that is nothing'),
        settings({'heartbeat_backoff_cap_sec': 599}, 'a cap below the interval'),
        settings({'heartbeat_backoff_cap_sec': 86401}, 'a cap longer than a day'),
        settings({'heartbeat_dormant_after_noops': 101}, 'more heartbeats than a hundred'),
        settings({'heartbeat_dormant_after_noops': -1}, 'fewer than none'),
        settings({'heartbeat_backoff_strategy': 'quadratic'}, 'a strategy nobody knows'),
        settings({'heartbeat_backoff_strategy': 'Linear'}, 'a strategy in other letters'),
        settings({'heartbeat_backoff_strategy': 7}, 'a strategy that is a number'),
        settings({'heartbeat_interval_sec': 900, 'heartbeat_backoff_strategy': 'quadratic'},
                 'one right and one wrong: nothing is changed'),
        settings({'heartbeat_backoff_cap_sec': '9', 'heartbeat_interval_sec': 5}, 'two wrong: the first is named'),
        settings({'heartbeat_interval_sec': 86400, 'heartbeat_backoff_cap_sec': 86400,
                  'heartbeat_backoff_strategy': 'fixed', 'heartbeat_dormant_after_noops': 100, 'something': 'else'},
                 'the longest of everything, and something nobody asked about'),
        settings({'heartbeat_interval_sec': 60, 'heartbeat_backoff_cap_sec': 60, 'heartbeat_backoff_strategy': 'exponential'},
                 'the shortest of everything'),
        settings([1], 'something that is no object'),
        settings({'heartbeat_interval_sec': 600, 'heartbeat_backoff_cap_sec': 3600}, 'ten minutes, an hour at most'),
        get('/heartbeat/settings', 'the settings afterwards'),
        {**HEARTBEAT, 'name': 'the cadence of an agent that did not ask'},
        post('/agent-heartbeat', {'session': AGENT, 'heartbeat_enabled': True}, 'an agent asks for the heartbeat'),
        {**HEARTBEAT, 'name': 'its cadence before anybody spoke with it'},
        {**SNAPSHOT, 'name': 'the snapshot says who asked'},
        send('synthetic heartbeat alfa', 'hb-alfa'),
        {'op': 'wait_reply', 'after': 'u-hb-alfa'},
        {'op': 'quiesce'},
        # The conversation is read, so that its rows are compared as rows where the snapshot
        # names one, and not by how each Host spells an id.
        {**LOG, 'name': 'the conversation after somebody spoke with it'},
        {**HEARTBEAT, 'name': 'its cadence after somebody spoke with it'},
        post('/agent-heartbeat', {'session': AGENT, 'heartbeat_enabled': True}, 'asked for once more'),
        get(f'/agent-heartbeat/status?session=%20{AGENT}%20', 'its cadence, asked for with blanks around its name'),
        post('/agent-heartbeat', {'session': f' {AGENT} ', 'heartbeat_enabled': False},
             'turned off, with blanks around its name'),
        {**HEARTBEAT, 'name': 'its cadence when it is off'},
        post('/agent-heartbeat', {'session': AGENT, 'heartbeat_enabled': 1}, 'a heartbeat that is a number'),
        post('/agent-heartbeat', {'session': AGENT, 'heartbeat_enabled': None}, 'a heartbeat that is nothing'),
        post('/agent-heartbeat', {'session': '   ', 'heartbeat_enabled': True}, 'a heartbeat for blanks'),
        {**SNAPSHOT, 'name': 'the snapshot afterwards'},
    ],
})


def asking(path, name, **headers):
    """A request for `path` with headers of its own; an underscore in a name stands for a hyphen."""
    return {**get(path, name), 'headers': {key.replace('_', '-'): value for key, value in headers.items()}}


def guessing(path, name, address, body=None):
    """A request by whoever a proxy on the Host's machine says asks from `address`, with a token
    that is none; with a body, a pairing code that is none."""
    step = post(path, body, name) if body is not None else get(path, name)
    return {**step, 'headers': {'Authorization': 'Bearer synthetic-guess', 'X-Forwarded-For': address}}


SCENARIOS.append({
    'id': 'the-door',
    'what': "who is trusted as the Host's own machine: the administrator's token opens the Host only when nobody "
            'handed the request on, a token in an address is taken only there, whoever fails too often to say who '
            'they are is told to wait, each asker for itself, and a Host that is told of no relay says so',
    'steps': [
        get('/network/relay', 'the relay, on a Host that is told of none'),
        SNAPSHOT,
        # What a proxy on the machine, the relay, or a page from elsewhere puts on a request: the socket is the
        # machine's own, and the request is not.
        asking('/agents/snapshot', 'handed on for an address', X_Forwarded_For='203.0.113.7'),
        asking('/agents/snapshot', 'handed on over TLS', X_Forwarded_Proto='https'),
        asking('/agents/snapshot', 'handed on by the tailnet', Tailscale_User_Login='owner@example.com'),
        asking('/agents/snapshot', 'handed on by an edge', CF_Connecting_IP='203.0.113.7'),
        asking('/agents/snapshot', 'handed on, by the standard word for it', Forwarded='for=203.0.113.7'),
        asking('/agents/snapshot', 'handed on for a real address', X_Real_IP='203.0.113.7'),
        asking('/agents/snapshot', 'said to have come through the relay', X_Clarp_Transport='relay'),
        asking('/agents/snapshot', 'asked by a page from elsewhere', Origin='https://evil.example'),
        asking('/agents/snapshot', 'asked across sites', Sec_Fetch_Site='cross-site'),
        # And what says none of that.
        asking('/agents/snapshot', 'asked by a page of the machine itself', Origin='http://localhost:7682'),
        asking('/agents/snapshot', 'asked within the site', Sec_Fetch_Site='same-origin'),
        asking('/agents/snapshot', 'asked by an app that says which it is', X_Clarp_Client='ios/141 contract=99'),
        asking('/agents/snapshot', 'the word bearer in small letters', Authorization=f'bearer {TOKEN}'),
        # A token in an address, and in a cookie: from the machine itself, and handed on.
        asking(f'/agents/snapshot?token={TOKEN}', 'the token in the address', **NOBODY),
        asking(f'/agents/snapshot?token={TOKEN}', 'the token in the address, handed on', **NOBODY,
               X_Forwarded_For='203.0.113.7'),
        asking('/agents/snapshot', 'the token in a cookie', **NOBODY, Cookie=f'x=1; claude_pwa_token={TOKEN}'),
        asking('/agents/snapshot', 'the token in a cookie, handed on', **NOBODY,
               Cookie=f'claude_pwa_token={TOKEN}', X_Forwarded_For='203.0.113.7'),
        asking('/agents/snapshot', 'no token at all', **NOBODY),
        asking('/agents/snapshot', 'a token that is none', Authorization='Bearer synthetic-guess'),
        asking('/agents/snapshot', 'a token said another way', Authorization=f'Basic {TOKEN}'),
        # Twenty failures in a minute are answered as failures; the next is told to wait. Who asks rightly is
        # never kept waiting, and neither is another asker.
        *[guessing('/agents/snapshot', f'a guess, the {n}th', '198.51.100.9') for n in range(1, 21)],
        guessing('/agents/snapshot', 'one guess too many', '198.51.100.9'),
        SNAPSHOT,
        guessing('/agents/snapshot', 'a guess from another address', '198.51.100.10'),
        # A pairing code that is none is such a failure too.
        *[guessing('/pairing/exchange', f'a code that is none, the {n}th', '198.51.100.11',
                   {'code': 'clp_synthetic', 'device_name': ''}) for n in range(1, 21)],
        guessing('/pairing/exchange', 'one code too many', '198.51.100.11', {'code': 'clp_synthetic', 'device_name': ''}),
    ],
})


# A child a fork asks for, and the child it makes. Its session name is the Host's to choose: the
# suite names it where a Host says it (`<forked:1>`).
CHILD = {'name': 'Fork One', 'task': 'synthetic task one'}
FORKED = '<forked:1>'


def fork(body, name, session=AGENT):
    return post(f'/agents/{session}/fork', body, name)


def fork_raw(text, name):
    """A fork whose body is sent as it is written here."""
    return {'op': 'post', 'path': f'/agents/{AGENT}/fork', 'name': name,
            'raw': {'bytes': text, 'content_type': 'application/json'}}


SCENARIOS.append({
    'id': 'agent-forks',
    'what': 'an agent forks itself into helpers that start from its conversation and each get a task: what a '
            'request that asks wrongly is told and that it makes nothing, a child that cannot be made told in its '
            'own row beside its siblings, and a child that is made: a helper of the agent that forked, at work',
    'sessions': (AGENT, FORKED),
    'steps': [
        send('synthetic greeting alpha', 'alpha'),
        {'op': 'wait_reply', 'after': 'u-alpha'},
        {'op': 'quiesce'},
        LOG,
        # What asks wrongly is refused whole, in a word for the reason and in words.
        fork({}, 'a fork that asks for nothing'),
        fork_raw('', 'a fork without a body'),
        fork_raw('[1]', 'a fork whose body is a list'),
        fork_raw('{not json', 'a fork whose body is no JSON'),
        fork({'children': 'two'}, 'children that are no list'),
        fork({'children': []}, 'no children'),
        fork({'children': ['one']}, 'a child that is no object'),
        fork({'children': [{'name': 'Fork One'}]}, 'a child without a task'),
        fork({'children': [{'task': 'synthetic task one'}]}, 'a child without a name'),
        fork({'children': [{'name': 'x' * 65, 'task': 'synthetic task one'}]}, 'a child whose name is too long'),
        fork({'children': [CHILD, {**CHILD, 'name': ' FORK ONE '}]}, 'two children of one name'),
        fork({'children': [{'name': f'Fork {number}', 'task': 'synthetic task'} for number in range(17)]},
             'more children than a fork may make'),
        fork({'count': 17, 'task': 'synthetic task one'}, 'more of one task than a fork may make'),
        fork({'count': 'several', 'task': 'synthetic task one'}, 'a count that is no number'),
        fork({'count': 2}, 'so many of no task'),
        fork({'count': 0, 'task': 'synthetic task one'}, 'none of a task'),
        fork({'children': [{**CHILD, 'worktree': 'repo:branch'}]}, 'a worktree that is no object'),
        fork({'children': [{**CHILD, 'worktree': {'branch': 'synthetic'}}]}, 'a worktree without a repository'),
        fork({'children': [{**CHILD, 'worktree': {'repo': '{workspace}'}}]}, 'a worktree without a branch'),
        fork({'children': [CHILD, {**CHILD, 'name': 'Fork Two', 'backend': 'abacus'}]},
             'a child on a tool no Host has'),
        fork({'children': [CHILD]}, 'a fork of an agent that is not there', session='nobody-there'),
        fork({}, 'a fork that asks wrongly of an agent that is not there', session='nobody-there'),
        {**SNAPSHOT, 'name': 'the snapshot when nothing was made'},
        # A child that cannot be made is told in its own row, and its sibling beside it too: where one is to
        # work is no directory, and what the other is to have a worktree of is no repository.
        fork({'children': [{**CHILD, 'cwd': '{workspace}/nowhere'},
                           {**CHILD, 'name': 'Fork Two',
                            'worktree': {'repo': '{workspace}/no-repository', 'branch': 'synthetic'}}]},
             'children that cannot be made'),
        {**SNAPSHOT, 'name': 'the snapshot when no child could be made'},
        # A child that is made: a helper of the agent that forked, with its task delivered.
        {**fork({'children': [CHILD]}, 'a fork of one child'), 'save': {'forked': 'children.0.session'}},
        {'op': 'quiesce', 'session': '{forked}'},
        {'op': 'quiesce'},
        get('/agent-helper-state?session={forked}', 'who made the child and how its work stands'),
        get(f'/agent-helper-state?session={AGENT}', 'the same of the agent that forked'),
        {**LOG, 'name': 'the conversation of the agent that forked'},
        # An agent that has said nothing yet has nothing a child could start from: one on the tool of the
        # scenarios, and one on another.
        post('/agents', {**MAKE_HELPER, 'name': 'Quiet One', 'session': 'quiet-one'}, 'an agent that says nothing'),
        fork({'children': [{**CHILD, 'name': 'Fork Three'}]}, 'a fork of an agent that has said nothing',
             session='quiet-one'),
        fork({'children': [{**CHILD, 'name': 'Fork Four'}]}, 'a fork of an agent on another tool that has said nothing',
             session='rachel'),
    ],
})

BY_ID = {scenario['id']: scenario for scenario in SCENARIOS}
