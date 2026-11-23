//! Whether the account an agent runs on is near a limit of its usage, before it is at it: what
//! an agent's row says of its tool then (`backend_quota` with `state: near`).
//!
//! The tools say how much of each window of their account is used (`crate::quota` for Codex,
//! `crate::standing` for Claude). A window stands near its limit from a share of it that
//! depends on how long the window is ([`near_at`]): four fifths of the five hours, nine
//! tenths of the week. At four fifths of five hours about an hour of steady work is left,
//! time to finish what is under way or to choose another tool; four fifths of a week leave
//! more than a day, and a warning that stood for a day and more would be read past, so the
//! week warns at nine tenths, when well under a day is left. The five hours' share is the
//! one the Host already tells as a warning of the five-hour window (`quota::WARNING_AT`), so
//! the row and that event agree.
//!
//! What is used of a window only grows until the window begins anew. So a window that was
//! said to be near stands until its reset has passed, however old the word is, while the
//! account is the same one; a window that says no reset stands while the word of it is fresh.
//! A window that is all used is no longer near: whether the account is used up is said apart
//! (`state: exhausted`), by its own rule.
//!
//! A warning that another account would take the turn from is noise, so the row warns only
//! where no other seat of the tool would take it ([`failover`]). Where the Host cannot know,
//! it warns and says that it could not know (`failover: unknown`): a warning that a switch
//! then makes needless costs a glance, and a limit reached without one stops the agent's
//! work, which is what the warning is for.

use serde_json::{Value, json};

use crate::quota::{from_text, moment};
use crate::recovery::Accounts;

/// From how much of a window, in percent, it stands near its limit: four fifths of the five
/// hours, nine tenths of a week (and of any other window, which is at least as long).
pub fn near_at(window: &str) -> f64 {
    match window {
        "five_hour" => FIVE_HOURS_NEAR_AT,
        _ => WEEK_NEAR_AT,
    }
}

pub const FIVE_HOURS_NEAR_AT: f64 = 80.0;
pub const WEEK_NEAR_AT: f64 = 90.0;

/// A window of an account as a tool last said it.
#[derive(Debug, Clone, Copy)]
pub struct Window<'a> {
    /// `five_hour` or `seven_day`.
    pub kind: &'a str,
    /// How much of it is used, in percent.
    pub used: f64,
    /// When it begins anew, as text; `None` where that was not said.
    pub resets_at: Option<&'a str>,
    /// Whether the word of it counts as fresh by its tool's rule.
    pub fresh: bool,
    pub observed_at_ms: i64,
}

impl Window<'_> {
    /// Whether it stands near its limit now: at or past its share and not all used, and the
    /// word of it still holds (its reset has not passed; else, without one, it is fresh).
    fn near(&self, now_ms: i64) -> bool {
        let holds = match self.resets_at.filter(|at| !at.is_empty()).map(from_text) {
            Some(Some(reset_ms)) => now_ms < reset_ms,
            _ => self.fresh,
        };
        holds && self.used >= near_at(self.kind) && self.used < 100.0
    }
}

/// What an agent's row says of a tool whose account stands near a limit, before it is known
/// whether another seat would take the turn: the most used window among those that stand
/// near (of two as used, the first given), or `None`. When it begins anew is written as
/// every moment of the row is (UTC, to the second), whatever form the tool gave it in.
pub fn nearest<'a>(
    provider: &str,
    windows: impl IntoIterator<Item = Window<'a>>,
    now_ms: i64,
) -> Option<Value> {
    let mut nearest: Option<Window<'a>> = None;
    for window in windows.into_iter().filter(|window| window.near(now_ms)) {
        if nearest.is_none_or(|kept| window.used > kept.used) {
            nearest = Some(window);
        }
    }
    let window = nearest?;
    Some(json!({
        "state": "near",
        "provider_id": provider,
        "reason": "usage_limit",
        "window": window.kind,
        "used_percentage": window.used,
        "resets_at": window.resets_at.and_then(from_text).map(moment),
        "observed_at": moment(window.observed_at_ms),
    }))
}

/// Whether another seat of a tool would take an agent's turn from an account near its limit.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Failover {
    /// Another seat has room, and something on this Host moves the tool to it.
    Spare,
    /// None would: nothing moves the tool to another seat, or no other seat has room.
    None,
    /// This Host cannot know: another Host keeps the logins, or the reading of the seats is
    /// old, failed, or does not say enough.
    Unknown,
}

impl Failover {
    /// What the row says of it (`failover`); a spare seat is never said, the row is null.
    pub fn word(self) -> &'static str {
        match self {
            Self::Spare => "spare",
            Self::None => "none",
            Self::Unknown => "unknown",
        }
    }
}

