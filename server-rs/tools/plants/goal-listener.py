#!/usr/bin/env python3
"""Wrong rules about observing a burst, admitting a wake and recovering its listener."""
import runner

RUNS = {
    'actor': ['test', '--test', 'actor', 'a_quiet_listener_keeps_its_heartbeat'],
    'clock': ['test', '--test', 'goal_listener'],
    'host': ['test', '--test', 'host', 'the_listener_wakes_the_delegate_once'],
}
K = 'clarp-kernel/src/goals/listener.rs'
H = 'clarp-host/src/goal_listener.rs'
PLANTS = [(K,'clock',why,old,new) for why,old,new in [
    ('a heartbeat is always due', '>= 5000', '>= 0'),
    ('a heartbeat waits one extra millisecond', '>= 5000', '> 5000'),
    ('an idle delegation sends its empty source marker', 'if activity.marker == (0, 0)', 'if false'),
    ('the retry deadline also forbids its own instant', 'if now < self.retry_at', 'if now <= self.retry_at'),
    ('a failed wake can be sent while its backoff is active', 'return Decision::Backoff;', 'return Decision::Idle;'),
    ('the time of source is the tick time','state.last_source_at = Some(at);','state.last_source_at = Some(now);'),
    ('an event-only wake forgets the last message applied','activity.marker.0.max(applied.0)','activity.marker.0'),
    ('a message-only wake forgets the last event applied','activity.marker.1.max(applied.1)','activity.marker.1'),
    ('applied and dispatched must differ on both cursors to be in flight','|| state.dispatched_goal_event_through > applied.1','&& state.dispatched_goal_event_through > applied.1'),
    ('timeout forbids retry on its exact deadline','< 900_000','<= 900_000'),
    ('a wake times out a millisecond early','< 900_000','< 899_999'),
    ('a busy retry loses the reason it waited','previous wake not applied yet: {busy}','waiting: {busy}'),
    ('a full acknowledgement still allows an unchanged wake','if through.0 <= state.dispatched_message_through','if through.0 < state.dispatched_message_through'),
    ('a changed marker does not restart quiet time','self.last_change = now;', 'self.last_change = self.first_seen;'),
    ('new activity forgets when the burst first began','if self.first_seen == 0', 'if true'),
    ('the listener dispatches one millisecond before quiet','< 8000','< 7999'),
    ('the listener waits an extra millisecond after quiet','< 8000','<= 8000'),
    ('a continuous burst dispatches one millisecond early','< 60_000','< 59_999'),
    ('a continuous burst waits past its final deadline','< 60_000','<= 60_000'),
    ('an admitted wake is ignored before the first dispatch','waiting before the next wake: {busy}','working'),
    ('a retry repeats the original client id','wake_id.push_str(&format!("-r{}", now.div_euclid(1000)));','wake_id.push_str("");'),
    ('retry ids use milliseconds','now.div_euclid(1000)','now'),
    ('the first wake has only an event cursor in its id','bookkeeping-{id}-m{}-e{}','bookkeeping-{id}-{}-e{}'),
    ('a successful send advances the event marker as a message','max(wake.through.0)','max(wake.through.1)'),
    ('a successful send advances the message marker as an event','max(wake.through.1)','max(wake.through.0)'),
    ('successful dispatch does not persist the wake id','state.last_wake_id.clone_from(&wake.id);','state.last_wake_id.clear();'),
    ('successful dispatch does not date the wake','state.last_wake_at = Some(now);','state.last_wake_at = None;'),
    ('after success the next wake inherits the failure count','self.failures = 0;', 'self.failures += 0;'),
    ('after success the retry deadline survives','self.retry_at = 0;', 'self.retry_at += 0;'),
    ('on failure the delay is one second times the power of two','2000_i64 <<','1000_i64 <<'),
    ('the retry delay can pass five minutes','.min(300_000)', '.min(600_000)'),
    ('a dispatch error takes 499 characters','{why}"), 500','{why}"), 499'),
    ('a dispatch error has no description','wake failed: {why}','{why}'),
    ('a delegate prompt asks for the dispatched rather than observed cursor','using the through values observe returned','using the through values dispatched'),
]]
PLANTS += [
    (K,'clock','a two-hour-old delegate turn still prevents a wake','at > now.saturating_sub(7_200_000)','at >= now.saturating_sub(7_200_000)'),
    (K,'clock','a queued wake can be joined','if queued {','if false {'),
    (K,'clock','success preserves a stale error','state.last_error.clear();','state.last_error.push_str("");'),
    (K,'clock','after idle the listener still tracks the old marker','self.marker = (0, 0);\n            self.first_seen = 0;', 'self.first_seen = 0;'),
    (H,'host','listener does not recover a failed job','job.filter(|job| job.status.is_active())','job.filter(|job| job.status != JobStatus::Cancelled)'),
    (H,'host','stopping the grant fails its job','failed: None,','failed: Some("stopped".into()),'),
    (H,'host','the queued bookkeeping turn goes to the principal','session: helper.record.session.clone(),','session: owner.record.session.clone(),'),
    (H,'host','an applied cursor is used as the dispatched wake id','client_msg_id: Some(wake.id.clone()),','client_msg_id: Some(row.id.clone()),'),
    (H,'host','background job cancellation leaves authority intact','stop("listener job cancelled")','stop("paused")'),
    ('clarp-host/src/edge.rs','host','the Host advertises the wrong goal ledger contract','("goal_ledger", 52)','("goal_ledger", 53)'),
    ('clarp-actor/src/bookkeeping.rs','actor','the heartbeat is journaled','self.listener_heartbeat = update.state.last_heartbeat_at.take();','self.listener_heartbeat = update.state.last_heartbeat_at;'),
]
if __name__ == '__main__':
    raise SystemExit(runner.main(__file__, RUNS, PLANTS))
