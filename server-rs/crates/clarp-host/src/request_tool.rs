//! `clarp-admin request` and `clarp-admin reply`: a request between agents that comes back.
//!
//! The words and the names are in [`clarp_kernel::requests`]. Here is what the two commands
//! do, which is what the Python tool does (`bin/clarp-admin.py`, `cmd_request` and
//! `cmd_reply`, over `server/lib/peer_requests.py`): the same checks in the same order, the
//! same refusals, the same exit codes. A request is a message; the asker's goal waits for
//! its result as a dependency; a reply is that dependency's result, or a message.

use std::sync::Arc;

use clarp_actor::conversation::Which;
use clarp_kernel::conversation::Entry;
use clarp_kernel::requests::{
    self, DEFAULT_DEADLINE_S, KINDS, MAX_DEADLINE_S, MAX_UPDATES, NOT_DELIVERED, Who,
};
use clarp_protocol::requests::SendRequest;
use serde_json::{Value, json};

use crate::artifact_tool::parse;
use crate::edge::{self, App};
use crate::fleet::Member;
use crate::goaltool::{self, Ask};
use crate::ids;
use crate::tools::ToolAnswer;

/// How far back a conversation is read to count the updates a request has brought.
const LOOK_BACK: usize = 2_000;

/// What argparse says of a command line it cannot read, and exits 2 with.
fn usage(command: &str, why: &str) -> ToolAnswer {
    ToolAnswer::said(2, format!("clarp-admin {command}: error: {why}"))
}

fn who(member: &Member) -> Who<'_> {
    Who {
        persona: &member.record.persona,
        session: &member.record.session,
    }
}

/// The agent with this session name, or why there is none, as the Python tool says it.
/// An agent that is put away neither asks nor is asked.
fn agent(app: &App, session: &str, role: &str) -> Result<Arc<Member>, String> {
    let member = app.member(session).ok_or_else(|| {
        let named = if session.is_empty() {
            "(empty)"
        } else {
            session
        };
        format!("no such {role} session: {named}")
    })?;
    if member.record.archived_at_ms.is_some() {
        return Err(format!("{} ({session}) is archived", member.record.persona));
    }
    Ok(member)
}

/// A plan as anyone may read it, with the agent it belongs to.
fn plan_of(app: &App, plan_id: &str) -> Option<(Arc<Member>, Value)> {
    let owner = goaltool::owner_of(plan_id);
    let directory = app.fleet.directory();
    let member = directory
        .members
        .iter()
        .find(|member| member.record.agent_id == owner)?
        .clone();
    let plan = member
        .agent
        .view()
        .books
        .goals
        .get(plan_id, app.clock.now_ms())?;
    Some((member, plan))
}

/// Does something to a goal as its owner would, and says what the rules said when they
/// refused.
async fn act(
    member: &Member,
    plan: &Value,
    action: &str,
    data: Value,
    caller: crate::agent::GoalCaller,
) -> Result<(), String> {
    let ask = Ask::Act {
        plan_id: plan["plan_id"].as_str().unwrap_or("").to_string(),
        revision: plan["revision"].as_i64().unwrap_or(0),
        action: action.to_string(),
        data,
    };
    match member.agent.goal_as(ask, caller).await {
        Ok(said) if said.exit == 0 => Ok(()),
        Ok(said) => Err(goaltool::refusal(&said).to_string()),
        Err(unavailable) => Err(unavailable.0),
    }
}

fn request_owner(turn: Option<&str>, owner: &Member) -> crate::agent::GoalCaller {
    match turn {
        Some(id) if id == owner.record.agent_id => {
            crate::agent::GoalCaller::Verified(id.to_owned())
        }
        _ => crate::agent::GoalCaller::Default,
    }
}

