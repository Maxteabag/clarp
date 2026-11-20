#!/usr/bin/env python3
"""Wrong rules, planted one at a time (see `runner.py`, beside this list).

What a live voice says that this Host does not read (a session's standby, `session.paused` and `session.resumed`, or
a kind to come): passed over by a ring's call and by the Oracle's, ending neither, and said once a call in the journal
by its type alone (`clarp_kernel::live_call::unread_voice_event`).
"""
import runner

RUNS = {
    'kernel': ['test', '-p', 'clarp-kernel', '--lib', 'live_call::unread_voice_events'],
    'oracle': ['test', '--test', 'host', '--', 'voice_events::'],
    'ring': ['test', '--test', 'host', '--', 'a_ring_is_held_as_a_live_voice_call_between_a_phone_and_a_voice'],
}
K = 'clarp-kernel/src/live_call.rs'
L = 'clarp-host/src/calls/live.rs'
O = 'clarp-host/src/oracle/call.rs'
PLANTS = [
    (K, 'ring', 'a kind the Host reads is called unread', '    "session.started",\n', '    "session.begun",\n'),
    (K, 'kernel', 'what the kind carries is said with it', '    let shown: String = kind\n        .chars()\n        .take(64)',
     '    let shown: String = kind\n        .chars()\n        .take(640)'),
    (K, 'kernel', 'a kind is said in signs a journal line cannot hold', '            if sign.is_ascii_alphanumeric() || "._-".contains(sign) {',
     '            if true {'),
    (L, 'ring', "a ring's call says nothing of what it does not read",
     '                        eprintln!("agentLiveEventUnread type={kind}");', '                        let _ = kind;'),
    (L, 'ring', "a ring's call says an unread kind each time", '                        && unread.insert(kind.clone())\n',
     '                        && (unread.insert(kind.clone()) || true)\n'),
    (O, 'oracle', "the Oracle's call says nothing of what it does not read",
     '                eprintln!("oracleVoiceEventUnread type={kind}");', '                let _ = kind;'),
    (O, 'oracle', "the Oracle's call says an unread kind each time", '                && unread.insert(kind.clone())\n',
     '                && (unread.insert(kind.clone()) || true)\n'),
]
# NOT PLANTED: the Oracle's call leaving the Host's own `oracle_v2.*` events out of what it says: only a second voice
#   (Gemini) that reconnects puts one there, which no test can reach without a second voice's stand-in reconnecting.


if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
