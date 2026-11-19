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

pub(crate) mod events;
use std::sync::Arc;

use axum::extract::{Extension, Path, Query, RawQuery, State};
use axum::http::{HeaderMap, HeaderValue, StatusCode, header};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use clarp_actor::{Actor, ActorError, Kept, Outputs};
use clarp_kernel::artifacts::decisions::{self, Ask, Given, OnBehalf};
use clarp_kernel::artifacts::forms::{self, Sent, Submission};
use clarp_kernel::artifacts::render_spec::{self as spec, Catalog, J, Publish};
use clarp_kernel::artifacts::reports::{self, Page, Revisions};
use clarp_kernel::artifacts::updates;
use clarp_kernel::artifacts::{
    self, Artifact, ArtifactBook, Ctx, Listing, Made, Maker, New, Refused, Shelf, Why,
};
use clarp_kernel::conversation::Message;
use clarp_kernel::goals::GoalBook;
use clarp_kernel::media::MediaBook;
use clarp_kernel::push;
use clarp_kernel::python;
use clarp_kernel::turn::{Admission, Origin};
use clarp_ports::blobs::Blobs;
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};

use crate::agent::View;
use crate::edge::{ApiError, App, Principal};
use crate::fleet::{AgentRecord, Member};
use crate::ids;
use crate::pairing::Scope;
use crate::pusher::Requested;
use crate::render_spec;

/// An agent's books as anyone may read them without asking the agent.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct Books {
    pub artifacts: ArtifactBook,
    pub goals: GoalBook,
    /// The files the agent published.
    pub media: Arc<MediaBook>,
}

/// What is asked of one agent's artifacts.
#[derive(Debug, Clone, PartialEq)]
pub enum Want {
    Create {
        new: New,
        /// Another agent has an artifact with the id this one is to get.
        taken_elsewhere: bool,
        /// And has not thrown it away.
        live_elsewhere: bool,
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
        /// The sessions the owner lets answer in his place.
        designated: Vec<String>,
        /// The session of the turn that asks, where it is one; empty otherwise.
        verified: String,
        /// An agent has the session that answers in the user's place.
        known: bool,
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
    /// A user's answers to a form.
    Submit {
        artifact_id: String,
        sent: Box<Sent>,
    },
    /// A render spec published, or published again as its next revision.
    SpecPublish(Box<Publish>),
    /// A user's answers to a render spec, read as the Python Host reads them. `elsewhere`
    /// is a submission under the same id in another agent's book, with its artifact's id.
    SpecSubmit {
        artifact_id: String,
        data: Box<J>,
        elsewhere: Option<Box<(String, Submission)>>,
        catalog: Box<Catalog>,
    },
}

/// What was done: the artifact as it is shown, whether anything changed, and whether how a
/// request ended still waits to be told to its agent.
#[derive(Debug, Clone, PartialEq)]
pub struct Done {
    pub artifact: Value,
    pub changed: bool,
    pub delivery_pending: bool,
    /// What the one who sent answers to a form is answered.
    pub receipt: Option<Value>,
}

/// What carrying something out comes to, for the agent's thread: the answer, and what the
/// actor decided beside it.
pub struct Carried {
    pub said: Result<Done, Refused>,
    pub outs: Vec<Outputs>,
}

fn kept(artifacts: &[Artifact]) -> Kept<'_> {
    Kept {
        artifacts,
        ..Kept::default()
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
            ..Message::default()
        };
        let told = [told];
        outs.push(actor.keep_with(&kept(&told), &message, admission, now)?.1);
    }
    // What a user answered to a form is left for the form's agent the way a user leaves a
    // message for an agent that is busy: it waits its turn in the queue, where its user can
    // still take it back, and starts at once when the agent is free.
    for (artifact_id, submission_id) in actor.artifacts().untold() {
        let told = actor.artifacts().told(&artifact_id, &submission_id, now);
        let Some((form, prompt, spoken)) = told else {
            continue;
        };
        let message = Message {
            id: format!("u-html-form-{submission_id}"),
            speak: spoken,
            text: prompt,
            origin: Origin::User,
            trace_id: Some(ids::hex(8)),
            sender: None,
            client: None,
            ..Message::default()
        };
        let told = [form];
        let said = actor.keep_with(&kept(&told), &message, Admission::Later, now)?;
        outs.push(said.1);
    }
    Ok(outs)
}

/// A page as it is kept, whole. On a thread that may wait for the disk.
fn kept_page(pages: &dyn Blobs, page: &Page) -> Option<String> {
    let most = usize::try_from(page.bytes).ok()?;
    let bytes = pages.read(&page.blob(), 0, most).ok()?;
    (bytes.len() == most)
        .then(|| String::from_utf8(bytes).ok())
        .flatten()
}

