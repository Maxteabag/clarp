//! What agents made for the user and what they ask the user, as the Host serves it.
//!
//! The rules are in [`clarp_kernel::artifacts`]. An artifact is in the log of the agent that
//! made it, so everything that changes one is carried out on that agent's thread
//! ([`carry_out`]); what the apps read across agents (the library, the inbox, an artifact by
//! its id) is read off the books the agents publish. The routes answer as the Python Host's
//! do (`server/lib/routes/artifacts.py`): the same statuses, the same words.
//!
//! When the user answers a request, the answer is told to the agent that asked as a
//! message, and the request's mark that it was told is written in the same append: an
//! answer is told once, also across a kill of the Host between the two.

use std::sync::Arc;

use axum::extract::{Path, Query, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use clarp_actor::{Actor, ActorError, Kept, Outputs};
use clarp_kernel::artifacts::decisions::{self, Ask, Given, OnBehalf};
use clarp_kernel::artifacts::{
    self, Artifact, ArtifactBook, Ctx, Listing, Maker, New, Refused, Shelf, Why,
};
use clarp_kernel::conversation::Message;
use clarp_kernel::goals::GoalBook;
use clarp_kernel::push;
use clarp_kernel::python;
use clarp_kernel::turn::{Admission, Origin};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};

use crate::agent::View;
use crate::edge::{ApiError, App};
use crate::fleet::{AgentRecord, Member};
use crate::ids;
use crate::pusher::Requested;

/// An agent's books as anyone may read them without asking the agent.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct Books {
    pub artifacts: ArtifactBook,
    pub goals: GoalBook,
}

/// What is asked of one agent's artifacts.
#[derive(Debug, Clone, PartialEq)]
pub enum Want {
    Create {
        new: New,
        /// Another agent has an artifact with the id this one is to get.
        taken_elsewhere: bool,
    },
    Update {
        artifact_id: String,
        data: Value,
    },
    /// A request to the user.
    Request(Box<Ask>),
    Resolve {
        decision_id: String,
        expected_revision: i64,
        choice: Option<Value>,
        answer: Option<Value>,
        on_behalf_session: Option<String>,
    },
    Dismiss {
        decision_id: String,
        expected_revision: i64,
    },
    Withdraw {
        decision_id: String,
        session: String,
    },
    Archive {
        artifact_id: String,
        archived: bool,
        expected_updated_at: i64,
    },
    Discard {
        artifact_id: String,
        expected_updated_at: i64,
    },
    Pin {
        artifact_id: String,
        pinned: bool,
    },
}

/// What was done: the artifact as it is shown, whether anything changed, and whether how a
/// request ended still waits to be told to its agent.
#[derive(Debug, Clone, PartialEq)]
pub struct Done {
    pub artifact: Value,
    pub changed: bool,
    pub delivery_pending: bool,
}

/// What carrying something out comes to, for the agent's thread: the answer, and what the
/// actor decided beside it.
pub struct Carried {
    pub said: Result<Done, Refused>,
    pub outs: Vec<Outputs>,
}

fn kept(artifacts: &[Artifact]) -> Kept<'_> {
    Kept {
        goal: None,
        artifacts,
    }
}

