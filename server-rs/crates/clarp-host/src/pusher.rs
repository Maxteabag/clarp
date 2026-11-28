//! Telling a phone that is not listening.
//!
//! A turn that ended with something for the user, and a request an agent makes of its user,
//! are told to the apps as events. A phone in a pocket hears no events: it is pushed to.
//! This is the thread that does it. Whoever has something to tell hands it over and goes on;
//! nothing an agent does ever waits for a relay.
//!
//! What is sent, to whom and when not is the Python Host's (`server/lib/apns.py`:
//! `send_user_notification`, `send_decision_created`): to every live row of the register
//! whose owner may still receive, not while the user sits at a desk with the app open, and a
//! token the push service calls dead is not sent to again.
//!
//! A ring that begins to ring is first pushed as an incoming call to the phones that left a
//! token for one, by the Host itself, where its owner gave it a key at the push service
//! ([`crate::direct`]); the alert follows only where that reached no phone, and never for a
//! ring held as a live voice call (`agent_rings._deliver`).

use std::collections::VecDeque;
use std::sync::{Arc, Mutex, mpsc};
use std::time::Duration;

use tokio::sync::watch;
use tokio::time::Instant;

use clarp_kernel::alert_routes::{Path, Routes};
use clarp_kernel::devices::PushRow;
use clarp_kernel::notify_events::{Event, Loc, Settings};
use clarp_kernel::presence::Presence;
use clarp_kernel::push::{self, Asked, Told};
use clarp_ports::clock::Clock;
use serde_json::Value;

mod android_lanes;

use crate::direct::Direct;
use crate::fleet::{FleetHandle, Pushed};
use crate::relay::{Outcome, Push, Relay};

/// What a push shows of what an agent said: its words, or, for an agent bound to a model
/// provider of its own, only that it answered. A push goes through the phone maker's
/// service, and that agent's words go to none.
pub fn preview_of(record: &crate::fleet::AgentRecord, said: &str) -> String {
    if record.bound() {
        format!("{} answered", display_name(record))
    } else {
        said.to_string()
    }
}

/// The actions of a reply's notification: answer from the banner, play it.
pub const REPLY_CATEGORY: &str = "agent-turn";
/// The actions of a failed turn's notification: try again, open.
pub const FAILED_CATEGORY: &str = "turn-failed";

/// The key of a reply's banner where the Host wrote it ([`preview_of`]): of a bound agent.
pub fn preview_loc(record: &crate::fleet::AgentRecord) -> Option<Loc> {
    record
        .bound()
        .then(|| Loc::new("notify.body.answered", &[display_name(record)]))
}

/// The key a reply's or a note's banner is translated by, and what the push carries of its
/// preview line beside its English (`preview_parts`, from the Host's preview formatter, where
/// the event the apps were told has it). Of a bound agent only that it answered: its words go
/// to no push service.
pub fn preview_keys(
    record: &crate::fleet::AgentRecord,
    told: &Value,
) -> (Option<Loc>, serde_json::Map<String, Value>) {
    let mut extra = serde_json::Map::new();
    if let Some(answered) = preview_loc(record) {
        return (Some(answered), extra);
    }
    let parts = &told["preview_parts"];
    if !parts.is_null() {
        extra.insert("preview_parts".into(), parts.clone());
    }
    (clarp_kernel::notify_events::preview_loc(parts), extra)
}

/// What a bound agent's failed turn says on a phone.
const WITHHELD_FAILURE: &str = "notify.body.failed_withheld";

/// The push of a turn that failed, from what the apps were told of it
/// ([`crate::wire::failed_notification`]): the same key, arguments and English.
///
/// An agent bound to a model provider of its own sends none of its turn's words to a push
/// service, nor what they name (a working folder, the provider's message): its push says only
/// that the turn did not finish, and carries codes and ids, never the sentence or its arguments.
pub fn failed(record: &crate::fleet::AgentRecord, told: &Value) -> TurnDone {
    let title = Loc::new("notify.body.failed_title", &[display_name(record)]);
    let body = if record.bound() {
        Loc::new(WITHHELD_FAILURE, &[])
    } else {
        Loc {
            key: told["body_loc_key"]
                .as_str()
                .unwrap_or("error.unknown")
                .to_string(),
            args: told["body_loc_args"]
                .as_array()
                .cloned()
                .unwrap_or_default(),
        }
    };
    let preview = if record.bound() {
        body.english()
    } else {
        told["preview"].as_str().unwrap_or("").to_string()
    };
    let mut extra = serde_json::Map::new();
    for key in [
        "error_kind",
        "error_code",
        "turn_id",
        "time_ms",
        "primary_action",
    ] {
        if !told[key].is_null() {
            extra.insert(key.into(), told[key].clone());
        }
    }
    let message_id = told["cause_message_id"].as_str().unwrap_or("").to_string();
    TurnDone {
        badge: true,
        unread: true,
        session: record.session.clone(),
        persona: record.persona.clone(),
        notification_id: told["notification_id"].as_str().unwrap_or("").to_string(),
        preview,
        title: title.english(),
        event: Event::Failed,
        agent_id: record.agent_id.clone(),
        message_id,
        category: FAILED_CATEGORY.to_string(),
        title_loc: Some(title),
        body_loc: Some(body),
        extra,
        ..TurnDone::default()
    }
}

/// The push of a job that ended, from what the apps were told of it
/// ([`crate::wire::job_notification`]).
pub fn job_done(record: &crate::fleet::AgentRecord, told: &Value) -> TurnDone {
    let key = if told["job_status"] == "failed" {
        "notify.body.job_failed"
    } else {
        "notify.body.job_succeeded"
    };
    let arg = told["body_loc_args"][0].as_str().unwrap_or("");
    let body = Loc::new(key, &[arg]);
    let mut extra = serde_json::Map::new();
    extra.insert("job_id".into(), told["job_id"].clone());
    extra.insert("job_status".into(), told["job_status"].clone());
    TurnDone {
        session: record.session.clone(),
        persona: record.persona.clone(),
        notification_id: told["notification_id"].as_str().unwrap_or("").to_string(),
        preview: body.english(),
        event: Event::JobDone,
        agent_id: record.agent_id.clone(),
        body_loc: Some(body),
        extra,
        ..TurnDone::default()
    }
}

