//! Notifications per event (Host feature `notification_events`): the
//! Host's settings (which events alert, globally and per contact), read on
//! connect and kept live from `notification-settings-changed`; which
//! `user-notification` becomes a desktop popup; marking a chat read from a
//! popup (`POST /chat-read`); and answering a question from its buttons. On
//! a Host with `decision_undo` that answer is held a few seconds and the
//! popup offers Undo (`POST /decisions/{id}/undo`), which brings the
//! question back. A question answered elsewhere closes its popup: its card
//! is read again on `artifact-updated`, as Updates reads it.
//! A Host without the feature keeps the old rule: a reply in a chat that
//! is not the selected one pops up.

use clarp_core::json::{self, Object};
use clarp_core::notify_events::{self, Choice, Incoming, Preset, Recent, Settings};
use serde_json::{Value, json};

use crate::{Change, Engine};
use clarp_core::notify_events::{AlertOrder, ReadTarget};
use std::collections::HashMap;

const SETTINGS_TAG: &str = "notification-settings";
const UNDO_TAG: &str = "notify-undo:";
/// The Host feature that holds an answer so it can be taken back.
const UNDO_FEATURE: &str = "decision_undo";
/// How long an answer from a popup waits for Undo (as long as a popup shows).
const UNDO_MS: u64 = 8_000;
/// `response_type` of the popup that says what was answered: its one
/// answer button is Undo.
pub const ANSWERED: &str = "answered";
const UNDO: &str = "undo";
/// The questions kept to show again after an Undo.
const QUESTIONS_KEPT: usize = 32;
const SETTINGS_PATH: &str = "/notification-settings";

#[derive(PartialEq, Eq, Hash)]
struct UnreadSource {
    host: String,
    decision: String,
    epoch: Option<String>,
}

#[derive(Default)]
pub(crate) struct Notifications {
    storage_host: String,
    // One high-water mark per Host, chat and process generation.
    boundaries: HashMap<(String, String, String), (u64, u64)>,
    // Unread contributors per chat. Keep only the maximum within each
    // Host/decision/generation; unordered alerts survive chat reads.
    unread: HashMap<String, HashMap<UnreadSource, Option<(u64, u64)>>>,
    /// The Host lists `notification_events`.
    offered: bool,
    /// Its settings document, once read.
    settings: Option<Settings>,
    /// The last notification ids that popped up (or were dropped).
    recent: Recent,
    /// The Host lists `decision_undo`.
    undo: bool,
    /// Questions that popped up, newest last, to show again after Undo.
    questions: Vec<Popup>,
    /// Failed turns that popped up, newest last, for their button.
    failures: Vec<Popup>,
    /// Answers sent from a popup, by decision id: the answer's words, and
    /// the revision Undo names once the Host has held it.
    answers: std::collections::HashMap<String, (String, Option<i64>)>,
}

/// One desktop popup: who, what, and what its buttons do.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Popup {
    pub title: String,
    pub body: String,
    /// The chat (qualified for another Host's).
    pub session: String,
    pub notification: Incoming,
    /// The chat is the one selected in this window: the window drops the
    /// popup while it has the focus.
    pub selected: bool,
}

impl Engine {
    // ---- queries ---------------------------------------------------------

    /// The Host keeps notification settings per event.
    pub fn notification_events_offered(&self) -> bool {
        self.notifications.offered
    }

    /// The Host's notification settings, once read.
    pub fn notification_settings(&self) -> Option<&Settings> {
        self.notifications.settings.as_ref()
    }

    /// The settings of the Host that holds this chat (another Host's for
    /// its chats), and the agent id they are keyed by there.
    pub fn notification_settings_for(&self, session: &str) -> Option<(&Settings, String)> {
        on_peer!(self.peer_ref(session), |engine, local| engine.notification_settings_for(local));
        if !self.notifications.offered {
            return None;
        }
        let agent_id = self.roster.find(session).map(|a| a.agent_id.clone()).filter(|id| !id.is_empty())?;
        Some((self.notifications.settings.as_ref()?, agent_id))
    }

