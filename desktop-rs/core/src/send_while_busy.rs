//! What Send does while the agent works (feature `send_while_busy`, issue
//! #920): the owner's setting, the value a user's send carries, what the
//! Host says it did instead (the fallback line), and the chip above the
//! message field ("1 waiting · Steer now").
//!
//! The setting is the Host's (`GET`/`POST /send-settings`, followed on
//! `send-settings-updated`), the same in every app. Every send the user
//! makes carries it as `while_busy`; Ctrl+Enter (wait for the turn) carries
//! `after_turn` for that one send. An older Host has no setting: sends
//! carry nothing and no chip shows.

use serde_json::Value;

use crate::json::{self, Object};

/// The feature in `/server-info`.
pub const FEATURE: &str = "send_while_busy";

/// What Send does while the agent works.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum WhileBusy {
    /// Read into the running turn (the default); where the agent cannot
    /// take words mid-turn the Host stops the turn and sends instead.
    #[default]
    Steer,
    /// Wait for the running turn and run next.
    AfterTurn,
    /// Stop the running turn now and run the message next.
    TakeOver,
}

impl WhileBusy {
    pub const ALL: [WhileBusy; 3] = [WhileBusy::Steer, WhileBusy::AfterTurn, WhileBusy::TakeOver];

    /// The Host's word for it.
    pub fn as_str(self) -> &'static str {
        match self {
            WhileBusy::Steer => "steer",
            WhileBusy::AfterTurn => "after_turn",
            WhileBusy::TakeOver => "take_over",
        }
    }

    pub fn parse(value: &str) -> Option<Self> {
        Self::ALL.into_iter().find(|mode| mode.as_str() == value)
    }

    /// As the setting lists it.
    pub fn label(self) -> &'static str {
        match self {
            WhileBusy::Steer => "Steer now",
            WhileBusy::AfterTurn => "After the turn",
            WhileBusy::TakeOver => "Stop and send",
        }
    }

    pub fn about(self) -> &'static str {
        match self {
            WhileBusy::Steer => "Reads your message into the running turn; where the agent can't take it mid-turn, stops the turn and sends it",
            WhileBusy::AfterTurn => "Waits for the running turn to end, then runs your message",
            WhileBusy::TakeOver => "Stops the running turn and runs your message now",
        }
    }
}

/// The setting in a `/send-settings` answer or a `send-settings-updated`
/// event; None for a value this app does not know.
pub fn setting(object: &Object) -> Option<WhileBusy> {
    WhileBusy::parse(&json::string(object, "while_busy"))
}

/// What a user's send carries: Ctrl+Enter waits for the turn whatever the
/// setting says.
pub fn send_value(setting: WhileBusy, queue_if_busy: bool) -> WhileBusy {
    if queue_if_busy { WhileBusy::AfterTurn } else { setting }
}

/// The Host did something else than asked (`fallback` in the send's
/// answer): what it did, why, and its line for the chip.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Fallback {
    pub from: String,
    pub to: String,
    pub reason: String,
    pub text: String,
}

/// The fallback in a send's answer, its line never empty: the Host's own
/// words, else said here from where it went and why.
pub fn fallback(answer: &Object) -> Option<Fallback> {
    let object = answer.get("fallback")?.as_object()?;
    let (from, to, reason) = (json::string(object, "from"), json::string(object, "to"), json::string(object, "reason"));
    let mut text = json::string(object, "text").trim().to_owned();
    if text.is_empty() {
        text = match (to.as_str(), reason.as_str()) {
            (_, "not_words") => "Files and / commands wait for the turn to end".to_owned(),
            ("take_over", _) => "This agent can't take a message mid-turn: stopped and sent".to_owned(),
            ("after_turn", _) => "Waits for the turn to end".to_owned(),
            _ => return None,
        };
    }
    Some(Fallback { from, to, reason, text })
}

/// The chip's button: read the message into the turn, or stop the turn
/// and run it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ChipAction {
    /// `POST /turn-queue/<id>/steer`.
    Steer(String),
    /// `POST /turn-queue/<id>/take-over`.
    TakeOver(String),
}

/// The chip above the message field while the agent works with messages
/// waiting: how many, and what its button does (None: the count alone).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Chip {
    pub count: usize,
    pub action: Option<ChipAction>,
}

fn item_id(item: &Value) -> &str {
    item.get("queue_id").and_then(Value::as_str).filter(|id| !id.is_empty()).or_else(|| item.get("id").and_then(Value::as_str)).unwrap_or_default()
}

