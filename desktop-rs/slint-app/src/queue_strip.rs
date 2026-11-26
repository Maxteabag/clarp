//! The open chat's waiting messages above its composer as one line
//! ("1 waiting · Steer now", `clarp_core::queue_line`), and the composer
//! frame's context and quota meter.
//!
//! The line reads the open chat's queue (`GET /turn-queue`): whenever the
//! chat has waiting messages this loads it, the engine follows it on
//! `queue-updated`, and it is read again every 30 seconds while something
//! waits; the Host's answer wins over a count an event left behind. Its
//! action reads the messages into the running turn, or stops the turn for
//! them, on a Host with `queue_steer` (the engine tries the next way when
//! the Host refuses one), else `POST /turn-queue/<id>/send`; what could not
//! be sent says why under it. Settings and Ctrl+K hide it (Show queue).

use std::cell::RefCell;

use clarp_core::json;
use clarp_engine::Engine;
use serde_json::Value;

use crate::{App, WaitingTurn};

/// Whether the waiting line shows at all (Settings "Show the queue", Ctrl+K
/// "Hide queue"). Off, the composer's count line alone says what waits.
pub const SETTING: &str = "composer/showQueue";

pub fn shown(engine: &Engine) -> bool {
    engine.settings().boolean(SETTING, true)
}

/// Shows or hides the strip in every chat.
pub fn set_shown(app: &App, on: bool) {
    app.engine.borrow_mut().settings_mut().set(SETTING, on);
    app.refresh(&[clarp_engine::Change::Panes]);
}

thread_local! {
    /// The chat and count the strip last loaded the queue for.
    static FOLLOWED: RefCell<(String, i32)> = RefCell::new((String::new(), 0));
    /// When the strip last asked the Host for the queue.
    static LAST_READ: std::cell::Cell<Option<std::time::Instant>> = const { std::cell::Cell::new(None) };
}

fn item_id(item: &Value) -> String {
    let id = item.get("queue_id").and_then(Value::as_str).unwrap_or_default();
    if id.is_empty() { item.get("id").and_then(Value::as_str).unwrap_or_default().to_owned() } else { id.to_owned() }
}

/// `session`'s waiting messages as the strip lists them, once its queue
/// is the loaded one (else none: the composer's count line stands in).
pub fn waiting(engine: &Engine, session: &str) -> Vec<WaitingTurn> {
    // Steer and take over need the Host's `queue_steer` and a running turn.
    let steering = engine.turn_queue_steer(session) && engine.roster().find(session).is_some_and(|a| a.busy);
    engine
        .turn_queue(session)
        .iter()
        .filter(|item| !item_id(item).is_empty())
        .map(|item| {
            let text = item.as_object().map(|o| json::string(o, "text")).unwrap_or_default();
            // Only a message that met the busy agent may steer; one its
            // sender asked to wait (`kind: later`) is left for later.
            let waiting_kind = item.get("kind").and_then(Value::as_str).is_none_or(|k| k == "waiting");
            // Both act on the owner's own messages only (origin user or
            // oracle); another sender's keeps the plain Send now.
            let owners = item.get("origin").and_then(Value::as_str).is_none_or(|o| o == "user" || o == "oracle");
            WaitingTurn {
                id: item_id(item).into(),
                text: text.lines().map(str::trim).find(|l| !l.is_empty()).unwrap_or_default().into(),
                reading: item.get("status").and_then(Value::as_str) == Some("started"),
                steer: steering && owners && waiting_kind && item.get("steerable").and_then(Value::as_bool) == Some(true),
                take_over: steering && owners,
            }
        })
        .collect()
}

/// The waiting line (#920, the same in every app): shown while a turn runs
/// and some of the user's own messages wait. Its count, its action ("steer"
/// or "stop"; "" when the line is not shown), the message Steer now reads in
/// (the newest of the user's own `kind: waiting` messages the turn can
/// take; the Host reads the older ones in first), its word, the dim reason
/// before Stop and send, and whether the action runs now.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Chip {
    pub kind: &'static str,
    pub target: String,
    pub label: &'static str,
    pub count: i32,
    pub reason: String,
    pub acting: bool,
}

