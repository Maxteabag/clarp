//! Native forms (`render_spec`, issue 428) as the chat answers them: the
//! single read of a published spec, what the user has chosen and typed, and
//! the one answer sent through the form path (iOS `RenderSpecFormOwner`).
//!
//! A list row never carries the spec: it is read with `GET /artifacts/<id>`
//! when its card is drawn, again whenever the row's `updated_at` moves on (an
//! answer accepted elsewhere, a new version), and checked against the Host's
//! content identity before it is drawn. One submission id per attempt: it is
//! repeated after an unknown outcome so the Host answers with the same
//! receipt, and renewed after a refusal.

use std::collections::HashMap;

use clarp_core::json::{self, Object};
use clarp_core::render_spec::{Accepted, Answer, Form, Meaning, Read, Reading, Refusal, Submission, Values};
use serde_json::{Value, json};

use crate::{Change, Engine};

/// What the card draws.
#[derive(Debug, Clone, PartialEq)]
pub enum FormState {
    Loading,
    Form(Box<Form>),
    NeedsNewer,
    Unreadable,
    /// The read failed; Retry reads again.
    Failed,
}

/// One form as the chat shows it.
#[derive(Debug, Clone, PartialEq)]
pub struct FormView {
    pub state: FormState,
    pub values: Values,
    /// The Host's accepted answer: the form is read-only with it.
    pub accepted: Option<Accepted>,
    pub sending: bool,
    /// The owner's word on the last send ("" for none).
    pub note: String,
    /// The Host takes no answer any more; the form stays readable.
    pub closed: bool,
    /// The action of the button last pressed (a version 2 button form):
    /// the one being sent while `sending`.
    pub pressed: Option<String>,
    /// While the Host holds the answer back from the agent: until when
    /// (epoch ms, 0 for no undo); "too-late" or why an undo failed.
    pub undo_until: i64,
    pub undo_note: String,
    /// When the note was set (epoch ms): it shows for a moment.
    pub undo_note_at: i64,
}

struct Entry {
    view: FormView,
    /// The row's `updated_at` the last read was asked for.
    asked_for: i64,
    /// The read in flight (its generation), if any.
    reading: Option<u64>,
    submission_id: String,
}

impl Entry {
    fn new() -> Self {
        Entry {
            view: FormView {
                state: FormState::Loading,
                values: Values::new(),
                accepted: None,
                sending: false,
                note: String::new(),
                closed: false,
                pressed: None,
                undo_until: 0,
                undo_note: String::new(),
                undo_note_at: 0,
            },
            asked_for: i64::MIN,
            reading: None,
            submission_id: new_submission_id(),
        }
    }
}

fn new_submission_id() -> String {
    uuid::Uuid::new_v4().to_string()
}

#[derive(Default)]
pub(crate) struct RenderSpecs {
    entries: HashMap<String, Entry>,
    generation: u64,
}

const READ: &str = "render-spec-read:";
const SUBMIT: &str = "render-spec-submit:";
const UNDO: &str = "render-spec-undo:";

/// The hold-for-undo fields of a receipt or a row, while the Host holds
/// the answer (`decision_undo`).
fn undo_until(object: &Value) -> i64 {
    if object.get("undoable").and_then(Value::as_bool) == Some(true) { object.get("undo_until").and_then(Value::as_i64).unwrap_or(0) } else { 0 }
}

impl Engine {
    /// The form as the card draws it; None before its card asked for it.
    pub fn render_spec(&self, artifact_id: &str) -> Option<FormView> {
        on_peer!(self.owner_ref(artifact_id), |engine, _local| engine.render_spec(artifact_id));
        self.render_specs.entries.get(artifact_id).map(|e| e.view.clone())
    }

    /// A card for this form is drawn with its row at `updated_at`: read it
    /// when it has not been, or the row moved on since.
    pub fn want_render_spec(&mut self, artifact_id: &str, updated_at: i64) {
        on_peer!(self.owner_mut(artifact_id), |engine, _local| engine.want_render_spec(artifact_id, updated_at));
        if artifact_id.is_empty() {
            return;
        }
        let entry = self.render_specs.entries.entry(artifact_id.to_owned()).or_insert_with(Entry::new);
        if entry.asked_for >= updated_at {
            return;
        }
        entry.asked_for = updated_at;
        self.read_render_spec(artifact_id);
    }

