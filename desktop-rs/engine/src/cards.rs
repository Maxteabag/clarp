//! What the decision cards need from the Host beyond resolve and dismiss:
//! which of the newer card features it offers (an answer held for undo, an
//! own answer on an approval, matching a spoken answer), taking an answer
//! back, and asking what a spoken answer means.

use std::collections::{HashMap, HashSet};

use clarp_core::json::Object;
use serde_json::{Value, json};

use crate::{Change, Engine};

/// The Host holds an answer for undo (`undo_ms` on resolve, `/undo`).
pub const UNDO_FEATURE: &str = "decision_undo";
/// An approval takes the user's own words (`answer.text`).
pub const APPROVAL_TEXT_FEATURE: &str = "approval_custom_text";
/// `/decisions/{id}/match` says what a spoken answer means.
pub const VOICE_MATCH_FEATURE: &str = "decision_voice_match";

/// How long an answer stays takeable back, as the cards ask for it.
pub const UNDO_MS: i64 = 6000;

/// Which card features the Host offers.
#[derive(Debug, Default, Clone, Copy, PartialEq, Eq)]
pub struct CardFeatures {
    pub undo: bool,
    pub approval_text: bool,
    pub voice_match: bool,
}

/// How an undo went, for the card that asked.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum UndoOutcome {
    /// The decision is open again.
    Undone,
    /// The agent started on the answer, or it was delivered.
    TooLate,
    Failed(String),
}

#[derive(Default)]
pub(crate) struct Cards {
    features: CardFeatures,
    /// decision id -> the revision its answer left it at (the resolve's
    /// reply), which an undo is sent against.
    answered_revision: HashMap<String, i64>,
    /// Undos asked for while their answer was still on its way, with the
    /// revision the card expected the answer to leave.
    undo_owed: HashMap<String, i64>,
    /// `artifact|decision` while its undo is in flight: one at a time.
    undoing: HashSet<String>,
    /// artifact id -> how its latest undo went, until the card takes it.
    undo_outcomes: HashMap<String, UndoOutcome>,
    /// artifact id -> what the Host made of a spoken answer, or why not.
    matches: HashMap<String, Result<Value, String>>,
}

fn has(info: &Object, feature: &str) -> bool {
    info.get("capabilities")
        .and_then(|c| c.get("features"))
        .and_then(Value::as_array)
        .is_some_and(|features| features.iter().any(|f| f.as_str() == Some(feature)))
}

/// The features a Host's `/server-info` offers.
pub fn offered_by(info: &Object) -> CardFeatures {
    CardFeatures { undo: has(info, UNDO_FEATURE), approval_text: has(info, APPROVAL_TEXT_FEATURE), voice_match: has(info, VOICE_MATCH_FEATURE) }
}

fn decision_path(decision_id: &str, action: &str) -> String {
    format!("/decisions/{}/{action}", clarp_core::endpoint::percent_encode_segment(decision_id))
}

/// The id a reply's artifact is listed under: its own, else the one the
/// request was made for (an inbox item without an artifact id).
fn listed_id(item: &Value, asked: &str) -> String {
    item.get("artifact_id").and_then(Value::as_str).filter(|id| !id.is_empty()).unwrap_or(asked).to_owned()
}

/// The revision a resolve's reply says the decision is at now.
fn reply_revision(object: &Object) -> Option<i64> {
    let artifact = object.get("artifact");
    [
        object.get("decision").and_then(|d| d.get("revision")),
        artifact.and_then(|a| a.get("decision")).and_then(|d| d.get("revision")),
        object.get("revision"),
    ]
    .into_iter()
    .flatten()
    .find_map(Value::as_i64)
}

impl Engine {
    pub(crate) fn cards_server_info(&mut self, info: &Object) {
        let features = offered_by(info);
        if features != self.cards.features {
            self.cards.features = features;
            self.changes.push(Change::Updates);
        }
    }