    // ---- commands --------------------------------------------------------

    /// Turns an event on or off for every contact (the owner's choice).
    pub fn set_notification_event(&mut self, event: &str, on: bool) {
        let Some(settings) = self.notifications.settings.as_mut() else { return };
        let body = settings.set_global(event, on);
        self.post_notification_settings(body);
    }

    /// One contact's choice for an event: Some(on/off), or None to follow
    /// the owner's choice.
    pub fn set_agent_notification_event(&mut self, session: &str, event: &str, choice: Option<bool>) {
        on_peer!(self.peer_mut(session), |engine, local| engine.set_agent_notification_event(local, event, choice));
        let Some(agent_id) = self.roster.find(session).map(|a| a.agent_id.clone()).filter(|id| !id.is_empty()) else { return };
        let Some(settings) = self.notifications.settings.as_mut() else { return };
        let body = settings.set_agent(&agent_id, event, choice);
        self.post_notification_settings(body);
    }

    /// A contact's preset: All or Only when it needs me set its choices
    /// (and unmute it), Muted mutes it; Custom changes nothing.
    pub fn set_agent_notification_preset(&mut self, session: &str, preset: Preset) {
        on_peer!(self.peer_mut(session), |engine, local| engine.set_agent_notification_preset(local, preset));
        let Some(agent) = self.roster.find(session) else { return };
        let (agent_id, muted) = (agent.agent_id.clone(), agent.muted);
        match preset {
            Preset::Custom => {}
            Preset::Muted => self.set_agent_push_muted(session, true),
            Preset::All | Preset::NeedsMe => {
                if muted {
                    self.set_agent_push_muted(session, false);
                }
                if let Some(choices) = self.notifications.settings.as_mut().and_then(|s| s.agents.get_mut(&agent_id)) {
                    choices.muted = false;
                }
                let body = self.notifications.settings.as_mut().and_then(|s| s.set_preset(&agent_id, preset));
                if let Some(body) = body {
                    self.post_notification_settings(body);
                } else {
                    self.changes.push(Change::NotificationSettings);
                }
            }
        }
    }

    fn post_notification_settings(&mut self, body: Value) {
        self.changes.push(Change::NotificationSettings);
        self.api.post_json(SETTINGS_TAG, SETTINGS_PATH, body, None);
    }

    /// Asks the Host to mark the chat read; its receipt supplies the boundary.
    pub fn mark_chat_read(&mut self, session: &str) {
        self.mark_popup_read(session, "");
    }

    /// A popup action addresses the Host that originally displayed the alert.
    pub fn mark_popup_read(&mut self, session: &str, host: &str) {
        on_peer!(self.peer_mut(session), |engine, local| engine.mark_popup_read(local, host));
        if !self.notifications.offered || session.is_empty() || self.server_id.is_empty() || (!host.is_empty() && host != self.server_id) {
            return;
        }
        let tag = format!("chat-read:{}", json!([self.server_id, session]));
        self.api.post_json(&tag, "/chat-read", json!({"session": session}), None);
    }

