//! What follows an attempt of a turn that failed.
//!
//! A turn is tried more than once when the failure is one that passes: a connection that
//! dropped, a tool that stalled. Everything else ends the turn, and the user is told why
//! (`failures::told`). The Python Host's rule (`turn_retry._handle_failure`): three attempts
//! in all, a second after the first, two after the second.

use crate::failures;

/// How many attempts a turn gets, the first included.
pub const MAX_ATTEMPTS: u32 = 3;

/// How long the turn waits after its first attempt. Twice as long after each one more.
pub const BACKOFF_BASE_MS: i64 = 1_000;

/// What follows a failed attempt.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Next {
    /// The same again, after so long.
    Retry { after_ms: i64 },
    /// The turn ends on this failure.
    Fail,
}

/// What follows the failure of a turn's attempt number `attempt` (from 1), for a failure of
/// this kind (`failures::classify_error`).
pub fn after_failed_attempt(kind: &str, attempt: u32) -> Next {
    let passes = matches!(kind, failures::CONNECTION | failures::TIMEOUT);
    if passes && (1..MAX_ATTEMPTS).contains(&attempt) {
        Next::Retry {
            after_ms: BACKOFF_BASE_MS << (attempt - 1),
        }
    } else {
        Next::Fail
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_failure_that_passes_is_tried_three_times_in_all() {
        for kind in [failures::CONNECTION, failures::TIMEOUT] {
            assert_eq!(
                after_failed_attempt(kind, 1),
                Next::Retry { after_ms: 1_000 }
            );
            assert_eq!(
                after_failed_attempt(kind, 2),
                Next::Retry { after_ms: 2_000 }
            );
            assert_eq!(after_failed_attempt(kind, 3), Next::Fail);
            assert_eq!(after_failed_attempt(kind, 4), Next::Fail);
            // An attempt that has no number was none.
            assert_eq!(after_failed_attempt(kind, 0), Next::Fail);
        }
    }

    #[test]
    fn every_other_failure_ends_the_turn() {
        for kind in [
            failures::TRANSIENT,
            failures::INTERRUPTED,
            failures::USAGE_LIMIT,
            failures::AUTH,
            failures::ACCOUNT_PLAN,
            failures::RUNNER_EXIT,
            failures::NOT_STARTED,
            failures::UNKNOWN,
            failures::CLEAN,
            "a kind nobody has heard of",
        ] {
            assert_eq!(after_failed_attempt(kind, 1), Next::Fail, "{kind}");
        }
    }
}