/// The name a neutral line of a bound agent gives it.
pub fn display_name(record: &crate::fleet::AgentRecord) -> &str {
    if record.persona.is_empty() {
        &record.session
    } else {
        &record.persona
    }
}

/// A finished turn the user is told of.
#[derive(Debug, Clone, Default)]
pub struct TurnDone {
    pub order: Order,
    pub badge: bool,
    pub unread: bool,
    pub session: String,
    pub persona: String,
    pub notification_id: String,
    /// What the agent said, as a notification shows it.
    pub preview: String,
    /// The agent still waits for an answer from the user: the push makes a sound.
    pub needs_response: bool,
    /// What the owner switches it by ([`clarp_kernel::notify_events`]).
    pub event: Event,
    pub agent_id: String,
    /// The row a tap opens.
    pub message_id: String,
    /// The actions the app offers on it. Empty: none.
    pub category: String,
    /// The banner's title where it is not the agent's name.
    pub title: String,
    /// The keys of what the Host wrote itself, where it did.
    pub title_loc: Option<Loc>,
    pub body_loc: Option<Loc>,
    /// Fields of its own the event adds (`error_kind`, `job_id`, ...).
    pub extra: serde_json::Map<String, Value>,
}

/// A request an agent just made of its user.
#[derive(Debug, Clone, Default)]
pub struct Requested {
    pub session: String,
    pub persona: String,
    pub title: String,
    pub question: String,
    pub decision_id: String,
    pub artifact_id: String,
    pub time_sensitive: bool,
    pub text_input: bool,
    pub revision: i64,
    pub expires_at: Option<i64>,
    pub agent_id: String,
    /// `approval`, `single_choice` or `text_input`.
    pub response_type: String,
    /// The choices of a `single_choice`, or the yes and no of an `approval`. A bound agent's
    /// choices are its words and go to no push service; its yes and no are Yes and No.
    pub options: Vec<Value>,
    /// The keys of what the Host wrote itself, where it did.
    pub title_loc: Option<Loc>,
    pub body_loc: Option<Loc>,
    /// It was held back while a desk was present, and is sent to the phones now, desk or no
    /// desk: its chat was not opened and it still waits ([`ESCALATE_AFTER`]).
    pub escalated: bool,
    /// When it was handed over, by the Host's clock: a chat opened after it saw it.
    pub order: Order,
}

/// How long a request the desk held back waits for its chat to be opened before the phones
/// are told after all. The desk is where the user is; a question nobody looked at there may
/// have caught them away from it.
pub const ESCALATE_AFTER: Duration = Duration::from_secs(180);

/// A ring of an agent as it is now, and what happened to it: `ring` for the incoming call,
/// then `answered`, `declined`, `missed` or `cancelled`.
#[derive(Debug, Clone)]
pub struct Rung {
    pub ring: clarp_kernel::rings::Ring,
    pub kind: &'static str,
    /// The owner lets a locked phone show why the agent calls.
    pub show_reason: bool,
    /// The phones that switched rings off for themselves: none of their rows is told.
    pub without: Vec<String>,
    /// The phones a ring that begins to ring is pushed to as an incoming call: the token
    /// each left for calls, and the side of the push service it belongs to.
    pub calls: Vec<(String, String)>,
    /// The ring is for a phone that left a token for calls, whether or not that phone is
    /// one that is pushed.
    pub calls_asked: bool,
    pub live_rows: Vec<(String, String)>,
}

/// What became of the pushes of one change of a ring, as the one who rang is told.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Rang {
    /// The pushes as incoming calls, for a ring that begins to ring (`delivery.voip`).
    pub calls: Option<Value>,
    /// The alert, where one followed.
    pub alert: Option<Counted>,
}

impl Rang {
    /// As the Python Host says it (`agent_rings._deliver`).
    pub fn told(&self) -> Value {
        let mut delivery = serde_json::json!({});
        if let Some(calls) = &self.calls {
            delivery["voip"] = calls.clone();
        }
        if let (Some(alert), Some(delivery)) = (&self.alert, delivery.as_object_mut())
            && let Value::Object(counted) = alert.told()
        {
            delivery.extend(counted);
        }
        delivery
    }
}

/// What became of a push, as the one who asked for it is told: whether this Host pushes at
/// all, and to how many phones it went, did not go, and will not go again.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct Counted {
    pub enabled: bool,
    pub sent: u32,
    pub failed: u32,
    pub disabled: u32,
}

impl Counted {
    /// As the Python Host says it (`apns.send_agent_ring`).
    pub fn told(&self) -> Value {
        serde_json::json!({
            "enabled": self.enabled, "sent": self.sent, "failed": self.failed,
            "disabled": self.disabled,
        })
    }
}

enum Job {
    /// A finished turn, and who waits to hear what became of its push.
    Turn(TurnDone, Option<tokio::sync::oneshot::Sender<Rang>>),
    Request(Requested),
    BadgeWake,
    /// A ring, and who waits to hear what became of its pushes.
    Ring(Box<Rung>, Option<tokio::sync::oneshot::Sender<Rang>>),
    /// A chat was read elsewhere: its notifications are taken down.
    Read(Read),
}

/// A chat read on some device, or a request in it that is answered: the silent push that
/// takes its notifications down on every phone.
#[derive(Debug, Clone, Default)]
pub struct Read {
    pub order: Order,
    pub session: String,
    /// The request that was answered, dismissed or withdrawn. Empty: the chat was read.
    pub decision_id: String,
}

/// Host observation order. Only compare wire values within one Host generation.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, PartialOrd, Ord)]
pub struct Order {
    at_ms: i64,
    seq: u64,
}