/// Closes the requests whose time ran out and tells every agent-bound end that still waits
/// to be told. Answers what the actor decided.
pub fn settle(actor: &mut Actor, now: i64) -> Result<Vec<Outputs>, ActorError> {
    let mut outs = Vec::new();
    let expired = actor.artifacts().expired(now);
    if !expired.is_empty() {
        outs.push(actor.keep(&kept(&expired), now)?);
    }
    for decision_id in actor.artifacts().undelivered() {
        let Some(artifact) = actor.artifacts().of_decision(&decision_id).cloned() else {
            continue;
        };
        let (Some(prompt), Some(told)) = (
            decisions::delivery_prompt(&artifact),
            actor.artifacts().delivered(&decision_id, now),
        ) else {
            continue;
        };
        // An approval that was given or refused is said to the agent like anything said to
        // it. Every other end waits its turn, and is held with what a Stop holds.
        let approval = artifact.decision.as_ref().is_some_and(|decision| {
            decision.response_type == "approval"
                && matches!(decision.status.as_str(), "accepted" | "rejected")
        });
        let admission = if approval {
            Admission::Queue
        } else {
            Admission::Background
        };
        let message = Message {
            id: format!("u-decision-{decision_id}"),
            text: prompt,
            origin: Origin::Trigger {
                name: "automation".to_string(),
            },
            trace_id: Some(ids::hex(8)),
            sender: None,
            client: None,
        };
        let told = [told];
        outs.push(actor.keep_with(&kept(&told), &message, admission, now)?.1);
    }
    Ok(outs)
}

/// Carries out what is asked of an agent's artifacts. On the agent's own thread.
pub fn carry_out(
    actor: &mut Actor,
    record: &AgentRecord,
    want: &Want,
    now: i64,
) -> Result<Carried, ActorError> {
    // What ran out is closed before anything is answered about it.
    let mut outs = settle(actor, now)?;
    let maker = Maker {
        agent_id: &record.agent_id,
        session: &record.session,
        persona: &record.persona,
    };
    let decided = {
        let book = actor.artifacts();
        let made = |made: Result<Artifact, Refused>| made.map(|artifact| (artifact, true));
        match want {
            Want::Create {
                new,
                taken_elsewhere,
            } => {
                let mut ctx = Ctx {
                    now_ms: now,
                    token_hex: &mut ids::hex,
                    taken: &|id| *taken_elsewhere || book.any(id).is_some(),
                };
                made(book.create(maker, new, &mut ctx))
            }
            Want::Request(ask) => {
                let mut ctx = Ctx {
                    now_ms: now,
                    token_hex: &mut ids::hex,
                    taken: &|id| book.any(id).is_some(),
                };
                made(book.ask(maker, ask, &mut ctx))
            }
            Want::Update { artifact_id, data } => made(book.update(artifact_id, data, now)),
            Want::Resolve {
                decision_id,
                expected_revision,
                choice,
                answer,
                on_behalf_session,
            } => {
                let given = Given {
                    expected_revision: *expected_revision,
                    choice: choice.as_ref(),
                    answer: answer.as_ref(),
                };
                // Nobody is designated to answer for the user on this Host yet.
                let by = OnBehalf {
                    session: on_behalf_session.as_deref(),
                    ..OnBehalf::default()
                };
                book.resolve(decision_id, &given, &by, &|_| false, now)
            }
            Want::Dismiss {
                decision_id,
                expected_revision,
            } => book.dismiss(decision_id, *expected_revision, now),
            Want::Withdraw {
                decision_id,
                session,
            } => book.withdraw(decision_id, session, now),
            Want::Archive {
                artifact_id,
                archived,
                expected_updated_at,
            } => book.archive(artifact_id, *archived, *expected_updated_at, now),
            Want::Discard {
                artifact_id,
                expected_updated_at,
            } => book.discard(artifact_id, *expected_updated_at, now),
            Want::Pin {
                artifact_id,
                pinned,
            } => book.pin(artifact_id, *pinned, now),
        }
    };
    let (artifact, changed) = match decided {
        Ok(decided) => decided,
        Err(refused) => {
            return Ok(Carried {
                said: Err(refused),
                outs,
            });
        }
    };
    if changed {
        outs.push(actor.keep(&kept(std::slice::from_ref(&artifact)), now)?);
    }
    // The artifact is shown as the change left it; then an answer is told to its agent.
    let shown = artifact.public(&record.persona, actor.goals(), now);
    outs.extend(settle(actor, now)?);
    let delivery_pending = actor
        .artifacts()
        .any(&artifact.artifact_id)
        .and_then(|artifact| artifact.decision.as_ref())
        .is_some_and(decisions::Decision::delivery_pending);
    Ok(Carried {
        said: Ok(Done {
            artifact: shown,
            changed,
            delivery_pending,
        }),
        outs,
    })
}

