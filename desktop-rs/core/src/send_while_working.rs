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

/// The Host's feature name in `/server-info` (`capabilities.features`).
pub const FEATURE: &str = "send_while_busy";
/// `GET` answers `{FIELD: value}`; `POST {FIELD: value}` changes it.
pub const PATH: &str = "/send-settings";
/// The setting's field, in the settings, the event and every `POST /send`
/// (the setting, or the one send's own choice from Send's menu).
pub const FIELD: &str = "while_busy";
/// The event every client is sent when it changes: `{type, FIELD}`.
pub const EVENT: &str = "send-settings-updated";
/// On a send's answer where the Host did otherwise than asked: `{from, to,
/// reason: not_steerable | not_words, text}`; `text` is shown as sent.
pub const FALLBACK_FIELD: &str = "fallback";

/// The setting's title (Settings › Chats).
pub const TITLE: &str = "Send while working";
/// Under its choices.
pub const FOOTER: &str = "If an agent can't take a message mid-turn, Steer now stops and sends instead.";
/// The waiting line's dim words before Stop and send when the Host gives no
/// reason of its own.
pub const CANT_STEER: &str = "Can't steer this turn";

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

    pub fn label(self) -> &'static str {
        match self {
            Mode::Steer => "Steer now",
            Mode::AfterTurn => "After the turn",
            Mode::StopAndSend => "Stop and send",
        }
    }

    /// One line about the choice.
    pub fn about(self) -> &'static str {
        match self {
            Mode::Steer => "Your message goes into the reply the agent is writing.",
            Mode::AfterTurn => "Your message waits until the agent is done.",
            Mode::StopAndSend => "The agent stops and answers your message.",
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
    Some(reason_line(text))
}

/// The dim line before Stop and send: the Host's reason on one line, or
/// `CANT_STEER`.
pub fn reason_line(text: &str) -> String {
    let line = text.split_whitespace().collect::<Vec<_>>().join(" ");
    if line.is_empty() { CANT_STEER.to_owned() } else { line }
}

/// What the waiting line says when its action failed: a title and why,
/// by the Host's refusal code where it gave one.
pub fn failure(steer: bool, code: &str) -> String {
    let title = if steer { "Couldn't steer it in" } else { "Couldn't send" };
    let why = match code {
        "not_steerable" => "This turn can't take it now. It waits for the next turn.",
        "turn_not_cuttable" => "This turn can't be stopped for it now. It waits for the next turn.",
        "left_for_later" => "It was sent to wait for later.",
        "not_the_owners" => "Only its sender can do that.",
        _ => "It waits for the next turn.",
    };
    format!("{title}. {why}")
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
        assert_eq!(fallback(&with_words).as_deref(), Some("This agent can't take a message mid-turn: stopped and sent"));
        let coded = object(json!({FALLBACK_FIELD: {"from": "steer", "to": "after_turn", "reason": "not_words"}}));
        assert_eq!(fallback(&coded).as_deref(), Some(CANT_STEER));
        assert_eq!(fallback(&object(json!({}))), None);
        assert_eq!(failure(true, "not_steerable"), "Couldn't steer it in. This turn can't take it now. It waits for the next turn.");
        assert_eq!(failure(false, ""), "Couldn't send. It waits for the next turn.");
    }
}
