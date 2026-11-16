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

use std::sync::{Arc, Mutex, mpsc};

use clarp_kernel::devices::PushRow;
use clarp_kernel::presence::Presence;
use clarp_kernel::push::{self, Asked, Told};
use clarp_ports::clock::Clock;
use serde_json::Value;

use crate::fleet::{FleetHandle, Pushed};
use crate::relay::{Outcome, Push, Relay};

/// A finished turn the user is told of.
#[derive(Debug, Clone, Default)]
pub struct TurnDone {
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

enum Job {
    Turn(TurnDone),
    Request(Requested),
}

/// The way to the pusher's thread. Handing it something never waits.
#[derive(Clone)]
pub struct Pusher {
    tx: mpsc::Sender<Job>,
}

impl Pusher {
    pub fn turn_done(&self, told: TurnDone) {
        let _ = self.tx.send(Job::Turn(told));
    }

    pub fn requested(&self, asked: Requested) {
        let _ = self.tx.send(Job::Request(asked));
    }
}

/// What is handed to the pusher before its thread runs: kept, and sent once it does.
pub struct Waiting {
    rx: mpsc::Receiver<Job>,
}

/// A pusher, and what waits for its thread.
pub fn pusher() -> (Pusher, Waiting) {
    let (tx, rx) = mpsc::channel();
    (Pusher { tx }, Waiting { rx })
}

/// What the pusher's thread works with.
pub struct Works {
    /// The way to the phones. `None`: this Host pushes nothing.
    pub relay: Option<Relay>,
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
}

impl Waiting {
    /// Starts the pusher's thread.
    pub fn run(self, works: Works) -> Result<(), String> {
        std::thread::Builder::new()
            .name("pusher".to_string())
            .spawn(move || {
                for job in self.rx {
                    // Without a relay what is handed over is taken and let go.
                    if let Some(relay) = &works.relay {
                        deliver(relay, &works, &job);
                    }
                }
            })
            .map(|_| ())
            .map_err(|error| format!("the pusher's thread could not be started: {error}"))
    }
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
    }
}

/// Sends one job to every row of the register that is to have it.
fn deliver(relay: &Relay, works: &Works, job: &Job) {
    let directory = works.fleet.directory();
    let now = works.clock.now_ms();
    let (_, name) = payload(job, &works.server_id, ("", false));
    // The agent it is about: what the address of its own picture carries, when it has one,
    // and the picture this Host ships for it. A push for nobody by name shows no face.
    let (session, persona) = match job {
        Job::Turn(told) => (&told.session, &told.persona),
        Job::Request(asked) => (&asked.session, &asked.persona),
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
    // A user who sits at a desk with the app open sees it there.
    let at_a_desk = works
        .desks
        .lock()
        .is_ok_and(|desks| desks.active(now, &|owner| directory.can_receive(owner)));
    if at_a_desk {
        eprintln!("clarp-host: push {name}: not sent, a desktop is active");
        return;
    }
    // A revoked device's rows are stopped when it is revoked; this is for any that outlived
    // that. Phones that are reached through another push service are not reached yet.
    let rows: Vec<&PushRow> = directory
        .push
        .active()
        .filter(|row| row.provider != "fcm" && directory.can_receive(&row.principal))
        .collect();
    if rows.is_empty() {
        return;
    }
    let (mut sent, mut failed, mut disabled) = (0, 0, 0);
    for row in rows {
        // The face is fetched from where this phone reaches the Host.
        let face = face_for(row);
        let face = face
            .as_ref()
            .map_or(("", false), |(address, own)| (address.as_str(), *own));
        let (payload, _) = payload(job, &works.server_id, face);
        let collapse_id = push::collapse_id(&payload);
        let delivery = relay.send(
            row,
            &Push {
                payload: &payload,
                collapse_id: &collapse_id,
                push_type: "alert",
                priority: "10",
            },
        );
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
}
