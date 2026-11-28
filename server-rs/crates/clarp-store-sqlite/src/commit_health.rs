//! Process-local counts of successful slow agent-journal commits.
use std::sync::{LazyLock, Mutex};
use std::time::{Duration, Instant};

const SECONDS: usize = 60;
static START: LazyLock<Instant> = LazyLock::new(Instant::now);
static BUCKETS: Mutex<[Bucket; SECONDS + 1]> = Mutex::new([Bucket::EMPTY; SECONDS + 1]);

#[derive(Clone, Copy)]
struct Bucket {
    second: u64,
    count: u64,
    max_ms: u64,
}

impl Bucket {
    const EMPTY: Self = Self {
        second: 0,
        count: 0,
        max_ms: 0,
    };
}

pub(super) fn start() {
    LazyLock::force(&START);
}

pub(super) fn record(elapsed: Duration) {
    if elapsed < super::SLOW_APPEND {
        return;
    }
    let mut buckets = BUCKETS
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    let second = START.elapsed().as_secs();
    let slot = &mut buckets[(second % (SECONDS as u64 + 1)) as usize];
    if slot.second != second {
        *slot = Bucket {
            second,
            ..Bucket::EMPTY
        };
    }
    slot.count = slot.count.saturating_add(1);
    slot.max_ms = slot
        .max_ms
        .max(u64::try_from(elapsed.as_millis()).unwrap_or(u64::MAX));
}

#[derive(Debug)]
pub struct Recent {
    pub window_ms: u64,
    pub covered_ms: u64,
    pub threshold_ms: u64,
    pub count: u64,
    pub max_ms: Option<u64>,
}

/// The most recent complete seconds since SQLite monitoring started.
/// Excluding the in-progress second keeps the defined window exact.
pub fn recent() -> Recent {
    let buckets = BUCKETS
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    summary(&buckets[..], START.elapsed().as_secs())
}

fn summary(buckets: &[Bucket], end: u64) -> Recent {
    let start = end.saturating_sub(SECONDS as u64);
    let mut count = 0_u64;
    let mut max_ms = None;
    for slot in buckets {
        if slot.count > 0 && (start..end).contains(&slot.second) {
            count = count.saturating_add(slot.count);
            max_ms = Some(max_ms.unwrap_or(0).max(slot.max_ms));
        }
    }
    Recent {
        window_ms: SECONDS as u64 * 1000,
        covered_ms: (end - start) * 1000,
        threshold_ms: super::SLOW_APPEND.as_secs() * 1000,
        count,
        max_ms,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn reports_partial_coverage_and_expires_the_old_maximum() {
        let samples = [
            Bucket {
                second: 0,
                count: 2,
                max_ms: 8000,
            },
            Bucket {
                second: 59,
                count: 1,
                max_ms: 3000,
            },
        ];
        assert_eq!(summary(&samples, 0).covered_ms, 0);
        assert_eq!(summary(&samples, 1).covered_ms, 1000);
        assert_eq!(summary(&samples, 60).count, 3);
        assert_eq!(summary(&samples, 61).max_ms, Some(3000));
        let quiet = summary(&samples, 120);
        assert_eq!(quiet.covered_ms, 60000);
        assert_eq!(quiet.count, 0);
        assert!(quiet.max_ms.is_none());
    }
}
