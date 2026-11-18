#!/usr/bin/env python3
"""Writes the vectors the session of a live call is tested against.

A ring held as a live voice call (`docs/agent-rings.md`, feature `agent_ring_live`) is, once a phone
joined it, one session: the phone on one side, the voice model on the other, and the Host between
them. The Host gives the voice its opening once both sides are ready, lets no sound of the voice
reach the phone before the voice took that opening, and no sound of the user reach the voice before
either; it ends the call for whoever leaves, and for whatever goes wrong, with a reason.

This runs the Python Host's own session (`server/lib/agent_ring_live_socket.py`, `LiveSocket`) on a
ring that was placed, answered and joined through its own modules, with a phone, a voice and a
clock made up here, through stories of what each side says and of time passing, and writes what the
phone and the voice were told after each step, and what the ring's own record says of the call, to
`crates/clarp-kernel/tests/fixtures/live-session-vectors.json`.

    "$(sed -n 1p ~/.local/share/clarp/current/SERVICE_PYTHON)" server-rs/tools/live-session-vectors.py

It runs in a process of its own whose every home, data, config and cache directory is a new empty
directory (the replay harness's own isolation, `tests/replay/host.py`), with no way out of the
machine. No Host is touched or started and no provider is asked anything: the voice is a stand-in
that writes down what it is sent.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import importlib.util
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT = HERE.parent / 'crates' / 'clarp-kernel' / 'tests' / 'fixtures' / 'live-session-vectors.json'

AGENTS = [{'session': 'theo-97e5', 'agent_id': 'a-theo', 'persona': 'Theo'}]
PHONE = 'device_a'
WHY = 'Possible water leak at pump 01'
LINE = 'Camera 2 shows a possible water leak at pump 01. Shall I find an available field worker?'
LIVE = 'agent_live_v1'
PERSONA = 'You are Theo, who guards the pumps.\n'
PROFILE = {'profile_id': 'pump-guard', 'version': '3', 'agent_session': 'theo-97e5', 'display_name': 'Theo the guard',
           'files': {'persona': {'file': 'PERSONA.md', 'sha256': hashlib.sha256(PERSONA.encode()).hexdigest()}},
           'stories': {'pump': ['persona'], 'user_call': ['persona']},
           'user_call_default': 'live', 'user_call_opening': 'Hei, det er Theo.'}
EVENT = {'version': 1, 'profile_id': 'pump-guard', 'story': 'pump', 'source': 'leak-watch',
         'event': {'id': 'evt-1', 'time': '2026-11-18T04:08:00+01:00', 'camera_id': 'cam-2', 'camera_label': 'Camera 2',
                   'when_text': 'just now', 'what_text': 'Water on the floor below pump 01.', 'log': ['04:08 water seen']},
         'opening_text': LINE}


def sound(count):
    """Sound of `count` bytes, as it goes between a phone, a Host and a voice: in base64. All of it is silence, and
    one piece is told from another by how long it is."""
    return base64.b64encode(bytes(count)).decode('ascii')


def start(key=True, connects=True, late=None):
    """What the Host does once a phone joined: it reaches the voice and tells both sides of the call. `late`: what
    became of the call between the join and this (`closed`: its phone closed it)."""
    return {'do': 'start', 'key': key, 'connects': connects, 'late': late}


def phone(event):
    return {'do': 'phone', 'said': event}


def ready(audio=True, capture=True, **other):
    """The phone says that it can play and that it listens: with the names of its call, or `other` ones."""
    return {'do': 'ready', 'audio': audio, 'capture': capture, 'other': other}


def hears(count=320, **more):
    return phone({'type': 'session.input_audio.append', 'audio': sound(count), **more})


def voice(kind, **more):
    return {'do': 'voice', 'said': {'type': kind, **more}}


def speaks(count=480):
    return voice('session.output_audio.delta', delta=sound(count))


def took(how='its'):
    """The voice says that it took an instruction: `its` names the opening, `none` names nothing, else that name."""
    return {'do': 'took', 'how': how}


def ticks(count=1):
    return {'do': 'ticks', 'count': count}


def wait(seconds):
    """Time passes between two ticks of the session's clock: so much of it."""
    return {'do': 'wait', 'seconds': seconds}