/// Makes the asker's own goal wait for the request's result. Refused when the goal is not
/// the asker's, not being worked with wakes, or already waits for something else: another
/// dependency is never replaced.
async fn wait_on(
    app: &App,
    requester: &Member,
    plan_id: &str,
    request_id: &str,
    recipient: &Member,
    deadline_s: i64,
    caller: crate::agent::GoalCaller,
) -> Result<(), String> {
    let Some((owner, plan)) = plan_of(app, plan_id) else {
        return Err(format!("no such goal: {plan_id}"));
    };
    let text = |value: &Value| value.as_str().unwrap_or("").to_string();
    if owner.record.agent_id != requester.record.agent_id {
        return Err(format!(
            "goal {plan_id} belongs to {}, not {}",
            text(&plan["session"]),
            requester.record.session
        ));
    }
    let goal = &plan["goal"];
    let enrolled = plan["recovery_enabled"].as_i64().unwrap_or(0) != 0;
    if !goal.is_object() || plan["status"] != "active" || !enrolled {
        return Err(format!(
            "goal {plan_id} is {} and {} for recovery",
            text(&plan["status"]),
            if enrolled { "enrolled" } else { "not enrolled" }
        ));
    }
    let wake = &goal["continuation"];
    let state = text(&wake["state"]);
    let key = requests::dependency_key(request_id);
    if matches!(state.as_str(), "attention" | "blocked" | "not_enrolled") {
        return Err(format!(
            "goal recovery is {state}: {}",
            text(&wake["reason"])
        ));
    }
    if state == "waiting" && wake["dependency_key"] != key.as_str() {
        let on = [&wake["dependency_key"], &wake["job_handle"]]
            .into_iter()
            .map(text)
            .find(|named| !named.is_empty())
            .unwrap_or_else(|| "another dependency".to_string());
        return Err(format!("goal already waits on {on}"));
    }
    let point = &goal["checkpoint"];
    let persona = &recipient.record.persona;
    let read = format!(
        "When woken by request {request_id} to {persona}: read the dependency result \
         (clarp-goal get {plan_id}); it is their answer, or the deadline."
    );
    // What the goal said it would do next stays; an older note of this kind goes.
    let mut next: Vec<String> = text(&point["next_work"])
        .split("\n\n")
        .filter(|part| !part.is_empty() && !part.starts_with("When woken by request "))
        .map(str::to_string)
        .collect();
    next.push(read);
    let progress = match text(&point["progress"]) {
        said if said.is_empty() => format!("Asked {persona} (request {request_id})"),
        said => said,
    };
    let data = json!({
        "progress": progress,
        "next_work": next.join("\n\n"),
        "continuation": {
            "kind": "dependency", "key": key,
            "reason": format!("Waiting for {persona} to answer request {request_id}"),
            "due_at": app.clock.now_ms() + deadline_s * 1_000,
        },
    });
    act(&owner, &plan, "checkpoint", data, caller)
        .await
        .map_err(|why| format!("goal {plan_id} changed while arming the wait ({why}); try again"))
}

/// The Host refused the request: the wait is settled truthfully, not left to its deadline.
/// Answers whether a goal was told.
async fn abandon_wait(
    app: &App,
    plan_id: &str,
    request_id: &str,
    why: &str,
    turn: Option<&str>,
) -> bool {
    let key = requests::dependency_key(request_id);
    for _ in 0..3 {
        let Some((owner, plan)) = plan_of(app, plan_id) else {
            return false;
        };
        let wake = &plan["goal"]["continuation"];
        if wake["state"] != "waiting" || wake["dependency_key"] != key.as_str() {
            return false;
        }
        let evidence = requests::evidence(&format!("request {request_id} {NOT_DELIVERED}: {why}"));
        let data = json!({ "key": key, "outcome": "failed", "evidence": evidence });
        if act(
            &owner,
            &plan,
            "dependency",
            data,
            request_owner(turn, &owner),
        )
        .await
        .is_ok()
        {
            return true;
        }
    }
    false
}

