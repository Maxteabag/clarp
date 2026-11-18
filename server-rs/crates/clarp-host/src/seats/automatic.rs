//! The scheduled Hotseat Janitor. Its owner and generation are checked again after the
//! quota read and inside the provider lock before a decision can move a login.
use super::*;
use clarp_kernel::janitors::adaptive;
use clarp_kernel::seat_switch::{self, Demand};
const ROLE: &str = "account-hotseat";

async fn owner(app: &App) -> Option<(String, u64, Value)> {
    app.janitors
        .read(app, |book, world, _| {
            let id = book.resolve(ROLE, None, world).ok()??;
            let config = book.configs.get(&id)?;
            let options = clarp_kernel::janitors::option_values(
                ROLE,
                Some(&Value::Object(config.options.clone())),
            )
            .ok()?;
            Some((id, config.generation, Value::Object(options)))
        })
        .await
        .ok()
        .flatten()
}

pub(super) async fn enabled(app: &App) -> bool {
    if app.config.accounts != Accounts::Select || !app.seats.told() {
        return false;
    }
    owner(app).await.is_some()
}

pub fn start(app: &App) {
    if app.config.accounts != Accounts::Select || !app.seats.told() {
        return;
    }
    // The account mode and configured tool are fixed for this Host process.
    // Only an admitted worker looks up Janitor owners on subsequent ticks.
    let app = app.clone();
    tokio::spawn(async move {
        let every = Duration::from_millis(app.config.janitors_checked_every_ms.max(50));
        loop {
            tokio::time::sleep(every).await;
            if let Some((id, generation, options)) = owner(&app).await {
                for provider in ["claude", "codex"] {
                    tick(&app, &id, generation, &options, provider).await;
                }
            }
        }
    });
}

pub(super) async fn current(app: &App, run: &str) -> bool {
    app.janitors
        .read(app, |book, world, now| book.is_current(run, world, now))
        .await
        .unwrap_or(false)
}

fn models(app: &App, tool: &str, now: i64, base: i64) -> (Vec<String>, Vec<String>) {
    if tool != "claude" {
        return (vec![], vec![]);
    }
    let mut needed = Vec::new();
    let mut running = Vec::new();
    let dir = app.fleet.directory();
    for m in dir
        .members
        .iter()
        .filter(|m| m.record.backend == tool && m.record.archived_at_ms.is_none())
    {
        let view = m.agent.view();
        let status = &view.status;
        let model = &m.record.model;
        let live = status.live.as_ref();
        let runs = live.is_some_and(|t| !waits_for_an_account(t));
        let recent = stopped(status).is_some_and(|(cause, at)| {
            matches!(cause, Cause::UsageLimit)
                && (0..=(base * 2000).max(900000)).contains(&(now - at))
        });
        if !model.is_empty()
            && (live.is_some() || !status.paused && !status.waiting.is_empty() || recent)
        {
            needed.push(model.clone());
            if runs {
                running.push(model.clone());
            }
        }
    }
    (needed, running)
}

