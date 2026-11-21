//! Who an agent is when nobody named it: the contact an agent that is made takes from its
//! tool's pool or has minted for it (`POST /agents` with `auto_contact` or `mint_contact`),
//! and the contact a running agent is given (`POST /agent-assign`). The rules are the kernel's
//! ([`clarp_kernel::contacts`]); here they are read against the personas and agents there
//! are, and a minted contact is written as a persona of the owner's.
//!
//! A minted contact has no picture of its own: the Python Host asks OpenAI for one afterwards
//! (`persona-avatar`), and this Host asks no image provider (docs/differences.md).

use axum::body::Bytes;
use axum::extract::State;
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::post;
use axum::{Json, Router};
use clarp_kernel::contacts::{self, Assigned, Assignee, Contact, Wearer};
use serde_json::{Value, json};

use crate::edge::{ApiError, App};
use crate::fleet::{AgentRecord, Amend, Directory, NotAmended, PersonaSaid};

/// Every persona there is, as an app is told them: the built-in ones in the roster's order,
/// then the owner's in the order he made them (the order the pool is read in).
fn personas_of(app: &App, directory: &Directory) -> Vec<Value> {
    let [named, cartesia, gemini] = crate::owner::personas(&app.mouth.owners());
    directory.personas.listed(&clarp_kernel::personas::Owner {
        roster: &named,
        cartesia: &cartesia,
        gemini: &gemini,
    })
}

/// The personas as the pool reads them. The tier is their name's.
pub(crate) fn contacts_of(personas: &[Value]) -> Vec<Contact<'_>> {
    personas
        .iter()
        .map(|persona| Contact {
            name: persona["name"].as_str().unwrap_or(""),
            voice_id: persona["voice_id"].as_str().unwrap_or(""),
            tier: "",
        })
        .collect()
}

/// Every member of the fleet as the pool reads it: the agents of the roster and the
/// janitors, which go by names too.
pub(crate) fn wearers_of<'a>(records: impl Iterator<Item = &'a AgentRecord>) -> Vec<Wearer<'a>> {
    records
        .map(|record| Wearer {
            agent_id: &record.agent_id,
            persona: &record.persona,
            voice_id: &record.voice_id,
        })
        .collect()
}

/// The members of the fleet as a snapshot of it has them.
fn members_of(directory: &Directory) -> impl Iterator<Item = &AgentRecord> {
    directory
        .members
        .iter()
        .chain(&directory.janitors)
        .map(|member| &member.record)
}

fn refused(status: u16, error: &str, message: &str) -> Response {
    let status = StatusCode::from_u16(status).unwrap_or(StatusCode::CONFLICT);
    (status, Json(json!({ "error": error, "message": message }))).into_response()
}

/// A field as the Python Host reads it with `str(... or "")`: a text as it is, a number in
/// its digits, anything that says nothing as nothing.
fn said(fields: &serde_json::Map<String, Value>, key: &str, otherwise: &str) -> String {
    match fields.get(key) {
        Some(Value::String(text)) if !text.is_empty() => text.clone(),
        Some(Value::Number(number)) if number.as_f64() != Some(0.0) => number.to_string(),
        Some(Value::Bool(true)) => "True".to_string(),
        _ => otherwise.to_string(),
    }
}

/// `POST /agent-assign` with `{session, mode?, name?}`: a running agent is given a contact
/// (`contact_assignment.assign_contact`), its conversation kept. `options` lists the contacts
/// of its pool; `auto` keeps its own where it is in the pool, else takes the pool's first,
/// else one minted for it; `choose` takes any persona of that name nobody else goes by;
/// `create` makes a persona of a new name. The apps are told (`contact-assigned`), except of
/// `options`. A janitor is refused before this (`edge`: inspection only).
async fn assign(State(app): State<App>, body: Bytes) -> Result<Response, ApiError> {
    let asked = if body.is_empty() {
        json!({})
    } else {
        serde_json::from_slice(&body).unwrap_or(Value::Null)
    };
    let Some(asked) = asked.as_object() else {
        return Err(ApiError::new(StatusCode::BAD_REQUEST, "bad json"));
    };
    let session = said(asked, "session", "");
    let mode = said(asked, "mode", "auto");
    let decided = {
        let directory = app.fleet.directory();
        let personas = personas_of(&app, &directory);
        let contacts = contacts_of(&personas);
        let agent = directory.by_session(&session).map(|member| Assignee {
            agent_id: &member.record.agent_id,
            persona: &member.record.persona,
            backend: &member.record.backend,
            janitor: member.record.is_janitor,
        });
        contacts::assign(
            &mode,
            asked.get("name"),
            agent,
            &contacts,
            &wearers_of(members_of(&directory)),
        )
    };
    let assigned = match decided {
        Ok(decided) => decided,
        Err(why) => return Ok(refused(why.status, why.error, why.message)),
    };
    let name = match assigned {
        Assigned::Options(names) => {
            let contacts: Vec<Value> = names.iter().map(|name| json!({ "name": name })).collect();
            return Ok(
                Json(json!({ "ok": true, "session": session, "contacts": contacts }))
                    .into_response(),
            );
        }
        // `auto` (a contact taken or minted) is decided again in the fleet's own turn, which
        // gives it: two assignments at once never take the same contact, nor mint the same
        // name (`ContactAuto`). What was decided here is only that it is refused nothing.
        Assigned::Mint => String::new(),
        Assigned::Take(name) => name,
        // A persona of a new name, made as the owner makes one without a voice: one nobody
        // speaks with is picked for it.
        Assigned::Create(name) => {
            let said = PersonaSaid {
                name,
                ..PersonaSaid::default()
            };
            match app
                .fleet
                .persona_mint(said, app.mouth.cached_voice_names().0)
                .await?
            {
                Ok(persona) => persona["name"].as_str().unwrap_or("").to_string(),
                Err(why) => {
                    let told = json!({ "error": why });
                    return Ok((StatusCode::CONFLICT, Json(told)).into_response());
                }
            }
        }
    };
    // The reference asks a picture after an assignment that made a contact: `create`, and
    // `auto` where it minted one; a contact with a picture of its own is left by the asking.
    let asks_picture = mode == "create" || mode == "auto";
    let change = if mode == "auto" {
        Amend::ContactAuto(app.mouth.cached_voice_names().0)
    } else {
        Amend::Contact(name, app.mouth.cached_voice_names().0)
    };
    let record = match app.fleet.amend(session.clone(), change).await? {
        Ok(record) => record,
        Err(NotAmended::Unknown) => return Ok(refused(404, "no such agent", "no such agent")),
        Err(NotAmended::NoContact) => {
            let word = "contact_pool_empty";
            return Ok(refused(409, word, word));
        }
        Err(NotAmended::Occupied { owner, session }) => {
            let told = crate::roster::occupied(&owner, &session);
            return Ok((StatusCode::CONFLICT, Json(told)).into_response());
        }
    };
    // A contact minted or made here is asked its picture (`needs_avatar`); nothing waits for it.
    if asks_picture {
        let (app, name, backend) = (app.clone(), record.persona.clone(), record.backend.clone());
        tokio::spawn(async move { crate::pictures::ask(&app, &name, "", &backend).await });
    }
    let told = crate::wire::roster_contact_assigned(&record.session, &record.persona);
    app.hub.publish(told, app.clock.now_ms());
    Ok(Json(json!({
        "ok": true,
        "session": session,
        "name": record.persona,
        "agent_id": record.agent_id,
    }))
    .into_response())
}

pub fn routes() -> Router<App> {
    Router::new().route("/agent-assign", post(assign))
}
