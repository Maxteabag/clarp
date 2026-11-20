#!/usr/bin/env python3
"""Wrong rules, planted one at a time (see `runner.py`, beside this list).

A send whose tool could not be started (R121, the release owner's PR 466): what one refusal is, the waits and the count
and the notice's words (the kernel, held to the Python Host's own functions), and how the agent's actor keeps them: a
send's own try and a second, the counted tries, the eighth of one refusal ending the turn with its sender told, by the
launcher's path and the shim's; a user's send asking for the agent again; a Host that starts again in the middle; and
what the apps are told of the waits.
"""
import runner

RUNS = {
    'vectors': ['test', '-p', 'clarp-kernel', '--test', 'launch_refused_vectors'],
    'unit': ['test', '-p', 'clarp-kernel', '--lib', 'launch_refused::'],
    'actor': ['test', '-p', 'clarp-actor', '--test', 'actor', '--', 'r121_'],
    'host': ['test', '--test', 'host', '--', 'launch_refused::'],
}
K = 'clarp-kernel/src/launch_refused.rs'
A = 'clarp-actor/src/lib.rs'
W = 'clarp-host/src/wire.rs'
# NOT PLANTED, each with its reason:
#   launch_refused.rs, the bound of the doubling (`.min(16)`): the wait reaches its own bound of a minute at the
#     seventh refusal, long before the doubling would reach sixteen.
#   lib.rs, a launch that went through ends the count (`Spawned`): only a turn that started, then failed in a way that
#     passes, and whose next launch was refused could show it; the Python Host's rule is its `clear` on a launch.
PLANTS = [
    # ---- what one refusal is, and the notice (the kernel)
    (K, 'vectors', 'an id is not made the same', '    let said = UUID.replace_all(&said, "<id>");',
     '    let said = UUID.replace_all(&said, "<uuid>");'),
    (K, 'vectors', 'an id in capitals is not an id', 'Regex::new(r"(?i)[0-9a-f]{8}-', 'Regex::new(r"[0-9a-f]{8}-'),
    (K, 'vectors', 'six signs of hex are no number', r'Regex::new(r"[0-9a-f]{6,}|\d+")', r'Regex::new(r"[0-9a-f]{7,}|\d+")'),
    (K, 'vectors', 'the digits of other scripts are not counts', r'Regex::new(r"[0-9a-f]{6,}|\d+")',
     r'Regex::new(r"[0-9a-f]{6,}|[0-9]+")'),
    (K, 'vectors', 'the kind of a refusal is not in what it is', '    let said = format!("{kind}: {text}");',
     '    let said = text.to_string();'),
    (K, 'vectors', 'a refusal keeps a sign less', 'const SIGNED_MOST: usize = 300;', 'const SIGNED_MOST: usize = 299;'),
    (K, 'vectors', 'the notice keeps a sign less of a refusal', 'const BRIEF_MOST: usize = 240;',
     'const BRIEF_MOST: usize = 239;'),
    (K, 'vectors', 'the notice keeps half a refusal from its start', '    let head = BRIEF_MOST / 3;',
     '    let head = BRIEF_MOST / 2;'),
    (K, 'vectors', 'the notice says a refusal whole', '        self.error = brief(text);',
     '        self.error = text.to_string();'),
    (K, 'vectors', 'the waits begin at two seconds', '    let doubled = 1_i64 << failures.saturating_sub(1).min(16);',
     '    let doubled = 1_i64 << failures.min(16);'),
    (K, 'vectors', 'the longest wait is shorter', 'pub const MAX_MS: i64 = 60_000;', 'pub const MAX_MS: i64 = 59_000;'),
    (K, 'vectors', 'a ninth refusal ends the turn', 'pub const PARK_AFTER: u32 = 8;', 'pub const PARK_AFTER: u32 = 9;'),
    (K, 'unit', 'refusals that alternate never end the turn', '        if self.identical >= PARK_AFTER || self.failures >= REFUSED_IN_ALL {',
     '        if self.identical >= PARK_AFTER {'),
    (K, 'unit', 'a twenty-first refusal ends the turn', 'pub const REFUSED_IN_ALL: u32 = 20;', 'pub const REFUSED_IN_ALL: u32 = 21;'),
    (K, 'vectors', 'another refusal does not begin the count of one again',
     '        self.identical = if signed == self.signature {', '        self.identical = if true {'),
    (K, 'vectors', "the notice keeps a refusal's last dot", "    let error = error.trim_end_matches('.');",
     '    let error = error;'),
    (K, 'vectors', "a send's own try is followed by a longer wait", 'pub const FIRST_WAIT_MS: i64 = 1_000;',
     'pub const FIRST_WAIT_MS: i64 = 2_000;'),
    # ---- the actor
    (A, 'actor', "a send's own try is counted",
     '        if start.id == turn && start.attempt == 1 && start.started_at_ms == start.accepted_at_ms);',
     '        if start.id == turn && false);'),
    (A, 'actor', "the first try of a send that waited is its own try",
     '        if start.id == turn && start.attempt == 1 && start.started_at_ms == start.accepted_at_ms);',
     '        if start.id == turn && start.attempt == 1);'),
    (A, 'actor', 'a launcher that cannot start the shim ends the turn at once', '        if refusable {',
     '        if false {'),
    (A, 'actor', "a launcher's last refusal tells the sender nothing",
     '                AfterRefusal::End { notice } => self.never_run = Some(notice),',
     '                AfterRefusal::End { .. } => {}'),
    (A, 'actor', "a shim's last refusal says the failure as any other",
     '                                AfterRefusal::End { notice } => said = Some(notice),',
     '                                AfterRefusal::End { .. } => {}'),
    (A, 'actor', 'every refusal of the shim waits a second, as an opening does', '                            let after = if live.opening {',
     '                            let after = if true {'),
    (A, 'actor', "a user's send does not ask for the agent again",
     '        let asks_again = matches!(message.origin.name(), "user" | "agent" | "oracle");',
     '        let asks_again = false;'),
    (A, 'actor', "a user's send asks for the agent again on the count it had",
     '        if asks_again && waits_on_refusal {\n            self.refused = None;\n',
     '        if asks_again && waits_on_refusal {\n'),
    (A, 'actor', 'a Host that starts again waits out the wait it had',
     '            Some(Turn::Parked(parked)) if self.refusal_uncounted(parked) => Some(parked.since_ms),\n', ''),
    # ---- what the apps are told
    (W, 'host', 'a refused launch is told of the tries of a failure that passes',
     '        launch_refused::PARK_AFTER + 1\n', '        recovery::MAX_ATTEMPTS\n'),
]


if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
