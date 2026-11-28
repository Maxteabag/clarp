//! Triage of many chats: the explorer's Working, Needs you, Done and
//! Snoozed views, and snoozing a chat.
//!
//! A chat works while its agent works; it needs you when it has something
//! unread, waits for an answer or has a decision for you; otherwise it is
//! done. A snoozed chat is out of Needs you until its time comes (a timed
//! snooze) or its next message arrives (a snooze until the next message).
//! The Host keeps no snooze, so this device keeps them, in its settings
//! (`sidebar/snoozed`: session → `{until, message}`, `until` in epoch ms or
//! 0 for "until its next message", `message` the latest message's preview
//! when it was snoozed).

use std::collections::HashSet;

use chrono::{Duration, Local, TimeZone, Timelike};
use clarp_core::roster::AgentRow;
use clarp_engine::Engine;
use serde_json::{Map, Value, json};

use crate::App;

pub const SETTING: &str = "sidebar/snoozed";

/// The explorer's scopes this module filters by.
pub const SCOPES: [&str; 4] = ["working", "needs-you", "done", "snoozed"];

/// Where a chat stands.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    Working,
    NeedsYou,
    Done,
    Snoozed,
}

impl Kind {
    pub fn scope(self) -> &'static str {
        match self {
            Kind::Working => "working",
            Kind::NeedsYou => "needs-you",
            Kind::Done => "done",
            Kind::Snoozed => "snoozed",
        }
    }
}

/// One snooze as kept: until when (epoch ms; 0 for the next message) and
/// the latest message then.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Snooze {
    pub until: i64,
    pub message: String,
}

fn now_ms() -> i64 {
    chrono::Utc::now().timestamp_millis()
}

fn stored(engine: &Engine) -> Map<String, Value> {
    engine.settings().get(SETTING).and_then(Value::as_object).cloned().unwrap_or_default()
}

/// `session`'s snooze, if it holds: not yet due, and for a snooze until the
/// next message, no new message since.
pub fn snooze_of(engine: &Engine, row: &AgentRow) -> Option<Snooze> {
    let map = stored(engine);
    let entry = map.get(&row.session)?.as_object()?;
    let snooze = Snooze {
        until: entry.get("until").and_then(Value::as_i64).unwrap_or(0),
        message: entry.get("message").and_then(Value::as_str).unwrap_or_default().to_owned(),
    };
    holds(&snooze, &row.last_message, now_ms()).then_some(snooze)
}

/// Whether a snooze still holds at `now` for a chat whose latest message is
/// `message`.
pub fn holds(snooze: &Snooze, message: &str, now: i64) -> bool {
    if snooze.until > 0 { now < snooze.until } else { message == snooze.message }
}

/// Where `row` stands; `pending` holds the chats with a decision for you.
pub fn kind(row: &AgentRow, pending: &HashSet<String>, snoozed: bool) -> Kind {
    if row.busy {
        Kind::Working
    } else if snoozed {
        Kind::Snoozed
    } else if row.unread || row.state == "waiting" || pending.contains(&row.session) {
        Kind::NeedsYou
    } else {
        Kind::Done
    }
}

/// The chats with a decision or question waiting for you.
pub fn pending(engine: &Engine) -> HashSet<String> {
    engine.attention_items().iter().filter_map(|item| item.get("session").and_then(Value::as_str)).filter(|s| !s.is_empty()).map(str::to_owned).collect()
}

/// Whether `row` is in `scope` (a triage scope; any other scope keeps it).
/// Snoozed lists the snoozed chats, working or not.
pub fn in_scope(scope: &str, row: &AgentRow, pending: &HashSet<String>, snoozed: bool) -> bool {
    if !SCOPES.contains(&scope) {
        return true;
    }
    if row.is_helpers_chat() {
        return false;
    }
    if scope == "snoozed" {
        return snoozed;
    }
    kind(row, pending, snoozed).scope() == scope
}

/// What a snoozed row says after the "z": "next message", "14:30", or
/// "Thu 08:00" past today.
pub fn label(snooze: &Snooze) -> String {
    if snooze.until <= 0 {
        // i18n: chat list, after the snooze mark: the chat wakes at the agent's next message (lowercase)
        return clarp_core::tr!("next message");
    }
    let Some(at) = Local.timestamp_millis_opt(snooze.until).single() else { return String::new() };
    let tag = clarp_core::i18n::current();
    let time = clarp_core::i18n::format::time(tag, at.time(), clarp_core::i18n::format::Clock::H24);
    if at.date_naive() == Local::now().date_naive() {
        time
    } else {
        // i18n: a weekday and a clock time, "Tue 01:00"; {0} is the weekday, {1} the time
        clarp_core::tr!("{0} {1}", clarp_core::i18n::format::weekday(tag, at.date_naive()), time)
    }
}

/// When a snooze of `how` started now ends: "message" (0: its next
/// message), "hour", or "morning" (08:00 tomorrow).
pub fn until_for(how: &str) -> i64 {
    match how {
        "hour" => now_ms() + 3_600_000,
        "morning" => {
            let tomorrow = Local::now() + Duration::days(1);
            tomorrow
                .with_hour(8)
                .and_then(|t| t.with_minute(0))
                .and_then(|t| t.with_second(0))
                .and_then(|t| t.with_nanosecond(0))
                .map_or_else(|| now_ms() + 86_400_000, |t| t.timestamp_millis())
        }
        _ => 0,
    }
}