impl Order {
    pub fn observe(at_ms: i64) -> Self {
        use std::sync::atomic::{AtomicU64, Ordering};
        static SEQ: AtomicU64 = AtomicU64::new(1);
        Self {
            at_ms,
            seq: SEQ.fetch_add(1, Ordering::Relaxed),
        }
    }

    fn put(self, v: &mut Value, read: bool) {
        static EPOCH: std::sync::LazyLock<String> =
            std::sync::LazyLock::new(|| crate::ids::hex(16));
        if self.seq == 0 {
            return;
        }
        v["order_epoch"] = serde_json::json!(&*EPOCH);
        v[if read {
            "read_through_ms"
        } else {
            "event_at_ms"
        }] = self.at_ms.into();
        v[if read {
            "read_through_seq"
        } else {
            "event_seq"
        }] = self.seq.into();
    }

    pub fn event(self, v: &mut Value) {
        self.put(v, false);
    }
    pub fn boundary(self, v: &mut Value) {
        self.put(v, true);
    }
}

/// The way to the pusher's thread. Handing it something never waits.
#[derive(Clone)]
pub struct Pusher {
    live: mpsc::Sender<Rung>,
    tx: mpsc::Sender<Job>,
    badge_wake: watch::Sender<()>,
}

impl Pusher {
    pub fn turn_done(&self, told: TurnDone) {
        let _ = self.tx.send(Job::Turn(told, None));
    }

    /// The same, for one who is told what became of the push (`alert`): when every phone
    /// was tried. A pusher that is gone answers nothing.
    pub fn turn_done_and_told(&self, done: TurnDone) -> tokio::sync::oneshot::Receiver<Rang> {
        let (told, answer) = tokio::sync::oneshot::channel();
        let _ = self.tx.send(Job::Turn(done, Some(told)));
        answer
    }

    pub fn requested(&self, asked: Requested) {
        let _ = self.tx.send(Job::Request(asked));
    }

    /// A chat was read, or a request in it ended: the phones take its notifications down.
    pub fn read(&self, read: Read) {
        let _ = self.tx.send(Job::Read(read));
    }

    /// The phone's local unread list may count this chat differently now.
    pub(crate) fn refresh_badge(&self) {
        self.badge_wake.send_modify(|_| {});
    }

    fn android_ring(&self, ring: &Rung) {
        if !ring.live_rows.is_empty() {
            let _ = self.live.send(ring.clone());
        }
    }

    /// A ring changed: the owner's phones are told, whether or not anybody sits at a desk.
    /// An agent calling is the one interruption its user asked for.
    pub fn rung(&self, rung: Rung) {
        self.android_ring(&rung);
        let _ = self.tx.send(Job::Ring(Box::new(rung), None));
    }

    /// The same, for the one who placed the ring and is told what became of its push. The
    /// answer comes when every phone was tried; a pusher that is gone answers nothing.
    pub fn rung_and_told(&self, rung: Rung) -> tokio::sync::oneshot::Receiver<Rang> {
        let (told, answer) = tokio::sync::oneshot::channel();
        self.android_ring(&rung);
        let _ = self.tx.send(Job::Ring(Box::new(rung), Some(told)));
        answer
    }
}

/// What is handed to the pusher before its thread runs: kept, and sent once it does.
pub struct Waiting {
    live: mpsc::Receiver<Rung>,
    rx: mpsc::Receiver<Job>,
    badge_wake: watch::Receiver<()>,
    badge_jobs: mpsc::Sender<Job>,
}

/// A pusher, and what waits for its thread.
pub fn pusher() -> (Pusher, Waiting) {
    let (tx, rx) = mpsc::channel();
    let (live, waiting) = mpsc::channel();
    let (badge_wake, wakes) = watch::channel(());
    let badge_jobs = tx.clone();
    (
        Pusher {
            tx,
            live,
            badge_wake,
        },
        Waiting {
            rx,
            live: waiting,
            badge_wake: wakes,
            badge_jobs,
        },
    )
}

/// What the pusher's thread works with.
#[derive(Clone)]
pub struct Works {
    pub android: Arc<crate::fcm::Sender>,
    /// The way to the phones. `None`: this Host pushes nothing.
    pub relay: Option<Arc<Relay>>,
    pub fleet: FleetHandle,
    pub desks: Arc<Mutex<Presence>>,
    pub clock: Arc<dyn Clock + Send + Sync>,
    /// Which Host this is, for an app that knows several.
    pub server_id: String,
    /// The pictures this Host ships: the face a push shows of an agent with none of its own.
    pub faces: Arc<crate::faces::Faces>,
    /// What signs the address a phone fetches an agent's own picture by: the Host's own
    /// token. Empty on a Host that asks no token of anybody.
    pub secret: String,
    /// The way to the push service itself, for the pushes the relay does not carry.
    pub direct: Arc<Direct>,
    /// Where what is spoken over a connection of the Host's own is run.
    pub runtime: tokio::runtime::Handle,
}

