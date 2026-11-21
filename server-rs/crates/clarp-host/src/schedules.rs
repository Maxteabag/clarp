//! Schedules at the Host's edge: a prompt an agent is sent at times a cron expression names.
//!
//! The rules are in [`clarp_kernel::schedules`]. Here is where they are asked: by an app's
//! screen and an agent's own tool (`clarp-admin schedule`), one request at a time; where the
//! schedules are kept (with the fleet, whole); and the runner that looks for what is due,
//! moves each schedule on first and then sends its prompt to its agent, as something the
//! Host sends by itself: nobody is waited for, and nothing is said aloud.
//!
//! The routes answer as the Python Host's do (`server/lib/routes/schedules.py`): the same
//! statuses, bodies and words.

use std::collections::HashMap;
use std::time::Duration;

use axum::body::Bytes;
use axum::extract::{Path, Query, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::{delete, get, post};
use axum::{Json, Router};
use clarp_kernel::python;
use clarp_kernel::schedules::{Change, Made, Schedule, Schedules};
use clarp_protocol::requests::SendRequest;
use serde_json::{Value, json};

use crate::agent::Unavailable;
use crate::edge::{self, ApiError, App};
use crate::ids;

/// What this desk keeps, declared once (`kept.rs`: the registry walks it at every start).
pub(crate) const KEPT: &[crate::kept::Kind] =
    &[crate::kept::Kind::book(KIND, crate::kept::Key::Plain, fits_book).by_entries(unfit_entries)];

/// The kind of thing the schedules are, among what is left with the fleet, and their name.
const KIND: &str = "schedule";
const ALL: &str = "all";

/// The schedules of this Host. `None` until they were read where they are kept.
#[derive(Default)]
pub struct Desk {
    schedules: tokio::sync::Mutex<Option<Schedules>>,
    /// The schedules kept that are not schedules of this Host's, put back with every save.
    carried: std::sync::Mutex<crate::kept::Carried>,
}

fn fits_book(stored: &[u8]) -> bool {
    crate::kept::fits_carried::<Vec<Schedule>, Schedule>(stored, &[])
}

fn unfit_entries(stored: &[u8]) -> Vec<String> {
    crate::kept::unfit::<Vec<Schedule>, Schedule>(stored, &[])
}

impl Desk {
    /// One request on the schedules. What it changed is kept before this answers.
    pub async fn ask<T>(
        &self,
        app: &App,
        ask: impl FnOnce(&mut Schedules, i64) -> T,
    ) -> Result<T, Unavailable> {
        let mut held = self.schedules.lock().await;
        if held.is_none() {
            // Through the loader: what cannot be read is no schedule, and nothing is sent on a
            // guess; a schedule that is not one of this Host's is carried through (`kept.rs`).
            let (rows, carried) = app
                .fleet
                .read_book::<Vec<Schedule>, Schedule>(KIND, ALL, &[])
                .await?;
            if let Ok(mut kept) = self.carried.lock() {
                *kept = carried;
            }
            *held = Some(Schedules::kept(rows));
        }
        let Some(schedules) = held.as_mut() else {
            return Err(Unavailable::new(
                "the schedules could not be read".to_string(),
            ));
        };
        let mut next = schedules.clone();
        let answer = ask(&mut next, app.clock.now_ms());
        if next != *schedules {
            let carried = self.carried.lock().map(|c| c.clone()).unwrap_or_default();
            let kept = crate::kept::saved(&next.rows(), &carried);
            app.fleet.keep(KIND, ALL.to_string(), Some(kept)).await?;
            *schedules = next;
        }
        Ok(answer)
    }

    /// A schedule is made for the agent a request names by its session or its id. `Err`:
    /// why not, in words.
    pub async fn create(
        &self,
        app: &App,
        (named, name, cron, prompt): (&str, &str, &str, &str),
        enabled: bool,
    ) -> Result<Result<Value, String>, Unavailable> {
        let directory = app.fleet.directory();
        let by_id = || {
            let mut members = directory.members.iter();
            members.find(|member| member.record.agent_id == named)
        };
        let member = directory.by_session(named).or_else(by_id);
        let agent = member.map(|member| {
            (
                member.record.agent_id.as_str(),
                member.record.session.as_str(),
            )
        });
        let made = Made {
            agent,
            named,
            name,
            cron,
            prompt,
            enabled,
        };
        let new_id = ids::hex(16);
        self.ask(app, |schedules, now| {
            let made = schedules.create(&made, &new_id, now);
            made.map(|schedule| json!(schedule))
        })
        .await
    }

    /// The runner's turn: what is due is moved on, and its prompts are sent.
    async fn run(&self, app: &App) {
        let Ok(due) = self.ask(app, |schedules, now| schedules.run(now)).await else {
            return;
        };
        for (agent_id, _, prompt) in due {
            // To the agent the schedule is for, whatever its session is called now. One
            // that is no more is sent nothing.
            let directory = app.fleet.directory();
            let mut members = directory.members.iter();
            let Some(member) = members.find(|member| member.record.agent_id == agent_id) else {
                continue;
            };
            let request = SendRequest {
                session: member.record.session.clone(),
                text: prompt,
                client_msg_id: Some(format!("schedule-{}", ids::hex(8))),
                synthesize_audio: Some(false),
                hands_free: Some(false),
                force_session: Some(true),
                origin: Some("automation".to_string()),
                ..SendRequest::default()
            };
            if let Err(refused) = edge::send_own(app, request).await {
                eprintln!(
                    "clarp-host: a scheduled prompt for {} was not sent: {}",
                    member.record.session,
                    refused.message()
                );
            }
        }
    }
}

/// Starts the runner: it looks for what is due every so often.
pub fn run(app: &App) {
    let app = app.clone();
    let every = Duration::from_millis(app.config.schedules_checked_every_ms.max(50));
    tokio::spawn(async move {
        let desk = app.schedules.clone();
        loop {
            tokio::time::sleep(every).await;
            crate::request_tool::settle_failed_waits(&app).await;
            desk.run(&app).await;
        }
    });
}

// ---- the routes ----------------------------------------------------------------------------

fn refuse(status: StatusCode, why: &str) -> Response {
    ApiError::new(status, why).into_response()
}

/// What a request says: an object, or nothing at all, which is an empty one.
fn said(bytes: &Bytes) -> Option<Value> {
    let said = if bytes.is_empty() {
        json!({})
    } else {
        serde_json::from_slice(bytes).unwrap_or(Value::Null)
    };
    said.is_object().then_some(said)
}

/// A field as the text the Python Host makes of it (`str(value or "").strip()`).
fn text(said: &Value, key: &str) -> String {
    let text = match &said[key] {
        Value::String(text) => text.clone(),
        Value::Number(number) => number.to_string(),
        _ => String::new(),
    };
    python::strip(&text).to_string()
}

/// Whether a value counts as yes, as Python takes any value for one.
fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(yes) => *yes,
        Value::Number(number) => number.as_f64().is_some_and(|number| number != 0.0),
        Value::String(text) => !text.is_empty(),
        Value::Array(list) => !list.is_empty(),
        Value::Object(fields) => !fields.is_empty(),
    }
}

