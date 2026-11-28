//! A failure card's retries (`POST /turn-retry`, docs/turn-errors.md, Rust
//! Host contract 217): the Host sends the failed turn's message again, at
//! once, at a time, in a fresh session or after compacting. Each button's
//! request is kept by chat, message and action so the card shows it going,
//! done or refused until the chat moves on.

use std::collections::HashMap;

use clarp_core::json::{self, Object};
use clarp_core::turn_error::{Action, RETRY_PATH, TurnError};

use crate::{Change, Engine};

/// Where one retry button stands.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RetryState {
    Sending,
    /// The Host took it: `queued`, `scheduled`, `compacting`, `refreshing`.
    Done(String),
    /// The Host refused it: its HTTP status (0 unreachable) and words.
    Failed(u16, String),
}

#[derive(Default)]
pub(crate) struct Retries {
    states: HashMap<String, RetryState>,
}

const TAG: &str = "turn-retry:";

fn key(session: &str, message_id: &str, action: &str) -> String {
    format!("{session}\u{1f}{message_id}\u{1f}{action}")
}

fn session_of(key: &str) -> &str {
    key.split('\u{1f}').next().unwrap_or_default()
}

impl Engine {
    /// Presses `action` of `error`'s card in `session`: sends the message
    /// again as the action says. Nothing for an action that sends nothing
    /// or one already on its way.
    pub fn turn_retry(&mut self, session: &str, error: &TurnError, action: &Action) {
        on_peer!(self.peer_mut(session), |engine, local| engine.turn_retry(local, error, action));
        let Some(body) = error.retry_body(session, action) else { return };
        let key = key(session, &error.message_id, &action.id);
        if self.retries.states.get(&key) == Some(&RetryState::Sending) {
            return;
        }
        self.retries.states.insert(key.clone(), RetryState::Sending);
        self.api.post_json(&format!("{TAG}{key}"), RETRY_PATH, body, None);
        self.changes.push(Change::Conversation(session.to_owned()));
    }

    /// Where `action` of the card for `message_id` stands; `None` before it
    /// was pressed.
    pub fn turn_retry_state(&self, session: &str, message_id: &str, action: &str) -> Option<RetryState> {
        on_peer!(self.peer_ref(session), |engine, local| engine.turn_retry_state(local, message_id, action));
        self.retries.states.get(&key(session, message_id, action)).cloned()
    }

    pub(crate) fn turn_retry_json(&mut self, tag: &str, object: &Object) -> bool {
        let Some(key) = tag.strip_prefix(TAG) else { return false };
        let state = if object.get("ok").and_then(serde_json::Value::as_bool) == Some(true) {
            RetryState::Done(json::string(object, "status"))
        } else {
            RetryState::Failed(200, json::string(object, "error"))
        };
        self.changes.push(Change::Conversation(session_of(key).to_owned()));
        self.retries.states.insert(key.to_owned(), state);
        true
    }

    pub(crate) fn turn_retry_failure(&mut self, tag: &str, message: &str, status: u16) -> bool {
        let Some(key) = tag.strip_prefix(TAG) else { return false };
        self.changes.push(Change::Conversation(session_of(key).to_owned()));
        self.retries.states.insert(key.to_owned(), RetryState::Failed(status, message.to_owned()));
        true
    }
}
