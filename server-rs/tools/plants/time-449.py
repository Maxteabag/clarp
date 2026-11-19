#!/usr/bin/env python3
"""Wrong rules, planted one at a time (see `runner.py`, beside this list).

The port of the Python Host's PR 449 (R90: the Host answers alike on every interpreter): an event's time read by one
stated grammar, then the calendar day and the zone (agent_ring_live._EVENT_TIME and validate_context); the model's
answer that holds no JSON told in the Host's own sentence (orchestrator_model.ModelAnswerNotJson); a media name ending
in a dot has no extension (media_store._extension).
"""
import runner

RUNS = {
    'live': ['test', '-p', 'clarp-kernel', '--test', 'live_call_vectors'],
    'router': ['test', '-p', 'clarp-kernel', '--test', 'router_vectors'],
    'flow': ['test', '-p', 'clarp-kernel', '--test', 'router_flow_vectors'],
    'media': ['test', '-p', 'clarp-kernel', '--test', 'media_vectors'],
}
L = 'clarp-kernel/src/live_call.rs'
R = 'clarp-kernel/src/router.rs'
M = 'clarp-kernel/src/media.rs'
PLANTS = [
    # ---- an event's time
    (L, 'live', 'a space is taken for the T', "at(10, b'T')", "(at(10, b'T') || at(10, b' '))"),
    (L, 'live', 'the year nought is taken', '    let date = year >= 1 &&', '    let date = year >= 0 &&'),
    (L, 'live', 'every month has thirty-one days', '(1..=days_in(year, month)).contains(&day)', '(1..=31).contains(&day)'),
    (L, 'live', 'the hour 24 is taken', 'hour > 23 || minute > 59', 'hour > 24 || minute > 59'),
    (L, 'live', 'a fraction has six digits at most', '        if !(1..=9).contains(&digits) {', '        if !(1..=6).contains(&digits) {'),
    (L, 'live', 'a small z names the zone', "        [b'Z'] => true,", "        [b'Z' | b'z'] => true,"),
    (L, 'live', "the zone's hour 24 is taken", 'if h <= 23 && m <= 59)', 'if h <= 24 && m <= 59)'),
    # ---- a model's answer that holds no JSON
    (R, 'router', "the model's answer is told in other words", '''pub const NOT_JSON: &str = "the model's answer was not JSON (JSONDecodeError)";''',
     '''pub const NOT_JSON: &str = "the model's answer is no JSON";'''),
    # ---- a media name ending in a dot
    (M, 'media', "a name ending in a dot has the extension '.'", '        Some(dot) if dot > 0 && dot + 1 < name.len() => &name[dot..],',
     '        Some(dot) if dot > 0 => &name[dot..],'),
]


if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
