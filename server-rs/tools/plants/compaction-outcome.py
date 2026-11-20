#!/usr/bin/env python3
"""Wrong rules, planted one at a time (see `runner.py`, beside this list).

What the user's compaction of an agent did (`last_compaction`, the Python Host's PR 483, R148): the kernel's rule
from Claude Code's record of it and from how its turn ended, the agent's state that owes one outcome for that turn
and keeps the last, the actor that writes it with the turn's end, the tool's record read from its output, the row,
and the refusal of a compaction while messages wait.
"""
import runner

RUNS = {
    'vectors': ['test', '--test', 'compaction_outcome_vectors'],
    'agent': ['test', '-p', 'clarp-kernel', '--lib', 'a_compaction_owes_one_outcome_and_the_agent_keeps_the_last'],
    'host': ['test', '--test', 'host', '--', 'what_the_users_compaction_did_is_said_on_the_agents_row_from_numbers'],
}
K = 'clarp-kernel/src/compaction.rs'
A = 'clarp-kernel/src/agent.rs'
R = 'clarp-actor/src/lib.rs'
B = 'clarp-backend/src/claude.rs'
W = 'clarp-host/src/wire.rs'
C = 'clarp-host/src/compact.rs'
# NOT PLANTED, each with its reason:
#   that the actor says what a compaction did in the write that ends its turn (`compaction_said` in `closing`), and
#     that the reading of a turn's output says it before the turn is forgotten (`say_owed_compaction` in `pump`):
#     each alone says it for a turn that ends as its output is read, with the same facts. Both are there so that the
#     outcome is in the same write as the turn's end.
#   that a compaction taken back before it began, or stopped while it waited for its next attempt, is said by the
#     actor (`say_owed_compaction` in `asked` and `stop_as`): no app takes a compaction back, and the kernel's test
#     holds that such a turn owes its outcome.
#   that an actor says at its start what an actor before it owed (`say_owed_compaction` in `open`): with the outcome
#     in the same write as the turn's end, nothing is owed at a start.
#   that a record the agent's state does not owe is refused (`Command::RecordCompaction`): the actor asks only for
#     the turn that is owed, and the state that applies such an event refuses it (held by the kernel's test).
#   that the gauge before is how full the conversation was when the turn was taken up again after a restart: the
#     tool's record says the size before in the case the Host's test has.
PLANTS = [
    # The kernel's rule, held to the Python Host's own functions and to what a turn's end means here.
    (K, 'vectors', 'an estimate is said of a size there is not', '        after_is_estimate: after_is_estimate && after.is_some(),', '        after_is_estimate,'),
    (K, 'vectors', 'a record without a size before has none', '        let before = if proof == 0 { before } else { Some(proof) };', '        let before = Some(proof);'),
    (K, 'vectors', 'no conversation to read it in is a compaction that did not happen', '    if !transcript {\n        return outcome(\n            UNKNOWN,', '    if !transcript {\n        return outcome(\n            NOT_COMPACTED,'),
    (K, 'vectors', 'what ran out of time did not compact', '        "cap" => outcome(\n            UNKNOWN,', '        "cap" => outcome(\n            NOT_COMPACTED,'),
    (K, 'vectors', 'the time is said without its space', '{HARD_CAP_SEC} s"', '{HARD_CAP_SEC}s"'),
    (K, 'vectors', 'a record of another kind of line is read', '        || event["subtype"].as_str() != Some("compact_boundary")', '        || event["subtype"].as_str() == Some("compact_boundaries")'),
    (K, 'vectors', 'the size after is read as the size before', '        pre_tokens: metadata["pre_tokens"].as_u64().unwrap_or(0),', '        pre_tokens: metadata["post_tokens"].as_u64().unwrap_or(0),'),
    (K, 'vectors', 'a fraction is a size before', '        pre_tokens: metadata["pre_tokens"].as_u64().unwrap_or(0),', '        pre_tokens: metadata["pre_tokens"].as_f64().map_or(0, |size| size as u64),'),
    (K, 'vectors', 'the first record of a turn counts', '    seen.or(held)', '    held.or(seen)'),
    (K, 'vectors', 'the size after the tool says is not said', '            said.after_tokens = Some(after);', '            said.after_tokens = None;'),
    (K, 'vectors', 'the size after is said as an estimate', '            said.after_is_estimate = false;', '            said.after_is_estimate = true;'),
    (K, 'vectors', 'the words still say the size after comes with the next call', '            said.detail = said.detail.split("; ").next().unwrap_or_default().to_string();\n', ''),
    (K, 'vectors', 'a turn that ended well without a record compacted', '        Outcome::Completed => decide_claude(gauge, None, true, "settled"),', '        Outcome::Completed => decide_claude(gauge, Some(0), true, "settled"),'),
    (K, 'vectors', 'a stopped compaction is said in the words of one that ended', '        Outcome::Stopped { .. } => decide_claude(gauge, None, true, "stopped"),', '        Outcome::Stopped { .. } => decide_claude(gauge, None, true, "settled"),'),
    (K, 'vectors', 'one taken back is said in other words', 'pub const STOPPED_BEFORE_IT_BEGAN: &str = "stopped before it began";', 'pub const STOPPED_BEFORE_IT_BEGAN: &str = "stopped";'),
    (K, 'vectors', 'a failure is told in the tool\'s raw words', '            outcome(NOT_COMPACTED, gauge, None, false, &told.row)', '            outcome(NOT_COMPACTED, gauge, None, false, reason)'),
    (K, 'vectors', 'a failure without the tool\'s word did not compact', '            if !concluded && kind != failures::NOT_STARTED {', '            if concluded && kind != failures::NOT_STARTED {'),
    (K, 'vectors', 'a tool the Host never started may have compacted', '            if !concluded && kind != failures::NOT_STARTED {', '            if !concluded {'),
    (K, 'vectors', 'a tool that ended without a word did not compact', '        Outcome::Interrupted { .. } => outcome(UNKNOWN,', '        Outcome::Interrupted { .. } => outcome(NOT_COMPACTED,'),
    # The agent's state: one outcome owed for the user's compaction, the last kept.
    (A, 'agent', 'every turn owes an outcome', '        if matches!(&ended.origin, Origin::Trigger { name } if name == COMPACTION) {', '        if !ended.origin.name().is_empty() {'),
    (A, 'agent', 'a compaction taken back owes nothing', '            Turn::Settled(ended) => {\n                self.settled += 1;\n                self.owe_compaction(&ended);\n            }', '            Turn::Settled(_) => self.settled += 1,'),
    (A, 'agent', 'an outcome said is still owed', '                self.compaction_owed = None;\n                self.last_compaction = Some(said.clone());', '                self.last_compaction = Some(said.clone());'),
    (A, 'agent', 'the first outcome is kept', '                self.last_compaction = Some(said.clone());', '                self.last_compaction = self.last_compaction.clone().or_else(|| Some(said.clone()));'),
    (A, 'agent', 'an outcome nothing owes is in the log', '                if self.compaction_owed.as_ref().map(|owed| owed.id) != Some(*turn) {\n                    return Err(IllegalTransition {', '                if self.compaction_owed.is_some() && self.compaction_owed.is_none() {\n                    return Err(IllegalTransition {'),
    # What the tool's output says, and what the actor keeps of it.
    (B, 'host', "the tool's record is not told", '                if let Some(record) = clarp_kernel::compaction::boundary(event) {', '                if let Some(record) = clarp_kernel::compaction::boundary(&Value::Null) {'),
    (B, 'host', "the tool's own result is not told", '                out.push(Signal::Concluded);\n', ''),
    (R, 'host', "the tool's own result is not kept", '                            live.concluded = true;', '                            live.concluded = false;'),
    (R, 'host', "the turn's record is not kept", '            compacted: self.compacted,', '            compacted: None,'),
    (R, 'host', 'how full the conversation was is not kept', '            gauge: self.gauge,', '            gauge: None,'),
    (R, 'host', 'a Host that comes back does not read the record again', '                            compacted = compaction::newest(compacted, Some(record));', '                            let _ = record;'),
    (R, 'host', 'whose tool it was is not said', '        backend: facts.backend.to_string(),', '        backend: String::new(),'),
    (R, 'host', 'when it ended is not said', '        at: owed.ended_at_ms,', '        at: 0,'),
    (R, 'host', 'the state does not carry it', '            last_compaction: agent.last_compaction().cloned(),', '            last_compaction: None,'),
    # The row, and the refusal.
    (W, 'host', 'the row says nothing of it', '        "last_compaction": status.last_compaction,', '        "last_compaction": null,'),
    (C, 'host', 'a compaction is asked for while messages wait', '    if !status.waiting.is_empty() {', '    if status.waiting.len() > 1 {'),
]


if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
