//! Per-conversation FCM ordering. A slow Google response never owns the APNs
//! sender. Under pressure older replies are dropped before a waiting decision.
use super::{Counted, Job, Rang, Targets, Works, deliver};
use std::collections::{HashMap, VecDeque};
use std::sync::{Arc, Mutex, Weak};
use std::time::Instant;
use tokio::sync::oneshot;

struct Pending {
    job: Job,
    rows: Vec<(String, String)>,
    rang: Rang,
    told: Option<oneshot::Sender<Rang>>,
}

impl Pending {
    fn finish(mut self, android: Counted) {
        let total = self.rang.alert.get_or_insert_with(Counted::default);
        total.enabled |= android.enabled;
        total.sent += android.sent;
        total.failed += android.failed;
        total.disabled += android.disabled;
        if let Some(told) = self.told {
            let _ = told.send(self.rang);
        }
    }
}

#[derive(Default)]
struct Lane {
    queue: VecDeque<Pending>,
    since: Option<Instant>,
}

impl Lane {
    fn prune(&mut self) {
        let Some(started) = self.since else {
            return;
        };
        while let Some(victim) = clarp_kernel::fcm::slow_queue_victim(
            started.elapsed(),
            self.queue.iter().map(|p| matches!(p.job, Job::Request(_))),
        ) {
            if let Some(dropped) = self.queue.remove(victim) {
                let failed = dropped.rows.len() as u32;
                dropped.finish(Counted {
                    enabled: true,
                    failed,
                    ..Counted::default()
                });
            }
        }
    }
}

#[derive(Default)]
pub(super) struct Lanes(HashMap<String, Weak<Mutex<Lane>>>);

impl Lanes {
    pub(super) fn submit(
        &mut self,
        works: Arc<Works>,
        job: Job,
        rows: Vec<(String, String)>,
        rang: Rang,
        told: Option<oneshot::Sender<Rang>>,
    ) {
        let session = match &job {
            Job::Turn(t) => &t.session,
            Job::Request(r) => &r.session,
            Job::Ring(r, _) => &r.ring.session,
        };
        self.0.retain(|_, lane| lane.strong_count() > 0);
        let lane = self.0.entry(session.clone()).or_default();
        let held = lane.upgrade().unwrap_or_else(|| {
            let held = Arc::new(Mutex::new(Lane::default()));
            *lane = Arc::downgrade(&held);
            held
        });
        let mut queue = held.lock().unwrap_or_else(|e| e.into_inner());
        let pending = Pending {
            job,
            rows,
            rang,
            told,
        };
        if queue.since.is_some() {
            queue.queue.push_back(pending);
            queue.prune();
            return;
        }
        queue.since = Some(Instant::now());
        drop(queue);
        std::thread::spawn(move || {
            let mut next = pending;
            loop {
                let count = deliver(&works, &next.job, Targets::Android(&next.rows));
                next.finish(count);
                let mut lane = held.lock().unwrap_or_else(|e| e.into_inner());
                lane.prune();
                let Some(queued) = lane.queue.pop_front() else {
                    lane.since = None;
                    return;
                };
                lane.since = Some(Instant::now());
                next = queued;
            }
        });
    }
}
