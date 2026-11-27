//! How long each step of reading an agent took (issue 935).
//!
//! A Host reads every agent from what its log committed before the agent's own thread opens it
//! (`Actor::glance`). One of those readings once took 17.6 seconds and held the first answer of
//! that agent's chat all that time; only the whole was timed. The steps are timed on the
//! monotonic clock and said, as whole milliseconds, with the one line a slow reading already
//! has; nothing is said of a reading that was quick.

use std::time::{Duration, Instant};

/// The steps of one reading of an agent, in the order they ran, each with what it took; and the
/// step under way, where one is: where a reading that failed stopped.
#[derive(Debug, Default, Clone)]
pub struct Phases {
    done: Vec<(&'static str, Duration)>,
    current: Option<(&'static str, Instant)>,
}

impl Phases {
    /// The step named begins now, and the one before it, where one is under way, ends.
    pub fn begin(&mut self, step: &'static str) {
        self.end();
        self.current = Some((step, Instant::now()));
    }

    /// The step under way, where one is, ends now.
    pub fn end(&mut self) {
        if let Some((step, began)) = self.current.take() {
            self.done.push((step, began.elapsed()));
        }
    }

    /// The step under way: the last one begun, where a reading that stopped did not end it.
    pub fn under_way(&self) -> Option<&'static str> {
        self.current.map(|(step, _)| step)
    }

    /// The steps that ended, in their order, each with what it took.
    pub fn done(&self) -> &[(&'static str, Duration)] {
        &self.done
    }

    /// Each step with its whole milliseconds, in order (`open=12 recorder=3400 ...`), and the
    /// step under way with what it took so far, said as the one it stopped in.
    pub fn said(&self) -> String {
        let mut said: Vec<String> = self
            .done
            .iter()
            .map(|(step, took)| format!("{step}={}", took.as_millis()))
            .collect();
        if let Some((step, began)) = self.current {
            said.push(format!("stopped in {step}={}", began.elapsed().as_millis()));
        }
        said.join(" ")
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;

    /// Each step ends where the next begins, in its order; one that was not ended is the step a
    /// reading stopped in.
    #[test]
    fn each_step_is_timed_in_its_order_and_one_left_under_way_is_where_it_stopped() {
        let mut phases = Phases::default();
        phases.begin("open");
        phases.begin("recorder");
        std::thread::sleep(Duration::from_millis(30));
        phases.begin("jobs");
        assert_eq!(phases.under_way(), Some("jobs"));
        let steps: Vec<&str> = phases.done().iter().map(|(step, _)| *step).collect();
        assert_eq!(steps, ["open", "recorder"]);
        assert!(phases.done()[1].1 >= Duration::from_millis(30));
        let said = phases.said();
        assert!(said.starts_with("open="), "{said}");
        assert!(said.contains(" recorder="), "{said}");
        assert!(said.contains(" stopped in jobs="), "{said}");
        phases.end();
        assert_eq!(phases.under_way(), None);
        assert!(!phases.said().contains("stopped in"));
    }
}