    /// Answers a question from a popup's button, at the newest revision the
    /// desktop knows (Updates', else the push's). False when none is known:
    /// the caller opens the chat at it instead.
    pub fn answer_from_notification(&mut self, decision_id: &str, response_type: &str, option: &str) -> bool {
        on_peer!(self.owner_mut(decision_id), |engine, _local| engine.answer_from_notification(decision_id, response_type, option));
        if response_type == ANSWERED {
            return option == UNDO && self.undo_from_notification(decision_id);
        }
        let undo_ms = self.notifications.undo.then_some(UNDO_MS);
        let words = self
            .notifications
            .questions
            .iter()
            .rev()
            .find(|q| q.notification.decision_id == decision_id)
            .and_then(|q| q.notification.options.iter().find(|c| c.id == option))
            .map(|c| c.label.clone())
            .unwrap_or_else(|| option.to_owned());
        // The newest the desktop knows: Updates' (live), else the push's.
        let listed = self.attention_items().iter().filter_map(Value::as_object).find(|item| json::string(item, "decision_id") == decision_id).map(|item| json::integer(item, "revision"));
        let pushed = self.notifications.questions.iter().rev().find(|q| q.notification.decision_id == decision_id).and_then(|q| q.notification.revision);
        let Some(revision) = listed.max(pushed).filter(|r| *r > 0) else { return false };
        match response_type {
            "approval" => {
                let choice = match option {
                    "yes" | "accepted" => "yes",
                    "no" | "rejected" => "no",
                    _ => return false,
                };
                self.resolve_decision_held(decision_id, choice, revision, undo_ms);
                self.notifications.answers.insert(decision_id.to_owned(), (words, None));
                true
            }
            "single_choice" => {
                let mut body = json!({"answer": {"option_id": option}});
                if let Some(undo_ms) = undo_ms {
                    body["undo_ms"] = json!(undo_ms);
                }
                // Not in Updates yet: the decision alone (the artifact only
                // names the card's status line there).
                let pushed = self.question(decision_id).map(|q| q.notification.artifact_id).filter(|a| !a.is_empty());
                let artifact = self.decision_artifact(decision_id).or(pushed).unwrap_or_default();
                self.send_decision(&artifact, decision_id, "resolve", body, revision);
                self.notifications.answers.insert(decision_id.to_owned(), (words, None));
                true
            }
            _ => false,
        }
    }

    /// A failed turn's popup button: sends the message again as its action
    /// says (`POST /turn-retry`, through the same call as the chat's card).
    /// False when the action does not send (switch_model, sign_in) or the
    /// failure is not known here: the caller opens the chat.
    pub fn retry_from_notification(&mut self, session: &str, message_id: &str) -> bool {
        on_peer!(self.peer_mut(session), |engine, local| engine.retry_from_notification(local, message_id));
        let Some(popup) = self.notifications.failures.iter().rev().find(|p| p.session == session && p.notification.message_id == message_id).cloned() else {
            return false;
        };
        let Some(failure) = popup.notification.failure.as_ref().filter(|f| f.retries()) else { return false };
        let error = failure.turn_error(message_id, &popup.body);
        let Some(action) = error.actions.first().cloned() else { return false };
        self.turn_retry(session, &error, &action);
        true
    }

    /// Takes back an answer sent from a popup while the Host still holds
    /// it. False when there is nothing to take back (the caller opens the
    /// chat).
    fn undo_from_notification(&mut self, decision_id: &str) -> bool {
        let Some(revision) = self.notifications.answers.get(decision_id).and_then(|(_, r)| *r) else { return false };
        let path = format!("/decisions/{}/undo", clarp_core::endpoint::percent_encode_segment(decision_id));
        self.api.post_json(&format!("{UNDO_TAG}{decision_id}"), &path, json!({"expected_revision": revision}), None);
        true
    }

    /// The decision id an answer's reply is for (`update-action:decision:<id>`
    /// or `decision-answer:<artifact>|<id>`).
    fn answered_decision(tag: &str) -> Option<&str> {
        tag.strip_prefix("update-action:decision:").or_else(|| tag.strip_prefix("decision-answer:").and_then(|rest| rest.split_once('|').map(|(_, id)| id)))
    }