    /// This Host holds answers for undo.
    pub(crate) fn cards_undo(&self) -> bool {
        self.cards.features.undo
    }

    /// The card features of the Host that holds `artifact_id`.
    pub fn card_features(&self, artifact_id: &str) -> CardFeatures {
        on_peer!(self.owner_ref(artifact_id), |engine, _local| engine.card_features(artifact_id));
        self.cards.features
    }

    /// Takes a user's answer back while the agent has not started on it
    /// (`decision_undo`). `revision` is the one the card last saw; the
    /// resolve's reply, when it came, knows better.
    pub fn undo_decision(&mut self, artifact_id: &str, decision_id: &str, revision: i64) {
        if let Some(peer) = self.owner_mut(artifact_id) {
            if let Some((engine, _)) = peer {
                engine.undo_decision(artifact_id, decision_id, revision);
            }
            return;
        }
        if decision_id.is_empty() || !self.cards.features.undo {
            return;
        }
        let key = format!("{artifact_id}|{decision_id}");
        if self.cards.undoing.contains(&key) {
            return;
        }
        // The answer is still on its way: the undo follows it.
        if self.update_action_pending("decision", decision_id) {
            self.cards.undo_owed.insert(key, revision);
            return;
        }
        let revision = self.cards.answered_revision.get(decision_id).copied().unwrap_or(revision);
        self.cards.undoing.insert(key);
        let tag = format!("decision-undo:{artifact_id}|{decision_id}");
        self.api.post_json(&tag, &decision_path(decision_id, "undo"), json!({"expected_revision": revision}), None);
    }

    /// An undo of the card is in flight (its Undo hides meanwhile).
    pub fn undo_in_flight(&self, artifact_id: &str, decision_id: &str) -> bool {
        on_peer!(self.owner_ref(artifact_id), |engine, _local| engine.undo_in_flight(artifact_id, decision_id));
        self.cards.undoing.contains(&format!("{artifact_id}|{decision_id}")) || self.cards.undo_owed.contains_key(&format!("{artifact_id}|{decision_id}"))
    }

    /// How the card's latest undo went; taken once.
    pub fn take_undo_outcome(&mut self, artifact_id: &str) -> Option<UndoOutcome> {
        on_peer!(self.owner_mut(artifact_id), |engine, _local| engine.take_undo_outcome(artifact_id));
        self.cards.undo_outcomes.remove(artifact_id)
    }

    /// Asks the Host what a spoken answer means (`decision_voice_match`):
    /// an option, an approval, the user's own words or a command. The
    /// command words are the app's own, in the user's language.
    pub fn match_decision(&mut self, artifact_id: &str, decision_id: &str, text: &str, commands: Value) {
        if let Some(peer) = self.owner_mut(artifact_id) {
            if let Some((engine, _)) = peer {
                engine.match_decision(artifact_id, decision_id, text, commands);
            }
            return;
        }
        if decision_id.is_empty() {
            return;
        }
        let tag = format!("decision-match:{artifact_id}|{decision_id}");
        self.api.post_json(&tag, &decision_path(decision_id, "match"), json!({"text": text, "commands": commands}), None);
    }

    /// What the Host made of a spoken answer, once it says; taken once.
    pub fn take_decision_match(&mut self, artifact_id: &str) -> Option<Result<Value, String>> {
        on_peer!(self.owner_mut(artifact_id), |engine, _local| engine.take_decision_match(artifact_id));
        self.cards.matches.remove(artifact_id)
    }

