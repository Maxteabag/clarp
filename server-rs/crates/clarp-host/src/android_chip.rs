//! The fleet summary subscribed to by a paired Android phone.
use crate::{
    edge::{ApiError, App, Principal},
    fleet::Pushed,
    pairing::Scope,
    relay::Outcome,
};
use axum::{
    Json,
    http::StatusCode,
    response::{IntoResponse, Response},
};
use clarp_kernel::fcm::{self, ActivityBudget};
use serde_json::{Value, json};
use std::collections::HashMap;
use std::sync::Mutex;
use std::time::Duration;
use tokio::sync::Notify;

/// What this desk keeps, declared once (`kept.rs`: the registry walks it at every start).
pub(crate) const KEPT: &[crate::kept::Kind] = &[
    crate::kept::Kind::book(REGISTER, crate::kept::Key::Plain, crate::kept::json),
    crate::kept::Kind::book(
        BUDGET,
        crate::kept::Key::Plain,
        crate::kept::fits::<ActivityBudget>,
    ),
];

const REGISTER: &str = "android_activity";
const BUDGET: &str = "android_activity_budget";
type State = (Value, bool);

#[derive(Default)]
pub struct Desk {
    latest: Mutex<Option<State>>,
    wake: Notify,
    writes: tokio::sync::Mutex<()>,
}

#[derive(Default)]
struct Device {
    pending: Option<State>,
    paused: i64,
    spent: i64,
    floor: i64,
}

fn tokens(app: &App, owner: &str) -> Vec<String> {
    let directory = app.fleet.directory();
    directory
        .push
        .active()
        .filter(|r| r.provider == "fcm" && r.principal == owner && directory.can_receive(owner))
        .map(|r| r.token.clone())
        .collect()
}

impl Desk {
    pub fn offer(&self, state: &Value, high: bool) {
        if let Ok(mut held) = self.latest.lock() {
            let urgent = high || held.as_ref().is_some_and(|(_, high)| *high);
            *held = Some((state.clone(), urgent));
        }
        self.wake.notify_one();
    }

    pub async fn forget(&self, app: &App, owner: &str) -> Result<(), crate::agent::Unavailable> {
        let _guard = self.writes.lock().await;
        app.fleet.keep(REGISTER, owner.into(), None).await?;
        app.fleet.keep(BUDGET, owner.into(), None).await?;
        self.wake.notify_one();
        Ok(())
    }

    pub async fn registration(
        &self,
        app: &App,
        who: &Principal,
        kind: &Value,
        ended: bool,
    ) -> Response {
        let Principal::Device {
            device_id,
            scope: Scope::Full,
        } = who
        else {
            return ApiError::new(StatusCode::FORBIDDEN, "full device access required")
                .into_response();
        };
        let _guard = self.writes.lock().await;
        if !app.fleet.directory().can_receive(device_id) {
            return ApiError::new(StatusCode::FORBIDDEN, "full device access required")
                .into_response();
        }
        let prior = match app.fleet.kept(REGISTER, device_id.clone()).await {
            Ok(row) => row,
            Err(why) => return ApiError::from(why).into_response(),
        };
        let (row, answer) = if ended {
            (
                None,
                json!({"ok":true,"removed":if prior.is_some(){1}else{0}}),
            )
        } else {
            if clarp_kernel::python::truthy(kind) && kind != "agents" && kind != "agents-working" {
                return (
                    StatusCode::BAD_REQUEST,
                    Json(json!({"ok":false,"error":"kind must be agents-working"})),
                )
                    .into_response();
            }
            if tokens(app, device_id).is_empty() {
                return (
                    StatusCode::CONFLICT,
                    Json(json!({"ok":false,"error":"fcm_push_token_required"})),
                )
                    .into_response();
            }
            // The generation fences a slow delivery from an earlier registration.
            let shown = prior
                .as_ref()
                .and_then(|row| serde_json::from_slice::<Value>(row).ok())
                .is_some_and(|r| r["shown"] == true);
            let row = json!({"generation":crate::ids::hex(16),"shown":shown});
            (
                Some(row.to_string().into_bytes()),
                json!({"ok":true,"platform":"fcm","kind":"agents-working"}),
            )
        };
        match app.fleet.keep(REGISTER, device_id.clone(), row).await {
            Ok(()) => {
                self.wake.notify_one();
                Json(answer).into_response()
            }
            Err(why) => ApiError::from(why).into_response(),
        }
    }