/// The agents with what they publish, read once for one request.
struct Fleet(Vec<(Arc<Member>, Arc<View>)>);

impl Fleet {
    fn of(app: &App) -> Self {
        let directory = app.fleet.directory();
        let agents = directory.members.iter();
        Self(
            agents
                .map(|member| (member.clone(), member.agent.view()))
                .collect(),
        )
    }

    fn shelves(&self) -> Vec<Shelf<'_>> {
        self.0
            .iter()
            .map(|(member, view)| Shelf {
                maker: Maker {
                    agent_id: &member.record.agent_id,
                    session: &member.record.session,
                    persona: &member.record.persona,
                },
                artifacts: &view.books.artifacts,
                goals: &view.books.goals,
            })
            .collect()
    }

    /// The agent that has the artifact with this id. `discarded` also finds one that was
    /// discarded.
    fn holder(&self, artifact_id: &str, discarded: bool) -> Option<&Arc<Member>> {
        let has = |view: &View| {
            let found = view.books.artifacts.any(artifact_id);
            found.is_some_and(|artifact| discarded || artifact.deleted_at.is_none())
        };
        self.0
            .iter()
            .find(|(_, view)| has(view))
            .map(|(member, _)| member)
    }

    /// The agent that asked the request with this id.
    fn asker(&self, decision_id: &str) -> Option<&Arc<Member>> {
        let asked = |view: &View| view.books.artifacts.of_decision(decision_id).is_some();
        self.0
            .iter()
            .find(|(_, view)| asked(view))
            .map(|(member, _)| member)
    }
}

fn refused(status: StatusCode, message: impl Into<String>) -> ApiError {
    ApiError::new(status, message)
}

/// A refusal of the rules as the routes answer it: what the caller may not do is forbidden,
/// everything else is a conflict with what is there.
fn conflict(why: Refused) -> ApiError {
    let status = match why.why {
        Why::Permission => StatusCode::FORBIDDEN,
        Why::Value | Why::Integrity => StatusCode::CONFLICT,
    };
    ApiError::new(status, why.message)
}

/// The body of a request, which has to be a JSON object.
fn object(body: &[u8]) -> Result<Map<String, Value>, ApiError> {
    match serde_json::from_slice(body) {
        Ok(Value::Object(fields)) => Ok(fields),
        _ => Err(refused(StatusCode::BAD_REQUEST, "json object required")),
    }
}

/// A field as the text it is, where what says nothing is no text.
fn said(data: &Map<String, Value>, key: &str, otherwise: &str) -> String {
    match data.get(key) {
        Some(Value::String(text)) if !text.is_empty() => text.clone(),
        Some(value) if decisions::says_yes(value) => value.to_string(),
        _ => otherwise.to_string(),
    }
}

/// A field that has to be a whole number, and is no truth value.
fn whole(data: &Map<String, Value>, key: &str) -> Result<Value, ApiError> {
    match data.get(key) {
        Some(value) if value.is_i64() || value.is_u64() => Ok(value.clone()),
        _ => Err(refused(
            StatusCode::BAD_REQUEST,
            format!("{key} must be an integer"),
        )),
    }
}

async fn ask_agent(member: &Member, want: Want) -> Result<Done, ApiError> {
    member.agent.artifact(want).await?.map_err(conflict)
}

