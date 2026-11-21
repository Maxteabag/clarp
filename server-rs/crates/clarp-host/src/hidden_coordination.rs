//! The owner can change what a chat displays without touching its conversation.

use std::collections::{BTreeMap, HashMap};

use axum::Json;
use axum::body::Bytes;
use axum::extract::State;
use axum::http::{HeaderMap, StatusCode};
use clarp_kernel::hidden_coordination::{self as rules, Visibility};
use clarp_kernel::python;
use serde_json::{Value, json};

use crate::edge::{ApiError, App};
use crate::fleet::{AgentRecord, Directory};

#[derive(Default)]
pub(crate) struct Roster {
    global: Visibility,
    chats: HashMap<String, (Option<Visibility>, bool)>,
}

impl Roster {
    /// The visibility choices that belong to this published directory. The current turn key
    /// can become readable independently, so callers still apply the actor's note capability.
    pub(crate) fn of<'a>(
        settings: &BTreeMap<String, String>,
        agents: impl Iterator<Item = &'a AgentRecord>,
    ) -> Self {
        let read = |key: &str| settings.get(key).and_then(|word| Visibility::read(word));
        let global = read(rules::GLOBAL_KEY).unwrap_or_default();
        let chats = agents
            .map(|agent| {
                let own = read(&rules::chat_key(&agent.agent_id));
                let hides = own.unwrap_or(global) == Visibility::Hide
                    && clarp_kernel::coordination::can_leave_note(
                        &agent.agent_id,
                        Some(&agent.backend),
                        true,
                    );
                (agent.agent_id.clone(), (own, hides))
            })
            .collect();
        Self { global, chats }
    }
}

pub(crate) fn visibility(
    directory: &Directory,
    agent_id: &str,
) -> (Visibility, Option<Visibility>) {
    let roster = &directory.coordination;
    (
        roster.global,
        roster.chats.get(agent_id).and_then(|(own, _)| *own),
    )
}

pub(crate) fn hides(directory: &Directory, agent_id: &str) -> bool {
    let roster = &directory.coordination;
    roster
        .chats
        .get(agent_id)
        .map_or(roster.global == Visibility::Hide, |(_, hides)| *hides)
}

fn owner(headers: &HeaderMap) -> Result<(), ApiError> {
    if crate::coordination::from_an_agent(headers) {
        return Err(ApiError::new(StatusCode::FORBIDDEN, "agents_may_not"));
    }
    Ok(())
}

fn bad(words: &str) -> ApiError {
    ApiError::new(StatusCode::BAD_REQUEST, words)
}

pub(crate) async fn get(State(app): State<App>) -> Json<Value> {
    let (global, _) = visibility(&app.fleet.directory(), "");
    Json(json!({"coordination_visibility": global}))
}

pub(crate) async fn set(
    State(app): State<App>,
    headers: HeaderMap,
    body: Bytes,
) -> Result<Json<Value>, ApiError> {
    owner(&headers)?;
    let asked = serde_json::from_slice::<Value>(&body).unwrap_or_default();
    let asked = asked
        .get("coordination_visibility")
        .ok_or_else(|| bad("coordination_visibility required"))?;
    let value = asked
        .as_str()
        .and_then(Visibility::read)
        .ok_or_else(|| bad(rules::GLOBAL_ERROR))?;
    let effective = app.fleet.set_coordination(None, Some(value)).await?;
    Ok(Json(json!({"coordination_visibility": effective})))
}

pub(crate) async fn set_chat(
    State(app): State<App>,
    headers: HeaderMap,
    body: Bytes,
) -> Result<Json<Value>, ApiError> {
    owner(&headers)?;
    let asked = serde_json::from_slice::<Value>(&body)
        .ok()
        .filter(Value::is_object)
        .ok_or_else(|| bad("bad json"))?;
    let word = &asked["session"];
    let session = if let Some(text) = word.as_str() {
        python::strip(text).to_owned()
    } else if python::truthy(word) {
        python::repr(word)
    } else {
        String::new()
    };
    let raw = asked
        .get("visibility")
        .filter(|_| !session.is_empty())
        .ok_or_else(|| bad("session and visibility required"))?;
    let directory = app.fleet.directory();
    let agent = directory
        .anyone(&session)
        .ok_or_else(|| ApiError::new(StatusCode::NOT_FOUND, "no such agent"))?;
    let own = rules::chat_choice(raw.as_str().unwrap_or("")).map_err(bad)?;
    let effective = app
        .fleet
        .set_coordination(Some((agent.record.agent_id.clone(), session.clone())), own)
        .await?;
    Ok(Json(json!({
        "ok": true,
        "session": session,
        "coordination_visibility": effective,
        "coordination_override": own,
    })))
}

pub(crate) fn gaps(
    answer: &mut Value,
    page: &clarp_actor::conversation::Page,
    directory: &Directory,
) {
    let public = |run: &clarp_actor::conversation::HiddenRun| {
        let agents: Vec<_> = run
            .senders
            .iter()
            .map(|id| {
                let name = directory
                    .named(id)
                    .map_or("", |member| member.record.persona.as_str());
                json!({"agent_id": id, "name": name})
            })
            .collect();
        json!({"messages": run.messages, "exchanges": run.exchanges,
               "agents": agents, "capped": run.capped})
    };
    if let Some(rows) = answer["turns"].as_array_mut() {
        for row in rows {
            if let Some(run) = row["id"].as_str().and_then(|id| page.hidden_before.get(id)) {
                row["hidden_before"] = public(run);
            }
        }
    }
    if let Some(run) = &page.hidden_after {
        answer["hidden_after"] = public(run);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn published_visibility_changes_with_settings_and_the_roster() {
        let mut agents = [
            AgentRecord {
                agent_id: "ivy".into(),
                backend: "claude".into(),
                ..AgentRecord::default()
            },
            AgentRecord {
                agent_id: "oak".into(),
                backend: "codex".into(),
                ..AgentRecord::default()
            },
            AgentRecord {
                agent_id: "moss".into(),
                backend: "muse".into(),
                ..AgentRecord::default()
            },
        ];
        let mut settings = BTreeMap::from([
            (rules::GLOBAL_KEY.into(), "hide".into()),
            (rules::chat_key("oak"), "show".into()),
        ]);
        let directory = |settings: &BTreeMap<String, String>, agents: &[AgentRecord]| Directory {
            coordination: Roster::of(settings, agents.iter()),
            ..Directory::default()
        };
        let prior = directory(&settings, &agents);
        assert!(hides(&prior, "ivy"));
        assert!(!hides(&prior, "oak"));
        assert!(!hides(&prior, "moss"));
        assert_eq!(
            visibility(&prior, "oak"),
            (Visibility::Hide, Some(Visibility::Show))
        );

        settings.insert(rules::GLOBAL_KEY.into(), "show".into());
        settings.insert(rules::chat_key("ivy"), "hide".into());
        settings.remove(&rules::chat_key("oak"));
        agents[0].backend = "muse".into();
        let current = directory(&settings, &agents);
        assert!(!hides(&current, "ivy"));
        assert_eq!(
            visibility(&current, "ivy"),
            (Visibility::Show, Some(Visibility::Hide))
        );
        assert_eq!(visibility(&current, "oak"), (Visibility::Show, None));
        assert!(hides(&prior, "ivy"));
        assert_eq!(
            visibility(&prior, "oak"),
            (Visibility::Hide, Some(Visibility::Show))
        );
    }
}
