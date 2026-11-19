//! The Quota keeper at the Host's edge: the janitor job `quota-monitor`, which reads what is
//! left of the tools' accounts and tells the owner when a window runs low.
//!
//! The rule is the kernel's ([`clarp_kernel::janitors::quota`]): which windows count, when the
//! owner is told, the receipt of it and what is kept of each window. Here is what a Host
//! gives it, as the Python Host's lane does (`janitor_autonomy.quota_once`,
//! `deliver_notifications`):
//!
//! **When it looks.** In the janitors' own runner ([`crate::janitors::run`]), as a kind of
//! work that is due: when a look ends, the moment of the next is known and the runner is
//! armed for it, and a look of the runner before then costs nothing. A change of any janitor
//! disarms it: the keeper may have been turned on, off or set anew. A look runs beside the
//! runner, one at a time.
//!
//! **What it reads**: what `GET /backend-usage` answers, which asks a provider only on a Host
//! that was told to (`--ask-usage`).
//!
//! **What a look keeps**, in one write with its run in the janitors' book: what was last
//! seen of each window that moved (`quota-keeper-window`, one record a window, under the
//! name the Python Host gives its memory), its receipts (`quota-keeper-receipt`, one record
//! a receipt) and its lane (`quota-keeper-lane`: when it looks next). Then each receipt is
//! pushed as the Python Host pushes it (the alert of a finished turn), and what became of the
//! push is written on it.
//!
//! **Who tells the owner.** Where an automatic Hotseat switcher is on, it tells of Claude and
//! of Codex itself: such a receipt is kept, cancelled, as there. A receipt kept for a keeper
//! that was set anew before it went out is cancelled, as there. On a Host beside another
//! (`--accounts follow`), the other tells the owner: a receipt is kept and marked as not
//! delivered, and nothing is pushed. A recovery is asked of no runtime here yet: a window
//! with nothing left says what the Python Host says where none runs.
//!
//! **What is let go**: a receipt delivered or cancelled thirty days ago (the Python Host keeps
//! every one for ever). One that waits to be delivered stays.

use std::collections::BTreeMap;
use std::sync::Arc;

use clarp_kernel::janitors::book::Book;
use clarp_kernel::janitors::quota::{self, Lane, Memory, Owner, ROLE};
use clarp_kernel::janitors::{SEATS_JOB, adaptive, option_values};
use clarp_kernel::recovery::Accounts;
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};

use crate::edge::App;
use crate::janitors::{Gone, Row};
use crate::pusher::TurnDone;

/// The kinds of thing the keeper leaves with the fleet.
const WINDOWS: &str = "quota-keeper-window";
const RECEIPTS: &str = "quota-keeper-receipt";
const LANE: &str = "quota-keeper-lane";
/// The one lane there is.
const THE_LANE: &str = "quota-keeper";
/// How long a receipt delivered or cancelled is kept.
const SETTLED_KEPT_MS: i64 = 30 * 86_400_000;
/// What a receipt says on a Host beside another, which tells the owner.
pub const BESIDE: &str = "not delivered: the Host beside this one tells the owner";
/// What a run that could not begin says.
const COULD_NOT_START: &str = "The check could not start";

/// A receipt of a crossing: the run it was made in, what the phones are told, and what
/// became of it (nothing while it waits to be delivered).
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(default)]
struct Receipt {
    receipt_id: String,
    run_id: String,
    payload: Value,
    delivery: Option<Value>,
    created_at: i64,
    /// When what became of it was written.
    settled_at: Option<i64>,
}

/// The Quota keeper of this Host.
#[derive(Default)]
pub struct Desk {
    /// When a look is due next: known once a look ended. Nothing: the next look of the
    /// runner finds out.
    armed: std::sync::Mutex<Option<i64>>,
    /// One look at a time.
    looking: Arc<tokio::sync::Mutex<()>>,
    /// The receipts, by their ids: read where they are kept by the first look.
    receipts: tokio::sync::Mutex<Option<BTreeMap<String, Receipt>>>,
}

