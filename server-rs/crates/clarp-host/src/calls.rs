//! Calls at the Host's edge: the record of a hands-free conversation with several agents.
//!
//! The rules are in [`clarp_kernel::calls`]. Here is where they are asked: by an app's
//! buttons (`POST /calls`, `/calls/add`, ...), by a message that carries its call
//! (`call_id` of a send), and by an agent's own tool. One request at a time: the calls are
//! read where the fleet keeps them when the first one comes, each phone's calls are kept
//! again when they changed, the apps are told of each change (`group-call`), and the agents
//! a change concerns find a quiet word about it in their conversations.
//!
//! The routes answer as the Python Host's do (`server/lib/routes/calls.py`,
//! `docs/group-calls.md`): the same statuses, bodies and words.
//!
//! And rings: an agent calls its owner ([`clarp_kernel::rings`], `docs/agent-rings.md`). The
//! phone shows an incoming call; answering starts a call with that agent, whose opening
//! line, voiced when the ring was placed, is heard first. A ring and the call it becomes
//! are one desk's, because an answer changes both.
//!
//! A ring may be held as a live voice call, and the owner may call an agent so: that is
//! [`live`], on the same desk and under the same lock.

use std::cell::RefCell;
use std::collections::{BTreeMap, HashMap, HashSet};
use std::time::Duration;

use axum::body::Bytes;
use axum::extract::{Query, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Extension, Json, Router};
use clarp_kernel::calls::{ADMINISTRATOR, Asker, At, Call, Calls, Contact, Effects, Refused};
use clarp_kernel::rings::{self, Rings};
use serde_json::{Value, json};

use crate::agent::Unavailable;
use crate::edge::{ApiError, App, Principal};
use crate::fleet::Directory;
use crate::ids;
use crate::pairing::Scope;

pub mod live;

/// What became of a request to ring, under the desk's lock.
enum Placing {
    /// It is answered already: the ring its event rang before.
    Told(Value),
    /// A ring held as a live voice call: placed, with nothing voiced.
    Placed(live::Placed),
    /// An ordinary ring: its line is voiced, and then it is placed.
    ToVoice(rings::Admitted),
}

/// The kind of thing a phone's calls are, among what is left with the fleet.
const KIND: &str = "call";

/// What a phone without full scope is told.
const FULL_ONLY: &str = "Group calls require full-device authentication";

/// The kind of thing the rings are, and the names their parts are kept under.
const RINGS: &str = "ring";
const RINGS_KEPT: &str = "rings";
const AUDIT_KEPT: &str = "audit";
const SETTINGS_KEPT: &str = "settings";
const VOIP_KEPT: &str = "voip";
/// The phones that switched rings off for themselves, and the agents known to take the
/// user's calls live.
const PREFS_KEPT: &str = "prefs";
const TAKING_KEPT: &str = "taking";

/// How long after a ring is overdue its timer looks: by then it is overdue for certain.
const TIMER_SLACK: Duration = Duration::from_millis(500);
/// How long the one who placed a ring waits to hear what became of its push. The ring
/// rings either way.
const PUSH_WAIT: Duration = Duration::from_secs(25);

/// The calls and the rings of this Host.
struct Books {
    calls: Calls,
    rings: Rings,
    /// The agents known to take the user's calls live, as it is kept.
    taking: Option<String>,
    /// The sessions of the live calls that run now, by their call: how each is stopped.
    sessions: HashMap<String, live::Running>,
    /// The phones that hold a live call's socket now: one each.
    voices: HashSet<String>,
}

/// The desk of calls and rings. Its books are `None` until they were read where they are
/// kept.
#[derive(Default)]
pub struct Desk {
    books: tokio::sync::Mutex<Option<Books>>,
    /// One ring is placed at a time: from the moment it is admitted until it rings, while
    /// its opening line is voiced. Reading and answering wait for nobody's voice.
    placing: tokio::sync::Mutex<()>,
    /// The rings whose agents wait to hear how they ended (`clarp-admin call ring --wait`):
    /// by the call of the tool that placed each, with the moment it was called.
    waited_for: std::sync::Mutex<HashMap<String, (String, i64)>>,
}

/// How long a tool that waits for its ring is remembered when it never asks again.
const WAIT_KEPT_MS: i64 = 5 * 60 * 1000;

/// What a request on the rings is given beside the books: the moment, the agents a name
/// may mean, any agent by its id, and a new id.
pub struct With<'a> {
    pub at: At<'a>,
    pub roster: &'a [Contact],
    pub known: &'a dyn Fn(&str) -> Option<rings::Caller>,
    pub new_id: &'a str,
    /// What of the Host a ring held as a live voice call is admitted and held by.
    pub world: rings::World<'a>,
    /// The live calls a request ended on their rings: their sessions are stopped before the
    /// books are let go of, so that a call is never retired and its session not told.
    pub stops: &'a RefCell<Vec<(String, String)>>,
}

