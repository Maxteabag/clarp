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


UPGRADE = {'Upgrade': 'websocket', 'Connection': 'Upgrade', 'Sec-WebSocket-Version': '13',
           'Sec-WebSocket-Key': 'cmVwbGF5LWhhcm5lc3Mta2V5'}  # a fixed synthetic key

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
            # The tool-explanation worker settles each explanation on its own schedule
            # (queued/running -> failed here, no provider): wait for it before the final
            # reads, or they race it (rf-routes, 3 Nov: pending vs failed, one live frame).
            # The job row settles before the live patch goes out, so wait for the patch
            # too (rf-routes G2, 4 Nov: the same frame, missing under load).
            {'op': 'wait_sql', 'sql': "SELECT (SELECT count(*) FROM tool_explanation_jobs) > 0 AND "
                                      "(SELECT count(*) FROM tool_explanation_jobs WHERE status IN ('queued', 'running')) = 0"},
            {'op': 'wait_explained'},
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
            # The tool-explanation worker settles each explanation on its own schedule
            # (queued/running -> failed here, no provider): wait for it before the final
            # reads, or they race it (rf-routes, 3 Nov: pending vs failed, one live frame).
            # The job row settles before the live patch goes out, so wait for the patch
            # too (rf-routes G2, 4 Nov: the same frame, missing under load).
            {'op': 'wait_sql', 'sql': "SELECT (SELECT count(*) FROM tool_explanation_jobs) > 0 AND "
                                      "(SELECT count(*) FROM tool_explanation_jobs WHERE status IN ('queued', 'running')) = 0"},
            {'op': 'wait_explained'},
            {'op': 'quiesce'},
            {'op': 'get', 'path': '/log?session=rachel'},
            {'op': 'get', 'path': '/live?session=rachel'},
        ],
    },
    {
        'id': 'limited-device-scope',
        'what': 'a limited-scope paired device: agent terminal, schedule PATCH, /dirs, all past sessions, '
                'unmatched PATCH (HTTP-01/02/03; records today\'s behaviour until the fixes land)',
        'steps': [
            {'op': 'cli', 'name': 'pair limited', 'save': {'limited_code': 'code'},
             'args': ['pair', 'create', '--scope', 'limited', '--name', 'Replay limited phone',
                      '--url', 'http://127.0.0.1:{port}', '--allow-loopback', '--json']},
            {'op': 'post', 'path': '/pairing/exchange', 'name': 'exchange limited',
             'body': {'code': '{limited_code}', 'device_name': 'Replay limited phone'},
             'save': {'limited': 'device.token'}},
            {'op': 'post', 'path': '/agent-schedules', 'name': 'schedule (administrator)',
             'body': {'session': 'rachel', 'name': 'synthetic schedule tango', 'cron_expression': '0 0 1 1 *',
                      'prompt': 'synthetic scheduled prompt', 'enabled': False},
             'save': {'schedule': 'schedule.schedule_id'}},
            {'op': 'get', 'path': '/terminal/rachel', 'auth': 'limited', 'name': 'terminal (limited)'},
            {'op': 'patch', 'path': '/schedules/{schedule}', 'auth': 'limited', 'name': 'schedule patch (limited)',
             'body': {'prompt': 'synthetic changed prompt', 'cron_expression': '0 0 2 1 *'}},
            {'op': 'delete', 'path': '/schedules/{schedule}', 'auth': 'limited', 'name': 'schedule delete (limited)'},
            {'op': 'get', 'path': '/dirs?path={root}/workspace', 'auth': 'limited', 'name': 'dirs (limited)'},
            {'op': 'get', 'path': '/past-sessions?scope=all', 'auth': 'limited', 'name': 'past sessions (limited)'},
            {'op': 'patch', 'path': '/no-such-route', 'auth': 'limited', 'name': 'unmatched patch (limited)',
             'body': {}},
            {'op': 'patch', 'path': '/no-such-route', 'name': 'unmatched patch (administrator)', 'body': {}},
            {'op': 'get', 'path': '/agent-schedules?session=rachel', 'name': 'schedules after'},
        ],
    },
    {
        'id': 'janitor-terminal',
        'what': 'a full-scope device asks for a janitor\'s terminal, plain and as a WebSocket upgrade (HTTP-01b: '
                'only the upgrade reaches the janitor check)',
        'steps': [
            {'op': 'cli', 'name': 'pair full', 'save': {'full_code': 'code'},
             'args': ['pair', 'create', '--scope', 'full', '--name', 'Replay full phone',
                      '--url', 'http://127.0.0.1:{port}', '--allow-loopback', '--json']},
            {'op': 'post', 'path': '/pairing/exchange', 'name': 'exchange full',
             'body': {'code': '{full_code}', 'device_name': 'Replay full phone'}, 'save': {'full': 'device.token'}},
            {'op': 'save_sql', 'var': 'janitor',
             'sql': 'SELECT session FROM agents WHERE is_janitor = 1 AND deleted_at IS NULL ORDER BY session LIMIT 1'},
            {'op': 'get', 'path': '/terminal/{janitor}', 'auth': 'full', 'name': 'janitor terminal (full device)'},
            # A valid upgrade reaches the janitor check (HTTP-01b); a plain GET is answered
            # 426 before it. The replay Host refuses to spawn a PTY, so getting past every
            # check ends without a response (status 0).
            {'op': 'get', 'path': '/terminal/{janitor}', 'auth': 'full', 'name': 'janitor terminal upgrade (full device)',
             'headers': UPGRADE},
            {'op': 'get', 'path': '/terminal/rachel', 'auth': 'full', 'name': 'agent terminal upgrade (full device)',
             'headers': UPGRADE},
            {'op': 'get', 'path': '/terminal/rachel', 'auth': 'full', 'name': 'agent terminal (full device)'},
        ],
    },
    {
        'id': 'voice-cartesia-no-key',
        'what': 'a spoken reply routed to Cartesia with no key or voice, raw-pcm delivery, elevenlabs '
                'fallback configured (VP-02; records today\'s behaviour until the fix lands)',
        'config': ('[tts]\nprovider = "cartesia"\nfallback = "elevenlabs"\n'
                   '[audio]\ndelivery = "raw-pcm"\n[network]\nadvertise_lan = false\n'),
        'steps': [
            {'op': 'post', 'path': '/send', 'name': 'send uniform (spoken)',
             'body': {'session': 'rachel', 'text': 'synthetic spoken question uniform',
                      'client_msg_id': 'uniform', 'synthesize_audio': True}},
            {'op': 'wait_settled', 'message_id': 'u-uniform'},
            {'op': 'quiesce'},
            {'op': 'get', 'path': '/log?session=rachel'},
            {'op': 'get', 'path': '/clips/recoverable'},
        ],
    },
    {
        'id': 'preempt-live-process',
        'what': 'a plain send to a busy non-steerable (claude) agent preempts it while the old CLI is still '
                'alive (TP-01; provider/claude-procs.jsonl records start/sigterm/exit order)',
        'sessions': ('claire',),
        'steps': [
            _send('claire', '[qa-slow][replay-linger] synthetic long claude task', 'victor'),
            {'op': 'wait_open', 'message_id': 'u-victor'},
            {'op': 'wait_busy', 'session': 'claire', 'busy': True},
            {'op': 'wait_file', 'name': 'claude-procs.jsonl', 'contains': '"start"'},
            _send('claire', 'synthetic preempting whiskey', 'whiskey'),
            {'op': 'wait_settled', 'message_id': 'u-whiskey'},
            # The preempted CLI lingers 1.5 s after SIGTERM before it logs its exit.
            {'op': 'wait_file', 'name': 'claude-procs.jsonl', 'all_exited': True},
            {'op': 'quiesce', 'sessions': ('claire',)},
            {'op': 'get', 'path': '/log?session=claire'},
            {'op': 'get', 'path': '/status?session=claire'},
        ],
    },
    {
        'id': 'codex-pool-stop-during-start',
        'what': 'two codex agents on the one shared app-server: one is stopped while its turn/start is '
                'still in flight (fake_provider holds the request), the other completes (Codex pool '
                'stage 1/2 baseline; provider/codex-procs.jsonl records app-server starts and the hold)',
        'sessions': ('rachel', 'mike'),
        'steps': [
            # /send answers once the provider has started the turn, so both sends
            # are open requests (joined below) while rachel's start is held.
            {**_send('rachel', '[qa-slow][replay-hold-start] synthetic pool turn to stop', 'pool-a'),
             'background': 'send pool-a'},
            {'op': 'wait_file', 'name': 'codex-procs.jsonl', 'contains': '"held"'},
            # rachel's own follow-up while her start is held: her launching send above
            # waits for its start by design; a follow-up answers at once (queued).
            {**_send('rachel', 'synthetic follow-up while the start is held', 'pool-a2'),
             'background': 'send pool-a2'},
            {'op': 'wait_sql', 'sql': "SELECT count(*) >= 2 FROM prompt_admissions p JOIN agents a "
                                      "ON a.agent_id = p.agent_id WHERE a.session = 'rachel'"},
            {**_send('mike', 'synthetic pool turn for mike', 'pool-b'), 'background': 'send pool-b'},
            # mike's send is admitted, and his turn runs to the end while rachel's start
            # is still held: one agent's in-flight start holds up no other agent (TP-N3
            # (ii); on main his turn waits behind it). Settled first, so the Stop and the
            # release below never race his turn.
            {'op': 'wait_sql', 'sql': "SELECT count(*) FROM prompt_admissions p JOIN agents a "
                                      "ON a.agent_id = p.agent_id WHERE a.session = 'mike'"},
            {'op': 'wait_settled', 'message_id': 'u-pool-b'},
            # The stop waits for the turn lock that rachel's start holds: an open request
            # (host.py records that it is waiting before it takes the lock).
            {'op': 'post', 'path': '/stop', 'body': {'session': 'rachel'}, 'background': 'stop rachel'},
            {'op': 'wait_file', 'name': 'host-events.jsonl', 'contains': '"stop-waiting"'},
            # Released after the stop: the slow turn then starts and the pending stop
            # interrupts it, whichever of the stop and mike's start takes the pool first.
            {'op': 'provider_file', 'name': 'release-start', 'text': '1'},
            {'op': 'join', 'name': 'stop rachel'},
            {'op': 'join', 'name': 'send pool-a'},
            {'op': 'join', 'name': 'send pool-a2'},
            {'op': 'join', 'name': 'send pool-b'},
            {'op': 'wait_settled', 'message_id': 'u-pool-a'},
            {'op': 'wait_settled', 'message_id': 'u-pool-b'},
            # rachel's stopped execution has really ended (its Codex handle is gone) before
            # the resume: resume defers a row while the agent still has an active handle and
            # retries a second later, so an earlier resume would race that (rf-fix).
            {'op': 'quiesce', 'sessions': ('rachel',)},
            # A follow-up acknowledged as queued survives the Stop in the paused queue
            # (lead's decision on TP-N3 (iii)): resumed, it runs exactly once.
            {'op': 'get', 'path': '/turn-queue?session=rachel'},
            {'op': 'post', 'path': '/turn-queue/resume', 'body': {'session': 'rachel'}},
            {'op': 'wait_settled', 'message_id': 'u-pool-a2'},
            {'op': 'quiesce', 'sessions': ('rachel', 'mike')},
            {'op': 'get', 'path': '/turn-queue?session=rachel'},
            {'op': 'get', 'path': '/log?session=rachel'},
            {'op': 'get', 'path': '/log?session=mike'},
            {'op': 'get', 'path': '/agents/snapshot'},
        ],
    },
    {
        'id': 'push-notifications',
        'what': 'a registered iPhone (device token and a Live Activity token) through the real apns '
                'module to a recording relay (host.py): which pushes a turn sends, with their type, '
                'priority, collapse id and payload (APNS-N1 baseline)',
        'config': ('[tts]\nprovider = "none"\n[network]\nadvertise_lan = false\n'
                   '[apns]\nmode = "relay"\n'
                   '[audio_central]\nurl = "https://relay.replay.invalid"\n'
                   'credential = "cav1.replay.synthetic.credential"\n'),
        'steps': [
            {'op': 'post', 'path': '/devices', 'body': {
                'token': 'a1' * 32, 'session': 'rachel', 'environment': 'sandbox', 'platform': 'ios'}},
            {'op': 'post', 'path': '/devices/live-activity', 'body': {
                'token': 'b2' * 32, 'activity_id': 'replay-activity', 'kind': 'agents-working',
                'environment': 'sandbox'}},
            _send('rachel', 'synthetic question with a push', 'push1'),
            {'op': 'wait_settled', 'message_id': 'u-push1'},
            # The Live Activity pusher sends the first change at once and the rest of the
            # turn in one push after its 15 s minimum interval: wait for that one too.
            {'op': 'wait_file', 'name': 'push-relay.jsonl', 'contains': '"liveactivity"', 'count': 2,
             'timeout': 30},
            {'op': 'quiesce'},
            {'op': 'get', 'path': '/log?session=rachel'},
        ],
    },
    {
        'id': 'stop-holds-then-edit-queue',
        'what': 'a Stop holds three queued sends; the held queue is edited (PUT text), one is '
                'cancelled (DELETE), one is sent at once while paused (POST /send), then the queue '
                'resumes: which turn runs, with which text, in what order (QUEUE-N1, TP-N1/N2, RM-N1)',
        'steps': [
            _send('rachel', '[qa-slow] synthetic task before an edited queue', 'quebec'),
            {'op': 'wait_provider_started', 'marker': '[qa-slow] synthetic task before an edited queue'},
            {'op': 'wait_open', 'message_id': 'u-quebec'},
            {'op': 'wait_state', 'session': 'rachel', 'kind': 'thinking', 'count': 2},
            _send('rachel', 'synthetic queued romeo', 'romeo', queue_if_busy=True),
            _send('rachel', 'synthetic queued sierra', 'sierra', queue_if_busy=True),
            _send('rachel', 'synthetic queued tango', 'tango3', queue_if_busy=True),
            {'op': 'post', 'path': '/stop', 'body': {'session': 'rachel'}},
            {'op': 'wait_settled', 'message_id': 'u-quebec'},
            {'op': 'quiesce'},
            # The held queue: romeo, sierra, tango3 (paused).
            {'op': 'get', 'path': '/turn-queue?session=rachel'},
            {'op': 'put', 'path': '/turn-queue/{queue_id}', 'from_last': {'queue_id': 'items.0.id'},
             'body': {'text': 'synthetic edited romeo'}},
            {'op': 'get', 'path': '/turn-queue?session=rachel'},
            {'op': 'delete', 'path': '/turn-queue/{queue_id}', 'from_last': {'queue_id': 'items.1.id'}},
            {'op': 'get', 'path': '/turn-queue?session=rachel'},
            # tango3 sent at once while the queue is still paused.
            {'op': 'post', 'path': '/turn-queue/{queue_id}/send', 'from_last': {'queue_id': 'items.1.id'}},
            {'op': 'wait_settled', 'message_id': 'u-tango3'},
            {'op': 'quiesce'},
            {'op': 'get', 'path': '/turn-queue?session=rachel'},
            # Resumed: the edited romeo runs, with its new text.
            {'op': 'post', 'path': '/turn-queue/resume', 'body': {'session': 'rachel'}},
            {'op': 'wait_settled', 'message_id': 'u-romeo'},
            {'op': 'quiesce'},
            {'op': 'get', 'path': '/turn-queue?session=rachel'},
            {'op': 'get', 'path': '/log?session=rachel'},
        ],
    },
    {
        'id': 'voice-streamed-clip',
        'what': 'a spoken reply streamed through the real Cartesia adapter (synthetic offline response, '
                'host.py) with chunked-file delivery: the clip file, its stream bytes and its chunks',
        'config': ('[tts]\nprovider = "cartesia"\nfallback = "none"\n'
                   '[audio]\ndelivery = "chunked-file"\n[network]\nadvertise_lan = false\n'
                   '[cartesia]\napi_key = "replay-synthetic-key"\n'
                   '[cartesia.voices]\nRachel = "replay-voice-rachel"\n'),
        'steps': [
            # Each adapter read waits 0.5 s, so the live stream can be joined mid-production.
            {'op': 'provider_file', 'name': 'cartesia-pace', 'text': '0.5'},
            {'op': 'post', 'path': '/send', 'name': 'send xray (spoken)',
             # The fake provider echoes the prompt, so its reply carries this <speak> block,
             # which is what the Host enqueues for synthesis.
             'body': {'session': 'rachel', 'text': 'synthetic question <speak>synthetic spoken answer xray</speak>',
                      'client_msg_id': 'xray', 'synthesize_audio': True}},
            # Join the clip's live stream while it is produced: it carries every chunk the
            # adapter hands the delivery through on_chunk(index, bytes), via the clip broker.
            {'op': 'wait_sql', 'sql': "SELECT count(*) FROM clips WHERE producer_status = 'streaming'"},
            # The row is committed before the broker opens the clip's stream, so a GET in
            # between finds neither stream nor file (404; G2 noise, 5 Nov). Clients learn
            # the stream URL from the audio event, sent after the broker opens: wait for it.
            {'op': 'wait_sse', 'match': {'type': 'audio', 'streamable': True}},
            {'op': 'save_sql', 'var': 'clip', 'sql': 'SELECT clip_id FROM clips ORDER BY clip_id LIMIT 1'},
            {'op': 'get', 'path': '/clips/{clip}/stream', 'name': 'clip live stream'},
            {'op': 'wait_settled', 'message_id': 'u-xray'},
            {'op': 'wait_sql', 'sql': "SELECT count(*) FROM clips WHERE producer_status IN ('complete', 'failed')"},
            {'op': 'quiesce'},
            # And the finished clip, served from its file.
            {'op': 'get', 'path': '/clips/{clip}/stream', 'name': 'clip file stream'},
            {'op': 'get', 'path': '/log?session=rachel'},
        ],
    },
    {
        'id': 'goal-turn',
        'what': 'a goal is set on an agent; the provider runs an unrequested goal turn and completes it',
        'steps': [
            _send('rachel', 'synthetic opener romeo', 'romeo'),
            {'op': 'wait_settled', 'message_id': 'u-romeo'},
            {'op': 'quiesce'},
            {'op': 'post', 'path': '/agent-goal',
             'body': {'session': 'rachel', 'objective': 'synthetic goal sierra'}},
            {'op': 'wait_sql', 'sql': "SELECT count(*) FROM agent_goals g JOIN agents a ON a.agent_id = g.agent_id "
                                      "WHERE a.session = 'rachel' AND g.status = 'complete'"},
            {'op': 'quiesce'},
            {'op': 'get', 'path': '/log?session=rachel'},
            {'op': 'get', 'path': '/agents/snapshot'},
        ],
    },
    {
        'id': 'cli-prompt-and-sessions',
        'what': "the build's clarp-admin lists sessions and prompts an agent through the replay Host",
        'steps': [
            {'op': 'cli', 'name': 'sessions', 'args': ['sessions']},
            {'op': 'cli', 'name': 'prompt', 'args': ['prompt', '--to', 'rachel', '--text', 'synthetic cli quebec']},
            {'op': 'quiesce'},
            {'op': 'get', 'path': '/log?session=rachel'},
            {'op': 'cli', 'name': 'sessions again', 'args': ['sessions']},
        ],
    },
    {
        'id': 'stop-at-admission',
        'what': 'a Stop between the two thinking rows of a turn start (pins TP-N1)',
        # Before TP-N1's fix (a307bbd9) the late thinking row overwrote the stop and the
        # agent stayed busy, and quiesce timed out (10/10 runs at dfc558ba). Since the
        # fix, whether the Stop lands before or after the second thinking row is timing
        # (both are correct; 2/10 runs differ), so its run error counts, its captures
        # are not compared.
        'errors_only': 'TP-N1 race: either order of Stop and the second thinking row is correct',
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