impl Desk {
    /// The keeper may have been turned on, off or set anew: the next look finds out.
    pub(crate) fn disarm(&self) {
        if let Ok(mut armed) = self.armed.lock() {
            *armed = None;
        }
    }

    fn arm(&self, at: i64) {
        if let Ok(mut armed) = self.armed.lock() {
            *armed = Some(at);
        }
    }

    fn due(&self, now_ms: i64) -> bool {
        self.armed
            .lock()
            .map_or(true, |armed| armed.is_none_or(|at| now_ms >= at))
    }
}

/// The keeper that is on, as a look reads it.
struct Keeper {
    agent_id: String,
    session: String,
    name: String,
    generation: u64,
    options: Map<String, Value>,
}

impl Keeper {
    fn number(&self, key: &str, otherwise: i64) -> i64 {
        self.options
            .get(key)
            .and_then(Value::as_i64)
            .unwrap_or(otherwise)
    }

    /// The interval the keeper was set to, and the longest it may grow to.
    fn bounds(&self) -> (i64, i64) {
        (
            self.number("interval_seconds", 300),
            self.number("max_interval_seconds", 0),
        )
    }

    fn mode(&self) -> &str {
        let mode = self.options.get("recovery_mode").and_then(Value::as_str);
        mode.unwrap_or("notify")
    }
}

/// The one keeper that is on and watches every agent, as the Python Host resolves it.
fn keeper_of(book: &Book, world: &dyn clarp_kernel::janitors::book::World) -> Option<Keeper> {
    let agent_id = book.resolve(ROLE, None, world).ok()??;
    let config = book.configs.get(&agent_id)?;
    let agent = world.agent(&agent_id)?;
    let kept = Value::Object(config.options.clone());
    let options = option_values(ROLE, Some(&kept)).ok()?;
    Some(Keeper {
        agent_id,
        session: agent.session,
        name: agent.name,
        generation: config.generation,
        options,
    })
}

/// Whether an automatic Hotseat switcher is on: it tells of Claude and of Codex itself.
fn switcher_tells(book: &Book, world: &dyn clarp_kernel::janitors::book::World) -> bool {
    let Some(id) = book.resolve(SEATS_JOB, None, world).ok().flatten() else {
        return false;
    };
    let kept = book
        .configs
        .get(&id)
        .map(|config| Value::Object(config.options.clone()));
    let options = option_values(SEATS_JOB, kept.as_ref()).ok();
    options.is_some_and(|options| options.get("mode") == Some(&json!("automatic")))
}

/// What is remembered of the windows a look reads, and which of them it changed.
#[derive(Default)]
struct Windows {
    kept: BTreeMap<String, Value>,
    changed: Vec<String>,
}

impl Memory for Windows {
    fn get(&self, key: &str) -> Option<Value> {
        self.kept.get(key).cloned()
    }
    fn set(&mut self, key: &str, kept: Value) {
        self.kept.insert(key.to_string(), kept);
        if !self.changed.iter().any(|changed| changed == key) {
            self.changed.push(key.to_string());
        }
    }
}

fn row<T: Serialize>(kind: &'static str, key: &str, value: &T) -> Row {
    (
        kind,
        key.to_string(),
        serde_json::to_vec(value).unwrap_or_default(),
    )
}

/// The runner's look: where a look of the keeper is due, it is begun beside the runner,
/// unless one is under way.
pub(crate) fn look(app: &App) {
    let desk = &app.quota_keeper;
    if !desk.due(app.clock.now_ms()) {
        return;
    }
    let Ok(one) = desk.looking.clone().try_lock_owned() else {
        return;
    };
    let app = app.clone();
    tokio::spawn(async move {
        pass(&app).await;
        drop(one);
    });
}

