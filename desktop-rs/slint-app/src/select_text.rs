//! Selecting text in the chat. Slint's rich text cannot be selected, so a
//! message (its icon, or `v` on vim mode's cursor) or the whole loaded chat
//! (`V` in the chat) opens as plain text on the select-text page, where
//! the mouse or Shift+arrows select and Ctrl+C copies.

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

fn open(app: &App, window: &AppWindow, title: String, body: String) {
    window.set_select_title(title.into());
    window.set_select_body(body.into());
    crate::commands::open_overlay(app, window, "select-text");
    window.invoke_open_select_text();
}

/// One message on the select-text page.
pub fn open_message(app: &App, window: &AppWindow, pane: &str, id: &str) {
    let Some(text) = message_text(app, pane, id) else {
        eprintln!("clarp-slint: no message {id} to select from");
        return;
    };
    let name = app.engine.borrow().chat_name(&app.session_of(pane));
    open(app, window, format!("Select text · {name}"), text);
}

/// Every message of the chat in the active pane, oldest first, each under
/// who wrote it.
pub fn open_conversation(app: &App, window: &AppWindow) {
    let session = app.active_session();
    if session.is_empty() {
        return;
    }
    let body = {
        let engine = app.engine.borrow();
        let name = engine.chat_name(&session);
        let Some(conversation) = engine.conversation(&session) else { return };
        conversation
            .rows()
            .iter()
            .filter(|m| !m.activity && !m.pending && m.kind != "live" && (m.role == "user" || m.role == "assistant"))
            .filter_map(|m| {
                let text = readable(m);
                if text.trim().is_empty() {
                    return None;
                }
                let who = if m.role == "user" { "You".to_owned() } else if !m.sender_name.is_empty() { m.sender_name.clone() } else { name.clone() };
                Some(format!("{who}:\n{text}"))
            })
            .collect::<Vec<_>>()
            .join("\n\n")
    };
    if body.is_empty() {
        app.engine.borrow_mut().report_error("Nothing in this chat to select from yet");
        return;
    }
    let name = app.engine.borrow().chat_name(&session);
    open(app, window, format!("Select text · {name} (whole chat)"), body);
}