impl Waiting {
    /// Starts the pusher's thread.
    pub fn run(self, works: Works) -> Result<(), String> {
        let escalations = self.badge_jobs.clone();
        works
            .runtime
            .spawn(badge_wakes(self.badge_wake, self.badge_jobs));
        let android = works.clone();
        std::thread::Builder::new()
            .name("android-live-rings".into())
            .spawn(move || live_rings(self.live, &android))
            .map_err(|e| e.to_string())?;
        std::thread::Builder::new()
            .name("pusher".to_string())
            .spawn(move || {
                let mut lanes = android_lanes::Lanes::default();
                let mut reads = Reads::default();
                let mut rings = RingAlerts::default();
                let works = Arc::new(works);
                for job in self.rx {
                    if let Job::Request(asked) = &job {
                        let now = works.clock.now_ms();
                        if asked.escalated {
                            // Its chat was opened since, or it was answered, changed or ran
                            // out: the desk had it, and the phones are not told.
                            if reads.opened_since(&asked.session, asked.order)
                                || !still_waits(&works, asked, now)
                            {
                                continue;
                            }
                        } else if desk_present(&works) {
                            // Held back for the desk, where it is shown: the phones are told
                            // later if nobody opens its chat ([`ESCALATE_AFTER`]).
                            escalate_later(&works, &escalations, asked);
                            continue;
                        }
                    }
                    if matches!(job, Job::BadgeWake) {
                        deliver(&works, &job, Targets::Apple);
                        continue;
                    }
                    match &job {
                        Job::Read(read) if !reads.due(read) => continue,
                        _ => {}
                    }
                    let (job, told) = match job {
                        Job::Ring(rung, told) => (Job::Ring(rung, None), told),
                        Job::Turn(done, told) => (Job::Turn(done, None), told),
                        other => (other, None),
                    };
                    if let Job::Ring(rung, _) = &job {
                        let on = calls_on(&works, &rung.ring.agent_id);
                        if !rings.pushed(rung.kind, &rung.ring.ring_id, on) {
                            if let Some(told) = told {
                                let _ = told.send(Rang::default());
                            }
                            continue;
                        }
                    }
                    let mut rang = Rang::default();
                    // A ring is pushed as an incoming call first, to the phones the desk names
                    // for it: the phones of a ring that begins to ring, and none for what
                    // becomes of one.
                    let mut reached = 0;
                    if let Job::Ring(rung, _) = &job {
                        let counted = called(&works, rung);
                        reached = counted["sent"].as_u64().unwrap_or(0);
                        rang.calls = Some(counted);
                    }
                    // The alert of a ring follows where no phone was rung as a call, and
                    // never for a ring held as a live voice call, in any state of it: a
                    // phone that cannot hold one must never be shown one.
                    let alerted = match &job {
                        Job::Ring(rung, _) => {
                            let mode = rung.ring.conversation_mode.as_str();
                            clarp_kernel::rings::alert_follows(mode, reached)
                        }
                        _ => true,
                    };
                    if alerted {
                        let android: Vec<_> = works
                            .fleet
                            .directory()
                            .push
                            .active()
                            .filter(|r| r.provider == "fcm")
                            .map(|r| (r.token.clone(), r.principal.clone()))
                            .collect();
                        let fcm = works.android.configured();
                        let mut count = deliver(&works, &job, Targets::Apple);
                        count.enabled |= fcm;
                        rang.alert = Some(count);
                        if !android.is_empty() && fcm {
                            lanes.submit(works.clone(), job, android, rang, told);
                            continue;
                        }
                    }
                    if let Some(told) = told {
                        let _ = told.send(rang);
                    }
                }
            })
            .map(|_| ())
            .map_err(|error| format!("the pusher's thread could not be started: {error}"))
    }
}

/// Whether a user sits at a desk with the app open now.
fn desk_present(works: &Works) -> bool {
    let directory = works.fleet.directory();
    let now = works.clock.now_ms();
    works
        .desks
        .lock()
        .is_ok_and(|desks| desks.active(now, &|owner| directory.can_receive(owner)))
}

/// Hands a request the desk holds back to the pusher again after [`ESCALATE_AFTER`], by a
/// timer of the Host's runtime. What it finds then decides: see the pusher's loop.
fn escalate_later(works: &Works, jobs: &mpsc::Sender<Job>, asked: &Requested) {
    let later = Requested {
        escalated: true,
        ..asked.clone()
    };
    let jobs = jobs.clone();
    works.runtime.spawn(async move {
        tokio::time::sleep(ESCALATE_AFTER).await;
        let _ = jobs.send(Job::Request(later));
    });
}

/// Whether a request held for the desk is still the one to push: it waits for the user, at the
/// revision it was asked at, and has not run out.
fn still_waits(works: &Works, asked: &Requested, now_ms: i64) -> bool {
    let directory = works.fleet.directory();
    let mut everyone = directory.members.iter().chain(directory.janitors.iter());
    everyone.any(|member| {
        let view = member.agent.view();
        view.books
            .artifacts
            .of_decision(&asked.decision_id)
            .and_then(|artifact| artifact.decision.as_ref())
            .is_some_and(|decision| {
                waits(
                    (&decision.status, decision.revision, decision.expires_at),
                    asked.revision,
                    now_ms,
                )
            })
    })
}

/// The rule of [`still_waits`]: pending, at the same revision, and not past its end.
fn waits(decision: (&str, i64, Option<i64>), asked_revision: i64, now_ms: i64) -> bool {
    let (status, revision, expires_at) = decision;
    status == "pending" && revision == asked_revision && expires_at.is_none_or(|at| at > now_ms)
}

/// Whether the owner lets this agent's rings reach the phones (`notify_events`, event `call`,
/// as the agent's own choice). The ring switch for every agent is held where a ring is placed.
fn calls_on(works: &Works, agent_id: &str) -> bool {
    let directory = works.fleet.directory();
    let settings = Settings::stored(directory.setting(clarp_kernel::notify_events::KEY));
    settings.alerts(Event::Call, agent_id, false, true)
}

/// Which changes of rings are pushed. A ring that begins to ring is pushed where calls of its
/// agent are on; what became of it (answered, declined, missed, cancelled) follows to the
/// phones, also when the switch changed since, unless the ring itself was held back. A ring
/// this Host never saw begin (one from before a restart) is told what became of it.
#[derive(Default)]
struct RingAlerts {
    /// The rings whose beginning was held back by the switch.
    withheld: std::collections::HashSet<String>,
}

impl RingAlerts {
    fn pushed(&mut self, kind: &str, ring_id: &str, calls_on: bool) -> bool {
        if kind != "ring" {
            return !self.withheld.contains(ring_id);
        }
        if calls_on {
            self.withheld.remove(ring_id);
        } else {
            self.withheld.insert(ring_id.to_string());
        }
        calls_on
    }
}