impl With<'_> {
    fn people(&self) -> rings::People<'_> {
        rings::People {
            roster: self.roster,
            known: self.known,
        }
    }
}

/// The agents that may be asked into a call: the ones an app shows as contacts.
fn roster(directory: &Directory) -> Vec<Contact> {
    let shown = directory
        .members
        .iter()
        .map(|member| &member.record)
        .filter(|record| record.archived_at_ms.is_none());
    shown
        .map(|record| Contact {
            session: record.session.clone(),
            agent_id: record.agent_id.clone(),
            persona: record.persona.clone(),
            helper: record.is_helper(),
        })
        .collect()
}

impl Desk {
    /// One request on the calls. `ask` is given the calls, the moment, the agents that may
    /// be asked in, a new id for a call, and where to put what the request does beside its
    /// answer. What it did is done before this answers: kept, told, noted.
    pub async fn ask<T>(
        &self,
        app: &App,
        ask: impl FnOnce(&mut Calls, At, (&[Contact], &str), &mut Effects) -> T,
    ) -> Result<T, Unavailable> {
        let mut held = self.books.lock().await;
        let books = Self::read(&mut held, app).await?;
        let directory = app.fleet.directory();
        let roster = roster(&directory);
        let at = At {
            now_ms: app.clock.now_ms(),
            host_id: &app.server_id,
        };
        let mut out = Effects::default();
        let answer = ask(&mut books.calls, at, (&roster, &ids::hex(16)), &mut out);
        Self::done(app, &directory, &books.calls, out, at.now_ms).await?;
        Ok(answer)
    }