/// One look of the keeper, where one is due.
async fn pass(app: &App) {
    let desk = &app.quota_keeper;
    let keeper = app
        .janitors
        .read(app, |book, world, _| keeper_of(book, world));
    let Some(keeper) = keeper.await.ok().flatten() else {
        // Nobody keeps: nothing is due until a janitor changes.
        desk.arm(i64::MAX);
        return;
    };
    let mut receipts = desk.receipts.lock().await;
    if receipts.is_none() {
        let Ok(kept) = app.fleet.kept_all(RECEIPTS).await else {
            return;
        };
        let read = kept.iter().filter_map(|(id, written)| {
            let receipt: Receipt = serde_json::from_slice(written).ok()?;
            Some((id.clone(), receipt))
        });
        *receipts = Some(read.collect());
    }
    let Some(receipts) = receipts.as_mut() else {
        return;
    };
    deliver(app, &keeper, receipts).await;
    let lane = app.fleet.kept(LANE, THE_LANE.to_string()).await;
    let Ok(lane) = lane else {
        return;
    };
    let lane = lane.and_then(|kept| serde_json::from_slice::<Lane>(&kept).ok());
    let mut lane = Lane::of(lane, &keeper.agent_id, keeper.generation);
    let (base, longest) = keeper.bounds();
    let now = app.clock.now_ms();
    let next = lane.next_run(base);
    if now < next {
        desk.arm(next);
        return;
    }
    lane.start((base, longest), now);
    let usage = crate::usage::view(app, false).await;
    // What is remembered of each window the reading names, under its name and its old one.
    let mut windows = Windows::default();
    for (provider, said) in usage["providers"].as_object().into_iter().flatten() {
        let account = said.get("provider_instance_id").cloned();
        let account = account.unwrap_or_else(|| json!(provider));
        for window in said["windows"].as_array().into_iter().flatten() {
            let names = [
                quota::window_key(provider, &account, window),
                quota::old_key(provider, &account, window),
            ];
            for name in names {
                if windows.kept.contains_key(&name) {
                    continue;
                }
                let Ok(kept) = app.fleet.kept(WINDOWS, name.clone()).await else {
                    return;
                };
                if let Some(kept) = kept.and_then(|kept| serde_json::from_slice(&kept).ok()) {
                    windows.kept.insert(name, kept);
                }
            }
        }
    }
    let accounts = app.config.accounts;
    let looked = app.janitors.with_kept(app, |book, world, at| {
        let mut rows = Vec::new();
        let mut gone = Vec::new();
        // The keeper is the same as when the look began.
        let same = keeper_of(book, world).is_some_and(|now| {
            now.agent_id == keeper.agent_id && now.generation == keeper.generation
        });
        if !same {
            return (None, rows, gone);
        }
        let request = json!(quota::digest(&json!([keeper.agent_id, now])));
        let context = json!({ "input_hash": quota::digest(&usage) });
        let begun = book.begin_run_of(
            Some(keeper.agent_id.clone()),
            ROLE,
            &request,
            Some(&context),
            None,
            world,
            at,
        );
        let run_id = begun.ok().flatten().and_then(|run| {
            let run_id = run["run_id"].as_str()?.to_string();
            book.claim_run(&run_id, world, at).then_some(run_id)
        });
        let Some(run_id) = run_id else {
            let failed = (adaptive::FAILED, COULD_NOT_START);
            lane.record((base, longest), failed, now);
            rows.push(row(LANE, THE_LANE, &lane));
            return (Some(lane.next_run(base)), rows, gone);
        };
        let owner = Owner {
            agent_id: &keeper.agent_id,
            session: &keeper.session,
            name: &keeper.name,
            generation: keeper.generation,
            threshold: keeper.number("remaining_threshold", 25) as f64,
            recovery_mode: keeper.mode(),
        };
        let notices = quota::looked(&usage, &owner, &mut windows);
        let switched = switcher_tells(book, world);
        let (mut told, mut recoveries) = (0, Vec::new());
        for notice in notices {
            if receipts.contains_key(&notice.receipt_id) {
                continue;
            }
            let mut payload = notice.payload;
            let recovery = quota::recovery(&notice.provider, keeper.mode(), notice.spent, accounts);
            if let Some(recovery) = recovery {
                let status = recovery["status"].as_str().unwrap_or("unknown");
                recoveries.push(format!("{}: {status}", notice.provider));
                payload["recovery"] = recovery;
            }
            let suppressed = switched && quota::SWITCHED.contains(&notice.provider.as_str());
            // Where the switcher tells, the receipt is cancelled as it is kept; any other
            // waits to be delivered (or marked, beside another Host), below.
            let delivery = suppressed.then(|| json!({ "cancelled": quota::SWITCHER_TELLS }));
            let receipt = Receipt {
                receipt_id: notice.receipt_id.clone(),
                run_id: run_id.clone(),
                payload,
                settled_at: delivery.as_ref().map(|_| now),
                delivery,
                created_at: now,
            };
            rows.push(row(RECEIPTS, &receipt.receipt_id, &receipt));
            receipts.insert(notice.receipt_id, receipt);
            told += usize::from(!suppressed);
        }
        for key in &windows.changed {
            if let Some(kept) = windows.kept.get(key) {
                rows.push(row(WINDOWS, key, kept));
            }
        }
        let (activity, summary, result) = quota::ran(told, &recoveries);
        let done = Some((activity, summary.as_str()));
        let _ = book.complete_run(&run_id, "completed", Some(&result), "", done, world, at);
        lane.record((base, longest), (activity, &summary), now);
        rows.push(row(LANE, THE_LANE, &lane));
        // What was settled thirty days ago is let go.
        let old = receipts.iter().filter(|(_, receipt)| {
            receipt
                .settled_at
                .is_some_and(|at| at + SETTLED_KEPT_MS <= now)
        });
        let old: Vec<String> = old.map(|(id, _)| id.clone()).collect();
        for id in old {
            receipts.remove(&id);
            gone.push((RECEIPTS, id));
        }
        (Some(lane.next_run(base)), rows, gone)
    });
    let Ok(next) = looked.await else {
        return;
    };
    // The keeper changed while it looked: the next look finds out how it stands.
    let Some(next) = next else {
        return;
    };
    desk.arm(next);
    deliver(app, &keeper, receipts).await;
    crate::janitors::changed(app);
}