/// The artifact as a representation asks for it: as it is, without the payload its fields
/// were taken from, or also without the bodies a list does not need.
fn represented(mut item: Value, representation: &str) -> Value {
    if representation.is_empty() {
        return item;
    }
    if let Some(fields) = item.as_object_mut() {
        fields.remove("payload");
    }
    if representation == "flat-v1" {
        return item;
    }
    // A plan's history is left out of a list: how much there is of it, when the last of it
    // was, and a digest by which an app knows the history it holds is this one.
    let goal = item
        .get_mut("plan")
        .and_then(|plan| plan.get_mut("goal"))
        .and_then(Value::as_object_mut);
    if let Some(goal) = goal
        && let Some(history) = goal.remove("history")
    {
        let entries = history.as_array().map_or(&[][..], Vec::as_slice);
        let last_at = entries.last().and_then(|entry| entry.get("at")).cloned();
        let digest = Sha256::digest(python::canonical(&history).as_bytes());
        let digest: String = digest.iter().map(|byte| format!("{byte:02x}")).collect();
        let left_out = json!({
            "reason": "summary",
            "count": entries.len(),
            "last_at": last_at,
            "digest": format!("h1:{digest}"),
        });
        goal.insert("history_omitted".to_string(), left_out);
    }
    item
}

fn representation(query: &std::collections::HashMap<String, String>) -> Result<&str, ApiError> {
    match query.get("representation").map_or("", String::as_str) {
        known @ ("" | "flat-v1" | "summary-v1") => Ok(known),
        _ => Err(refused(
            StatusCode::BAD_REQUEST,
            "unsupported artifact representation",
        )),
    }
}

type Params = Query<std::collections::HashMap<String, String>>;

async fn list_artifacts(
    State(app): State<App>,
    Query(query): Params,
) -> Result<Json<Value>, ApiError> {
    let representation = representation(&query)?;
    let text = |key: &str| query.get(key).map_or("", |value| value.trim()).to_string();
    let number = |key: &str, otherwise: Option<i64>| match query.get(key).map(String::as_str) {
        None | Some("") => Ok(otherwise),
        Some(given) => given
            .trim()
            .parse::<i64>()
            .map(Some)
            .map_err(|_| refused(StatusCode::BAD_REQUEST, "invalid limit")),
    };
    let listing = Listing {
        session: text("session"),
        agent_id: text("agent_id"),
        kind: text("type"),
        search: text("search"),
        created_from: number("created_from", None)?,
        created_to: number("created_to", None)?,
        limit: number("limit", Some(100))?.unwrap_or(100),
        offset: number("offset", Some(0))?.unwrap_or(0),
        by_creation: text("order") == "created",
    };
    let fleet = Fleet::of(&app);
    let rows = artifacts::list(&fleet.shelves(), &listing, app.clock.now_ms());
    let rows: Vec<Value> = rows
        .into_iter()
        .map(|row| represented(row, representation))
        .collect();
    Ok(Json(json!({ "artifacts": rows })))
}

async fn pinned_artifacts(
    State(app): State<App>,
    Query(query): Params,
) -> Result<Json<Value>, ApiError> {
    let representation = representation(&query)?;
    let fleet = Fleet::of(&app);
    let rows = artifacts::list_pinned(&fleet.shelves(), 100, app.clock.now_ms());
    let rows: Vec<Value> = rows
        .into_iter()
        .map(|row| represented(row, representation))
        .collect();
    Ok(Json(json!({ "artifacts": rows })))
}

async fn get_artifact(
    State(app): State<App>,
    Path(artifact_id): Path<String>,
    Query(query): Params,
) -> Result<Json<Value>, ApiError> {
    let fleet = Fleet::of(&app);
    let found = artifacts::get(
        &fleet.shelves(),
        artifact_id.trim_matches('/'),
        app.clock.now_ms(),
    );
    let Some(row) = found else {
        return Err(refused(StatusCode::NOT_FOUND, "artifact not found"));
    };
    // Only this one representation is known here; any other is the artifact as it is.
    let flat = query
        .get("representation")
        .is_some_and(|given| given == "flat-v1");
    let row = if flat {
        represented(row, "flat-v1")
    } else {
        row
    };
    Ok(Json(json!({ "artifact": row })))
}