fn answered<T>(asked: Result<T, Unavailable>, shown: impl FnOnce(T) -> Response) -> Response {
    match asked {
        Ok(answer) => shown(answer),
        Err(unavailable) => ApiError::from(unavailable).into_response(),
    }
}

/// `GET /agent-schedules?session=&agent_id=`: the schedules of one agent, or all of them.
async fn list(State(app): State<App>, Query(asked): Query<HashMap<String, String>>) -> Response {
    let named = |key: &str| {
        let given = asked.get(key).map_or("", String::as_str);
        python::strip(given).to_string()
    };
    let (session, agent_id) = (named("session"), named("agent_id"));
    let desk = app.schedules.clone();
    let listed = desk.ask(
        &app,
        |schedules, _| json!({ "schedules": schedules.list(&session, &agent_id) }),
    );
    answered(listed.await, |listed| Json(listed).into_response())
}

/// `POST /agent-schedules` with `{session, name, cron_expression, prompt, enabled}`: a
/// schedule is made.
async fn create(State(app): State<App>, bytes: Bytes) -> Response {
    let Some(said) = said(&bytes) else {
        return refuse(StatusCode::BAD_REQUEST, "bad json");
    };
    let cron = match text(&said, "cron_expression") {
        none if none.is_empty() => text(&said, "cron"),
        given => given,
    };
    let (session, name, prompt) = (
        text(&said, "session"),
        text(&said, "name"),
        text(&said, "prompt"),
    );
    if [&session, &name, &cron, &prompt]
        .iter()
        .any(|given| given.is_empty())
    {
        let why = "session, name, cron_expression, and prompt are required";
        return refuse(StatusCode::BAD_REQUEST, why);
    }
    let enabled = said.get("enabled").is_none_or(truthy);
    let desk = app.schedules.clone();
    let made = desk.create(&app, (&session, &name, &cron, &prompt), enabled);
    answered(made.await, |made| match made {
        Ok(schedule) => {
            let made = json!({ "ok": true, "schedule": schedule });
            (StatusCode::CREATED, Json(made)).into_response()
        }
        Err(why) => refuse(StatusCode::BAD_REQUEST, &why),
    })
}

/// `POST /agent-schedules/toggle` with `{schedule_id, enabled}`: a schedule is turned off
/// or on.
async fn toggle(State(app): State<App>, bytes: Bytes) -> Response {
    let Some(said) = said(&bytes) else {
        return refuse(StatusCode::BAD_REQUEST, "bad json");
    };
    let schedule_id = text(&said, "schedule_id");
    let (false, Some(enabled)) = (schedule_id.is_empty(), said["enabled"].as_bool()) else {
        let why = "schedule_id and enabled boolean required";
        return refuse(StatusCode::BAD_REQUEST, why);
    };
    let change = Change {
        enabled: Some(enabled),
        ..Change::default()
    };
    changed(&app, &schedule_id, &change).await
}

