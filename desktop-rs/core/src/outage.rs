//! A Host that stops answering for a moment: a gateway in front of it
//! answering 502 while it restarts, or a dropped connection. The desktop
//! keeps trying on its own and keeps showing what it has (the chat list,
//! the open chat, cached rows) for [`GRACE`], counted once on the monotonic
//! clock from the first failure, however many follow. Only after that does
//! it say the Host is out of reach, in the calm words the list already has
//! for it (`reconnecting`, rows `offline`); the first answer after it ends
//! that at once. A refusal (401, 403) or another HTTP error is not an
//! outage: it is said straight away, as before.

use std::time::{Duration, Instant};

/// How long failures stay quiet, from the first one.
pub const GRACE: Duration = Duration::from_secs(15);

/// [`GRACE`], or `CLARP_OUTAGE_GRACE_MS` where a headless check shortens it.
pub fn grace() -> Duration {
    static GRACE_SET: std::sync::OnceLock<Duration> = std::sync::OnceLock::new();
    *GRACE_SET.get_or_init(|| {
        std::env::var("CLARP_OUTAGE_GRACE_MS").ok().and_then(|ms| ms.trim().parse().ok()).map_or(GRACE, Duration::from_millis)
    })
}

/// A failure that is the way to the Host and not the Host's answer: no
/// HTTP status (the connection failed or dropped), or a gateway saying the
/// Host behind it does not answer (502, 503, 504).
pub fn transient(status: u16) -> bool {
    matches!(status, 0 | 502 | 503 | 504)
}

/// How a recovered outage went, for the log line that measures it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Recovery {
    /// From the first failure to the answer that ended it.
    pub after: Duration,
    /// The out-of-reach treatment had been shown.
    pub shown: bool,
    pub failures: u32,
}

/// One Host's outage, if it has one.
#[derive(Debug, Clone, Default)]
pub struct Outage {
    /// The grace this outage waits; [`grace`] when zero.
    grace: Duration,
    since: Option<Instant>,
    shown: bool,
    failures: u32,
    detail: String,
}

impl Outage {
    /// A transient failure at `now`. True when it is the first: the grace
    /// starts now and does not start again for the failures after it.
    pub fn failed(&mut self, now: Instant, detail: &str) -> bool {
        self.failures += 1;
        if !detail.is_empty() {
            self.detail = detail.to_owned();
        }
        if self.since.is_some() {
            return false;
        }
        self.since = Some(now);
        true
    }

    pub fn active(&self) -> bool {
        self.since.is_some()
    }

    /// Whether the out-of-reach treatment shows (the grace ran out).
    pub fn shown(&self) -> bool {
        self.shown
    }

    /// The last failure's words, for the log and the connection page.
    pub fn detail(&self) -> &str {
        &self.detail
    }

    /// How long since the first failure; zero without an outage.
    pub fn elapsed(&self, now: Instant) -> Duration {
        self.since.map_or(Duration::ZERO, |since| now.saturating_duration_since(since))
    }

    /// An outage that waits `grace` instead of [`grace`].
    pub fn with_grace(grace: Duration) -> Self {
        Self { grace, ..Self::default() }
    }

    pub fn grace(&self) -> Duration {
        if self.grace.is_zero() { grace() } else { self.grace }
    }

    /// The time left of the grace; None without an outage.
    pub fn grace_left(&self, now: Instant) -> Option<Duration> {
        self.since.map(|_| self.grace().saturating_sub(self.elapsed(now)))
    }

    /// True once, when the grace has run out at `now`: show the outage.
    pub fn grace_over(&mut self, now: Instant) -> bool {
        if self.since.is_none() || self.shown || self.elapsed(now) < self.grace() {
            return false;
        }
        self.shown = true;
        true
    }

    /// The Host answered at `now`: the outage, if any, is over.
    pub fn recovered(&mut self, now: Instant) -> Option<Recovery> {
        let since = self.since.take()?;
        let recovery = Recovery { after: now.saturating_duration_since(since), shown: self.shown, failures: self.failures };
        *self = Self { grace: self.grace, ..Self::default() };
        Some(recovery)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn gateway_and_transport_failures_are_transient_refusals_are_not() {
        for status in [0, 502, 503, 504] {
            assert!(transient(status), "{status}");
        }
        for status in [400, 401, 403, 404, 409, 413, 429, 500] {
            assert!(!transient(status), "{status}");
        }
    }

    #[test]
    fn the_grace_is_counted_once_from_the_first_failure() {
        let start = Instant::now();
        let mut outage = Outage::with_grace(GRACE);
        assert!(!outage.active());
        assert_eq!(outage.grace_left(start), None);
        assert!(outage.failed(start, "HTTP 502"));
        assert!(!outage.failed(start + Duration::from_secs(5), "closed"), "a later failure does not restart it");
        assert!(!outage.failed(start + Duration::from_secs(14), "HTTP 502"));
        assert_eq!(outage.grace_left(start + Duration::from_secs(14)), Some(Duration::from_secs(1)));
        assert!(!outage.grace_over(start + Duration::from_millis(14_999)));
        assert!(outage.grace_over(start + GRACE));
        assert!(!outage.grace_over(start + Duration::from_secs(20)), "shown once");
        assert!(outage.shown());
        assert_eq!(outage.detail(), "HTTP 502");
        let recovery = outage.recovered(start + Duration::from_secs(45)).unwrap();
        assert_eq!(recovery, Recovery { after: Duration::from_secs(45), shown: true, failures: 3 });
        assert!(!outage.active() && !outage.shown());
        assert_eq!(outage.recovered(start + Duration::from_secs(46)), None, "nothing to recover from");
    }

    #[test]
    fn a_short_outage_recovers_without_being_shown() {
        let start = Instant::now();
        let mut outage = Outage::with_grace(GRACE);
        outage.failed(start, "closed");
        let recovery = outage.recovered(start + Duration::from_secs(3)).unwrap();
        assert!(!recovery.shown);
        assert!(outage.failed(start + Duration::from_secs(60), "again"), "a new outage starts its own grace");
        assert_eq!(outage.grace_left(start + Duration::from_secs(60)), Some(GRACE));
    }
}
