//! What the user's compaction of an agent did (`last_compaction` on its row, the feature
//! `compaction_outcome`): the Python Host's PR 483 (R148), `server/lib/compaction.py`.
//!
//! A compaction is asked for from an app and runs as a turn of the agent's own (the Host's
//! `compact.rs`). When that turn has ended, what it did is said from numbers, never from a
//! clock: `compacted`, `not_compacted` or `unknown`, with the sizes before and after and why in
//! words. The proof that it compacted is Claude Code's own record of it in the turn's output, the
//! `compact_boundary` (its last one, where there were more). The rule is the Python Host's
//! `decide_claude`, held to its answers by `tests/compaction_outcome_vectors.rs`; what the turn's
//! end means for it ([`decide`]) is this Host's, because here the compaction is a turn and not a
//! terminal that was watched. One thing this Host says that the Python Host does not: the size
//! after, where the record says it (`post_tokens`), which is the tool's own number and no
//! estimate. The Python Host reads it only at the agent's next call.

use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::failures;
use crate::turn::Outcome;

pub const COMPACTED: &str = "compacted";
pub const NOT_COMPACTED: &str = "not_compacted";
pub const UNKNOWN: &str = "unknown";

/// How long the Python Host lets a compaction in a terminal run before it gives up, in
/// seconds: only in the words it says then. This Host has no such clock.
pub const HARD_CAP_SEC: u64 = 180;

/// What is said of a compaction taken back before it began.
pub const STOPPED_BEFORE_IT_BEGAN: &str = "stopped before it began";

/// What is said of a compaction whose tool ended without saying how it went: it may have
/// compacted, and the Host did not see it.
pub const ENDED_UNSAID: &str =
    "Claude Code ended without saying how it went, so whether it compacted is not known";

/// Claude Code's record of a compaction: the size before (0 where it says none) and after
/// (where it says one).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct Boundary {
    pub pre_tokens: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub post_tokens: Option<u64>,
}

/// What a compaction did, without whose and when.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Decided {
    pub outcome: String,
    pub before_tokens: Option<u64>,
    pub after_tokens: Option<u64>,
    pub after_is_estimate: bool,
    pub detail: String,
}

/// What the user's last compaction of an agent did: the agent's `last_compaction`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct LastCompaction {
    #[serde(flatten)]
    pub decided: Decided,
    pub backend: String,
    /// When the compaction's turn ended, in ms.
    pub at: i64,
}

/// What is said (the Python Host's `outcome`): an estimate only of a size there is.
pub fn outcome(
    kind: &str,
    before: Option<u64>,
    after: Option<u64>,
    after_is_estimate: bool,
    detail: &str,
) -> Decided {
    Decided {
        outcome: kind.to_string(),
        before_tokens: before,
        after_tokens: after,
        after_is_estimate: after_is_estimate && after.is_some(),
        detail: detail.to_string(),
    }
}

/// The Python Host's rule for Claude Code (`decide_claude`): `compacted` on Claude Code's own
/// record of it (`proof`, its size before, 0 where it said none); its size before is that, or
/// `before` where the record said none. `ending`: how the wait for it ended there (`settled`,
/// `stopped`, `cap`, `waited`).
pub fn decide_claude(
    before: Option<u64>,
    proof: Option<u64>,
    transcript: bool,
    ending: &str,
) -> Decided {
    if !transcript {
        return outcome(
            UNKNOWN,
            before,
            None,
            false,
            "the Host found no transcript to check the compaction in",
        );
    }
    if let Some(proof) = proof {
        let before = if proof == 0 { before } else { Some(proof) };
        return outcome(
            COMPACTED,
            before,
            None,
            false,
            "Claude Code recorded the compaction; the size after is known at the agent's next call",
        );
    }
    match ending {
        "stopped" => outcome(
            NOT_COMPACTED,
            before,
            None,
            false,
            "stopped before Claude Code recorded a compaction",
        ),
        "cap" => outcome(
            UNKNOWN,
            before,
            None,
            false,
            &format!("Claude Code did not finish within {HARD_CAP_SEC} s"),
        ),
        _ => outcome(
            NOT_COMPACTED,
            before,
            None,
            false,
            "Claude Code recorded no compaction",
        ),
    }
}

/// Claude Code's record of a compaction, where `event` is one (`{"type": "system", "subtype":
/// "compact_boundary", "compact_metadata": {"pre_tokens": N, "post_tokens": M}}`). A size that
/// is no whole number of nothing or more is none: before, that is 0, as the Python Host reads it
/// (`compact_boundary_tokens`).
pub fn boundary(event: &Value) -> Option<Boundary> {
    if event["type"].as_str() != Some("system")
        || event["subtype"].as_str() != Some("compact_boundary")
    {
        return None;
    }
    let metadata = &event["compact_metadata"];
    Some(Boundary {
        pre_tokens: metadata["pre_tokens"].as_u64().unwrap_or(0),
        post_tokens: metadata["post_tokens"].as_u64(),
    })
}

/// The record that counts of a turn's: the newest one.
pub fn newest(held: Option<Boundary>, seen: Option<Boundary>) -> Option<Boundary> {
    seen.or(held)
}

/// What the compaction turn that ended so did: from Claude Code's record of it in that turn
/// (`boundary`), else from how the turn ended. `gauge`: how full the conversation was when it
/// began; `model`: what the tool ran on, for the words of a failure; `concluded`: the tool said
/// how the turn went, in a result of its own. A failure it told did not compact; a process that
/// ended without its word may have, unless the Host never started it.
pub fn decide(
    ended: &Outcome,
    boundary: Option<Boundary>,
    gauge: Option<u64>,
    model: &str,
    concluded: bool,
) -> Decided {
    if let Some(record) = boundary {
        let mut said = decide_claude(gauge, Some(record.pre_tokens), true, "settled");
        if let Some(after) = record.post_tokens {
            // The size after is here: the Python Host's word that it comes with the next call
            // is not true of this Host.
            said.after_tokens = Some(after);
            said.after_is_estimate = false;
            said.detail = said.detail.split("; ").next().unwrap_or_default().to_string();
        }
        return said;
    }
    match ended {
        Outcome::Completed => decide_claude(gauge, None, true, "settled"),
        Outcome::Stopped { .. } => decide_claude(gauge, None, true, "stopped"),
        Outcome::Withdrawn { .. } => outcome(NOT_COMPACTED, gauge, None, false, STOPPED_BEFORE_IT_BEGAN),
        Outcome::Failed { reason, kind } => {
            let kind = if kind.is_empty() {
                failures::classify_error(reason)
            } else {
                kind.as_str()
            };
            if !concluded && kind != failures::NOT_STARTED {
                return outcome(UNKNOWN, gauge, None, false, ENDED_UNSAID);
            }
            let told = failures::told(kind, reason, model, failures::Known::default());
            outcome(NOT_COMPACTED, gauge, None, false, &told.row)
        }
        Outcome::Interrupted { .. } => outcome(UNKNOWN, gauge, None, false, ENDED_UNSAID),
    }
}