async fn create_artifact(
    State(app): State<App>,
    body: axum::body::Bytes,
) -> Result<Response, ApiError> {
    let artifact = create(&app, &object(&body)?).await?;
    Ok((StatusCode::CREATED, Json(json!({ "artifact": artifact }))).into_response())
}

/// Makes an artifact from what a request says. Answers the artifact as it is shown.
pub(crate) async fn create(app: &App, data: &Map<String, Value>) -> Result<Value, ApiError> {
    let kind = said(data, "type", "");
    if matches!(
        kind.trim().to_lowercase().as_str(),
        "decision" | "question" | "plan"
    ) {
        return Err(refused(StatusCode::CONFLICT, "reserved artifact type"));
    }
    let new = New {
        kind,
        title: said(data, "title", ""),
        summary: said(data, "summary", ""),
        status: said(data, "status", "ready"),
        reference_id: said(data, "reference_id", ""),
        payload: data.get("payload").cloned(),
        artifact_id: said(data, "artifact_id", ""),
    };
    let fleet = Fleet::of(app);
    let session = said(data, "session", "");
    let Some(member) = app.member(session.trim()) else {
        return Err(conflict(artifacts::create_of_nobody(&new)));
    };
    let id = new.artifact_id.trim();
    let taken_elsewhere = !id.is_empty()
        && fleet.0.iter().any(|(other, view)| {
            other.record.agent_id != member.record.agent_id
                && view.books.artifacts.any(id).is_some()
        });
    let want = Want::Create {
        new,
        taken_elsewhere,
    };
    Ok(ask_agent(&member, want).await?.artifact)
}

/// `POST /artifacts/<id>` and what a user does with an artifact: `/archive`, `/discard`,
/// `/pin`.
async fn post_artifact(
    State(app): State<App>,
    Path(rest): Path<String>,
    body: axum::body::Bytes,
) -> Result<Json<Value>, ApiError> {
    Ok(Json(change(&app, &rest, object(&body)?).await?))
}

/// Changes an artifact, or does with it what a user does. `rest` is what follows
/// `/artifacts/`: the artifact's id, and `/archive`, `/discard` or `/pin` when it is one of
/// those. Answers the body of the answer.
pub(crate) async fn change(
    app: &App,
    rest: &str,
    data: Map<String, Value>,
) -> Result<Value, ApiError> {
    let rest = rest.trim_matches('/');
    let (artifact_id, action) = ["archive", "discard", "pin"]
        .iter()
        .find_map(|action| Some((rest.strip_suffix(&format!("/{action}"))?, *action)))
        .unwrap_or((rest, "update"));
    let artifact_id = artifact_id.trim_matches('/').to_string();
    let fleet = Fleet::of(app);
    let missing = || {
        conflict(Refused {
            why: Why::Value,
            message: "artifact not found".to_string(),
        })
    };
    let want = match action {
        "update" => Want::Update {
            artifact_id: artifact_id.clone(),
            data: Value::Object(data),
        },
        "pin" => {
            let Some(pinned) = data.get("pinned").and_then(Value::as_bool) else {
                return Err(refused(StatusCode::BAD_REQUEST, "pinned must be a boolean"));
            };
            Want::Pin {
                artifact_id: artifact_id.clone(),
                pinned,
            }
        }
        _ => {
            let expected = whole(&data, "expected_updated_at")?;
            let archived = data.get("archived").and_then(Value::as_bool);
            if action == "archive" && archived.is_none() {
                return Err(refused(
                    StatusCode::BAD_REQUEST,
                    "archived must be a boolean",
                ));
            }
            let expected_updated_at =
                decisions::integer(&expected, "expected_updated_at", 0).map_err(conflict)?;
            match archived.filter(|_| action == "archive") {
                Some(archived) => Want::Archive {
                    artifact_id: artifact_id.clone(),
                    archived,
                    expected_updated_at,
                },
                None => Want::Discard {
                    artifact_id: artifact_id.clone(),
                    expected_updated_at,
                },
            }
        }
    };
    let Some(member) = fleet.holder(&artifact_id, action == "discard") else {
        // A pin of an artifact that is not there is the one refusal that says not found.
        return Err(if action == "pin" {
            refused(StatusCode::NOT_FOUND, "artifact not found")
        } else {
            missing()
        });
    };
    let done = ask_agent(member, want).await?;
    Ok(if action == "update" {
        json!({ "artifact": done.artifact })
    } else {
        json!({ "artifact": done.artifact, "changed": done.changed })
    })
}