/// Carries out what is asked of an agent's artifacts. On the agent's own thread. `pages` is
/// where the pages of reports are kept.
pub fn carry_out(
    actor: &mut Actor,
    record: &AgentRecord,
    want: &Want,
    now: i64,
    pages: &dyn Blobs,
) -> Result<Carried, ActorError> {
    // What ran out is closed before anything is answered about it.
    let mut outs = settle(actor, now)?;
    let maker = Maker {
        agent_id: &record.agent_id,
        session: &record.session,
        persona: &record.persona,
    };
    let mut receipt = None;
    let decided = {
        let (book, published) = (actor.artifacts(), actor.media());
        let page = |page: &Page| kept_page(pages, page);
        let made =
            |made: Result<Made, Refused>| made.map(|made| (made.artifact, made.changed, made.page));
        let changed = |changed: Result<(Artifact, bool), Refused>| {
            changed.map(|(artifact, changed)| (artifact, changed, None))
        };
        match want {
            Want::Create {
                new,
                taken_elsewhere,
                live_elsewhere,
            } => {
                let mut ctx = Ctx {
                    now_ms: now,
                    token_hex: &mut ids::hex,
                    taken: &|id| *taken_elsewhere || book.any(id).is_some(),
                    published,
                    elsewhere: &|_| *live_elsewhere,
                    page: &page,
                };
                made(book.create(maker, new, &mut ctx))
            }
            Want::Request(ask) => {
                let mut ctx = Ctx {
                    now_ms: now,
                    token_hex: &mut ids::hex,
                    taken: &|id| book.any(id).is_some(),
                    published,
                    elsewhere: &|_| false,
                    page: &page,
                };
                changed(
                    book.ask(maker, ask, &mut ctx)
                        .map(|artifact| (artifact, true)),
                )
            }
            Want::Update { artifact_id, data } => {
                let mut ctx = Ctx {
                    now_ms: now,
                    token_hex: &mut ids::hex,
                    taken: &|id| book.any(id).is_some(),
                    published,
                    elsewhere: &|_| false,
                    page: &page,
                };
                made(book.update(artifact_id, data, &mut ctx))
            }
            Want::Resolve {
                decision_id,
                expected_revision,
                choice,
                answer,
                on_behalf_session,
                designated,
                verified,
                known,
            } => {
                let given = Given {
                    expected_revision: *expected_revision,
                    choice: choice.as_ref(),
                    answer: answer.as_ref(),
                };
                let by = OnBehalf {
                    session: on_behalf_session.as_deref(),
                    designated,
                    verified,
                };
                changed(book.resolve(decision_id, &given, &by, &|_| *known, now))
            }
            Want::Dismiss {
                decision_id,
                expected_revision,
            } => changed(book.dismiss(decision_id, *expected_revision, now)),
            Want::Withdraw {
                decision_id,
                session,
            } => changed(book.withdraw(decision_id, session, now)),
            Want::Archive {
                artifact_id,
                archived,
                expected_updated_at,
            } => changed(book.archive(artifact_id, *archived, *expected_updated_at, now)),
            Want::Discard {
                artifact_id,
                expected_updated_at,
            } => changed(book.discard(artifact_id, *expected_updated_at, now)),
            Want::Pin {
                artifact_id,
                pinned,
            } => changed(book.pin(artifact_id, *pinned, now)),
            // Answers that came before under their id are answered as they were: two that
            // were sent at once come here one after the other.
            Want::Submit { artifact_id, sent } => match book.submission(&sent.submission_id) {
                Some((form, before)) => before.again(&form.artifact_id, sent).map(|said| {
                    receipt = Some(said);
                    (form.clone(), false, None)
                }),
                None => book.submitted(artifact_id, sent, now).map(|(form, said)| {
                    receipt = Some(said);
                    (form, true, None)
                }),
            },
            Want::SpecPublish(asked) => {
                let mut ctx = Ctx {
                    now_ms: now,
                    token_hex: &mut ids::hex,
                    taken: &|id| book.any(id).is_some(),
                    published,
                    elsewhere: &|_| false,
                    page: &page,
                };
                // What is only checked is checked where it is asked, and never comes here.
                book.spec_published(maker, asked, &mut ctx)
                    .and_then(|made| {
                        made.ok_or_else(|| spec::Refusal::new("not_open", "").refused())
                    })
                    .map(|made| (made, true, None))
            }
            Want::SpecSubmit {
                artifact_id,
                data,
                elsewhere,
                catalog,
            } => {
                let elsewhere = elsewhere
                    .as_deref()
                    .map(|(id, before)| (id.as_str(), before));
                book.spec_submitted(catalog, artifact_id, data, elsewhere, now)
                    .map(|(form, said, changed)| {
                        receipt = Some(said);
                        (form, changed, None)
                    })
            }
        }
    };
    let (artifact, changed, page) = match decided {
        Ok(decided) => decided,
        Err(refused) => {
            return Ok(Carried {
                said: Err(refused),
                outs,
            });
        }
    };
    // A report's page is kept before the report is written: what the log says of a report
    // never names a page that is not there.
    if let (Some(content), Some(named)) = (&page, &artifact.page) {
        pages
            .put(&named.blob(), content.as_bytes())
            .map_err(|error| ActorError::Io(format!("a report's page was not kept: {error}")))?;
    }
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
            receipt,
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
        Why::Spec(_) => return render_spec::said(why),
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

/// A report's page as it is kept, read on a thread that may wait for the disk.
async fn page_of(app: &App, page: &Page) -> Option<String> {
    let (blobs, page) = (app.blobs.clone(), page.clone());
    let read = tokio::task::spawn_blocking(move || kept_page(blobs.as_ref(), &page)).await;
    read.ok().flatten()
}

/// A row as it is answered: as a representation asks for it, with its report's page put
/// back, or named where a list leaves it out.
pub(crate) async fn shown(app: &App, mut row: Value, representation: &str, capable: bool) -> Value {
    // A render spec's row is the same in every representation: plain, or with what was
    // left out for a client that can see the spec.
    if row["type"] == spec::KIND
        && capable
        && let Some(artifact) = spec_of(app, row["artifact_id"].as_str().unwrap_or(""))
    {
        spec::marked(&mut row, &artifact);
    }
    let page = reports::named_page(&mut row);
    let mut row = represented(row, representation);
    match page {
        Some(page) if representation == "summary-v1" => reports::without_page(&mut row, &page),
        Some(page) => {
            let content = page_of(app, &page).await;
            if content.is_none() {
                eprintln!("clarp-host: the page of a report is not where it is kept");
            }
            reports::with_page(&mut row, content.as_deref().unwrap_or(""));
        }
        None => {}
    }
    row
}

/// Rows as they are answered, in their order.
async fn all_shown(app: &App, rows: Vec<Value>, representation: &str, capable: bool) -> Vec<Value> {
    let mut answered = Vec::with_capacity(rows.len());
    for row in rows {
        answered.push(shown(app, row, representation, capable).await);
    }
    answered
}

/// A render spec as its agent keeps it, also one that was thrown away.
fn spec_of(app: &App, artifact_id: &str) -> Option<Artifact> {
    let fleet = Fleet::of(app);
    let member = fleet.holder(artifact_id, true)?;
    let view = member.agent.view();
    let found = view.books.artifacts.any(artifact_id);
    found
        .filter(|artifact| artifact.kind == spec::KIND)
        .cloned()
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
    headers: HeaderMap,
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
    let rows = {
        let fleet = Fleet::of(&app);
        artifacts::list(&fleet.shelves(), &listing, app.clock.now_ms())
    };
    let capable = render_spec::capable(&app, &headers);
    let rows = all_shown(&app, rows, representation, capable).await;
    Ok(Json(json!({ "artifacts": rows })))
}

async fn pinned_artifacts(
    State(app): State<App>,
    Query(query): Params,
    headers: HeaderMap,
) -> Result<Json<Value>, ApiError> {
    let representation = representation(&query)?;
    let rows = {
        let fleet = Fleet::of(&app);
        artifacts::list_pinned(&fleet.shelves(), 100, app.clock.now_ms())
    };
    let capable = render_spec::capable(&app, &headers);
    let rows = all_shown(&app, rows, representation, capable).await;
    Ok(Json(json!({ "artifacts": rows })))
}

/// `GET /artifacts/<id>`, and what is read of a report beside it: `/html`, its page as a
/// browser is given it, and `/revisions`, what it was.
async fn get_artifact(
    State(app): State<App>,
    Path(rest): Path<String>,
    Query(query): Params,
    Extension(principal): Extension<Principal>,
    headers: HeaderMap,
) -> Result<Response, ApiError> {
    let rest = rest.trim_matches('/');
    if let Some(id) = rest.strip_suffix("/events-config") {
        events::full(&principal)?;
        return Ok(Json(events::configure(&app, id, None).await?).into_response());
    }
    if let Some(id) = rest.strip_suffix("/events") {
        events::full(&principal)?;
        let integer = |key: &str| {
            query
                .get(key)
                .filter(|v| !v.is_empty())
                .map(|s| {
                    python::int(s).ok_or_else(|| {
                        refused(
                            StatusCode::BAD_REQUEST,
                            format!("invalid literal for int() with base 10: '{s}'"),
                        )
                    })
                })
                .transpose()
        };
        let page = events::page(
            &app,
            id,
            integer("after")?.unwrap_or(0),
            integer("limit")?.unwrap_or(100),
            integer("through")?,
        )
        .await?;
        return match query.get("format").map(String::as_str).unwrap_or("json") {
            "" | "json" => Ok(Json(page).into_response()),
            "jsonl" => Ok((
                [(header::CONTENT_TYPE, "application/x-ndjson")],
                events::jsonl(&page),
            )
                .into_response()),
            _ => Err(refused(StatusCode::BAD_REQUEST, "unsupported event format")),
        };
    }
    if let Some(artifact_id) = rest.strip_suffix("/html") {
        return page_for_a_browser(&app, artifact_id.trim_matches('/'), &principal, &headers).await;
    }
    if let Some(artifact_id) = rest.strip_suffix("/revisions") {
        return revisions(&app, artifact_id.trim_matches('/'), &query).await;
    }
    let Some(row) = find(&app, rest) else {
        return Err(refused(StatusCode::NOT_FOUND, "artifact not found"));
    };
    // A render spec is read whole only by a client that can see it.
    if row["type"] == spec::KIND {
        let opened = match spec_of(&app, rest) {
            Some(artifact) if render_spec::capable(&app, &headers) => {
                render_spec::opened(row, &artifact)?
            }
            _ => row,
        };
        return Ok(Json(json!({ "artifact": opened })).into_response());
    }
    // Only this one representation is known here; any other is the artifact as it is.
    let flat = query
        .get("representation")
        .is_some_and(|given| given == "flat-v1");
    let row = shown(&app, row, if flat { "flat-v1" } else { "" }, false).await;
    Ok(Json(json!({ "artifact": row })).into_response())
}

/// `GET /artifacts/<id>/html`: a report's page as a browser is given it, behind the policy
/// that keeps it from reaching anything the artifact does not name. The page's own scripts
/// can read the address it was fetched by, so a token in the address is refused; and a
/// device that may only read and send does not get pages that run.
async fn page_for_a_browser(
    app: &App,
    artifact_id: &str,
    principal: &Principal,
    headers: &HeaderMap,
) -> Result<Response, ApiError> {
    let limited = matches!(
        principal,
        Principal::Device {
            scope: Scope::Limited,
            ..
        }
    );
    if limited {
        return Err(refused(
            StatusCode::FORBIDDEN,
            "full device access required",
        ));
    }
    let said = |name| headers.get(name).and_then(|value| value.to_str().ok());
    let in_a_header = said(header::AUTHORIZATION).is_some_and(|value| value.starts_with("Bearer "))
        || said(header::COOKIE).is_some_and(|cookies| cookies.contains("claude_pwa_token="));
    if !in_a_header && !app.config.auth_token.is_empty() {
        return Err(refused(
            StatusCode::FORBIDDEN,
            "send the credential in a header or cookie, not the URL",
        ));
    }
    let artifact = {
        let fleet = Fleet::of(app);
        let held = fleet.holder(artifact_id, false);
        held.and_then(|member| {
            member
                .agent
                .view()
                .books
                .artifacts
                .get(artifact_id)
                .cloned()
        })
    };
    let Some((artifact, page)) = artifact
        .filter(|artifact| artifact.kind == "html_form")
        .and_then(|artifact| artifact.page.clone().map(|page| (artifact, page)))
    else {
        return Err(refused(StatusCode::NOT_FOUND, "HTML artifact not found"));
    };
    let content = page_of(app, &page).await.unwrap_or_default();
    let (body, policy) = reports::rendered(&artifact.payload, &content);
    let mut response = (
        [
            (header::CONTENT_TYPE, "text/html; charset=utf-8"),
            (header::X_CONTENT_TYPE_OPTIONS, "nosniff"),
            (header::REFERRER_POLICY, "no-referrer"),
        ],
        body,
    )
        .into_response();
    if let Ok(policy) = HeaderValue::from_str(&policy) {
        response
            .headers_mut()
            .insert(header::CONTENT_SECURITY_POLICY, policy);
    }
    Ok(response)
}

/// `GET /artifacts/<id>/revisions`: what a report was, version by version, or one version
/// of it with its page.
async fn revisions(
    app: &App,
    artifact_id: &str,
    query: &std::collections::HashMap<String, String>,
) -> Result<Response, ApiError> {
    let not_found = |why: String| refused(StatusCode::NOT_FOUND, why);
    let number = |key: &str, otherwise: i64| match query.get(key) {
        None => Ok(otherwise),
        Some(given) => given
            .trim()
            .parse::<i64>()
            .map_err(|_| not_found(format!("invalid literal for int() with base 10: '{given}'"))),
    };
    let (limit, offset) = (number("limit", 50)?, number("offset", 0)?);
    let version = query.get("version").map(String::as_str);
    let answer = report_history(app, artifact_id, version, limit, offset).await?;
    Ok(Json(answer).into_response())
}

/// What a report was, as `GET /artifacts/<id>/revisions` answers it: its versions, or one
/// of them with its page.
pub(crate) async fn report_history(
    app: &App,
    artifact_id: &str,
    version: Option<&str>,
    limit: i64,
    offset: i64,
) -> Result<Value, ApiError> {
    let not_found = |why: String| refused(StatusCode::NOT_FOUND, why);
    let asked = {
        let fleet = Fleet::of(app);
        let held = fleet.holder(artifact_id, false);
        held.map(|member| {
            let view = member.agent.view();
            view.books
                .artifacts
                .revisions(artifact_id, version, limit, offset)
        })
    };
    let asked = asked.unwrap_or_else(|| {
        Err(Refused {
            why: Why::Value,
            message: "read-only report not found".to_string(),
        })
    });
    match asked.map_err(|why| not_found(why.message))? {
        Revisions::Listed(listed) => Ok(json!({ "revisions": listed })),
        Revisions::One(mut one) => {
            // The page of that version, back in its payload.
            if let Some(page) = one.get_mut("payload").and_then(reports::named_page) {
                let content = page_of(app, &page).await.unwrap_or_default();
                one["payload"]["content"] = json!(content);
            }
            Ok(json!({ "revision": one }))
        }
    }
}

async fn create_artifact(
    State(app): State<App>,
    RawQuery(query): RawQuery,
    headers: HeaderMap,
    body: axum::body::Bytes,
) -> Result<Response, ApiError> {
    let validate_only = render_spec::validate_only(query.as_deref());
    let capable = render_spec::capable(&app, &headers);
    let (status, answer) = posted(&app, &body, validate_only, capable).await?;
    Ok((status, Json(answer)).into_response())
}

/// What `POST /artifacts` answers for this body: the status and the body of the answer.
pub(crate) async fn posted(
    app: &App,
    body: &[u8],
    validate_only: bool,
    capable: bool,
) -> Result<(StatusCode, Value), ApiError> {
    // A render spec is read as the Python Host reads it: the rules see what that reader
    // sees (a key twice, a number's spelling).
    if let Some(J::Object(data)) = render_spec::body(body)
        && render_spec::is_spec(&data)
    {
        return render_spec::publish(app, &data, validate_only, capable).await;
    }
    let artifact = made(app, &object(body)?, validate_only, capable).await?;
    Ok((StatusCode::CREATED, json!({ "artifact": artifact })))
}

/// Makes an artifact from what a request says. Answers the artifact as it is shown.
pub(crate) async fn create(app: &App, data: &Map<String, Value>) -> Result<Value, ApiError> {
    made(app, data, false, false).await
}

/// Makes an artifact, or only checks a render spec when `validate_only` says so; the row as
/// this client is shown it.
async fn made(
    app: &App,
    data: &Map<String, Value>,
    validate_only: bool,
    capable: bool,
) -> Result<Value, ApiError> {
    if let J::Object(fields) = J::of(&Value::Object(data.clone()))
        && render_spec::is_spec(&fields)
    {
        let (_, answer) = render_spec::publish(app, &fields, validate_only, capable).await?;
        return Ok(answer.get("artifact").cloned().unwrap_or(answer));
    }
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
        expected_version: data.get("expected_version").cloned(),
        validate_only,
    };
    let fleet = Fleet::of(app);
    let session = said(data, "session", "");
    let Some(member) = app.member(session.trim()) else {
        return Err(conflict(artifacts::create_of_nobody(&new)));
    };
    let id = new.artifact_id.trim();
    let elsewhere = |has: &dyn Fn(&ArtifactBook) -> bool| {
        !id.is_empty()
            && fleet.0.iter().any(|(other, view)| {
                other.record.agent_id != member.record.agent_id && has(&view.books.artifacts)
            })
    };
    let (taken_elsewhere, live_elsewhere) = (
        elsewhere(&|book| book.any(id).is_some()),
        elsewhere(&|book| book.get(id).is_some()),
    );
    let want = Want::Create {
        new,
        taken_elsewhere,
        live_elsewhere,
    };
    let made = ask_agent(&member, want).await?.artifact;
    Ok(shown(app, made, "", capable).await)
}

