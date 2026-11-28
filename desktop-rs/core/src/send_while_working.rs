//! Send while working (issue 920): what Send does while the agent is
//! working, one setting kept on the Host so every app shows and uses the
//! same: Steer now (the default), After the turn, or Stop and send. The
//! Host applies it to a send that arrives during a turn; where the turn
//! cannot be steered it stops and sends instead, and gives a short reason
//! the waiting line shows. Send's menu offers the two other choices for one
//! message.
//!
//! The Host's contract is #920's (Maximus, 26 Nov 2026): each name is one
//! constant here. Only a Host that lists `FEATURE` in `/server-info` is
//! read or sent `while_busy`; an older one shows no setting and sends as
//! before. Every word lives here, the same in the iOS and Android apps.

use serde_json::Value;

use crate::json::Object;
use crate::tr;

// i18n-screen: send while working

/// The Host's feature name in `/server-info` (`capabilities.features`).
pub const FEATURE: &str = "send_while_busy";
/// `GET` answers `{FIELD: value}`; `POST {FIELD: value}` changes it.
pub const PATH: &str = "/send-settings";
/// The setting's field, in the settings, the event and every `POST /send`
/// (the setting, or the one send's own choice from Send's menu).
pub const FIELD: &str = "while_busy";
/// The event every client is sent when it changes: `{type, FIELD}`.
pub const EVENT: &str = "send-settings-updated";
/// Only on a send's answer, where the Host did otherwise than asked: `{from,
/// to, reason: not_steerable | not_words | turn_not_cuttable, text}`;
/// `text` is shown as sent. The answer's `FIELD` says what was done.
pub const FALLBACK_FIELD: &str = "fallback";

/// The setting's title (Settings › Chats).
pub const TITLE: &str = "Send while working";
/// Under its choices.
pub const FOOTER: &str = "Stops and sends if steering is unavailable.";
/// The waiting line's dim words before Stop and send when the Host gives no
/// reason of its own.
pub const CANT_STEER: &str = "Can't steer this turn";

/// `TITLE` in the app's language.
pub fn title() -> String {
    // i18n: Settings > Chats, row title: what Send does while the agent's turn runs
    tr!("Send while working")
}

/// `FOOTER` in the app's language.
pub fn footer() -> String {
    // i18n: Settings > Chats, note under the Send while working choices
    tr!("Stops and sends if steering is unavailable.")
}

/// `CANT_STEER` in the app's language.
pub fn cant_steer() -> String {
    // i18n: chat, waiting message strip: dim words before the Stop and send button when the running turn cannot take the message
    tr!("Can't steer this turn")
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum Mode {
    /// Read the message into the running turn.
    #[default]
    Steer,
    /// The message waits until the turn has ended.
    AfterTurn,
    /// Stop the turn and send the message.
    StopAndSend,
}

impl Mode {
    pub const ALL: [Mode; 3] = [Mode::Steer, Mode::AfterTurn, Mode::StopAndSend];

    /// The Host's value.
    pub fn wire(self) -> &'static str {
        match self {
            Mode::Steer => "steer",
            Mode::AfterTurn => "after_turn",
            Mode::StopAndSend => "take_over",
        }
    }

    pub fn from_wire(value: &str) -> Option<Mode> {
        Mode::ALL.into_iter().find(|m| m.wire() == value)
    }

    pub fn label(self) -> String {
        match self {
            // i18n: Settings > Chats > Send while working, and Send's menu during a turn: choice that reads the message into the running turn now (imperative)
            Mode::Steer => tr!("Steer now"),
            // i18n: Settings > Chats > Send while working, and Send's menu during a turn: choice that sends the message once the turn has ended
            Mode::AfterTurn => tr!("After the turn"),
            // i18n: Settings > Chats > Send while working, and Send's menu during a turn: choice that stops the turn, then sends (imperative)
            Mode::StopAndSend => tr!("Stop and send"),
        }
    }

    /// One line about the choice.
    pub fn about(self) -> String {
        match self {
            // i18n: description under the Steer now choice: the message joins the running turn
            Mode::Steer => tr!("Joins current turn."),
            // i18n: description under the After the turn choice: the message waits until the turn ends
            Mode::AfterTurn => tr!("Waits for current turn."),
            // i18n: description under the Stop and send choice: the turn is stopped and the message sent
            Mode::StopAndSend => tr!("Stops turn and sends."),
        }
    }

    /// The two other choices, for one message (Send's menu during a turn).
    pub fn others(self) -> [Mode; 2] {
        match self {
            Mode::Steer => [Mode::AfterTurn, Mode::StopAndSend],
            Mode::AfterTurn => [Mode::Steer, Mode::StopAndSend],
            Mode::StopAndSend => [Mode::Steer, Mode::AfterTurn],
        }
    }
}

/// Whether a `/server-info` answer offers the setting.
pub fn offered_by(info: &Object) -> bool {
    info.get("capabilities")
        .and_then(|c| c.get("features"))
        .and_then(Value::as_array)
        .is_some_and(|features| features.iter().any(|f| f.as_str() == Some(FEATURE)))
}

/// The setting in the settings, the event or a write's answer; None when it
/// says none or a value this app does not know.
pub fn read(object: &Object) -> Option<Mode> {
    object.get(FIELD).and_then(Value::as_str).and_then(Mode::from_wire)
}

/// The words for a fallback the Host reported (`FALLBACK_FIELD`): its own
/// text, one line, as it was sent, else `CANT_STEER`. None when it
/// reported none.
pub fn fallback(object: &Object) -> Option<String> {
    let found = object.get(FALLBACK_FIELD)?.as_object()?;
    let text = found.get("text").and_then(Value::as_str).unwrap_or_default();
    if text.trim().is_empty() { None } else { Some(text.to_owned()) }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn object(value: Value) -> Object {
        value.as_object().cloned().unwrap()
    }

    #[test]
    fn modes_round_trip_and_default_to_steer() {
        for mode in Mode::ALL {
            assert_eq!(Mode::from_wire(mode.wire()), Some(mode));
            assert!(!mode.others().contains(&mode));
        }
        assert_eq!(Mode::default(), Mode::Steer);
        assert_eq!(Mode::from_wire("later"), None);
        assert_eq!(read(&object(json!({FIELD: "after_turn"}))), Some(Mode::AfterTurn));
        assert_eq!(read(&object(json!({"type": EVENT, FIELD: "take_over"}))), Some(Mode::StopAndSend));
        assert_eq!(read(&object(json!({FIELD: "stop_and_send"}))), None);
        assert_eq!(read(&object(json!({}))), None);
    }

    #[test]
    fn the_feature_gates_it() {
        assert!(offered_by(&object(json!({"capabilities": {"features": ["queue_steer", "send_while_busy"]}}))));
        assert!(!offered_by(&object(json!({"capabilities": {"features": ["queue_steer"]}}))));
    }

    #[test]
    fn a_fallback_says_the_hosts_reason_or_cant_steer() {
        let with_words = object(json!({FALLBACK_FIELD: {"from": "steer", "to": "take_over", "reason": "not_steerable", "text": "This agent can't take a message\nmid-turn: stopped and sent"}}));
        assert_eq!(fallback(&with_words).as_deref(), Some("This agent can't take a message\nmid-turn: stopped and sent"));
        let coded = object(json!({FALLBACK_FIELD: {"from": "steer", "to": "after_turn", "reason": "not_words"}}));
        assert_eq!(fallback(&coded), None);
        assert_eq!(fallback(&object(json!({}))), None);
    }
}