async fn create_decision(
    State(app): State<App>,
    body: axum::body::Bytes,
) -> Result<Response, ApiError> {
    let artifact = request(&app, &object(&body)?).await?;
    Ok((StatusCode::CREATED, Json(json!({ "artifact": artifact }))).into_response())
}

/// Asks the user something for an agent, from what a request says. Answers the artifact
/// the request is, as it is shown.
pub(crate) async fn request(app: &App, data: &Map<String, Value>) -> Result<Value, ApiError> {
    let given = |key: &str, otherwise: Value| data.get(key).cloned().unwrap_or(otherwise);
    let ask = Ask {
        title: said(data, "title", "Decision"),
        question: json!(said(data, "question", "")),
        context: json!(said(data, "context", "")),
        yes_label: json!(said(data, "yes_label", "Yes")),
        no_label: json!(said(data, "no_label", "No")),
        payload: data.get("payload").cloned(),
        reference_id: said(data, "reference_id", ""),
        expires_at: data.get("expires_at").cloned(),
        response_type: given("response_type", json!("approval")),
        options: data.get("options").cloned(),
        allow_custom_text: data.get("allow_custom_text").cloned(),
        recommended_option_id: data.get("recommended_option_id").cloned(),
        blocks_progress: given("blocks_progress", json!(false)),
        priority_reason: given("priority_reason", json!("")),
        urgency: given("urgency", json!("normal")),
        response_effort: given("response_effort", json!("review")),
        deadline_at: data.get("deadline_at").cloned(),
        input_hint: data.get("input_hint").cloned(),
    };
    let session = said(data, "session", "");
    let Some(member) = app.member(session.trim()) else {
        return Err(conflict(decisions::ask_of_nobody(&ask, app.clock.now_ms())));
    };
    let asked = ask_agent(&member, Want::Request(Box::new(ask)))
        .await?
        .artifact;
    push_request(app, &asked);
    Ok(asked)
}

/// Tells the phones of a request that was just made. A request is the one thing an agent
/// cannot finish without its user, and an agent that asks in the middle of a turn goes on
/// working: without this the request would sit unseen until the user opened that chat.
fn push_request(app: &App, artifact: &Value) {
    let decision = &artifact["decision"];
    let text = |value: &Value| value.as_str().unwrap_or("").to_string();
    let decision_id = text(&decision["decision_id"]);
    if decision_id.is_empty() || decision["status"].as_str().unwrap_or("pending") != "pending" {
        return;
    }
    let or = |said: String, otherwise: String| if said.is_empty() { otherwise } else { said };
    app.pusher.requested(Requested {
        session: text(&artifact["session"]),
        persona: or(text(&artifact["agent_name"]), "Clarp".to_string()),
        title: text(&artifact["title"]),
        question: or(text(&decision["question"]), text(&artifact["summary"])),
        decision_id,
        artifact_id: text(&artifact["artifact_id"]),
        time_sensitive: push::needs_interruption(
            decision["blocks_progress"] == true,
            decision["urgency"].as_str().unwrap_or(""),
        ),
        text_input: decision["response_type"] == "text_input",
        revision: decision["revision"].as_i64().unwrap_or(1),
        expires_at: decision["expires_at"].as_i64(),
    });
}

