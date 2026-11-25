//! What one OpenCode turn makes of its server's events, the same in the per-turn driver and in
//! the OpenCode lane: which events are the turn's record, which questions it answers for the
//! user, and when the turn is over.
use std::collections::BTreeSet;

use clarp_tool_args::opencode;
use serde_json::{Value, json};

use crate::opencode_http::{Client, Result, component};

/// The body of a prompt: the words, and the model and effort where the turn names them.
pub(crate) fn body(text: &str, model: &str, effort: &str) -> Value {
    let mut body = json!({"parts":[{"type":"text","text":text}]});
    if let Some(model) = opencode::model_pair(model) {
        body["model"] = model;
    }
    if !effort.is_empty() {
        body["variant"] = json!(effort);
    }
    body
}

/// One turn of the session `sid`, followed event by event.
pub(crate) struct Follow {
    sid: String,
    /// The turn's session and the sessions it started (a task's sub-agents): a question of
    /// any of them is the turn's to answer.
    sessions: BTreeSet<String>,
    /// The session has started working on the prompt: an idle after this ends the turn.
    busy: bool,
}

impl Follow {
    pub(crate) fn new(sid: &str) -> Self {
        Self {
            sid: sid.to_owned(),
            sessions: BTreeSet::from([sid.to_owned()]),
            busy: false,
        }
    }

    /// Handles one event: answers a permission or question of the turn's sessions, hands the
    /// turn's record to `emit`. True once the turn is over.
    pub(crate) fn see(
        &mut self,
        client: &Client,
        ev: &Value,
        emit: &mut dyn FnMut(&Value) -> Result<()>,
    ) -> Result<bool> {
        let props = &ev["properties"];
        let kind = ev["type"].as_str().unwrap_or("");
        let mine = props["sessionID"].as_str() == Some(&self.sid);
        match kind {
            "session.created" => {
                if props["info"]["parentID"]
                    .as_str()
                    .is_some_and(|id| self.sessions.contains(id))
                    && let Some(id) = props["info"]["id"].as_str()
                {
                    self.sessions.insert(id.to_string());
                }
            }
            "permission.asked" | "question.asked" => {
                if props["sessionID"]
                    .as_str()
                    .is_some_and(|id| self.sessions.contains(id))
                    && let Some(id) = props["id"].as_str().filter(|id| !id.is_empty())
                {
                    let id = component(id);
                    if kind == "permission.asked" {
                        client.post(
                            &format!("/permission/{id}/reply"),
                            Some(&json!({"reply":"once"})),
                            30,
                        )?;
                    } else {
                        client.post(&format!("/question/{id}/reject"), None, 30)?;
                    }
                }
            }
            "message.part.updated" if props["part"]["sessionID"] == self.sid.as_str() => {
                self.busy |= props["part"]["type"] == "step-start";
                emit(ev)?;
            }
            "message.part.delta" | "session.error" if mine => emit(ev)?,
            // An answer of the turn's session names the model that gave it.
            "message.updated"
                if props["info"]["sessionID"] == self.sid.as_str()
                    && props["info"]["role"] == "assistant" =>
            {
                emit(ev)?;
            }
            // A retry that no retry gets past (the provider refused the account): OpenCode
            // would retry without end. Its words go to the record, the turn is let go.
            "session.status" if mine && opencode::refused(&props["status"]).is_some() => {
                emit(ev)?;
                let _ = client.abort(&self.sid, 30);
                return Ok(true);
            }
            "session.status" if mine => match props["status"]["type"].as_str() {
                Some("busy" | "retry") => self.busy = true,
                Some("idle") if self.busy => return Ok(true),
                _ => {}
            },
            "session.idle" if mine && self.busy => return Ok(true),
            _ => {}
        }
        Ok(false)
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;

    fn seen(events: &[Value]) -> (Vec<String>, Option<usize>) {
        // Nothing here asks the server anything: no permission or question is the turn's.
        let client = Client::new(9, "unused", "/w".into());
        let mut turn = Follow::new("ses_a");
        let mut kept = Vec::new();
        for (at, event) in events.iter().enumerate() {
            let over = turn
                .see(&client, event, &mut |line| {
                    kept.push(line["type"].as_str().unwrap_or_default().to_owned());
                    Ok(())
                })
                .unwrap();
            if over {
                return (kept, Some(at));
            }
        }
        (kept, None)
    }

    fn status(session: &str, kind: &str) -> Value {
        json!({"type":"session.status","properties":{"sessionID":session,"status":{"type":kind}}})
    }

    #[test]
    fn a_turn_ends_at_the_idle_after_its_work_began_and_keeps_only_its_own_lines() {
        let step = json!({"type":"message.part.updated","properties":{"part":{"sessionID":"ses_a","type":"step-start"}}});
        let other = json!({"type":"message.part.updated","properties":{"part":{"sessionID":"ses_b","type":"text"}}});
        // An idle left from before the prompt does not end the turn.
        let (kept, over) = seen(&[
            status("ses_a", "idle"),
            other.clone(),
            step.clone(),
            status("ses_b", "idle"),
            status("ses_a", "idle"),
        ]);
        assert_eq!(kept, ["message.part.updated"]);
        assert_eq!(over, Some(4));
        // Busy is enough to begin; `session.idle` ends as the status does.
        let idle = json!({"type":"session.idle","properties":{"sessionID":"ses_a"}});
        assert_eq!(seen(&[status("ses_a", "busy"), idle]).1, Some(1));
        assert_eq!(seen(&[status("ses_a", "retry")]).1, None);
    }

    #[test]
    fn a_prompt_names_its_model_and_effort_only_when_the_turn_does() {
        assert_eq!(
            body("hi", "", ""),
            json!({"parts":[{"type":"text","text":"hi"}]})
        );
        let named = body("hi", "opencode/big-pickle", "high");
        assert_eq!(
            named["model"],
            json!({"providerID":"opencode","modelID":"big-pickle"})
        );
        assert_eq!(named["variant"], "high");
    }
}