/// Whether another seat of `provider` would take the turn of an agent on `model`.
///
/// - `--accounts none`: nobody puts another login in place. None would.
/// - `--accounts follow`: another Host on the machine does, by seats this Host does not read.
///   Unknown.
/// - `--accounts select`: this Host does, where its pool has the owner's selector for the
///   tool (`selector`) or the switcher of the seats is on and switches by itself (`desk`, the
///   strict reading of the seats as `GET /hotseat/status` serves it). Where neither is, none
///   would. Else by the desk: unknown where its reading of the tool is old, failed or not
///   there; none where it read no account at all; spare where an account other than the one in
///   use is read as ok, with every window that bears on the agent below its share
///   ([`near_at`]: the five hours and the week, and the window of the model's family where the
///   account has one; of every family where the model's is not known) and read fresh or begun
///   anew since; none where every other account is read and none is so; unknown where one of
///   them could not be judged. `model` is the model the tool runs for the agent: its own, else
///   the tool's default.
pub fn failover(
    accounts: Accounts,
    provider: &str,
    model: &str,
    selector: bool,
    desk: &Value,
) -> Failover {
    match accounts {
        Accounts::None => return Failover::None,
        Accounts::Follow => return Failover::Unknown,
        Accounts::Select => {}
    }
    let switcher = desk["switcher"]["enabled"] == true
        && desk["switcher"]["lanes"]
            .as_array()
            .into_iter()
            .flatten()
            .any(|lane| lane["provider"] == provider && lane["mode"] == "automatic");
    if !selector && !switcher {
        return Failover::None;
    }
    let read = desk["providers"]
        .as_array()
        .into_iter()
        .flatten()
        .find(|read| read["provider"] == provider);
    let Some(read) = read.filter(|read| read["stale"] == false && read["read_state"] == "ok")
    else {
        return Failover::Unknown;
    };
    match read["inventory"].as_str() {
        Some("ok") => {}
        Some("empty") => return Failover::None,
        _ => return Failover::Unknown,
    }
    let family = crate::seats::family(&json!(model));
    let served_at = desk["served_at"].as_i64().unwrap_or(0);
    let mut unjudged = false;
    let others = read["accounts"]
        .as_array()
        .into_iter()
        .flatten()
        .filter(|account| account["active_default"] != true);
    for account in others {
        match room(account, family, served_at) {
            Some(true) => return Failover::Spare,
            Some(false) => {}
            None => unjudged = true,
        }
    }
    if unjudged {
        Failover::Unknown
    } else {
        Failover::None
    }
}

/// Whether an account of the desk has room for a turn of a model's family: `None` where it
/// cannot be told (its state is not known, or a window that bears on it is old or has no
/// number). A window that began anew since it was read has nothing of it used.
fn room(account: &Value, family: Option<&str>, served_at: i64) -> Option<bool> {
    match account["state"].as_str() {
        Some("ok") => {}
        Some("exhausted" | "auth_error") => return Some(false),
        _ => return None,
    }
    let bears = |kind: &str| match kind.strip_prefix("model:") {
        Some(of) => family.is_none_or(|family| of == family),
        None => matches!(kind, "5h" | "7d"),
    };
    let mut general = false;
    for window in account["windows"].as_array().into_iter().flatten() {
        let kind = window["kind"].as_str().unwrap_or("");
        if !bears(kind) {
            continue;
        }
        let begun_anew = window["used_ratio"].is_null()
            && window["resets_at"]
                .as_i64()
                .is_some_and(|reset| reset <= served_at);
        let used = if begun_anew {
            0.0
        } else {
            window["used_ratio"]
                .as_f64()
                .filter(|_| window["stale"] == false)?
        };
        let span = if kind == "5h" {
            "five_hour"
        } else {
            "seven_day"
        };
        if used * 100.0 >= near_at(span) {
            return Some(false);
        }
        general |= matches!(kind, "5h" | "7d");
    }
    general.then_some(true)
}

