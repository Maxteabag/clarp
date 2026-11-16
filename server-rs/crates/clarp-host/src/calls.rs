//! Calls at the Host's edge: the record of a hands-free conversation with several agents.
//!
//! The rules are in [`clarp_kernel::calls`]. Here is where they are asked: by an app's
//! buttons (`POST /calls`, `/calls/add`, ...), by a message that carries its call
//! (`call_id` of a send), and by an agent's own tool. One request at a time: the calls are
//! read where the fleet keeps them when the first one comes, each phone's calls are kept
//! again when they changed, the apps are told of each change (`group-call`), and the agents
//! a change concerns find a quiet word about it in their conversations.
//!
//! The routes answer as the Python Host's do (`server/lib/routes/calls.py`,
//! `docs/group-calls.md`): the same statuses, bodies and words.

use std::collections::{BTreeMap, HashMap};

use axum::body::Bytes;
use axum::extract::{Path, Query, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Extension, Json, Router};
use clarp_kernel::calls::{ADMINISTRATOR, Asker, At, Call, Calls, Contact, Effects, Refused};
use serde_json::Value;

use crate::agent::Unavailable;
use crate::edge::{ApiError, App, Principal};
use crate::fleet::Directory;
use crate::ids;
use crate::pairing::Scope;

/// The kind of thing a phone's calls are, among what is left with the fleet.
const KIND: &str = "call";

/// What a phone without full scope is told.
const FULL_ONLY: &str = "Group calls require full-device authentication";

/// The calls of this Host. `None` until they were read where they are kept.
#[derive(Default)]
pub struct Desk {
    calls: tokio::sync::Mutex<Option<Calls>>,
}

/// The agents that may be asked into a call: the ones an app shows as contacts.
fn roster(directory: &Directory) -> Vec<Contact> {
    let shown = directory
        .members
        .iter()
        .map(|member| &member.record)
        .filter(|record| record.archived_at_ms.is_none());
    shown
        .map(|record| Contact {
            session: record.session.clone(),
            agent_id: record.agent_id.clone(),
            persona: record.persona.clone(),
            helper: record.is_helper(),
        })
        .collect()
}

impl Desk {
    /// One request on the calls. `ask` is given the calls, the moment, the agents that may
    /// be asked in, a new id for a call, and where to put what the request does beside its
    /// answer. What it did is done before this answers: kept, told, noted.
    pub async fn ask<T>(
        &self,
        app: &App,
        ask: impl FnOnce(&mut Calls, At, (&[Contact], &str), &mut Effects) -> T,
    ) -> Result<T, Unavailable> {
        let mut held = self.calls.lock().await;
        if held.is_none() {
            let mut books: BTreeMap<String, Vec<Call>> = BTreeMap::new();
            for (principal, kept) in app.fleet.kept_all(KIND).await? {
                // What cannot be read is no call: the phone starts a new one.
                if let Ok(calls) = serde_json::from_slice::<Vec<Call>>(&kept) {
                    books.insert(principal, calls);
                }
            }
            *held = Some(Calls::kept(books));
        }
        let Some(calls) = held.as_mut() else {
            return Err(Unavailable("the calls could not be read".to_string()));
        };
        let directory = app.fleet.directory();
        let roster = roster(&directory);
        let at = At {
            now_ms: app.clock.now_ms(),
            host_id: &app.server_id,
        };
        let mut out = Effects::default();
        let answer = ask(calls, at, (&roster, &ids::hex(16)), &mut out);
        for principal in &out.changed {
            let kept = serde_json::to_vec(calls.of(principal)).unwrap_or_default();
            app.fleet.keep(KIND, principal.clone(), Some(kept)).await?;
        }
        for event in out.events {
            app.hub.publish(event, at.now_ms);
        }
        for notice in out.notices {
            let mut members = directory.members.iter();
            if let Some(member) = members.find(|member| member.record.agent_id == notice.agent_id) {
                let _ = member
                    .agent
                    .note(format!("call:{}", notice.id), notice.text)
                    .await;
            }
        }
        Ok(answer)
    }

