#!/usr/bin/env python3
"""Wrong rules, planted one at a time (see `runner.py`, beside this list).

Closing an agent never waits without a bound: how long a close waits and what it counts as closed
(`AgentHandle::close`), what the fleet does and leaves undone when a removal's close did not come, what it does when
asked again (`Fleet::remove`), and how a start finishes a removal that did not come (`fleet::start`).
"""
import runner

RUNS = {
    'close': ['test', '-p', 'clarp-host', '--lib', 'fleet::closing_tests'],
    'start': ['test', '-p', 'clarp-host', '--test', 'host', '--', 'bounded_close'],
}
A = 'clarp-host/src/agent.rs'
F = 'clarp-host/src/fleet.rs'
# NOT PLANTED, each with its reason:
#   that an agent asked to close before is not stopped again (`if asked_before.is_none() {` before
#     `stop_for_roster`): its moment of deletion is kept apart from the stop (planted below), and a second stop finds no
#     delegation the first left running and writes the same tombstone; nothing tells the two apart.
#   that the fleet forgets an agent it no longer closes only after a write that may fail
#     (`self.closing.remove` after the tombstone is put again): a store that refuses that one write is not a case the
#     fleet's tests can make without refusing every write.
SEND = ('        if self.tx.send(Ask::Close(closed)).is_err() {\n'
        '            return true;\n'
        '        }')
WAIT = '        !matches!(wait.recv_timeout(within), Err(RecvTimeoutError::Timeout))'
BOUND = 'pub const CLOSE_WITHIN: Duration = Duration::from_secs(3);'
ASKED = '        let asked_before = self.closing.get(&record.agent_id).copied();'
WHEN = '        record.deleted_at_ms = Some(asked_before.unwrap_or_else(|| self.now_ms()));'
KEPT = ('            self.closing.insert(\n'
        '                record.agent_id.clone(),\n'
        '                record.deleted_at_ms.unwrap_or_default(),\n'
        '            );\n')
PAST = '        if !self.members[at].agent.close(agent::CLOSE_WITHIN) {'
WORDS = '                "The agent\'s thread did not answer within {} seconds, so it is not removed yet: ask again to finish removing it.",'
BEFORE = ('        // Its thread is the one writer of what is its own: until it has closed, nothing of\n'
          '        // that is touched, and the agent stays as it is. Its tombstone is kept.\n')
AGAIN = ('        if asked_before.is_some() {\n'
         '            // What was changed of it since may have written over its tombstone.\n'
         '            self.put(&record).map_err(Unavailable)?;\n'
         '        }\n')
FORGOTTEN = '        self.closing.remove(&record.agent_id);\n        self.gone.insert('
DELETED = '        .filter(|(_, record)| record.deleted_at_ms.is_some())'
PARENT = '            .is_some_and(|parent| deleted.contains(parent));'
EVENT = '        if let Some(moved) = helpers::next_state(current, "parent_gone").filter(|_| parent_gone) {'
COMPLETED = '            record.helper_completed_at_ms = moved.completed.then(|| works.clock.now_ms());'
WRITTEN = ('            store\n'
           '                .put(AGENTS, &agent_id, &value)\n'
           '                .map_err(|e| e.to_string())?;\n'
           '        }\n'
           '        match setup(&works, &delegation_desk, &record) {')
PLANTS = [
    # How long a close waits, and what it counts as closed.
    (A, 'close', 'a close waits ten times its bound', WAIT, WAIT.replace('within)', 'within * 10)')),
    (A, 'close', 'a close waits half its bound', WAIT, WAIT.replace('within)', 'within / 2)')),
    (A, 'close', 'the bound is four seconds', BOUND, BOUND.replace('3)', '4)')),
    (A, 'close', 'the bound is two seconds', BOUND, BOUND.replace('3)', '2)')),
    (A, 'close', 'a thread that is gone when asked is not closed', SEND, SEND.replace('return true;', 'return false;')),
    (A, 'close', 'a thread that ends during the wait is not closed', WAIT,
     '        matches!(wait.recv_timeout(within), Ok(()))'),
    (A, 'close', 'a thread that did not answer in time is closed', WAIT,
     '        {\n            let _ = wait.recv_timeout(within);\n            true\n        }'),
    (A, 'close', 'a close is not waited for at all', WAIT, '        true'),
    # What the fleet does past the bound, and when asked again.
    (F, 'close', 'past the bound the removal goes on', PAST, PAST.replace(' {', ' && false {')),
    (F, 'close', 'the refusal says another bound', WORDS, WORDS.replace('within {}', 'within {} whole')),
    (F, 'close', 'the refusal does not say to ask again', WORDS, WORDS.replace(': ask again to finish removing it.', '.')),
    (F, 'close', 'past the bound its goals are retired', BEFORE,
     '        let _ = retire_goals(&self.works, &record, 0);\n' + BEFORE),
    (F, 'close', 'past the bound its helpers are abandoned', BEFORE,
     '        self.abandon_helpers_of(&record.agent_id)?;\n' + BEFORE),
    (F, 'close', 'past the bound the apps are told it was deleted', BEFORE,
     '        self.tell_roster("deleted", session);\n' + BEFORE),
    (F, 'close', 'past the bound the focus is let go', BEFORE,
     '        if self.focus.as_deref() == Some(record.agent_id.as_str()) {\n'
     '            self.focus = None;\n'
     '            self.publish();\n'
     '        }\n' + BEFORE),
    (F, 'close', 'past the bound it leaves the roster', BEFORE,
     '        self.members.remove(at);\n        self.publish();\n        return Ok(true);\n' + BEFORE),
    (F, 'close', 'an agent asked to close before is not known as such', ASKED,
     ASKED.replace('.get(&record.agent_id)', '.get("")')),
    (F, 'close', 'asked again, it is deleted at the moment of the second ask', WHEN,
     '        record.deleted_at_ms = Some(self.now_ms());'),
    (F, 'close', 'an agent whose close did not come is not kept as closing', KEPT, ''),
    (F, 'close', 'asked again, its tombstone is not kept again', AGAIN, ''),
    (F, 'close', 'removed, it is still kept as closing', FORGOTTEN, '        self.gone.insert('),
    # How a start finishes a removal that did not come.
    (F, 'start', 'a start takes the living for the deleted', DELETED, DELETED.replace('is_some()', 'is_none()')),
    (F, 'start', "a start abandons the helpers of the living", PARENT, PARENT.replace('deleted.contains', '!deleted.contains')),
    (F, 'start', 'a start abandons every helper', EVENT, EVENT.replace('.filter(|_| parent_gone)', '')),
    (F, 'start', 'a start abandons none', EVENT, EVENT.replace('parent_gone)', 'false)')),
    (F, 'start', 'a start moves the helper by another event', EVENT, EVENT.replace('"parent_gone"', '"went_idle"')),
    (F, 'start', 'a start keeps no moment its helper finished', COMPLETED,
     '            record.helper_completed_at_ms = None;'),
    (F, 'start', 'a start moves the helper and writes nothing', WRITTEN,
     '            let _ = value;\n        }\n        match setup(&works, &delegation_desk, &record) {'),
]


if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