/// A schedule is changed, and shown as it is now.
async fn changed(app: &App, schedule_id: &str, change: &Change<'_>) -> Response {
    let desk = app.schedules.clone();
    let done = desk.ask(app, |schedules, now| {
        let done = schedules.update(schedule_id, change, now);
        done.map(|schedule| schedule.map(|schedule| json!(schedule)))
    });
    answered(done.await, |done| match done {
        Ok(Some(schedule)) => Json(json!({ "ok": true, "schedule": schedule })).into_response(),
        Ok(None) => refuse(StatusCode::NOT_FOUND, "schedule not found"),
        Err(why) => refuse(StatusCode::BAD_REQUEST, &why),
    })
}

/// `POST /agent-schedules/delete` with `{schedule_id}`: a schedule is removed; the answer
/// says whether there was one.
async fn remove(State(app): State<App>, bytes: Bytes) -> Response {
    let Some(said) = said(&bytes) else {
        return refuse(StatusCode::BAD_REQUEST, "bad json");
    };
    let schedule_id = text(&said, "schedule_id");
    if schedule_id.is_empty() {
        return refuse(StatusCode::BAD_REQUEST, "schedule_id required");
    }
    let desk = app.schedules.clone();
    let removed = desk.ask(&app, |schedules, _| schedules.delete(&schedule_id));
    answered(removed.await, |removed| {
        Json(json!({ "ok": removed })).into_response()
    })
}

/// The id a path names: what follows `/schedules/`, without slashes around it.
fn named(path: &str) -> &str {
    path.trim_matches('/')
}

/// `DELETE /schedules/<id>`: a schedule is removed.
async fn remove_named(State(app): State<App>, Path(schedule_id): Path<String>) -> Response {
    let schedule_id = named(&schedule_id).to_string();
    let desk = app.schedules.clone();
    let removed = desk.ask(&app, |schedules, _| schedules.delete(&schedule_id));
    answered(removed.await, |removed| {
        if removed {
            Json(json!({ "ok": true })).into_response()
        } else {
            refuse(StatusCode::NOT_FOUND, "schedule not found")
        }
    })
}

/// `PATCH /schedules/<id>` with any of `{enabled, name, cron_expression, prompt}`: a
/// schedule is changed. What is given empty is left as it is.
async fn change_named(
    State(app): State<App>,
    Path(schedule_id): Path<String>,
    bytes: Bytes,
) -> Response {
    let Some(said) = said(&bytes) else {
        return refuse(StatusCode::BAD_REQUEST, "bad json");
    };
    let given = |key: &str| truthy(&said[key]).then(|| text(&said, key));
    let (name, prompt) = (given("name"), given("prompt"));
    let cron = given("cron_expression").or_else(|| given("cron"));
    let change = Change {
        enabled: said["enabled"].as_bool(),
        name: name.as_deref(),
        cron: cron.as_deref(),
        prompt: prompt.as_deref(),
    };
    changed(&app, named(&schedule_id), &change).await
}

pub fn routes() -> Router<App> {
    Router::new()
        .route("/agent-schedules", get(list).post(create))
        .route("/agent-schedules/toggle", post(toggle))
        .route("/agent-schedules/delete", post(remove))
        .route(
            "/schedules/{*schedule_id}",
            delete(remove_named).patch(change_named),
        )
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod due_work_tests {
    use super::*;

    #[tokio::test]
    async fn the_cached_book_matches_what_the_store_accepted() {
        let place = crate::unkept::at_the_port::place("schedules-kept");
        let app = place.app().await;
        let desk = &app.schedules;
        let made = Made {
            agent: Some(("agent-id", "agent")),
            named: "agent",
            name: "weekly",
            cron: "0 12 * * 1",
            prompt: "weekly report",
            enabled: true,
        };
        let id = desk
            .ask(&app, |rows, now| {
                rows.create(&made, "schedule-id", now)
                    .map(|row| row.schedule_id.clone())
            })
            .await
            .unwrap()
            .unwrap();
        let read = |rows: &mut Schedules, _now| rows.get(&id).is_some();
        place.refuse_writes(Some(KIND));
        let refused = desk.ask(&app, |rows, _| rows.delete(&id)).await;
        assert!(refused.unwrap_err().0.contains("refused here"));
        assert!(desk.ask(&app, read).await.unwrap());
        assert!(Desk::default().ask(&app, read).await.unwrap());
        place.refuse_writes(None);
        assert!(desk.ask(&app, |rows, _| rows.delete(&id)).await.unwrap());
        assert!(!desk.ask(&app, read).await.unwrap());
        assert!(!Desk::default().ask(&app, read).await.unwrap());
    }
}