    /// Reads the form again (its card's Retry).
    pub fn retry_render_spec(&mut self, artifact_id: &str) {
        on_peer!(self.owner_mut(artifact_id), |engine, _local| engine.retry_render_spec(artifact_id));
        if let Some(entry) = self.render_specs.entries.get_mut(artifact_id) {
            entry.view.state = FormState::Loading;
            self.changes.push(Change::Updates);
            self.read_render_spec(artifact_id);
        }
    }

    fn read_render_spec(&mut self, artifact_id: &str) {
        self.render_specs.generation += 1;
        let generation = self.render_specs.generation;
        if let Some(entry) = self.render_specs.entries.get_mut(artifact_id) {
            entry.reading = Some(generation);
        }
        let path = format!("/artifacts/{}", clarp_core::endpoint::percent_encode_segment(artifact_id));
        self.api.get(&format!("{READ}{artifact_id}|{generation}"), &path, &[("representation", "flat-v1")]);
    }

    /// The user chose or typed `answer` for `key`.
    pub fn set_render_spec_value(&mut self, artifact_id: &str, key: &str, answer: Answer) {
        on_peer!(self.owner_mut(artifact_id), |engine, _local| engine.set_render_spec_value(artifact_id, key, answer));
        let Some(entry) = self.render_specs.entries.get_mut(artifact_id) else { return };
        let view = &mut entry.view;
        let FormState::Form(form) = &view.state else { return };
        if view.accepted.is_some() || !form.bound.contains(key) {
            return;
        }
        let answer = match answer {
            Answer::Str(text) => Answer::Str(form.held(key, &text)),
            other => other,
        };
        if view.values.get(key) != Some(&answer) {
            view.values.insert(key.to_owned(), answer);
            // An edit after a refusal is a new attempt's (iOS keeps the
            // closed form's note).
            if !view.closed {
                view.note.clear();
            }
            self.changes.push(Change::Updates);
        }
    }

    /// Sends the form's answers through its footer submit, when every
    /// required one is there (a form with buttons has no footer submit).
    pub fn send_render_spec(&mut self, artifact_id: &str) {
        on_peer!(self.owner_mut(artifact_id), |engine, _local| engine.send_render_spec(artifact_id));
        self.submit_render_spec(artifact_id, None);
    }

    /// Presses the form's button with `action` (its question, if any, was
    /// already answered OK): the form is sent once with intent `button` and
    /// that action, when every required value is there.
    pub fn press_render_spec_button(&mut self, artifact_id: &str, action: &str) {
        on_peer!(self.owner_mut(artifact_id), |engine, _local| engine.press_render_spec_button(artifact_id, action));
        self.submit_render_spec(artifact_id, Some(action));
    }

    fn submit_render_spec(&mut self, artifact_id: &str, pressing: Option<&str>) {
        let undo = self.cards_undo();
        let Some(entry) = self.render_specs.entries.get_mut(artifact_id) else { return };
        let view = &mut entry.view;
        let FormState::Form(form) = &view.state else { return };
        // After one submission every button is off; a button form has no
        // footer submit, and a form without buttons no button.
        if view.accepted.is_some() || view.sending || view.closed || form.buttons.is_empty() != pressing.is_none() {
            return;
        }
        let Submission::Ready(answers) = form.submission(&view.values, pressing) else { return };
        let mut body = json!({"submission_id": entry.submission_id, "version": form.version, "answers": answers});
        // A form answered with one click can be taken back for a moment.
        if undo && form.one_tap() {
            body["undo_ms"] = json!(crate::cards::UNDO_MS);
        }
        view.undo_note.clear();
        view.pressed = pressing.map(str::to_owned);
        view.sending = true;
        view.note.clear();
        let tag = format!("{SUBMIT}{artifact_id}|{}", entry.submission_id);
        self.changes.push(Change::Updates);
        let path = format!("/artifacts/{}/submit", clarp_core::endpoint::percent_encode_segment(artifact_id));
        self.api.post_json(&tag, &path, body, None);
    }

    /// Takes a held answer back (`decision_undo`): the form opens again.
    pub fn undo_render_spec(&mut self, artifact_id: &str) {
        on_peer!(self.owner_mut(artifact_id), |engine, _local| engine.undo_render_spec(artifact_id));
        let Some(entry) = self.render_specs.entries.get(artifact_id) else { return };
        let Some(accepted) = entry.view.accepted.as_ref() else { return };
        let path = format!(
            "/artifacts/{}/submissions/{}/undo",
            clarp_core::endpoint::percent_encode_segment(artifact_id),
            clarp_core::endpoint::percent_encode_segment(&accepted.submission_id)
        );
        self.api.post_json(&format!("{UNDO}{artifact_id}|{}", accepted.submission_id), &path, json!({}), None);
    }

