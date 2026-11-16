//! The "agents working" chip at the Host's edge: what a phone shows of the fleet on its
//! lock screen, kept current by pushes.
//!
//! The rules are in [`clarp_kernel::chip`]. Here is where they are fed and where what they
//! say goes. One task follows what every agent's turn shows (the hub's feed of statuses)
//! and what the apps' badge counts, asks the rules when the chip is to be told again, and
//! sends it to every activity a phone left: through the gate that keeps the chip to its
//! share of the push service's budget, one push after another, so that an older state never
//! lands after a newer one and an alert is never starved.
//!
//! An activity's token is reached with the Host's own account at the relay: a phone gives
//! no grant for it. A Host without a relay and an account there keeps the register, and
//! says nothing of the chip in what it can do.
//!
//! The routes answer as the Python Host's do (`server/server.py`,
//! `_handle_live_activity_register`; `docs/live-items.md` §8).

use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;

use axum::body::Bytes;
use axum::extract::{Query, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::post;
use axum::{Extension, Json, Router};
use clarp_kernel::chip::{
    self, Activity, Attention, Budget, Fleet, Left, Pacer, Register, Shown, Spent,
};
use serde_json::{Value, json};
use tokio::sync::broadcast::error::RecvError;
use tokio::sync::mpsc::{UnboundedReceiver, UnboundedSender};

use crate::agent::Unavailable;
use crate::edge::{ApiError, App, Principal};
use crate::pairing::Scope;
use crate::relay::{Limited, Outcome, Push, Relay};

/// The kind of thing the chip keeps with the fleet, and the names of its parts.
const KIND: &str = "chip";
const ACTIVITIES: &str = "activities";
const DAY: &str = "day";
/// How old the badge's count may be when the chip is looked at.
const BADGE_EVERY_MS: i64 = 5_000;

/// What a push of the chip carries through the gate: its payload, and how urgent it is.
type Carried = (Value, &'static str);

struct Held {
    pacer: Pacer,
    register: Register,
    budget: Budget<Carried>,
    badge_at_ms: i64,
}

/// The chip of this Host.
pub struct Chip {
    /// `None` until what is kept was read.
    held: tokio::sync::Mutex<Option<Held>>,
    /// The way to the phones, when this Host has one.
    relay: Option<Arc<Relay>>,
    /// Wakes the task that paces when something it sleeps on changed.
    nudge: tokio::sync::Notify,
    /// Where the relay tells of its limits: held here too, so that it stays open on a Host
    /// that has no relay.
    _limits: UnboundedSender<Limited>,
}

/// What the Host knows of every agent, as the chip's rules read it.
fn roster(app: &App) -> Fleet {
    let directory = app.fleet.directory();
    let agents = directory.members.iter().map(|member| {
        let record = &member.record;
        let helper_of = match (&record.parent_agent_id, record.is_helper()) {
            (Some(parent), true) => parent.clone(),
            _ => String::new(),
        };
        let shown = Shown {
            session: record.session.clone(),
            persona: record.persona.clone(),
            helper_of,
            archived: record.archived_at_ms.is_some(),
            janitor: false,
            activity: Value::Null,
        };
        (record.agent_id.clone(), shown)
    });
    agents.collect()
}

fn seconds(now_ms: i64) -> f64 {
    now_ms as f64 / 1000.0
}

impl Chip {
    pub fn new(relay: Option<Arc<Relay>>, limits: UnboundedSender<Limited>) -> Self {
        Self {
            held: tokio::sync::Mutex::new(None),
            relay,
            nudge: tokio::sync::Notify::new(),
            _limits: limits,
        }
    }

    /// Whether this Host can keep a phone's chip current: it has a relay, and an account
    /// of its own there.
    pub fn pushes(&self) -> bool {
        self.relay
            .as_ref()
            .is_some_and(|relay| relay.has_credential())
    }

    async fn read<'a>(held: &'a mut Option<Held>, app: &App) -> Result<&'a mut Held, Unavailable> {
        if held.is_none() {
            let kept: HashMap<String, Vec<u8>> =
                app.fleet.kept_all(KIND).await?.into_iter().collect();
            let activities: Vec<Activity> = kept
                .get(ACTIVITIES)
                .and_then(|kept| serde_json::from_slice(kept).ok())
                .unwrap_or_default();
            *held = Some(Held {
                pacer: Pacer::default(),
                register: Register::kept(activities),
                budget: Budget::new(Spent::kept(kept.get(DAY).map(Vec::as_slice))),
                badge_at_ms: 0,
            });
        }
        held.as_mut()
            .ok_or_else(|| Unavailable("the chip could not be read".to_string()))
    }

    async fn keep_register(app: &App, register: &Register) -> Result<(), Unavailable> {
        let activities: Vec<&Activity> = register.activities().collect();
        let kept = serde_json::to_vec(&activities).unwrap_or_default();
        app.fleet
            .keep(KIND, ACTIVITIES.to_string(), Some(kept))
            .await
    }

    async fn keep_day(app: &App, budget: &Budget<Carried>) {
        if let Some(day) = budget.spent().day() {
            let kept = serde_json::to_vec(day).unwrap_or_default();
            let _ = app.fleet.keep(KIND, DAY.to_string(), Some(kept)).await;
        }
    }

    /// A phone leaves an activity. `Ok(Err)`: why not, in words.
    async fn register(
        &self,
        app: &App,
        left: &Left<'_>,
    ) -> Result<Result<String, &'static str>, Unavailable> {
        let mut guard = self.held.lock().await;
        let held = Self::read(&mut guard, app).await?;
        let activity_id = match held.register.register(left, app.clock.now_ms()) {
            Ok(activity) => activity.activity_id.clone(),
            Err(why) => return Ok(Err(why)),
        };
        Self::keep_register(app, &held.register).await?;
        Ok(Ok(activity_id))
    }

    /// An activity ended on the phone. Answers how many were taken off the register.
    async fn unregister(
        &self,
        app: &App,
        activity_id: &str,
        token: &str,
    ) -> Result<usize, Unavailable> {
        let mut guard = self.held.lock().await;
        let held = Self::read(&mut guard, app).await?;
        let removed = held.register.unregister(activity_id, token);
        if removed > 0 {
            Self::keep_register(app, &held.register).await?;
        }
        Ok(removed)
    }

    /// The tokens the chip goes to now: every activity whose phone may still be told.
    fn tokens(app: &App, register: &Register) -> Vec<String> {
        let directory = app.fleet.directory();
        let reached = register
            .activities()
            .filter(|activity| directory.can_receive(&activity.principal));
        reached.map(|activity| activity.token.clone()).collect()
    }

    /// A state of the chip is to be told: it waits at the gate for every activity there is.
    fn offer(&self, app: &App, held: &mut Held, push: &chip::Push, now_ms: i64) {
        if !self.pushes() {
            return;
        }
        let tokens = Self::tokens(app, &held.register);
        let carried = (chip::payload(&push.state, now_ms), push.priority);
        held.budget.offer(&tokens, &carried);
    }

    /// The badge is counted again when it was counted a while ago, or because it changed.
    fn count_badge(&self, app: &App, held: &mut Held, roster: &Fleet, now_ms: i64, changed: bool) {
        if !changed && now_ms - held.badge_at_ms < BADGE_EVERY_MS {
            return;
        }
        held.badge_at_ms = now_ms;
        let items = crate::library::badge(app);
        let attention = Attention {
            count: i64::try_from(items.len()).unwrap_or(i64::MAX),
            agent_ids: items
                .iter()
                .filter(|agent| !agent.is_empty())
                .cloned()
                .collect(),
            items: Some(items),
        };
        if let Some(push) = held.pacer.attention_is(attention, roster, now_ms) {
            self.offer(app, held, &push, now_ms);
        }
    }

    /// What an agent's turn shows changed.
    async fn observed(&self, app: &App, event: &Value) {
        let mut guard = self.held.lock().await;
        let Ok(held) = Self::read(&mut guard, app).await else {
            return;
        };
        let (roster, now_ms) = (roster(app), app.clock.now_ms());
        self.count_badge(app, held, &roster, now_ms, false);
        if let Some(push) = held.pacer.observe(event, &roster, now_ms) {
            self.offer(app, held, &push, now_ms);
        }
    }

    /// What the badge counts changed.
    async fn badge_changed(&self, app: &App) {
        let mut guard = self.held.lock().await;
        let Ok(held) = Self::read(&mut guard, app).await else {
            return;
        };
        let (roster, now_ms) = (roster(app), app.clock.now_ms());
        self.count_badge(app, held, &roster, now_ms, true);
    }

    /// How long until something that was held back is to be looked at: a state of the chip
    /// by its pace, a push by the budget.
    async fn sleeps(&self, app: &App) -> Option<Duration> {
        let mut guard = self.held.lock().await;
        let held = Self::read(&mut guard, app).await.ok()?;
        let now_ms = app.clock.now_ms();
        let paced = held.pacer.wake_at().map(|wake| wake - now_ms);
        let budgeted = held
            .budget
            .wake_at()
            .map(|wake| ((wake - seconds(now_ms)) * 1000.0).ceil() as i64);
        let soonest = [paced, budgeted].into_iter().flatten().min()?;
        Some(Duration::from_millis(u64::try_from(soonest).unwrap_or(0)))
    }

    /// The time something was held back until has come.
    async fn woken(&self, app: &App) {
        let mut guard = self.held.lock().await;
        let Ok(held) = Self::read(&mut guard, app).await else {
            return;
        };
        let (roster, now_ms) = (roster(app), app.clock.now_ms());
        if held.pacer.wake_at().is_some_and(|wake| wake <= now_ms) {
            self.count_badge(app, held, &roster, now_ms, false);
            if let Some(push) = held.pacer.woken(&roster, now_ms) {
                self.offer(app, held, &push, now_ms);
            }
        }
        if held
            .budget
            .wake_at()
            .is_some_and(|wake| wake <= seconds(now_ms))
        {
            held.budget.woken();
        }
    }

    /// The push service said "too many", to the chip or to an alert.
    async fn limited(&self, app: &App, said: Limited) {
        let mut guard = self.held.lock().await;
        let Ok(held) = Self::read(&mut guard, app).await else {
            return;
        };
        let mut changed = false;
        let now = seconds(app.clock.now_ms());
        held.budget
            .rate_limited(said.retry_after, said.whole_day, now, &mut changed);
        if changed {
            Self::keep_day(app, &held.budget).await;
        }
    }

    /// Sends what may go now, one push after another. What the push service does not take
    /// now is put back, the newest state for each token, and goes when it may.
    async fn flush(&self, app: &App, limited: &mut UnboundedReceiver<Limited>) {
        let Some(relay) = self.relay.clone().filter(|relay| relay.has_credential()) else {
            return;
        };
        let ready = {
            let mut guard = self.held.lock().await;
            let Ok(held) = Self::read(&mut guard, app).await else {
                return;
            };
            held.budget.due(seconds(app.clock.now_ms()))
        };
        if ready.is_empty() {
            return;
        }
        let (mut sent, mut failed, mut gone) = (0, 0, 0);
        let mut put_back = Vec::new();
        let mut ready = ready.into_iter();
        while let Some((token, (payload, priority))) = ready.next() {
            // Whether this one goes: the push service is not being left alone, and the
            // day's budget has it.
            let activity = {
                let mut guard = self.held.lock().await;
                let Ok(held) = Self::read(&mut guard, app).await else {
                    return;
                };
                let mut activities = held.register.activities();
                let Some(activity) = activities.find(|activity| activity.token == token).cloned()
                else {
                    // Taken back meanwhile: nothing to tell.
                    continue;
                };
                let now = seconds(app.clock.now_ms());
                let mut changed = false;
                let goes = !held.budget.paused(now) && held.budget.reserve(now, &mut changed);
                if changed {
                    Self::keep_day(app, &held.budget).await;
                }
                if !goes {
                    put_back.push((token, (payload, priority)));
                    put_back.extend(ready);
                    break;
                }
                activity
            };
            let outcome = {
                let relay = relay.clone();
                let (token, payload) = (token.clone(), payload.clone());
                let collapse_id = format!("live-activity-{}", activity.activity_id);
                let environment = activity.environment.clone();
                let sending = tokio::task::spawn_blocking(move || {
                    let push = Push {
                        payload: &payload,
                        collapse_id: &collapse_id,
                        push_type: "liveactivity",
                        priority,
                    };
                    relay.to_token(&token, &environment, &push)
                });
                let unsent = |_| Outcome::Down("the push was not sent".to_string());
                sending.await.unwrap_or_else(unsent)
            };
            let mut guard = self.held.lock().await;
            let Ok(held) = Self::read(&mut guard, app).await else {
                return;
            };
            let now = seconds(app.clock.now_ms());
            let mut changed = false;
            // What the relay said of being asked too much, of this push or of an alert.
            while let Ok(said) = limited.try_recv() {
                held.budget
                    .rate_limited(said.retry_after, said.whole_day, now, &mut changed);
            }
            match outcome {
                Outcome::Sent => {
                    sent += 1;
                    held.budget.delivered();
                }
                Outcome::Dead(_) => {
                    gone += 1;
                    held.register.unregister("", &token);
                    let _ = Self::keep_register(app, &held.register).await;
                }
                Outcome::Refused { status: 429, .. } => {
                    failed += 1;
                    if !held.budget.paused(now) {
                        held.budget.rate_limited(0.0, false, now, &mut changed);
                    }
                    // The state is kept, not lost: it goes when the push service takes it.
                    put_back.push((token, (payload, priority)));
                }
                _ => failed += 1,
            }
            if changed {
                Self::keep_day(app, &held.budget).await;
            }
        }
        if !put_back.is_empty() {
            let mut guard = self.held.lock().await;
            if let Ok(held) = Self::read(&mut guard, app).await {
                held.budget.requeue(put_back, seconds(app.clock.now_ms()));
            }
        }
        eprintln!("clarp-host: chip: sent {sent}, failed {failed}, gone {gone}");
    }
}

