//! The router of spoken messages at the Host's edge.
//!
//! A message that is dictated hands-free and is not said to be for the agent it names can
//! be given to a model that says whom it is for. The rules are the kernel's
//! ([`clarp_kernel::router`]): what a model is shown, and a message's way from the moment
//! it comes in to what is kept of it. Here is what only a Host can do on that way: read
//! what the agents were last told and said, ask the model, give a message to an agent, say
//! a word aloud, open, stop or make an agent, and keep the router's book.
//!
//! The router is a janitor's job (`message-delegator`): it is on when that janitor is, set
//! as that janitor is set, and each message is routed in a run of that janitor. The Host
//! installs the janitor paused, and so the router is off until its owner turns it on. The
//! routes an app sets it by are the Python Host's (`server/lib/routes/send.py`), in its
//! shapes and words.
//!
//! **Whom the model is.** A tool of the Host (Codex or Claude Code), asked one question
//! outside every conversation ([`crate::once`]), with the question on standard input. The
//! Python Host can also ask the OpenAI API and a typed decision model; this Host has
//! neither built in, and says so in words when it is set to one.
//!
//! **How much a model is shown.** Asked a second time, with every agent in view, the
//! Python Host shows what every agent was last told and said. On a Host with two hundred
//! agents that is half a megabyte of question. Here every agent is in view, and the last
//! words are shown of the twenty that were last active, and always of the agent the
//! message was said to and of the one that is open.

mod phrases;

use std::collections::BTreeMap;
use std::collections::HashMap;
use std::time::{Duration, Instant};

