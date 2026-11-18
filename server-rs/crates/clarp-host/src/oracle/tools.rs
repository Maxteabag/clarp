//! The tools of the fleet, as this Host does them for the Oracle's router.
//!
//! What a tool means and what it answers are the kernel's rules
//! ([`clarp_kernel::oracle::tools`]). Here they are done with this Host's fleet: an agent's
//! state and what it said are read from the agent itself, work is handed on through the
//! Oracle's book of delegations as any delegation is, and a stop is the book's stop of an
//! agent, with other work handed on in the place of what was stopped where that is asked.

use std::sync::Arc;

use clarp_actor::conversation::Which;
use clarp_kernel::oracle::tools::{self as rules, Agent, Message, Plan, State as Doing};
use serde_json::Value;

use super::delegations;
use crate::edge::App;
use crate::fleet::{Directory, Member};
use crate::wire;

/// What came of a tool.
pub(super) struct Done {
    /// What it answered; nothing where it refused, or could not do it.
    pub receipt: Option<Value>,
    /// The agent that was told to leave what it was handed: none of that is told as a
    /// result of the call.
    pub left: Option<String>,
}

/// An agent of this Host, as the tools of the fleet know one.
fn known(member: &Member) -> Agent<'_> {
    let record = &member.record;
    Agent {
        agent_id: &record.agent_id,
        persona: &record.persona,
        session: &record.session,
        backend: &record.backend,
        personality: &record.personality,
        archived: record.archived_at_ms.is_some(),
        janitor: record.is_janitor,
    }
}

/// The agents of this Host, as the tools of the fleet know them. Its janitors are apart
/// from its roster, and no tool reaches one.
pub(super) fn agents(directory: &Directory) -> Vec<Agent<'_>> {
    let roster = directory.members.iter();
    roster.map(|member| known(member)).collect()
}

/// A message of an agent's conversation, as it was read.
struct Said {
    id: String,
    role: String,
    text: String,
    timestamp: String,
    at_ms: i64,
}

/// The newest messages of an agent's own talk, the oldest first: without what a watcher or
/// the Host's routine began.
async fn said(member: &Member, most: usize) -> Option<Vec<Said>> {
    let page = member
        .agent
        .tail(most, Which::WithoutAutomated)
        .await
        .ok()?;
    let rows = page.entries.iter().map(|entry| {
        let row = wire::row(entry);
        let of = |name: &str| row[name].as_str().unwrap_or("").to_string();
        Said {
            id: of("id"),
            role: of("role"),
            text: of("text"),
            timestamp: of("timestamp"),
            at_ms: entry.ts_ms,
        }
    });
    Some(rows.collect())
}

fn messages(rows: &[Said]) -> Vec<Message<'_>> {
    let rows = rows.iter().map(|row| Message {
        id: &row.id,
        role: &row.role,
        text: &row.text,
        timestamp: &row.timestamp,
        at_ms: row.at_ms,
    });
    rows.collect()
}

/// What a tool is to do, without the agents it was found among: so that it can be done
/// while the fleet goes on.
enum Todo {
    Answer(Value),
    Status(Arc<Member>),
    Messages(Arc<Member>),
    Transcript(Arc<Member>, usize),
    Stop(Arc<Member>, Option<(String, String)>),
    Hand(Arc<Member>, String, String),
}

/// Does what a router wants done with a tool, for the one whose call it is.
pub(super) async fn execute(
    app: &App,
    (principal, contact): (&str, &str),
    wanted: (&str, &Value, &str),
) -> Done {
    let mut left = None;
    let receipt = answered(app, (principal, contact), wanted, &mut left).await;
    Done { receipt, left }
}

/// What a tool answers; `None`: it refused, or could not do it. `left`: the agent a stop
/// reached, also where the work that was to take the place of what it left was refused.
async fn answered(
    app: &App,
    (principal, contact): (&str, &str),
    wanted: (&str, &Value, &str),
    left: &mut Option<String>,
) -> Option<Value> {
    let todo = {
        let directory = app.fleet.directory();
        let agents = agents(&directory);
        let member = |agent: &Agent| directory.named(agent.agent_id).cloned();
        match rules::plan(wanted, principal, &agents, contact).ok()? {
            Plan::Answer(answer) => Todo::Answer(answer),
            Plan::Status(agent) => Todo::Status(member(agent)?),
            Plan::Messages(agent) => Todo::Messages(member(agent)?),
            Plan::Transcript(agent, limit) => Todo::Transcript(member(agent)?, limit),
            Plan::Stop { agent, follow } => Todo::Stop(member(agent)?, follow),
            Plan::Hand {
                agent,
                operation_id,
                request,
            } => Todo::Hand(member(agent)?, operation_id, request),
        }
    };
    let now_ms = app.clock.now_ms();
    match todo {
        Todo::Answer(answer) => Some(answer),
        Todo::Status(member) => {
            let rows = said(&member, rules::STATUS_MESSAGES).await?;
            let view = member.agent.view();
            let focused = false;
            let (newest, ended) = (view.newest.as_ref(), view.ended.as_ref());
            let row = wire::summary(
                &member.record,
                &view.status,
                view.backend,
                newest,
                ended,
                focused,
            );
            let latest = Doing {
                kind: row["latest_state"].as_str().unwrap_or(""),
                detail: &row["activity"],
                at_ms: row["latest_state_ts"].as_i64().filter(|at| *at > 0),
            };
            let rows = messages(&rows);
            Some(rules::status(&known(&member), Some(latest), &rows, now_ms))
        }
        Todo::Messages(member) => {
            let rows = said(&member, 2 * rules::MESSAGES_READ).await?;
            Some(rules::messages(&known(&member), &messages(&rows), now_ms))
        }
        Todo::Transcript(member, limit) => {
            let rows = said(&member, 2 * limit).await?;
            let rows = messages(&rows);
            Some(rules::transcript(&known(&member), &rows, limit, now_ms))
        }
        Todo::Stop(member, follow) => {
            let session = member.record.session.as_str();
            delegations::called_off(app, principal, session)
                .await
                .ok()?;
            *left = Some(session.to_string());
            let Some((operation_id, request)) = follow else {
                return Some(rules::stopped());
            };
            // What is handed on in the place of what was stopped is not held by the stop.
            member.agent.resume("oracle".to_string()).await.ok()?;
            handed(app, principal, (&operation_id, session, &request)).await
        }
        Todo::Hand(member, operation_id, request) => {
            let session = member.record.session.as_str();
            handed(app, principal, (&operation_id, session, &request)).await
        }
    }
}

/// Hands an agent work under a name, as a request for a delegation does: taken once
/// however often it is asked. Says what a router is answered; `None`: it was not taken.
async fn handed(
    app: &App,
    owner: &str,
    (operation_id, session, request): (&str, &str, &str),
) -> Option<Value> {
    let work = (operation_id, session, request);
    let taken = delegations::handed(app, owner, work).await.ok()?;
    let status = taken["status"].as_str()?;
    Some(rules::handed(status, operation_id, session))
}
