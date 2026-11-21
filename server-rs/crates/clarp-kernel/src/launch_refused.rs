//! A send whose tool could not be started (R121, the release owner's PR 466): a launch refused
//! before anything was given to the tool is tried again, and after eight refusals that are one
//! refusal the turn ends failed and its sender is told why.
//!
//! The Python Host's rule (`server/lib/recovery_backoff.py`, `turn_queue.REFUSED_LAUNCH_NOTICE`
//! and `fail_refused_launch`): a send that is refused at its own try is tried again from the
//! queue a second later; every refusal from there on is counted, and the next try waits 1, 2, 4
//! ... 60 s. Refusals that differ only in ids, counts or times are one refusal ([`signature`]);
//! another refusal begins the count of one again. At the eighth of one ([`PARK_AFTER`]), or at the
//! twentieth refusal of the turn in all ([`REFUSED_IN_ALL`], the release owner's ruling of 20 Nov 2026 on
//! refusals that alternate between two kinds), whichever comes first, the turn ends. Held by `tests/fixtures/launch-refused-vectors.json`, written by the Python Host's own
//! functions.

use std::sync::LazyLock;

use regex::Regex;

/// The first wait between two counted tries.
pub const BASE_MS: i64 = 1_000;
/// The longest wait between two tries.
pub const MAX_MS: i64 = 60_000;
/// How many tries of one refusal end the turn.
pub const PARK_AFTER: u32 = 8;
/// How many counted refusals of a turn in all end it, whatever they were (about a quarter of an
/// hour at the waits above; `recovery_backoff.REFUSED_IN_ALL`).
pub const REFUSED_IN_ALL: u32 = 20;
/// The wait after a send's own try, before the first counted one: the queue is looked at again
/// a second later.
pub const FIRST_WAIT_MS: i64 = 1_000;
/// How much of its words a refusal keeps.
const SIGNED_MOST: usize = 300;
/// How much of its words the notice says of a refusal.
const BRIEF_MOST: usize = 240;

// Written here, not read from anywhere: one that does not compile is a mistake in this file,
// which the tests of this module would not get past.
static UUID: LazyLock<Regex> = LazyLock::new(|| {
    #[allow(clippy::expect_used)]
    Regex::new(r"(?i)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
        .expect("a pattern of this module compiles")
});
static NUMBERS: LazyLock<Regex> = LazyLock::new(|| {
    #[allow(clippy::expect_used)]
    Regex::new(r"[0-9a-f]{6,}|\d+").expect("a pattern of this module compiles")
});

/// What a refusal is, as one refusal is told from another: its kind and its words, with every
/// id, count and time made the same (`recovery_backoff.signature`).
pub fn signature(kind: &str, text: &str) -> String {
    let said = format!("{kind}: {text}");
    let said = UUID.replace_all(&said, "<id>");
    let said = NUMBERS.replace_all(&said, "#");
    said.chars().take(SIGNED_MOST).collect()
}

/// The words of a refusal as the notice says them: whole where they are short, and otherwise
/// both their ends, where a provider puts the cause (`recovery_backoff._brief`).
pub fn brief(text: &str) -> String {
    let signs: Vec<char> = text.chars().collect();
    if signs.len() <= BRIEF_MOST {
        return text.to_string();
    }
    let head = BRIEF_MOST / 3;
    let tail = BRIEF_MOST - head - 3;
    let start: String = signs[..head].iter().collect();
    let end: String = signs[signs.len() - tail..].iter().collect();
    format!("{start} … {end}")
}

/// The wait after the `failures`th counted refusal.
pub fn wait_ms(failures: u32) -> i64 {
    let doubled = 1_i64 << failures.saturating_sub(1).min(16);
    MAX_MS.min(BASE_MS.saturating_mul(doubled))
}

/// What the sender is told when the turn ends (`REFUSED_LAUNCH_NOTICE`).
pub fn notice(failures: u32, error: &str) -> String {
    let error = error.trim_end_matches('.');
    format!(
        "A message you sent was never run.\n\nThe agent could not be started for it {failures} \
         times in a row: {error}. It will not be tried again; send it again once the agent can \
         start."
    )
}

/// The count of a turn's refusals (`recovery_backoff.Backoff`).
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Refusals {
    /// Every counted refusal.
    pub failures: u32,
    /// The refusals in a row that are one refusal, this one included.
    pub identical: u32,
    signature: String,
    /// The words of the last one, as the notice says them.
    pub error: String,
}

/// What follows a counted refusal.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Next {
    /// The turn is tried again after this wait.
    Wait { ms: i64 },
    /// The turn ends failed, and its sender is told.
    End,
}

impl Refusals {
    /// One more refusal (`RecoveryBackoff.failed`).
    pub fn failed(&mut self, kind: &str, text: &str) -> Next {
        let signed = signature(kind, text);
        self.failures += 1;
        self.identical = if signed == self.signature {
            self.identical + 1
        } else {
            1
        };
        self.signature = signed;
        self.error = brief(text);
        if self.identical >= PARK_AFTER || self.failures >= REFUSED_IN_ALL {
            Next::End
        } else {
            Next::Wait {
                ms: wait_ms(self.failures),
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The turn's refusals, by kind: whether each ends the turn, and the words the notice then
    /// says (the last refusal's).
    fn run(kinds: &[&str]) -> Vec<(bool, String)> {
        let mut refusals = Refusals::default();
        kinds
            .iter()
            .enumerate()
            .map(|(at, kind)| {
                let next = refusals.failed(kind, &format!("refused by {kind}, try {at}"));
                (next == Next::End, refusals.error.clone())
            })
            .collect()
    }

    #[test]
    fn a_turn_ends_at_the_eighth_identical_refusal_or_the_twentieth_in_all_whichever_comes_first() {
        // By table: the refusals' kinds, and at which (from 1) the turn ends.
        let alternating: Vec<&str> = ["A", "B"].iter().copied().cycle().take(25).collect();
        let seven_then_other: Vec<&str> = std::iter::repeat_n("A", 7)
            .chain(["B"])
            .chain(std::iter::repeat_n("A", 7))
            .chain(["B"])
            .chain(std::iter::repeat_n("A", 9))
            .collect();
        let identical: Vec<&str> = std::iter::repeat_n("A", 25).collect();
        let late_run: Vec<&str> = ["A", "B"]
            .iter()
            .copied()
            .cycle()
            .take(6)
            .chain(std::iter::repeat_n("C", 10))
            .collect();
        for (what, kinds, ends_at) in [
            ("eight identical in a row", identical.as_slice(), 8),
            (
                "two kinds that alternate",
                alternating.as_slice(),
                REFUSED_IN_ALL as usize,
            ),
            (
                "seven, another, seven, another: the twentieth",
                seven_then_other.as_slice(),
                20,
            ),
            (
                "eight identical after six others: the fourteenth",
                late_run.as_slice(),
                14,
            ),
        ] {
            let ran = run(kinds);
            let first_end = ran.iter().position(|(ends, _)| *ends).map(|at| at + 1);
            assert_eq!(first_end, Some(ends_at), "{what}");
            // The notice says the last refusal's words.
            let (_, said) = &ran[ends_at - 1];
            assert_eq!(
                said,
                &format!("refused by {}, try {}", kinds[ends_at - 1], ends_at - 1),
                "{what}"
            );
        }
        // Nineteen that alternate do not end it.
        assert!(run(&alternating[..19]).iter().all(|(ends, _)| !ends));
    }
}
