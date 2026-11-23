//! When the Hotseat switcher may spend a banked reset, and what the reset tool answered.
//!
//! The owner's rule (23 Nov 2026), held here in code: a banked reset is spent only when ALL
//! accounts of a tool are exhausted AND the soonest natural reset among them is MORE than 48
//! hours away. An account whose reading or reset time is unknown makes that unprovable, and
//! nothing is spent. Which account and grant is the reset tool's choice (the owner's order:
//! the banked reset that expires soonest, then the account holding the most, then the one
//! whose natural reset is furthest away); the tool holds the same rule itself.
//!
//! The lane is the Python Host's (`server/lib/janitor_reset.py`): one JSON request on the
//! tool's stdin (`{"models": [...]}`, or `{"models": [], "reconcile_only": true}` to settle
//! an earlier attempt whose outcome is unknown), one JSON receipt on its stdout; a cap of
//! redemptions per 24 hours for Codex; what the tool claims was spent counts against the cap,
//! verified or not, since a spent reset cannot be given back.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};
use serde_json::Value;

/// How far away the soonest natural reset must be, at least, for a reset to be spent.
pub const LEAD_MS: i64 = 48 * 3600 * 1000;
/// A window used this far has no room left.
const SPENT: f64 = 0.99;
/// How long after a check the next one may run, and its doubling for a reconciliation.
pub const CHECK_INTERVAL_MS: i64 = 10 * 60_000;
/// How many reconciliations in a row are tried before the owner is asked to look.
pub const MAX_RECONCILE_ATTEMPTS: u32 = 6;
const DAY_MS: i64 = 24 * 3600 * 1000;
/// How long the reset tool may take, in seconds.
pub const TIMEOUT_SECONDS: u64 = 150;

/// A moment of the hotseat read (seconds, possibly fractional) in milliseconds.
fn ms(value: &Value) -> Option<i64> {
    let seconds = value.as_f64().filter(|s| s.is_finite() && *s > 0.0)?;
    Some((seconds * 1000.0).round() as i64)
}

/// The windows of one account of a hotseat read, each as (used fraction, reset in ms).
fn windows(tool: &str, row: &Value) -> Option<Vec<(f64, Option<i64>)>> {
    let usage = &row["usage"];
    if !usage.is_object()
        || crate::python::truthy(&row["error"])
        || crate::python::truthy(&usage["error"])
        || crate::python::truthy(&usage["degraded"])
    {
        return None;
    }
    let mut out = Vec::new();
    if tool == "codex" {
        for window in usage["windows"].as_array()? {
            out.push((window["used"].as_f64()?, ms(&window["reset"])));
        }
    } else {
        for (used, reset) in [("used_5h", "reset_5h"), ("used_7d", "reset_7d")] {
            out.push((usage[used].as_f64()?, ms(&usage[reset])));
        }
        for scoped in usage["scoped"].as_array().into_iter().flatten() {
            if let Some(used) = scoped["used"].as_f64() {
                out.push((used, ms(&scoped["reset"])));
            }
        }
    }
    (!out.is_empty()).then_some(out)
}

/// When each account of a hotseat read gets room back by itself: the latest reset of the
/// windows that block it. `None` for the whole read when an account cannot be read, has room
/// now (no reset is due at all), or has a blocking window whose reset is unknown or past.
pub fn natural_resets(tool: &str, body: &Value, now_ms: i64) -> Option<BTreeMap<String, i64>> {
    let rows = body["accounts"]
        .as_array()
        .filter(|rows| !rows.is_empty())?;
    let mut back = BTreeMap::new();
    for row in rows {
        let alias = row["alias"].as_str()?.to_string();
        let blocked: Vec<Option<i64>> = windows(tool, row)?
            .into_iter()
            .filter(|(used, _)| *used >= SPENT)
            .map(|(_, reset)| reset)
            .collect();
        if blocked.is_empty() {
            return None;
        }
        let mut latest = 0;
        for reset in blocked {
            latest = latest.max(reset.filter(|at| *at > now_ms)?);
        }
        back.insert(alias, latest);
    }
    Some(back)
}

/// The owner's rule: every account is out, and the soonest natural reset among them all is
/// more than 48 hours away.
pub fn due(tool: &str, body: &Value, now_ms: i64) -> bool {
    natural_resets(tool, body, now_ms)
        .and_then(|back| back.values().min().copied())
        .is_some_and(|soonest| soonest > now_ms + LEAD_MS)
}

/// What one run of the reset tool came to.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Receipt {
    pub status: String,
    pub verified: bool,
    /// The tool says a reset was spent, verified or not: it counts against the cap.
    pub claimed: bool,
    pub account: Option<String>,
    pub grant: Option<String>,
    pub resets_left: Option<i64>,
    pub needs_reconcile: bool,
}