    /// The books, read where they are kept when this is the first request since the Host
    /// started. A ring that rang when the Host stopped is given its timer again.
    async fn read<'a>(
        held: &'a mut Option<Books>,
        app: &App,
    ) -> Result<&'a mut Books, Unavailable> {
        if held.is_none() {
            let mut books: BTreeMap<String, Vec<Call>> = BTreeMap::new();
            for (principal, kept) in app.fleet.kept_all(KIND).await? {
                // What cannot be read is no call: the phone starts a new one.
                if let Ok(calls) = serde_json::from_slice::<Vec<Call>>(&kept) {
                    books.insert(principal, calls);
                }
            }
            // And of the rings what can be read: the rest begins anew.
            let kept: HashMap<String, Vec<u8>> =
                app.fleet.kept_all(RINGS).await?.into_iter().collect();
            let read = |name: &str| -> Value {
                let kept = kept.get(name).map(|kept| serde_json::from_slice(kept));
                kept.and_then(Result::ok).unwrap_or(Value::Null)
            };
            let list = |name: &str| match read(name) {
                Value::Array(list) => list,
                _ => Vec::new(),
            };
            let rung = serde_json::from_value(read(RINGS_KEPT)).unwrap_or_default();
            let rings = Rings::kept(
                rung,
                list(AUDIT_KEPT),
                &read(SETTINGS_KEPT),
                list(VOIP_KEPT),
            );
            let mut rings = rings.with_prefs(list(PREFS_KEPT));
            // A live call that was joined under a Host before this one has no session now.
            let mut out = rings::Effects::default();
            if !rings.orphaned(&mut out).is_empty() {
                let kept = serde_json::to_vec(rings.rings()).unwrap_or_default();
                app.fleet
                    .keep(RINGS, RINGS_KEPT.to_string(), Some(kept))
                    .await?;
            }
            if let Some(ring) = rings.ringing() {
                timer(app, ring.expires_at);
            }
            *held = Some(Books {
                calls: Calls::kept(books),
                rings,
                taking: read(TAKING_KEPT).as_str().map(str::to_string),
                sessions: HashMap::new(),
                voices: HashSet::new(),
            });
        }
        held.as_mut()
            .ok_or_else(|| Unavailable("the calls could not be read".to_string()))
    }

    /// What a request did to the calls beside its answer: kept, told, noted.
    async fn done(
        app: &App,
        directory: &Directory,
        calls: &Calls,
        out: Effects,
        now_ms: i64,
    ) -> Result<(), Unavailable> {
        for principal in &out.changed {
            let kept = serde_json::to_vec(calls.of(principal)).unwrap_or_default();
            app.fleet.keep(KIND, principal.clone(), Some(kept)).await?;
        }
        for event in out.events {
            app.hub.publish(event, now_ms);
        }
        for notice in out.notices {
            note(directory, &notice.agent_id, &notice.id, notice.text).await;
        }
        Ok(())
    }

    /// One request on the rings. `ask` is given the rings, the calls an answer starts one
    /// in, what a request needs to know, and where to put what it does beside its answer.
    /// What it did is done before this answers: kept, told, noted; and the phones are told
    /// on the side, except of what `waited_for` picks: that push is handed back, for the
    /// one who placed a ring and waits to hear what became of it.
    async fn ask_rings<T>(
        &self,
        app: &App,
        ask: impl FnOnce(&mut Rings, &mut Calls, &With, (&mut rings::Effects, &mut Effects)) -> T,
    ) -> Result<(T, Vec<rings::Pushed>), Unavailable> {
        let mut held = self.books.lock().await;
        let books = Self::read(&mut held, app).await?;
        let directory = app.fleet.directory();
        let roster = roster(&directory);
        let now_ms = app.clock.now_ms();
        let (mut out, mut of_calls) = (rings::Effects::default(), Effects::default());
        let shelf = live::Shelf::of(app, books.taking.clone());
        let stops = RefCell::new(Vec::new());
        // What the request is given lives no longer than the request: nothing of it waits
        // with what is done afterwards.
        let answer = {
            let androids = live::androids(&directory);
            let paired = |principal: &str| directory.can_receive(principal);
            let world = rings::World {
                profiles: &shelf,
                android: &androids,
                // This Host pushes nothing to Android phones.
                can_push_android: false,
                device_ok: &paired,
                host_id: &app.server_id,
                has_key: app.ears.key("openai").is_some(),
                // The tools of a live call are not built here yet.
                runs_tools: false,
            };
            let known = |agent_id: &str| -> Option<rings::Caller> {
                let mut members = directory.members.iter();
                let member = members.find(|member| member.record.agent_id == agent_id)?;
                let record = &member.record;
                Some(rings::Caller {
                    session: record.session.clone(),
                    persona: record.persona.clone(),
                    agent_id: record.agent_id.clone(),
                    muted: record.muted,
                    can_call: record.archived_at_ms.is_none() && record.deleted_at_ms.is_none(),
                })
            };
            let with = With {
                at: At {
                    now_ms,
                    host_id: &app.server_id,
                },
                roster: &roster,
                known: &known,
                new_id: &ids::hex(16),
                world,
                stops: &stops,
            };
            ask(
                &mut books.rings,
                &mut books.calls,
                &with,
                (&mut out, &mut of_calls),
            )
        };
        // A call that was retired on its ring: its session is told now, under the same lock.
        for (call_id, reason) in stops.into_inner() {
            if let Some(running) = books.sessions.get(&call_id) {
                running.stop(&reason);
            }
        }
        if let Some(taking) = shelf.to_keep() {
            let kept = serde_json::to_vec(&json!(taking)).unwrap_or_default();
            app.fleet
                .keep(RINGS, TAKING_KEPT.to_string(), Some(kept))
                .await?;
            books.taking = Some(taking);
        }
        // What a call says of itself comes before what the ring says of its answer.
        Self::done(app, &directory, &books.calls, of_calls, now_ms).await?;
        let rings = &books.rings;
        let keeps = [
            (
                out.changed.rings,
                RINGS_KEPT,
                serde_json::to_vec(rings.rings()),
            ),
            (
                out.changed.audit,
                AUDIT_KEPT,
                serde_json::to_vec(rings.audit()),
            ),
            (
                out.changed.settings,
                SETTINGS_KEPT,
                serde_json::to_vec(&rings.settings()),
            ),
            (
                out.changed.voip,
                VOIP_KEPT,
                serde_json::to_vec(rings.voip()),
            ),
            (
                out.changed.prefs,
                PREFS_KEPT,
                serde_json::to_vec(rings.prefs()),
            ),
        ];
        for (changed, name, kept) in keeps {
            if changed {
                app.fleet
                    .keep(RINGS, name.to_string(), Some(kept.unwrap_or_default()))
                    .await?;
            }
        }
        for event in out.events {
            app.hub.publish(event, now_ms);
        }
        for notice in out.notices {
            note(&directory, &notice.agent_id, &notice.id, notice.text).await;
        }
        Ok((answer, out.pushes))
    }

    /// The owner's phones are told of what became of rings, on the side.
    async fn push(&self, app: &App, pushes: Vec<rings::Pushed>) {
        if pushes.is_empty() {
            return;
        }
        let (show_reason, without) = self.alerted().await;
        for pushed in pushes {
            app.pusher.rung(crate::pusher::Rung {
                ring: pushed.ring,
                kind: pushed.kind,
                show_reason,
                without: without.clone(),
                calls: Vec::new(),
            });
        }
    }

    /// How the alert of a ring is sent: whether a locked phone may show why the agent calls,
    /// and the phones that are left out of it, having switched rings off for themselves.
    async fn alerted(&self) -> (bool, Vec<String>) {
        let held = self.books.lock().await;
        let rings = held.as_ref().map(|books| &books.rings);
        rings.map_or((false, Vec::new()), |rings| {
            (rings.shows_reason(), rings.switched_off())
        })
    }

    /// A request on the rings that places none: asked, done, and the phones told on the side.
    pub async fn rings<T>(
        &self,
        app: &App,
        ask: impl FnOnce(&mut Rings, &mut Calls, &With, (&mut rings::Effects, &mut Effects)) -> T,
    ) -> Result<T, Unavailable> {
        let (answer, pushes) = self.ask_rings(app, ask).await?;
        self.push(app, pushes).await;
        Ok(answer)
    }

    /// An agent rings its owner (`POST /calls/ring`, `clarp-admin call ring`). `turn_agent_id`
    /// is the agent whose turn asks, which rings as itself only; empty for the owner's own
    /// tools, which name the agent. The ring is admitted, its opening line is voiced in the
    /// agent's own voice, and then it rings: the phones are pushed and the one who asked is
    /// told what became of that. A ring held as a live voice call is placed in the moment it
    /// is admitted, with nothing voiced: its opening is the voice's own.
    pub async fn ring(
        &self,
        app: &App,
        turn_agent_id: &str,
        said: &Value,
    ) -> Result<Result<Value, Refused>, Unavailable> {
        let _one_at_a_time = self.placing.lock().await;
        let (agent, reason, line) = (
            text(said, "agent"),
            text(said, "reason"),
            text(said, "opening_line"),
        );
        let asked = rings::Asked {
            turn_agent_id,
            agent: &agent,
            reason: &reason,
            opening_line: &line,
            conversation_mode: &said["conversation_mode"],
            event_context: &said["event_context"],
            opening_mode: said.get("opening_mode"),
        };
        let admitted = self.ask_rings(app, |rings, _, with, (out, _)| {
            let now = with.at.now_ms;
            let admission = rings.admit(&asked, with.people(), &with.world, now, out)?;
            Ok(match admission {
                rings::Admission::Again(answer) => Placing::Told(answer),
                // A live ring's opening is its voice's own: it is placed at once.
                rings::Admission::Place(admitted) if admitted.is_live() => {
                    let ring = rings.place(admitted, "", with.new_id, now, out);
                    Placing::Placed(live::placed(rings, ring, &with.world))
                }
                rings::Admission::Place(admitted) => Placing::ToVoice(admitted),
            })
        });
        let (admitted, pushes) = admitted.await?;
        let (placed, pushes) = match admitted {
            Ok(Placing::Placed(placed)) => (placed, pushes),
            Ok(Placing::ToVoice(admitted)) => {
                // What else the request did is told as ever (an older ring missed by now).
                self.push(app, pushes).await;
                // Said in the agent's own voice now, so that answering plays it at once.
                // When nothing here can say it, the ring rings all the same and the phone
                // shows the line.
                let voiced = match app.member(&admitted.caller().session) {
                    Some(member) => {
                        let said = app
                            .mouth
                            .said_outside_a_turn(&member.record, admitted.opening());
                        said.await
                    }
                    None => None,
                };
                let voiced = voiced.map_or_else(String::new, |name| format!("/audio/{name}"));
                let placed = self.ask_rings(app, |rings, _, with, (out, _)| {
                    let now = with.at.now_ms;
                    let ring = rings.place(admitted, &voiced, with.new_id, now, out);
                    live::placed(rings, ring, &with.world)
                });
                placed.await?
            }
            Ok(Placing::Told(answer)) => {
                self.push(app, pushes).await;
                return Ok(Ok(answer));
            }
            Err(refused) => {
                self.push(app, pushes).await;
                return Ok(Err(refused));
            }
        };
        timer(app, placed.ring.expires_at);
        let delivery = self.rang(app, &placed, pushes).await;
        Ok(Ok(Rings::rung(&placed.ring, delivery, app.clock.now_ms())))
    }

    /// A ring that was placed is pushed to the phones, and what became of that is what the
    /// one who rang is told (`delivery`): as an incoming call to the phones that left a token
    /// for one, where this Host has a key at the push service; and as an alert where that
    /// reached no phone, never for a ring held as a live voice call.
    async fn rang(&self, app: &App, placed: &live::Placed, pushes: Vec<rings::Pushed>) -> Value {
        let (show_reason, without) = self.alerted().await;
        let mut rang = None;
        let mut still_going = false;
        for pushed in pushes {
            let begins = pushed.kind == "ring" && pushed.ring.ring_id == placed.ring.ring_id;
            let rung = crate::pusher::Rung {
                ring: pushed.ring,
                kind: pushed.kind,
                show_reason,
                without: without.clone(),
                calls: Vec::new(),
            };
            if !begins {
                app.pusher.rung(rung);
                continue;
            }
            let calls = placed.calls.clone();
            let told = app
                .pusher
                .rung_and_told(crate::pusher::Rung { calls, ..rung });
            match tokio::time::timeout(PUSH_WAIT, told).await {
                Ok(Ok(told)) => rang = Some(told),
                // The pusher is gone with the Host; nothing was sent.
                Ok(Err(_)) => {}
                // A push service that takes its time: the ring rings, and the one who rang
                // is not kept waiting for the count.
                Err(_) => still_going = true,
            }
        }
        let live = !placed.ring.conversation_mode.is_empty();
        let mut delivery = rang.unwrap_or_default().told();
        if delivery.get("voip").is_none() {
            delivery["voip"] = json!({ "enabled": false, "sent": 0, "failed": 0 });
        }
        if placed.android > 0 {
            delivery["fcm_live"] = json!({ "pending": placed.android });
        }
        if still_going {
            if !live && let Some(counted) = delivery.as_object_mut() {
                let relay = crate::pusher::Counted {
                    enabled: !app.config.push_relay.is_empty(),
                    ..crate::pusher::Counted::default()
                };
                if let Value::Object(relay) = relay.told() {
                    counted.extend(relay);
                }
            }
            delivery["pending"] = json!(true);
        }
        delivery
    }

    /// Where a message that carries a call goes: the session of the agent that takes it,
    /// and everybody of the call who can take speech. `Err` is what the sender is told
    /// instead (the call is over, nobody can take speech).
    pub async fn route(
        &self,
        app: &App,
        (caller, call_id, session): (&str, &str, &str),
    ) -> Result<Result<(String, Vec<String>), Refused>, Unavailable> {
        let asker = Asker {
            caller,
            call_id,
            ..Asker::default()
        };
        let routed = self
            .ask(app, |calls, at, _, out| {
                calls.route(asker, session, at, out)
            })
            .await?;
        Ok(routed.map(|routed| {
            let reachable = routed["reachable"].as_array();
            let reachable = reachable.map(|all| all.iter().filter_map(Value::as_str));
            let reachable = reachable.map(|all| all.map(str::to_string).collect());
            let session = routed["session"].as_str().unwrap_or("").to_string();
            (session, reachable.unwrap_or_default())
        }))
    }

    /// A message of a call went to this agent by another way than the call's own: the
    /// router of spoken messages found it. The agent takes the floor.
    pub async fn routed(
        &self,
        app: &App,
        (caller, call_id, session): (&str, &str, &str),
    ) -> Result<(), Unavailable> {
        let asker = Asker {
            caller,
            call_id,
            ..Asker::default()
        };
        self.ask(app, |calls, at, _, out| {
            calls.routed(asker, session, at, out)
        })
        .await
    }
}

