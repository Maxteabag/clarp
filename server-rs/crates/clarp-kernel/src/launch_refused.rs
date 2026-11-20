//! A send whose tool could not be started (R121, the release owner's PR 466): a launch refused
//! before anything was given to the tool is tried again, and after eight refusals that are one
//! refusal the turn ends failed and its sender is told why.
//!
//! The Python Host's rule (`server/lib/recovery_backoff.py`, `turn_queue.REFUSED_LAUNCH_NOTICE`
//! and `fail_refused_launch`): a send that is refused at its own try is tried again from the
//! queue a second later; every refusal from there on is counted, and the next try waits 1, 2, 4
//! ... 60 s. Refusals that differ only in ids, counts or times are one refusal ([`signature`]);
//! another refusal begins the count of one again. At the eighth of one ([`PARK_AFTER`]) the turn
//! ends. Held by `tests/fixtures/launch-refused-vectors.json`, written by the Python Host's own
//! functions.

use std::sync::LazyLock;

use regex::Regex;

/// The first wait between two counted tries.
pub const BASE_MS: i64 = 1_000;
/// The longest wait between two tries.
pub const MAX_MS: i64 = 60_000;
/// How many tries of one refusal end the turn.
pub const PARK_AFTER: u32 = 8;
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
        if self.identical >= PARK_AFTER {
            Next::End
        } else {
            Next::Wait {
                ms: wait_ms(self.failures),
            }
        }
    }
}