/// `clarp-admin request --to SESSION --from ME --text TEXT [--goal PLAN] [--deadline S]`.
pub async fn request(app: &App, args: &[String], token: Option<&str>) -> ToolAnswer {
    let valued = ["--to", "--from", "--text", "--goal", "--deadline"];
    let parsed = match parse(args, &valued, &[], &[]) {
        Ok(parsed) => parsed,
        Err(why) => return usage("request", &why),
    };
    let missing: Vec<&str> = ["--to", "--from", "--text"]
        .into_iter()
        .filter(|name| parsed.option(name).is_none())
        .collect();
    if !missing.is_empty() {
        let why = format!(
            "the following arguments are required: {}",
            missing.join(", ")
        );
        return usage("request", &why);
    }
    let deadline = match parsed.option("--deadline").map(str::parse::<i64>) {
        None => None,
        Some(Ok(seconds)) => Some(seconds),
        Some(Err(_)) => {
            let given = parsed.option("--deadline").unwrap_or_default();
            return usage(
                "request",
                &format!("argument --deadline: invalid int value: '{given}'"),
            );
        }
    };
    let goal = parsed.option("--goal").filter(|goal| !goal.is_empty());
    let (to, from, text) = (
        parsed.option("--to").unwrap_or_default(),
        parsed.option("--from").unwrap_or_default(),
        parsed.option("--text").unwrap_or_default(),
    );
    let request_id = requests::request_id(&ids::hex(6));
    // Everything that can refuse is asked before anything is sent.
    let prepared = async {
        if deadline.is_some() && goal.is_none() {
            return Err("--deadline needs --goal: without a goal nothing waits".to_string());
        }
        let deadline = deadline.unwrap_or(DEFAULT_DEADLINE_S);
        if !(1..=MAX_DEADLINE_S).contains(&deadline) {
            return Err(format!("deadline must be 1 to {MAX_DEADLINE_S} seconds"));
        }
        let requester = agent(app, from, "requester")?;
        let recipient = agent(app, to, "recipient")?;
        let mut turn = None;
        if let Some(plan_id) = goal {
            turn = crate::turn_identity::presented(app, token)
                .await
                .map_err(|e| e.message().to_owned())?
                .map(|who| who.record.agent_id.clone());
            wait_on(
                app,
                &requester,
                plan_id,
                &request_id,
                &recipient,
                deadline,
                request_owner(turn.as_deref(), &requester),
            )
            .await?;
        }
        Ok((requester, recipient, turn))
    };
    let (requester, recipient, turn) = match prepared.await {
        Ok(found) => found,
        Err(why) => {
            return ToolAnswer::said(4, format!("clarp-admin request: {why}; nothing was sent"));
        }
    };
    let message = SendRequest {
        session: recipient.record.session.clone(),
        text: requests::request_text(&request_id, &who(&requester), text),
        client_msg_id: Some(requests::request_client_id(&request_id)),
        synthesize_audio: Some(false),
        hands_free: Some(false),
        origin: Some("agent".to_string()),
        sender: Some(json!(requester.record.session)),
        ..SendRequest::default()
    };
    let sent = match edge::send_message(app, message).await {
        Ok(sent) => sent,
        Err(refused) if refused.status().is_client_error() => {
            // Refused: it certainly did not arrive.
            let status = refused.status().as_u16();
            let settled = match goal {
                Some(plan_id) => {
                    abandon_wait(
                        app,
                        plan_id,
                        &request_id,
                        &format!("HTTP {status}"),
                        turn.as_deref(),
                    )
                    .await
                }
                None => false,
            };
            let then = if settled {
                "your goal records it as not delivered"
            } else {
                "nothing waits on it"
            };
            return ToolAnswer::said(
                1,
                format!(
                    "clarp-admin request: request {request_id} was refused (HTTP {status}); {then}"
                ),
            );
        }
        Err(failed) => {
            let kept = if goal.is_some() {
                "Your goal keeps waiting until its deadline; do not resend the same request."
            } else {
                "Check the recipient's chat before resending."
            };
            return ToolAnswer::said(
                5,
                format!(
                    "clarp-admin request: delivery of request {request_id} is uncertain ({}); it \
                     may have arrived. {kept}",
                    failed.message()
                ),
            );
        }
    };
    let shown = json!({
        "request_id": request_id, "to": recipient.record.session, "goal": goal,
        "trace_id": sent.trace_id, "queued": sent.queued,
    });
    ToolAnswer {
        exit: 0,
        stdout: format!(
            "{}\n",
            serde_json::to_string_pretty(&shown).unwrap_or_default()
        ),
        stderr: if goal.is_some() {
            String::new()
        } else {
            "clarp-admin request: no goal waits on it; the result arrives as a message, and \
             nothing wakes you if none comes\n"
                .to_string()
        },
        again_ms: None,
    }
}