/// What an agent's row says of a tool near its limit, given whether another seat would take
/// the turn: null where one would.
pub fn row(near: &Value, failover: Failover) -> Value {
    if failover == Failover::Spare {
        return Value::Null;
    }
    let mut row = near.clone();
    row["failover"] = json!(failover.word());
    row
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;

    const NOW: i64 = 1_900_000_000_000;

    fn window<'a>(kind: &'a str, used: f64, resets_at: Option<&'a str>, fresh: bool) -> Window<'a> {
        Window {
            kind,
            used,
            resets_at,
            fresh,
            observed_at_ms: NOW - 60_000,
        }
    }

    #[test]
    fn a_window_is_near_from_its_share_until_it_is_all_used() {
        let later = Some("2030-03-17T19:00:00Z");
        assert_eq!(moment(NOW), "2030-03-17T17:46:40Z");
        // Below the five hours' share, at it, and all used.
        assert!(nearest("codex", [window("five_hour", 79.9, later, true)], NOW).is_none());
        let at = nearest("codex", [window("five_hour", 80.0, later, true)], NOW).unwrap();
        assert_eq!(
            at,
            json!({
                "state": "near", "provider_id": "codex", "reason": "usage_limit",
                "window": "five_hour", "used_percentage": 80.0,
                "resets_at": "2030-03-17T19:00:00Z", "observed_at": "2030-03-17T17:45:40Z",
            })
        );
        assert!(nearest("codex", [window("five_hour", 100.0, later, true)], NOW).is_none());
        // The week warns later: at 85 it does not, at 90 it does.
        assert!(nearest("claude", [window("seven_day", 85.0, later, true)], NOW).is_none());
        let week = nearest("claude", [window("seven_day", 90.0, later, true)], NOW).unwrap();
        assert_eq!(week["window"], "seven_day");
        // Of two near, the more used.
        let both = [
            window("five_hour", 82.0, later, true),
            window("seven_day", 95.5, Some("2030-03-20T00:00:00Z"), true),
        ];
        let named = nearest("claude", both, NOW).unwrap();
        assert_eq!(
            (named["window"].as_str(), named["used_percentage"].as_f64()),
            (Some("seven_day"), Some(95.5))
        );
    }

    #[test]
    fn a_near_window_stands_until_it_begins_anew() {
        // Old words of a window whose reset is still ahead stand: what is used only grows.
        let ahead = window("five_hour", 90.0, Some("2030-03-17T19:00:00Z"), false);
        assert!(nearest("codex", [ahead], NOW).is_some());
        // Its reset passed: it is no longer near, fresh or not.
        let passed = window("five_hour", 90.0, Some("2030-03-17T17:46:40Z"), true);
        assert!(nearest("codex", [passed], NOW).is_none());
        // No reset said: it stands while the word is fresh.
        for unsaid in [None, Some("")] {
            assert!(nearest("claude", [window("five_hour", 90.0, unsaid, true)], NOW).is_some());
            assert!(nearest("claude", [window("five_hour", 90.0, unsaid, false)], NOW).is_none());
            let row = nearest("claude", [window("five_hour", 90.0, unsaid, true)], NOW).unwrap();
            assert!(row["resets_at"].is_null());
        }
    }

    fn desk(enabled: bool, mode: &str, read: Value) -> Value {
        json!({
            "switcher": { "enabled": enabled, "lanes": [{ "provider": "claude", "mode": mode }] },
            "providers": [read],
        })
    }

    fn read(accounts: Value) -> Value {
        json!({
            "provider": "claude", "inventory": "ok", "stale": false, "read_state": "ok",
            "accounts": accounts,
        })
    }

    fn seat(active: bool, state: &str, windows: &[(&str, Option<f64>, bool)]) -> Value {
        let windows: Vec<Value> = windows
            .iter()
            .map(|(kind, used, stale)| json!({ "kind": kind, "used_ratio": used, "stale": stale }))
            .collect();
        json!({ "active_default": active, "state": state, "windows": windows })
    }

    #[test]
    fn another_seat_takes_the_turn_only_where_the_host_moves_to_it_and_it_has_room() {
        let in_use = seat(
            true,
            "ok",
            &[("5h", Some(0.85), false), ("7d", Some(0.2), false)],
        );
        let spare = seat(
            false,
            "ok",
            &[("5h", Some(0.1), false), ("7d", Some(0.3), false)],
        );
        let full = seat(
            false,
            "ok",
            &[("5h", Some(0.81), false), ("7d", Some(0.3), false)],
        );
        let week = seat(
            false,
            "ok",
            &[("5h", Some(0.1), false), ("7d", Some(0.9), false)],
        );
        let out = seat(false, "exhausted", &[]);
        let unread = seat(false, "unknown", &[]);
        let old = seat(
            false,
            "ok",
            &[("5h", Some(0.1), true), ("7d", Some(0.3), false)],
        );
        let select = |desk: &Value, selector: bool| {
            failover(
                Accounts::Select,
                "claude",
                "claude-opus-5-5",
                selector,
                desk,
            )
        };
        let with = |accounts: Value| desk(true, "automatic", read(accounts));
        // The modes that say it without a desk.
        let any = with(json!([in_use, spare]));
        assert_eq!(
            failover(Accounts::None, "claude", "", true, &any),
            Failover::None
        );
        assert_eq!(
            failover(Accounts::Follow, "claude", "", true, &any),
            Failover::Unknown
        );
        // A seat with room, and the switcher moves to it, or the pool's selector does (with a
        // selector the desk is on, whatever the switcher's lane says).
        assert_eq!(select(&any, false), Failover::Spare);
        assert_eq!(
            select(&desk(true, "notify", read(json!([in_use, spare]))), true),
            Failover::Spare
        );
        // Nothing moves the tool: none would, whatever seat has room.
        assert_eq!(
            select(
                &desk(false, "automatic", read(json!([in_use, spare]))),
                false
            ),
            Failover::None
        );
        assert_eq!(
            select(&desk(true, "notify", read(json!([in_use, spare]))), false),
            Failover::None
        );
        // No other seat has room: near its own share, out, or signed out.
        assert_eq!(
            select(&with(json!([in_use, full, week, out])), false),
            Failover::None
        );
        assert_eq!(select(&with(json!([in_use])), false), Failover::None);
        // One that cannot be judged: unknown, unless another has room.
        assert_eq!(
            select(&with(json!([in_use, full, unread])), false),
            Failover::Unknown
        );
        assert_eq!(
            select(&with(json!([in_use, old])), false),
            Failover::Unknown
        );
        assert_eq!(
            select(&with(json!([in_use, unread, spare])), false),
            Failover::Spare
        );
        // A reading that is old, failed or not there: unknown.
        let mut stale = read(json!([in_use, spare]));
        stale["stale"] = json!(true);
        assert_eq!(
            select(&desk(true, "automatic", stale), false),
            Failover::Unknown
        );
        let mut failed = read(json!([in_use, spare]));
        failed["read_state"] = json!("failed");
        assert_eq!(
            select(&desk(true, "automatic", failed), false),
            Failover::Unknown
        );
        assert_eq!(select(&json!({}), true), Failover::Unknown);
        // A reading of no account at all: no other seat.
        let mut empty = read(json!([]));
        empty["inventory"] = json!("empty");
        assert_eq!(
            select(&desk(true, "automatic", empty), false),
            Failover::None
        );
    }

    #[test]
    fn a_seat_whose_window_began_anew_since_it_was_read_has_room() {
        let in_use = seat(true, "ok", &[("5h", Some(0.85), false)]);
        // As the desk serves a window whose reset passed since it was read: no number, stale.
        let anew = json!({ "active_default": false, "state": "ok", "windows": [
            { "kind": "5h", "used_ratio": null, "stale": true, "resets_at": NOW - 1_000 },
            { "kind": "7d", "used_ratio": 0.3, "stale": false, "resets_at": NOW + 1_000_000 },
        ]});
        let mut served = desk(true, "automatic", read(json!([in_use, anew])));
        served["served_at"] = json!(NOW);
        let of = failover(Accounts::Select, "claude", "", false, &served);
        assert_eq!(of, Failover::Spare);
        // Not yet begun anew, and no number: it cannot be judged.
        served["served_at"] = json!(NOW - 2_000);
        let of = failover(Accounts::Select, "claude", "", false, &served);
        assert_eq!(of, Failover::Unknown);
    }

    #[test]
    fn a_seat_blocked_for_the_agents_model_takes_none_of_its_turns() {
        let in_use = seat(true, "ok", &[("5h", Some(0.85), false)]);
        let no_opus = seat(
            false,
            "ok",
            &[
                ("5h", Some(0.1), false),
                ("7d", Some(0.3), false),
                ("model:opus", Some(0.95), false),
            ],
        );
        let desk = desk(true, "automatic", read(json!([in_use, no_opus])));
        let of = |model: &str| failover(Accounts::Select, "claude", model, false, &desk);
        assert_eq!(of("claude-opus-5-5"), Failover::None);
        assert_eq!(of("claude-sonnet-5-5"), Failover::Spare);
        // A model of no family that is known: every family's window bears on it.
        assert_eq!(of(""), Failover::None);
        assert_eq!(of("some-model"), Failover::None);
    }

    #[test]
    fn the_row_says_whether_failover_was_known() {
        let near = nearest(
            "codex",
            [window(
                "five_hour",
                84.0,
                Some("2030-03-17T19:00:00Z"),
                true,
            )],
            NOW,
        )
        .unwrap();
        assert!(row(&near, Failover::Spare).is_null());
        assert_eq!(row(&near, Failover::None)["failover"], "none");
        let unknown = row(&near, Failover::Unknown);
        assert_eq!(unknown["failover"], "unknown");
        assert_eq!(unknown["used_percentage"], 84.0);
        // A reset the tool gave with parts of a second or another offset is written in UTC to
        // the second.
        let odd = window(
            "five_hour",
            84.0,
            Some("2030-03-17T21:00:00.597645+02:00"),
            true,
        );
        let near = nearest("claude", [odd], NOW).unwrap();
        assert_eq!(near["resets_at"], "2030-03-17T19:00:00Z");
    }
}
