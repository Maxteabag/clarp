//! The owner can change what a chat displays without touching its conversation.

use axum::Json;
use axum::body::Bytes;
use axum::extract::State;
use axum::http::{HeaderMap, StatusCode};
use clarp_kernel::hidden_coordination::{self as rules, Visibility};
use clarp_kernel::python;
use serde_json::{Value, json};

use crate::edge::{ApiError, App};
use crate::fleet::Directory;

pub(crate) fn visibility(
    directory: &Directory,
    agent_id: &str,
) -> (Visibility, Option<Visibility>) {
    let global = directory
        .setting(rules::GLOBAL_KEY)
        .and_then(Visibility::read)
        .unwrap_or_default();
    let chat = directory
        .setting(&rules::chat_key(agent_id))
        .and_then(Visibility::read);
    (global, chat)
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