impl Desk {
    /// The ring a call of an agent's tool placed and waits for, when it placed one.
    pub fn waited_for(&self, tool_call: &str) -> Option<String> {
        let waits = self.waited_for.lock().ok()?;
        waits.get(tool_call).map(|(ring_id, _)| ring_id.clone())
    }

    /// A call of an agent's tool waits for this ring, from now on.
    pub fn wait_for(&self, tool_call: &str, ring_id: &str, now_ms: i64) {
        if let Ok(mut waits) = self.waited_for.lock() {
            waits.retain(|_, (_, since)| now_ms - *since < WAIT_KEPT_MS);
            waits.insert(tool_call.to_string(), (ring_id.to_string(), now_ms));
        }
    }

    /// The call of the tool has its answer: it waits no more.
    pub fn waited(&self, tool_call: &str) {
        if let Ok(mut waits) = self.waited_for.lock() {
            waits.remove(tool_call);
        }
    }
}

/// A quiet word in an agent's conversation about its call or its ring, written once.
async fn note(directory: &Directory, agent_id: &str, id: &str, text: String) {
    let mut members = directory.members.iter();
    if let Some(member) = members.find(|member| member.record.agent_id == agent_id) {
        let _ = member.agent.note(format!("call:{id}"), text).await;
    }
}