/// Starts the task that keeps the phones' chips current. `limited` is where the relay says
/// that it was asked too much.
pub fn run(app: &App, mut limited: UnboundedReceiver<Limited>) {
    let app = app.clone();
    tokio::spawn(async move {
        let chip = app.chip.clone();
        let mut statuses = app.hub.statuses();
        let mut badge = app.hub.badge();
        loop {
            let sleeps = chip.sleeps(&app).await;
            let held_back = async {
                match sleeps {
                    Some(sleeps) => tokio::time::sleep(sleeps).await,
                    None => std::future::pending().await,
                }
            };
            tokio::select! {
                status = statuses.recv() => match status {
                    Ok(event) => chip.observed(&app, &event).await,
                    // What was missed is told again by the next status of each agent.
                    Err(RecvError::Lagged(_)) => {}
                    Err(RecvError::Closed) => break,
                },
                changed = badge.changed() => match changed {
                    Ok(()) => chip.badge_changed(&app).await,
                    Err(_) => break,
                },
                Some(said) = limited.recv() => chip.limited(&app, said).await,
                () = held_back => chip.woken(&app).await,
                () = chip.nudge.notified() => {}
            }
            chip.flush(&app, &mut limited).await;
        }
    });
}

// ---- the routes ----------------------------------------------------------------------------

