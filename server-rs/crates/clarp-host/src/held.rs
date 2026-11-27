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
    /// How long each step of what it served took, where they were timed: said with the one line
    /// a slow hold has, and formatted only then (issue 935).
    steps: Option<clarp_actor::Phases>,
}

impl<'a> Held<'a> {
    pub(crate) fn new(thread: &'a str, what: &'static str) -> Self {
        Self {
            thread,
            what,
            began: Instant::now(),
            steps: None,
        }
    }

    /// The steps of what it served, each timed: said only where the hold was slow.
    pub(crate) fn steps(&mut self, steps: clarp_actor::Phases) {
        self.steps = Some(steps);
    }
}

impl Held<'_> {
    /// What the journal is told of a hold this long: nothing where it was quick.
    fn said(&self, held: std::time::Duration) -> Option<String> {
        if held < SLOW {
            return None;
        }
        let steps = self
            .steps
            .as_ref()
            .map(|steps| format!(" (steps, ms: {})", steps.said()))
            .unwrap_or_default();
        Some(format!(
            "clarp-host: {} was held {} ms by {}{steps}",
            self.thread,
            held.as_millis(),
            self.what
        ))
    }
}

impl Drop for Held<'_> {
    fn drop(&mut self) {
        if let Some(said) = self.said(self.began.elapsed()) {
            eprintln!("{said}");
        }
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;

    /// Issue 935: a slow glance is said in its one line as before, with each of its steps after
    /// it; a quick one is not said; a hold with no steps is said as it always was.
    #[test]
    fn a_slow_hold_says_its_steps_in_its_one_line_and_a_quick_one_nothing() {
        let mut phases = clarp_actor::Phases::default();
        phases.begin("open");
        phases.begin("recorder");
        phases.end();
        let mut held = Held::new("the thread of agent maximus", "its glance");
        held.steps(phases);
        assert_eq!(held.said(Duration::from_millis(1_999)), None);
        let said = held.said(Duration::from_millis(17_574)).unwrap();
        assert!(
            said.starts_with(
                "clarp-host: the thread of agent maximus was held 17574 ms by its glance (steps, ms: open="
            ),
            "{said}"
        );
        assert!(said.contains(" recorder="), "{said}");
        let plain = Held::new("the fleet's thread", "Kept");
        assert_eq!(
            plain.said(Duration::from_millis(5_633)).unwrap(),
            "clarp-host: the fleet's thread was held 5633 ms by Kept"
        );
        drop(held);
        drop(plain);
    }
}