/// The Host has started: what it kept of calls and rings is read now. A ring that rang when
/// the Host stopped is given its timer again; one that is overdue by now is missed now.
pub fn resume(app: &App) {
    let app = app.clone();
    tokio::spawn(async move {
        let desk = app.calls.clone();
        let _ = desk
            .rings(&app, |rings, _, with, (out, _)| {
                rings.timer(with.at.now_ms, out)
            })
            .await;
    });
}

/// When a ring is overdue, it is missed: the apps, the phones and the agent are told then,
/// and not only when somebody next looks.
fn timer(app: &App, expires_at: i64) {
    let app = app.clone();
    tokio::spawn(async move {
        let left = u64::try_from(expires_at - app.clock.now_ms()).unwrap_or(0);
        tokio::time::sleep(Duration::from_millis(left) + TIMER_SLACK).await;
        let desk = app.calls.clone();
        let _ = desk
            .rings(&app, |rings, _, with, (out, _)| {
                rings.timer(with.at.now_ms, out)
            })
            .await;
    });
}

/// How many of the newest things of each agent's conversation are looked at for what was
/// said in a call: more than the header quotes of all of them together.
const LOOKED_AT: usize = 40;

impl Desk {
    /// What the agent of this session is told of its call with a turn that starts now: who
    /// else is in it, and what was said since it last spoke. Empty when no call's latest
    /// utterance went to it, or not lately.
    pub async fn header(&self, app: &App, session: &str) -> String {
        use clarp_actor::conversation::Which;
        use clarp_kernel::calls::Said;
        use clarp_kernel::conversation::Entry;
        use clarp_speech::markup;

        let found = self
            .ask(app, |calls, at, _, _| {
                calls.turn_of(session, at.now_ms).cloned()
            })
            .await;
        let Ok(Some(call)) = found else {
            return String::new();
        };
        // What was said in the call's agents' conversations since it began, by people and
        // by the agents at the end of their turns, as a reader sees it, the oldest first.
        let directory = app.fleet.directory();
        let mut said: Vec<(i64, usize, String, bool, String)> = Vec::new();
        for part in &call.participants {
            let mut members = directory.members.iter();
            let Some(member) = members.find(|member| member.record.agent_id == part.agent_id)
            else {
                continue;
            };
            let Ok(page) = member.agent.tail(LOOKED_AT, Which::WithoutAutomated).await else {
                continue;
            };
            for row in page.entries {
                if row.ts_ms < call.started_at || row.live {
                    continue;
                }
                let (by_user, text) = match &row.entry {
                    Entry::Message(message) => (true, &message.text),
                    Entry::Reply(reply) if !row.commentary => (false, &reply.text),
                    _ => continue,
                };
                let text = markup::clean_for_display(text, true);
                if !text.is_empty() {
                    said.push((row.ts_ms, said.len(), part.agent_id.clone(), by_user, text));
                }
            }
        }
        said.sort_by_key(|(at, place, ..)| (*at, *place));
        let said: Vec<Said> = said
            .iter()
            .map(|(_, _, agent_id, by_user, text)| Said {
                agent_id,
                by_user: *by_user,
                text,
            })
            .collect();
        call.header(session, &said)
    }
}