def gone(who):
    """A side is gone: `phone` (its socket ended) or `voice` (its socket ended)."""
    return {'do': 'gone', 'who': who}


def close(reason='user_hangup'):
    return {'do': 'close', 'reason': reason}


def fallback():
    return {'do': 'fallback'}


def device(state):
    return {'do': 'device', 'state': state}


def deaf(after=0):
    """The voice takes nothing more, once it has taken `after` things more: what is sent to it then fails."""
    return {'do': 'deaf', 'after': after}


def retired(reason='user_hangup'):
    """The call loses its authority on its ring, and its session is not told."""
    return {'do': 'retired', 'reason': reason}


OPENED = [start(), ready(), voice('session.started'), took()]

STORIES = [
    {'name': 'a call from its opening to the hangup',
     'steps': [start(), ticks(2), ready(), ticks(2), voice('session.started'), ticks(3), took(), speaks(), speaks(960), ticks(2),
               hears(), hears(640), ticks(2), phone({'type': 'oracle_v2.interrupt'}), voice('session.output_audio.interrupted'),
               speaks(), voice('session.input_transcript.delta', delta='hei'), phone({'type': 'something.else'}), ready(),
               voice('session.started'), took(), took('none'), ticks(12), phone({'type': 'session.close'}), speaks(), hears(),
               ticks(3), voice('session.closed')]},
    {'name': 'the voice is ready before the phone, and its sound is held until it took the opening',
     'steps': [start(), voice('session.started'), ticks(1), ready(audio=False), ready(capture=False), ready(audio=1, capture=1),
               ticks(1), ready(), speaks(480), speaks(482), speaks(2), took('none'), speaks(484), gone('phone'),
               speaks(), ticks(2)]},
    {'name': 'what the voice says it took, and when',
     'steps': [start(), took(), took('none'), ready(), voice('session.started'), took('another'), took(''), took(None), took(5),
               speaks(), took(), took(), took('another'), speaks(), gone('voice')]},
    {'name': 'the voice speaks before it was given its opening',
     'steps': [start(), ready(), speaks(), speaks(), took(), ticks(2)]},
    {'name': 'the voice speaks before anything', 'steps': [start(), voice('session.output_audio.delta', delta=5), speaks()]},
    {'name': 'sound that is none, while the opening is not yet taken',
     'steps': [start(), ready(), voice('session.started'), voice('session.output_audio.delta', delta='not base64!'), took()]},
    {'name': 'sound of an odd length, while the opening is not yet taken',
     'steps': [start(), ready(), voice('session.started'), speaks(480), speaks(481), took()]},
    {'name': 'empty sound, while the opening is not yet taken',
     'steps': [start(), ready(), voice('session.started'), voice('session.output_audio.delta', delta=''), took()]},
    {'name': 'more sound than is held', 'steps': [start(), ready(), voice('session.started'), speaks(72000), speaks(72000), speaks(2), took()]},
    {'name': 'as much sound as is held', 'steps': [start(), ready(), voice('session.started'), speaks(72000), speaks(72000), took(), speaks(2)]},
    {'name': 'one piece of sound too large to hold', 'steps': [start(), ready(), voice('session.started'), speaks(144002), took()]},
    {'name': 'the largest piece of sound that is held', 'steps': [start(), ready(), voice('session.started'), speaks(144000), took()]},
    {'name': 'a piece too long to be looked at, which is no sound either',
     'steps': [start(), ready(), voice('session.started'), voice('session.output_audio.delta', delta='!' * 192001), took()]},
    {'name': 'a piece as long as may be looked at, which is no sound',
     'steps': [start(), ready(), voice('session.started'), voice('session.output_audio.delta', delta='!' * 192000), took()]},
    {'name': 'the user is heard before the voice took its opening',
     'steps': [start(), ready(), voice('session.started'), hears(), hears(), took()]},
    {'name': 'the user is heard before anything', 'steps': [start(), hears()]},
    {'name': 'what a phone sends for sound that is none',
     'steps': OPENED + [hears(2), phone({'type': 'session.input_audio.append', 'audio': 'no sound'}), hears()]},
    {'name': 'sound of the user of an odd length', 'steps': OPENED + [hears(3)]},
    {'name': 'no sound of the user at all', 'steps': OPENED + [phone({'type': 'session.input_audio.append', 'audio': ''})]},
    {'name': 'sound of the user that is no text', 'steps': OPENED + [phone({'type': 'session.input_audio.append', 'audio': 7})]},
    {'name': 'sound of the user without the sound', 'steps': OPENED + [phone({'type': 'session.input_audio.append'})]},
    {'name': 'the largest piece of sound of the user', 'steps': OPENED + [hears(196608), hears(196610)]},
    {'name': 'a piece of sound of the user that is one sample too large', 'steps': OPENED + [hears(2), hears(196610), hears()]},
    {'name': 'a phone that says it is ready for another call',
     'steps': [start(), ready(call_id='alv_none'), ready(), voice('session.started')]},
    {'name': 'a phone ready for another epoch', 'steps': [start(), ready(call_epoch=2)]},
    {'name': 'a phone ready for another attempt', 'steps': [start(), ready(attempt_id='att_none')]},
    {'name': 'nobody is ready in time', 'steps': [start(), ticks(150), ticks(1), ticks(1), ready()]},
    {'name': 'only the phone is ready in time', 'steps': [start(), ready(), ticks(151), voice('session.started')]},
    {'name': 'only the voice is ready in time', 'steps': [start(), voice('session.started'), ticks(151)]},
    {'name': 'both are ready just in time, and the voice takes its opening just in time',
     'steps': [start(), ticks(149), ready(), voice('session.started'), ticks(100), took(), ticks(60)]},
    {'name': 'the voice does not take its opening', 'steps': [start(), ready(), voice('session.started'), ticks(100), ticks(1), took()]},
    {'name': 'the two sides are ready a moment before their time is up',
     'steps': [start(), wait(14.86), ticks(1), ready(), voice('session.started'), took()]},
    {'name': 'the two sides are not ready a moment after their time is up', 'steps': [start(), wait(14.91), ticks(1), ready()]},
    {'name': 'the voice takes its opening a moment before its time is up',
     'steps': [start(), ready(), voice('session.started'), wait(9.86), ticks(1), took(), speaks()]},
    {'name': 'the voice takes its opening a moment after its time is up',
     'steps': [start(), ready(), voice('session.started'), wait(9.91), ticks(1), took()]},
    {'name': 'the voice says that something is wrong, before it took the opening',
     'steps': [start(), ready(), voice('error', error={'message': 'no'}), voice('session.started')]},
    {'name': 'the voice says that something is wrong, in the call',
     'steps': OPENED + [voice('error', error={'message': 'no'}), speaks(), voice('session.closed', usage={'seconds': 3}), speaks()]},
    {'name': 'the voice is gone before the call was under way', 'steps': [start(), ready(), gone('voice'), ready()]},
    {'name': 'the voice is gone in the call', 'steps': OPENED + [hears(), gone('voice'), hears()]},
    {'name': 'the phone is gone before anything', 'steps': [start(), gone('phone'), voice('session.started')]},
    {'name': 'the phone is no longer paired', 'steps': OPENED + [ticks(9), device('revoked'), ticks(1), ticks(1)]},
    {'name': 'the phone has fewer rights', 'steps': [start(), device('limited'), ticks(9), ticks(1)]},
    {'name': 'the phone closes the call beside its socket',
     'steps': OPENED + [hears(), close('transport_lost'), speaks(), hears(), ticks(2), phone({'type': 'session.close'})]},
    {'name': 'the phone closes the call before the voice is ready', 'steps': [start(), ready(), close(), voice('session.started'), ticks(1)]},
    {'name': 'the call is switched to an ordinary one', 'steps': [start(), ready(), fallback(), voice('session.started'), speaks(), ticks(1)]},
    {'name': 'a switch that comes too late', 'steps': OPENED + [fallback(), speaks(), hears(), close()]},
    {'name': 'the call lost its authority and its session was not told',
     'steps': OPENED + [ticks(5), retired(), speaks(), hears(), ticks(4), ticks(1), ticks(1)]},
    {'name': 'the call lost its authority, its session was not told, and only time passes',
     'steps': OPENED + [ticks(5), retired('transport_lost'), ticks(4), ticks(1), ticks(1)]},
    {'name': 'the call lost its authority before both sides were ready, and its session was not told',
     'steps': [start(), ready(), retired(), voice('session.started'), speaks(), ticks(1)]},
    {'name': 'the call lost its authority before its phone was ready, and its session was not told',
     'steps': [start(), voice('session.started'), retired('transport_lost'), ready(), ticks(1)]},
    {'name': 'the call lost its authority before its voice took the opening, and its session was not told',
     'steps': [start(), ready(), voice('session.started'), speaks(), retired(), took(), speaks(), ticks(1)]},
    {'name': 'the call lost its authority before its voice took the opening under no name',
     'steps': [start(), ready(), voice('session.started'), retired('transport_lost'), took('none'), ticks(1)]},
    {'name': 'the voice says it was interrupted, and the phone breaks in, before the call is under way',
     'steps': [start(), voice('session.output_audio.interrupted'), phone({'type': 'oracle_v2.interrupt'}), ready(),
               voice('session.started'), voice('session.output_audio.interrupted'), phone({'type': 'oracle_v2.interrupt'}),
               speaks(), voice('session.output_audio.interrupted'), took(), voice('session.output_audio.interrupted'),
               phone({'type': 'oracle_v2.interrupt'}), took('none')]},
    {'name': 'the call was closed before its session began', 'steps': [start(late='closed'), ready()]},
    {'name': 'a Host without a key for the voice', 'steps': [start(key=False), ready()]},
    {'name': 'a voice that cannot be reached', 'steps': [start(connects=False), ready(), ticks(1)]},
    {'name': 'a voice that takes nothing when it is given its opening',
     'steps': [start(), ready(), deaf(), voice('session.started'), took(), ticks(1)]},
    {'name': 'a voice that takes nothing in the call', 'steps': OPENED + [deaf(), hears(), ticks(1), speaks(), close()]},
    {'name': 'a voice that takes the silence and not its opening',
     'steps': [start(), ready(), deaf(1), voice('session.started'), speaks(), took(), ticks(1)]},
    {'name': 'a voice that takes nothing when the phone is ready last',
     'steps': [start(), voice('session.started'), deaf(), ready(), ticks(1)]},
    {'name': 'a voice that takes nothing while it waits for the user', 'steps': OPENED + [deaf(), ticks(1), ticks(1)]},
    {'name': 'a voice that takes nothing when it is told to be still', 'steps': OPENED + [deaf(), phone({'type': 'oracle_v2.interrupt'})]},
    {'name': 'a voice that is gone when the user is heard, in a call under way',
     'steps': OPENED + [hears(), deaf(), voice('session.started'), hears()]},
    {'name': 'the user calls, and hangs up', 'call': 'user',
     'steps': [start(), ready(), voice('session.started'), took(), speaks(), hears(), fallback(), close()]},
]