    /// Forgets every form read (another Host, or a check's reload): each
    /// is read again when its card is next drawn.
    pub fn forget_render_specs(&mut self) {
        self.render_specs = RenderSpecs::default();
        self.changes.push(Change::Updates);
    }

    // ---- replies ---------------------------------------------------------

    pub(crate) fn render_spec_json(&mut self, tag: &str, object: &Object) -> bool {
        if let Some(rest) = tag.strip_prefix(READ) {
            let (artifact_id, generation) = split(rest);
            let mut item = object.get("artifact").cloned().unwrap_or(Value::Null);
            clarp_core::update_feeds::flatten(&mut item);
            let Some(entry) = self.render_specs.entries.get_mut(artifact_id) else { return true };
            if entry.reading != generation.parse().ok() {
                return true;
            }
            entry.reading = None;
            apply_read(entry, Read::of(&item).filter(|r| r.artifact_id == artifact_id));
            entry.view.undo_until = if entry.view.accepted.is_some() { undo_until(&item) } else { 0 };
            self.changes.push(Change::Updates);
            return true;
        }
        if let Some(rest) = tag.strip_prefix(UNDO) {
            let (artifact_id, _) = split(rest);
            let artifact_id = artifact_id.to_owned();
            let Some(entry) = self.render_specs.entries.get_mut(&artifact_id) else { return true };
            // Open again (or it was): a new attempt needs a new id, and
            // the form is read as the Host has it now.
            entry.submission_id = new_submission_id();
            entry.view.accepted = None;
            entry.view.undo_until = 0;
            entry.view.undo_note.clear();
            self.changes.push(Change::Updates);
            self.read_render_spec(&artifact_id);
            self.fetch_artifact(&artifact_id);
            return true;
        }
        if let Some(rest) = tag.strip_prefix(SUBMIT) {
            let (artifact_id, submission_id) = split(rest);
            let Some(entry) = self.render_specs.entries.get_mut(artifact_id) else { return true };
            let view = &mut entry.view;
            view.sending = false;
            let echoed = object.get("accepted").and_then(Value::as_bool) == Some(true)
                && json::string(object, "submission_id") == submission_id
                && json::string(object, "artifact_id") == artifact_id;
            match (&view.state, echoed) {
                (FormState::Form(form), true) => {
                    let state: serde_json::Map<String, Value> = view.values.iter().map(|(k, v)| (k.clone(), v.value())).collect();
                    let answers = match form.submission(&view.values, view.pressed.as_deref()) {
                        Submission::Ready(answers) => answers,
                        Submission::Blocked(_) => json!({"intent": form.submit_intent, "state": state}),
                    };
                    view.accepted = Accepted::of(Some(&json!({
                        "submission_id": submission_id, "version": json::string(object, "version"), "answers": answers,
                        "accepted_at": chrono::Utc::now().timestamp_millis(),
                        "delivery_status": Some(json::string(object, "delivery_status")).filter(|s| !s.is_empty()).unwrap_or_else(|| "pending".into()),
                    })));
                    view.note.clear();
                    view.undo_until = undo_until(&Value::Object(object.clone()));
                }
                _ => {
                    view.note = "Could not send. Check the connection and try again.".into();
                }
            }
            self.changes.push(Change::Updates);
            // The row says it is answered; its card reads the Host's answer.
            let artifact_id = artifact_id.to_owned();
            self.fetch_artifact(&artifact_id);
            return true;
        }
        false
    }

