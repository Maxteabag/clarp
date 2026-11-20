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
RUNS['tick'] = ['test', '--lib', 'a_refused_janitor_book_still_gives_each_keeper_its_pass']
JANITORS = 'clarp-host/src/janitors.rs'
PLANTS += [
    (JANITORS, 'tick', 'a refused schedule skips the other keepers', 'app.fleet.unkept(KIND, &why.0);', 'app.fleet.unkept(KIND, &why.0); return;'),
    (JANITORS, 'tick', 'a schedule refusal goes unsaid', 'app.fleet.unkept(KIND, &why.0);', 'let _ = why;'),
    (JANITORS, 'tick', 'the tick skips audio', '    crate::audio::keep(app).await;', ''),
    (JANITORS, 'tick', 'the tick skips quota', '    crate::quota_keeper::look(app);', ''),
    (JANITORS, 'tick', 'the tick skips labels', '    crate::label_checker::look(app);', ''),
    ('clarp-host/src/audio.rs', 'tick', 'audio loses a refused read', 'Err(why) => return app.fleet.unkept(FACTS, &why.0),', 'Err(_) => return,'),
    ('clarp-host/src/quota_keeper.rs', 'tick', 'the failed quota read is filed under a different kind', 'app.fleet.unkept(ROLE, &format!', 'app.fleet.unkept("other", &format!'),
    ('clarp-host/src/label_checker.rs', 'tick', 'the failed label read goes unsaid', 'app.fleet.unkept(ROLE, &why.0);', 'let _ = why;'),
]
if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