def run() -> dict:
    """Runs in a process of its own: what the Python Host's session does."""
    spec = importlib.util.spec_from_file_location('replay_host', REPO / 'tests' / 'replay' / 'host.py')
    host = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(host)
    root = pathlib.Path(tempfile.mkdtemp(prefix='clarp-live-session-vectors-', dir='/var/tmp'))
    (root / 'scenario-config.toml').write_text('')
    host.isolate(root)
    host.loopback_only()
    sys.path.insert(0, str(REPO / 'server'))
    import threading
    import time as real_time

    from lib import (agent_ring_live, agent_ring_live_socket, agent_ring_openings, agent_rings, agent_rings_base, agents, apns,
                     group_calls, oracle_contact, settings_store, ws)
    from lib.oracle_live_wire import LiveWire

    live, socket_ = agent_ring_live, agent_ring_live_socket
    group_calls._host_id = lambda: 'host-1'
    live.ROOT = root / 'agent-live'
    folder = live.ROOT / 'profiles' / 'pump-guard'
    folder.mkdir(parents=True)
    (folder / 'profile.json').write_text(json.dumps(PROFILE), encoding='utf-8')
    (folder / 'PERSONA.md').write_text(PERSONA, encoding='utf-8')
    # What goes to a phone is written down as it is said, and not as the bytes of a socket.
    ws.text_frame = lambda text: text.encode('utf-8')
    socket_.log = lambda *said: None
    socket_.log_exception = lambda *said, **more: None

    state = {'now': 1_800_000_000_000, 'made': 0, 'clock': 5000.0, 'devices': {PHONE: 'full'}, 'key': True}

    class Clock:
        """The time a session counts by: it passes only when a story says so."""

        def __getattr__(self, name):
            return getattr(real_time, name)

        @staticmethod
        def monotonic():
            return state['clock']

    socket_.time = Clock()
    import datetime as datetimes

    class Moment(datetimes.datetime):
        """The moment a call of the user is made at: the stories' own."""

        @classmethod
        def now(cls, tz=None):
            return cls.fromtimestamp(state['now'] / 1000, tz)

    live.datetime = Moment

    class Minted:
        def __init__(self, number):
            self.hex = f'{number:032x}'

    def mint():
        state['made'] += 1
        return Minted(state['made'])

    def secret(count):
        state['made'] += 1
        return f'{state["made"]:0{count * 2}x}'

    class Stream:
        def broadcast(self, told):
            pass

    class Context:
        stream = Stream()
        tts = None

    oracle_contact.roster = lambda: [dict(agent) for agent in AGENTS]
    agents.get_by_agent_id = lambda agent_id: next((dict(agent) for agent in AGENTS if agent['agent_id'] == agent_id), None)
    group_calls.bind(Context())
    agent_rings.bind(Context())
    for module in (agent_rings, agent_rings_base, agent_ring_openings, group_calls):
        module.now_ms = lambda: state['now']
    group_calls.uuid.uuid4 = mint
    agent_rings.secrets.token_hex = secret
    live.provider_problem = lambda: None
    live.device_ok = lambda principal: (not principal.startswith('device_')) or state['devices'].get(principal) == 'full'
    apns.declared_mode_rows = lambda: []
    apns.fcm_configured = lambda cfg: False
    apns.owned_active_tokens = lambda: []
    apns.send_agent_ring_voip = lambda record, tokens: {'enabled': True, 'sent': len(tokens), 'failed': 0}
    apns.send_agent_ring = lambda record, kind, tokens=None, exclude_owners=None: {'enabled': True, 'sent': 1, 'failed': 0}
    group_calls._notify = lambda record, notices: None
    agent_rings._voice = lambda text, agent: f"/audio/{state['now']}__{agent['session']}.mp3"
    agent_rings._schedule_expiry = lambda record: None
    agent_rings._notice = lambda record, text: None

    class Gate(threading.Event):
        """What a session's own clock waits at: it goes on one tick when a story says so."""

        def __init__(self):
            super().__init__()
            self.let = threading.Semaphore(0)
            self.back = threading.Event()

        def wait(self, timeout=None):
            if self.is_set():
                self.back.set()
                return True
            self.back.set()
            self.let.acquire()
            return self.is_set()

        def set(self):
            super().set()
            self.let.release()

    def played(story):
        for prefix in (group_calls.KEY, 'agent_ring.'):
            for key_ in settings_store.keys_with_prefix(prefix):
                settings_store.delete(key_)
        live.reset_for_tests()
        state.update(made=0, clock=5000.0, devices={PHONE: 'full'}, key=True)
        agent_rings.register_voip_token(PHONE, 'a' * 64, supported_conversation_modes=['classic', LIVE])
        if story.get('call') == 'user':
            answered = agent_rings.user_call_live(PHONE, 'a-theo', 'call-1', 'nonce-1')
        else:
            placed = agent_rings.ring(caller_agent_id='a-theo', agent='', reason=WHY, opening_line=LINE,
                                      conversation_mode=LIVE, event_context=copy.deepcopy(EVENT))
            answered = agent_rings.answer(PHONE, placed['ring']['ring_id'], conversation_mode=LIVE, request_id='answer-1',
                                          client_nonce='nonce-1')
        grant = answered['live']
        ring_id = grant['ring_id']
        join = {'type': 'agent_live.join', **{name: grant[name] for name in socket_.JOIN_KEYS}}
        ring = socket_._join(PHONE, join)
        told = {'phone': [], 'voice': [], 'shut': 0}

        class Written:
            def write(self, written):
                told['phone'].append(json.loads(written.decode('utf-8')))

            def flush(self):
                pass

        class Connection:
            def shutdown(self, how):
                told['shut'] += 1

            def settimeout(self, seconds):
                pass

        class Handler:
            wfile = Written()
            connection = Connection()
            ctx = None

        class Voice:
            takes = None

            def send(self, written):
                if self.takes is not None:
                    if self.takes == 0:
                        raise OSError('the voice takes nothing')
                    self.takes -= 1
                told['voice'].append(json.loads(written))

            def close(self):
                told['voice'].append('closed')

        socket_.uuid.uuid4 = mint
        session = socket_.LiveSocket(Handler(), PHONE, ring)
        gate = Gate()
        session.stop = gate
        there = Voice()
        clock = {'thread': None}

        def kept():
            record = next(r for r in agent_rings_base._load() if r['ring_id'] == ring_id)
            names = ('_live_phase', '_live_retired_reason', '_live_call_epoch', '_live_opening_consumed', '_live_audio_forwarded',
                     '_live_close_receipt', '_live_fallback_request_id', 'call_id')
            return {name: record[name] for name in names if name in record}

        def owner(**more):
            record = next(r for r in agent_rings_base._load() if r['ring_id'] == ring_id)
            return {'call_id': record['_live_call_id'], 'owner_nonce': record['_live_owner_nonce'],
                    'attempt_id': record['_live_attempt_id'], **more}

        def from_voice(said):
            """What the voice says, read as the session's own reader of the voice does (`pump`): what cannot be
            taken ends the reading, and the call with it."""
            try:
                session.receive(said)
            except Exception:  # noqa: BLE001 - as that reader has it
                if not session.closed.is_set():
                    if session.admitted:
                        session._end_locally('provider_closed')
                    else:
                        session.fail('live_unavailable', 'provider_lost')

        def from_phone(said):
            """What the phone says, read as `serve` reads it: what cannot be taken fails the call."""
            try:
                session.client(said)
            except Exception:  # noqa: BLE001 - as `serve` has it
                session.fail('live_unavailable', 'host_error')
                if not session.closed.is_set():
                    session._end_locally('transport_lost')

        def act(step):
            do = step['do']
            if do == 'start':
                # What `serve` does between the join and the reading of the phone.
                if step['late'] == 'closed':
                    live.close(PHONE, owner(call_epoch=1, reason='user_hangup', request_id='close-0'))
                with live._SESSIONS_LOCK:
                    live._SESSIONS[session.call_id] = session
                live._remember_closure(session)
                if not session.current():
                    return session.fail('live_call_stale', 'stale')
                if not step['key']:
                    return session.fail('live_unavailable', 'provider_unusable')
                session.connecting = True
                if not step['connects']:
                    session.connect_failed()
                    return session.fail('live_unavailable', 'provider_connect_failed')
                if not session.attach_upstream(there):
                    return None
                wire = LiveWire('api')
                session.downstream({'type': 'agent_live.session', 'protocol': live.PROTOCOL, 'host_id': 'host-1',
                                    'ring_id': session.ring_id, 'agent_id': session.agent_id, **session.ids(),
                                    'context_digest': session.context_digest, 'provider': live.PROVIDER,
                                    'model': wire.model, 'voice': wire.voice})
                clock['thread'] = threading.Thread(target=session.tick, daemon=True)
                gate.back.clear()
                clock['thread'].start()
                gate.back.wait(5)
                session.upstream_send({'type': 'session.start',
                                       'session': wire.session(socket_.instructions(session.snapshot), webrtc=False)})
                return None
            if do == 'phone':
                return from_phone(copy.deepcopy(step['said']))
            if do == 'ready':
                said = {'type': 'agent_live.client_ready', **session.ids(), 'audio': step['audio'], 'capture': step['capture']}
                said.update(step['other'])
                step['said'] = said
                return from_phone(dict(said))
            if do == 'voice':
                return from_voice(copy.deepcopy(step['said']))
            if do == 'took':
                said = {'type': 'session.instructions.appended'}
                if step['how'] == 'its':
                    said['client_event_id'] = session.opening_event_id
                elif step['how'] != 'none':
                    said['client_event_id'] = step['how']
                step['said'] = said
                return from_voice(dict(said))
            if do == 'wait':
                state['clock'] += step['seconds']
                return None
            if do == 'ticks':
                for _ in range(step['count']):
                    thread = clock['thread']
                    if thread is None or not thread.is_alive() or gate.is_set():
                        state['clock'] += socket_.TICK_S
                        continue
                    state['clock'] += socket_.TICK_S
                    gate.back.clear()
                    gate.let.release()
                    # The session is back at its clock, or its clock has stopped for good.
                    while not gate.back.wait(0.05):
                        if not thread.is_alive():
                            break
                return None
            if do == 'gone':
                if session.closed.is_set():
                    return None
                if step['who'] == 'phone':
                    return session._end_locally('transport_lost')
                # What the session does when the voice's socket has ended (`pump`).
                if session.admitted:
                    return session._end_locally('provider_closed')
                return session.fail('live_unavailable', 'provider_lost')
            if do == 'device':
                state['devices'][PHONE] = step['state']
                return None
            if do == 'deaf':
                there.takes = step['after']
                return None
            if do == 'retired':
                with agent_rings._lock:
                    records = agent_rings._load()
                    record = next(r for r in records if r['ring_id'] == ring_id)
                    live.retire(record, step['reason'])
                    agent_rings._save(records)
                return None
            if do == 'close':
                epoch = kept()['_live_call_epoch']
                return live.close(PHONE, owner(call_epoch=epoch, reason=step['reason'], request_id='close-1'))
            if do == 'fallback':
                epoch = kept()['_live_call_epoch']
                answer = live.fallback(PHONE, owner(expected_epoch=epoch, audio_stopped=True, request_id='fallback-1'))
                return {'conversation_mode': answer['conversation_mode'], 'call': bool(answer.get('call'))}
            raise ValueError(do)

        rows = []
        for step in story['steps']:
            step = copy.deepcopy(step)
            del told['phone'][:], told['voice'][:]
            told['shut'] = 0
            try:
                result, refused = act(step), None
            except agent_rings.CallError as error:
                result, refused = None, {'status': error.status, 'error': error.error}
            if session.closed.is_set():
                # Its voice is told that the call is over, and closed, beside the session itself.
                session.upstream_close_done.wait(5)
                thread = clock['thread']
                if thread is not None:
                    thread.join(3)
            rows.append(json.loads(json.dumps({
                'step': step, 'result': result if isinstance(result, dict) else None, 'refused': refused,
                'to_phone': list(told['phone']), 'to_voice': list(told['voice']), 'shut': told['shut'], 'kept': kept(),
                'over': session.closed.is_set(), 'reason': session.reason, 'settled': session.settled()})))
        gate.set()
        if clock['thread'] is not None:
            clock['thread'].join(3)
        return {'name': story['name'], 'call': story.get('call', 'ring'), 'ids': session.ids(),
                'opening_event_id': session.opening_event_id, 'context_digest': session.context_digest, 'ring_id': ring_id,
                'snapshot': session.snapshot, 'steps': rows}

    answered = {
        'what': 'The session of a ring held as a live voice call, by server/lib/agent_ring_live_socket.py (LiveSocket), on a ring '
                'placed, answered and joined through agent_rings: what the phone and the voice are told after each step. A long '
                'run of one sign is written {"~run": [before, sign, count, after]}.',
        'limits': {'ready_s': socket_.READY_S, 'opening_ack_s': socket_.OPENING_ACK_S, 'tick_s': socket_.TICK_S,
                   'held_bytes': socket_.OPENING_HOLD_BYTES, 'held_frame_signs': socket_.OPENING_HOLD_FRAME_B64,
                   'sound_signs': socket_.MAX_AUDIO_B64, 'device_check_ticks': socket_.DEVICE_CHECK_TICKS,
                   'client_idle_s': socket_.CLIENT_IDLE_S, 'join_wait_s': socket_.JOIN_WAIT_S},
        'silence': socket_.SILENCE, 'phone': PHONE, 'host_id': 'host-1',
        'stories': [played(story) for story in STORIES],
    }
    shutil.rmtree(root, ignore_errors=True)
    return answered