/// How a reply reaches the one who asked.
enum Way {
    /// The asker's goal waits for it: it becomes the dependency's result.
    Dependency {
        owner: Arc<Member>,
        plan: Value,
        data: Value,
    },
    /// The asker's goal has it already.
    Delivered { plan_id: String },
    /// As a message to the asker.
    Message(Box<SendRequest>),
}

/// The message with this id in an agent's conversation, when it is there.
async fn message(member: &Member, id: &str) -> Option<clarp_kernel::conversation::Message> {
    match member.agent.row(id.to_string()).await.ok()??.entry {
        Entry::Message(message) => Some(message),
        _ => None,
    }
}

/// The reply as a message to the asker, under an id of its own: sent again, it is the same
/// message.
async fn as_message(
    replier: &Member,
    requester: &Member,
    request_id: &str,
    kind: &str,
    text: &str,
) -> SendRequest {
    let body = requests::reply_text(request_id, &who(replier), kind, text);
    // A later, different result is a message of its own: under the first one's id the Host
    // would take it for the first answer again.
    let first = format!(
        "u-{}",
        requests::reply_client_id(request_id, kind, text, false)
    );
    let another_came_first = kind == "result"
        && message(requester, &first)
            .await
            .is_some_and(|earlier| earlier.text != body);
    SendRequest {
        session: requester.record.session.clone(),
        text: body,
        client_msg_id: Some(requests::reply_client_id(
            request_id,
            kind,
            text,
            another_came_first,
        )),
        synthesize_audio: Some(false),
        hands_free: Some(false),
        origin: Some("agent".to_string()),
        sender: Some(json!(replier.record.session)),
        ..SendRequest::default()
    }
}