/// Snoozes `session` (`how` as `until_for`), or wakes it (`how` "wake").
pub fn set(app: &App, session: &str, how: &str) {
    if session.is_empty() || session.starts_with("pair:") {
        return;
    }
    {
        let mut engine = app.engine.borrow_mut();
        let message = engine.roster().rows().iter().find(|r| r.session == session).map(|r| r.last_message.clone()).unwrap_or_default();
        let mut map = stored(&engine);
        if how == "wake" {
            map.remove(session);
        } else {
            map.insert(session.to_owned(), json!({"until": until_for(how), "message": message}));
        }
        engine.settings_mut().set(SETTING, Value::Object(map));
    }
    app.refresh(&[clarp_engine::Change::Roster]);
}

/// Whether `session` is snoozed now.
pub fn is_snoozed(engine: &Engine, session: &str) -> bool {
    engine.roster().rows().iter().find(|r| r.session == session).is_some_and(|row| snooze_of(engine, row).is_some())
}

/// Once a second: a timed snooze that came due wakes its chat (the list
/// moves it back), and kept snoozes of chats that woke or left go.
pub fn tick(app: &App) {
    let due = {
        let Ok(engine) = app.engine.try_borrow() else { return };
        let map = stored(&engine);
        if map.is_empty() {
            return;
        }
        let rows = engine.roster().rows();
        let now = now_ms();
        map.iter()
            .filter(|(session, entry)| {
                let snooze = Snooze {
                    until: entry.get("until").and_then(Value::as_i64).unwrap_or(0),
                    message: entry.get("message").and_then(Value::as_str).unwrap_or_default().to_owned(),
                };
                // A chat the roster no longer lists keeps its snooze until
                // the roster has loaded (an empty roster is not "gone").
                match rows.iter().find(|r| &r.session == *session) {
                    Some(row) => !holds(&snooze, &row.last_message, now),
                    None => snooze.until > 0 && now >= snooze.until,
                }
            })
            .map(|(session, _)| session.clone())
            .collect::<Vec<_>>()
    };
    if due.is_empty() {
        return;
    }
    if let Ok(mut engine) = app.engine.try_borrow_mut() {
        let mut map = stored(&engine);
        for session in &due {
            map.remove(session);
        }
        engine.settings_mut().set(SETTING, Value::Object(map));
    } else {
        return;
    }
    app.refresh(&[clarp_engine::Change::Roster]);
}

/// The counts the scope chips show: (working, needs you, snoozed), top
/// chats only.
pub fn counts(engine: &Engine) -> (i32, i32, i32) {
    let pending = pending(engine);
    let mut counts = (0, 0, 0);
    for row in engine.roster().rows().iter().filter(|r| !r.is_helpers_chat()) {
        let snoozed = snooze_of(engine, row).is_some();
        match kind(row, &pending, snoozed) {
            Kind::Working => counts.0 += 1,
            Kind::NeedsYou => counts.1 += 1,
            _ => {}
        }
        if snoozed {
            counts.2 += 1;
        }
    }
    counts
}

#[cfg(test)]
mod tests {
    use super::*;

    fn row(session: &str) -> AgentRow {
        AgentRow { session: session.into(), ..AgentRow::default() }
    }

    #[test]
    fn a_chat_works_needs_you_or_is_done_and_a_snooze_takes_it_out_of_needs_you() {
        let none = HashSet::new();
        let working = AgentRow { busy: true, unread: true, ..row("a") };
        assert_eq!(kind(&working, &none, false), Kind::Working);
        assert_eq!(kind(&working, &none, true), Kind::Working, "a snoozed chat that works still works");
        let unread = AgentRow { unread: true, ..row("b") };
        assert_eq!(kind(&unread, &none, false), Kind::NeedsYou);
        assert_eq!(kind(&unread, &none, true), Kind::Snoozed);
        let waiting = AgentRow { state: "waiting".into(), ..row("c") };
        assert_eq!(kind(&waiting, &none, false), Kind::NeedsYou);
        let decided: HashSet<String> = ["d".to_owned()].into();
        assert_eq!(kind(&row("d"), &decided, false), Kind::NeedsYou);
        assert_eq!(kind(&row("e"), &none, false), Kind::Done);
        assert!(in_scope("all", &row("e"), &none, false));
        assert!(in_scope("done", &row("e"), &none, false));
        assert!(!in_scope("needs-you", &unread, &none, true));
        assert!(in_scope("snoozed", &working, &none, true));
    }

    #[test]
    fn a_snooze_holds_until_its_time_or_its_next_message() {
        let timed = Snooze { until: 1_000, message: "old".into() };
        assert!(holds(&timed, "new", 999), "a timed snooze ignores messages");
        assert!(!holds(&timed, "old", 1_000));
        let next = Snooze { until: 0, message: "old".into() };
        assert!(holds(&next, "old", i64::MAX));
        assert!(!holds(&next, "new", 0));
        assert_eq!(label(&next), "next message");
        assert!(until_for("hour") > now_ms());
        assert!(until_for("morning") > now_ms());
        assert_eq!(until_for("message"), 0);
    }
}
