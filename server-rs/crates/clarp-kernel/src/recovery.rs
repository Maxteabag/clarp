//! What follows an attempt of a turn that failed.
//!
//! A turn is tried more than once when the failure is one that passes. A connection that
//! dropped or a tool that stalled passes by itself: the turn waits a moment (the Python Host's
//! rule, `turn_retry._handle_failure`: three attempts in all, a second after the first, two
//! after the second). An account that is out passes when another account is put in its place,
//! or when its limit is over: the turn waits for that, when this Host is told to. Everything
//! else ends the turn, and the user is told why (`failures::told`).

use crate::failures;

/// How many attempts a turn gets against failures that pass by themselves, the first included.
pub const MAX_ATTEMPTS: u32 = 3;

/// How long the turn waits after its first attempt. Twice as long after each one more.
pub const BACKOFF_BASE_MS: i64 = 1_000;

/// How long a turn that waits for an account waits before it is tried on whatever account
/// is there then: this long the first time, and twice as long each time after.
pub const ACCOUNT_RECHECK_MS: i64 = 60_000;

/// The longest a turn waits between two tries for an account.
pub const ACCOUNT_RECHECK_MOST_MS: i64 = 480_000;

/// How long a turn waits for an account in all. A turn that is left waiting looks like an
/// agent that stopped answering, with nothing for the user to act on: after this long it
/// ends, and says that the account is out. (The Python Host's `MAX_PARK_SECONDS`, and its
/// reason: on 17 Sep 2026 a selector that could not reach its usage API held every agent of
/// a Host for three and a half hours.)
pub const ACCOUNT_WAIT_MOST_MS: i64 = 1_800_000;

/// Whether this Host waits for an account when a turn fails on its own.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum Accounts {
    /// It does not: the turn ends and says why. A Host whose tools have one login, and
    /// nobody to put another in its place.
    #[default]
    None,
    /// Someone else on the machine puts another login in place when an account is out: a
    /// Host beside this one that runs the owner's selector. The turn waits, and is tried
    /// again when the login has changed, and now and then meanwhile. This Host never changes
    /// a login itself, and never stops a turn because another turn's account is out.
    Follow,
}

impl Accounts {
    /// The mode with this name in configuration.
    pub fn by_name(name: &str) -> Option<Self> {
        match name {
            "none" => Some(Self::None),
            "follow" => Some(Self::Follow),
            _ => None,
        }
    }

    pub fn name(self) -> &'static str {
        match self {
            Self::None => "none",
            Self::Follow => "follow",
        }
    }
}

/// An attempt of a turn that failed.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Failed<'a> {
    /// What kind of failure it was (`failures::classify_error`).
    pub kind: &'a str,
    /// Which attempt of the turn it was, from 1.
    pub attempt: u32,
    /// How many of the turn's waits so far were for an account.
    pub account_waits: u32,
    /// Since when the turn has waited for an account, when it has before.
    pub account_wait_since_ms: Option<i64>,
    pub now_ms: i64,
}

/// What follows a failed attempt.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Next {
    /// The same again, after so long.
    Retry { after_ms: i64 },
    /// The turn waits for an account, and is tried at this time at the latest.
    Account { until_ms: i64 },
    /// The turn ends on this failure.
    Fail,
}

/// What follows the failure of an attempt of a turn, on a Host that treats accounts this way.
pub fn after_failed_attempt(failed: Failed<'_>, accounts: Accounts) -> Next {
    // Waits for an account are no tries against a bad line: they do not use those up.
    let tries = failed.attempt.saturating_sub(failed.account_waits);
    match failed.kind {
        failures::CONNECTION | failures::TIMEOUT if (1..MAX_ATTEMPTS).contains(&tries) => {
            Next::Retry {
                after_ms: BACKOFF_BASE_MS << (tries - 1),
            }
        }
        failures::USAGE_LIMIT | failures::AUTH if accounts == Accounts::Follow => {
            let since = failed.account_wait_since_ms.unwrap_or(failed.now_ms);
            let left = since + ACCOUNT_WAIT_MOST_MS - failed.now_ms;
            if left <= 0 {
                return Next::Fail;
            }
            let step =
                (ACCOUNT_RECHECK_MS << failed.account_waits.min(8)).min(ACCOUNT_RECHECK_MOST_MS);
            Next::Account {
                until_ms: failed.now_ms + step.min(left),
            }
        }
        _ => Next::Fail,
    }
}