    /// An answer sent from a popup was taken: while the Host holds it, the
    /// chat's popup says what was answered and offers Undo. Looks only:
    /// Updates handles the reply too.
    pub(crate) fn notification_answer_json(&mut self, tag: &str, object: &Object) {
        let Some(decision_id) = Self::answered_decision(tag) else { return };
        let Some((words, _)) = self.notifications.answers.get(decision_id).cloned() else { return };
        let decision = object.get("artifact").and_then(|a| a.get("decision")).and_then(Value::as_object);
        let held = decision.filter(|d| d.get("undoable").and_then(Value::as_bool) == Some(true));
        let (Some(held), Some(question)) = (held, self.question(decision_id)) else {
            self.notifications.answers.remove(decision_id);
            return;
        };
        let revision = json::integer(held, "revision");
        self.notifications.answers.insert(decision_id.to_owned(), (words.clone(), Some(revision)));
        let mut notification = question.notification.clone();
        notification.event = ANSWERED.into();
        notification.response_type = ANSWERED.into();
        notification.notification_id.clear();
        notification.options = vec![Choice { id: UNDO.into(), label: notify_events::text("notify.action.undo") }];
        let body = notify_events::you_answered(&words);
        self.changes.push(Change::Notification(Popup { body, notification, selected: false, ..question }));
    }

    /// A question that popped up is on this card.
    pub(crate) fn question_popup_on(&self, artifact_id: &str) -> bool {
        !artifact_id.is_empty() && self.notifications.questions.iter().any(|q| q.notification.artifact_id == artifact_id)
    }

    /// The card of a question that popped up was read again (Updates reads
    /// it on `artifact-updated`): answered, dismissed or withdrawn elsewhere,
    /// its popup goes. An answer this desktop sent that the Host still holds
    /// for Undo keeps its popup until the hold ends.
    pub(crate) fn question_card_read(&mut self, artifact_id: &str, object: &Object) {
        if !self.question_popup_on(artifact_id) {
            return;
        }
        let Some(decision) = object.get("artifact").and_then(|a| a.get("decision")).and_then(Value::as_object) else { return };
        let status = json::string(decision, "status");
        if status.is_empty() || status == "pending" || decision.get("undoable").and_then(Value::as_bool) == Some(true) {
            return;
        }
        let said = json::string(decision, "decision_id");
        let ended: Vec<Popup> = self
            .notifications
            .questions
            .iter()
            .filter(|q| q.notification.artifact_id == artifact_id && (said.is_empty() || q.notification.decision_id == said))
            .cloned()
            .collect();
        for popup in ended {
            let decision_id = popup.notification.decision_id;
            self.notifications.questions.retain(|q| q.session != popup.session || q.notification.host != popup.notification.host || q.notification.decision_id != decision_id);
            self.notifications.answers.remove(&decision_id);
            self.apply_read(&popup.session, popup.notification.host, ReadTarget::Decision(decision_id));
        }
    }

    fn question(&self, decision_id: &str) -> Option<Popup> {
        self.notifications.questions.iter().rev().find(|q| q.notification.decision_id == decision_id).cloned()
    }

    fn undo_answered(&mut self, decision_id: &str, object: &Object) {
        self.notifications.answers.remove(decision_id);
        if object.get("undone").and_then(Value::as_bool) != Some(true) {
            return;
        }
        // Pending again: the question as it popped up, its answers back.
        if let Some(mut question) = self.question(decision_id) {
            question.notification.notification_id.clear();
            question.selected = false;
            self.changes.push(Change::Notification(question));
        }
        self.load_updates();
    }

    fn undo_too_late(&mut self, decision_id: &str) {
        self.notifications.answers.remove(decision_id);
        let Some(mut question) = self.question(decision_id) else { return };
        question.notification.options.clear();
        question.notification.decision_id.clear();
        question.notification.event = ANSWERED.into();
        question.body = notify_events::text("notify.body.too_late");
        question.selected = false;
        self.changes.push(Change::Notification(question));
    }

    /// The artifact a decision in Updates belongs to.
    fn decision_artifact(&self, decision_id: &str) -> Option<String> {
        self.attention_items()
            .iter()
            .filter_map(Value::as_object)
            .find(|item| json::string(item, "decision_id") == decision_id)
            .map(|item| json::string(item, "artifact_id"))
            .filter(|id| !id.is_empty())
    }