    pub(crate) fn render_spec_failure(&mut self, tag: &str, message: &str, status: u16) -> bool {
        if let Some(rest) = tag.strip_prefix(UNDO) {
            let (artifact_id, _) = split(rest);
            if let Some(entry) = self.render_specs.entries.get_mut(artifact_id) {
                entry.view.undo_note = if status == 409 && message.contains("too_late") { "too-late".into() } else { message.to_owned() };
                entry.view.undo_note_at = chrono::Utc::now().timestamp_millis();
                entry.view.undo_until = 0;
                self.changes.push(Change::Updates);
            }
            return true;
        }
        if let Some(rest) = tag.strip_prefix(READ) {
            let (artifact_id, generation) = split(rest);
            let Some(entry) = self.render_specs.entries.get_mut(artifact_id) else { return true };
            if entry.reading != generation.parse().ok() {
                return true;
            }
            entry.reading = None;
            eprintln!("render spec: event=read outcome=failed status={status}");
            entry.view.state = FormState::Failed;
            self.changes.push(Change::Updates);
            return true;
        }
        if let Some(rest) = tag.strip_prefix(SUBMIT) {
            let (artifact_id, _) = split(rest);
            let artifact_id = artifact_id.to_owned();
            let Some(entry) = self.render_specs.entries.get_mut(&artifact_id) else { return true };
            entry.view.sending = false;
            let code = clarp_core::endpoint::refusal_code(message);
            eprintln!("render spec: event=receipt outcome={} code={}", if code.is_some() { "refused" } else { "unknown" }, code.unwrap_or("none"));
            let Some(code) = code.filter(|_| status > 0) else {
                // Outcome unknown: the same id goes out again on the next Send.
                entry.view.note = "Could not send. Check the connection and try again.".into();
                self.changes.push(Change::Updates);
                return true;
            };
            // Whatever goes out next is a new attempt.
            entry.submission_id = new_submission_id();
            let refusal = Refusal { code: code.to_owned(), path: String::new(), message: String::new() };
            let mut read_again = false;
            match refusal.meaning() {
                Meaning::Reopen => {
                    entry.view.note = "The form changed. Check your answers and send again.".into();
                    read_again = true;
                }
                Meaning::AlreadyAnswered => read_again = true,
                Meaning::Closed => {
                    entry.view.closed = true;
                    entry.view.note = "This form is closed.".into();
                }
                Meaning::AppTooOld => {
                    entry.view.closed = true;
                    entry.view.note = "This Host does not take form answers from the desktop yet.".into();
                }
                Meaning::Answer => entry.view.note = "The Host did not take these answers. Check them and send again.".into(),
                Meaning::HostFault => entry.view.note = "The Host cannot check forms right now. Try again later.".into(),
                Meaning::Other if code == "reused_id" => read_again = true,
                Meaning::Other => entry.view.note = "The Host refused the form. Try again later.".into(),
            }
            self.changes.push(Change::Updates);
            if read_again {
                self.read_render_spec(&artifact_id);
            }
            return true;
        }
        false
    }

    pub(crate) fn reset_render_specs(&mut self) {
        self.render_specs = RenderSpecs::default();
    }
}

fn split(rest: &str) -> (&str, &str) {
    rest.rsplit_once('|').unwrap_or((rest, ""))
}

/// The Host's read into the entry: the form with what was typed carried
/// over, or read-only with the Host's answer.
fn apply_read(entry: &mut Entry, read: Option<Read>) {
    let view = &mut entry.view;
    let Some(read) = read else {
        eprintln!("render spec: event=read outcome=unreadable pointer=");
        view.state = FormState::Unreadable;
        return;
    };
    match read.reading {
        Reading::NeedsNewer => {
            eprintln!("render spec: event=read outcome=newer-app");
            view.state = FormState::NeedsNewer;
        }
        Reading::Unreadable(ref pointer) => {
            eprintln!("render spec: event=read outcome=unreadable pointer={pointer}");
            view.state = FormState::Unreadable;
        }
        Reading::Form(_) if !read.matches() => {
            // The spec must be the bytes the Host says it gave.
            eprintln!("render spec: event=read outcome=identity-mismatch");
            view.state = FormState::Unreadable;
        }
        Reading::Form(form) => {
            if let Some(accepted) = read.submission {
                eprintln!("render spec: event=read outcome=answered delivery={}", accepted.delivery_status);
                view.values = Values::new();
                view.pressed = None;
                view.accepted = Some(accepted);
                view.closed = false;
                view.note.clear();
            } else {
                let typed = std::mem::take(&mut view.values);
                view.values = form.carrying(&typed);
                view.accepted = None;
                // Only a ready or active form takes an answer.
                view.closed = !matches!(read.status.as_str(), "ready" | "active");
                if view.closed {
                    view.note = "This form is closed.".into();
                }
                eprintln!("render spec: event=read outcome=open keys={} closed={}", form.fields.len(), view.closed);
            }
            view.state = FormState::Form(form);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_refusal_code_survives_the_failure_text() {
        let body = br#"{"error": {"code": "version_mismatch", "path": "/version", "message": "The form changed."}}"#;
        let text = clarp_core::endpoint::error_message(body, "HTTP 409");
        assert_eq!(text, "The form changed. [version_mismatch]");
        assert_eq!(clarp_core::endpoint::refusal_code(&text), Some("version_mismatch"));
        assert_eq!(clarp_core::endpoint::refusal_code("Gateway Timeout (HTTP 504)"), None);
        assert_eq!(clarp_core::endpoint::error_message(br#"{"error": "busy"}"#, "x"), "busy");
    }
}