/// `POST /artifacts/<id>` and what a user does with an artifact: `/archive`, `/discard`,
/// `/pin`.
async fn post_artifact(
    State(app): State<App>,
    Path(rest): Path<String>,
    Extension(principal): Extension<Principal>,
    headers: HeaderMap,
    body: axum::body::Bytes,
) -> Result<Json<Value>, ApiError> {
    let path = rest.trim_matches('/');
    if let Some(id) = path.strip_suffix("/events") {
        events::full(&principal)?;
        let data = clarp_kernel::artifacts::form_events::posted(&body)
            .map_err(|why| refused(StatusCode::CONFLICT, why))?;
        return Ok(Json(events::append(&app, id, &data).await?));
    }
    if let Some(id) = path.strip_suffix("/events-config") {
        events::full(&principal)?;
        return Ok(Json(
            events::configure(
                &app,
                id,
                Some(&serde_json::from_slice(&body).unwrap_or(Value::Null)),
            )
            .await?,
        ));
    }
    let capable = render_spec::capable(&app, &headers);
    if let Some(artifact_id) = rest.trim_matches('/').strip_suffix("/submit") {
        return Ok(Json(
            submit(&app, artifact_id.trim_matches('/'), &body, capable).await?,
        ));
    }
    // A render spec is revised as a whole, from the body as the Python Host reads it.
    let id = rest.trim_matches('/');
    if let Some(current) = spec_of(&app, id).filter(|spec| spec.deleted_at.is_none()) {
        let Some(J::Object(data)) = render_spec::body(&body) else {
            return Err(refused(StatusCode::BAD_REQUEST, "json object required"));
        };
        return Ok(Json(
            render_spec::update(&app, &current, &data, capable).await?,
        ));
    }
    Ok(Json(
        changed_as(&app, &rest, object(&body)?, capable).await?,
    ))
}