/// A field as the text the Python Host makes of it (`str(value or "")`).
fn text(said: &Value, key: &str) -> String {
    match &said[key] {
        Value::String(text) => text.clone(),
        Value::Number(number) => number.to_string(),
        _ => String::new(),
    }
}

/// The paired phone a registration belongs to: its pushes stop when the phone is taken off
/// the Host. Nobody's when the administrator registers it.
fn owner(principal: &Principal) -> String {
    match principal {
        Principal::Device {
            device_id,
            scope: Scope::Full,
        } => device_id.clone(),
        _ => String::new(),
    }
}

/// The chip for a phone of another kind is a paired phone's to ask for, for itself; and
/// this Host does not have it.
fn of_another_kind(principal: &Principal) -> ApiError {
    match principal {
        Principal::Device {
            scope: Scope::Full, ..
        } => ApiError::new(
            StatusCode::NOT_IMPLEMENTED,
            "The summary of agents working for a phone of this kind is not built on this Host yet",
        ),
        _ => ApiError::new(StatusCode::FORBIDDEN, "full device access required"),
    }
}

fn unavailable(why: Unavailable) -> Response {
    ApiError::from(why).into_response()
}

/// `POST /devices/live-activity` with `{token, activity_id, kind, environment}`: a phone
/// leaves the activity its chip is pushed to, or its new token; with `ended: true` it takes
/// it back.
async fn register(
    State(app): State<App>,
    Extension(principal): Extension<Principal>,
    bytes: Bytes,
) -> Response {
    let said: Value = if bytes.is_empty() {
        json!({})
    } else {
        serde_json::from_slice(&bytes).unwrap_or(Value::Null)
    };
    if !said.is_object() {
        return ApiError::new(StatusCode::BAD_REQUEST, "bad json").into_response();
    }
    if said["platform"] == "fcm" {
        return of_another_kind(&principal).into_response();
    }
    let chip = app.chip.clone();
    let (activity_id, token) = (text(&said, "activity_id"), text(&said, "token"));
    if said["ended"] == true {
        return match chip.unregister(&app, &activity_id, &token).await {
            Ok(removed) => Json(json!({ "ok": true, "removed": removed })).into_response(),
            Err(why) => unavailable(why),
        };
    }
    let (kind, environment, owner) = (
        text(&said, "kind"),
        text(&said, "environment"),
        owner(&principal),
    );
    let left = Left {
        token: &token,
        activity_id: &activity_id,
        kind: &kind,
        environment: &environment,
        principal: &owner,
    };
    let registered = chip.register(&app, &left).await;
    chip.nudge.notify_one();
    match registered {
        Ok(Ok(activity_id)) => {
            Json(json!({ "ok": true, "activity_id": activity_id })).into_response()
        }
        Ok(Err(why)) => ApiError::new(StatusCode::BAD_REQUEST, why).into_response(),
        Err(why) => unavailable(why),
    }
}

/// `DELETE /devices/live-activity` with `{activity_id}` or `{token}`, said in the request
/// or in its address: the activity ended on the phone.
async fn unregister(
    State(app): State<App>,
    Extension(principal): Extension<Principal>,
    Query(asked): Query<HashMap<String, String>>,
    bytes: Bytes,
) -> Response {
    let said: Value = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
    let named = |key: &str| -> String {
        match text(&said, key) {
            given if given.is_empty() => asked.get(key).cloned().unwrap_or_default(),
            given => given,
        }
    };
    if named("platform") == "fcm" {
        return of_another_kind(&principal).into_response();
    }
    let chip = app.chip.clone();
    match chip
        .unregister(&app, &named("activity_id"), &named("token"))
        .await
    {
        Ok(removed) => Json(json!({ "ok": true, "removed": removed })).into_response(),
        Err(why) => unavailable(why),
    }
}

pub fn routes() -> Router<App> {
    Router::new().route("/devices/live-activity", post(register).delete(unregister))
}
