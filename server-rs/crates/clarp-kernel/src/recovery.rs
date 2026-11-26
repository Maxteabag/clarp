//! What follows an attempt of a turn that failed.
//!
//! A turn is tried more than once when the failure is one that passes. A connection that
//! dropped or a tool that stalled passes by itself, but not always within seconds: a laptop
//! that changes networks has none for half a minute or more. So the Host first checks whether
//! the machine reaches the provider at all ([`after_network_check`]). Where it does not, the
//! turn waits for the network, checked every few seconds, a quarter of an hour at most, and is
//! tried again when it is back. Where it does, the turn is tried again after 2, 4, 8, 16 and 30
//! seconds, a minute in all; one that timed out, three times in all, as before. A call's turn keeps the Python Host's shorter rule
//! (`turn_retry._handle_failure`: three attempts in all, a second after the first, two after
//! the second): someone listens. A provider that turned an attempt away for a moment
//! (overloaded, or holding requests back) passes too, more slowly: the turn waits half a
//! minute, then a minute, on the same account. Either way, one that has done nothing yet is
//! asked the same again; one
//! that had begun its work is told that it is taken up again and goes on from where it
//! stands ([`continue_prompt`]). A call is not kept waiting so long: its turn ends at once.
//! An account that is out passes when another account is put in its place, or when its limit
//! is over: the turn waits for that, when this Host is told to. Where the tool said when the
//! account has usage again, the turn waits until then (and a minute more) and is taken up by
//! itself; where it did not, it is tried now and then, for half an hour at most. Everything
//! else ends the turn, and the user is told why (`failures::told`).

use crate::failures;

/// How many attempts a call's turn gets against failures that pass by themselves, the first
/// included.
pub const MAX_ATTEMPTS: u32 = 3;

/// How long a call's turn waits after its first attempt. Twice as long after each one more.
pub const BACKOFF_BASE_MS: i64 = 1_000;

/// How often the Host checks whether a turn that waits for the network has one again.
pub const NETWORK_RECHECK_MS: i64 = 5_000;

/// How long a turn waits for the network in all. A laptop that changes networks has none for
/// half a minute or more; one that has none for a quarter of an hour is not about to, and the
/// user is told (`failures::network_gone`).
pub const NETWORK_WAIT_MOST_MS: i64 = 900_000;

/// How long a turn waits after its first attempt that failed on its connection while the
/// machine had a network. Twice as long after each one more, [`LINE_BACKOFF_MOST_MS`] at most.
pub const LINE_BACKOFF_MS: i64 = 2_000;

/// The longest a turn waits between two such attempts.
pub const LINE_BACKOFF_MOST_MS: i64 = 30_000;

/// How many times a turn is tried again on a connection that fails while the machine has a
/// network: after 2, 4, 8, 16 and 30 seconds, a minute in all.
pub const LINE_RETRIES: u32 = 5;

/// How long the Host waits for its check of the network before it asks again: a check
/// that was lost (its thread gone with the Host's) never holds a turn.
pub const NETWORK_ANSWER_MS: i64 = 30_000;

/// How long a turn waits after a first attempt its provider turned away for a moment. Twice
/// as long after each one more. The tool has tried again by itself before it gave up, and a
/// provider that holds requests back counts them by the minute.
pub const PROVIDER_BACKOFF_MS: i64 = 30_000;

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

/// How long after the time the tool said its account has usage again the turn is tried: the
/// provider's clock and this Host's are not one, and a turn tried a moment early is refused
/// again.
pub const RESET_MARGIN_MS: i64 = 60_000;

/// The furthest reset a turn waits for. A window is a week long at most: a time later than
/// this was not said of a window, and a turn does not wait for it.
pub const RESET_FURTHEST_MS: i64 = 8 * 24 * 3_600_000;

/// When a turn whose account ran out is tried again, where the tool said when the account has
/// usage again: then, and a minute more. `None` where it said nothing, or a time that is no
/// reset to wait for (one that is over, or one further than a week away).
pub fn after_reset(resets_at_ms: Option<i64>, now_ms: i64) -> Option<i64> {
    let at = resets_at_ms?.checked_add(RESET_MARGIN_MS)?;
    (now_ms < at && at - now_ms <= RESET_FURTHEST_MS).then_some(at)
}