/// What the tool is told when a turn is taken up again after it waited for an account: the
/// Python Host's words (`turn_launcher._resume_after_account_switch`), in the envelope its
/// transcript reader knows. `relapsed`: the turn has been told once already, and the account
/// ran out again.
pub fn resume_prompt(backend: &str, request: &str, relapsed: bool) -> String {
    let told = if relapsed {
        "The account ran out again and Clarp has resumed this turn once more. Carry on from \
         where the conversation stands; check the results of interrupted operations before \
         retrying them. Nothing new is asked."
            .to_string()
    } else {
        format!(
            "Clarp recovered from a {backend} account usage limit. Continue the unfinished \
             request from the existing conversation. Check the results of interrupted \
             operations before retrying them; do not repeat work or external actions already \
             completed. If the request is already complete, report that and stop.\n\n\
             Original request:\n{request}"
        )
    };
    format!("<clarp-account-recovery>\n{told}\n</clarp-account-recovery>")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn failed(kind: &str, attempt: u32) -> Failed<'_> {
        Failed {
            kind,
            attempt,
            account_waits: 0,
            account_wait_since_ms: None,
            now_ms: 10_000,
        }
    }

    #[test]
    fn a_failure_that_passes_is_tried_three_times_in_all() {
        for accounts in [Accounts::None, Accounts::Follow] {
            for kind in [failures::CONNECTION, failures::TIMEOUT] {
                assert_eq!(
                    after_failed_attempt(failed(kind, 1), accounts),
                    Next::Retry { after_ms: 1_000 }
                );
                assert_eq!(
                    after_failed_attempt(failed(kind, 2), accounts),
                    Next::Retry { after_ms: 2_000 }
                );
                assert_eq!(after_failed_attempt(failed(kind, 3), accounts), Next::Fail);
                assert_eq!(after_failed_attempt(failed(kind, 4), accounts), Next::Fail);
                // An attempt that has no number was none.
                assert_eq!(after_failed_attempt(failed(kind, 0), accounts), Next::Fail);
            }
        }
    }

    #[test]
    fn every_other_failure_ends_the_turn_on_a_host_that_waits_for_no_account() {
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
            assert_eq!(
                after_failed_attempt(failed(kind, 1), Accounts::None),
                Next::Fail,
                "{kind}"
            );
        }
        // And where one is waited for, a plan without the model still ends the turn: no
        // wait puts a model into a plan.
        for kind in [
            failures::ACCOUNT_PLAN,
            failures::TRANSIENT,
            failures::RUNNER_EXIT,
        ] {
            assert_eq!(
                after_failed_attempt(failed(kind, 1), Accounts::Follow),
                Next::Fail,
                "{kind}"
            );
        }
    }

    #[test]
    fn a_turn_whose_account_is_out_waits_longer_each_time_and_half_an_hour_in_all() {
        let mut now = 10_000;
        let (since, mut waits, mut tried) = (now, 0, Vec::new());
        loop {
            let next = after_failed_attempt(
                Failed {
                    kind: failures::USAGE_LIMIT,
                    attempt: waits + 1,
                    account_waits: waits,
                    account_wait_since_ms: (waits > 0).then_some(since),
                    now_ms: now,
                },
                Accounts::Follow,
            );
            let Next::Account { until_ms } = next else {
                assert_eq!(next, Next::Fail);
                break;
            };
            tried.push(until_ms - now);
            // The account is still out when the turn is tried.
            now = until_ms;
            waits += 1;
        }
        assert_eq!(
            tried,
            vec![60_000, 120_000, 240_000, 480_000, 480_000, 420_000],
            "a minute, then twice as long each time, eight minutes at most, and no longer \
             than half an hour in all"
        );
        assert_eq!(now - since, ACCOUNT_WAIT_MOST_MS);
        // A sign-in that ran out is waited for in the same way.
        assert_eq!(
            after_failed_attempt(failed(failures::AUTH, 1), Accounts::Follow),
            Next::Account { until_ms: 70_000 }
        );
    }

    #[test]
    fn waits_for_an_account_use_up_no_tries_against_a_bad_line() {
        // The third attempt of a turn that waited twice for an account is its first try
        // against this failure.
        let after_two_waits = Failed {
            kind: failures::CONNECTION,
            attempt: 3,
            account_waits: 2,
            account_wait_since_ms: Some(1_000),
            now_ms: 500_000,
        };
        assert_eq!(
            after_failed_attempt(after_two_waits, Accounts::Follow),
            Next::Retry { after_ms: 1_000 }
        );
    }

    #[test]
    fn a_turn_that_is_taken_up_again_is_told_so_in_the_python_hosts_words() {
        let first = resume_prompt("claude", "count the files", false);
        assert_eq!(
            first,
            "<clarp-account-recovery>\nClarp recovered from a claude account usage limit. \
             Continue the unfinished request from the existing conversation. Check the results \
             of interrupted operations before retrying them; do not repeat work or external \
             actions already completed. If the request is already complete, report that and \
             stop.\n\nOriginal request:\ncount the files\n</clarp-account-recovery>"
        );
        let again = resume_prompt("claude", "count the files", true);
        assert_eq!(
            again,
            "<clarp-account-recovery>\nThe account ran out again and Clarp has resumed this \
             turn once more. Carry on from where the conversation stands; check the results of \
             interrupted operations before retrying them. Nothing new is asked.\n\
             </clarp-account-recovery>"
        );
        assert_eq!(Accounts::by_name("follow"), Some(Accounts::Follow));
        assert_eq!(
            Accounts::by_name(Accounts::None.name()),
            Some(Accounts::None)
        );
        assert_eq!(Accounts::by_name("lead"), None);
    }
}