/// The chip for a chat's queue (`GET /turn-queue` items): shown while the
/// agent works and messages wait (`kind: waiting`; one its sender asked to
/// keep for later is not counted). The button acts on the first of the
/// owner's own waiting messages not already being read: Steer now where it
/// is `steerable`, else Stop and send; none where the Host offers neither
/// (`queue_steer`).
pub fn chip(busy: bool, steer_offered: bool, items: &[Value]) -> Option<Chip> {
    if !busy {
        return None;
    }
    let waiting: Vec<&Value> = items
        .iter()
        .filter(|item| !item_id(item).is_empty() && item.get("kind").and_then(Value::as_str).is_none_or(|k| k == "waiting"))
        .collect();
    if waiting.is_empty() {
        return None;
    }
    let action = steer_offered
        .then(|| {
            waiting.iter().find(|item| {
                item.get("status").and_then(Value::as_str) != Some("started")
                    && item.get("origin").and_then(Value::as_str).is_none_or(|o| o == "user" || o == "oracle")
            })
        })
        .flatten()
        .map(|item| {
            let id = item_id(item).to_owned();
            if item.get("steerable").and_then(Value::as_bool) == Some(true) { ChipAction::Steer(id) } else { ChipAction::TakeOver(id) }
        });
    Some(Chip { count: waiting.len(), action })
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn object(value: Value) -> Object {
        value.as_object().unwrap().clone()
    }

    #[test]
    fn the_setting_reads_the_hosts_three_words_and_nothing_else() {
        assert_eq!(WhileBusy::default(), WhileBusy::Steer);
        for mode in WhileBusy::ALL {
            assert_eq!(WhileBusy::parse(mode.as_str()), Some(mode));
        }
        assert_eq!(setting(&object(json!({"while_busy": "after_turn"}))), Some(WhileBusy::AfterTurn));
        assert_eq!(setting(&object(json!({"while_busy": "interrupt"}))), None);
        assert_eq!(setting(&object(json!({}))), None);
        assert_eq!(WhileBusy::TakeOver.label(), "Stop and send");
    }

    #[test]
    fn ctrl_enter_waits_for_the_turn_whatever_the_setting() {
        assert_eq!(send_value(WhileBusy::Steer, false), WhileBusy::Steer);
        assert_eq!(send_value(WhileBusy::TakeOver, false), WhileBusy::TakeOver);
        assert_eq!(send_value(WhileBusy::TakeOver, true), WhileBusy::AfterTurn);
        assert_eq!(send_value(WhileBusy::Steer, true), WhileBusy::AfterTurn);
    }

    #[test]
    fn the_fallback_line_is_the_hosts_else_said_here() {
        assert_eq!(fallback(&object(json!({"while_busy": "steer"}))), None);
        let own = fallback(&object(json!({"while_busy": "take_over", "fallback": {"from": "steer", "to": "take_over", "reason": "not_steerable", "text": "Stopped and sent "}}))).unwrap();
        assert_eq!(own.text, "Stopped and sent");
        assert_eq!(own.to, "take_over");
        let said = fallback(&object(json!({"fallback": {"from": "steer", "to": "take_over", "reason": "not_steerable"}}))).unwrap();
        assert_eq!(said.text, "This agent can't take a message mid-turn: stopped and sent");
        let words = fallback(&object(json!({"fallback": {"from": "steer", "to": "after_turn", "reason": "not_words", "text": ""}}))).unwrap();
        assert_eq!(words.text, "Files and / commands wait for the turn to end");
        assert_eq!(fallback(&object(json!({"fallback": {"from": "steer", "to": "elsewhere"}}))), None);
    }

    #[test]
    fn the_chip_counts_waiting_messages_while_the_agent_works() {
        let items = vec![
            json!({"queue_id": "q1", "kind": "waiting", "steerable": true, "origin": "user"}),
            json!({"queue_id": "q2", "kind": "later", "steerable": true}),
            json!({"queue_id": "q3", "kind": "waiting", "steerable": false}),
        ];
        assert_eq!(chip(false, true, &items), None, "an idle agent shows no chip");
        assert_eq!(chip(true, true, &[]), None);
        assert_eq!(chip(true, true, &items), Some(Chip { count: 2, action: Some(ChipAction::Steer("q1".into())) }));
        assert_eq!(chip(true, false, &items), Some(Chip { count: 2, action: None }), "no queue_steer: the count alone");
    }

    #[test]
    fn the_chips_button_stops_and_sends_what_cannot_be_steered_and_skips_what_is_read() {
        let items = vec![
            json!({"queue_id": "q1", "kind": "waiting", "steerable": true, "status": "started"}),
            json!({"queue_id": "q2", "kind": "waiting", "steerable": true, "origin": "agent"}),
            json!({"id": "q3", "kind": "waiting", "steerable": false, "origin": "user"}),
        ];
        assert_eq!(chip(true, true, &items), Some(Chip { count: 3, action: Some(ChipAction::TakeOver("q3".into())) }));
    }
}
