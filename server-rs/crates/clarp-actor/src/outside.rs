//! How an attempt ends that a signal from outside the Host reached (issue 829).
//!
//! The Host stops a turn through its shim's socket, never by a signal, so a signal that reached
//! the shim came from outside (an operator's `kill`, a service manager), and the shim's spool
//! says which. What the tool writes as it is ended says nothing of that: an error with no
//! words, a bad exit, an interruption. Such an end is told as ended from outside. A failure
//! the Host has handling of its own for (a usage limit, a sign-in, a dropped connection) wins
//! over it, so the account switch or the retry still happens. A completed attempt, and one
//! the Host was asked to stop, end as they did.

use clarp_kernel::failures;
use clarp_kernel::turn::RunOutcome;

/// The outcome of an attempt as the turn settles it. `outside`: the signal the shim's spool
/// says came from outside, if one did; `stopping`: the Host was asked to stop the turn.
pub(crate) fn settled(outcome: RunOutcome, outside: Option<&str>, stopping: bool) -> RunOutcome {
    let Some(signal) = outside.filter(|_| !stopping) else {
        return outcome;
    };
    let generic = match &outcome {
        RunOutcome::Failed { reason } => {
            failures::yields_to_outside(failures::classify_error(reason))
        }
        RunOutcome::Interrupted { .. } => true,
        RunOutcome::Completed | RunOutcome::Stopped { .. } => false,
    };
    if generic {
        RunOutcome::Failed {
            reason: failures::ended_from_outside(signal),
        }
    } else {
        outcome
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn failed(reason: &str) -> RunOutcome {
        RunOutcome::Failed {
            reason: reason.into(),
        }
    }

    fn outside() -> RunOutcome {
        failed(&failures::ended_from_outside("SIGTERM"))
    }

    #[test]
    fn what_the_tool_wrote_as_it_was_ended_is_told_as_ended_from_outside() {
        for said in ["claude reported an error", "claude exited with code 1", ""] {
            assert_eq!(settled(failed(said), Some("SIGTERM"), false), outside());
        }
        let cut = RunOutcome::Interrupted {
            reason: "claude was ended by signal 15".into(),
        };
        assert_eq!(settled(cut, Some("SIGTERM"), false), outside());
    }

    #[test]
    fn a_failure_with_handling_of_its_own_wins_over_the_outside_signal() {
        // The race: the signal came, and the tool's last word was a usage limit or a sign-in
        // that ran out. The turn keeps that kind, so it waits for another account.
        for (said, kind) in [
            ("Claude AI usage limit reached", failures::USAGE_LIMIT),
            ("Not logged in · Please run /login", failures::AUTH),
            ("API Error: Connection error.", failures::CONNECTION),
        ] {
            assert_eq!(failures::classify_error(said), kind, "{said}");
            assert_eq!(settled(failed(said), Some("SIGTERM"), false), failed(said));
        }
    }

    #[test]
    fn a_completed_attempt_a_stop_and_no_signal_end_as_they_did() {
        assert_eq!(
            settled(RunOutcome::Completed, Some("SIGTERM"), false),
            RunOutcome::Completed
        );
        let said = failed("claude reported an error");
        assert_eq!(settled(said.clone(), Some("SIGTERM"), true), said);
        assert_eq!(settled(said.clone(), None, false), said);
    }
}