    // ---- the Host --------------------------------------------------------

    /// `/server-info` answered: whether the Host keeps settings per event.
    pub(crate) fn notifications_server_info(&mut self, info: &Object) {
        self.restore_notification_reads();
        let offered = info
            .get("capabilities")
            .and_then(|c| c.get("features"))
            .and_then(Value::as_array)
            .is_some_and(|features| features.iter().any(|f| f.as_str() == Some(notify_events::FEATURE)));
        let was = (self.notifications.offered, self.notifications.settings.is_some());
        self.notifications.offered = offered;
        self.notifications.undo = info
            .get("capabilities")
            .and_then(|c| c.get("features"))
            .and_then(Value::as_array)
            .is_some_and(|features| features.iter().any(|f| f.as_str() == Some(UNDO_FEATURE)));
        if offered {
            self.api.get(SETTINGS_TAG, SETTINGS_PATH, &[]);
        } else {
            self.notifications.settings = None;
        }
        if was != (offered, self.notifications.settings.is_some()) {
            self.changes.push(Change::NotificationSettings);
        }
    }

    fn notification_cache(&self) -> Option<clarp_core::transcript_cache::TranscriptCache> {
        let cache = self.transcript_cache.as_ref()?;
        Some(clarp_core::transcript_cache::TranscriptCache::new(cache.root().join("notification-reads")))
    }

    // /server-info identifies the stream before it starts. Use its stable Host
    // identity both for the primary and for the same Host opened as a peer.
    fn restore_notification_reads(&mut self) {
        if self.notifications.storage_host == self.server_id {
            return;
        }
        self.notifications.storage_host = self.server_id.clone();
        self.notifications.boundaries.clear();
        self.notifications.unread.clear();
        let Some(cache) = self.notification_cache() else { return };
        let saved = cache.load(&self.server_id, "reads");
        for row in json::array(&saved, "boundaries") {
            if let Ok((host, session, epoch, position)) = serde_json::from_value(row.clone()) {
                self.notifications.boundaries.insert((host, session, epoch), position);
            }
        }
        for row in json::array(&saved, "unread") {
            if let Ok((session, host, decision, epoch, position)) = serde_json::from_value(row.clone()) {
                self.notifications.unread.entry(session).or_default().insert(UnreadSource { host, decision, epoch }, position);
            }
        }
        self.restore_notification_badges();
    }

    fn save_notification_reads(&self) {
        let Some(cache) = self.notification_cache() else { return };
        let state = &self.notifications;
        if state.storage_host.is_empty() {
            return;
        }
        let boundaries: Vec<_> = state.boundaries.iter().map(|((host, session, epoch), pos)| (host, session, epoch, pos)).collect();
        let unread: Vec<_> = state.unread.iter().flat_map(|(session, entries)| {
            entries.iter().map(move |(source, pos)| (session, &source.host, &source.decision, &source.epoch, pos))
        }).collect();
        let mut snapshot = Object::new();
        snapshot.insert("boundaries".into(), json!(boundaries));
        snapshot.insert("unread".into(), json!(unread));
        if let Err(error) = cache.save(&state.storage_host, "reads", &snapshot) {
            eprintln!("Engine: saving notification reads: {error}");
        }
    }

    pub(crate) fn forget_notification_reads(&mut self) {
        if let Some(cache) = self.notification_cache() {
            cache.remove(&self.notifications.storage_host, "reads");
        }
        self.notifications = Notifications::default();
    }

    pub(crate) fn restore_notification_badges(&mut self) {
        for session in self.notifications.unread.keys() {
            self.roster.mark_unread(session);
        }
        if !self.roster.take_ops().is_empty() {
            self.changes.push(Change::Roster);
        }
    }