const ALLOWED: [&str; 12] = [
    "no_pending",
    "cancelled",
    "disabled",
    "not_applicable",
    "no_owned_reset_accounts",
    "busy",
    "cooldown",
    "unverified",
    "no_eligible_grant",
    "eligible",
    "redeemed",
    "reconciled",
];

/// Reads the tool's answer. `ran`: it ended with exit 0 within its time (`false` for a timeout,
/// a kill or another exit). `saved`: the tool's accounts as the switcher read them; a receipt is
/// verified only when it names one of them.
pub fn receipt(ran: bool, printed: &str, saved: &[String], reconcile_only: bool) -> Receipt {
    let unverified = Receipt {
        status: "unverified".into(),
        needs_reconcile: true,
        ..Receipt::default()
    };
    let Ok(Value::Object(said)) = serde_json::from_str::<Value>(printed.trim()) else {
        return unverified;
    };
    let status = said.get("status").and_then(Value::as_str).unwrap_or("");
    let spent_statuses: &[&str] = if reconcile_only {
        &["reconciled"]
    } else {
        &["redeemed", "reconciled"]
    };
    let account = said.get("account").and_then(Value::as_str);
    let verified = ran
        && said.get("verified") == Some(&Value::Bool(true))
        && spent_statuses.contains(&status)
        && account.is_some_and(|account| saved.iter().any(|alias| alias == account));
    let claimed = matches!(status, "redeemed" | "reconciled");
    let status = if !ALLOWED.contains(&status) || !ran || (claimed && !verified) {
        "unverified"
    } else {
        status
    };
    let left = said
        .get("resets_left")
        .and_then(Value::as_i64)
        .filter(|left| *left >= 0 && verified);
    Receipt {
        status: status.into(),
        verified,
        claimed,
        account: account.filter(|_| verified).map(str::to_string),
        grant: said
            .get("grant_id")
            .and_then(Value::as_str)
            .filter(|_| verified)
            .map(str::to_string),
        resets_left: left,
        needs_reconcile: matches!(status, "unverified" | "cancelled")
            || (reconcile_only && !(verified || status == "no_pending")),
    }
}

/// What the switcher keeps of one tool's reset lane, across restarts.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct Ledger {
    /// When resets were claimed spent, within the last 24 hours.
    #[serde(default)]
    pub redeemed: Vec<i64>,
    /// An earlier attempt's outcome is unknown: the next call only reconciles it.
    #[serde(default)]
    pub needs_reconcile: bool,
    /// No call before this moment.
    #[serde(default)]
    pub next_at: i64,
    /// Reconciliations tried in a row.
    #[serde(default)]
    pub attempts: u32,
    /// The present attempt's claimed spend is counted already.
    #[serde(default)]
    pub counted: bool,
}

/// Why no new reset is asked for now, or `None` when one may be.
pub fn refused(ledger: &Ledger, cap: Option<u32>, now_ms: i64) -> Option<&'static str> {
    if cap == Some(0) {
        return Some("off");
    }
    if now_ms < ledger.next_at {
        return Some("cooldown");
    }
    if ledger.needs_reconcile {
        return Some("reconcile_first");
    }
    let spent = ledger
        .redeemed
        .iter()
        .filter(|at| now_ms - DAY_MS < **at && **at <= now_ms)
        .count();
    cap.filter(|cap| spent >= *cap as usize)
        .map(|_| "daily_cap")
}

/// Whether an earlier attempt is to be reconciled now.
pub fn reconcile_due(ledger: &Ledger, now_ms: i64) -> bool {
    ledger.needs_reconcile && now_ms >= ledger.next_at && ledger.attempts < MAX_RECONCILE_ATTEMPTS
}

/// What the owner and the told sessions must hear of a call, beyond a spent reset: said once
/// each, when it happens (the Python lane's `attention_required`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Attention {
    /// The tool claims a reset was spent and it could not be verified: it is reconciled
    /// before anything else.
    Unverified,
    /// Reconciliation gave up after [`MAX_RECONCILE_ATTEMPTS`]: the lane refuses
    /// (`reconcile_first`) until someone looks.
    GaveUp,
}

impl Ledger {
    /// Takes in what one call came to, and says what the owner must hear of it.
    pub fn after(
        &mut self,
        receipt: &Receipt,
        reconcile_only: bool,
        now_ms: i64,
    ) -> Option<Attention> {
        self.redeemed.retain(|at| now_ms - DAY_MS < *at);
        // A reconciliation finishes the attempt before it: a spend it already counted is not
        // counted again.
        if !reconcile_only {
            self.counted = false;
        }
        if receipt.claimed && !self.counted {
            self.redeemed.push(now_ms);
            self.counted = true;
        }
        self.needs_reconcile = receipt.needs_reconcile;
        self.attempts = if reconcile_only && receipt.needs_reconcile {
            self.attempts + 1
        } else {
            0
        };
        let backoff = 1i64 << self.attempts.saturating_sub(1).min(6);
        self.next_at = now_ms + CHECK_INTERVAL_MS * backoff;
        if self.needs_reconcile && reconcile_only && self.attempts == MAX_RECONCILE_ATTEMPTS {
            Some(Attention::GaveUp)
        } else if !reconcile_only && receipt.claimed && !receipt.verified {
            Some(Attention::Unverified)
        } else {
            None
        }
    }
}