/// `POST /artifacts/<id>/submit`: a user sends answers to a form. Answered with a receipt;
/// the same answers under the same id are answered with the same.
async fn submit(
    app: &App,
    artifact_id: &str,
    body: &[u8],
    capable: bool,
) -> Result<Value, ApiError> {
    // Answers to a render spec are read as the Python Host reads them.
    let spec = {
        let fleet = Fleet::of(app);
        fleet.holder(artifact_id, true).cloned().and_then(|member| {
            let found = member
                .agent
                .view()
                .books
                .artifacts
                .any(artifact_id)
                .cloned();
            found
                .filter(|form| form.kind == spec::KIND)
                .map(|form| (member, form))
        })
    };
    if let Some((member, form)) = spec {
        let Some(data @ J::Object(_)) = render_spec::body(body) else {
            return Err(refused(StatusCode::BAD_REQUEST, "JSON object required"));
        };
        return render_spec::submit(app, &member, &form, &data, capable).await;
    }
    let Ok(Value::Object(data)) = serde_json::from_slice(body) else {
        return Err(refused(StatusCode::BAD_REQUEST, "JSON object required"));
    };
    // What a report answers is a request that was wrong; every other refusal is one that
    // cannot be now.
    let told = |why: Refused| match why.message.as_str() {
        forms::READ_ONLY => refused(StatusCode::BAD_REQUEST, why.message),
        _ => refused(StatusCode::CONFLICT, why.message),
    };
    let sent = forms::sent(artifact_id, &data).map_err(told)?;
    let fleet = Fleet::of(app);
    // An id is used once on the whole Host: it is looked for before the form is.
    let before = fleet.0.iter().find_map(|(_, view)| {
        let (form, before) = view.books.artifacts.submission(&sent.submission_id)?;
        Some(before.again(&form.artifact_id, &sent))
    });
    if let Some(answered) = before {
        return answered.map_err(told);
    }
    let Some(member) = fleet.holder(artifact_id, false) else {
        return Err(refused(StatusCode::CONFLICT, "HTML form not found"));
    };
    let want = Want::Submit {
        artifact_id: artifact_id.to_string(),
        sent: Box::new(sent),
    };
    let done = member.agent.artifact(want).await?.map_err(told)?;
    done.receipt
        .ok_or_else(|| refused(StatusCode::INTERNAL_SERVER_ERROR, "no receipt"))
}