/// `POST /decisions/<id>/resolve`, `/dismiss` and `/withdraw`.
async fn post_decision(
    State(app): State<App>,
    Path(rest): Path<String>,
    headers: HeaderMap,
    body: axum::body::Bytes,
) -> Result<Json<Value>, ApiError> {
    let as_turn = headers.contains_key("x-clarp-turn");
    Ok(Json(decide(&app, &rest, as_turn, &object(&body)?).await?))
}

/// Ends a request: the user answers or discards it, the agent takes it back. `rest` is what
/// follows `/decisions/`: the request's id and `/resolve`, `/dismiss` or `/withdraw`.
/// `as_turn` says the caller claims to be an agent's turn. Answers the body of the answer.
pub(crate) async fn decide(
    app: &App,
    rest: &str,
    as_turn: bool,
    data: &Map<String, Value>,
) -> Result<Value, ApiError> {
    let rest = rest.trim_matches('/');
    let found = ["resolve", "dismiss", "withdraw"]
        .iter()
        .find_map(|action| Some((rest.strip_suffix(&format!("/{action}"))?, *action)));
    let Some((decision_id, action)) = found else {
        return Err(ApiError::plain(StatusCode::NOT_FOUND, "not found"));
    };
    let decision_id = decision_id.trim_matches('/').to_string();
    let want = match action {
        "resolve" => {
            let on_behalf = data
                .get("on_behalf_session")
                .filter(|value| !value.is_null());
            let on_behalf_session = match on_behalf.map(Value::as_str) {
                None => None,
                Some(Some(session)) if !session.trim().is_empty() => Some(session.to_string()),
                Some(_) => {
                    let why = "on_behalf_session must be a nonempty session";
                    return Err(refused(StatusCode::BAD_REQUEST, why));
                }
            };
            let choice = data.get("choice").filter(|value| !value.is_null());
            let answer = data.get("answer").filter(|value| !value.is_null());
            if choice.is_some() == answer.is_some() {
                let why = "exactly one of choice or answer is required";
                return Err(refused(StatusCode::BAD_REQUEST, why));
            }
            let choice = match choice.map(|choice| choice.as_str().map(|c| c.trim().to_lowercase()))
            {
                None => None,
                Some(Some(choice)) if matches!(choice.as_str(), "accepted" | "rejected") => {
                    Some(json!(choice))
                }
                Some(_) => return Err(refused(StatusCode::BAD_REQUEST, "invalid decision choice")),
            };
            let expected = whole(data, "expected_revision")?;
            // An agent's turn cannot be told from its request on this Host yet: one that
            // says it is a turn is not taken for the user.
            if as_turn {
                let why = "verified turn identity required";
                return Err(refused(StatusCode::FORBIDDEN, why));
            }
            Want::Resolve {
                expected_revision: decisions::integer(&expected, "expected_revision", 1)
                    .map_err(conflict)?,
                decision_id: decision_id.clone(),
                choice,
                answer: answer.cloned(),
                on_behalf_session,
            }
        }
        "dismiss" => {
            let expected = whole(data, "expected_revision")?;
            Want::Dismiss {
                expected_revision: decisions::integer(&expected, "expected_revision", 1)
                    .map_err(conflict)?,
                decision_id: decision_id.clone(),
            }
        }
        _ => {
            let session = data.get("session").and_then(Value::as_str);
            let Some(session) = session.filter(|session| !session.trim().is_empty()) else {
                return Err(refused(StatusCode::BAD_REQUEST, "session required"));
            };
            Want::Withdraw {
                decision_id: decision_id.clone(),
                session: session.to_string(),
            }
        }
    };
    let fleet = Fleet::of(app);
    let Some(member) = fleet.asker(&decision_id) else {
        return Err(refused(StatusCode::CONFLICT, "decision not found"));
    };
    let done = ask_agent(member, want).await?;
    Ok(if action == "withdraw" {
        json!({ "artifact": done.artifact, "changed": done.changed })
    } else {
        json!({
            "artifact": done.artifact,
            "changed": done.changed,
            "delivery_pending": done.delivery_pending,
        })
    })
}