/// Who asks, as the calls know a caller: a phone by its id, or the administrator. A phone
/// that may only read and send has no call.
pub fn caller(principal: &Principal) -> Result<String, ApiError> {
    match principal {
        Principal::Administrator | Principal::Tools => Ok(ADMINISTRATOR.to_string()),
        Principal::Device {
            device_id,
            scope: Scope::Full,
        } => Ok(device_id.clone()),
        Principal::Device { .. } => Err(ApiError::new(StatusCode::UNAUTHORIZED, FULL_ONLY)),
    }
}

fn answered(asked: Result<Result<Value, Refused>, Unavailable>) -> Response {
    match asked {
        Ok(Ok(answer)) => Json(answer).into_response(),
        Ok(Err(refused)) => {
            let status = StatusCode::from_u16(refused.status).unwrap_or(StatusCode::CONFLICT);
            (status, Json(refused.body)).into_response()
        }
        Err(unavailable) => ApiError::from(unavailable).into_response(),
    }
}

/// A field as the text the Python Host makes of it (`str(value or "")`).
fn text(said: &Value, key: &str) -> String {
    match &said[key] {
        Value::String(text) => text.clone(),
        Value::Number(number) => number.to_string(),
        _ => String::new(),
    }
}

/// The agent a change is made by, when the request names one this Host has: its session.
fn by(app: &App, said: &Value) -> String {
    let named = text(said, "by");
    let directory = app.fleet.directory();
    let member = directory.named(named.trim());
    member.map_or_else(String::new, |member| member.record.session.clone())
}

/// What a request says. Nothing at all is an empty object, as on the Python Host; what is
/// no object is told so by whoever asks for one.
fn body(bytes: &Bytes) -> Value {
    if bytes.is_empty() {
        return json!({});
    }
    serde_json::from_slice(bytes).unwrap_or(Value::Null)
}

/// `GET /calls`: the call that is up for the phone that asks, or none. The administrator
/// may name the phone (`?principal=`).
async fn snapshot(
    State(app): State<App>,
    Extension(principal): Extension<Principal>,
    Query(asked): Query<HashMap<String, String>>,
) -> Response {
    let caller = match caller(&principal) {
        Ok(caller) => caller,
        Err(refused) => return refused.into_response(),
    };
    let named = match caller.as_str() {
        ADMINISTRATOR => asked.get("principal").cloned().unwrap_or_default(),
        _ => String::new(),
    };
    let asker = Asker {
        caller: &caller,
        principal: &named,
        ..Asker::default()
    };
    let desk = app.calls.clone();
    answered(
        desk.ask(&app, |calls, at, _, out| calls.snapshot(asker, at, out))
            .await,
    )
}