/// The latest a turn that waits in the pool of its tool's accounts waits on its own: half an
/// hour, or until its account has usage again where that is later. The pool takes it up or
/// lets it go before then.
pub fn pool_wait_until(resets_at_ms: Option<i64>, now_ms: i64) -> i64 {
    let half_hour = now_ms + ACCOUNT_WAIT_MOST_MS;
    after_reset(resets_at_ms, now_ms).map_or(half_hour, |reset| reset.max(half_hour))
}

/// Whether this Host waits for an account when a turn fails on its own.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum Accounts {
    /// It does not: the turn ends and says why. A Host whose tools have one login, and
    /// nobody to put another in its place. Also where the tool said when the account has
    /// usage again: such a Host does not follow the login, so a wait of hours would outlast
    /// a sign-in to another account, and the agent's next messages would queue behind it. The
    /// user is told when it resets (`failures::told`) and sends again.
    #[default]
    None,
    /// Someone else on the machine puts another login in place when an account is out: a
    /// Host beside this one that runs the owner's selector. The turn waits, and is tried
    /// again when the login has changed, and now and then meanwhile; or, where the tool said
    /// when the account has usage again, then and not before. This Host never changes
    /// a login itself, and never stops a turn because another turn's account is out.
    Follow,
    /// This Host runs the owner's selector itself: it is alone on its machine, and nobody
    /// else puts another login in place. What follows a failure on an account is said by
    /// the pool of the tool's accounts (`accounts::Pool`), which stops the tool's running
    /// turns before the selector changes the login and takes them up after.
    Select,
}

impl Accounts {
    /// The mode with this name in configuration.
    pub fn by_name(name: &str) -> Option<Self> {
        match name {
            "none" => Some(Self::None),
            "follow" => Some(Self::Follow),
            "select" => Some(Self::Select),
            _ => None,
        }
    }

    pub fn name(self) -> &'static str {
        match self {
            Self::None => "none",
            Self::Follow => "follow",
            Self::Select => "select",
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
    /// How many of the turn's attempts followed a wait in which the network was gone.
    pub offline: u32,
    /// Since when the turn has waited for an account, when it has before.
    pub account_wait_since_ms: Option<i64>,
    /// The turn has put nothing into the conversation and begun no tool, in any attempt:
    /// asking the same again repeats nothing of it.
    pub nothing_done: bool,
    /// The tool's words say that its provider turned the attempt away for a moment
    /// (`failures::provider_busy`): a failure of the transient kind that is worth a wait.
    pub busy: bool,
    /// The turn is a call's: someone listens, and is not kept in silence for a minute.
    pub in_call: bool,
    /// When the account has usage again, where the tool said so with its refusal.
    pub resets_at_ms: Option<i64>,
    pub now_ms: i64,
}

/// What follows a failed attempt.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Next {
    /// The same again, after so long. `resume`: the turn had begun its work, and the next
    /// attempt goes on from where it stands instead of being given the message anew.
    Retry { after_ms: i64, resume: bool },
    /// The turn waits for an account, and is tried at this time at the latest.
    Account { until_ms: i64 },
    /// The turn waits for the machine's network, which the Host checks now: it ends at
    /// `until_ms` if the network is gone and does not come back by then. `resume` as for
    /// [`Next::Retry`].
    Network { until_ms: i64, resume: bool },
    /// The network is gone: the Host checks it again at this time.
    Offline { check_at_ms: i64 },
    /// The network did not come back in time: the turn ends and says so.
    NetworkGone,
    /// The turn failed on its account, on a Host that runs the selector itself: the pool of
    /// the tool's accounts says whether it waits or ends. `plan`: the provider said that the
    /// account does not offer the turn's model, which is a fact about one turn and not about
    /// the account's limits.
    Pool { plan: bool },
    /// The turn ends on this failure.
    Fail,
}