    /// Where a message that carries a call goes: the session of the agent that takes it.
    /// `Err` is what the sender is told instead (the call is over, nobody can take speech).
    pub async fn route(
        &self,
        app: &App,
        (caller, call_id, session): (&str, &str, &str),
    ) -> Result<Result<String, Refused>, Unavailable> {
        let asker = Asker {
            caller,
            call_id,
            ..Asker::default()
        };
        let routed = self
            .ask(app, |calls, at, _, out| {
                calls.route(asker, session, at, out)
            })
            .await?;
        Ok(routed.map(|routed| routed["session"].as_str().unwrap_or("").to_string()))
    }
}

/// How many of the newest things of each agent's conversation are looked at for what was
/// said in a call: more than the header quotes of all of them together.
const LOOKED_AT: usize = 40;

impl Desk {
    /// What the agent of this session is told of its call with a turn that starts now: who
    /// else is in it, and what was said since it last spoke. Empty when no call's latest
    /// utterance went to it, or not lately.
    pub async fn header(&self, app: &App, session: &str) -> String {
        use clarp_actor::conversation::Which;
        use clarp_kernel::calls::Said;
        use clarp_kernel::conversation::Entry;
        use clarp_speech::markup;

        let found = self
            .ask(app, |calls, at, _, _| {
                calls.turn_of(session, at.now_ms).cloned()
            })
            .await;
        let Ok(Some(call)) = found else {
            return String::new();
        };
        // What was said in the call's agents' conversations since it began, by people and
        // by the agents at the end of their turns, as a reader sees it, the oldest first.
        let directory = app.fleet.directory();
        let mut said: Vec<(i64, usize, String, bool, String)> = Vec::new();
        for part in &call.participants {
            let mut members = directory.members.iter();
            let Some(member) = members.find(|member| member.record.agent_id == part.agent_id)
            else {
                continue;
            };
            let Ok(page) = member.agent.tail(LOOKED_AT, Which::WithoutAutomated).await else {
                continue;
            };
            for row in page.entries {
                if row.ts_ms < call.started_at || row.live {
                    continue;
                }
                let (by_user, text) = match &row.entry {
                    Entry::Message(message) => (true, &message.text),
                    Entry::Reply(reply) if !row.commentary => (false, &reply.text),
                    _ => continue,
                };
                let text = markup::clean_for_display(text, true);
                if !text.is_empty() {
                    said.push((row.ts_ms, said.len(), part.agent_id.clone(), by_user, text));
                }
            }
        }
        said.sort_by_key(|(at, place, ..)| (*at, *place));
        let said: Vec<Said> = said
            .iter()
            .map(|(_, _, agent_id, by_user, text)| Said {
                agent_id,
                by_user: *by_user,
                text,
            })
            .collect();
        call.header(session, &said)
    }
}

/// Who asks, as the calls know a caller: a phone by its id, or the administrator. A phone
/// that may only read and send has no call.
pub fn caller(principal: &Principal) -> Result<String, ApiError> {
    match principal {
        Principal::Administrator | Principal::Tools => Ok(ADMINISTRATOR.to_string()),
        Principal::Device {
            device_id,
            scope: Scope::Full,
        } => Ok(device_id.clone()),
        Principal::Device { .. } => Err(ApiError::new(StatusCode::UNAUTHORIZED, FULL_ONLY)),
    }
}

fn answered(asked: Result<Result<Value, Refused>, Unavailable>) -> Response {
    match asked {
        Ok(Ok(answer)) => Json(answer).into_response(),
        Ok(Err(refused)) => {
            let status = StatusCode::from_u16(refused.status).unwrap_or(StatusCode::CONFLICT);
            (status, Json(refused.body)).into_response()
        }
        Err(unavailable) => ApiError::from(unavailable).into_response(),
    }
}