/// Changes an artifact, or does with it what a user does. `rest` is what follows
/// `/artifacts/`: the artifact's id, and `/archive`, `/discard` or `/pin` when it is one of
/// those. Answers the body of the answer.
pub(crate) async fn change(
    app: &App,
    rest: &str,
    data: Map<String, Value>,
) -> Result<Value, ApiError> {
    changed_as(app, rest, data, false).await
}

/// The same, with the row as this client is shown it.
async fn changed_as(
    app: &App,
    rest: &str,
    data: Map<String, Value>,
    capable: bool,
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
    let artifact = shown(app, done.artifact, "", capable).await;
    Ok(if action == "update" {
        json!({ "artifact": artifact })
    } else {
        json!({ "artifact": artifact, "changed": done.changed })
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
    // An agent the user muted pages nobody, also when it asks. The request waits in the
    // inbox like any other.
    let directory = app.fleet.directory();
    let asker = artifact["agent_id"].as_str().unwrap_or("");
    let muted = directory
        .members
        .iter()
        .any(|member| member.record.agent_id == asker && member.record.muted);
    if muted {
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
    // A header that is no text is a token that names nobody.
    let turn = headers
        .get(crate::turn_identity::HEADER)
        .map(|token| token.to_str().unwrap_or_default());
    Ok(Json(decide(&app, &rest, turn, &object(&body)?).await?))
}

/// Ends a request: the user answers or discards it, the agent takes it back. `rest` is what
/// follows `/decisions/`: the request's id and `/resolve`, `/dismiss` or `/withdraw`.
/// `turn`: the token the caller presents as an agent's turn (`X-Clarp-Turn`, or what the call
/// of a tool carries), where it presents one. Answers the body of the answer.
pub(crate) async fn decide(
    app: &App,
    rest: &str,
    turn: Option<&str>,
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
            // Whose turn asks, where one says it is a turn: a token that names nobody is
            // refused, and an agent never answers as the user.
            let caller = crate::turn_identity::presented(app, turn).await?;
            if caller.is_some() && on_behalf_session.is_none() {
                let why = "agent resolution requires on_behalf_session";
                return Err(refused(StatusCode::FORBIDDEN, why));
            }
            // In the user's place: those the owner names, the session of the turn that asks, and
            // whether an agent has the session that answers (every agent counts, the janitors
            // and those put away too, as on the Python Host).
            let (designated, verified, known) = match &on_behalf_session {
                None => (Vec::new(), String::new(), false),
                Some(acting) => {
                    let owners = crate::owner::OwnerFile::new(app.config.speech_keys.clone());
                    let designated = owners.read().as_ref().map(crate::owner::on_behalf_sessions);
                    let verified = caller.as_ref().map(|member| member.record.session.clone());
                    let directory = app.fleet.directory();
                    let mut everyone = directory.members.iter().chain(directory.janitors.iter());
                    let known = everyone.any(|member| member.record.session == acting.trim());
                    (
                        designated.unwrap_or_default(),
                        verified.unwrap_or_default(),
                        known,
                    )
                }
            };
            Want::Resolve {
                expected_revision: decisions::integer(&expected, "expected_revision", 1)
                    .map_err(conflict)?,
                decision_id: decision_id.clone(),
                choice,
                answer: answer.cloned(),
                on_behalf_session,
                designated,
                verified,
                known,
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

/// `GET /attention/inbox?limit=&cursor=&representation=`: a page of Updates, what agents
/// made that their user may want to look at. A cursor of a list that has changed since is
/// answered 409: the client begins again.
async fn updates_page(
    State(app): State<App>,
    Query(query): Params,
    headers: HeaderMap,
) -> Result<Json<Value>, ApiError> {
    let wrong = || refused(StatusCode::BAD_REQUEST, "invalid attention query");
    let limit = query
        .get("limit")
        .map_or(Ok(100), |given| given.trim().parse::<i64>());
    let limit = limit.map_err(|_| wrong())?;
    let cursor = query.get("cursor").map_or("", String::as_str);
    let representation = representation(&query).map_err(|_| wrong())?;
    let page = {
        let fleet = Fleet::of(&app);
        updates::page(&fleet.shelves(), limit, cursor, app.clock.now_ms())
    };
    let page = page.map_err(|no| match no.message.as_str() {
        updates::STALE => refused(StatusCode::CONFLICT, no.message),
        _ => wrong(),
    })?;
    Ok(Json(json!({
        "artifacts": all_shown(&app, page.artifacts, representation, render_spec::capable(&app, &headers)).await,
        "next_cursor": page.next_cursor,
        "snapshot_revision": page.snapshot_revision,
        "total": page.total,
        "index_version": updates::INDEX_VERSION,
    })))
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

/// What the apps' badge counts, as the agent each thing is of: every request that waits for
/// the user, and everything an agent made that blocks on the user and is not put away.
pub(crate) fn badge(app: &App) -> Vec<String> {
    let fleet = Fleet::of(app);
    let (shelves, now) = (fleet.shelves(), app.clock.now_ms());
    let agent_of = |row: &Value| row["agent_id"].as_str().unwrap_or("").to_string();
    let waiting = decisions::attention(&shelves, true, false, now);
    let mut items: Vec<String> = waiting.iter().map(agent_of).collect();
    let mut cursor = String::new();
    // As many pages as a list of Updates can sensibly have: a list that changes under the
    // count is counted again the next time.
    for _ in 0..50 {
        let Ok(page) = updates::page(&shelves, 200, &cursor, now) else {
            break;
        };
        let blocking = page.artifacts.iter().filter(|row| {
            row["attention_bucket"] == "blocking"
                && row["archived_at"].is_null()
                && row["type"] != "workflow_run"
        });
        items.extend(blocking.map(agent_of));
        match page.next_cursor {
            Some(next) => cursor = next,
            None => break,
        }
    }
    items
}

/// One artifact by its id, as it is shown.
pub(crate) fn find(app: &App, artifact_id: &str) -> Option<Value> {
    let fleet = Fleet::of(app);
    artifacts::get(&fleet.shelves(), artifact_id, app.clock.now_ms())
}

/// The artifacts of one session, as `GET /artifacts?session=` answers them.
pub(crate) async fn of_session(app: &App, session: &str) -> Value {
    let listing = Listing {
        session: session.trim().to_string(),
        limit: 100,
        ..Listing::default()
    };
    let rows = {
        let fleet = Fleet::of(app);
        artifacts::list(&fleet.shelves(), &listing, app.clock.now_ms())
    };
    json!({ "artifacts": all_shown(app, rows, "", false).await })
}

/// A request to `/artifacts` or `/decisions` that is not a read, whose body holds half of a
/// UTF-16 pair in any text or key, is refused here with one sentence, before any route
/// looks at it, and nothing is kept (the Python Host's `request_texts`, R79). A render spec's
/// own requests are left to the contract's refusals, which name the field (R71).
pub(crate) async fn texts(
    State(app): State<App>,
    request: axum::extract::Request,
    next: axum::middleware::Next,
) -> Response {
    if request.method() == axum::http::Method::GET {
        return next.run(request).await;
    }
    let path = request.uri().path().to_string();
    let (parts, body) = request.into_parts();
    let Ok(bytes) = axum::body::to_bytes(body, 2 * 1024 * 1024).await else {
        return refused(StatusCode::PAYLOAD_TOO_LARGE, "length limit exceeded").into_response();
    };
    let data = render_spec::body(&bytes);
    let lone =
        matches!(&data, Some(J::Object(_))) && data.as_ref().is_some_and(spec::json::holds_lone);
    if lone && !spec_request(&app, &path, data.as_ref()) {
        eprintln!(
            "clarp-host: requestTextRefused {}",
            path.split('/').nth(1).unwrap_or_default()
        );
        return refused(StatusCode::BAD_REQUEST, spec::UNSTORABLE).into_response();
    }
    let request = axum::extract::Request::from_parts(parts, axum::body::Body::from(bytes));
    next.run(request).await
}

/// Whether a request is a render spec's own: one made (`type` render_spec), or one to a
/// render spec that is there.
fn spec_request(app: &App, path: &str, data: Option<&J>) -> bool {
    if path == "/artifacts" {
        return data.and_then(J::object).is_some_and(render_spec::is_spec);
    }
    let Some(rest) = path.strip_prefix("/artifacts/") else {
        return false;
    };
    let rest = percent_decoded(rest);
    let id = rest.trim_matches('/').split('/').next().unwrap_or_default();
    spec_of(app, id).is_some_and(|spec| spec.deleted_at.is_none())
}

/// A part of a path with its `%XX` read, as Python's `unquote` reads it.
fn percent_decoded(part: &str) -> String {
    let bytes = part.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut at = 0;
    while at < bytes.len() {
        let hex = bytes
            .get(at + 1..at + 3)
            .and_then(|two| std::str::from_utf8(two).ok())
            .and_then(|two| u8::from_str_radix(two, 16).ok());
        match (bytes[at], hex) {
            (b'%', Some(byte)) => {
                out.push(byte);
                at += 3;
            }
            (byte, _) => {
                out.push(byte);
                at += 1;
            }
        }
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// The routes of the library and the inbox.
pub fn routes() -> Router<App> {
    Router::new()
        .route("/artifacts", get(list_artifacts).post(create_artifact))
        .route("/artifacts/{*rest}", get(get_artifact).post(post_artifact))
        .route("/pinned-artifacts", get(pinned_artifacts))
        .route("/artifact-catalogs/clarp.form/1", get(render_spec::catalog))
        .route("/decisions", post(create_decision))
        .route("/decisions/{*rest}", post(post_decision))
        .route("/attention", get(attention))
        .route("/attention/inbox", get(updates_page))
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    /// How deep a body of an ordinary route (`POST /decisions`, `/decisions/<id>/...`,
    /// `/artifacts/<id>` for a kind other than a render spec) may nest, measured through its
    /// reader: the outermost container is the first level. serde_json stops by its own limit,
    /// below the 512 levels of the render spec's reader (`docs/differences.md`).
    #[test]
    fn an_ordinary_body_is_read_to_serde_jsons_depth_and_refused_below_it() {
        // `{"a":0}`, `{"a":[0]}`, `{"a":[[0]]}`: the object and the lists inside it.
        let nested = |levels: usize| {
            let inner = levels - 1;
            format!("{{\"a\":{}0{}}}", "[".repeat(inner), "]".repeat(inner))
        };
        let deepest = (1..=600)
            .take_while(|levels| object(nested(*levels).as_bytes()).is_ok())
            .last();
        assert_eq!(deepest, Some(127));
        let refused = object(nested(128).as_bytes()).unwrap_err();
        assert_eq!(
            (refused.status(), refused.message()),
            (StatusCode::BAD_REQUEST, "json object required")
        );
    }

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