    /// Replies for the notification routes; true when it was one.
    pub(crate) fn notifications_json(&mut self, tag: &str, object: &Object) -> bool {
        if tag == SETTINGS_TAG {
            self.take_notification_settings(object);
            return true;
        }
        if let Some(decision_id) = tag.strip_prefix(UNDO_TAG) {
            self.undo_answered(decision_id, object);
            return true;
        }
        if let Some(scope) = tag.strip_prefix("chat-read:") {
            if let Ok((host, session)) = serde_json::from_str::<(String, String)>(scope)
                && host == self.server_id
                && object.get("ok").and_then(Value::as_bool) == Some(true)
                && object.get("session").and_then(Value::as_str) == Some(session.as_str())
            {
                self.chat_read_event(&session, object);
            }
            return true;
        }
        false
    }

    pub(crate) fn notifications_failure(&mut self, tag: &str, detail: &str) -> bool {
        if tag == SETTINGS_TAG {
            // The Host kept what it had: read it again and say why.
            eprintln!("Engine: notification settings failed: {detail}");
            if self.notifications.offered {
                self.api.get(SETTINGS_TAG, SETTINGS_PATH, &[]);
            }
            self.set_error(&format!("Could not change notifications: {detail}"));
            return true;
        }
        if let Some(decision_id) = tag.strip_prefix(UNDO_TAG) {
            eprintln!("Engine: undo of {decision_id} failed: {detail}");
            self.undo_too_late(decision_id);
            return true;
        }
        if let Some(session) = tag.strip_prefix("chat-read:") {
            // Read here already; the other devices learn it when the chat opens.
            eprintln!("Engine: marking {session} read on the Host failed: {detail}");
            return true;
        }
        false
    }

    fn take_notification_settings(&mut self, document: &Object) {
        let settings = Settings::parse(document);
        if self.notifications.settings.as_ref() != Some(&settings) {
            self.notifications.settings = Some(settings);
            self.changes.push(Change::NotificationSettings);
        }
    }

    // ---- the event stream ------------------------------------------------

    /// `notification-settings-changed`: the whole document, from any device.
    pub(crate) fn notification_settings_event(&mut self, event: &Object) {
        if !self.notifications.offered {
            return;
        }
        match event.get("settings").and_then(Value::as_object) {
            Some(document) => self.take_notification_settings(&document.clone()),
            None => self.take_notification_settings(event),
        }
    }

    pub(crate) fn chat_read_event(&mut self, session: &str, event: &Object) {
        let host = self.server_id.clone();
        if !self.notifications.offered || session.is_empty() || host.is_empty() {
            return;
        }
        let decision = json::string(event, "decision_id");
        let target = if !decision.is_empty() {
            ReadTarget::Decision(decision)
        } else {
            let Some(order) = AlertOrder::parse(event, "read_through_ms", "read_through_seq") else { return };
            ReadTarget::Through(order)
        };
        self.apply_read(session, host, target);
    }

    fn apply_read(&mut self, session: &str, host: String, target: ReadTarget) {
        if let ReadTarget::Through(order) = &target {
            let scope = (host.clone(), session.into(), order.epoch.clone());
            if self.notifications.boundaries.get(&scope).is_some_and(|saved| *saved >= order.position) {
                return;
            }
            self.notifications.boundaries.insert(scope, order.position);
        }
        if let Some(unread) = self.notifications.unread.get_mut(session) {
            unread.retain(|source, position| {
                source.host != host || match &target {
                    ReadTarget::Decision(id) => source.decision != *id,
                    ReadTarget::Through(end) => source.epoch.as_ref() != Some(&end.epoch) || !position.is_some_and(|p| p <= end.position),
                }
            });
            if unread.is_empty() {
                self.notifications.unread.remove(session);
                self.mutate_roster(|r| r.clear_unread(session));
            }
        }
        self.save_notification_reads();
        self.changes.push(Change::NotificationRead(session.into(), host, target));
    }

