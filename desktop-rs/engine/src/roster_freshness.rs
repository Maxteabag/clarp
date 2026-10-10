//! How current the roster is. `/agents/snapshot` is the only way to learn
//! about agents the event stream did not announce, and on a busy Host it is
//! about a megabyte that can take many seconds. When it fails the roster
//! already shown is kept, the engine retries on its own (`retry_delay`), and
//! the explorer and the switcher say the list is stale instead of a banner.

use std::time::{Duration, SystemTime};

/// Waits between a failed snapshot and the next try; the last repeats.
pub const RETRY_SCHEDULE: [Duration; 5] =
    [Duration::from_secs(2), Duration::from_secs(5), Duration::from_secs(15), Duration::from_secs(30), Duration::from_secs(60)];

/// A snapshot that has not answered by then has failed. Long enough for a
/// megabyte from a slow Host, short enough that a hung one is retried.
pub const SNAPSHOT_TIMEOUT: Duration = Duration::from_secs(60);

/// The wait before the retry that follows the `failures`th failure in a row
/// (1 for the first).
pub fn retry_delay(failures: u32) -> Duration {
    let index = (failures.max(1) as usize - 1).min(RETRY_SCHEDULE.len() - 1);
    RETRY_SCHEDULE[index]
}

/// `CLARP_SNAPSHOT_TIMEOUT_MS`, else [`SNAPSHOT_TIMEOUT`].
pub fn snapshot_timeout() -> Duration {
    match std::env::var("CLARP_SNAPSHOT_TIMEOUT_MS") {
        Ok(value) if !value.is_empty() => match value.parse::<u64>() {
            Ok(ms) if ms > 0 => Duration::from_millis(ms),
            _ => {
                eprintln!("Engine: ignoring CLARP_SNAPSHOT_TIMEOUT_MS={value:?}: not a positive number of milliseconds");
                SNAPSHOT_TIMEOUT
            }
        },
        _ => SNAPSHOT_TIMEOUT,
    }
}

/// What the explorer and the switcher may say about the roster.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum State {
    /// The last snapshot arrived and none is being fetched.
    Fresh,
    /// A snapshot is on its way and the last one did not fail.
    Refreshing,
    /// The last `failures` snapshots in a row failed: the roster is the one
    /// from `since` (None: no snapshot has arrived yet).
    Stale { since: Option<SystemTime>, reason: String, failures: u32 },
}

#[derive(Debug, Clone, Default)]
pub struct Freshness {
    loaded_at: Option<SystemTime>,
    in_flight: bool,
    failures: u32,
    reason: String,
}

impl Freshness {
    pub fn started(&mut self) {
        self.in_flight = true;
    }

    pub fn succeeded(&mut self, now: SystemTime) {
        self.loaded_at = Some(now);
        self.in_flight = false;
        self.failures = 0;
        self.reason.clear();
    }

    /// Counts a failure and returns how many in a row there have been.
    pub fn failed(&mut self, reason: &str) -> u32 {
        self.in_flight = false;
        self.failures += 1;
        self.reason = reason.to_owned();
        self.failures
    }

    /// A request was dropped (a reconnect): what it would have said is lost.
    pub fn abandoned(&mut self) {
        self.in_flight = false;
    }

    pub fn failures(&self) -> u32 {
        self.failures
    }

    pub fn state(&self) -> State {
        if self.failures > 0 {
            State::Stale { since: self.loaded_at, reason: self.reason.clone(), failures: self.failures }
        } else if self.in_flight {
            State::Refreshing
        } else {
            State::Fresh
        }
    }
}

impl State {
    pub fn is_stale(&self) -> bool {
        matches!(self, State::Stale { .. })
    }

    /// One short line for the explorer and the switcher ("Agent list from
    /// 14:02 · retrying"); empty unless stale. `clock` writes a time.
    pub fn note(&self, clock: impl Fn(SystemTime) -> String) -> String {
        match self {
            State::Stale { since: Some(since), .. } => format!("Agent list from {} · retrying", clock(*since)),
            State::Stale { since: None, .. } => "Agent list not loaded yet · retrying".to_owned(),
            _ => String::new(),
        }
    }

    /// Why, for a tooltip or a screen reader: the last failure and the count.
    pub fn detail(&self) -> String {
        match self {
            State::Stale { reason, failures, .. } => {
                let times = if *failures == 1 { "once".to_owned() } else { format!("{failures} times") };
                format!("Refreshing the agent list failed {times}: {reason}")
            }
            _ => String::new(),
        }
    }
}

/// `since` as the local wall clock's hours and minutes.
pub fn local_clock(since: SystemTime) -> String {
    chrono::DateTime::<chrono::Local>::from(since).format("%H:%M").to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn retries_back_off_then_repeat_every_minute() {
        let secs: Vec<u64> = (1..=8).map(|n| retry_delay(n).as_secs()).collect();
        assert_eq!(secs, [2, 5, 15, 30, 60, 60, 60, 60]);
        assert_eq!(retry_delay(0), Duration::from_secs(2), "no failure yet counts as the first");
    }

    #[test]
    fn fresh_refreshing_stale_and_fresh_again() {
        let mut f = Freshness::default();
        assert_eq!(f.state(), State::Fresh);
        f.started();
        assert_eq!(f.state(), State::Refreshing);
        let at = SystemTime::UNIX_EPOCH + Duration::from_secs(1_000);
        f.succeeded(at);
        assert_eq!(f.state(), State::Fresh);
        f.started();
        assert_eq!(f.failed("operation timed out"), 1);
        assert_eq!(f.state(), State::Stale { since: Some(at), reason: "operation timed out".into(), failures: 1 });
        // A retry on its way is still stale: the list shown is the old one.
        f.started();
        assert!(f.state().is_stale());
        assert_eq!(f.failed("Gateway Timeout (HTTP 504)"), 2);
        assert_eq!(f.state().detail(), "Refreshing the agent list failed 2 times: Gateway Timeout (HTTP 504)");
        f.started();
        f.succeeded(at + Duration::from_secs(60));
        assert_eq!(f.state(), State::Fresh);
        assert_eq!(f.failures(), 0);
        assert_eq!(f.state().note(|_| "x".into()), "");
    }

    #[test]
    fn the_note_names_when_the_list_is_from() {
        let at = SystemTime::UNIX_EPOCH;
        let stale = State::Stale { since: Some(at), reason: "x".into(), failures: 1 };
        assert_eq!(stale.note(|_| "14:02".into()), "Agent list from 14:02 · retrying");
        assert_eq!(stale.detail(), "Refreshing the agent list failed once: x");
        let never = State::Stale { since: None, reason: "x".into(), failures: 3 };
        assert_eq!(never.note(|_| unreachable!()), "Agent list not loaded yet · retrying");
        assert_eq!(State::Refreshing.note(|_| unreachable!()), "");
    }

    #[test]
    fn an_abandoned_request_is_not_refreshing() {
        let mut f = Freshness::default();
        f.started();
        f.abandoned();
        assert_eq!(f.state(), State::Fresh);
    }
}
