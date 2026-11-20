#!/usr/bin/env python3
"""Wrong rules, planted one at a time (see `runner.py`, beside this list).

The shim holds its tool: a hold stops every process of the tool's group, waits until each of them stands still,
takes all they printed into the spool and says how far the spool goes and whether a line was begun. It lets the
tool go on a release or at its bound, kills it where it stands when it is ended while held, and refuses a hold it
cannot make in words, signalling nothing.
"""
import runner

SHIM = ['test', '--test', 'shim', '--']
UNIT = ['test', '-p', 'clarp-shim', '--bin', 'clarp-shim', '--']
RUNS = {
    'count': SHIM + ['a_hold_counts_everything_printed_before_the_tool_stood_still'],
    'half': SHIM + ['a_half_line_at_the_hold_is_said_and_whole_lines_are_counted'],
    'child': SHIM + ['a_child_of_the_tool_is_held_and_let_go_with_it'],
    'bound': SHIM + ['the_bound_lets_the_tool_go_when_the_host_is_gone_and_the_turn_completes_whole'],
    'ended': SHIM + ['a_tool_ended_while_it_is_held_is_killed_without_running_again'],
    'stdin': SHIM + ['what_is_written_to_a_held_tool_is_read_when_it_is_let_go'],
    'once': SHIM + ['a_hold_once_held_is_refused_and_a_release_does_no_harm'],
    'zero': SHIM + ['a_hold_whose_bound_is_spent_lets_go_and_counts_as_the_one'],
    'gone': SHIM + ['a_tool_that_is_gone_is_not_held'],
    'outside': SHIM + ['a_tool_killed_from_outside_while_it_is_held_leaves_nothing_standing'],
    'hook': SHIM + ['a_hooks_report_under_way_at_the_hold_is_counted'],
    'served': SHIM + ['a_served_tool_is_not_held'],
    'shim': SHIM + ['a_request_ends_with_its_line_or_with_the_connection'],
    'unit-late': UNIT + ['a_hold_that_cannot_count_within_its_bound'],
    'unit-count': UNIT + ['a_count_'],
    'unit-still': UNIT + ['a_group_that_does_not_stand_still_is_let_go_at_the_bound'],
    'unit-ended': UNIT + ['a_held_tool_is_ended_at_once_and_a_hold_under_way_says_so'],
    'unit-stat': UNIT + ['a_stat_is_read_after_the_last_bracket_of_the_name', 'a_group_with_one_process_awake'],
    'words': ['test', '-p', 'clarp-shim', '--lib', '--', 'a_reply_is_written_as_before'],
}
LIB = 'clarp-shim/src/lib.rs'
MAIN = 'clarp-shim/src/main.rs'
# NOT PLANTED, each with its reason:
#   that every thread of a process is looked at, not only its first (`/proc/{pid}/task`): the threads of a stopped
#     process stop one after another within microseconds, and no test can hold one of them awake while the first
#     already stands still.
#   that an answer counted at the very end of the bound is refused (`if !left.is_zero()`): the count and the end of
#     the bound would have to meet within a microsecond. The arm that follows is the same refusal as a count that
#     ran out of time, which `zero` and `unit-count` hold.
#   that the half line a pump records when its pipe closes is recorded in the pump's turn: a hold that counts in
#     the moment between the pipe's end and that record is a race of microseconds; `half` holds the record itself.
#   that the kill of a held tool is not preceded by a SIGCONT: a SIGCONT followed at once by the SIGKILL kills
#     the group before its own code runs again, so no test can tell the two apart. The rule is kept because the
#     two are only alike while nothing comes between them.
#   that a group that `/proc` cannot show does not stand still (`return false` after `read_dir("/proc")`): `/proc`
#     is there wherever the shim and its tests run.
#   that a connection is taken from the listener under the list of connections (`let mut connections = lock(...)`
#     in `accept`): what it guards is a connection taken and not yet in the list, a window of one system call.
#     That a connection waiting in the listener is in a count's way is planted below.
PLANTS = [
    # The words a client is told, and the bound.
    (LIB, 'bound', 'the bound is three seconds', 'pub const HOLD_MOST_MS: u64 = 2_000;', 'pub const HOLD_MOST_MS: u64 = 3_000;'),
    (LIB, 'bound', 'the bound is one second', 'pub const HOLD_MOST_MS: u64 = 2_000;', 'pub const HOLD_MOST_MS: u64 = 1_000;'),
    (LIB, 'words', 'the hold asks under another name', '    Hold {\n        ms: u64,\n    },', '    Hold {\n        #[serde(rename = "millis")]\n        ms: u64,\n    },'),
    (LIB, 'words', 'a reply without a hold says it has none', '    #[serde(default, skip_serializing_if = "Option::is_none")]\n    pub held: Option<Held>,', '    #[serde(default)]\n    pub held: Option<Held>,'),
    (LIB, 'words', 'a reply that is no release says it let nothing go', '    #[serde(default, skip_serializing_if = "Option::is_none")]\n    pub let_go: Option<bool>,', '    #[serde(default)]\n    pub let_go: Option<bool>,'),
    (LIB, 'words', 'the count is told under another name', '    pub spool: u64,', '    #[serde(rename = "end")]\n    pub spool: u64,'),
    (LIB, 'words', 'a half line is told under another name', '    pub partial_out: u64,', '    #[serde(rename = "partial")]\n    pub partial_out: u64,'),

    # Who is held at all.
    (MAIN, 'served', 'the driver is known by another word', 'const SERVED: &str = "opencode-turn";', 'const SERVED: &str = "opencode";'),
    (MAIN, 'served', 'the driver is known by its last word', 'args.program_args.first().is_some_and(|word| word == SERVED)', 'args.program_args.last().is_some_and(|word| word == SERVED)'),
    (MAIN, 'served', 'a served tool is held', '        if self.served {\n', '        if !self.served && self.pid == 0 {\n'),
    (MAIN, 'served', 'a served tool is refused in other words', 'Reply::failed("a served tool cannot be held")', 'Reply::failed("served")'),
    (MAIN, 'gone', 'a tool that is gone is held', '            if self.leader_exited.load(Ordering::SeqCst) {\n                return Reply::failed("the tool is gone");', '            if self.pid == 0 {\n                return Reply::failed("the tool is gone");'),
    (MAIN, 'gone', 'a tool that is gone is refused in other words', 'Reply::failed("the tool is gone")', 'Reply::failed("gone")'),
    (MAIN, 'gone', 'a tool being stopped is held', '            if !matches!(*stage, StopStage::Ready) {\n                return Reply::failed("the tool is being stopped");', '            if matches!(*stage, StopStage::Finished) {\n                return Reply::failed("the tool is being stopped");'),
    (MAIN, 'gone', 'a tool being stopped is refused in other words', 'Reply::failed("the tool is being stopped")', 'Reply::failed("stopping")'),
    (MAIN, 'once', 'a turn released is held again', '            if !matches!(*hold, Hold::Free) {', '            if matches!(*hold, Hold::Held { .. }) {'),
    (MAIN, 'once', 'a second hold is refused in other words', 'Reply::failed("the turn has been held once already")', 'Reply::failed("held")'),
    (MAIN, 'zero', 'a hold that ran out of time does not count as the one', '            if !matches!(*hold, Hold::Free) {', '            if !matches!(*hold, Hold::Free | Hold::Released) {'),

    # The bound.
    (MAIN, 'bound', 'the bound is what was asked', 'Duration::from_millis(ms.min(HOLD_MOST_MS))', 'Duration::from_millis(ms)'),
    (MAIN, 'count', 'the bound is always the most', 'Duration::from_millis(ms.min(HOLD_MOST_MS))', 'Duration::from_millis(HOLD_MOST_MS)'),
    (MAIN, 'bound', 'nothing lets the tool go at the bound', '        std::thread::spawn(move || control.let_go_at_the_bound());', '        drop(control);'),
    (MAIN, 'bound', 'the bound is waited for three times over', '                .wait_timeout(hold, left)', '                .wait_timeout(hold, left * 3)'),
    (MAIN, 'bound', 'the bound lets nothing go', '            if left.is_zero() {\n                drop(hold);\n                self.let_go();\n                return;', '            if left.is_zero() {\n                drop(hold);\n                return;'),
    (MAIN, 'count', 'the time left is not told', '                held.left_ms = u64::try_from(left.as_millis()).unwrap_or(u64::MAX);', '                held.left_ms = 0;'),
    (MAIN, 'count', 'the time left is told as the whole bound', 'held.left_ms = u64::try_from(left.as_millis()).unwrap_or(u64::MAX);', 'held.left_ms = HOLD_MOST_MS;'),

    # Standing still.
    (MAIN, 'count', 'the group is never told to stop', '                    let _ = killpg(self.group(), Signal::SIGSTOP);', '                    let _ = killpg(self.group(), Signal::SIGCONT);'),
    (MAIN, 'child', 'only the tool is told to stop, not its children', '                    let _ = killpg(self.group(), Signal::SIGSTOP);', '                    let _ = kill(self.group(), Signal::SIGSTOP);'),
    (MAIN, 'unit-still', 'the group is told to stop after the bound', '                Hold::Held { .. } if Instant::now() < until => {', '                Hold::Held { .. } => {'),
    (MAIN, 'unit-still', 'a group that does not stand still is left stopped', '                _ => {\n                    drop(hold);\n                    self.let_go();\n                    return Err("the tool did not stand still within the bound");', '                _ => {\n                    drop(hold);\n                    return Err("the tool did not stand still within the bound");'),
    (MAIN, 'unit-still', 'a hold ended while it is looked at goes on', '                Hold::Ended => return Err(ENDED_WHILE_HELD),\n                Hold::Held', '                Hold::Ended => {}\n                Hold::Held'),
    (MAIN, 'unit-still', 'the group is looked at once only', '            if still() {\n                return Ok(());\n            }', '            let _ = still();\n            return Ok(());'),
    (MAIN, 'zero', 'a spent bound is refused in other words', 'return Err("the tool did not stand still within the bound");', 'return Err("not still");'),
    (MAIN, 'unit-stat', 'a running thread stands still', "                && !matches!(state, 'T' | 'Z')", "                && !matches!(state, 'T' | 'Z' | 'R' | 'S')"),
    (MAIN, 'half', 'a zombie of the group does not stand still', "                && !matches!(state, 'T' | 'Z')", "                && !matches!(state, 'T')"),
    (MAIN, 'count', 'a stopped process does not stand still', "                && !matches!(state, 'T' | 'Z')", "                && !matches!(state, 'Z')"),
    (MAIN, 'unit-stat', 'every process is of the group', '            Some((_, member)) if member == group => {}', '            Some(_) => {}'),
    (MAIN, 'unit-stat', 'the state is read after the first bracket', "    let mut after = stat[stat.rfind(')')? + 1..].split_whitespace();", "    let mut after = stat[stat.find(')')? + 1..].split_whitespace();"),
    (MAIN, 'unit-stat', 'the group is read where the parent is', '    let group = after.nth(1)?.parse().ok()?;', '    let group = after.next()?.parse().ok()?;'),

    # The count.
    (MAIN, 'unit-count', 'a count goes on past its bound', '            if Instant::now() >= until {\n                return None;\n            }\n            let connections', '            if until.elapsed() > Duration::from_secs(60) {\n                return None;\n            }\n            let connections'),
    (MAIN, 'unit-count', 'a connection in the listener is not waited for', '                .is_none_or(|listener| waits(listener.as_fd(), false));', '                .is_some_and(|_| false);'),
    (MAIN, 'unit-count', 'no connection is waited for', '                idle = connection.idle();', '                idle = true;'),
    (MAIN, 'unit-count', 'the count waits for its own connection', '            for connection in connections.iter().filter(|c| !Arc::ptr_eq(c, own)) {', '            for connection in connections.iter() {'),
    (MAIN, 'unit-count', 'a connection is not waited for while it carries out a request', '                turns.push(lock(&connection.turn));', '                drop(connection.turn.try_lock());'),
    (MAIN, 'unit-count', 'a request read and not carried out is no request', '        !self.pending.load(Ordering::SeqCst) && !waits(self.socket.as_fd(), false)', '        !waits(self.socket.as_fd(), false)'),
    (MAIN, 'unit-count', 'what waits in a connection is no request', '        !self.pending.load(Ordering::SeqCst) && !waits(self.socket.as_fd(), false)', '        !self.pending.load(Ordering::SeqCst)'),
    (MAIN, 'unit-count', 'what waits in standard output is not waited for', '                if !out.waits(false) && !err.waits(false) {', '                if !err.waits(false) {'),
    (MAIN, 'unit-count', 'what waits in standard error is not waited for', '                if !out.waits(false) && !err.waits(false) {', '                if !out.waits(false) {'),
    (MAIN, 'count', 'the pumps are not waited for', '                let _out = lock(&out.turn);\n                let _err = lock(&err.turn);', '                let _out = ();\n                let _err = ();'),
    (MAIN, 'unit-count', 'the spool is measured before what was sent to it is written', '                let _ = answer.send(writer.end());', '                let _ = answer.send(Ok(0));'),
    (MAIN, 'unit-count', 'the half line of standard output is not told', '                        partial_out: out.partial.load(Ordering::SeqCst) as u64,', '                        partial_out: 0,'),
    (MAIN, 'unit-count', 'the half line of standard error is told as the output', '                        partial_err: err.partial.load(Ordering::SeqCst) as u64,', '                        partial_err: out.partial.load(Ordering::SeqCst) as u64,'),
    (MAIN, 'half', 'a pump does not say what it holds of a line', '            printed.partial.store(held.len(), Ordering::SeqCst);', '            printed.partial.store(0, Ordering::SeqCst);'),
    (MAIN, 'half', 'a pump whose pipe closed still says it holds a line', '                printed.partial.store(0, Ordering::SeqCst);', '                printed.partial.store(held.len(), Ordering::SeqCst);'),
    (MAIN, 'half', 'standard error is not taken a turn at a time', '            pump(stderr, &to_spool, Entry::Err, errors.as_deref());', '            pump(stderr, &to_spool, Entry::Err, None);'),
    (MAIN, 'half', 'standard output is not taken a turn at a time', '            pump(stdout, &to_spool, Entry::Out, printed.as_deref());', '            pump(stdout, &to_spool, Entry::Out, None);'),

    # The answer.
    (MAIN, 'unit-ended', 'a hold ended while it counted answers with its count', '            (_, Hold::Ended) => Reply::failed(ENDED_WHILE_HELD),\n', ''),
    (MAIN, 'unit-ended', 'a hold ended while it counted is refused in other words', 'const ENDED_WHILE_HELD: &str = "the tool was ended while it was held";', 'const ENDED_WHILE_HELD: &str = "ended";'),
    (MAIN, 'unit-late', 'a count that ran out of time is refused in other words', 'Reply::failed("what the tool printed could not be counted within the bound")', 'Reply::failed("not counted")'),
    (MAIN, 'unit-late', 'a count that ran out of time leaves the tool stopped', '            _ => {\n                drop(hold);\n                self.let_go();\n                Reply::failed(', '            _ => {\n                drop(hold);\n                Reply::failed('),

    # Letting go.
    (MAIN, 'once', 'a tool that is not held is let go', '        if !matches!(*hold, Hold::Held { .. }) {\n            return false;\n        }\n        *hold = Hold::Released;', '        if matches!(*hold, Hold::Ended) {\n            return false;\n        }\n        *hold = Hold::Released;'),
    (MAIN, 'count', 'a release does not let the tool go', '        let _ = killpg(self.group(), Signal::SIGCONT);', '        let _ = killpg(self.group(), Signal::SIGWINCH);'),
    (MAIN, 'child', 'a release lets only the tool go, not its children', '        let _ = killpg(self.group(), Signal::SIGCONT);', '        let _ = kill(self.group(), Signal::SIGCONT);'),
    (MAIN, 'unit-still', 'a tool let go is still held', '        *hold = Hold::Released;\n', ''),
    (MAIN, 'count', 'a release says it let nothing go', '                let_go: Some(self.let_go()),', '                let_go: Some(!self.let_go()),'),
    (MAIN, 'outside', 'a tool killed from outside leaves its group stopped', '    control.let_go();\n    child', '    child'),

    # Ended while held.
    (MAIN, 'ended', 'a held tool is stopped politely', '            if matches!(*hold, Hold::Held { .. }) {\n                *hold = Hold::Ended;', '            if matches!(*hold, Hold::Ended) {\n                *hold = Hold::Ended;'),
    (MAIN, 'unit-ended', 'a held tool ended is not marked so', '                *hold = Hold::Ended;\n                self.hold_changed.notify_all();', '                self.hold_changed.notify_all();'),
    (MAIN, 'ended', 'a held tool is ended with the polite signal', '                drop(hold);\n                Self::signal(self.pid, &self.to_spool, Signal::SIGKILL);', '                drop(hold);\n                Self::signal(self.pid, &self.to_spool, Signal::SIGTERM);'),
    (MAIN, 'ended', 'the kill of a held tool is not on record', '                drop(hold);\n                Self::signal(self.pid, &self.to_spool, Signal::SIGKILL);', '                drop(hold);\n                let _ = killpg(self.group(), Signal::SIGKILL);'),
    (MAIN, 'ended', 'the end of a held tool is never written', '                *stage = StopStage::Signalled;\n                self.changed.notify_all();\n                return;', '                return;'),

    # The connections, rewritten for the count: what they did before still holds.
    (MAIN, 'shim', 'a request is carried out only when the next one comes', '        me.pending.store(taken.contains(&b\'\\n\'), Ordering::SeqCst);', '        me.pending.store(false, Ordering::SeqCst);'),
    (MAIN, 'shim', 'the last request without its line end is dropped', '                Ok(0) => ended = true,', '                Ok(0) => break,'),
    (MAIN, 'shim', 'a line ending in a carriage return is not a request', "        if line.last() == Some(&b'\\r') {", "        if line.last() == Some(&b'\\x7f') {"),
    (MAIN, 'once', 'a connection is not forgotten when it ends', '    lock(&control.connections).retain(|other| !Arc::ptr_eq(other, me));', '    let _ = &control.connections;'),
]

if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