/// Decides how a reply reaches the asker, and who that is. The error is why it does not.
async fn plan_reply(
    app: &App,
    replier: &Member,
    request_id: &str,
    kind: &str,
    text: &str,
) -> Result<(Arc<Member>, Way), String> {
    if !KINDS.contains(&kind) {
        return Err(format!("kind must be one of {}", KINDS.join(", ")));
    }
    if text.trim().is_empty() {
        return Err("a reply needs text".to_string());
    }
    if !requests::is_request_id(request_id) {
        return Err(format!("not a request id: {request_id}"));
    }
    // The request is the message that asked: it says who asked.
    let asked = format!("u-{}", requests::request_client_id(request_id));
    let sender = message(replier, &asked)
        .await
        .and_then(|asked| asked.sender)
        .map(|sender| sender.agent_id)
        .filter(|agent_id| !agent_id.is_empty());
    let Some(sender) = sender else {
        return Err(format!(
            "request {request_id} was not sent to {}",
            replier.record.session
        ));
    };
    let directory = app.fleet.directory();
    let Some(requester) = directory
        .members
        .iter()
        .find(|member| member.record.agent_id == sender)
        .cloned()
    else {
        return Err(format!(
            "the agent that sent request {request_id} no longer exists"
        ));
    };
    if requester.record.archived_at_ms.is_some() {
        return Err(format!(
            "{}, who sent request {request_id}, is archived",
            requester.record.persona
        ));
    }
    if app
        .fleet
        .delegation_view
        .has_principal(&requester.record.agent_id)
    {
        let delegation = app
            .fleet
            .goal_delegation(crate::fleet::delegations::Request::Current(
                requester.record.agent_id.clone(),
            ))
            .await
            .map_err(|why| why.message().to_owned())?;
        if delegation["delegate_agent_id"] == replier.record.agent_id {
            return Err("a bookkeeping delegate does not reply into its principal's goal".into());
        }
    }
    // The asker's goal that names this request, whatever its state: the last changed one.
    let key = requests::dependency_key(request_id);
    let plans = requester.agent.view().books.goals.list(app.clock.now_ms());
    let plan = plans.as_array().and_then(|plans| {
        plans.iter().find(|plan| {
            matches!(
                plan["status"].as_str(),
                Some("active" | "paused" | "blocked")
            ) && plan["goal"]["continuation"]["dependency_key"] == key.as_str()
        })
    });
    let wake = plan.map(|plan| &plan["goal"]["continuation"]);
    let state = wake.and_then(|wake| wake["state"].as_str()).unwrap_or("");
    let said = requests::reply_text(request_id, &who(replier), kind, text);
    if let (true, Some(plan), Some(wake)) = (kind == "result", plan, wake) {
        // Only a result that came from a reply is this reply again: a "not delivered"
        // record, or a deadline, is not an answer.
        let recorded = &wake["dependency_result"];
        let answered = recorded["key"] == key.as_str()
            && recorded["outcome"] == "succeeded"
            && recorded["evidence"].as_str().is_some_and(|evidence| {
                evidence.starts_with(&format!("[Reply to request {request_id} "))
            });
        if answered {
            let plan_id = plan["plan_id"].as_str().unwrap_or("").to_string();
            return Ok((requester, Way::Delivered { plan_id }));
        }
        // Waiting, or held for the resume of a pause or a block: the goal takes it. A goal
        // that moved on (its deadline woke the asker, say) cannot: then the result comes
        // as a message rather than being lost.
        let truthy =
            |value: &Value| !(value.is_null() || value == &json!({}) || value == &json!(""));
        let holds = state == "waiting"
            || (matches!(state, "paused" | "blocked")
                && wake.get("paused_dependency_due_at").is_some()
                && !truthy(&wake["dependency_result"]));
        if holds {
            let data = json!({
                "key": key, "outcome": "succeeded", "evidence": requests::evidence(&said),
            });
            let way = Way::Dependency {
                owner: requester.clone(),
                plan: plan.clone(),
                data,
            };
            return Ok((requester, way));
        }
    }
    if kind != "result" && plan.is_some() && matches!(state, "paused" | "blocked") {
        return Err(format!(
            "{}'s goal is {state} by the user; only the result is kept for the resume, so send \
             it with --kind result when you have it",
            requester.record.persona
        ));
    }
    let message_to_asker = as_message(replier, &requester, request_id, kind, text).await;
    if kind != "result" {
        // A retry of an update that did arrive is the same message, not a new one.
        let id = format!(
            "u-{}",
            message_to_asker.client_msg_id.as_deref().unwrap_or("")
        );
        let already_sent = message(&requester, &id).await.is_some();
        if !already_sent {
            let page = requester.agent.tail(LOOK_BACK, Which::All).await;
            let sent = page.map_or(0, |page| {
                page.entries
                    .iter()
                    .filter(|row| {
                        matches!(row.entry, Entry::Message(_))
                            && requests::is_reply_to(row.entry.id(), request_id)
                    })
                    .count()
            });
            if sent >= MAX_UPDATES {
                return Err(format!(
                    "{MAX_UPDATES} updates already went to {} for request {request_id}; send the \
                     result, or one when something changes materially",
                    requester.record.persona
                ));
            }
        }
    }
    Ok((requester, Way::Message(Box::new(message_to_asker))))
}