/// `POST /calls` with `{agents: [...], request_id?, principal?, by?}`: a call with one to
/// eight agents; the first has the floor.
async fn start(
    State(app): State<App>,
    Extension(principal): Extension<Principal>,
    bytes: Bytes,
) -> Response {
    let caller = match caller(&principal) {
        Ok(caller) => caller,
        Err(refused) => return refused.into_response(),
    };
    let said = body(&bytes);
    let Some(agents) = said["agents"].as_array() else {
        return ApiError::new(StatusCode::BAD_REQUEST, "agents must be a list").into_response();
    };
    let agents: Vec<String> = agents
        .iter()
        .map(|agent| match agent {
            Value::String(name) => name.clone(),
            Value::Number(number) => number.to_string(),
            _ => String::new(),
        })
        .collect();
    let (request_id, named, by) = (
        text(&said, "request_id"),
        text(&said, "principal"),
        by(&app, &said),
    );
    let asker = Asker {
        caller: &caller,
        principal: &named,
        by: &by,
        ..Asker::default()
    };
    let desk = app.calls.clone();
    let asked = desk.ask(&app, |calls, at, with, out| {
        calls.start(asker, (&agents, &request_id, None), with, at, out)
    });
    answered(asked.await)
}

/// `POST /calls/<action>` with `{agent, call_id?, principal?, by?}`: add, remove, hold,
/// resume, switch to or transfer to one agent; and `POST /calls/end`.
async fn change(
    State(app): State<App>,
    Extension(principal): Extension<Principal>,
    action: String,
    bytes: Bytes,
) -> Response {
    let caller = match caller(&principal) {
        Ok(caller) => caller,
        Err(refused) => return refused.into_response(),
    };
    let said = body(&bytes);
    if !said.is_object() {
        return ApiError::new(StatusCode::BAD_REQUEST, "bad json").into_response();
    }
    let (call_id, named, by) = (
        text(&said, "call_id"),
        text(&said, "principal"),
        by(&app, &said),
    );
    let agent = text(&said, "agent");
    let asker = Asker {
        caller: &caller,
        call_id: &call_id,
        principal: &named,
        by: &by,
    };
    let desk = app.calls.clone();
    let asked = desk.ask(&app, |calls, at, (roster, _), out| match action.as_str() {
        "end" => calls.end(asker, at, out),
        action => calls.change(asker, (action, &agent), roster, at, out),
    });
    answered(asked.await)
}

// ---- rings ---------------------------------------------------------------------------------

/// Who asks about a ring, and what the request says: an object, or the refusal.
fn ring_asked(principal: &Principal, bytes: &Bytes) -> Result<(String, Value), ApiError> {
    let caller = caller(principal)?;
    let said = body(bytes);
    if !said.is_object() {
        return Err(ApiError::new(StatusCode::BAD_REQUEST, "bad json"));
    }
    Ok((caller, said))
}

/// `POST /calls/ring` with `{reason, opening_line, agent}`: the owner's own tools ring for
/// the agent they name. (An agent rings as itself with its own tool, `clarp-admin call ring`.)
async fn ring(
    State(app): State<App>,
    Extension(principal): Extension<Principal>,
    bytes: Bytes,
) -> Response {
    let said = match ring_asked(&principal, &bytes) {
        Ok((_, said)) => said,
        Err(refused) => return refused.into_response(),
    };
    let desk = app.calls.clone();
    answered(desk.ring(&app, "", &said).await)
}

/// `GET /calls/ring`: the ring that rings now, or with `?ring_id=` that one, and the
/// owner's switches.
async fn ring_look(
    State(app): State<App>,
    Extension(principal): Extension<Principal>,
    Query(asked): Query<HashMap<String, String>>,
) -> Response {
    let caller = match caller(&principal) {
        Ok(caller) => caller,
        Err(refused) => return refused.into_response(),
    };
    let ring_id = asked.get("ring_id").cloned().unwrap_or_default();
    let desk = app.calls.clone();
    // What a client is shown is what it may be told of: a live ring only a phone that can
    // hold one, a call the user started only its own phone.
    let looked = desk.rings(&app, |rings, _, with, (out, _)| {
        rings.look_as((&ring_id, &caller), &with.world, with.at.now_ms, out)
    });
    answered(looked.await)
}

/// `POST /calls/ring/answer` with `{ring_id}`: the user takes the call. A call starts with
/// the agent on the floor, and the answer hands back the opening line to play first.
async fn ring_answer(
    State(app): State<App>,
    Extension(principal): Extension<Principal>,
    bytes: Bytes,
) -> Response {
    let (caller, said) = match ring_asked(&principal, &bytes) {
        Ok(asked) => asked,
        Err(refused) => return refused.into_response(),
    };
    let ring_id = text(&said, "ring_id");
    let desk = app.calls.clone();
    let asked = rings::Answer {
        principal: &caller,
        ring_id: &ring_id,
        conversation_mode: &said["conversation_mode"],
        opening_mode: said.get("opening_mode"),
        request_id: &said["request_id"],
        client_nonce: &said["client_nonce"],
    };
    // A ring is answered as it was placed: with a call, or with the grant of a live one.
    let names = live::Names::new();
    let taken = desk.rings(&app, |rings, calls, with, effects| {
        let given = rings::Given {
            roster: with.roster,
            new_id: with.new_id,
            minted: names.minted(),
        };
        rings.answer(calls, &asked, given, &with.world, with.at, effects)
    });
    answered(taken.await)
}