    pub(crate) fn notification_event(&mut self, event: &Object) {
        let mut incoming = Incoming::parse(event);
        incoming.host = self.server_id.clone();
        if !self.notifications.offered || incoming.host.is_empty() {
            incoming.order = None;
        }
        let session = incoming.session.clone();
        if let Some(order) = &incoming.order {
            let scope = (incoming.host.clone(), session.clone(), order.epoch.clone());
            if self.notifications.boundaries.get(&scope).is_some_and(|end| order.position <= *end) {
                return;
            }
        }
        let selected = session == self.selected;
        if !selected && incoming.unread != Some(false) && !session.is_empty() && !self.roster.find(&session).is_some_and(|a| a.quiet) {
            let key = UnreadSource {
                host: incoming.host.clone(),
                decision: incoming.decision_id.clone(),
                epoch: incoming.order.as_ref().map(|o| o.epoch.clone()),
            };
            let position = incoming.order.as_ref().map(|o| o.position);
            let latest = self.notifications.unread.entry(session.clone()).or_default().entry(key).or_default();
            *latest = (*latest).max(position);
            self.save_notification_reads();
            self.mutate_roster(|r| r.apply_notification_event(event));
        }
        let quiet = self.roster.find(&session).is_some_and(|a| a.quiet);
        if json::string(event, "type") == "chat-unread" || !incoming.alerts(quiet) || (selected && !self.notifications.offered) {
            return;
        }
        if !self.notifications.recent.first_time(&incoming.notification_id) {
            return;
        }
        // Words are made here, as the popup is shown: in the app's language now.
        let title = [incoming.title_text(), incoming.persona.clone()].into_iter().find(|t| !t.is_empty()).unwrap_or_else(|| session.clone());
        let mut body = incoming
            .body_text()
            .or_else(|| event.get("preview_parts").and_then(Value::as_object).and_then(clarp_core::chat_preview::line))
            .unwrap_or_else(|| clarp_core::decision_receipt::preview_line(&incoming.preview));
        // An approval's answers in the card's own words, where Updates lists it.
        if incoming.response_type == "approval" {
            let listed = self.attention_items().iter().filter_map(Value::as_object).find(|i| json::string(i, "decision_id") == incoming.decision_id).cloned();
            for choice in incoming.options.iter_mut() {
                let said = listed.as_ref().map(|i| json::string(i, if choice.id == "accepted" { "yes_label" } else { "no_label" })).unwrap_or_default();
                if !said.is_empty() && (choice.label == notify_events::text("notify.action.yes") || choice.label == notify_events::text("notify.action.no")) {
                    choice.label = said;
                }
            }
        }
        // A failed turn says what its card says, in the app's language.
        if let Some(failure) = &incoming.failure {
            let now = chrono::Utc::now().timestamp_millis();
            let sentence = failure.turn_error(&incoming.message_id, &body).sentence_in(clarp_core::turn_error_text::app_language(), now);
            if !sentence.is_empty() {
                body = sentence;
            }
        }
        if body.is_empty() {
            let key = format!("notify.body.{}", incoming.event);
            let words = notify_events::text(&key);
            body = if words == key { notify_events::text("notify.body.done") } else { words };
        }
        let popup = Popup { title, body, session, notification: incoming, selected };
        if popup.notification.failure.is_some() {
            let failures = &mut self.notifications.failures;
            failures.push(popup.clone());
            let extra = failures.len().saturating_sub(QUESTIONS_KEPT);
            failures.drain(..extra);
        }
        if !popup.notification.decision_id.is_empty() {
            let questions = &mut self.notifications.questions;
            questions.retain(|q| q.session != popup.session || q.notification.host != popup.notification.host || q.notification.decision_id != popup.notification.decision_id);
            questions.push(popup.clone());
            let extra = questions.len().saturating_sub(QUESTIONS_KEPT);
            questions.drain(..extra);
        }
        self.changes.push(Change::Notification(popup));
    }
}