/// What follows the failure of an attempt of a turn, on a Host that treats accounts this way.
pub fn after_failed_attempt(failed: Failed<'_>, accounts: Accounts) -> Next {
    // Waits for an account are no tries against a bad line: they do not use those up. Nor
    // are attempts that followed a wait in which the network was gone.
    let tries = failed
        .attempt
        .saturating_sub(failed.account_waits)
        .saturating_sub(failed.offline);
    match failed.kind {
        // Someone listens to a call: it is not kept waiting for minutes. Its next attempt is
        // given the message anew (`resume: false`), as before the network wait, also where
        // the turn had begun its work: a call's turn rarely runs a tool.
        failures::CONNECTION | failures::TIMEOUT
            if failed.in_call && (1..MAX_ATTEMPTS).contains(&tries) =>
        {
            Next::Retry {
                after_ms: BACKOFF_BASE_MS << (tries - 1),
                resume: false,
            }
        }
        // Whether the machine has a network says what follows: the Host checks it, and
        // `after_network_check` says. A turn that had begun its work goes on from where it
        // stands when it is tried again.
        failures::CONNECTION | failures::TIMEOUT if !failed.in_call && failed.attempt >= 1 => {
            Next::Network {
                until_ms: failed.now_ms + NETWORK_WAIT_MOST_MS,
                resume: !failed.nothing_done,
            }
        }
        // No other account helps with a provider that is busy. A turn that had begun its
        // work is not asked the same again, which could do it twice: it is told to go on.
        failures::TRANSIENT
            if failed.busy && !failed.in_call && (1..MAX_ATTEMPTS).contains(&tries) =>
        {
            Next::Retry {
                after_ms: PROVIDER_BACKOFF_MS << (tries - 1),
                resume: !failed.nothing_done,
            }
        }
        // Also a plan that does not offer the model: another account's plan may, and the
        // pool knows when asking for one would stop turns that run fine.
        failures::USAGE_LIMIT | failures::AUTH | failures::ACCOUNT_PLAN
            if accounts == Accounts::Select =>
        {
            Next::Pool {
                plan: failed.kind == failures::ACCOUNT_PLAN,
            }
        }
        failures::USAGE_LIMIT | failures::AUTH if accounts == Accounts::Follow => {
            // The tool said when the account has usage again: the turn is not tried before,
            // unless the login changes, and nothing but a Stop ends the wait, however long
            // the turn has waited already.
            if failed.kind == failures::USAGE_LIMIT
                && let Some(until_ms) = after_reset(failed.resets_at_ms, failed.now_ms)
            {
                return Next::Account { until_ms };
            }
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
        // Everything else ends the turn and says why: a conversation too long for its model
        // among it, which no try and no account makes fit.
        _ => Next::Fail,
    }
}

/// What the Host found when it checked the network for a turn that waits for it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Checked {
    /// The machine reaches the provider.
    pub up: bool,
    /// The attempt failed on a timeout (`failures::TIMEOUT`), not on its connection.
    pub timed_out: bool,
    /// An earlier check of this wait found the network gone.
    pub was_gone: bool,
    /// Which attempt of the turn failed, from 1.
    pub attempt: u32,
    /// How many of the turn's waits so far were for an account.
    pub account_waits: u32,
    /// How many of the turn's attempts followed a wait in which the network was gone.
    pub offline: u32,
    /// When the wait ends if the network does not come back.
    pub until_ms: i64,
    /// The turn had begun its work: it goes on from where it stands.
    pub resume: bool,
    pub now_ms: i64,
}

