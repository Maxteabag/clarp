//! A thread that serves one request at a time (the fleet's, an agent's) makes every other
//! request wait while one is served. When one holds it long, the journal says which, so that a
//! wait seen elsewhere (a background job's registration that did not answer within 30 seconds,
//! issue 775) can be traced to what the thread was doing.

use std::time::{Duration, Instant};

/// How long one request may hold its thread before the journal is told.
pub(crate) const SLOW: Duration = Duration::from_secs(2);

/// What a thread is serving, from now until it is dropped.
pub(crate) struct Held<'a> {
    thread: &'a str,
    what: &'static str,
    began: Instant,
}

impl<'a> Held<'a> {
    pub(crate) fn new(thread: &'a str, what: &'static str) -> Self {
        Self {
            thread,
            what,
            began: Instant::now(),
        }
    }
}

impl Drop for Held<'_> {
    fn drop(&mut self) {
        let held = self.began.elapsed();
        if held >= SLOW {
            eprintln!(
                "clarp-host: {} was held {} ms by {}",
                self.thread,
                held.as_millis(),
                self.what
            );
        }
    }
}
