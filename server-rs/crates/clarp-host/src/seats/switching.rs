//! Switching has one queue per provider. Wait for the machine's login lock before
//! reading the active account, so a queued request sees the preceding switch.
use super::*;
use clarp_kernel::seat_switch;

fn unavailable(tool: &str, alias: &str) -> resume::Answer {
    (
        503,
        json!({"ok":false,"error":"hotseat_unavailable","provider":tool,"alias":alias,
    "message":"Hotseat could not say which account is in use. Nothing was switched."}),
    )
}
pub(super) async fn manual(app: &App, tool: String, alias: String) -> resume::Answer {
    if !app.seats.told() {
        return unavailable(&tool, &alias);
    }
    let (owner, provider, target) = (app.clone(), tool.clone(), alias.clone());
    let task = tokio::spawn(async move {
        let answer = perform(&owner, &provider, &target, None).await;
        if answer.1["switched"] == true {
            let reader = owner.clone();
            tokio::spawn(async move {
                reader.seats.read(&reader,true).await;
            });
        }
        answer
    });
    match tokio::time::timeout(Duration::from_secs(30), task).await {
        Ok(Ok(result)) => result,
        Ok(Err(_)) => (
            500,
            json!({"ok":false,"error":"switch_error","provider":tool,"alias":alias}),
        ),
        Err(_) => (
            202,
            json!({"ok":true,"switched":false,"pending":true,"reason":"switch_in_progress","provider":tool,"alias":alias,
            "message":"The switch is still running. The desk shows how it ended."}),
        ),
    }
}

pub(super) async fn perform(app: &App, tool: &str, target: &str, expected: Option<(&str,&str,&Value)>) -> resume::Answer {
    let Some(pool) = app.pools.iter().find(|p| p.tool() == tool) else {
        return unavailable(tool, target);
    };
    let desk = &app.seats;
    let _serial = desk.switches[usize::from(tool == "codex")].lock().await;
    let now = app.clock.now_ms();
    let mut record = json!({"provider":tool,"alias":target,"requested_at":now,"finished_at":null,"state":"running","from":null});
    if expected.is_none() {
        desk.state
            .lock()
            .await
            .running
            .insert(tool.into(), record.clone());
    }
    let lock = pool.switch_lock().await;
    let active_args: &[&str] = if tool == "claude" {
        &["list", "--json"]
    } else {
        &["codex", "--no-quota", "--json"]
    };
    let active = match &lock {
        Some(file) => desk
            .command(active_args, 45, Some(file), false)
            .await
            .map(|v| {
                seat_switch::parse(tool, &v)
                    .into_iter()
                    .find(|s| s.active)
                    .map(|s| s.alias)
            }),
        None => Err(Failed::DidNotRun),
    };
    let (state, answer) = match active {
        Err(_) => ("unavailable", unavailable(tool, target)),
        Ok(from) => {
            record["from"] = json!(from);
            if expected.is_none() {
                desk.state
                    .lock()
                    .await
                    .running
                    .insert(tool.into(), record.clone());
            }
            if let Some((was,run,last))=expected {
                let mut state=desk.state.lock().await;
                let same=state.kept(app).await.is_some_and(|kept|kept.last_switch.get(tool).unwrap_or(&Value::Null)==last);
                drop(state);
                if !same || from.as_deref()!=Some(was) || !automatic::current(app,run).await {
                    return (409,json!({"reason":"superseded"}));
                }
            }
            if from.as_deref() == Some(target) {
                let alias = clarp_kernel::seats::display_alias(&json!(target));
                (
                    "already_active",
                    (
                        200,
                        json!({"ok":true,"switched":false,"reason":"already_active","from":from,"provider":tool,"alias":target,
                    "message":format!("{alias} is already the account in use.")}),
                    ),
                )
            } else {
                let args: &[&str] = if tool == "claude" {
                    &["switch", target, "--yes", "--json"]
                } else {
                    &["codex-account", "switch", target]
                };
                let confirmed = desk
                    .command(args, 60, lock.as_ref(), tool == "codex")
                    .await
                    .is_ok_and(|v| {
                        if tool == "codex" {
                            v == true
                        } else {
                            clarp_kernel::python::truthy(&v["switched"])
                        }
                    });
                if confirmed {
                    (
                        "switched",
                        (
                            200,
                            json!({"ok":true,"switched":true,"from":from,"provider":tool,"alias":target}),
                        ),
                    )
                } else {
                    (
                        "failed",
                        (
                            409,
                            json!({"ok":false,"error":"switch_failed","from":from,"provider":tool,"alias":target,"message":"Hotseat did not confirm the switch."}),
                        ),
                    )
                }
            }
        }
    };
    let at = app.clock.now_ms();
    record["state"] = json!(state);
    record["finished_at"] = json!(at);
    let mut held = desk.state.lock().await;
    held.running.remove(tool);
    if let Some(kept) = held.kept(app).await {
        if state == "switched" {
            kept.last_switch.insert(
                tool.into(),
                json!({"from":record["from"],"to":target,"at":at}),
            );
            kept.desk.mark_active(tool, target);
        }
        if expected.is_none() {
            kept.switches.insert(tool.into(), record);
        }
        if let Ok(bytes) = serde_json::to_vec(kept) {
            let _ = app.fleet.keep(KIND, KEPT.into(), Some(bytes)).await;
        }
    }
    drop(held);
    drop(lock);
    if state == "switched" && expected.is_none() {
        pool.available(|_| true);
    }
    answer
}
