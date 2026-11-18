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

use std::sync::{Arc, Mutex, mpsc};

use clarp_kernel::alert_routes::{Path, Routes};
use clarp_kernel::devices::PushRow;
use clarp_kernel::presence::Presence;
use clarp_kernel::push::{self, Asked, Told};
use clarp_ports::clock::Clock;
use serde_json::Value;

mod android_lanes;

use crate::direct::Direct;
use crate::fleet::{FleetHandle, Pushed};
use crate::relay::{Outcome, Push, Relay};

/// A finished turn the user is told of.
#[derive(Debug, Clone, Default)]
pub struct TurnDone {
    pub badge: bool,
    pub unread: bool,
    pub session: String,
    pub persona: String,
    pub notification_id: String,
    /// What the agent said, as a notification shows it.
    pub preview: String,
    /// The agent still waits for an answer from the user: the push makes a sound.
    pub needs_response: bool,
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
}

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
    Turn(TurnDone),
    Request(Requested),
    /// A ring, and who waits to hear what became of its pushes.
    Ring(Box<Rung>, Option<tokio::sync::oneshot::Sender<Rang>>),
}

/// The way to the pusher's thread. Handing it something never waits.
#[derive(Clone)]
pub struct Pusher {
    live: mpsc::Sender<Rung>,
    tx: mpsc::Sender<Job>,
}

impl Pusher {
    pub fn turn_done(&self, told: TurnDone) {
        let _ = self.tx.send(Job::Turn(told));
    }

    pub fn requested(&self, asked: Requested) {
        let _ = self.tx.send(Job::Request(asked));
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
}

/// A pusher, and what waits for its thread.
pub fn pusher() -> (Pusher, Waiting) {
    let (tx, rx) = mpsc::channel();
    let (live, waiting) = mpsc::channel();
    (Pusher { tx, live }, Waiting { rx, live: waiting })
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
        let android = works.clone();
        std::thread::Builder::new()
            .name("android-live-rings".into())
            .spawn(move || live_rings(self.live, &android))
            .map_err(|e| e.to_string())?;
        std::thread::Builder::new()
            .name("pusher".to_string())
            .spawn(move || {
                let mut lanes = android_lanes::Lanes::default();
                let works = Arc::new(works);
                for job in self.rx {
                    let (job, told) = match job {
                        Job::Ring(rung, told) => (Job::Ring(rung, None), told),
                        other => (other, None),
                    };
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
    match job {
        Job::Turn(told) => (
            push::turn_done(&Told {
                persona: &told.persona,
                session: &told.session,
                body: &told.preview,
                avatar_url,
                avatar_custom,
                notification_id: &told.notification_id,
                server_instance_id: server_id,
                needs_response: told.needs_response,
            }),
            &told.notification_id,
        ),
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
            }),
            &asked.decision_id,
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
    }
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
        Job::Turn(told) => (&told.session, &told.persona),
        Job::Request(asked) => (&asked.session, &asked.persona),
        Job::Ring(rung, _) => (&rung.ring.session, &rung.ring.persona),
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
    let at_a_desk = !matches!(job, Job::Ring(..))
        && works
            .desks
            .lock()
            .is_ok_and(|desks| desks.active(now, &|owner| directory.can_receive(owner)));
    let mut counted = Counted {
        enabled: true,
        ..Counted::default()
    };
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
            Targets::Apple => routes.enabled() && row.provider != "fcm",
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
        if !matches!(job, Job::Ring(..))
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
        let collapse_id = push::collapse_id(&payload);
        let push = Push {
            payload: &payload,
            collapse_id: &collapse_id,
            push_type: "alert",
            priority: "10",
        };
        let delivery = if row.provider == "fcm" {
            let mut context = serde_json::json!({"agent_id":agent_id});
            if let Job::Turn(told) = job {
                context["badge"] = serde_json::json!(told.badge);
                context["unread"] = serde_json::json!(told.unread);
            }
            clarp_kernel::alert_routes::Delivery {
                outcome: works.android.push(
                    &row.token,
                    &payload,
                    &context,
                    "10",
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