/// What follows a check of the network for a turn that waits for it.
///
/// The network came back after it was gone: the turn is tried again at once, and that try is
/// none against a bad line. It is there and was never gone: the connection failed for another
/// reason, and the turn is tried again after 2, 4, 8, 16 and 30 seconds, then it ends as it
/// failed. A timeout with the network there keeps the three attempts of the Python Host's rule,
/// a second and then two apart: a tool that stalls can take a watchdog's whole window each
/// time, and five of those are no short wait. It is gone: it is checked again in a few seconds, until the wait's end, and then
/// the turn ends and says that the network was gone.
pub fn after_network_check(checked: Checked) -> Next {
    if checked.up && checked.was_gone {
        return Next::Retry {
            after_ms: 0,
            resume: checked.resume,
        };
    }
    if checked.up {
        let tries = checked
            .attempt
            .saturating_sub(checked.account_waits)
            .saturating_sub(checked.offline);
        if checked.timed_out {
            if !(1..MAX_ATTEMPTS).contains(&tries) {
                return Next::Fail;
            }
            return Next::Retry {
                after_ms: BACKOFF_BASE_MS << (tries - 1),
                resume: checked.resume,
            };
        }
        if !(1..=LINE_RETRIES).contains(&tries) {
            return Next::Fail;
        }
        return Next::Retry {
            after_ms: (LINE_BACKOFF_MS << (tries - 1)).min(LINE_BACKOFF_MOST_MS),
            resume: checked.resume,
        };
    }
    if checked.now_ms >= checked.until_ms {
        return Next::NetworkGone;
    }
    Next::Offline {
        check_at_ms: (checked.now_ms + NETWORK_RECHECK_MS).min(checked.until_ms),
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

/// The target sees the same request and the recorded progress from the source backend.
pub fn transit_prompt(request: &str) -> String {
    format!(
        "<clarp-turn-resumed>\nClarp moved this unfinished turn to another backend. Continue \
         from the transferred conversation. Check the results of interrupted operations \
         before retrying them, and do not repeat work or external actions already completed. \
         If the request is complete, report that and stop.\n\nOriginal request:\n{request}\n</clarp-turn-resumed>"
    )
}

/// What the tool is told when a turn that had begun its work is taken up again after its
/// provider turned an attempt away for a moment, or its connection to the provider was lost:
/// it goes on from where the conversation stands, in an envelope its transcript reader
/// knows, as [`resume_prompt`]'s.
pub fn continue_prompt(request: &str) -> String {
    format!(
        "<clarp-turn-resumed>\nThe provider held requests back for a moment, or the connection \
         to it was lost, and this turn stopped part way; Clarp has taken it up again on the \
         same account. Continue the \
         unfinished request from the existing conversation. Check the results of interrupted \
         operations before retrying them; do not repeat work or external actions already \
         completed. If the request is already complete, report that and stop.\n\n\
         Original request:\n{request}\n</clarp-turn-resumed>"
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    fn failed(kind: &str, attempt: u32) -> Failed<'_> {
        Failed {
            kind,
            attempt,
            account_waits: 0,
            offline: 0,
            account_wait_since_ms: None,
            nothing_done: false,
            busy: false,
            in_call: false,
            resets_at_ms: None,
            now_ms: 10_000,
        }
    }

    #[test]
    fn a_busy_provider_is_waited_for_on_the_same_account_and_the_work_goes_on() {
        for accounts in [Accounts::None, Accounts::Follow, Accounts::Select] {
            let busy = |attempt, nothing_done| Failed {
                busy: true,
                nothing_done,
                ..failed(failures::TRANSIENT, attempt)
            };
            for nothing_done in [true, false] {
                // Nothing of the turn happened: asked the same again. Something did: it goes on.
                let resume = !nothing_done;
                assert_eq!(
                    after_failed_attempt(busy(1, nothing_done), accounts),
                    Next::Retry {
                        after_ms: 30_000,
                        resume
                    }
                );
                assert_eq!(
                    after_failed_attempt(busy(2, nothing_done), accounts),
                    Next::Retry {
                        after_ms: 60_000,
                        resume
                    }
                );
                assert_eq!(
                    after_failed_attempt(busy(3, nothing_done), accounts),
                    Next::Fail
                );
            }
            // A call's listener is not kept in silence; and a failure whose words do not
            // say the provider is busy ends as before.
            let calling = Failed {
                in_call: true,
                ..busy(1, true)
            };
            assert_eq!(after_failed_attempt(calling, accounts), Next::Fail);
            assert_eq!(
                after_failed_attempt(failed(failures::TRANSIENT, 1), accounts),
                Next::Fail
            );
        }
    }

    #[test]
    fn a_turn_taken_up_after_a_busy_provider_goes_on_from_where_it_stands() {
        let told = continue_prompt("count the files");
        assert!(told.starts_with("<clarp-turn-resumed>\n"));
        assert!(told.contains("or the connection to it was lost"));
        assert!(told.ends_with("\nOriginal request:\ncount the files\n</clarp-turn-resumed>"));
        assert!(told.contains("do not repeat work or external actions already completed"));
    }

    #[test]
    fn a_calls_failure_that_passes_is_tried_three_times_in_all() {
        for accounts in [Accounts::None, Accounts::Follow, Accounts::Select] {
            for kind in [failures::CONNECTION, failures::TIMEOUT] {
                let calling = |attempt| Failed {
                    in_call: true,
                    ..failed(kind, attempt)
                };
                assert_eq!(
                    after_failed_attempt(calling(1), accounts),
                    Next::Retry {
                        after_ms: 1_000,
                        resume: false
                    }
                );
                assert_eq!(
                    after_failed_attempt(calling(2), accounts),
                    Next::Retry {
                        after_ms: 2_000,
                        resume: false
                    }
                );
                assert_eq!(after_failed_attempt(calling(3), accounts), Next::Fail);
                assert_eq!(after_failed_attempt(calling(4), accounts), Next::Fail);
                // An attempt that has no number was none.
                assert_eq!(after_failed_attempt(calling(0), accounts), Next::Fail);
            }
        }
    }

    #[test]
    fn a_connection_that_fails_makes_the_turn_wait_for_the_network_on_any_host() {
        for accounts in [Accounts::None, Accounts::Follow, Accounts::Select] {
            for kind in [failures::CONNECTION, failures::TIMEOUT] {
                // However often it was tried: whether the network is there says what follows.
                for attempt in [1, 2, 3, 9] {
                    for nothing_done in [true, false] {
                        let failure = Failed {
                            nothing_done,
                            ..failed(kind, attempt)
                        };
                        assert_eq!(
                            after_failed_attempt(failure, accounts),
                            Next::Network {
                                until_ms: 10_000 + NETWORK_WAIT_MOST_MS,
                                resume: !nothing_done
                            },
                            "{kind}, attempt {attempt}"
                        );
                    }
                }
                assert_eq!(after_failed_attempt(failed(kind, 0), accounts), Next::Fail);
            }
        }
    }

    fn checked(up: bool, attempt: u32) -> Checked {
        Checked {
            up,
            timed_out: false,
            was_gone: false,
            attempt,
            account_waits: 0,
            offline: 0,
            until_ms: 10_000 + NETWORK_WAIT_MOST_MS,
            resume: true,
            now_ms: 10_000,
        }
    }

    #[test]
    fn a_turn_whose_network_is_gone_waits_for_it_and_goes_on_when_it_is_back() {
        // Gone: checked again in five seconds.
        assert_eq!(
            after_network_check(checked(false, 1)),
            Next::Offline {
                check_at_ms: 15_000
            }
        );
        // Back after it was gone: tried again at once. A turn that had begun its work goes
        // on from where it stands; one that had done nothing is asked again.
        for resume in [true, false] {
            let back = Checked {
                was_gone: true,
                resume,
                now_ms: 400_000,
                ..checked(true, 1)
            };
            assert_eq!(
                after_network_check(back),
                Next::Retry {
                    after_ms: 0,
                    resume
                }
            );
        }
        // However often the network was gone before: each time it is back the turn goes on.
        let often = Checked {
            was_gone: true,
            offline: 7,
            ..checked(true, 8)
        };
        assert_eq!(
            after_network_check(often),
            Next::Retry {
                after_ms: 0,
                resume: true
            }
        );
    }

    #[test]
    fn a_turn_waits_a_quarter_of_an_hour_for_the_network_and_then_says_it_was_gone() {
        assert_eq!(NETWORK_WAIT_MOST_MS, 15 * 60_000);
        let until_ms = 10_000 + NETWORK_WAIT_MOST_MS;
        let (mut now, mut checks) = (10_000, 0);
        loop {
            let next = after_network_check(Checked {
                was_gone: checks > 0,
                now_ms: now,
                ..checked(false, 1)
            });
            match next {
                Next::Offline { check_at_ms } => {
                    assert!(check_at_ms > now && check_at_ms <= until_ms);
                    now = check_at_ms;
                    checks += 1;
                }
                Next::NetworkGone => break,
                other => panic!("{other:?}"),
            }
        }
        assert_eq!(now, until_ms, "it ends at the wait's end, and not before");
        assert_eq!(checks, 180, "every five seconds for a quarter of an hour");
        // The network back at the last moment still lets it go on.
        let back = Checked {
            was_gone: true,
            now_ms: until_ms,
            ..checked(true, 1)
        };
        assert_eq!(
            after_network_check(back),
            Next::Retry {
                after_ms: 0,
                resume: true
            }
        );
    }

    #[test]
    fn a_connection_that_fails_while_the_network_is_there_is_tried_for_a_minute() {
        let mut waits = Vec::new();
        for attempt in 1.. {
            match after_network_check(checked(true, attempt)) {
                Next::Retry { after_ms, resume } => {
                    assert!(resume);
                    waits.push(after_ms);
                }
                Next::Fail => break,
                other => panic!("{other:?}"),
            }
        }
        assert_eq!(waits, vec![2_000, 4_000, 8_000, 16_000, 30_000]);
        assert_eq!(waits.iter().sum::<i64>(), 60_000, "a minute in all");
        // Waits for an account and attempts after the network came back are no tries
        // against a bad line.
        let later = Checked {
            account_waits: 2,
            offline: 3,
            ..checked(true, 6)
        };
        assert_eq!(
            after_network_check(later),
            Next::Retry {
                after_ms: 2_000,
                resume: true
            }
        );
        assert_eq!(after_network_check(checked(true, 0)), Next::Fail);
    }

    #[test]
    fn a_timeout_while_the_network_is_there_is_tried_three_times_and_waits_for_it_when_gone() {
        let timeout = |up, attempt| Checked {
            timed_out: true,
            ..checked(up, attempt)
        };
        // There: three attempts in all, a second and then two apart, as before.
        assert_eq!(
            after_network_check(timeout(true, 1)),
            Next::Retry {
                after_ms: 1_000,
                resume: true
            }
        );
        assert_eq!(
            after_network_check(timeout(true, 2)),
            Next::Retry {
                after_ms: 2_000,
                resume: true
            }
        );
        assert_eq!(after_network_check(timeout(true, 3)), Next::Fail);
        // Gone: it waits for the network as a connection does, and goes on when it is back,
        // and that attempt uses none of the three.
        assert_eq!(
            after_network_check(timeout(false, 3)),
            Next::Offline {
                check_at_ms: 15_000
            }
        );
        let back = Checked {
            was_gone: true,
            ..timeout(true, 3)
        };
        assert_eq!(
            after_network_check(back),
            Next::Retry {
                after_ms: 0,
                resume: true
            }
        );
        let after_it = Checked {
            offline: 1,
            ..timeout(true, 3)
        };
        assert_eq!(
            after_network_check(after_it),
            Next::Retry {
                after_ms: 2_000,
                resume: true
            }
        );
    }

    #[test]
    fn a_connection_failure_never_waits_for_an_account() {
        for accounts in [Accounts::None, Accounts::Follow, Accounts::Select] {
            for kind in [failures::CONNECTION, failures::TIMEOUT] {
                for in_call in [true, false] {
                    let next = after_failed_attempt(
                        Failed {
                            in_call,
                            ..failed(kind, 1)
                        },
                        accounts,
                    );
                    assert!(
                        !matches!(next, Next::Account { .. } | Next::Pool { .. }),
                        "{next:?}"
                    );
                }
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
            failures::PROMPT_TOO_LONG,
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
        // A conversation too long for its model fits no better asked again, also where the
        // turn has done nothing yet.
        for accounts in [Accounts::None, Accounts::Follow, Accounts::Select] {
            let untouched = Failed {
                nothing_done: true,
                ..failed(failures::PROMPT_TOO_LONG, 1)
            };
            assert_eq!(after_failed_attempt(untouched, accounts), Next::Fail);
        }
        // And where one is waited for, a plan without the model still ends the turn: no
        // wait puts a model into a plan. Nor does any account make a conversation fit.
        for kind in [
            failures::ACCOUNT_PLAN,
            failures::TRANSIENT,
            failures::PROMPT_TOO_LONG,
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
                    offline: 0,
                    account_wait_since_ms: (waits > 0).then_some(since),
                    nothing_done: false,
                    busy: false,
                    in_call: false,
                    resets_at_ms: None,
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
    fn a_turn_whose_account_resets_at_a_time_it_was_told_waits_until_then_and_no_longer() {
        let hour = 3_600_000;
        let resets = |now_ms: i64, at: i64, waits: u32| Failed {
            account_waits: waits,
            account_wait_since_ms: (waits > 0).then_some(10_000),
            resets_at_ms: Some(at),
            now_ms,
            ..failed(failures::USAGE_LIMIT, waits + 1)
        };
        // Five hours away: the turn is tried a minute after the reset, not now and then.
        let at = 10_000 + 5 * hour;
        assert_eq!(
            after_failed_attempt(resets(10_000, at, 0), Accounts::Follow),
            Next::Account {
                until_ms: at + RESET_MARGIN_MS
            }
        );
        // Out again when it is tried, after it waited far longer than half an hour: it waits
        // for the next reset all the same.
        let later = at + RESET_MARGIN_MS;
        assert_eq!(
            after_failed_attempt(resets(later, later + 2 * hour, 3), Accounts::Follow),
            Next::Account {
                until_ms: later + 2 * hour + RESET_MARGIN_MS
            }
        );
        // A reset that is over, or one beyond a week, is waited for as a limit that said no
        // time: now and then, and half an hour in all.
        for at in [5_000 - RESET_MARGIN_MS, 10_000 + 9 * 24 * hour] {
            assert_eq!(
                after_failed_attempt(resets(10_000, at, 0), Accounts::Follow),
                Next::Account { until_ms: 70_000 },
                "{at}"
            );
        }
        // Said of a sign-in, a time is no reset.
        let auth = Failed {
            kind: failures::AUTH,
            ..resets(10_000, 10_000 + hour, 0)
        };
        assert_eq!(
            after_failed_attempt(auth, Accounts::Follow),
            Next::Account { until_ms: 70_000 }
        );
        // On a Host that waits for no account the turn ends at once, and on one that runs
        // the selector the pool says what follows.
        assert_eq!(
            after_failed_attempt(resets(10_000, at, 0), Accounts::None),
            Next::Fail
        );
        assert_eq!(
            after_failed_attempt(resets(10_000, at, 0), Accounts::Select),
            Next::Pool { plan: false }
        );
        // The turn's own wait in the pool lasts half an hour, or to the reset where that is
        // later.
        assert_eq!(pool_wait_until(None, 0), ACCOUNT_WAIT_MOST_MS);
        assert_eq!(pool_wait_until(Some(60_000), 0), ACCOUNT_WAIT_MOST_MS);
        assert_eq!(
            pool_wait_until(Some(5 * hour), 0),
            5 * hour + RESET_MARGIN_MS
        );
        assert_eq!(after_reset(Some(-RESET_MARGIN_MS), 0), None);
        assert_eq!(after_reset(Some(i64::MAX), 0), None);
    }

    #[test]
    fn on_a_host_that_runs_the_selector_the_pool_says_what_follows_a_failure_on_an_account() {
        for kind in [failures::USAGE_LIMIT, failures::AUTH] {
            // However often the turn has waited: the pool keeps that count.
            for attempt in [1, 2, 9] {
                let next = after_failed_attempt(failed(kind, attempt), Accounts::Select);
                assert_eq!(
                    next,
                    Next::Pool { plan: false },
                    "{kind}, attempt {attempt}"
                );
            }
        }
        // A plan that does not offer the model is the pool's to say as well, and it is told
        // that this is what it is.
        let next = after_failed_attempt(failed(failures::ACCOUNT_PLAN, 1), Accounts::Select);
        assert_eq!(next, Next::Pool { plan: true });
        for kind in [
            failures::TRANSIENT,
            failures::INTERRUPTED,
            failures::PROMPT_TOO_LONG,
            failures::RUNNER_EXIT,
            failures::NOT_STARTED,
            failures::UNKNOWN,
            failures::CLEAN,
        ] {
            let next = after_failed_attempt(failed(kind, 1), Accounts::Select);
            assert_eq!(next, Next::Fail, "{kind}");
        }
    }

    #[test]
    fn waits_for_an_account_use_up_no_tries_against_a_bad_line() {
        // The third attempt of a turn that waited twice for an account is its first try
        // against this failure.
        let after_two_waits = Failed {
            kind: failures::CONNECTION,
            attempt: 3,
            account_waits: 2,
            offline: 0,
            account_wait_since_ms: Some(1_000),
            nothing_done: false,
            busy: false,
            in_call: true,
            resets_at_ms: None,
            now_ms: 500_000,
        };
        assert_eq!(
            after_failed_attempt(after_two_waits, Accounts::Follow),
            Next::Retry {
                after_ms: 1_000,
                resume: false
            }
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
            Accounts::by_name(Accounts::Select.name()),
            Some(Accounts::Select)
        );
        assert_eq!(
            Accounts::by_name(Accounts::None.name()),
            Some(Accounts::None)
        );
        assert_eq!(Accounts::by_name("lead"), None);
    }
}