def shrunk(value):
    """A value with every long run of one sign written as what stands before it, the sign, how often, and what
    follows: `{"~run": [before, sign, count, after]}`."""
    if isinstance(value, dict):
        return {key: shrunk(item) for key, item in value.items()}
    if isinstance(value, list):
        return [shrunk(item) for item in value]
    if not isinstance(value, str) or len(value) < 64:
        return value
    start_ = 0
    while start_ < len(value):
        end = start_
        while end < len(value) and value[end] == value[start_]:
            end += 1
        if end - start_ >= 64:
            return {'~run': [value[:start_], value[start_], end - start_, shrunk(value[end:])]}
        start_ = end
    return value


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == '--run':
        print(json.dumps(run()))
        return 0
    ran = subprocess.run([sys.executable, '-I', __file__, '--run'], capture_output=True, text=True,
                         env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}, check=False)
    if ran.returncode != 0:
        print(ran.stderr[-4000:], file=sys.stderr)
        return 1
    said = json.loads(ran.stdout.splitlines()[-1])
    out = ['{']
    for name in ('what', 'limits', 'silence', 'phone', 'host_id'):
        out.append(f' {json.dumps(name)}: {json.dumps(shrunk(said[name]), ensure_ascii=True)},')
    out.append(' "stories": [')
    stories = said['stories']
    for at, story in enumerate(stories):
        head = {name: shrunk(story[name]) for name in story if name != 'steps'}
        out.append('  {"head": ' + json.dumps(head, ensure_ascii=True) + ', "steps": [')
        rows = story['steps']
        out.extend('   ' + json.dumps(shrunk(row), ensure_ascii=True) + (',' if index < len(rows) - 1 else '')
                   for index, row in enumerate(rows))
        out.append('  ]}' + (',' if at < len(stories) - 1 else ''))
    out.append(' ]')
    out.append('}')
    OUT.write_text('\n'.join(out) + '\n')
    steps = [row for story in stories for row in story['steps']]
    ended = {}
    for story in stories:
        reason = story['steps'][-1]['reason'] or 'not over'
        ended[reason] = ended.get(reason, 0) + 1
    print(f'{OUT.relative_to(REPO)}: {len(stories)} stories of {len(steps)} steps; {sum(len(row["to_phone"]) for row in steps)} '
          f'things told to the phone, {sum(len(row["to_voice"]) for row in steps)} to the voice; ended: '
          + ', '.join(f'{reason} {count}' for reason, count in sorted(ended.items())))
    return 0


if __name__ == '__main__':
    sys.exit(main())
