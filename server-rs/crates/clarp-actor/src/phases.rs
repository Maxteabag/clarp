//! How long each step of reading an agent took (issue 935).
//!
//! A Host reads every agent from what its log committed before the agent's own thread opens it
//! (`Actor::glance`). One of those readings once took 17.6 seconds and held the first answer of
//! that agent's chat all that time; only the whole was timed. The steps are timed on the
//! monotonic clock and said, as whole milliseconds, with the one line a slow reading already
//! has; nothing is said of a reading that was quick. The writer's own opening reads the same
//! books with a recorder that is off: no clock is read and nothing is kept.

use std::time::{Duration, Instant};

/// The steps of one reading of an agent, in the order they ran, each with what it took; the
/// step under way, where one is; and the step a reading stopped in, with what it took until it
/// stopped.
#[derive(Debug, Clone)]
pub struct Phases {
    on: bool,
    done: Vec<(&'static str, Duration)>,
    current: Option<(&'static str, Instant)>,
    stopped: Option<(&'static str, Duration)>,
}

impl Default for Phases {
    /// A recorder that times.
    fn default() -> Self {
        Self {
            on: true,
            done: Vec::new(),
            current: None,
            stopped: None,
        }
    }
}

impl Phases {
    /// A recorder that times nothing: every mark is a no-op, with no clock read and nothing kept.
    pub fn off() -> Self {
        Self {
            on: false,
            ..Self::default()
        }
    }

    /// The step named begins now, and the one before it, where one is under way, ends.
    pub fn begin(&mut self, step: &'static str) {
        if !self.on {
            return;
        }
        self.end();
        self.current = Some((step, Instant::now()));
    }

    /// The step under way, where one is, ends now.
    pub fn end(&mut self) {
        if let Some((step, began)) = self.current.take() {
            self.done.push((step, began.elapsed()));
        }
    }

    /// The reading stopped now, in the step under way: its time is what it took until now, and
    /// whatever follows (a look at the disk, the journal's line) is not counted in it.
    pub fn stop(&mut self) {
        if let Some((step, began)) = self.current.take() {
            self.stopped = Some((step, began.elapsed()));
        }
    }

    /// The step a reading stopped in, or the one under way: the last one begun and not ended.
    pub fn under_way(&self) -> Option<&'static str> {
        self.current
            .map(|(step, _)| step)
            .or(self.stopped.map(|(step, _)| step))
    }

    /// The steps that ended, in their order, each with what it took.
    pub fn done(&self) -> &[(&'static str, Duration)] {
        &self.done
    }

    /// Each step with its whole milliseconds, in order (`open=12 recorder=3400 ...`), and the
    /// step it stopped in with what it took until then; a step still under way, with what it
    /// took so far.
    pub fn said(&self) -> String {
        let mut said: Vec<String> = self
            .done
            .iter()
            .map(|(step, took)| format!("{step}={}", took.as_millis()))
            .collect();
        if let Some((step, took)) = self.stopped {
            said.push(format!("stopped in {step}={}", took.as_millis()));
        } else if let Some((step, began)) = self.current {
            said.push(format!("stopped in {step}={}", began.elapsed().as_millis()));
        }
        said.join(" ")
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;

    /// Each step ends where the next begins, in its order; a reading that stops keeps the time
    /// of its step as it was when it stopped; a recorder that is off keeps nothing.
    #[test]
    fn each_step_is_timed_in_its_order_and_a_stopped_one_keeps_its_time() {
        let mut phases = Phases::default();
        phases.begin("open");
        phases.begin("recorder");
        std::thread::sleep(Duration::from_millis(30));
        phases.begin("jobs");
        let steps: Vec<&str> = phases.done().iter().map(|(step, _)| *step).collect();
        assert_eq!(steps, ["open", "recorder"]);
        assert!(phases.done()[1].1 >= Duration::from_millis(30));
        phases.stop();
        assert_eq!(phases.under_way(), Some("jobs"));
        let said = phases.said();
        std::thread::sleep(Duration::from_millis(30));
        assert_eq!(phases.said(), said, "a stopped step's time does not grow");
        assert!(said.contains(" stopped in jobs="), "{said}");

        let mut off = Phases::off();
        off.begin("open");
        off.stop();
        assert!(off.done().is_empty() && off.under_way().is_none() && off.said().is_empty());
    }
}
