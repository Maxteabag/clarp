#!/usr/bin/env python3
"""Wrong rules, planted one at a time (see `runner.py`, beside this list).

A Host that starts again and the live calls it finds: which of them are over, under which word, and that such a
call is not switched to an ordinary one (the Python Host's `agent_rings.recover_joined_live_calls` and the first
check of its `agent_ring_live.fallback`, since its PR 420).
"""
import runner

RUNS = {
    'vectors': ['test', '--test', 'live_ring_vectors'],
}
K = 'clarp-kernel/src/rings/live.rs'
# NOT PLANTED, each with its reason:
#   that the Host looks for such calls when it starts and keeps what it found (`rings.orphaned` in the Host's
#     `calls.rs`): held by the Host's test of a live call, in which a phone whose call was under way calls anew
#     after the Host was started again; that test is run with the whole of the Host's tests, not by this list.
ENDED = '            if live.holds() && live.phase != WAITING && live.retire(HOST_RESTART) {'
SWITCH = ('        // A call that a Host\'s start ended is not switched: its phone calls anew.\n'
          '        if live.retired_reason == HOST_RESTART {\n'
          '            return Err(not_allowed());\n'
          '        }\n')
REST = ('        // Claimed before, by a switch under way or one that failed; or the owner hung up.\n'
        '        if !live.fallback_request_id.is_empty() {\n'
        '            return Err(not_allowed());\n'
        '        }\n'
        '        let hung_up = live.close_receipt.as_ref();\n'
        '        if hung_up.is_some_and(|receipt| receipt["reason"] == "user_hangup") {\n'
        '            return Err(not_allowed());\n'
        '        }\n'
        '        if epoch != live.call_epoch {\n'
        '            return Err(stale());\n'
        '        }\n')
PLANTS = [
    (K, 'vectors', 'a call that was granted and not joined is over when a Host starts', ENDED, '            if live.holds() && live.retire(HOST_RESTART) {'),
    (K, 'vectors', 'only a call that was granted and not joined is over when a Host starts', ENDED, '            if live.holds() && live.phase == WAITING && live.retire(HOST_RESTART) {'),
    (K, 'vectors', 'no call is over when a Host starts', ENDED, '            if live.holds() && live.phase != WAITING && false {'),
    (K, 'vectors', 'a call a start ended is counted though it was over before', ENDED, '            if live.phase != WAITING && (live.retire(HOST_RESTART) || live.phase == RETIRED) {'),
    (K, 'vectors', 'a call a start ended is over under another word', 'const HOST_RESTART: &str = "host_restart";', 'const HOST_RESTART: &str = "host_restarted";'),
    (K, 'vectors', 'a call a start ended may be switched to an ordinary call', SWITCH, ''),
    (K, 'vectors', 'the switch of a call a start ended is refused as one of another epoch', SWITCH, SWITCH.replace('return Err(not_allowed());', 'return Err(stale());')),
    (K, 'vectors', 'a call a start ended is looked at after the epoch its switch names', SWITCH + REST, REST + SWITCH),
    (K, 'vectors', 'a call that ended otherwise is not switched either', SWITCH, SWITCH.replace('live.retired_reason == HOST_RESTART', '!live.retired_reason.is_empty()')),
]


if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