/// What the owner and the told sessions hear when a call needs someone.
pub fn told_attention(tool: &str, attention: Attention) -> String {
    let name = if tool == "codex" { "Codex" } else { "Claude" };
    match attention {
        Attention::Unverified => format!(
            "Hotseat's {name} reset tool says it spent a banked reset, but that could not be \
             verified. Nothing more is spent until it is reconciled."
        ),
        Attention::GaveUp => format!(
            "Hotseat could not reconcile an earlier {name} banked reset after \
             {MAX_RECONCILE_ATTEMPTS} tries. No {name} reset is spent until someone looks."
        ),
    }
}

/// What the owner and the front desk are told of a reset that was spent.
pub fn told(tool: &str, receipt: &Receipt, natural_ms: Option<i64>, now_ms: i64) -> String {
    let name = if tool == "codex" { "Codex" } else { "Claude" };
    let account = receipt.account.as_deref().unwrap_or("an account");
    let grant = receipt
        .grant
        .as_deref()
        .map_or(String::new(), |grant| format!(" (grant {grant})"));
    let left = receipt
        .resets_left
        .map_or(String::new(), |left| format!(", {left} left there"));
    let natural = natural_ms.map_or(String::new(), |at| {
        let when = jiff::Timestamp::from_millisecond(at)
            .map(|at| at.strftime("%Y-%m-%d %H:%M UTC").to_string())
            .unwrap_or_default();
        format!(
            "; its own reset would have come {when}, {} h from now",
            (at - now_ms) / 3_600_000
        )
    });
    format!(
        "Hotseat spent a banked {name} reset on {account}{grant}{left}: every {name} account was \
         out{natural}."
    )
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;
    use serde_json::json;

    const NOW: i64 = 1_800_000_000_000;
    const H: i64 = 3_600_000;

    fn codex(accounts: &[(&str, f64, i64)]) -> Value {
        json!({"accounts": accounts.iter().map(|(alias, used, back)| json!({
            "alias": alias,
            "usage": {"windows": [
                {"label": "weekly", "used": used, "reset": (NOW + back) as f64 / 1000.0},
                {"label": "5h", "used": 0.1, "reset": (NOW + H) as f64 / 1000.0},
            ]},
        })).collect::<Vec<_>>()})
    }

    #[test]
    fn the_owners_48_hours_47_refuses_and_49_spends() {
        assert!(!due(
            "codex",
            &codex(&[("a", 1.0, 47 * H), ("b", 1.0, 100 * H)]),
            NOW
        ));
        assert!(due(
            "codex",
            &codex(&[("a", 1.0, 49 * H), ("b", 1.0, 100 * H)]),
            NOW
        ));
    }

    #[test]
    fn an_account_with_room_or_unreadable_means_no_reset_is_due() {
        assert!(!due(
            "codex",
            &codex(&[("a", 0.5, 100 * H), ("b", 1.0, 100 * H)]),
            NOW
        ));
        let mut body = codex(&[("a", 1.0, 100 * H), ("b", 1.0, 100 * H)]);
        body["accounts"][1]["usage"] = json!({"error": "auth"});
        assert!(!due("codex", &body, NOW));
        assert!(!due("codex", &json!({"accounts": []}), NOW));
    }

    #[test]
    fn claude_counts_every_blocking_window_of_every_seat() {
        let seat = |alias: &str, week: f64, back: i64| {
            json!({"alias": alias, "usage": {
                "used_5h": 0.2, "reset_5h": (NOW + H) / 1000,
                "used_7d": week, "reset_7d": (NOW + back) / 1000,
                "scoped": [{"model": "Fable", "used": 1.0, "reset": (NOW + back) / 1000}]}})
        };
        let body = json!({"accounts": [seat("ai1", 1.0, 72 * H), seat("ai2", 1.0, 60 * H)]});
        assert!(due("claude", &body, NOW));
        let back = natural_resets("claude", &body, NOW).unwrap();
        assert_eq!(back["ai2"], NOW + 60 * H);
        let soon = json!({"accounts": [seat("ai1", 1.0, 72 * H), seat("ai2", 1.0, 40 * H)]});
        assert!(!due("claude", &soon, NOW));
    }

    #[test]
    fn the_cap_and_the_reconciliation_hold_new_requests() {
        let mut ledger = Ledger::default();
        assert_eq!(refused(&ledger, Some(1), NOW), None);
        assert_eq!(refused(&ledger, Some(0), NOW), Some("off"));
        let spent = receipt(
            true,
            r#"{"status":"redeemed","verified":true,"account":"b","resets_left":2}"#,
            &["a".into(), "b".into()],
            false,
        );
        assert!(spent.verified && spent.claimed && !spent.needs_reconcile);
        ledger.after(&spent, false, NOW);
        assert_eq!(
            refused(&ledger, Some(1), NOW + 11 * 60_000),
            Some("daily_cap")
        );
        assert_eq!(refused(&ledger, Some(2), NOW + 11 * 60_000), None);
        assert_eq!(refused(&ledger, Some(1), NOW + 25 * H), None);
        // A claimed spend that could not be verified counts, and is reconciled before any other.
        let unsure = receipt(
            true,
            r#"{"status":"redeemed","verified":false,"account":"b"}"#,
            &["b".into()],
            false,
        );
        assert!(unsure.claimed && !unsure.verified && unsure.needs_reconcile);
        let mut ledger = Ledger::default();
        ledger.after(&unsure, false, NOW);
        assert_eq!(ledger.redeemed.len(), 1);
        assert_eq!(
            refused(&ledger, Some(5), NOW + 11 * 60_000),
            Some("reconcile_first")
        );
        assert!(reconcile_due(&ledger, NOW + 11 * 60_000));
        let settled = receipt(
            true,
            r#"{"status":"reconciled","verified":true,"account":"b"}"#,
            &["b".into()],
            true,
        );
        ledger.after(&settled, true, NOW + 11 * 60_000);
        assert!(!ledger.needs_reconcile);
        assert_eq!(
            ledger.redeemed.len(),
            1,
            "the reconciled spend is not counted twice"
        );
    }

    #[test]
    fn an_unverified_spend_is_told_once_and_a_reconciliation_that_gives_up_holds_the_lane() {
        let saved = ["b".to_string()];
        let unsure = receipt(
            true,
            r#"{"status":"redeemed","verified":false,"account":"b"}"#,
            &saved,
            false,
        );
        let mut ledger = Ledger::default();
        assert_eq!(
            ledger.after(&unsure, false, NOW),
            Some(Attention::Unverified)
        );
        let still = receipt(false, "", &saved, true);
        let mut at = NOW;
        let mut told = Vec::new();
        for _ in 0..MAX_RECONCILE_ATTEMPTS {
            at = ledger.next_at;
            assert!(reconcile_due(&ledger, at));
            told.push(ledger.after(&still, true, at));
        }
        assert_eq!(
            told.iter().filter(|said| said.is_some()).count(),
            1,
            "{told:?}"
        );
        assert_eq!(told.last(), Some(&Some(Attention::GaveUp)));
        // No more tries, and nothing new is asked for, however long it waits.
        assert!(!reconcile_due(&ledger, at + 100 * H));
        assert_eq!(
            refused(&ledger, Some(5), at + 100 * H),
            Some("reconcile_first")
        );
        assert!(told_attention("codex", Attention::GaveUp).contains("until someone looks"));
    }

    #[test]
    fn a_receipt_that_names_no_saved_account_or_failed_is_not_verified() {
        let foreign = receipt(
            true,
            r#"{"status":"redeemed","verified":true,"account":"x"}"#,
            &["a".into()],
            false,
        );
        assert!(!foreign.verified && foreign.status == "unverified" && foreign.needs_reconcile);
        let timeout = receipt(false, "", &["a".into()], false);
        assert!(timeout.needs_reconcile);
        let wait = receipt(
            true,
            r#"{"status":"cooldown","verified":false,"reason":"natural_reset_within_48h"}"#,
            &[],
            false,
        );
        assert_eq!(wait.status, "cooldown");
        assert!(!wait.needs_reconcile && !wait.claimed);
        let none = receipt(
            true,
            r#"{"status":"no_pending","verified":false}"#,
            &[],
            true,
        );
        assert!(!none.needs_reconcile);
    }

    #[test]
    fn the_owner_is_told_which_account_which_grant_and_when_its_own_reset_would_have_come() {
        let spent = Receipt {
            status: "redeemed".into(),
            verified: true,
            claimed: true,
            account: Some("ai2".into()),
            grant: Some("fable-launch".into()),
            resets_left: Some(0),
            needs_reconcile: false,
        };
        let text = told("claude", &spent, Some(NOW + 60 * H), NOW);
        assert!(
            text.contains("ai2") && text.contains("fable-launch") && text.contains("60 h"),
            "{text}"
        );
    }
}