/// `clarp-admin reply --request ID --from ME --kind result|blocked|progress --text TEXT`.
pub async fn reply(app: &App, args: &[String], token: Option<&str>) -> ToolAnswer {
    let valued = ["--request", "--from", "--kind", "--text"];
    let parsed = match parse(args, &valued, &[], &[]) {
        Ok(parsed) => parsed,
        Err(why) => return usage("reply", &why),
    };
    let missing: Vec<&str> = valued
        .into_iter()
        .filter(|name| parsed.option(name).is_none())
        .collect();
    if !missing.is_empty() {
        let why = format!(
            "the following arguments are required: {}",
            missing.join(", ")
        );
        return usage("reply", &why);
    }
    let kind = match parsed.choice("--kind", &KINDS, "") {
        Ok(kind) => kind,
        Err(why) => return usage("reply", &why),
    };
    let (request_id, from, text) = (
        parsed.option("--request").unwrap_or_default(),
        parsed.option("--from").unwrap_or_default(),
        parsed.option("--text").unwrap_or_default(),
    );
    let refused = |why: String| {
        ToolAnswer::said(
            4,
            format!("clarp-admin reply: {why}; nothing was delivered"),
        )
    };
    let mut as_a_message = false;
    for _ in 0..4 {
        let replier = match agent(app, from, "replier") {
            Ok(replier) => replier,
            Err(why) => return refused(why),
        };
        let (requester, way) = match plan_reply(app, &replier, request_id, &kind, text).await {
            Ok(planned) => planned,
            Err(why) => return refused(why),
        };
        // What the goal will not take goes as a message.
        let way = match way {
            Way::Dependency { .. } if as_a_message => Way::Message(Box::new(
                as_message(&replier, &requester, request_id, &kind, text).await,
            )),
            other => other,
        };
        let via = match way {
            Way::Dependency { owner, plan, data } => {
                let caller = match crate::turn_identity::presented(app, token).await {
                    Ok(Some(who)) => {
                        crate::agent::GoalCaller::Peer(who.record.agent_id.clone(), true)
                    }
                    Ok(None) => {
                        crate::agent::GoalCaller::Peer(replier.record.agent_id.clone(), false)
                    }
                    Err(why) => return refused(why.message().to_string()),
                };
                match act(&owner, &plan, "dependency", data, caller).await {
                    Ok(()) => {
                        // A helper that answers the agent that made it has reported, as a
                        // message to it would say.
                        let (helper, maker) = (&replier.record, &requester.record);
                        let noted = app
                            .fleet
                            .noted(helper.agent_id.clone(), maker.agent_id.clone())
                            .await;
                        if let Err(why) = noted {
                            app.fleet.unkept("agent", &why.0);
                        }
                        let wake = &plan["goal"]["continuation"];
                        let state = wake["state"].as_str().unwrap_or("");
                        let mut via = format!("goal {}", plan["plan_id"].as_str().unwrap_or(""));
                        if matches!(state, "paused" | "blocked") {
                            // A user's stop or pause holds the result; it is not to be sent
                            // again.
                            let why = match wake["reason"].as_str() {
                                Some(reason) if !reason.is_empty() => format!(": {reason}"),
                                _ => String::new(),
                            };
                            via.push_str(&format!(
                                " (held: the goal is {state}{why}; it wakes the requester when \
                                 the user resumes it)"
                            ));
                        }
                        via
                    }
                    // Someone else wrote the goal: decide again.
                    Err(why) if why.contains("plan changed") => continue,
                    // The goal will not take it (closed, or no longer waiting): as a message.
                    Err(_) => {
                        as_a_message = true;
                        continue;
                    }
                }
            }
            Way::Delivered { plan_id } => format!("goal {plan_id} (already recorded)"),
            Way::Message(message) => match edge::send_message(app, *message).await {
                Ok(_) => "message".to_string(),
                Err(refusal) if refusal.status().is_client_error() => {
                    return ToolAnswer::said(
                        4,
                        format!(
                            "clarp-admin reply: the Host refused the reply (HTTP {}); nothing \
                             was delivered",
                            refusal.status().as_u16()
                        ),
                    );
                }
                Err(failed) => {
                    return ToolAnswer::said(
                        5,
                        format!(
                            "clarp-admin reply: delivery is uncertain ({}); send the same reply \
                             again later (it is deduplicated)",
                            failed.message()
                        ),
                    );
                }
            },
        };
        let shown = json!({
            "request_id": request_id, "kind": kind, "to": requester.record.session, "via": via,
        });
        return ToolAnswer::printed(0, serde_json::to_string_pretty(&shown).unwrap_or_default());
    }
    ToolAnswer::said(
        1,
        "clarp-admin reply: the requester's goal kept changing; try again",
    )
}
