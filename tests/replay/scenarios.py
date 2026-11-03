"""The CI replay corpus: synthetic turns through the fakes.

Every text here is synthetic. A scenario is a list of steps the capture
runner executes in order (see capture.run_step); waits poll observable state
(database, provider request log, /status), never the clock. ``quiesce`` (every
listed session idle and the SSE stream quiet) comes before the final reads, so
they never race the Host's background import.
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
            {'op': 'quiesce'},
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
            {'op': 'quiesce'},
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
            # Stop only once both thinking rows of the turn exist (spawn, then provider
            # turn start): a Stop between them is overwritten by the late row and the
            # agent stays busy (pre-existing race, reported to the lead).
            {'op': 'wait_state', 'session': 'rachel', 'kind': 'thinking', 'count': 2},
            {'op': 'post', 'path': '/stop', 'body': {'session': 'rachel'}},
            {'op': 'wait_settled', 'message_id': 'u-tostop'},
            {'op': 'quiesce'},
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
            {'op': 'quiesce'},
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
            # Message revisions come from one global clock: Rachel's background
            # import must finish before Mike's send, or his revision races hers.
            {'op': 'quiesce'},
            _send('mike', 'synthetic question for mike', 'echo'),
            {'op': 'wait_settled', 'message_id': 'u-echo'},
            {'op': 'quiesce', 'sessions': ('rachel', 'mike')},
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
            {'op': 'quiesce'},
            {'op': 'get', 'path': '/log?session=rachel'},
            {'op': 'get', 'path': '/status?session=rachel'},
        ],
    },
    {
        'id': 'steer-into-running',
        'what': 'a send without queue_if_busy while a turn runs is steered into it',
        'steps': [
            _send('rachel', '[qa-slow] synthetic running task', 'golf'),
            {'op': 'wait_provider_started', 'marker': '[qa-slow] synthetic running task'},
            {'op': 'wait_open', 'message_id': 'u-golf'},
            _send('rachel', 'synthetic steering hotel', 'hotel'),
            {'op': 'wait_settled', 'message_id': 'u-golf'},
            {'op': 'quiesce'},
            {'op': 'get', 'path': '/log?session=rachel'},
        ],
    },
    {
        'id': 'focus-and-reads',
        'what': 'focus moves between agents; the read model answers for both',
        'sessions': ('rachel', 'mike'),
        'steps': [
            {'op': 'post', 'path': '/select', 'body': {'session': 'mike'}},
            {'op': 'post', 'path': '/focus', 'body': {'session': 'rachel'}},
            {'op': 'get', 'path': '/agents/snapshot'},
            {'op': 'get', 'path': '/live?session=rachel'},
            {'op': 'get', 'path': '/turn-queue?session=mike'},
            {'op': 'get', 'path': '/attention'},
            {'op': 'get', 'path': '/status?session=mike'},
        ],
    },
    {
        'id': 'voice-clip-turn',
        'what': 'a recorded clip is transcribed (stub STT) and sent as a voice turn',
        'steps': [
            {'op': 'post', 'path': '/transcribe', 'name': 'transcribe india',
             'raw': {'content_type': 'audio/webm', 'bytes': 'synthetic-audio-bytes-india'},
             'headers': {'X-Transcription-Session': 'rachel', 'X-Utterance-ID': 'utt-india'}},
            {'op': 'post', 'path': '/send', 'name': 'send india',
             'body': {'session': 'rachel', 'text': 'replay voice prompt', 'client_msg_id': 'india',
                      'synthesize_audio': False, 'utterance_id': 'utt-india'},
             'body_from_last': {'trace_id': 'trace_id', 'transcription_id': 'transcription_id'}},
            {'op': 'wait_settled', 'message_id': 'u-india'},
            {'op': 'quiesce'},
            {'op': 'get', 'path': '/log?session=rachel'},
            {'op': 'get', 'path': '/voice-events?session=rachel'},
        ],
    },
    {
        'id': 'stop-with-queue',
        'what': 'a Stop while a message waits: the turn is interrupted, the queue pauses, resume runs it',
        'steps': [
            _send('rachel', '[qa-slow] synthetic task before a queue', 'juliet'),
            {'op': 'wait_provider_started', 'marker': '[qa-slow] synthetic task before a queue'},
            {'op': 'wait_open', 'message_id': 'u-juliet'},
            {'op': 'wait_state', 'session': 'rachel', 'kind': 'thinking', 'count': 2},
            _send('rachel', 'synthetic waiting kilo', 'kilo', queue_if_busy=True),
            {'op': 'post', 'path': '/stop', 'body': {'session': 'rachel'}},
            {'op': 'wait_settled', 'message_id': 'u-juliet'},
            {'op': 'quiesce'},
            {'op': 'get', 'path': '/turn-queue?session=rachel'},
            {'op': 'post', 'path': '/turn-queue/resume', 'body': {'session': 'rachel'}},
            {'op': 'wait_settled', 'message_id': 'u-kilo'},
            {'op': 'quiesce'},
            {'op': 'get', 'path': '/turn-queue?session=rachel'},
            {'op': 'get', 'path': '/log?session=rachel'},
        ],
    },
    {
        'id': 'queue-cancel',
        'what': 'a queued message is deleted before it runs',
        'steps': [
            _send('rachel', '[qa-slow] synthetic task before a cancel', 'lima'),
            {'op': 'wait_provider_started', 'marker': '[qa-slow] synthetic task before a cancel'},
            {'op': 'wait_open', 'message_id': 'u-lima'},
            _send('rachel', 'synthetic message to cancel', 'mike', queue_if_busy=True),
            {'op': 'get', 'path': '/turn-queue?session=rachel'},
            {'op': 'delete', 'path': '/turn-queue/{queue_id}', 'from_last': {'queue_id': 'items.0.id'}},
            {'op': 'wait_settled', 'message_id': 'u-lima'},
            {'op': 'quiesce'},
            {'op': 'get', 'path': '/turn-queue?session=rachel'},
            {'op': 'get', 'path': '/log?session=rachel'},
        ],
    },
    {
        'id': 'tool-use',
        'what': 'a turn with reasoning and a command that streams output and succeeds',
        'steps': [
            _send('rachel', '[replay-tool] synthetic look around oscar', 'oscar'),
            {'op': 'wait_settled', 'message_id': 'u-oscar'},
            {'op': 'quiesce'},
            {'op': 'get', 'path': '/log?session=rachel'},
            {'op': 'get', 'path': '/live?session=rachel'},
        ],
    },
    {
        'id': 'tool-failure',
        'what': 'a turn whose command fails (exit 2) before the reply',
        'steps': [
            _send('rachel', '[replay-tool-fail] synthetic look for papa', 'papa'),
            {'op': 'wait_settled', 'message_id': 'u-papa'},
            {'op': 'quiesce'},
            {'op': 'get', 'path': '/log?session=rachel'},
            {'op': 'get', 'path': '/live?session=rachel'},
        ],
    },
    {
        'id': 'stop-at-admission',
        'what': 'a Stop between the two thinking rows of a turn start (pins TP-N1)',
        # Known bug: the late thinking row overwrites the stop and the agent stays busy,
        # so this scenario fails (in most runs) until the fix lands. It runs and is
        # reported, but never counts in the zero or the exit code.
        'known_bug': 'TP-N1',
        'steps': [
            _send('rachel', '[qa-slow] synthetic task stopped at once', 'november'),
            {'op': 'wait_open', 'message_id': 'u-november'},
            {'op': 'post', 'path': '/stop', 'body': {'session': 'rachel'}},
            {'op': 'wait_settled', 'message_id': 'u-november'},
            {'op': 'quiesce'},
            {'op': 'get', 'path': '/status?session=rachel'},
        ],
    },
]

GATING = [s for s in SCENARIOS if not s.get('known_bug')]
KNOWN_BUGS = [s for s in SCENARIOS if s.get('known_bug')]

BY_ID = {s['id']: s for s in SCENARIOS}