/// What waits to be delivered is pushed, or cancelled where its keeper was set anew since,
/// and what became of each is written on it, in one write.
async fn deliver(app: &App, keeper: &Keeper, receipts: &mut BTreeMap<String, Receipt>) {
    let mut waiting: Vec<&mut Receipt> = receipts
        .values_mut()
        .filter(|receipt| receipt.delivery.is_none())
        .filter(|receipt| receipt.payload["agent_id"] == json!(keeper.agent_id))
        .collect();
    if waiting.is_empty() {
        return;
    }
    waiting.sort_by(|a, b| (a.created_at, &a.receipt_id).cmp(&(b.created_at, &b.receipt_id)));
    let now = app.clock.now_ms();
    let mut rows = Vec::new();
    for receipt in waiting {
        let delivery = if receipt.payload["owner_generation"] != json!(keeper.generation) {
            json!({ "cancelled": quota::SET_ANEW })
        } else if app.config.accounts == Accounts::Follow {
            json!({ "cancelled": BESIDE })
        } else {
            let said = |key: &str| {
                receipt.payload[key]
                    .as_str()
                    .unwrap_or_default()
                    .to_string()
            };
            let done = TurnDone {
                badge: false,
                unread: false,
                session: said("session"),
                persona: said("persona"),
                notification_id: said("notification_id"),
                preview: said("preview"),
                needs_response: false,
            };
            let rang = app.pusher.turn_done_and_told(done).await;
            rang.ok()
                .and_then(|rang| rang.alert.map(|counted| counted.told()))
                .unwrap_or_else(|| json!({}))
        };
        receipt.delivery = Some(delivery);
        receipt.settled_at = Some(now);
        rows.push(row(RECEIPTS, &receipt.receipt_id, &*receipt));
    }
    let gone: Vec<Gone> = Vec::new();
    if let Err(why) = app.fleet.change_together(rows, gone).await {
        eprintln!(
            "clarp-host: the Quota keeper's receipts were not kept: {}",
            why.0
        );
    }
}
