//! Numbers for the changes the whole Host counts in one row.
//!
//! A conversation's revisions belong to one agent. Some changes are ordered across all of
//! them: a client holds one number for "the background jobs as I last saw them". Those numbers
//! come from here. They only grow, and a Host that starts again goes on above every number it
//! gave before, because the time leads: nothing has to be stored for that.

use std::sync::atomic::{AtomicU64, Ordering};

#[derive(Debug, Default)]
pub struct Revisions {
    last: AtomicU64,
}

impl Revisions {
    pub fn new() -> Self {
        Self::default()
    }

    /// The next number: above every number given so far, and not below the time.
    pub fn next(&self, now_ms: i64) -> u64 {
        // A thousand numbers to the millisecond, and still well inside what JSON keeps exact.
        let floor = u64::try_from(now_ms).unwrap_or(0).saturating_mul(1_000);
        let mut last = self.last.load(Ordering::Relaxed);
        loop {
            let next = floor.max(last + 1);
            match self
                .last
                .compare_exchange_weak(last, next, Ordering::Relaxed, Ordering::Relaxed)
            {
                Ok(_) => return next,
                Err(seen) => last = seen,
            }
        }
    }

    /// The newest number given out: 0 when none was.
    pub fn latest(&self) -> u64 {
        self.last.load(Ordering::Relaxed)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_numbers_only_grow_and_follow_the_time() {
        let revisions = Revisions::new();
        assert_eq!(revisions.latest(), 0);
        let first = revisions.next(1_794_700_000_000);
        assert_eq!(first, 1_794_700_000_000_000);
        // Several in one millisecond, and a clock that steps back: still growing.
        let second = revisions.next(1_794_700_000_000);
        let third = revisions.next(1_794_600_000_000);
        assert!(first < second && second < third);
        assert!(revisions.next(1_794_700_000_001) > third);
        assert_eq!(revisions.latest(), 1_794_700_000_001_000);
        assert!(revisions.latest() < (1 << 53), "exact in JSON");
    }
}