/// Which read pushes are worth sending. A chat read takes down everything shown of the chat:
/// once one was sent, the same push says nothing new until something is shown of that chat
/// again. A request's end takes down that request's notification only: each request's once,
/// beside any other, and it is not a reading of the chat.
#[derive(Default)]
struct Reads {
    /// The requests whose end was pushed.
    ended: std::collections::HashSet<String>,
    /// When each chat was last opened or read on some device.
    opened: std::collections::HashMap<String, Order>,
}

impl Reads {
    fn due(&mut self, read: &Read) -> bool {
        if read.decision_id.is_empty() {
            let previous = self.opened.entry(read.session.clone()).or_default();
            if read.order <= *previous {
                return false;
            }
            *previous = read.order;
            true
        } else {
            self.ended.insert(read.decision_id.clone())
        }
    }

    /// Whether the chat was opened or read at or after this moment.
    fn opened_since(&self, session: &str, order: Order) -> bool {
        self.opened
            .get(session)
            .is_some_and(|opened| *opened >= order)
    }
}

/// The wake reloads the whole roster, so one pending wake covers every chat. Give quick
/// transitions a moment to join and keep a trailing wake when the background budget is spent.
async fn badge_wakes(mut changes: watch::Receiver<()>, jobs: mpsc::Sender<Job>) {
    const HOUR: Duration = Duration::from_secs(3600);
    const SPACING: Duration = Duration::from_secs(60);
    const COALESCE: Duration = Duration::from_secs(2);
    let mut sent: VecDeque<Instant> = VecDeque::new();
    while changes.changed().await.is_ok() {
        let now = Instant::now();
        sent.retain(|when| now.duration_since(*when) < HOUR);
        let mut due = now + COALESCE;
        if let Some(last) = sent.back() {
            due = due.max(*last + SPACING);
        }
        if sent.len() == 3
            && let Some(first) = sent.front()
        {
            due = due.max(*first + HOUR);
        }
        tokio::time::sleep_until(due).await;
        drop(changes.borrow_and_update());
        if jobs.send(Job::BadgeWake).is_err() {
            break;
        }
        let now = Instant::now();
        sent.retain(|when| now.duration_since(*when) < HOUR);
        sent.push_back(now);
    }
}

/// Pushes a ring that begins to ring as an incoming call to the phones that are to have it,
/// one after the other, and says what became of it as the one who rang is told
/// (`apns.send_agent_ring_voip`). Without a key of the Host's own nothing is pushed.
fn called(works: &Works, rung: &Rung) -> Value {
    let key = works.direct.key();
    let Some(key) = key.filter(|_| rung.calls_asked) else {
        return push::calls_counted(false, &[]);
    };
    let ring = &rung.ring;
    let said = push::Rung {
        ring_id: &ring.ring_id,
        session: &ring.session,
        persona: &ring.persona,
        reason: &ring.reason,
        expires_at: ring.expires_at,
        show_reason: rung.show_reason,
        server_instance_id: &works.server_id,
        ..push::Rung::default()
    };
    let payload = push::call(&said, &ring.conversation_mode);
    let collapse_id = push::ring_notification_id(&ring.ring_id);
    let push = clarp_kernel::direct_push::Push {
        payload: &payload,
        push_type: "voip",
        priority: "10",
        collapse_id: &collapse_id,
        expiration: Some(push::call_expires(ring.expires_at)),
    };
    let mut answers = Vec::new();
    for (token, environment) in &rung.calls {
        let pushed = works.direct.push(&key, (token, environment), &push);
        let answer = match works.runtime.block_on(pushed) {
            Ok((status, reason, _)) => {
                if status != 200 {
                    let reason = reason.as_str().unwrap_or("");
                    eprintln!(
                        "clarp-host: push ring {}: a phone was not rung ({status} {reason})",
                        ring.ring_id
                    );
                }
                Some(status)
            }
            Err(crate::direct::Unsent(why)) => {
                eprintln!(
                    "clarp-host: push ring {}: a phone was not rung: {why}",
                    ring.ring_id
                );
                None
            }
        };
        answers.push(answer);
    }
    push::calls_counted(true, &answers)
}

/// The payload of a job, and the name it goes by in what the Host says of it. `face` is
/// the face it shows of its agent, for the phone it goes to: an address, and whether it is
/// a picture of the agent's own.
fn payload<'a>(job: &'a Job, server_id: &str, face: (&str, bool)) -> (Value, &'a str) {
    let (avatar_url, avatar_custom) = face;
    let (mut value, id): (Value, &str) = match job {
        Job::Turn(told, _) => {
            let mut payload = push::turn_done(&Told {
                persona: &told.persona,
                title: &told.title,
                session: &told.session,
                body: &told.preview,
                avatar_url,
                avatar_custom,
                notification_id: &told.notification_id,
                server_instance_id: server_id,
                needs_response: told.needs_response,
                event: told.event.id(),
                message_id: &told.message_id,
                agent_id: &told.agent_id,
                category: &told.category,
                title_loc: told.title_loc.as_ref(),
                body_loc: told.body_loc.as_ref(),
            });
            for (key, value) in &told.extra {
                payload[key.as_str()] = value.clone();
            }
            (payload, &told.notification_id)
        }
        Job::Request(asked) => (
            push::decision(&Asked {
                persona: &asked.persona,
                session: &asked.session,
                title: &asked.title,
                question: &asked.question,
                decision_id: &asked.decision_id,
                artifact_id: &asked.artifact_id,
                time_sensitive: asked.time_sensitive,
                text_input: asked.text_input,
                revision: asked.revision,
                expires_at: asked.expires_at,
                avatar_url,
                avatar_custom,
                server_instance_id: server_id,
                agent_id: &asked.agent_id,
                response_type: &asked.response_type,
                options: &asked.options,
                title_loc: asked.title_loc.as_ref(),
                body_loc: asked.body_loc.as_ref(),
            }),
            &asked.decision_id,
        ),
        Job::BadgeWake => (push::badge_wake(server_id), "badge-sync"),
        Job::Read(read) => (
            push::read(&read.session, server_id, &read.decision_id),
            &read.session,
        ),
        Job::Ring(rung, _) => (
            push::ring(&push::Rung {
                ring_id: &rung.ring.ring_id,
                state: &rung.ring.state,
                session: &rung.ring.session,
                persona: &rung.ring.persona,
                reason: &rung.ring.reason,
                opening_line: &rung.ring.opening_line,
                opening_audio_url: &rung.ring.opening_audio_url,
                expires_at: rung.ring.expires_at,
                kind: rung.kind,
                show_reason: rung.show_reason,
                avatar_url,
                avatar_custom,
                server_instance_id: server_id,
            }),
            &rung.ring.ring_id,
        ),
    };
    match job {
        Job::Turn(done, _) => done.order.event(&mut value),
        Job::Request(asked) => asked.order.event(&mut value),
        Job::Read(read) if read.decision_id.is_empty() => read.order.boundary(&mut value),
        _ => {}
    }
    (value, id)
}