/// The user's own waiting message (origin user or oracle, or none said),
/// not yet being read into the turn.
fn own_waiting(item: &Value) -> bool {
    let owners = item.get("origin").and_then(Value::as_str).is_none_or(|o| o == "user" || o == "oracle");
    owners && item.get("status").and_then(Value::as_str) != Some("started") && !item_id(item).is_empty()
}

pub fn chip(engine: &Engine, session: &str) -> Chip {
    use clarp_core::{queue_line as words, send_while_working as sww};
    if !engine.roster().find(session).is_some_and(|a| a.busy) {
        return Chip::default();
    }
    let items = engine.turn_queue(session);
    let own: Vec<&Value> = items.iter().filter(|item| own_waiting(item)).collect();
    if own.is_empty() {
        return Chip::default();
    }
    let acting = engine.turn_queue_acting(session) || engine.stopping_for_waiting(session);
    let count = own.len() as i32;
    let steerable = |item: &&&Value| {
        item.get("kind").and_then(Value::as_str).is_none_or(|k| k == "waiting") && item.get("steerable").and_then(Value::as_bool) == Some(true)
    };
    if engine.turn_queue_steer(session)
        && let Some(item) = own.iter().rev().find(steerable)
    {
        return Chip { kind: "steer", target: item_id(item), label: words::STEER_NOW, count, reason: String::new(), acting };
    }
    let reason = sww::reason_line(&engine.send_while_working_reason(session));
    Chip { kind: "stop", target: String::new(), label: words::STOP_AND_SEND, count, reason, acting }
}

/// Sends a one-off choice's words for `session`'s Host into Send's menu:
/// the two choices other than its setting, while it offers the setting.
pub fn fill_send_choices(engine: &Engine, session: &str, window: &crate::AppWindow) {
    use clarp_core::send_while_working::Mode;
    use slint::ComponentHandle;
    let options: Vec<crate::SendOption> = if !session.is_empty() && engine.send_while_working_offered_for(session) {
        engine
            .send_while_working_for(session)
            .others()
            .iter()
            .map(|m: &Mode| crate::SendOption { value: m.wire().into(), label: m.label().into(), about: m.about().into() })
            .collect()
    } else {
        Vec::new()
    };
    window.global::<crate::SendChoice>().set_options(slint::ModelRc::new(slint::VecModel::from(options)));
}

/// Once a second with the turn clock: the open chat's queue is read again
/// from the Host every 30 seconds while anything of it waits, so an entry
/// already sent or cleared never lingers.
pub fn tick(app: &App) {
    // `follow` reads it again once its last read is 30 seconds old; the
    // clock makes sure that happens with nothing else to refresh the window.
    follow(app);
}

/// The first message of `session`'s that Send now can still hand over.
pub fn first_waiting(engine: &Engine, session: &str) -> Option<String> {
    first(engine, session).map(|w| w.id.to_string())
}

fn first(engine: &Engine, session: &str) -> Option<WaitingTurn> {
    waiting(engine, session).into_iter().find(|w| !w.reading)
}

/// Loads the open chat's queue while it has waiting messages, again when
/// their count changes. The queue dialog, while open, keeps the chat it
/// shows.
pub fn follow(app: &App) {
    if app.overlay.borrow().as_str() == crate::agent_dialogs_view::QUEUE {
        return;
    }
    let (session, count, loaded) = {
        let Ok(engine) = app.engine.try_borrow() else { return };
        let session = engine.selected_session().to_owned();
        let count = engine.queue_count(&session);
        (session, count, engine.turn_queue_session().to_owned())
    };
    if session.is_empty() || session.starts_with("pair:") || count == 0 {
        FOLLOWED.with(|f| *f.borrow_mut() = (String::new(), 0));
        return;
    }
    let key = (session.clone(), count);
    // Read again when the count changes, and every half minute while
    // anything shows, so the strip follows the Host even when an event
    // that emptied it never came.
    let fresh = LAST_READ.with(|t| t.get().is_some_and(|at| at.elapsed() < std::time::Duration::from_secs(clarp_core::queue_line::REREAD_SECS)));
    if loaded == session && FOLLOWED.with(|f| *f.borrow() == key) && fresh {
        return;
    }
    LAST_READ.with(|t| t.set(Some(std::time::Instant::now())));
    FOLLOWED.with(|f| *f.borrow_mut() = key);
    if let Ok(mut engine) = app.engine.try_borrow_mut() {
        engine.load_turn_queue(&session);
    }
}