/// A field as the text the Python Host makes of it (`str(value or "")`).
fn text(said: &Value, key: &str) -> String {
    match &said[key] {
        Value::String(text) => text.clone(),
        Value::Number(number) => number.to_string(),
        _ => String::new(),
    }
}

/// The agent a change is made by, when the request names one this Host has: its session.
fn by(app: &App, said: &Value) -> String {
    let named = text(said, "by");
    let directory = app.fleet.directory();
    let member = directory.named(named.trim());
    member.map_or_else(String::new, |member| member.record.session.clone())
}

fn body(bytes: &Bytes) -> Value {
    serde_json::from_slice(bytes).unwrap_or(Value::Null)
}

/// `GET /calls`: the call that is up for the phone that asks, or none. The administrator
/// may name the phone (`?principal=`).
async fn snapshot(
    State(app): State<App>,
    Extension(principal): Extension<Principal>,
    Query(asked): Query<HashMap<String, String>>,
) -> Response {
    let caller = match caller(&principal) {
        Ok(caller) => caller,
        Err(refused) => return refused.into_response(),
    };
    let named = match caller.as_str() {
        ADMINISTRATOR => asked.get("principal").cloned().unwrap_or_default(),
        _ => String::new(),
    };
    let asker = Asker {
        caller: &caller,
        principal: &named,
        ..Asker::default()
    };
    let desk = app.calls.clone();
    answered(
        desk.ask(&app, |calls, at, _, out| calls.snapshot(asker, at, out))
            .await,
    )
}

/// `POST /calls` with `{agents: [...], request_id?, principal?, by?}`: a call with one to
/// eight agents; the first has the floor.
async fn start(
    State(app): State<App>,
    Extension(principal): Extension<Principal>,
    bytes: Bytes,
) -> Response {
    let caller = match caller(&principal) {
        Ok(caller) => caller,
        Err(refused) => return refused.into_response(),
    };
    let said = body(&bytes);
    let Some(agents) = said["agents"].as_array() else {
        return ApiError::new(StatusCode::BAD_REQUEST, "agents must be a list").into_response();
    };
    let agents: Vec<String> = agents
        .iter()
        .map(|agent| match agent {
            Value::String(name) => name.clone(),
            Value::Number(number) => number.to_string(),
            _ => String::new(),
        })
        .collect();
    let (request_id, named, by) = (
        text(&said, "request_id"),
        text(&said, "principal"),
        by(&app, &said),
    );
    let asker = Asker {
        caller: &caller,
        principal: &named,
        by: &by,
        ..Asker::default()
    };
    let desk = app.calls.clone();
    let asked = desk.ask(&app, |calls, at, with, out| {
        calls.start(asker, (&agents, &request_id, None), with, at, out)
    });
    answered(asked.await)
}

/// `POST /calls/<action>` with `{agent, call_id?, principal?, by?}`: add, remove, hold,
/// resume, switch to or transfer to one agent; and `POST /calls/end`.
async fn change(
    State(app): State<App>,
    Extension(principal): Extension<Principal>,
    Path(action): Path<String>,
    bytes: Bytes,
) -> Response {
    let caller = match caller(&principal) {
        Ok(caller) => caller,
        Err(refused) => return refused.into_response(),
    };
    let said = body(&bytes);
    if !said.is_object() {
        return ApiError::new(StatusCode::BAD_REQUEST, "bad json").into_response();
    }
    let (call_id, named, by) = (
        text(&said, "call_id"),
        text(&said, "principal"),
        by(&app, &said),
    );
    let agent = text(&said, "agent");
    let asker = Asker {
        caller: &caller,
        call_id: &call_id,
        principal: &named,
        by: &by,
    };
    let desk = app.calls.clone();
    let asked = desk.ask(&app, |calls, at, (roster, _), out| match action.as_str() {
        "end" => calls.end(asker, at, out),
        action => calls.change(asker, (action, &agent), roster, at, out),
    });
    answered(asked.await)
}

pub fn routes() -> Router<App> {
    Router::new()
        .route("/calls", get(snapshot).post(start))
        .route("/calls/{action}", post(change))
}