async fn attention(State(app): State<App>, Query(query): Params) -> Json<Value> {
    let is = |key: &str, value: &str| query.get(key).is_some_and(|given| given == value);
    Json(inbox(
        &app,
        is("decision_format", "2"),
        is("include_archived", "1"),
    ))
}

/// What waits for the user, as `GET /attention` answers it.
pub(crate) fn inbox(app: &App, include_questions: bool, include_archived: bool) -> Value {
    let fleet = Fleet::of(app);
    let now = app.clock.now_ms();
    let items = decisions::attention(&fleet.shelves(), include_questions, include_archived, now);
    json!({
        "items": items,
        "count": items.len(),
        "decision_format": 2,
        "janitor_items": [],
    })
}

/// One artifact by its id, as it is shown.
pub(crate) fn find(app: &App, artifact_id: &str) -> Option<Value> {
    let fleet = Fleet::of(app);
    artifacts::get(&fleet.shelves(), artifact_id, app.clock.now_ms())
}

/// The artifacts of one session, as `GET /artifacts?session=` answers them.
pub(crate) fn of_session(app: &App, session: &str) -> Value {
    let listing = Listing {
        session: session.trim().to_string(),
        limit: 100,
        ..Listing::default()
    };
    let fleet = Fleet::of(app);
    let rows = artifacts::list(&fleet.shelves(), &listing, app.clock.now_ms());
    json!({ "artifacts": rows })
}

/// The routes of the library and the inbox.
pub fn routes() -> Router<App> {
    Router::new()
        .route("/artifacts", get(list_artifacts).post(create_artifact))
        .route("/artifacts/{*rest}", get(get_artifact).post(post_artifact))
        .route("/pinned-artifacts", get(pinned_artifacts))
        .route("/decisions", post(create_decision))
        .route("/decisions/{*rest}", post(post_decision))
        .route("/attention", get(attention))
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    #[test]
    fn a_list_leaves_a_plans_history_out_and_says_what_it_left_out() {
        let row = json!({
            "artifact_id": "a1", "type": "plan", "payload": { "plan_id": "p" },
            "plan": { "plan_id": "p", "goal": { "outcome": "x", "history": [
                { "at": 5, "kind": "created" }, { "at": 9, "kind": "step" },
            ] } },
        });
        assert_eq!(represented(row.clone(), ""), row);
        let flat = represented(row.clone(), "flat-v1");
        assert!(flat.get("payload").is_none());
        assert_eq!(flat["plan"]["goal"]["history"].as_array().unwrap().len(), 2);
        let summary = represented(row, "summary-v1");
        let goal = &summary["plan"]["goal"];
        assert!(goal.get("history").is_none());
        let left_out = &goal["history_omitted"];
        assert_eq!(
            (
                &left_out["reason"],
                &left_out["count"],
                &left_out["last_at"]
            ),
            (&json!("summary"), &json!(2), &json!(9))
        );
        let digest = left_out["digest"].as_str().unwrap();
        assert!(
            digest.starts_with("h1:") && digest.len() == 3 + 64,
            "{digest}"
        );
        // An artifact that is no plan has nothing to leave out.
        let plain = represented(json!({ "artifact_id": "a2", "payload": {} }), "summary-v1");
        assert_eq!(plain, json!({ "artifact_id": "a2" }));
    }

    #[test]
    fn a_field_is_read_as_the_text_it_is() {
        let data = object(br#"{"title": "T", "count": 3, "none": null, "empty": ""}"#).unwrap();
        assert_eq!(said(&data, "title", "x"), "T");
        assert_eq!(said(&data, "count", "x"), "3");
        assert_eq!(said(&data, "none", "x"), "x");
        assert_eq!(said(&data, "empty", "x"), "x");
        assert_eq!(said(&data, "absent", "x"), "x");
        assert!(object(b"[1]").is_err());
        assert!(whole(&data, "count").is_ok());
        assert!(whole(&data, "title").is_err());
    }
}
