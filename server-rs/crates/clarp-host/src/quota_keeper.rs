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
//! armed for it, beside the count of the janitors' book's writes it was armed at: a look of
//! the runner before then costs nothing, unless the book was written since (the keeper may
//! have been turned on, off or set anew; [`clarp_kernel::janitors::quota::due`]). A look runs
//! beside the runner, one at a time. What could not be read is said once in the Host's log,
//! by what failed.
//!
//! **What it reads**: what `GET /backend-usage` answers, which asks a provider only on a Host
//! that was told to (`--ask-usage`).
//!
//! **What a look keeps**, in one write with its run in the janitors' book: what was last
//! seen of each window that moved (`quota-keeper-window`, one record a window, under the
//! name the Python Host gives its memory), its receipts (`quota-keeper-receipt`, one record
//! a receipt) and its lane (`quota-keeper-lane`: when it looks next). Then each receipt is
//! pushed as the Python Host pushes it (the alert of a finished turn), and what became of the
//! push is written on it, as there (R105): a notice nobody got because the transport failed
//! (a relay that is down) waits, its attempts written on it, and is tried again past the
//! keeper's interval until a phone takes it, a newer crossing of its window replaces it, its
//! window resets or the keeper is set anew ([`quota::delivering`], [`quota::after_push`]). One
//! the pusher never answered (it is gone) waits too, as the Python Host leaves one whose push
//! raised.
//!
//! **Who tells the owner.** Where an automatic Hotseat switcher is on, it tells of Claude and
//! of Codex itself: such a receipt is kept, cancelled, as there. A receipt kept for a keeper
//! that was set anew before it went out is cancelled, as there. On a Host beside another
//! (`--accounts follow`), the other tells the owner: a receipt is kept and marked as not
//! delivered, and nothing is pushed.
//!
//! **Recovery**, at a window with nothing left, on a Host that keeps the logins itself
//! (`--accounts select`) and has the owner's selector for the tool, as the Python Host's
//! `request_recovery`, `recover_approved` and `runtime_recover`: where the keeper asks, a card
//! in its own session (made once the look is kept, its id written on the receipt); where it
//! recovers by itself, the tool's pool ([`crate::pool::Service::recover`]) takes up the first
//! turn it still owns as one that failed on its account. A card the owner accepted runs a
//! recovery at the keeper's next look, once it is under way or there was nothing to take up
//! (`quota-keeper-approval`, one record a card). Elsewhere nothing is asked, and the receipt
//! says so in the Python Host's words.
//!
//! **What is let go**: a receipt delivered or cancelled thirty days ago (the Python Host keeps
//! every one for ever). One that waits to be delivered stays.

use std::collections::BTreeMap;
use std::sync::Arc;

use clarp_kernel::janitors::book::Book;
use clarp_kernel::janitors::quota::{self, Deliver, Lane, Memory, Owner, ROLE};
use clarp_kernel::janitors::{SEATS_JOB, adaptive, option_values};
use clarp_kernel::recovery::Accounts;
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};

use crate::edge::App;
use crate::janitors::{Gone, Row};
use crate::pusher::TurnDone;

/// What this desk keeps, declared once (`kept.rs`; judged when read where it says so).
pub(crate) const KEPT: &[crate::kept::Kind] = &[
    // Kept for ever and listed by a later look, never at the start: judged when read. Of an
    // approval only that it is there is read.
    crate::kept::Kind::book(APPROVALS, crate::kept::Key::Plain, crate::kept::any).when_read(),
    crate::kept::Kind::book(LANE, crate::kept::Key::Plain, crate::kept::fits::<Lane>),
    crate::kept::Kind::book(
        RECEIPTS,
        crate::kept::Key::Plain,
        crate::kept::fits::<Receipt>,
    )
    .when_read(),
    crate::kept::Kind::book(WINDOWS, crate::kept::Key::Plain, crate::kept::json),
];

/// The kinds of thing the keeper leaves with the fleet.
const WINDOWS: &str = "quota-keeper-window";
const RECEIPTS: &str = "quota-keeper-receipt";
const LANE: &str = "quota-keeper-lane";
/// A card of the owner's that a recovery was run by, under its id: it is not run again.
const APPROVALS: &str = "quota-keeper-approval";
/// What a look writes in one: its run in the janitors' book, its windows, receipts and lane.
const LOOK: &str = "quota-keeper-look";
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
    /// The order receipts were written in, the first one: what waits is delivered in that
    /// order, as the Python Host reads its table. 0: one written before this was kept.
    order: u64,
    /// The tool the crossing was of: what a card for it asks about, where its card is asked
    /// again (`quota::unasked`). Empty: one written before this was kept.
    provider: String,
}