/// Freeze ownership at submission; revalidate it when a queued ring is sent.
pub fn live_rows(
    directory: &crate::fleet::Directory,
    without: &[String],
    ring: &clarp_kernel::rings::Ring,
) -> Vec<(String, String)> {
    if ring.conversation_mode != "agent_live_v1" || ring.of_the_user().is_some() {
        return Vec::new();
    }
    directory
        .push
        .active()
        .filter(|row| {
            row.provider == "fcm"
                && row.platform == "android"
                && row.principal.starts_with("device_")
                && directory.can_receive(&row.principal)
                && !without.contains(&row.principal)
                && serde_json::from_str::<Vec<String>>(&row.supported_conversation_modes)
                    .is_ok_and(|modes| modes.iter().any(|mode| mode == "agent_live_v1"))
        })
        .map(|r| (r.token.clone(), r.principal.clone()))
        .collect()
}

/// Sends one job to every row of the register that is to have it. Answers what became of it.
fn live_rings(rx: mpsc::Receiver<Rung>, works: &Works) {
    let mut queued = std::collections::VecDeque::<Rung>::new();
    let mut rings = clarp_kernel::fcm::LiveRings::default();
    while let Some(rung) = queued.pop_front().or_else(|| rx.recv().ok()) {
        queued.extend(rx.try_iter());
        if !works.android.configured() {
            continue;
        }
        // A ring that begins to ring reaches only phones the owner lets this agent ring; what
        // became of one goes to the phones it was pushed to ([`LiveRings::targets`]).
        if rung.kind == "ring" && !calls_on(works, &rung.ring.agent_id) {
            continue;
        }
        let current = live_rows(&works.fleet.directory(), &rung.without, &rung.ring);
        let rows = rung
            .live_rows
            .iter()
            .filter(|row| current.contains(row))
            .map(|(token, _)| token.clone())
            .collect();
        let later = queued
            .iter()
            .any(|next| next.ring.ring_id == rung.ring.ring_id);
        let targets = rings.targets(
            &rung.ring.ring_id,
            rung.kind,
            rung.ring.expires_at,
            works.clock.now_ms(),
            later,
            rows,
        );
        if !targets.is_empty() {
            deliver(
                works,
                &Job::Ring(Box::new(rung), None),
                Targets::Live(&targets),
            );
        }
    }
}

