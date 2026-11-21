//! Bookkeeping listeners are Host workers. The principal owns their receipts
//! and the delegate owns their visible background jobs.
use crate::{
    agent::{AgentHandle, DelegationCommand, DelegationState, Unavailable},
    edge::{self, App},
    fleet::delegations::Request,
};
use clarp_actor::bookkeeping::ListenerUpdate;
use clarp_kernel::{
    goals::{
        delegation::carries_identity,
        listener::{Decision, Pending},
    },
    jobs::{JobCommand, JobStatus, Worker},
};
use clarp_protocol::requests::SendRequest;
use serde::Deserialize;
use serde_json::json;
use std::{
    collections::{BTreeMap, BTreeSet},
    time::Duration,
};

#[derive(Deserialize)]
struct Listener {
    id: String,
    principal: String,
    delegate: String,
    baseline: i64,
    created: i64,
}

async fn ask(owner: &AgentHandle, cmd: DelegationCommand) -> Result<DelegationState, Unavailable> {
    let agent = owner.clone();
    tokio::task::spawn_blocking(move || agent.delegation(cmd))
        .await
        .map_err(|e| Unavailable::new(e.to_string()))?
}

#[derive(Default)]
struct Window {
    pending: Pending,
    source: Option<(
        clarp_actor::bookkeeping::ActivityVersion,
        clarp_kernel::goals::listener::Activity,
    )>,
}

async fn tick(app: &App, row: &Listener, window: &mut Window) -> Result<(), Unavailable> {
    let now = app.clock.now_ms();
    let dir = app.fleet.directory();
    let Some(owner) = dir.anyone(&row.principal) else {
        return Ok(());
    };
    let Some(helper) = dir.anyone(&row.delegate) else {
        return Ok(());
    };
    let stop = |reason: &str| Request::Stop {
        id: row.id.clone(),
        reason: reason.into(),
    };
    if !carries_identity(&owner.record.backend) || !carries_identity(&helper.record.backend) {
        app.fleet
            .goal_delegation(stop(
                "principal or delegate moved to a backend without turn identity",
            ))
            .await
            .map_err(|e| Unavailable::new(e.message().into()))?;
        return Ok(());
    }
    let snapshot = ask(
        &owner.agent,
        DelegationCommand::ListenerRead(
            row.id.clone(),
            row.delegate.clone(),
            row.baseline,
            window.source.as_ref().map(|s| s.0),
        ),
    )
    .await?;
    let Some(progress) = snapshot.applied else {
        return Ok(());
    };
    if let (Some(version), Some(activity)) = (snapshot.version, snapshot.activity) {
        window.source = Some((version, activity));
    }
    let Some((_, activity)) = &window.source else {
        return Ok(());
    };
    let pending = &mut window.pending;
    let view = helper.agent.view();
    let job_id = format!("bookkeeping-{}", row.id);
    let job = view.jobs.iter().find(|job| job.id == job_id);
    if job.is_some_and(|j| j.status == JobStatus::Cancelled) {
        app.fleet
            .goal_delegation(stop("listener job cancelled"))
            .await
            .map_err(|e| Unavailable::new(e.message().into()))?;
        return Ok(());
    }
    let job_handle = if let Some(job) = job.filter(|job| job.status.is_active()) {
        if now.saturating_sub(job.heartbeat_at_ms.unwrap_or(job.started_at_ms)) >= 60_000 {
            helper
                .agent
                .job(JobCommand::Heartbeat {
                    id: job_id.clone(),
                    generation: Some(job.generation),
                    worker: Worker::none(),
                    source: "listener".into(),
                })
                .await?
                .map_err(|e| Unavailable::new(e.to_string()))?;
        }
        job.handle()
    } else {
        app.fleet
            .claim_job(job_id.clone(), helper.record.agent_id.clone())
            .await?
            .map_err(|_| Unavailable::new("listener job belongs to another owner".into()))?;
        let cmd = JobCommand::Register {
            queued: false,
            id: job_id.clone(),
            kind: "listener".into(),
            title: format!("Bookkeeping listener for {}", owner.record.session),
            detail: "Wakes the bookkeeping delegate when its principal acts".into(),
            worker: Worker::none(),
            restart_cancelled: false,
            started_trace_id: String::new(),
            heartbeat_timeout_ms: None,
            metadata: json!({"delegation_id":row.id}),
        };
        helper
            .agent
            .job(cmd)
            .await?
            .map_err(|e| Unavailable::new(e.to_string()))?
            .job
            .ok_or_else(|| Unavailable::new("listener job unavailable".into()))?
            .handle()
    };
    let prefix = format!("bookkeeping-{}-", row.id);
    let live = view.status.live.as_ref();
    let queued = live.is_some_and(|l| l.parked.is_some() && l.client_id.starts_with(&prefix))
        || (!view.status.waiting.is_empty()
            && helper
                .agent
                .queue()
                .await?
                .0
                .iter()
                .any(|q| q.client_id.starts_with(&prefix)));
    let busy =
        clarp_kernel::goals::listener::busy_reason(queued, live.map(|l| l.started_at_ms), now);
    let applied = (progress.message_through, progress.goal_event_through);
    let mut state = progress.listener;
    let since = (activity.marker != (0, 0))
        .then(|| activity.oldest.unwrap_or(now).max(row.created).min(now));
    if let Decision::Send(wake) = pending.decide(&mut state, &row.id, applied, activity, busy, now)
    {
        let send = SendRequest {
            session: helper.record.session.clone(),
            text: wake.text(&row.id, &owner.record.session),
            client_msg_id: Some(wake.id.clone()),
            synthesize_audio: Some(false),
            hands_free: Some(false),
            force_session: Some(true),
            queue_if_busy: Some(true),
            origin: Some("automation".into()),
            ..Default::default()
        };
        match edge::send_own(app, send).await {
            Ok(_) => pending.sent(&mut state, &wake, now),
            Err(error) => pending.failed(&mut state, error.message(), now),
        }
    }
    ask(
        &owner.agent,
        DelegationCommand::ListenerWrite(
            row.id.clone(),
            row.delegate.clone(),
            row.baseline,
            ListenerUpdate {
                state,
                applied,
                since,
                job_handle,
            },
        ),
    )
    .await?;
    Ok(())
}

