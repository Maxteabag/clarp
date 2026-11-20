//! Delegate-only goal-tool commands. The fleet verifies the credential and
//! resolves the principal; the principal checks the grant again in its mailbox.
use crate::fleet::delegations::Request;
use crate::{
    edge::App,
    tools::{ToolAnswer, ToolCall},
};
use clarp_actor::bookkeeping::Batch;
use serde_json::{Value, json};

pub struct Command {
    pub grant: Value,
    pub caller: String,
    pub data: Value,
    pub record: Option<(String, String)>,
}

pub(crate) fn execute(
    actor: &mut clarp_actor::Actor,
    cmd: Command,
    now: i64,
) -> Result<(ToolAnswer, clarp_actor::Outputs), clarp_actor::ActorError> {
    let mut grant = cmd.grant;
    let id = grant["delegation_id"].as_str().unwrap_or_default();
    let initial = grant["message_through"].as_i64().unwrap_or_default();
    let render = crate::wire::row;
    if let Some((plan, wake)) = cmd.record {
        let batch = Batch {
            id,
            caller: &cmd.caller,
            plan: &plan,
            wake: &wake,
            data: &cmd.data,
            initial_message: initial,
            created_at: grant["created_at"].as_i64().unwrap_or_default(),
            now,
        };
        match actor.record_books(&batch, render)? {
            Ok((count, progress, out)) => {
                progress.show(&mut grant, now);
                Ok((
                    ToolAnswer::printed(0, json!({"delegation":grant,"applied":count}).to_string()),
                    out,
                ))
            }
            Err(e) => Ok((
                ToolAnswer::said(e.tool_exit(), format!("agent_tasks: {e}")),
                Default::default(),
            )),
        }
    } else {
        let options = match clarp_kernel::goals::bookkeeping::observe_options(&cmd.data) {
            Ok(v) => v,
            Err(e) => {
                return Ok((
                    ToolAnswer::said(1, format!("agent_tasks: {e}")),
                    Default::default(),
                ));
            }
        };
        let said = match actor.observe_books(id, &cmd.caller, initial, &options, render)? {
            Some(page) => ToolAnswer::printed(0, page.to_string()),
            None => ToolAnswer::said(4, "agent_tasks: this delegation is stopped"),
        };
        Ok((said, Default::default()))
    }
}

pub(crate) async fn run(app: &App, call: &ToolCall) -> ToolAnswer {
    let args = &call.argv[1..];
    let (id, options, record) = match args {
        [action, id, rest @ ..] if action == "observe" && rest.len() <= 1 => {
            (id, rest.first(), None)
        }
        [action, id, plan, wake, batch] if action == "record" => {
            (id, Some(batch), Some((plan.clone(), wake.clone())))
        }
        _ => return ToolAnswer::said(2, crate::goaltool::USAGE),
    };
    let data = match options
        .map(|s| serde_json::from_str::<Value>(s))
        .transpose()
    {
        Ok(data) => data.unwrap_or_else(|| json!({})),
        Err(e) => return ToolAnswer::said(1, format!("agent_tasks: {e}")),
    };
    let token = call
        .env
        .get(crate::turn_identity::TURN_ENV)
        .map(String::as_str);
    let caller = match crate::turn_identity::presented(app, token).await {
        Ok(v) => v.map(|m| m.record.agent_id.clone()).unwrap_or_default(),
        Err(e) => return ToolAnswer::said(4, format!("agent_tasks: {}", e.message())),
    };
    if let Some((_, wake)) = &record {
        if !data.is_object() {
            return ToolAnswer::said(
                1,
                format!(
                    "agent_tasks: '{}' object has no attribute 'get'",
                    clarp_kernel::goals::subgoals::type_name(&data)
                ),
            );
        }
        let empty = json!([]);
        let entries = &data["entries"];
        if let Err(e) = clarp_kernel::goals::bookkeeping::batch(
            wake,
            if clarp_kernel::python::truthy(entries) {
                entries
            } else {
                &empty
            },
        ) {
            return ToolAnswer::said(1, format!("agent_tasks: {e}"));
        }
    }
    if record.is_none()
        && let Err(e) = clarp_kernel::goals::bookkeeping::observe_keywords(&data)
    {
        return ToolAnswer::said(1, format!("agent_tasks: {e}"));
    }
    let grant = match app
        .fleet
        .goal_delegation(Request::Authorize {
            id: id.clone(),
            caller: caller.clone(),
            credential: call.bookkeeping_credential.clone(),
        })
        .await
    {
        Ok(row) => row,
        Err(e) => {
            return ToolAnswer::said(
                if e.status().is_server_error() { 1 } else { 4 },
                format!("agent_tasks: {}", e.message()),
            );
        }
    };
    let principal = grant["principal_agent_id"].as_str().unwrap_or_default();
    let directory = app.fleet.directory();
    if let Some((pid, _)) = &record
        && data["entries"].as_array().is_some_and(|a| !a.is_empty())
        && crate::goaltool::owner_of(pid) != principal
    {
        let durable = directory
            .anyone(crate::goaltool::owner_of(pid))
            .is_some_and(|m| {
                m.agent
                    .view()
                    .books
                    .goals
                    .plan(pid)
                    .is_some_and(|p| p.goal.is_some())
            });
        return if durable {
            ToolAnswer::said(4, "agent_tasks: that goal belongs to another agent")
        } else {
            ToolAnswer::said(1, "agent_tasks: no durable goal with that id")
        };
    }
    let Some(member) = directory.anyone(principal) else {
        return ToolAnswer::said(
            4,
            "agent_tasks: the principal or delegate is deleted or archived",
        );
    };
    match member
        .agent
        .bookkeeping(Command {
            grant,
            caller,
            data,
            record,
        })
        .await
    {
        Ok(said) => said,
        Err(e) => ToolAnswer::said(1, format!("agent_tasks: {}", e.0)),
    }
}