    async fn flush(&self, app: &App, devices: &mut HashMap<String, Device>) -> Option<i64> {
        let latest = self.latest.lock().ok()?.take();
        let registrations = app.fleet.kept_all(REGISTER).await.ok()?;
        devices.retain(|id, _| registrations.iter().any(|(owner, _)| id == owner));
        let mut next = None;
        for (owner, bytes) in registrations {
            let Ok(mut row) = serde_json::from_slice::<Value>(&bytes) else {
                continue;
            };
            let targets = tokens(app, &owner);
            if targets.is_empty() {
                devices.remove(&owner);
                continue;
            }
            let device = devices.entry(owner.clone()).or_default();
            if let Some((state, high)) = &latest {
                device.pending = Some((
                    state.clone(),
                    *high || device.pending.as_ref().is_some_and(|p| p.1),
                ));
            }
            let Some((state, high)) = &device.pending else {
                continue;
            };
            let payload = fcm::activity(state, &app.server_id);
            let end = payload["event"] == "end";
            if end && row["shown"] != true {
                device.pending = None;
                continue;
            }
            let now = app.clock.now_ms();
            let due = if end {
                device.paused
            } else {
                device.spent.max(device.paused)
            };
            if due > now {
                next = Some(next.map_or(due, |n: i64| n.min(due)));
                continue;
            }
            let reserved = {
                let _guard = self.writes.lock().await;
                if !app.fleet.directory().can_receive(&owner) {
                    continue;
                }
                match app.fleet.kept(BUDGET, owner.clone()).await {
                    Ok(raw) => {
                        let budget = match raw {
                            None => Some(ActivityBudget::default()),
                            Some(raw) => serde_json::from_slice(&raw).ok(),
                        };
                        match budget.map(|mut budget| (budget.reserve(now, !end), budget)) {
                            Some((Ok(()), budget)) => app
                                .fleet
                                .keep(BUDGET, owner.clone(), serde_json::to_vec(&budget).ok())
                                .await
                                .ok()
                                .map(|_| 0),
                            Some((Err(at), _)) => Some(at),
                            None => None,
                        }
                    }
                    Err(_) => None,
                }
            };
            if reserved != Some(0) {
                let at = reserved.unwrap_or(now + 60_000);
                if reserved.is_none() {
                    device.paused = at;
                } else {
                    device.spent = at;
                }
                next = Some(next.map_or(at, |n| n.min(at)));
                continue;
            }
            let priority = if *high && !end { "10" } else { "5" };
            let (mut delivered, mut retry) = (false, false);
            for token in targets {
                if !tokens(app, &owner).contains(&token) {
                    continue;
                }
                let (sender, payload, to) = (app.android.clone(), payload.clone(), token.clone());
                let result = tokio::task::spawn_blocking(move || {
                    sender.push(
                        &to,
                        &payload,
                        &Value::Null,
                        priority,
                        "agents-working",
                        true,
                    )
                })
                .await;
                match result {
                    Ok(Outcome::Sent) => delivered = true,
                    Ok(Outcome::Dead(_)) => app.fleet.pushed(token, Pushed::Dead),
                    Ok(Outcome::Refused { reason, .. }) if reason == "PayloadTooLarge" => {}
                    _ => retry = true,
                }
            }
            if delivered {
                device.floor = 0;
                let _guard = self.writes.lock().await;
                if app
                    .fleet
                    .kept(REGISTER, owner.clone())
                    .await
                    .ok()
                    .flatten()
                    .as_ref()
                    == Some(&bytes)
                {
                    row["shown"] = json!(!end);
                    app.fleet
                        .keep_said(REGISTER, owner, Some(row.to_string().into_bytes()))
                        .await;
                }
            } else if retry {
                device.floor = (device.floor * 2).clamp(60_000, 1_800_000);
                device.paused = app.clock.now_ms() + device.floor;
                next = Some(next.map_or(device.paused, |n| n.min(device.paused)));
                continue;
            }
            device.pending = None;
        }
        next
    }
}

pub async fn run(app: App) {
    let desk = &app.android_chip;
    let mut devices = HashMap::new();
    let mut start = true;
    let mut due = Some(app.clock.now_ms() + 30_000);
    loop {
        let timer = async {
            match due {
                Some(at) => {
                    tokio::time::sleep(Duration::from_millis(
                        (at - app.clock.now_ms()).max(0) as u64
                    ))
                    .await
                }
                None => std::future::pending().await,
            }
        };
        tokio::select! { _=desk.wake.notified()=>{}, ()=timer=>{} }
        let offered = desk.latest.lock().is_ok_and(|p| p.is_some());
        if start && offered {
            start = false;
        }
        if start && due.is_some_and(|at| app.clock.now_ms() >= at) {
            start = false;
            if let Some(state) = app.chip.present(&app).await {
                desk.offer(&state, true);
            }
        }
        let pending = desk.flush(&app, &mut devices).await;
        if !start {
            due = pending;
        }
    }
}
