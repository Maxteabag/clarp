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
RUNS['cache'] = ['test', '--lib', 'the_cached_book_matches_what_the_store_accepted']
for module, book in [('heartbeat', 'book'), ('schedules', 'schedules')]:
    file = f'clarp-host/src/{module}.rs'
    keep = 'app.fleet.keep(KIND, ALL.to_string(), Some(kept)).await?;'
    publish = f'*{book} = next;'
    PLANTS.extend([
        (file, 'cache', 'publish the candidate even if the store refuses', keep + '\n            ' + publish, publish + '\n            ' + keep),
        (file, 'cache', 'a successful write never changes the cached book', publish, ''),
        (file, 'cache', 'write only when nothing changed', f'if next != *{book}', f'if next == *{book}'),
        (file, 'cache', 'turn a failed write into a success', keep, 'drop(app.fleet.keep(KIND, ALL.to_string(), Some(kept)).await);'),
    ])
RUNS['ring'] = ['test', '--lib', 'ring_timers_report_a_refusal_and_retry_until_the_store_answers']
RUNS['chip'] = ['test', '--lib', 'an_unread_chip_arms_a_timed_look_until_its_store_recovers']
RUNS['expiry'] = ['test', '--test', 'host', 'a_ring_nobody_takes_is_missed']
RUNS['restore'] = ['test', '--test', 'host', 'a_ring_that_rang_when_the_host_was_killed']
CALLS = 'clarp-host/src/calls.rs'
CHIP = 'clarp-host/src/chip.rs'
PLANTS += [
    (CALLS, 'ring', 'a ring read failure is silent', 'Err(why) => app.fleet.unkept(RINGS, &why.0),', 'Err(_) => (),'),
    (CALLS, 'ring', 'a refused read loses the retry', 'after = Duration::from_secs(1);', 'return;'),
    (CALLS, 'ring', 'a ring retry is delayed too long', 'after = Duration::from_secs(1);', 'after = Duration::from_secs(2);'),
    (CALLS, 'ring', 'startup always sleeps before it looks', 'if !after.is_zero() {', 'if true {'),
    (CALLS, 'expiry', 'a newly placed ring has no expiry alarm', 'timer(app, placed.ring.expires_at);', ''),
    (CALLS, 'restore', 'a restored ringing call loses its alarm', 'timer(app, ring.expires_at);', 'let _ = ring;'),
    (CHIP, 'chip', 'a refused chip read goes unsaid', 'app.fleet.unkept(KIND, &why.0);\n                return Some(Duration::from_secs(60));', 'let _ = why;\n                return Some(Duration::from_secs(60));'),
    (CHIP, 'chip', 'an unread chip sleeps indefinitely', 'return Some(Duration::from_secs(60));', 'return None;'),
    (CHIP, 'chip', 'an unread chip retries on every loop', 'return Some(Duration::from_secs(60));', 'return Some(Duration::ZERO);'),
]
if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