    /// True when the reply was a card's own (a resolve's reply is looked at
    /// and left to the Updates for the rest).
    pub(crate) fn cards_json(&mut self, tag: &str, object: &Object) -> bool {
        if let Some(rest) = tag.strip_prefix("decision-answer:") {
            let (artifact, decision) = rest.split_once('|').unwrap_or((rest, ""));
            if let Some(revision) = reply_revision(object) {
                self.cards.answered_revision.insert(decision.to_owned(), revision);
            }
            // The answered artifact (with its undo window) shows at once.
            if let Some(item) = object.get("artifact").filter(|a| a.is_object()) {
                self.apply_artifact_reply(&listed_id(item, artifact), item.clone());
            }
            let key = format!("{artifact}|{decision}");
            if let Some(expected) = self.cards.undo_owed.remove(&key) {
                // The answer is in: now take it back. The Updates' own
                // handling below clears the pending action first.
                let (artifact, decision) = (artifact.to_owned(), decision.to_owned());
                self.updates_json(tag, object);
                self.undo_decision(&artifact, &decision, expected);
                return true;
            }
            return false;
        }
        if let Some(rest) = tag.strip_prefix("decision-undo:") {
            let (artifact, decision) = rest.split_once('|').unwrap_or((rest, ""));
            self.cards.undoing.remove(rest);
            self.cards.answered_revision.remove(decision);
            if let Some(item) = object.get("artifact").filter(|a| a.is_object()) {
                self.apply_artifact_reply(&listed_id(item, artifact), item.clone());
            }
            // `undone: false` (it was open already) opens the card all the same.
            self.cards.undo_outcomes.insert(artifact.to_owned(), UndoOutcome::Undone);
            self.changes.push(Change::Updates);
            self.load_updates();
            return true;
        }
        if let Some(rest) = tag.strip_prefix("decision-match:") {
            let artifact = rest.split('|').next().unwrap_or_default();
            self.cards.matches.insert(artifact.to_owned(), Ok(Value::Object(object.clone())));
            self.changes.push(Change::Updates);
            return true;
        }
        false
    }

    pub(crate) fn cards_failure(&mut self, tag: &str, detail: &str, status: u16) -> bool {
        if let Some(rest) = tag.strip_prefix("decision-answer:") {
            // The answer did not go: nothing to take back.
            self.cards.undo_owed.remove(rest);
            self.changes.push(Change::Updates);
            return false;
        }
        if let Some(rest) = tag.strip_prefix("decision-undo:") {
            self.cards.undoing.remove(rest);
            let artifact = rest.split('|').next().unwrap_or_default();
            // Only `too_late` is too late; a changed revision or a gone decision
            // says why (the reload shows where it stands).
            let outcome = if status == 409 && detail.contains("too_late") { UndoOutcome::TooLate } else { UndoOutcome::Failed(detail.to_owned()) };
            // An undo that worked and is not yet shown stays the outcome.
            if self.cards.undo_outcomes.get(artifact) != Some(&UndoOutcome::Undone) {
                self.cards.undo_outcomes.insert(artifact.to_owned(), outcome);
            }
            self.changes.push(Change::Updates);
            self.load_updates();
            return true;
        }
        if let Some(rest) = tag.strip_prefix("decision-match:") {
            let artifact = rest.split('|').next().unwrap_or_default();
            self.cards.matches.insert(artifact.to_owned(), Err(detail.to_owned()));
            self.changes.push(Change::Updates);
            return true;
        }
        false
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn features_come_from_the_hosts_capabilities() {
        let info = json!({"capabilities": {"features": [UNDO_FEATURE, VOICE_MATCH_FEATURE]}});
        let features = offered_by(info.as_object().unwrap());
        assert_eq!(features, CardFeatures { undo: true, approval_text: false, voice_match: true });
        assert_eq!(offered_by(json!({}).as_object().unwrap()), CardFeatures::default());
    }

    #[test]
    fn the_resolves_reply_names_the_revision_wherever_it_carries_it() {
        let at = |v: Value| reply_revision(v.as_object().unwrap());
        assert_eq!(at(json!({"artifact": {"decision": {"revision": 5}}})), Some(5));
        assert_eq!(at(json!({"decision": {"revision": 3}})), Some(3));
        assert_eq!(at(json!({"ok": true})), None);
    }
}
