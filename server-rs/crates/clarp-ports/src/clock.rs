//! Time as a port, so the simulator can own it.

use std::sync::atomic::{AtomicI64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

/// Milliseconds since the Unix epoch.
pub trait Clock: Send + Sync {
    fn now_ms(&self) -> i64;
}

/// The machine's clock.
#[derive(Debug, Default, Clone, Copy)]
pub struct SystemClock;

impl Clock for SystemClock {
    fn now_ms(&self) -> i64 {
        match SystemTime::now().duration_since(UNIX_EPOCH) {
            Ok(elapsed) => i64::try_from(elapsed.as_millis()).unwrap_or(i64::MAX),
            // A clock set before 1970 is a broken machine, not a reason to stop serving.
            Err(_) => 0,
        }
    }
}

/// A clock that moves only when told to: tests and the simulator.
#[derive(Debug, Default)]
pub struct ManualClock {
    now_ms: AtomicI64,
}

impl ManualClock {
    pub fn starting_at(now_ms: i64) -> Self {
        Self {
            now_ms: AtomicI64::new(now_ms),
        }
    }

    pub fn advance_ms(&self, by: i64) {
        self.now_ms.fetch_add(by, Ordering::SeqCst);
    }
}

impl Clock for ManualClock {
    fn now_ms(&self) -> i64 {
        self.now_ms.load(Ordering::SeqCst)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn manual_clock_moves_only_when_told() {
        let clock = ManualClock::starting_at(1_000);
        assert_eq!(clock.now_ms(), 1_000);
        clock.advance_ms(250);
        assert_eq!(clock.now_ms(), 1_250);
    }

    #[test]
    fn system_clock_is_after_2026() {
        assert!(SystemClock.now_ms() > 1_767_225_600_000);
    }
}
