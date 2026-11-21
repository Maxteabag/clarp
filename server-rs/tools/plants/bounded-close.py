#!/usr/bin/env python3
"""Wrong rules, planted one at a time (see `runner.py`, beside this list).

Closing an agent never waits without a bound: how long a close waits and what it counts as closed
(`AgentHandle::close`), what the fleet does and leaves undone when a removal's close did not come, what it does when
asked again (`Fleet::remove`), and how a start finishes a removal that did not come (`fleet::start`).
"""
import runner

RUNS = {
    'close': ['test', '-p', 'clarp-host', '--lib', 'fleet::closing_tests'],
    'mailbox': ['test', '-p', 'clarp-host', '--lib', 'agent::mailbox_tests'],
    'start': ['test', '-p', 'clarp-host', '--test', 'host', '--', 'bounded_close'],
}
A = 'clarp-host/src/agent.rs'
F = 'clarp-host/src/fleet.rs'
# NOT PLANTED, each with its reason:
#   that an agent asked to close before is not stopped again (`if asked_before.is_none() {` before
#     `stop_for_roster`): its moment of deletion is kept apart from the stop (planted below), and a second stop finds no
#     delegation the first left running and writes the same tombstone; nothing tells the two apart.
SEND = '            let _ = self.tx.send(Ask::Close(CloseReply(self.close.clone())));'
WAIT = '            .wait_timeout_while(state, within, |state| *state == Closing::Requested)'
CLOSED = '        *state == Closing::Closed\n'
REPLY = '        *self.0.lock() = Closing::Closed;'
REQUEST = '        if matches!(*state, Closing::Open | Closing::Preparing) {'
FENCE = '        *self.close.lock() = Closing::Preparing;'
RESTORE = '            *self.close.lock() = Closing::Open;'
ADMISSION = '        Self::check_open(*state)?;'
BOUND = 'pub const CLOSE_WITHIN: Duration = Duration::from_secs(3);'
ASKED = '        let asked_before = self.closing.get(&record.agent_id).copied();'
WHEN = '        record.deleted_at_ms = Some(asked_before.unwrap_or_else(|| self.now_ms()));'
KEPT = ('            self.closing.insert(\n'
        '                record.agent_id.clone(),\n'
        '                record.deleted_at_ms.unwrap_or_default(),\n'
        '            );\n')
PAST = '        if !agent.close(agent::CLOSE_WITHIN) {'
WORDS = '                "The agent\'s thread did not answer within {} seconds, so it is not removed yet: ask again to finish removing it.",'
BEFORE = ('        // Its thread is the one writer of what is its own: until it has closed, nothing of\n'
          '        // that is touched, and the agent stays as it is. Its tombstone is kept.\n')
PARENT_GUARD = ('            if let Some(at) = self.place_of(parent) {\n'
                '                self.members[at]\n'
                '                    .agent\n'
                '                    .accepting_work()\n'
                '                    .map_err(|why| Told::new(503, agent::REMOVING_CODE, why.0))?;\n'
                '            }\n')
AMEND = ('        self.members[at].agent.accepting_work()?;\n'
         '        // A contact chosen by the rule is chosen here, in the turn that gives it.')
FORGOTTEN = '        self.closing.remove(&record.agent_id);\n        self.gone.insert('
DELETED = '        .filter(|(_, record)| record.deleted_at_ms.is_some())'
PARENT = '            .is_some_and(|parent| deleted.contains(parent));'
EVENT = '        if let Some(moved) = helpers::next_state(current, "parent_gone").filter(|_| parent_gone) {'
COMPLETED = '            record.helper_completed_at_ms = moved.completed.then(|| works.clock.now_ms());'
WRITTEN = ('            store\n'
           '                .put(AGENTS, &agent_id, &value)\n'
           '                .map_err(|e| e.to_string())?;\n'
           '        }\n'
           '        match setup(&works, &delegation_desk, &versions, &record) {')
PLANTS = [
    # How long a close waits, and what it counts as closed.
    (A, 'close', 'a close waits ten times its bound', WAIT, WAIT.replace('within,', 'within * 10,')),
    (A, 'close', 'a close waits half its bound', WAIT, WAIT.replace('within,', 'within / 2,')),
    (A, 'close', 'the bound is four seconds', BOUND, BOUND.replace('3)', '4)')),
    (A, 'close', 'the bound is two seconds', BOUND, BOUND.replace('3)', '2)')),
    (A, 'close', 'a thread that is gone when asked is not closed', SEND,
     '            if self.tx.send(Ask::Close(CloseReply(self.close.clone()))).is_err() { return false; }'),
    (A, 'close', 'a thread that ends during the wait is not closed', REPLY,
     '        if std::thread::panicking() { return; }\n' + REPLY),
    (A, 'close', 'a thread that did not answer in time is closed', CLOSED, '        true\n'),
    (A, 'close', 'a close is not waited for at all', WAIT, WAIT.replace('within,', 'Duration::ZERO,')),
    (A, 'mailbox', 'every attempt sends another close', REQUEST, '        if *state != Closing::Closed {'),
    (A, 'mailbox', 'the tombstone is written while work is still accepted', FENCE, ''),
    (A, 'mailbox', 'a failed tombstone write leaves admission closed', RESTORE, ''),
    (A, 'close', 'an old handle can send to an agent being removed', ADMISSION, ''),
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
    (F, 'close', 'an agent being removed can still receive a new helper', PARENT_GUARD, ''),
    (F, 'close', 'the roster record can still be amended during removal', AMEND, AMEND.split('\n')[1]),
    (F, 'close', 'a timed-out close holds the fleet another second', PAST,
     PAST + '\n            std::thread::sleep(Duration::from_secs(1));'),
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
     '            let _ = value;\n        }\n        match setup(&works, &delegation_desk, &versions, &record) {'),
]


if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