/// The Quota keeper of this Host.
#[derive(Default)]
pub struct Desk {
    /// When a look is due next, and the count of the janitors' book's writes it was armed at:
    /// known once a look ended. Nothing: the next look of the runner finds out.
    armed: std::sync::Mutex<Option<(i64, u64)>>,
    /// One look at a time.
    looking: Arc<tokio::sync::Mutex<()>>,
    /// The receipts, by their ids: read where they are kept by the first look.
    receipts: tokio::sync::Mutex<Option<BTreeMap<String, Receipt>>>,
}

impl Desk {
    fn arm(&self, at: i64, changes: u64) {
        if let Ok(mut armed) = self.armed.lock() {
            *armed = Some((at, changes));
        }
    }

    fn due(&self, now_ms: i64, changes: u64) -> bool {
        self.armed
            .lock()
            .map_or(true, |armed| quota::due(*armed, now_ms, changes))
    }
}

/// Name the failed read without the contents of its record. The fleet coalesces repeats.
fn failed(app: &App, what: &str, why: &crate::agent::Unavailable) {
    app.fleet.unread(ROLE, &format!("{what}: {}", why.0));
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
    /// The keeper as the kernel's rules read it.
    fn owner(&self) -> Owner<'_> {
        Owner {
            agent_id: &self.agent_id,
            session: &self.session,
            name: &self.name,
            generation: self.generation,
            threshold: self.number("remaining_threshold", 25) as f64,
            recovery_mode: self.mode(),
        }
    }

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
    if !desk.due(app.clock.now_ms(), app.janitors.changes()) {
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
    // What changes the book from here on is seen at the next look.
    let changes = app.janitors.changes();
    let keeper = app
        .janitors
        .read(app, |book, world, _| keeper_of(book, world));
    let keeper = match keeper.await {
        Ok(keeper) => keeper,
        Err(why) => return failed(app, "the janitors' book", &why),
    };
    let Some(keeper) = keeper else {
        // Nobody keeps: nothing is due until a janitor changes.
        desk.arm(i64::MAX, changes);
        return;
    };
    let mut receipts = desk.receipts.lock().await;
    if receipts.is_none() {
        let kept = match app.fleet.kept_listed(RECEIPTS).await {
            Ok(kept) => kept,
            Err(why) => return failed(app, "its receipts", &why),
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
    // A card that was not made is asked at every look, whether one is due or not.
    ask_cards(app, &keeper, receipts).await;
    let lane = match app.fleet.kept(LANE, THE_LANE.to_string()).await {
        Ok(lane) => lane,
        Err(why) => return failed(app, "its lane", &why),
    };
    let lane = lane.and_then(|kept| serde_json::from_slice::<Lane>(&kept).ok());
    let mut lane = Lane::of(lane, &keeper.agent_id, keeper.generation);
    let (base, longest) = keeper.bounds();
    let now = app.clock.now_ms();
    let next = lane.next_run(base);
    if now < next {
        desk.arm(next, changes);
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
                let kept = match app.fleet.kept(WINDOWS, name.clone()).await {
                    Ok(kept) => kept,
                    Err(why) => return failed(app, "what it remembers of a window", &why),
                };
                if let Some(kept) = kept.and_then(|kept| serde_json::from_slice(&kept).ok()) {
                    windows.kept.insert(name, kept);
                }
            }
        }
    }
    // The owner's answers to the keeper's cards, and which of them a recovery ran by already:
    // read only where a recovery can run (a Host that keeps the logins itself).
    let selects = app.config.accounts == Accounts::Select;
    let mut cards = if selects {
        crate::library::decisions_of(app, &keeper.session)
    } else {
        Vec::new()
    };
    cards.sort_by_key(|card| card["created_at"].as_i64().unwrap_or(0));
    // Read at once, where a card was accepted: one read of the kind, not one a card.
    let accepted = cards
        .iter()
        .any(|card| card["decision"]["resolved_choice"] == "accepted");
    let acted: std::collections::BTreeSet<String> = if accepted {
        match app.fleet.kept_listed(APPROVALS).await {
            Ok(kept) => kept.into_iter().map(|(id, _)| id).collect(),
            Err(why) => return failed(app, "the cards a recovery ran by", &why),
        }
    } else {
        std::collections::BTreeSet::new()
    };
    let pool_of = |tool: &str| app.pools.iter().find(|pool| pool.tool() == tool).cloned();
    let selector = |tool: &str| pool_of(tool).is_some_and(|pool| pool.has_selector());
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
        let owner = keeper.owner();
        // How the keeper stands, for a recovery that would run now.
        let config = book.configs.get(&keeper.agent_id);
        let standing = config.map(|config| quota::Standing {
            enabled: config.enabled,
            job: &config.template_id,
            generation: config.generation,
            mode: keeper.mode(),
        });
        let recover = |provider: &str, card: Option<&Value>| -> String {
            let frozen = (keeper.agent_id.as_str(), keeper.generation, provider);
            let may = quota::may_recover(standing, frozen, card, selector(provider));
            match may.map(|()| pool_of(provider).and_then(|pool| pool.recover())) {
                Ok(taken) => quota::recovered(taken).to_string(),
                Err(said) => said.to_string(),
            }
        };
        // The cards the owner accepted since: a recovery is run by each, once it is under
        // way or there was nothing to take up (`recover_approved`).
        let approvals = quota::approved(&cards, (&keeper.agent_id, keeper.generation), |id| {
            acted.contains(id)
        });
        for (id, provider) in approvals {
            let card = cards.iter().find(|card| card["artifact_id"] == id);
            let status = recover(provider, card);
            if quota::settled(&status) {
                let acted = json!({ "status": status, "at": now });
                rows.push(row(APPROVALS, id, &acted));
            }
        }
        let notices = quota::looked(&usage, &owner, &mut windows);
        let mut order = receipts
            .values()
            .map(|receipt| receipt.order)
            .max()
            .unwrap_or(0);
        let switched = switcher_tells(book, world);
        let (mut told, mut recoveries) = (0, Vec::new());
        for notice in notices {
            if receipts.contains_key(&notice.receipt_id) {
                continue;
            }
            let mut payload = notice.payload;
            let provider = notice.provider.as_str();
            let asked = quota::asked(provider, keeper.mode(), notice.spent, selector(provider));
            let recovery = match asked {
                None => None,
                Some(quota::Asked::Recover) => Some(json!({ "status": recover(provider, None) })),
                // The card is asked once the look is kept; its id is written on the receipt then
                // (`ask_cards`).
                Some(quota::Asked::Card) => Some(json!({ "status": "awaiting_approval" })),
                Some(refused) => quota::refused(refused),
            };
            if let Some(recovery) = recovery {
                let status = recovery["status"].as_str().unwrap_or("unknown");
                recoveries.push(format!("{provider}: {status}"));
                payload["recovery"] = recovery;
            }
            let suppressed = switched && quota::SWITCHED.contains(&notice.provider.as_str());
            // Where the switcher tells, the receipt is cancelled as it is kept; any other
            // waits to be delivered (or marked, beside another Host), below.
            let delivery = suppressed.then(|| json!({ "cancelled": quota::SWITCHER_TELLS }));
            order += 1;
            let receipt = Receipt {
                order,
                receipt_id: notice.receipt_id.clone(),
                run_id: run_id.clone(),
                payload,
                settled_at: delivery.as_ref().map(|_| now),
                delivery,
                created_at: now,
                provider: notice.provider.clone(),
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
        // A run that answers a request is kept as completed while it may still act; one that
        // may not was cut off by the book (`Ok(false)`). One the book refused to complete
        // (what it would keep is beyond its bounds) is closed as failed: a run left running
        // keeps every later look of the keeper from beginning.
        match book.complete_run(&run_id, "completed", Some(&result), "", done, world, at) {
            Ok(true) => {}
            Ok(false) => eprintln!("clarp-host: the Quota keeper's run {run_id} was cut off"),
            Err(refused) => {
                let why = format!("its result could not be kept: {}", refused.code);
                let closed = book.complete_run(&run_id, "failed", None, &why, None, world, at);
                eprintln!(
                    "clarp-host: the Quota keeper's run {run_id} {why}; closed as failed{}",
                    if closed.is_ok() {
                        ""
                    } else {
                        ", and that was refused too"
                    }
                );
            }
        }
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
    let next = match looked.await {
        Ok(next) => next,
        // A write that failed is said as the Host says every one (`FleetHandle::unkept`).
        Err(why) => return app.fleet.unkept(LOOK, &why.0),
    };
    // The keeper changed while it looked: the next look finds out how it stands.
    let Some(next) = next else {
        return;
    };
    // Armed after its own write: only what changes the book from now on moves the count.
    desk.arm(next, app.janitors.changes());
    ask_cards(app, &keeper, receipts).await;
    deliver(app, &keeper, receipts).await;
    crate::janitors::changed(app);
}

/// The cards the keeper asks its owner by (`request_recovery` where the keeper asks): one for
/// each of its receipts that waits for its card (`quota::unasked`: those of the look just kept,
/// and any whose card was not made before), each made in the keeper's session, and its id
/// written on the receipt it is for, in one write.
async fn ask_cards(app: &App, keeper: &Keeper, receipts: &mut BTreeMap<String, Receipt>) {
    let owner = keeper.owner();
    let mine = (keeper.agent_id.as_str(), keeper.generation);
    let mut waiting: Vec<&Receipt> = receipts
        .values()
        .filter(|receipt| !receipt.provider.is_empty() && quota::unasked(&receipt.payload, mine))
        .collect();
    waiting.sort_by_key(|receipt| (receipt.order, receipt.receipt_id.clone()));
    let asking: Vec<(String, Value)> = waiting
        .iter()
        .map(|receipt| {
            let card = quota::card(&receipt.provider, &owner, &receipt.receipt_id);
            (receipt.receipt_id.clone(), card)
        })
        .collect();
    if asking.is_empty() {
        return;
    }
    let Some(janitor) = app.fleet.directory().janitor(&keeper.agent_id).cloned() else {
        return;
    };
    // A card is one a receipt (its `reference_id`): one that was made and not named on its
    // receipt (the Host stopped between the two, or the write of its id failed) is named, never
    // asked a second time, which the book refuses as it is the reference's table's own key.
    let made_before: BTreeMap<String, Value> = crate::library::decisions_of(app, &keeper.session)
        .into_iter()
        .filter_map(|card| Some((card["reference_id"].as_str()?.to_string(), card)))
        .collect();
    let mut rows = Vec::new();
    for (receipt_id, card) in asking {
        let Some(fields) = card.as_object() else {
            continue;
        };
        let made = match made_before.get(&receipt_id) {
            Some(made) => Ok(made.clone()),
            None => crate::library::asked_of(app, &janitor, fields).await,
        };
        let made = match made {
            Ok(made) => made,
            Err(_) => {
                eprintln!(
                    "clarp-host: the Quota keeper's card for receipt {receipt_id} was not made; \
                     it is asked again at the next look"
                );
                continue;
            }
        };
        let Some(receipt) = receipts.get_mut(&receipt_id) else {
            continue;
        };
        receipt.payload["recovery"]["artifact_id"] = made["artifact_id"].clone();
        rows.push(row(RECEIPTS, &receipt_id, &*receipt));
    }
    if rows.is_empty() {
        return;
    }
    if let Err(why) = app.fleet.change_together(rows, Vec::new()).await {
        app.fleet.unkept(RECEIPTS, &why.0);
    }
}

/// What waits to be delivered is pushed, or cancelled where its keeper was set anew since,
/// and what became of each is written on it, in one write.
async fn deliver(app: &App, keeper: &Keeper, receipts: &mut BTreeMap<String, Receipt>) {
    let mut written: Vec<&Receipt> = receipts
        .values()
        .filter(|receipt| receipt.delivery.is_none())
        .collect();
    written.sort_by_key(|receipt| {
        (
            receipt.order,
            receipt.created_at,
            receipt.receipt_id.clone(),
        )
    });
    let waiting: Vec<quota::Waiting<'_>> = written
        .iter()
        .map(|receipt| {
            (
                receipt.receipt_id.as_str(),
                receipt.created_at,
                &receipt.payload,
            )
        })
        .collect();
    if waiting.is_empty() {
        return;
    }
    let now = app.clock.now_ms();
    let owner = (keeper.agent_id.as_str(), keeper.generation);
    let actions = quota::delivering(&waiting, owner, keeper.bounds().0, now);
    let mut rows = Vec::new();
    for (id, action) in actions {
        let Some(receipt) = receipts.get_mut(&id) else {
            continue;
        };
        let settled = match action {
            Deliver::Wait => continue,
            Deliver::Settle(settled) => Some(settled),
            // Beside another Host, the other tells the owner.
            Deliver::Push if app.config.accounts == Accounts::Follow => {
                Some(json!({ "cancelled": BESIDE }))
            }
            Deliver::Push => {
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
                // A pusher that never answers (it is gone) leaves the receipt waiting, to be
                // tried at the next look, as the Python Host leaves one whose push raised.
                let Ok(rang) = app.pusher.turn_done_and_told(done).await else {
                    continue;
                };
                let told = rang
                    .alert
                    .map_or_else(|| json!({}), |counted| counted.told());
                // One nobody got because the transport failed waits, its attempt written on it,
                // and is tried again past the keeper's interval (R105).
                quota::after_push(&mut receipt.payload, told, now)
            }
        };
        if let Some(settled) = settled {
            receipt.delivery = Some(settled);
            receipt.settled_at = Some(now);
        }
        rows.push(row(RECEIPTS, &receipt.receipt_id, &*receipt));
    }
    let gone: Vec<Gone> = Vec::new();
    if let Err(why) = app.fleet.change_together(rows, gone).await {
        app.fleet.unkept(RECEIPTS, &why.0);
    }
}