use axum::body::Bytes;
use axum::extract::{Query, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use axum::{Json, Router};
use clarp_actor::conversation::Which;
use clarp_backend::Question;
use clarp_kernel::conversation::Entry;
use clarp_kernel::janitors::Refused;
use clarp_kernel::janitors::book::Asked;
use clarp_kernel::python;
use clarp_kernel::router::flow::{
    self, Begun, Book, Done, Effect, Give, Next, Outcome, Routing, Say, Start,
};
use clarp_kernel::router::packet::{Came, InView, Member, Said, State as Doing, in_view};
use clarp_kernel::router::{self as rules, JOB, POLICY};
use clarp_protocol::requests::SendRequest;
use clarp_protocol::responses::SendResponse;
use serde_json::{Map, Value, json};

use crate::agent::Unavailable;
use crate::edge::{self, ApiError, App};
use crate::fleet::{AgentRecord, Member as Fellow};
use crate::{ids, logins, once, wire};

/// The kind of thing the router's book is, among what is left with the fleet.
const KIND: &str = "router";
const ALL: &str = "all";
/// How much of an agent's conversation is read for what it was last told and said.
const READ_BACK: usize = 60;
/// The setting an app keeps of how a message that names nobody finds its agent, and what
/// it can be: the apps apply it; the Host keeps it.
const ADDRESSING: &str = "orchestrator.addressing_mode";
const MODES: [&str; 4] = ["off", "first_name", "most_names", "ai"];
/// The tools of the Host a router can be set to, as an app names them.
const TOOLS: [(&str, &str); 7] = [
    ("claude", "Claude"),
    ("codex", "Codex"),
    ("agy", "Antigravity"),
    ("grok", "Grok"),
    ("opencode", "OpenCode"),
    ("deepseek", "DeepSeek"),
    ("muse", "Muse Code"),
];

/// The router of this Host: its book. `None` until it was read where it is kept.
#[derive(Default)]
pub struct Desk {
    pub(crate) phrases: phrases::Desk,
    book: tokio::sync::Mutex<Option<Book>>,
}

fn unread() -> Unavailable {
    Unavailable("the router's book could not be read".to_string())
}

impl Desk {
    /// The book, read where it is kept the first time it is asked for.
    async fn held(
        &self,
        app: &App,
    ) -> Result<tokio::sync::MutexGuard<'_, Option<Book>>, Unavailable> {
        let mut held = self.book.lock().await;
        if held.is_none() {
            let kept = app.fleet.kept(KIND, ALL.to_string()).await?;
            // What cannot be read is a book with nothing in it.
            let book = kept.and_then(|kept| serde_json::from_slice(&kept).ok());
            *held = Some(book.unwrap_or_default());
        }
        Ok(held)
    }

    /// One reading of the book.
    async fn read<T>(&self, app: &App, read: impl FnOnce(&Book) -> T) -> Result<T, Unavailable> {
        let held = self.held(app).await?;
        held.as_ref().map(read).ok_or_else(unread)
    }

    /// One step of a message's way: the router's book and the janitors' book, in which the
    /// runs of the router are, changed together and kept. Nobody else reads or changes
    /// either meanwhile. The router's book is always taken first.
    async fn step<T>(
        &self,
        app: &App,
        agents: &[Member],
        step: impl FnOnce(&mut Book, &mut flow::Around<'_>) -> T,
    ) -> Result<T, Unavailable> {
        let mut held = self.held(app).await?;
        let book = held.as_mut().ok_or_else(unread)?;
        let before = book.clone();
        let focus = app.fleet.directory().focus.clone();
        let janitors = app.janitors.clone();
        let stepped = janitors.with(app, |janitors, world, now_ms| {
            let mut around = flow::Around {
                agents,
                focus: focus.as_deref(),
                janitors,
                world,
                now_ms,
            };
            step(book, &mut around)
        });
        let done = stepped.await?;
        if *book != before {
            let kept = serde_json::to_vec(book).unwrap_or_default();
            app.fleet.keep(KIND, ALL.to_string(), Some(kept)).await?;
        }
        Ok(done)
    }
}

// ---- what the router reads of the agents -------------------------------------------------------

/// What an agent was last told and said, as its conversation has it: what a person or
/// another agent wrote to it and what it answered. What the Host sends by itself on its
/// own schedule says nothing of whom a spoken message is for, and is left out.
async fn said_of(member: &Fellow) -> Vec<Said> {
    let Ok(page) = member.agent.tail(READ_BACK, Which::WithoutAutomated).await else {
        return Vec::new();
    };
    let mut said = Vec::new();
    for version in &page.entries {
        let (role, text) = match &version.entry {
            Entry::Message(message) => ("user", message.text.as_str()),
            Entry::Reply(reply) => ("assistant", reply.text.as_str()),
            Entry::Imported(imported) => {
                let text = imported.row["text"].as_str().unwrap_or_default();
                match imported.row["role"].as_str() {
                    Some("user") => ("user", text),
                    Some("assistant") => ("assistant", text),
                    _ => continue,
                }
            }
            _ => continue,
        };
        if !python::strip(text).is_empty() {
            said.push(Said {
                role: role.to_string(),
                text: text.to_string(),
                trace_id: String::new(),
                source: "transcript".to_string(),
                created_at: version.ts_ms,
            });
        }
    }
    said
}

/// Every agent of the Host as the router sees it: the roster in its order, then the
/// janitors, which are looked at and not spoken to. Of those a packet shows in detail, and of
/// no other, the last words are read (`packet::in_view`): the agent the message was said to
/// and the one that is open, and with everyone in view (`whole`) the most recently active.
/// `only`: the agents of a call; `asked_for`: an app asks, that could place the message by
/// no name.
async fn members(
    app: &App,
    given: &BTreeMap<String, Vec<Said>>,
    requested: &str,
    only: Option<&[String]>,
    asked_for: bool,
    whole: bool,
) -> Vec<Member> {
    let directory = app.fleet.directory();
    let open = directory.focus.as_deref();
    let everyone = directory.members.iter().chain(directory.janitors.iter());
    let mut all: Vec<(Member, &Fellow)> = Vec::new();
    for fellow in everyone {
        let record = &fellow.record;
        let view = fellow.agent.view();
        let since = wire::state_since(record, &view.status);
        let doing = Doing {
            kind: wire::state_name(&wire::state_kind(&view.status)),
            detail: view.status.label.clone(),
            ts: since,
        };
        let member = Member {
            agent_id: record.agent_id.clone(),
            session: record.session.clone(),
            persona: record.persona.clone(),
            voice_id: record.voice_id.clone(),
            cwd: record.cwd.clone(),
            backend: record.backend.clone(),
            archived: record.archived_at_ms.is_some(),
            voice_target: !record.is_janitor,
            created_at: record.created_at_ms,
            last_activity: since,
            state: Some(doing),
            said: Vec::new(),
        };
        all.push((member, fellow.as_ref()));
    }
    // Whose last words are read: those the packet will show in detail, as it chooses them.
    let bare: Vec<Member> = all.iter().map(|(member, _)| member.clone()).collect();
    let view = InView {
        agents: &bare,
        requested_session: requested,
        focus: open,
        only_sessions: only,
        fallback_request: asked_for,
        scope_all: whole,
        now_ms: app.clock.now_ms(),
    };
    let chosen: Vec<String> = in_view(&view)
        .into_iter()
        .filter(|(_, detail)| *detail)
        .map(|(member, _)| member.agent_id.clone())
        .collect();
    let mut seen = Vec::with_capacity(all.len());
    for (mut member, fellow) in all {
        if chosen.contains(&member.agent_id) {
            member.said = said_of(fellow).await;
            let own = given.get(&member.agent_id);
            member.said.extend(own.into_iter().flatten().cloned());
        }
        seen.push(member);
    }
    seen
}

// ---- the model ---------------------------------------------------------------------------------

/// Asks the model a routing has to ask next: the tool of the Host its janitor is set to,
/// once, for no longer than the router is set to wait. Answers the object in what the
/// model said, or why there is none.
async fn ask(app: &App, routing: &Routing, phase: &str) -> Result<Map<String, Value>, String> {
    let settings = &routing.settings;
    let provider = rules::provider(Some(&settings.provider));
    let primary = clarp_kernel::janitors::fallbacks::Pick {
        backend: provider,
        model: settings.model.clone(),
        effort: settings.effort.clone(),
    };
    let prompt = rules::prompt(routing.packet());
    let within = Duration::from_millis(u64::try_from(settings.timeout_ms).unwrap_or(0));
    crate::model_fallbacks::execute(app, routing.run_id(), phase, primary, |pick| {
        let prompt = prompt.clone();
        async move {
            use crate::model_fallbacks::Failure;
            if !TOOLS.iter().any(|(t, _)| *t == pick.backend) {
                return Err(
                    Failure::model("tool_unavailable", not_a_tool(&pick.backend))
                        .because("RuntimeError"),
                );
            }
            let word = |s: String| (!s.is_empty()).then_some(s);
            let question = Question {
                prompt,
                model: word(pick.model),
                effort: word(pick.effort),
            };
            let _lock = crate::pool::Service::use_login(app, &pick.backend)
                .await
                .map_err(|_| Failure::model("tool_unavailable", "The login lock is unavailable"))?;
            let answer = once::ask(
                &app.config.run_dir,
                &pick.backend,
                &question,
                &app.config.default_cwd,
                within,
            )
            .await
            .map_err(Failure::routing)?;
            rules::extract_json(&answer)
                .map_err(|e| Failure::model("parse_error", e).because("ValueError"))
        }
    })
    .await
    .map(|(answer, _)| answer)
    .map_err(|e| e.detail)
}

/// What is said of a provider this Host cannot ask.
fn not_a_tool(provider: &str) -> String {
    match provider {
        "openai" | "typesafe" => format!(
            "unsupported orchestrator provider: {provider} (this Host asks a tool of its \
             own, and has no API of a provider built in)"
        ),
        other => format!("unsupported orchestrator provider: {other}"),
    }
}

// ---- what the Host does for a decision ---------------------------------------------------------

/// Gives a message to the agent the router found for it: a send like any other, said to be
/// for that agent, as the message came in. A held message goes as it came in then, under
/// its own id.
async fn give(app: &App, give: &Give) -> Done {
    let came = give.came.as_ref();
    let kept = came.and_then(|came| serde_json::from_str::<SendRequest>(&came.kept).ok());
    let mut request = kept.unwrap_or_default();
    if request.client_msg_id.is_none() {
        request.client_msg_id = came.map(|came| came.client_id.clone());
    }
    let request = SendRequest {
        session: give.session.clone(),
        text: give.text.clone(),
        trace_id: Some(give.trace_id.clone()),
        force_session: Some(true),
        orchestrator_fallback: None,
        // It was routed to the agent of its call when it came in.
        call_id: None,
        ..request
    };
    match edge::send_message(app, request).await {
        Ok(sent) => Done::Given {
            session: sent.session,
            backend: sent.dispatch,
        },
        Err(refused) => Done::NotGiven {
            status: refused.status().as_u16(),
            error: refused.message().to_string(),
        },
    }
}

/// Says something aloud: as an agent in its own voice, or as the router in the voice it is
/// set to. Where nothing on this Host can say it, it is not said; what was decided is told
/// to the apps either way.
async fn say(app: &App, say: &Say) {
    let directory = app.fleet.directory();
    let speaker = match &say.session {
        Some(session) => directory
            .by_session(session)
            .map(|member| member.record.clone()),
        // The router: nobody's session, the voice it is set to, and a name to be shown by.
        None => Some(AgentRecord {
            persona: "Clarp".to_string(),
            voice_id: say.voice_id.clone(),
            ..AgentRecord::default()
        }),
    };
    if let Some(speaker) = speaker {
        if let Some(phrase) = clarp_kernel::router::flow::phrase_key(&say.text) {
            let owners = app.mouth.owners();
            for (voice,primary) in crate::voice::phrase_voices(&owners,&speaker) {
                if !app.router.phrases.say(app, &speaker, &say.text, phrase, (voice,primary), &owners).await {
                    break;
                }
            }
            return;
        }
        app.mouth.announce(&speaker, &say.text).await;
    }
}

/// Starts an agent: again in place, as the Host knows it, or a new one by its name.
async fn start(app: &App, start: &Start) -> Done {
    let mut asked = Map::new();
    if let Some(name) = &start.name {
        asked.insert("name".to_string(), json!(name));
    }
    if let Some(session) = &start.again {
        // Everything about it is taken from what the Host knows of it: only that it is
        // started again is the model's to say.
        let directory = app.fleet.directory();
        let Some(agent) = directory.by_session(session) else {
            return Done::Broke("no such agent to replace".to_string());
        };
        asked.insert("name".to_string(), json!(agent.record.persona));
        asked.insert("replace_sid".to_string(), json!(session));
    }
    match edge::make_agent(app, &asked).await {
        Ok(made) => Done::Started {
            session: made["session"].as_str().unwrap_or_default().to_string(),
        },
        // Refused, in the Python Host's word for why (its lifecycle's `code`): the router says so
        // and keeps it as a question.
        Err((_, told)) => {
            let why = told["error"].as_str().or(told["message"].as_str());
            Done::Refused {
                code: why.unwrap_or("the agent could not be made").to_string(),
            }
        }
    }
}

/// Does what a decision asks of the Host, in its order. Answers what came of the one thing
/// that can fail in a way the router keeps: a message given, or an agent started.
async fn carry_out(app: &App, effects: &[Effect]) -> Done {
    let mut done = Done::Fine;
    for effect in effects {
        match effect {
            Effect::Give(message) => done = give(app, message).await,
            Effect::Say(words) => say(app, words).await,
            Effect::Repeat { session, or } => {
                let directory = app.fleet.directory();
                let agent = directory.by_session(session).cloned();
                let again = match agent {
                    Some(agent) => app.mouth.again(&agent).await,
                    None => false,
                };
                if let (false, Some(words)) = (again, or) {
                    say(app, words).await;
                }
            }
            Effect::Focus { session, .. } => {
                if let Ok(Some(record)) = app.fleet.select(session.clone()).await {
                    app.mouth.opened(&record.session).await;
                }
            }
            Effect::Stop { agent_id } => {
                let agent = app.fleet.directory().named(agent_id).cloned();
                if let Some(agent) = agent {
                    let _ = agent.agent.stop("user".to_string()).await;
                }
            }
            Effect::Start(agent) => done = start(app, agent).await,
        }
    }
    done
}

// ---- a message's way ---------------------------------------------------------------------------

/// What became of a message that was shown to the router.
pub(crate) enum Routed {
    /// The router does not take it: it goes on as any other message.
    NotMine,
    /// It is answered: with this status and this body.
    Answered(StatusCode, Box<SendResponse>),
}

/// What an app is answered that asked for the router where there is none: the Python
/// Host's answer for a router that is off. Nothing is sent, and the app asks its user.
fn disabled(trace_id: &str) -> Routed {
    let off = json!({ "action": "disabled", "decision_id": null, "decision": null });
    let answer = SendResponse {
        ok: true,
        trace_id: trace_id.to_string(),
        orchestrator: Some(off),
        ..SendResponse::default()
    };
    Routed::Answered(StatusCode::OK, Box::new(answer))
}

/// How a message came in, as the router keeps it: the id its sender gave it (its trace
/// where it gave none), who sent it, and the request whole, for the day it is given.
fn came_of(app: &App, request: &SendRequest, trace_id: &str) -> Came {
    let minted = request.client_msg_id.as_deref().map(python::strip);
    let minted = minted.filter(|minted| !minted.is_empty());
    let sender = edge::sender(app, request.sender.as_ref());
    let named = request.origin.as_deref().map(python::strip);
    let origin = match (&sender, named) {
        (Some(_), _) => "agent".to_string(),
        (None, Some(named)) if !named.is_empty() => named.to_lowercase(),
        (None, _) => "user".to_string(),
    };
    Came {
        client_id: minted.unwrap_or(trace_id).to_string(),
        origin,
        sender_agent_id: sender.map(|sender| sender.agent_id).unwrap_or_default(),
        kept: serde_json::to_string(request).unwrap_or_default(),
    }
}

/// Shows a message to the router: one that was dictated hands-free and is not said to be
/// for the agent it names. `requested`: the agent it was said to. `only`: the agents of
/// its call, when it is part of one.
pub(crate) async fn route(
    app: &App,
    request: &SendRequest,
    trace_id: &str,
    requested: &str,
    only: Option<&[String]>,
) -> Result<Routed, ApiError> {
    let desk = app.router.clone();
    let asked_for = request.orchestrator_fallback == Some(true);
    let not_mine = || {
        if asked_for {
            disabled(trace_id)
        } else {
            Routed::NotMine
        }
    };
    let came = came_of(app, request, trace_id);
    // Most messages meet no router: it is off, and they were not routed before. That is
    // seen before anything is read of any agent.
    let run = flow::request_id(Some(&came), trace_id);
    let janitors = app.janitors.clone();
    let looked = janitors.with(app, |book, _, _| {
        let on = |config: &clarp_kernel::janitors::book::Config| {
            config.template_id == JOB && config.enabled
        };
        book.configs.values().any(on) || matches!(book.request_run(JOB, &run), Ok(Some(_)))
    });
    if !looked.await? {
        return Ok(not_mine());
    }
    let text = python::strip(&request.text);
    let given = desk.read(app, |book| book.given.clone()).await?;
    let mut agents = members(app, &given, requested, only, asked_for, false).await;
    let message = flow::Message {
        text,
        requested_session: requested,
        trace_id,
        hands_free: request.hands_free == Some(true),
        fallback_request: asked_for,
        only_sessions: only,
        came: Some(&came),
    };
    let began = Instant::now();
    let begun = desk.step(app, &agents, |book, around| book.begin(&message, around));
    let outcome = match begun.await? {
        Begun::NotMine => return Ok(not_mine()),
        Begun::Answered(outcome) => outcome,
        Begun::Again(again) => {
            let done = give(app, &again.give).await;
            let kept = desk.step(app, &agents, |book, around| {
                book.given_again(&again, done, around)
            });
            kept.await?
        }
        Begun::Ask(mut routing) => {
            let first = ask(app, &routing, "initial").await;
            let answered = desk.step(app, &agents, |book, around| {
                book.answered(&mut routing, first, around)
            });
            let mut next = answered.await?;
            if next == Next::AskAgain {
                // Everyone is put in view: now the others' last words are read too.
                agents = members(app, &given, requested, only, asked_for, true).await;
                let widened = desk.step(app, &agents, |book, around| {
                    book.widen(&mut routing, around)
                });
                widened.await?;
                let second = ask(app, &routing, "broad").await;
                if let Err(why) = &second {
                    // The first answer stands.
                    eprintln!("clarp-host: the router's second asking failed ({trace_id}): {why}");
                }
                let answered = desk.step(app, &agents, |book, around| {
                    book.answered(&mut routing, second, around)
                });
                next = answered.await?;
            }
            match next {
                Next::Answered(outcome) => *outcome,
                Next::AskAgain | Next::Decided => {
                    let latency = i64::try_from(began.elapsed().as_millis()).unwrap_or(i64::MAX);
                    let hold_as = ids::uuid();
                    let planned = desk.step(app, &agents, |book, around| {
                        book.apply(&routing, &hold_as, around)
                    });
                    let plan = planned.await?;
                    let done = carry_out(app, &plan.effects).await;
                    let finished = desk.step(app, &agents, |book, around| {
                        book.finish(&routing, plan, done, latency, around)
                    });
                    let outcome = finished.await?;
                    // What is still to be said (a start the Host refused), before the apps
                    // are told what was decided, as the Python Host says it.
                    if let Some(words) = &outcome.say {
                        say(app, words).await;
                    }
                    outcome
                }
            }
        }
    };
    if let Some(told) = &outcome.told {
        app.hub.publish(told.clone(), app.clock.now_ms());
    }
    if outcome.action == flow::FALLBACK && !asked_for {
        // The router had nothing to say: the message goes where it was said to.
        return Ok(Routed::NotMine);
    }
    Ok(reply(outcome, trace_id))
}

/// What a message's sender is answered, in the Python Host's shape.
fn reply(outcome: Outcome, trace_id: &str) -> Routed {
    let status = StatusCode::from_u16(outcome.status).unwrap_or(StatusCode::OK);
    let decided = json!({
        "action": outcome.action, "decision_id": outcome.decision_id,
        "decision": outcome.decision,
    });
    let answer = SendResponse {
        ok: outcome.ok,
        session: outcome.session,
        dispatch: outcome.dispatch,
        trace_id: trace_id.to_string(),
        error: Some(outcome.error).filter(|error| !error.is_empty()),
        orchestrator: Some(decided),
        ..SendResponse::default()
    };
    Routed::Answered(status, Box::new(answer))
}

// ---- the routes an app sets the router by ------------------------------------------------------

fn refusal(status: StatusCode, why: &str) -> Response {
    ApiError::new(status, why).into_response()
}

fn unavailable(why: Unavailable) -> Response {
    ApiError::from(why).into_response()
}

/// The providers a router of this Host can be set to: its tools, each with whether its
/// program is there to be started.
fn providers() -> Value {
    let rows = TOOLS.iter().map(|(tool, label)| {
        let program = clarp_backend::program(tool);
        json!({
            "id": tool, "label": label,
            "detail": format!("Runs an isolated {label} request on this Host."),
            "kind": "backend", "catalog_backend": tool,
            "installed": program.is_some_and(|program| logins::installed(&program)),
            "effort_options": null,
        })
    });
    rows.collect()
}

/// How the router is set, as an app reads it. A Host whose janitor for it is gone answers
/// as one that was never set.
async fn settings_shown(app: &App) -> Result<Value, Unavailable> {
    let janitors = app.janitors.clone();
    let set = janitors.with(app, |book, world, _| flow::settings(book, world));
    Ok(set.await?.unwrap_or_default().shown())
}

/// `GET /orchestrator/settings`: how the router is set, what it can be set to, and what it
/// decided of late.
async fn settings(State(app): State<App>) -> Response {
    let shown = match settings_shown(&app).await {
        Ok(shown) => shown,
        Err(why) => return unavailable(why),
    };
    let desk = app.router.clone();
    let decided = desk.read(&app, |book| {
        (book.recent(5, None), book.recent(20, Some(rules::IGNORED)))
    });
    match decided.await {
        Ok((recent, ignored)) => Json(json!({
            "settings": shown, "providers": providers(), "recent_decisions": recent,
            "ignored_decisions": ignored,
        }))
        .into_response(),
        Err(why) => unavailable(why),
    }
}

/// `POST /orchestrator/settings`: sets the router: on or off, whom it asks, and how it
/// decides. It is the janitor that is set, as its own screen sets it.
async fn set(State(app): State<App>, body: Bytes) -> Response {
    let bad = |why: &str| refusal(StatusCode::BAD_REQUEST, why);
    let Ok(Value::Object(said)) = serde_json::from_slice::<Value>(&body) else {
        return bad("bad json");
    };
    let own = ["enabled", "provider", "model", "effort"];
    if said
        .keys()
        .any(|key| !own.contains(&key.as_str()) && !POLICY.contains(&key.as_str()))
    {
        return bad("Unsupported orchestrator settings");
    }
    let on = match said.get("enabled") {
        None => None,
        Some(Value::Bool(on)) => Some(*on),
        Some(_) => return bad("Enabled must be a boolean"),
    };
    let provider = match said.get("provider") {
        None => None,
        Some(named) => {
            let named = named.as_str().filter(|named| !named.is_empty());
            let provider = rules::provider(Some(named.unwrap_or(rules::DEFAULT_PROVIDER)));
            if !TOOLS.iter().any(|(tool, _)| *tool == provider) {
                return bad(&not_a_tool(&provider));
            }
            Some(provider)
        }
    };
    let word = |key: &str| {
        let said = said.get(key)?;
        Some(json!(python::strip(said.as_str().unwrap_or_default())))
    };
    let mut asked = Asked::new();
    for key in ["model", "effort"] {
        if let Some(said) = word(key) {
            asked.insert(key.to_string(), said);
        }
    }
    let options: Map<String, Value> = POLICY
        .iter()
        .filter_map(|key| Some(((*key).to_string(), said.get(*key)?.clone())))
        .collect();
    if !options.is_empty() {
        asked.insert("options".to_string(), Value::Object(options));
    }
    let janitors = app.janitors.clone();
    let owner = janitors.with(&app, |book, world, _| {
        let own = book.builtins.iter().find(|(_, role)| role.as_str() == JOB);
        own.and_then(|(id, _)| world.agent(id))
            .map(|agent| agent.session)
    });
    let session = match owner.await {
        Ok(Some(session)) => session,
        Ok(None) => return bad("The message delegator Janitor is unavailable"),
        Err(why) => return unavailable(why),
    };
    // Another tool for the janitor: only while it has no conversation with the one it has,
    // which a janitor that answers one request at a time never has.
    let tool_now = app
        .fleet
        .directory()
        .janitor(&session)
        .map(|member| member.record.backend.clone());
    let another = provider.is_some() && provider != tool_now;
    if another && !crate::janitors::tool_can_change(&app, &session) {
        let why = "The message delegator has a conversation with its tool: its tool is not changed";
        return refusal(StatusCode::CONFLICT, why);
    }
    let changed = app.janitors.change(&app, &session, |book, world, now_ms| {
        let gone = || Refused::new(404, "janitor_not_found", "Janitor not found");
        let own = book.builtins.iter().find(|(_, role)| role.as_str() == JOB);
        let id = own.map(|(id, _)| id.clone()).ok_or_else(gone)?;
        let kept = book.configs.get(&id).ok_or_else(gone)?;
        let was_on = kept.enabled;
        if let Some(provider) = &provider {
            let set_to = kept.execution.get("provider").and_then(Value::as_str);
            if set_to != Some(provider.as_str()) {
                // Another provider: its own model and effort, unless the request names them.
                for key in ["model", "effort"] {
                    asked.entry(key.to_string()).or_insert_with(|| json!(""));
                }
            }
            let run_by = json!({ "executor": "ephemeral", "provider": provider });
            asked.insert("execution".to_string(), run_by);
            asked.insert("backend".to_string(), json!(provider));
        }
        let mut effects = Vec::new();
        if !asked.is_empty() {
            // A janitor that is set anew is paused: it is turned on again below, when it
            // is to be on.
            let seen = json!(kept.revision);
            effects = book.configure(&session, &seen, &asked, world, now_ms)?;
        }
        let kept = book.configs.get(&id).ok_or_else(gone)?;
        let on = on.unwrap_or(was_on);
        if kept.enabled != on {
            let seen = json!(kept.revision);
            book.set_enabled(&session, &seen, on, world, now_ms)?;
        }
        Ok(effects)
    });
    match changed.await {
        Ok(Ok(_)) => {}
        Ok(Err(refused)) => {
            let status = StatusCode::from_u16(refused.status).unwrap_or(StatusCode::BAD_REQUEST);
            return refusal(status, &refused.message);
        }
        Err(why) => return unavailable(why),
    }
    match settings_shown(&app).await {
        Ok(shown) => Json(json!({ "ok": true, "settings": shown })).into_response(),
        Err(why) => unavailable(why),
    }
}

/// `GET /orchestrator/decisions?limit=`: what the router decided, the newest first.
async fn decisions(
    State(app): State<App>,
    Query(asked): Query<HashMap<String, String>>,
) -> Response {
    let limit = asked.get("limit").and_then(|limit| python::int(limit));
    let limit = limit.map_or(30, |limit| i64::try_from(limit).unwrap_or(i64::MAX));
    let desk = app.router.clone();
    match desk.read(&app, |book| book.recent(limit, None)).await {
        Ok(decided) => Json(json!({ "decisions": decided })).into_response(),
        Err(why) => unavailable(why),
    }
}

/// `POST /orchestrator/addressing` with `{mode}`: how a message that names nobody finds
/// its agent, as the apps apply it: never by another agent than the open one, by the first
/// name spoken, by the name spoken most, or by a model where no name was spoken. The Host
/// keeps what was chosen.
async fn addressing(State(app): State<App>, body: Bytes) -> Response {
    let Ok(Value::Object(said)) = serde_json::from_slice::<Value>(&body) else {
        return refusal(StatusCode::BAD_REQUEST, "bad json");
    };
    let chosen = python::strip(said.get("mode").and_then(Value::as_str).unwrap_or_default());
    if !MODES.contains(&chosen) {
        let told = json!({ "error": "unknown addressing mode", "modes": MODES });
        return (StatusCode::BAD_REQUEST, Json(told)).into_response();
    }
    let kept = vec![(ADDRESSING.to_string(), chosen.to_string())];
    match app.fleet.set(kept).await {
        Ok(()) => Json(json!({ "ok": true, "mode": chosen, "modes": MODES })).into_response(),
        Err(why) => unavailable(why),
    }
}

pub fn routes() -> Router<App> {
    Router::new()
        .route("/orchestrator/settings", get(settings).post(set))
        .route("/orchestrator/decisions", get(decisions))
        .route("/orchestrator/addressing", axum::routing::post(addressing))
}
