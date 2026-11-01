//! Selecting text in the chat. Slint's rich text cannot be selected, so the
//! loaded chat opens as plain text on the select-text page, where the mouse
//! or Shift+arrows select and Ctrl+C copies. From a message (its icon, or
//! `v` on vim mode's cursor) that message starts selected and in view;
//! from `V` in the chat the caret starts at the latest.

use clarp_core::protocol::Message;

use crate::{App, AppWindow};

/// A message as it reads: what its copy button copies.
fn readable(m: &Message) -> String {
    match clarp_core::decision_receipt::parse(&m.text) {
        // A receipt reads as its question and outcome, not the Host's prompt.
        Some(receipt) => format!("{}\n{}", receipt.question, receipt.outcome.label()),
        // A reply copies its written form only.
        None if m.display_text.is_empty() && m.role == "user" => m.text.clone(),
        None => m.display_text.clone(),
    }
}

/// The text of message `id` in pane `pane`'s chat.
pub fn message_text(app: &App, pane: &str, id: &str) -> Option<String> {
    let session = app.session_of(pane);
    app.engine.borrow().conversation(&session).and_then(|c| c.rows().iter().find(|m| m.id == id).map(readable))
}

fn open(app: &App, window: &AppWindow, title: String, body: String, selected: (usize, usize)) {
    window.set_select_title(title.into());
    window.set_select_body(body.into());
    window.set_select_from(selected.0 as i32);
    window.set_select_to(selected.1 as i32);
    crate::commands::open_overlay(app, window, "select-text");
    window.invoke_open_select_text();
}

/// The chat as it reads, oldest first, each message under who wrote it,
/// and where each message's text is in it (UTF-8 byte offsets).
fn chat_body(app: &App, session: &str) -> (String, Vec<(String, usize, usize)>) {
    let engine = app.engine.borrow();
    let name = engine.chat_name(session);
    let mut body = String::new();
    let mut spans = Vec::new();
    let Some(conversation) = engine.conversation(session) else { return (body, spans) };
    for m in conversation.rows().iter().filter(|m| !m.activity && !m.pending && m.kind != "live" && (m.role == "user" || m.role == "assistant")) {
        let text = readable(m);
        if text.trim().is_empty() {
            continue;
        }
        let who = if m.role == "user" { "You".to_owned() } else if !m.sender_name.is_empty() { m.sender_name.clone() } else { name.clone() };
        if !body.is_empty() {
            body.push_str("\n\n");
        }
        body.push_str(&who);
        body.push_str(":\n");
        spans.push((m.id.clone(), body.len(), body.len() + text.len()));
        body.push_str(&text);
    }
    (body, spans)
}

/// The whole loaded chat of `session` on the select-text page: message
/// `id` selected and in view when it is in it, else the caret at the end.
fn open_chat(app: &App, window: &AppWindow, session: &str, id: Option<&str>) {
    if session.is_empty() {
        return;
    }
    let (body, spans) = chat_body(app, session);
    if body.is_empty() {
        app.engine.borrow_mut().report_error("Nothing in this chat to select from yet");
        return;
    }
    let selected = id.and_then(|id| spans.iter().find(|(m, ..)| m == id)).map_or((body.len(), body.len()), |(_, from, to)| (*from, *to));
    let name = app.engine.borrow().chat_name(session);
    open(app, window, format!("Select text · {name}"), body, selected);
}

/// The chat with one message selected (its icon, or `v` on vim mode's
/// cursor): the selection starts there and reaches any other message.
pub fn open_message(app: &App, window: &AppWindow, pane: &str, id: &str) {
    open_chat(app, window, &app.session_of(pane), Some(id));
}

/// The chat in the active pane, the caret at its latest message.
pub fn open_conversation(app: &App, window: &AppWindow) {
    open_chat(app, window, &app.active_session(), None);
}