/// `POST /calls/ring/decline` with `{ring_id}`: the user does not take the call.
async fn ring_decline(
    State(app): State<App>,
    Extension(principal): Extension<Principal>,
    bytes: Bytes,
) -> Response {
    let (caller, said) = match ring_asked(&principal, &bytes) {
        Ok(asked) => asked,
        Err(refused) => return refused.into_response(),
    };
    let ring_id = text(&said, "ring_id");
    let desk = app.calls.clone();
    let declined = desk.rings(&app, |rings, _, with, (out, _)| {
        rings.decline((&caller, &ring_id), with.at.now_ms, out)
    });
    answered(declined.await)
}

/// `POST /calls/ring/cancel` with `{ring_id}`: the ring is taken back before the user
/// answers, by the owner's own tools. (An agent takes back its own with its own tool.)
async fn ring_cancel(
    State(app): State<App>,
    Extension(principal): Extension<Principal>,
    bytes: Bytes,
) -> Response {
    let said = match ring_asked(&principal, &bytes) {
        Ok((_, said)) => said,
        Err(refused) => return refused.into_response(),
    };
    let ring_id = text(&said, "ring_id");
    let desk = app.calls.clone();
    let cancelled = desk.rings(&app, |rings, _, with, (out, _)| {
        rings.cancel(("", &ring_id), with.at.now_ms, out)
    });
    answered(cancelled.await)
}

/// `GET /calls/ring/settings`: the owner's switches for rings, and the newest of the trail.
async fn ring_settings(
    State(app): State<App>,
    Extension(principal): Extension<Principal>,
) -> Response {
    if let Err(refused) = caller(&principal) {
        return refused.into_response();
    }
    let desk = app.calls.clone();
    let shown = desk.rings(&app, |rings, _, _, _| Ok(rings.shown()));
    answered(shown.await)
}

/// `POST /calls/ring/settings` with `{enabled}` or `{show_reason}`: the owner turns rings
/// on or off, or says whether a locked phone shows why an agent calls.
async fn ring_settings_change(
    State(app): State<App>,
    Extension(principal): Extension<Principal>,
    bytes: Bytes,
) -> Response {
    let said = match ring_asked(&principal, &bytes) {
        Ok((_, said)) => said,
        Err(refused) => return refused.into_response(),
    };
    let desk = app.calls.clone();
    let changed = desk.rings(&app, |rings, _, with, (out, _)| {
        rings.change(&said, with.at.now_ms, out)
    });
    answered(changed.await)
}

/// `POST /calls/ring/voip-token` with `{token, environment}`: a phone leaves the token an
/// incoming call would be pushed to. It is kept; this Host has no key of its own to push a
/// call with, so a ring reaches the phone as a notification that breaks through Focus.
async fn ring_voip_token(
    State(app): State<App>,
    Extension(principal): Extension<Principal>,
    bytes: Bytes,
) -> Response {
    let (caller, said) = match ring_asked(&principal, &bytes) {
        Ok(asked) => asked,
        Err(refused) => return refused.into_response(),
    };
    let (token, environment) = (text(&said, "token"), text(&said, "environment"));
    let desk = app.calls.clone();
    let left = desk.rings(&app, |rings, _, with, (out, _)| {
        let modes = &said["supported_conversation_modes"];
        rings.voip_token(&caller, (&token, &environment, modes), with.at.now_ms, out)
    });
    answered(left.await)
}

/// What of rings this Host does not have: a ring that listens first. Said in words, and
/// nothing is done.
async fn not_this_ring() -> ApiError {
    ApiError::new(
        StatusCode::NOT_IMPLEMENTED,
        "This Host has no ring that listens first",
    )
}

pub fn routes() -> Router<App> {
    // A route for each change there is: anything else under `/calls/` is no route, as on
    // the Python Host.
    let changes = [
        "add", "remove", "hold", "resume", "switch", "transfer", "end",
    ];
    let router = Router::new()
        .route("/calls", get(snapshot).post(start))
        .route("/calls/ring", get(ring_look).post(ring))
        .route("/calls/ring/answer", post(ring_answer))
        .route("/calls/ring/decline", post(ring_decline))
        .route("/calls/ring/cancel", post(ring_cancel))
        .route(
            "/calls/ring/settings",
            get(ring_settings).post(ring_settings_change),
        )
        .route("/calls/ring/voip-token", post(ring_voip_token));
    let router = live::routes(router).route(
        "/calls/ring/{*rest}",
        get(not_this_ring).post(not_this_ring),
    );
    changes.iter().fold(router, |router, action| {
        let (path, action) = (format!("/calls/{action}"), (*action).to_string());
        let change = move |app: State<App>, principal: Extension<Principal>, bytes: Bytes| {
            change(app, principal, action.clone(), bytes)
        };
        router.route(&path, post(change))
    })
}