#[derive(Clone, Copy)]
enum Targets<'a> {
    Apple,
    Android(&'a [(String, String)]),
    Live(&'a [String]),
}

fn deliver(works: &Works, job: &Job, targets: Targets<'_>) -> Counted {
    let (mode, key) = works.direct.alert_settings();
    let relay = works.relay.as_deref();
    let mut routes = Routes::new(
        &mode,
        relay.is_some(),
        relay.is_some_and(Relay::has_credential),
        key.is_some(),
    );
    let android = !matches!(targets, Targets::Apple) && works.android.configured();
    if !routes.enabled() && !android {
        return Counted::default();
    }
    let directory = works.fleet.directory();
    let now = works.clock.now_ms();
    let (_, name) = payload(job, &works.server_id, ("", false));
    // The agent it is about: what the address of its own picture carries, when it has one,
    // and the picture this Host ships for it. A push for nobody by name shows no face.
    let (session, persona) = match job {
        Job::Turn(told, _) => (told.session.as_str(), told.persona.as_str()),
        Job::Request(asked) => (asked.session.as_str(), asked.persona.as_str()),
        Job::Ring(rung, _) => (rung.ring.session.as_str(), rung.ring.persona.as_str()),
        Job::Read(read) => (read.session.as_str(), ""),
        Job::BadgeWake => ("", ""),
    };
    let about = directory.by_session(session).map(|member| &member.record);
    let own = about.and_then(|record| record.avatar.as_deref());
    let own = own.map(clarp_kernel::personas::version);
    let agent_id = about.map_or("", |record| record.agent_id.as_str());
    let shipped = match about {
        Some(record) if !persona.trim().is_empty() => {
            works.faces.of(&record.persona, &record.backend)
        }
        _ => String::new(),
    };
    let face_for = |row: &PushRow| -> Option<(String, bool)> {
        if persona.trim().is_empty() {
            return None;
        }
        let signed_by = (works.secret.as_str(), now / 1_000);
        clarp_kernel::portraits::face_for_push(&row.base_url, (agent_id, own), &shipped, signed_by)
    };
    // A user who sits at a desk with the app open sees it there. A ring goes to the phones
    // all the same: an agent calling is the interruption its user chose to allow.
    let desk_can_suppress = match job {
        Job::Turn(..) => true,
        Job::Request(asked) => !asked.escalated,
        _ => false,
    };
    let at_a_desk = desk_can_suppress
        && works
            .desks
            .lock()
            .is_ok_and(|desks| desks.active(now, &|owner| directory.can_receive(owner)));
    let mut counted = Counted {
        enabled: true,
        ..Counted::default()
    };
    // The owner's choice for this event of this agent (`notify_events`). A muted agent pages
    // nobody. Rings are switched in the pusher's loop ([`RingAlerts`]), so that what became of
    // a ring follows it.
    let switched = match job {
        Job::Turn(told, _) => Some((told.event, about.is_some_and(|record| record.muted))),
        Job::Request(_) => Some((Event::NeedsYou, about.is_some_and(|record| record.muted))),
        Job::Ring(..) | Job::BadgeWake | Job::Read(_) => None,
    };
    if let Some((event, muted)) = switched {
        let agent = match job {
            // A janitor's notice names it: it is no member a session finds.
            Job::Turn(told, _) if !told.agent_id.is_empty() => told.agent_id.as_str(),
            _ => agent_id,
        };
        let settings = Settings::stored(directory.setting(clarp_kernel::notify_events::KEY));
        if !settings.alerts(event, agent, muted, true) {
            eprintln!(
                "clarp-host: push {name}: not sent, {} is switched off",
                event.id()
            );
            return counted;
        }
    }
    if at_a_desk {
        eprintln!("clarp-host: push {name}: not sent, a desktop is active");
        return counted;
    }
    // A revoked device's rows are stopped when it is revoked; this is for any that outlived
    // that. Phones that are reached through another push service are not reached yet.
    let left_out = |row: &PushRow| match job {
        Job::Ring(rung, _) => !row.principal.is_empty() && rung.without.contains(&row.principal),
        _ => false,
    };
    let rows: Vec<&PushRow> = directory
        .push
        .active()
        .filter(|row| directory.can_receive(&row.principal))
        .filter(|row| match targets {
            Targets::Apple => {
                routes.enabled()
                    && row.provider != "fcm"
                    && (!matches!(job, Job::BadgeWake | Job::Read(_)) || row.platform == "ios")
            }
            Targets::Android(named) => {
                android
                    && row.provider == "fcm"
                    && named
                        .iter()
                        .any(|(t, o)| t == &row.token && o == &row.principal)
            }
            Targets::Live(named) => android && row.provider == "fcm" && named.contains(&row.token),
        })
        .filter(|row| !left_out(row))
        .collect();
    if rows.is_empty() {
        return counted;
    }
    let (mut sent, mut failed, mut disabled) = (0, 0, 0);
    for row in rows {
        let current = works.fleet.directory();
        if !current.can_receive(&row.principal)
            || !current
                .push
                .active()
                .any(|r| r.token == row.token && r.principal == row.principal)
        {
            continue;
        }
        if desk_can_suppress
            && works
                .desks
                .lock()
                .is_ok_and(|d| d.active(works.clock.now_ms(), &|owner| current.can_receive(owner)))
        {
            break;
        }
        // The face is fetched from where this phone reaches the Host.
        let face = face_for(row);
        let face = face
            .as_ref()
            .map_or(("", false), |(address, own)| (address.as_str(), *own));
        let (mut payload, _) = payload(job, &works.server_id, face);
        if matches!(targets, Targets::Live(_)) {
            payload["conversation_mode"] = serde_json::json!("agent_live_v1");
        }
        let background = matches!(job, Job::BadgeWake | Job::Read(_));
        let collapse_id = match job {
            Job::BadgeWake => "badge-sync".into(),
            Job::Read(read) => push::read_collapse_id(&read.session, &read.decision_id),
            _ => push::collapse_id(&payload),
        };
        let push = Push {
            payload: &payload,
            collapse_id: &collapse_id,
            push_type: if background { "background" } else { "alert" },
            priority: if background { "5" } else { "10" },
        };
        let delivery = if row.provider == "fcm" {
            let mut context = serde_json::json!({"agent_id":agent_id});
            if let Job::Turn(told, _) = job {
                context["badge"] = serde_json::json!(told.badge);
                context["unread"] = serde_json::json!(told.unread);
            }
            clarp_kernel::alert_routes::Delivery {
                outcome: works.android.push(
                    &row.token,
                    &payload,
                    &context,
                    if background { "5" } else { "10" },
                    &collapse_id,
                    false,
                ),
                grant_revoked: false,
            }
        } else {
            routes.send(!row.push_grant.is_empty(), |path| match path {
                Path::Grant => relay.map_or(Outcome::NoPath, |relay| {
                    relay.to_grant(&row.push_grant, &push)
                }),
                Path::Account => relay.map_or(Outcome::NoPath, |relay| {
                    relay.to_token(&row.token, &row.environment, &push)
                }),
                Path::Direct => key.as_ref().map_or(Outcome::NoPath, |key| {
                    let request = clarp_kernel::direct_push::Push {
                        push_type: push.push_type,
                        payload: push.payload,
                        priority: push.priority,
                        collapse_id: push.collapse_id,
                        expiration: None,
                    };
                    match works.runtime.block_on(works.direct.push(
                        key,
                        (&row.token, &row.environment),
                        &request,
                    )) {
                        Ok((200, _, _)) => Outcome::Sent,
                        Ok((status, reason, _)) => {
                            let reason = reason.as_str().unwrap_or("");
                            if status == 410
                                || clarp_kernel::direct_push::GONE_FOR_GOOD.contains(&reason)
                            {
                                Outcome::Dead(reason.to_string())
                            } else {
                                Outcome::Refused {
                                    status,
                                    reason: reason.to_string(),
                                }
                            }
                        }
                        Err(crate::direct::Unsent(why)) => Outcome::Down(why),
                    }
                }),
            })
        };
        if delivery.grant_revoked {
            works.fleet.pushed(row.token.clone(), Pushed::GrantRevoked);
        }
        let shown: String = row.token.chars().take(12).collect();
        match delivery.outcome {
            Outcome::Sent => {
                sent += 1;
                works.fleet.pushed(row.token.clone(), Pushed::Sent);
            }
            Outcome::Dead(why) => {
                disabled += 1;
                eprintln!("clarp-host: push {name}: token {shown}… is gone ({why})");
                works.fleet.pushed(row.token.clone(), Pushed::Dead);
            }
            Outcome::GrantRevoked => {
                disabled += 1;
                eprintln!(
                    "clarp-host: push {name}: the phone of token {shown}… took its grant back"
                );
                works.fleet.pushed(row.token.clone(), Pushed::Dead);
            }
            Outcome::Refused { status, reason } => {
                failed += 1;
                eprintln!(
                    "clarp-host: push {name}: refused for token {shown}… ({status} {reason})"
                );
            }
            Outcome::Down(why) => {
                failed += 1;
                eprintln!("clarp-host: push {name}: not sent to token {shown}…: {why}");
            }
            Outcome::NoPath => {
                failed += 1;
                eprintln!(
                    "clarp-host: push {name}: no way to token {shown}…: it gave no grant and \
                     this Host has no credential at the relay"
                );
            }
        }
    }
    eprintln!("clarp-host: push {name}: sent {sent}, failed {failed}, disabled {disabled}");
    (counted.sent, counted.failed, counted.disabled) = (sent, failed, disabled);
    counted
}

#[cfg(test)]
mod read_tests {
    use super::*;

    fn read(session: &str, decision_id: &str, at: i64) -> Read {
        Read {
            order: Order { at_ms: at, seq: 1 },
            session: session.to_string(),
            decision_id: decision_id.to_string(),
        }
    }

    #[test]
    fn delayed_reads_preserve_the_observed_boundary_and_a_later_decision_escalates() {
        let mut reads = Reads::default();
        let first = read("vera", "", 1000);
        let decision = Order {
            at_ms: 2000,
            seq: 2,
        };
        // A read observed at t1 arrives in the worker at t3, after the decision at t2.
        assert!(reads.due(&first));
        assert!(!reads.opened_since("vera", decision));
        let newer = read("vera", "", 3000);
        assert!(reads.due(&newer));
        assert!(!reads.due(&first));
        assert!(reads.opened_since("vera", newer.order));
    }

    #[test]
    fn delayed_chat_read_leaves_the_newer_alert_even_in_the_same_millisecond() {
        let stamp = |seq| Order { at_ms: 1000, seq };
        let alert = |seq| {
            payload(
                &Job::Turn(
                    TurnDone {
                        order: stamp(seq),
                        session: "vera".into(),
                        ..Default::default()
                    },
                    None,
                ),
                "host-1",
                ("", false),
            )
            .0
        };
        let clear = |id: &str| {
            payload(
                &Job::Read(Read {
                    order: stamp(2),
                    session: "vera".into(),
                    decision_id: id.into(),
                }),
                "host-1",
                ("", false),
            )
            .0
        };
        let (a, b, r) = (alert(1), alert(3), clear(""));
        assert_eq!(a["order_epoch"], r["order_epoch"]);
        assert_eq!(b["order_epoch"], r["order_epoch"]);
        assert_eq!(a["event_at_ms"], r["read_through_ms"]);
        assert_eq!(b["event_at_ms"], r["read_through_ms"]);
        assert_eq!(
            (
                a["event_seq"].as_u64(),
                r["read_through_seq"].as_u64(),
                b["event_seq"].as_u64()
            ),
            (Some(1), Some(2), Some(3))
        );
        let data = |v| clarp_kernel::fcm::data(v, &Value::Null, "", 9999).unwrap();
        assert_eq!(data(&a)["event_seq"], "1");
        assert_eq!(data(&b)["event_seq"], "3");
        assert_eq!(data(&r)["read_through_seq"], "2");
        assert_eq!(data(&r)["read_through_ms"], "1000");
        assert!(clear("d1").get("read_through_seq").is_none());
    }

    #[test]
    fn two_cards_in_one_chat_are_each_taken_down_once_and_neither_reads_the_chat() {
        let mut reads = Reads::default();
        assert!(reads.due(&read("vera", "d1", 1_000)));
        assert!(
            reads.due(&read("vera", "d2", 1_100)),
            "another card, at once"
        );
        assert!(
            !reads.due(&read("vera", "d1", 1_200)),
            "the same card again"
        );
        // A card's end is no reading of the chat: a real read after it is still sent, and
        // the chat was not opened by it.
        assert!(!reads.opened_since("vera", Order { at_ms: 0, seq: 1 }));
        assert!(reads.due(&read("vera", "", 1_300)));
        assert!(reads.opened_since(
            "vera",
            Order {
                at_ms: 1_300,
                seq: 1
            }
        ));
    }

    #[test]
    fn a_held_card_reaches_the_phones_unless_its_chat_was_opened_or_it_changed() {
        let mut reads = Reads::default();
        // Card d2 was held for the desk at 1 000; d1 in the same chat was answered after.
        assert!(reads.due(&read("vera", "d1", 1_500)));
        assert!(
            !reads.opened_since(
                "vera",
                Order {
                    at_ms: 1_000,
                    seq: 1
                }
            ),
            "d2 goes to the phones"
        );
        assert!(waits(("pending", 1, None), 1, 2_000));
        assert!(
            !waits(("pending", 2, None), 1, 2_000),
            "it changed: not this push"
        );
        assert!(!waits(("resolved", 1, None), 1, 2_000));
        assert!(!waits(("pending", 1, Some(1_999)), 1, 2_000), "it ran out");
        assert!(waits(("pending", 1, Some(2_001)), 1, 2_000));
        // Its chat opened after it was held: the desk had it.
        assert!(reads.due(&read("vera", "", 1_800)));
        assert!(reads.opened_since(
            "vera",
            Order {
                at_ms: 1_000,
                seq: 1
            }
        ));
    }

    #[test]
    fn what_became_of_a_ring_follows_it_whatever_the_switch_says_now() {
        let mut rings = RingAlerts::default();
        // Rung while calls were on, then switched off: its end still goes.
        assert!(rings.pushed("ring", "r1", true));
        assert!(rings.pushed("missed", "r1", false));
        // Held back while off: nothing of it goes, also once the switch is back on.
        assert!(!rings.pushed("ring", "r2", false));
        assert!(!rings.pushed("cancelled", "r2", true));
        // A ring from before a restart is told what became of it.
        assert!(rings.pushed("answered", "r0", false));
    }
}