async fn tick(app: &App, id: &str, generation: u64, options: &Value, tool: &str) {
    let now = app.clock.now_ms();
    let claude = tool == "claude";
    let base = options[if claude {
        "interval_seconds"
    } else {
        "codex_interval_seconds"
    }]
    .as_i64()
    .unwrap_or(if claude { 300 } else { 900 });
    let floor = options[if claude {
        "claude_min_remaining"
    } else {
        "codex_min_remaining"
    }]
    .as_i64()
    .unwrap_or(if claude { 25 } else { 10 });
    let mode = options["mode"].as_str().unwrap_or("automatic");
    let maximum = options["max_interval_seconds"].as_i64();
    let lane = format!("account-switch/{id}/{tool}");
    let last = {
        let mut state = app.seats.state.lock().await;
        let Some(kept) = state.kept(app).await else {
            return;
        };

        kept.last_switch.get(tool).cloned().unwrap_or(Value::Null)
    };
    let run = app
        .janitors
        .with(app, |book, world, at| {
            if !book
                .configs
                .get(id)
                .is_some_and(|c| c.enabled && c.generation == generation)
            {
                return None;
            }
            let progress = book.progress.entry(lane.clone()).or_default();
            if progress.generation != generation {
                *progress = Default::default();
                progress.generation = generation;
            }
            if progress.next_run_at.is_some_and(|at| now < at) {
                return None;
            }
            let interval =
                adaptive::current(progress.adaptive.get_or_insert_default(), base, maximum);
            progress.next_run_at = Some(now + interval * 1000);
            let request = json!(format!("{id}-{generation}-{tool}-{now}"));
            let run = book
                .begin_run_of(
                    Some(id.to_string()),
                    ROLE,
                    &request,
                    Some(&json!({"summary":tool})),
                    None,
                    world,
                    at,
                )
                .ok()??;
            let rid = run["run_id"].as_str()?;
            book.claim_run(rid, world, at).then(|| rid.to_string())
        })
        .await
        .ok()
        .flatten();
    let Some(run) = run else {
        return;
    };
    let args: &[&str] = if claude {
        &["list", "--json"]
    } else {
        &["codex", "--json"]
    };
    let read = app
        .seats
        .command(args, if claude { 60 } else { 150 }, None, false)
        .await;
    if !current(app, &run).await {
        return;
    }
    let (mut action, mut error) = (String::from("skip"), String::new());
    let accounts = read
        .as_ref()
        .map(|body| seat_switch::parse(tool, body))
        .unwrap_or_default();
    let (required, running) = models(app, tool, now, base);
    let decision = seat_switch::decide(
        &accounts,
        Demand {
            floor,
            required: &required,
            running: &running,
            last: &last,
            now,
            cooldown: (3 * base * 1000).max(900000),
        },
    );
    let mut switched = false;
    if read.is_ok() {
        action = decision["action"].as_str().unwrap_or("skip").into();
        if mode == "automatic" && action == "switch" {
            let target = decision["target"].as_str().unwrap_or("");
            let active = decision["active"].as_str().unwrap_or("");
            if resume::switch_wanted(tool, target).is_ok() {
                let answer =
                    switching::perform(app, tool, target, Some((active, &run, &last))).await;
                if answer.1["reason"] == "superseded" {
                    action = "noop".into();
                } else {
                    switched = answer.1["switched"] == true;
                }
            }
            if action == "switch" && !switched {
                error = format!("{tool}: Hotseat did not confirm the switch");
            }
        }
    } else {
        error = format!("{tool}: Hotseat reading unavailable");
    }
    if !current(app, &run).await {
        return;
    }
    let superseded = action == "noop" && decision["action"] != "noop";
    let (text, normal) = seat_switch::report(tool, &decision, mode, switched);
    let summary = if superseded {
        format!(
            "{tool}: the default account was switched while this check ran; deciding again at the next check"
        )
    } else if error.is_empty() || !text.is_empty() {
        normal
    } else {
        error.clone()
    };
    let outcome = if error.is_empty() {
        "completed"
    } else {
        "failed"
    };
    let activity = if !error.is_empty() {
        "failed"
    } else if action == "noop" {
        "idle"
    } else {
        "worked"
    };
    let done = json!({"status":action,"summary":summary,"reason":decision["reason"].as_str().unwrap_or("")});
    let next = app
        .janitors
        .with(app, |book, world, at| {
            if !book
                .complete_run(
                    &run,
                    outcome,
                    Some(&done),
                    &error,
                    Some((activity, &summary)),
                    world,
                    at,
                )
                .ok()?
            {
                return None;
            }
            let progress = book.progress.get_mut(&lane)?;
            let interval = adaptive::advance(
                progress.adaptive.get_or_insert_default(),
                activity,
                &summary,
                (base, maximum),
                Some(now),
            )?;
            progress.next_run_at = Some(now + 1000 * interval);
            progress.next_run_at
        })
        .await
        .ok()
        .flatten();
    let Some(until) = next else { return };
    if superseded {
        return;
    }
    let mut state = app.seats.state.lock().await;
    let Some(kept) = state.kept(app).await else {
        return;
    };
    // A read or a recommendation can finish after a manual switch. Never resume
    // a model, send a push or publish inventory from the superseded decision.
    let latest = kept.last_switch.get(tool).unwrap_or(&Value::Null);
    if (!switched && latest != &last) || (switched && latest["to"] != decision["target"]) {
        return;
    }
    match read {
        Ok(body) => {
            kept.seats.read(tool, &body, now);
            kept.desk.absorb(tool, &body, now);
            if let Some(last) = kept.last_switch.get(tool)
                && last["at"].as_i64().is_some_and(|at| at >= now)
                && let Some(target) = last["to"].as_str()
            {
                kept.desk.mark_active(tool, target);
            }
            kept.seats.acted(
                tool,
                &clarp_kernel::seats::Acted {
                    decision: &decision,
                    mode,
                    switched,
                    now,
                    floor_pct: &json!(floor),
                    cadence_s: &json!(base),
                    next_due_at: &json!(until),
                },
            );
        }
        Err(_) => kept.seats.failed(tool, "cli_failed", now),
    }
    let situation = json!([
        action,
        decision["active"],
        decision["target"],
        switched,
        decision["model"]
    ])
    .to_string();
    let distinct = kept.notifications.get(tool) != Some(&situation);
    let notify = action == "exhausted" && distinct;
    if action == "noop" {
        kept.notifications.remove(tool);
    } else {
        kept.notifications.insert(tool.into(), situation);
    }
    if let Ok(data) = serde_json::to_vec(kept) {
        let _ = app.fleet.keep(KIND, KEPT.into(), Some(data)).await;
    }
    drop(state);
    let serving = if switched {
        &decision["target"]
    } else {
        &decision["active"]
    };
    if let Some(account) = accounts
        .iter()
        .find(|a| serving.as_str() == Some(a.alias.as_str()))
        && let Some(pool) = app.pools.iter().find(|p| p.tool() == tool)
        && account.remaining.unwrap_or(0) > 0
    {
        pool.available(|model| !claude || account.serves(model));
    }
    if notify && let Some(member) = app.fleet.directory().janitor(id) {
        app.pusher.turn_done(crate::pusher::TurnDone {
            // Hotseat exhaustion receipts carry neither a badge nor an unread chat item.
            badge: false,
            unread: false,
            session: member.record.session.clone(),
            persona: member.record.persona.clone(),
            notification_id: run,
            preview: text,
            needs_response: false,
        });
    }
    crate::janitors::changed(app);
}
