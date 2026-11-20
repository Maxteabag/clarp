#!/usr/bin/env python3
"""Wrong rules for due work at the actor and Host desks."""
import runner

AGENT = 'clarp-host/src/agent.rs'
RUNS = {'mail': ['test', '--lib', 'a_ready_deadline_gets_a_turn_under_continuous_mail']}
PLANTS = [(AGENT,'mail',name,old,new) for name,old,new in [
    ('one extra message passes a ready deadline', 'MAILBOX_BURST: usize = 32;', 'MAILBOX_BURST: usize = 33;'),
    ('timers preempt the last message in the burst', 'MAILBOX_BURST: usize = 32;', 'MAILBOX_BURST: usize = 31;'),
    ('deadline at the current instant is skipped', 'due.is_some_and(|at| at <= now)', 'due.is_some_and(|at| at < now)'),
    ('a full burst forces even a future deadline', 'due.is_some_and(|at| at <= now)', 'due.is_some()'),
    ('a full burst invents a deadline in a mailbox without one', 'due.is_some_and(|at| at <= now)', 'due.is_none_or(|at| at <= now)'),
    ('the timer turn never releases the mailbox', 'self.handled = 0;\n            return Err', 'self.handled = MAILBOX_BURST;\n            return Err'),
    ('successful receives never count toward the burst', '(self.handled + 1).min(MAILBOX_BURST)', 'self.handled'),
]]
if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