pub fn run(app: &App) {
    let app = app.clone();
    tokio::spawn(async move {
        let mut windows: BTreeMap<String, (Window, String)> = BTreeMap::new();
        let mut recover_jobs = true;
        loop {
            tokio::time::sleep(Duration::from_secs(2)).await;
            let Ok(rows) = app.fleet.goal_delegation(Request::Listeners).await else {
                continue;
            };
            let Ok(rows) = serde_json::from_value::<Vec<Listener>>(rows) else {
                continue;
            };
            let mut active = BTreeSet::new();
            for row in rows {
                active.insert(row.id.clone());
                let (pending, _) = windows
                    .entry(row.id.clone())
                    .or_insert_with(|| (Window::default(), row.delegate.clone()));
                if let Err(e) = tick(&app, &row, pending).await {
                    eprintln!("clarp-host: bookkeeping listener {}: {}", row.id, e.0);
                }
            }
            // A stop may have committed just before the previous Host exited.
            // Across restart there is no in-memory window to trigger that job's finish.
            if recover_jobs {
                let directory = app.fleet.directory();
                for member in directory.members.iter().chain(&directory.janitors) {
                    for job in &member.agent.view().jobs {
                        if job.kind == "listener"
                            && job.status.is_active()
                            && let Some(id) =
                                job.metadata.get("delegation_id").and_then(|v| v.as_str())
                            && job.id == format!("bookkeeping-{id}")
                            && !active.contains(id)
                        {
                            windows.entry(id.into()).or_insert_with(|| {
                                (Window::default(), member.record.agent_id.clone())
                            });
                        }
                    }
                }
                recover_jobs = false;
            }
            let finished: Vec<_> = windows
                .keys()
                .filter(|id| !active.contains(*id))
                .cloned()
                .collect();
            for id in finished {
                let Some((_, delegate)) = windows.get(&id) else {
                    continue;
                };
                if let Some(member) = app.fleet.directory().anyone(delegate) {
                    let job_id = format!("bookkeeping-{id}");
                    if let Some(job) = member
                        .agent
                        .view()
                        .jobs
                        .iter()
                        .find(|j| j.id == job_id && j.status.is_active())
                    {
                        let finished = member
                            .agent
                            .job(JobCommand::Finish {
                                id: job_id,
                                generation: job.generation,
                                worker: Worker::none(),
                                failed: None,
                            })
                            .await;
                        if !matches!(finished, Ok(Ok(_))) {
                            continue;
                        }
                    }
                }
                windows.remove(&id);
            }
        }
    });
}