/// Does the line's action for the open chat's waiting messages (by key:
/// Ctrl+Shift+Enter): Steer now, or the plain Stop. False when the line
/// offers nothing, or its action already runs.
pub fn send_first(app: &App) -> bool {
    let (session, chip) = {
        let engine = app.engine.borrow();
        let session = engine.selected_session().to_owned();
        let chip = chip(&engine, &session);
        (session, chip)
    };
    act(app, &session, &chip.kind.to_owned(), &chip.target)
}

/// The line's action for `session`: Steer now (`target`) or Stop and send.
pub fn act(app: &App, session: &str, kind: &str, target: &str) -> bool {
    if session.is_empty() || kind.is_empty() {
        return false;
    }
    let mut engine = app.engine.borrow_mut();
    if chip(&engine, session).acting {
        return true;
    }
    match kind {
        "steer" => engine.steer_from_line(target),
        _ => engine.stop_for_waiting(session),
    }
    true
}

/// Tokens as the meter says them: 950, 21K, 1.2M, 1M.
fn tokens(count: i64) -> String {
    let count = count.max(0);
    let short = |value: f64, unit: &str| {
        let rounded = (value * 10.0).round() / 10.0;
        if rounded >= 10.0 || rounded.fract() == 0.0 { format!("{}{unit}", rounded.round() as i64) } else { format!("{rounded:.1}{unit}") }
    };
    if count >= 1_000_000 {
        short(count as f64 / 1_000_000.0, "M")
    } else if count >= 1_000 {
        short(count as f64 / 1_000.0, "K")
    } else {
        count.to_string()
    }
}

/// The meter's words and whether they warn: the account's limit first
/// when near or used up, then the context ("context 21K/1M · 2%"),
/// which warns from 80% (compaction is close). "" when neither is known.
pub fn meter_text(context_tokens: Option<i64>, context_window: i64, quota: Option<(String, bool)>) -> (String, bool) {
    let mut parts = Vec::new();
    let mut warn = false;
    if let Some((text, _)) = quota {
        parts.push(text);
        warn = true;
    }
    match context_tokens {
        // Unknown usage (null) shows nothing, never "0" (#403).
        None => {}
        Some(used) if context_window > 0 => {
            let fraction = used as f64 / context_window as f64;
            warn |= fraction >= 0.8;
            parts.push(format!("context {}/{} · {}%", tokens(used), tokens(context_window), (fraction * 100.0).round().clamp(0.0, 100.0) as i64));
        }
        Some(used) => parts.push(format!("context {}", tokens(used))),
    }
    (parts.join("  ·  "), warn)
}

/// The composer meter for `session`'s agent.
pub fn meter(engine: &Engine, session: &str) -> (String, bool) {
    let Some(agent) = engine.roster().find(session) else { return (String::new(), false) };
    meter_text(agent.context_tokens, agent.context_window, agent.quota_meter(chrono::Utc::now()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_meter_says_context_and_a_limit_and_hides_when_unknown() {
        assert_eq!(meter_text(None, 1_000_000, None), (String::new(), false));
        assert_eq!(meter_text(Some(21_400), 1_000_000, None), ("context 21K/1M · 2%".to_owned(), false));
        assert_eq!(meter_text(Some(170_000), 200_000, None), ("context 170K/200K · 85%".to_owned(), true));
        assert_eq!(meter_text(Some(950), 0, None), ("context 950".to_owned(), false));
        assert_eq!(
            meter_text(Some(1_250_000), 2_000_000, Some(("Claude 86% used".to_owned(), false))),
            ("Claude 86% used  ·  context 1.3M/2M · 63%".to_owned(), true)
        );
        assert_eq!(meter_text(None, 0, Some(("Codex limit reached".to_owned(), true))), ("Codex limit reached".to_owned(), true));
    }
}
