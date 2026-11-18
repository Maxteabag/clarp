//! A call of the Oracle, against the Python Host's own conversation.
//!
//! `fixtures/oracle-conversation-vectors.json` is written by
//! `server-rs/tools/oracle-conversation-vectors.py`: the Python Host's
//! `oracle_live_stable.Conversation` driven through stories with a voice, a phone, a router,
//! the tools of the fleet, a memory and two clocks made up there. After every step of a story
//! it has what the voice and the phone were told, what was kept of the call and how the call
//! stands. The same must come of the same here: the machine of the kernel is taken through
//! each story, and through each routing as the Host would carry it, a step at a time.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::BTreeMap;

use clarp_kernel::oracle::conversation::{
    Begin, Conversation, Ids, Now, Phoned, Strategy, Thread, Waiting, Wire,
};
use clarp_kernel::oracle::delegations::{Delegation, Status};
use clarp_kernel::oracle::routing::{self, Fed, Step};
use clarp_kernel::oracle::voice::{self, Beginning};
use clarp_kernel::oracle::{attention, earcons, phone};
use clarp_kernel::python;
use serde_json::{Value, json};

/// The wall's time when every story begins.
const WALL: i64 = 1_800_000_000_000;

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/oracle-conversation-vectors.json")).unwrap()
}

/// What the file has written short is written out again: one sign many times over, and the
/// sound of a cue.
fn whole(value: &Value) -> Value {
    match value {
        Value::Object(fields) if fields.len() == 1 && fields.contains_key("runs") => {
            let mut text = String::new();
            for part in fields["runs"].as_array().unwrap() {
                match part {
                    Value::String(as_it_is) => text += as_it_is,
                    run => {
                        let times = usize::try_from(run[1].as_u64().unwrap()).unwrap();
                        text += &run[0].as_str().unwrap().repeat(times);
                    }
                }
            }
            json!(text)
        }
        Value::Object(fields) if fields.len() == 1 && fields.contains_key("earcon") => {
            let cue = &fields["earcon"];
            let sound = earcons::pcm(cue[0].as_str().unwrap(), cue[1].as_str().unwrap());
            json!(python::b64encode(&sound.unwrap()))
        }
        Value::Object(fields) => {
            let fields = fields
                .iter()
                .map(|(name, value)| (name.clone(), whole(value)));
            Value::Object(fields.collect())
        }
        Value::Array(items) => Value::Array(items.iter().map(whole).collect()),
        plain => plain.clone(),
    }
}

/// What a conversation told the two sides of its call.
#[derive(Default)]
struct Told {
    voice: Vec<Value>,
    phone: Vec<Value>,
}

impl Wire for Told {
    fn hook(&mut self, hook: clarp_kernel::oracle::conversation::Hook) {
        self.voice.push(json!({"hook":hook}));
    }
    fn voice(&mut self, said: Value) {
        self.voice.push(said);
    }

    fn phone(&mut self, said: Value) {
        self.phone.push(said);
    }
}

/// A delegation as the book has it, of a row of the file.
fn delegation(row: &Value) -> Delegation {
    let text = |name: &str| row[name].as_str().unwrap_or("").to_string();
    Delegation {
        delegation_id: text("delegation_id"),
        owner_principal: "device-phone-1".to_string(),
        trace_id: String::new(),
        client_msg_id: String::new(),
        agent_id: text("agent_id"),
        session: text("session"),
        backend_session_id: text("backend_session_id"),
        request_text: text("request_text"),
        status: match row["status"].as_str().unwrap() {
            "accepted" => Status::Accepted,
            "queued" => Status::Queued,
            "completed" => Status::Completed,
            "failed" => Status::Failed,
            "cancelled" => Status::Cancelled,
            other => panic!("no such status: {other}"),
        },
        result_message_id: row["result_message_id"].as_str().map(str::to_string),
        result_text: text("result_text"),
        error: text("error"),
        created_at: row["created_at"].as_i64().unwrap_or(0),
        updated_at: 0,
        delivered_at: None,
    }
}

/// How a call stands, as the file says it of the Python Host's.
fn state(call: &Conversation) -> Value {
    let relay = call.active_relay().map(|relay| {
        json!({
            "key": relay.key, "next": relay.next, "sent": relay.sent, "auto": relay.auto,
            "held": relay.held, "total": relay.total(), "verbatim": relay.verbatim,
        })
    });
    let cues: Vec<Value> = call
        .cues()
        .iter()
        .map(|(name, persona)| {
            if persona.is_empty() {
                json!([name, null])
            } else {
                json!([name, persona])
            }
        })
        .collect();
    json!({
        "revision": call.revision(), "routed_revision": call.routed_revision(),
        "narration": call.narration(), "earcons": call.earcons(),
        "opening_answered": call.opening_answered(), "background_work": call.background(),
        "results_seen": call.results_seen(), "fragments": call.fragments().len(),
        "routing": call.routing(), "held": call.held(), "owned": call.owned(),
        "pending": call.pending().collect::<Vec<_>>(), "cues": cues,
        "speaking": call.speaking(), "progress_interval": call.progress_interval(),
        "relay": relay, "relays": call.relays().collect::<Vec<_>>(),
    })
}

/// One story as it is played here.
struct Played<'a> {
    vectors: &'a Value,
    opened: &'a Value,
    call: Conversation,
    ms: i64,
    told: Told,
    /// The rows of the book of delegations, by their ids.
    rows: BTreeMap<String, Value>,
    /// A result that lands in the book right after a stop has read it.
    landing: Option<Value>,
    decisions: Vec<Value>,
    completions: Vec<Value>,
    /// The actions that were written down as begun, by the number of the talk and their place.
    admissions: BTreeMap<(i64, usize), Value>,
    counted: Counted,
}

/// What the stories were seen to hold, so that a file that lost its cases is noticed.
#[derive(Default, Debug, PartialEq)]
struct Counted {
    steps: usize,
    routings: usize,
    asked: usize,
    done: usize,
    written: usize,
    again: usize,
    failed: usize,
    claims: usize,
    landed: usize,
}

impl<'a> Played<'a> {
    fn begun(vectors: &'a Value, opened: &'a Value) -> Self {
        // What was kept of the call before, with its times told from the story's beginning.
        let mut saved = opened.get("saved").cloned().unwrap_or_else(|| json!({}));
        if let Some(active) = saved["call_active_ms"].as_i64() {
            saved["call_active_ms"] = json!(active + WALL);
        }
        let texts = |name: &str| -> Vec<String> {
            let listed = opened[name].as_array().into_iter().flatten();
            listed
                .map(|text| text.as_str().unwrap().to_string())
                .collect()
        };
        let work = texts("work");
        let contexts = opened["contexts"].as_array().cloned().unwrap_or_default();
        let told_before: Vec<String> = opened["claimed"]
            .as_array()
            .into_iter()
            .flatten()
            .map(|pair| {
                format!(
                    "{}:{}",
                    pair[0].as_str().unwrap(),
                    pair[1].as_str().unwrap()
                )
            })
            .collect();
        let thread = Thread {
            thread_id: vectors["thread"].as_str().unwrap(),
            saved: &saved,
            work: &work,
            contexts: &contexts,
            told: &told_before,
        };
        let begin = Begin {
            provider_session: "conn-1",
            principal: vectors["owner"].as_str().unwrap(),
            strategy: Strategy::named(opened["strategy"].as_str()).unwrap(),
            contact: opened["fallback"].as_str().unwrap_or("theo"),
            direct_prefix: "prefix",
            thread,
            ids: Ids::new("e-"),
        };
        let mut call = Conversation::begin(begin, WALL);
        if opened["provider"] == "gemini" {
            call.use_gemini();
        }
        Self {
            vectors,
            opened,
            call,
            ms: 0,
            told: Told::default(),
            rows: BTreeMap::new(),
            landing: None,
            decisions: Vec::new(),
            completions: Vec::new(),
            admissions: BTreeMap::new(),
            counted: Counted::default(),
        }
    }

    fn now(&self) -> Now {
        Now {
            ms: self.ms,
            wall_ms: WALL + self.ms,
        }
    }

    /// The delegations of the call, as the book has them: in the order of their ids, as the
    /// file's stand-in for the tools tells them.
    fn book(&self) -> Vec<Delegation> {
        let owned = self.call.owned().iter();
        owned
            .filter_map(|id| self.rows.get(id))
            .map(delegation)
            .collect()
    }

    fn voice(&mut self, said: &Value) {
        let now = self.now();
        self.call.voice(&whole(said), &mut self.told, now);
    }

    fn step(&mut self, step: &Value, name: &str) {
        let book = self.book();
        let now = self.now();
        match step["do"].as_str().unwrap() {
            "open" => {}
            "wait" => {
                let seconds = step["seconds"].as_f64().unwrap();
                #[allow(clippy::cast_possible_truncation)]
                let ms = (seconds * 1000.0).round() as i64;
                self.ms += ms;
            }
            "phone" => {
                let raw = step["raw"].as_str().unwrap();
                let taken = phone::event(raw);
                assert_eq!(
                    taken.clone().unwrap_or(Value::Null),
                    step["event"],
                    "{name}: {raw}"
                );
                let phoned = self.call.phone(raw, &mut self.told, now);
                let expected = match &taken {
                    None => Phoned::Unsupported,
                    Some(event) if event["type"] == "session.close" => Phoned::Close {
                        handoff_id: event["handoff_id"].as_str().map(str::to_string),
                    },
                    Some(_) => Phoned::Taken,
                };
                assert_eq!(phoned, expected, "{name}: {raw}");
            }
            "voice" => {
                self.voice(&step["said"]);
                assert_eq!(json!(self.call.wanted()), step["to_route"], "{name}");
                assert_eq!(json!(self.call.closed()), step["closed"], "{name}");
            }
            "tick" => {
                for looked in step["looked"].as_array().unwrap() {
                    // What waits for the user is read when the call says it is the moment,
                    // and only then: at the ticks the Python Host looks for it.
                    let looks = self.call.tick(&mut self.told, now);
                    assert_eq!(json!(looks), *looked, "{name}: at {} ms", self.ms);
                    let waiting = looks.then_some(Waiting {
                        decisions: &self.decisions,
                        completions: &self.completions,
                    });
                    let before = self.told.voice.len();
                    let claim = self.call.ticked(&book, waiting, &mut self.told, now);
                    if let Some(claim) = claim {
                        // What was claimed is what the voice was just told of.
                        let told = self.told.voice[before]["content"].as_str().unwrap();
                        assert!(told.contains(&python::compact(&json!(claim.id))), "{name}");
                        let row = self.decisions.iter().chain(&self.completions).find(|row| {
                            row["decision_id"] == claim.id.as_str()
                                || row["source_message_id"] == claim.id.as_str()
                        });
                        let row = row.unwrap();
                        let (kind, reference) = if row.get("decision_id").is_some() {
                            ("decision", &row["updated_at"])
                        } else {
                            ("completion", &row["reference_ts"])
                        };
                        assert_eq!(claim.kind, kind, "{name}");
                        assert_eq!(json!(claim.reference_ts), *reference, "{name}");
                        self.counted.claims += 1;
                    }
                }
            }
            "result" => {
                let id = step["row"]["delegation_id"].as_str().unwrap().to_string();
                self.rows.insert(id, whole(&step["row"]));
            }
            "lands" => self.landing = Some(whole(&step["row"])),
            "owns" => {
                let opened = self.opened;
                let later = opened["owned_later"].as_array().into_iter().flatten();
                for id in step["ids"].as_array().unwrap().iter().chain(later) {
                    self.call.owns(id.as_str().unwrap());
                }
            }
            "attention" => {
                self.decisions = whole(&step["decisions"]).as_array().unwrap().clone();
                self.completions = whole(&step["completions"]).as_array().unwrap().clone();
                let texts = whole(&step["texts"]);
                for row in &self.decisions {
                    let id = row["decision_id"].as_str().unwrap();
                    let text = attention::decision_text(row);
                    assert_eq!(json!(text), texts[format!("decision:{id}")], "{name}: {id}");
                }
                for row in &self.completions {
                    let id = row["source_message_id"].as_str().unwrap();
                    let text = attention::completion_text(row);
                    assert_eq!(
                        json!(text),
                        texts[format!("completion:{id}")],
                        "{name}: {id}"
                    );
                }
            }
            "call" => self.called(step, &book, name),
            "route" => self.routed(step, name),
            other => panic!("no such step: {other}"),
        }
    }

    /// Something of the Host's own is asked of the conversation.
    fn called(&mut self, step: &Value, book: &[Delegation], name: &str) {
        let said = whole(&step["said"]);
        let text = |name: &str| said[name].as_str().unwrap_or("");
        let (now, told) = (self.now(), &mut self.told);
        let mut answered = Value::Null;
        match step["method"].as_str().unwrap() {
            "cue" => self.call.cue(text("name"), text("persona"), told, now),
            "handoff_offer" => self.call.handoff_offer(text("persona"), told, now),
            "handoff_mirror" => self.call.handoff_mirror(&said["record"], told),
            "handoff_hold" => self.call.handoff_hold(),
            "handoff_resume" => self.call.handoff_resume(),
            "handoff_failed" => {
                let failed = (text("persona"), text("reason"), said["note"] == true);
                self.call.handoff_failed(failed, told, now);
            }
            "handoff_returned" => self.call.handoff_returned(text("persona"), told, now),
            "host_event" => self.call.host_event(&said["fact"], told),
            "read_result" => {
                let which = (&said["operation_id"], &said["from_part"], &said["verbatim"]);
                answered = json!(self.call.read_result(which, book, told, now));
            }
            "deliver_tool_output" => {
                let delivered =
                    self.call
                        .deliver_tool_output(text("name"), &said["output"], told, now);
                delivered.unwrap();
            }
            "append" => self.call.append(text("kind"), text("content"), told),
            "supersede" => {
                // The stop reads the book; what lands after that read is not what it read.
                self.call.supersede(text("session"), book, now);
                if let Some(row) = self.landing.take() {
                    let id = row["delegation_id"].as_str().unwrap().to_string();
                    self.rows.insert(id, row);
                    self.counted.landed += 1;
                }
            }
            other => panic!("no such thing to ask: {other}"),
        }
        assert_eq!(answered, step["answered"], "{name}");
    }

    /// A routing, carried through its steps as a Host carries it: with the router's answers
    /// and the tools' of the story, and what the voice says meanwhile.
    fn routed(&mut self, step: &Value, name: &str) {
        let ident = step["ident"].as_str().unwrap();
        assert_eq!(self.call.next_wanted().as_deref(), Some(ident), "{name}");
        assert_eq!(json!(self.ms), step["began_ms"], "{name}");
        let (vectors, opened) = (self.vectors, self.opened);
        let roster = &vectors["roster"];
        let contact = opened["fallback"].as_str().unwrap_or("theo");
        let gone = opened["gone"].as_array().into_iter().flatten();
        let contact_there = gone.clone().all(|gone| gone != contact);
        let mut asked = step["router_asked"].as_array().unwrap().iter();
        let mut answers = step["router"].as_array().unwrap().iter();
        let mut executed = step["executed"].as_array().unwrap().iter();
        let mut meanwhile = step["meanwhile"].as_object().unwrap().clone();
        let mut provenance = Value::Null;
        let mut route = self.call.route(ident);
        assert_eq!(route.ident(), ident);
        let mut fed_owned;
        let mut fed = Fed::Begin;
        self.counted.routings += 1;
        loop {
            let (book, now) = (self.book(), self.now());
            let stepped = self
                .call
                .route_step(&mut route, fed, &book, &mut self.told, now);
            for begun in self.call.begun() {
                let row = json!({
                    "revision": begun.revision, "action_index": begun.index,
                    "call_id": begun.call_id, "tool": begun.tool,
                    "arguments_json": begun.arguments, "status": "planned", "result": null,
                });
                assert!(
                    self.admissions
                        .insert((begun.revision, begun.index), row)
                        .is_none()
                );
            }
            match stepped {
                Step::Over => break,
                Step::Wait => {
                    // As the Python Host waits: it looks every 50 ms, and once at least.
                    self.counted.again += usize::from(!matches!(fed, Fed::Begin));
                    self.ms += 50;
                    while self.ms < self.call.quiet_at() {
                        self.ms += 50;
                    }
                    fed = Fed::Quiet {
                        roster,
                        contact_there,
                    };
                }
                Step::Ask(question) => {
                    let recorded = asked.next().expect("the router was asked once more here");
                    assert_eq!(json!(self.ms), recorded["at_ms"], "{name}");
                    let body = routing::request(&question);
                    let input: Value =
                        serde_json::from_str(body["input"].as_str().unwrap()).unwrap();
                    assert_eq!(input, whole(&recorded["input"]), "{name}");
                    // And in the letters of the talk as they are.
                    let plain = body["input"].as_str().unwrap().is_ascii();
                    assert_eq!(json!(plain), recorded["ascii"], "{name}");
                    let router = &vectors["router"];
                    assert_eq!(body["model"], recorded["model"]);
                    assert_eq!(body["tools"], router["tools"]);
                    assert_eq!(body["instructions"], router["instructions"]);
                    let mut rest = body.as_object().unwrap().clone();
                    for told_apart in ["model", "input", "tools", "instructions"] {
                        rest.shift_remove(told_apart);
                    }
                    assert_eq!(Value::Object(rest), recorded["rest"], "{name}");
                    self.counted.asked += 1;
                    // A router takes its time, and the voice may say something meanwhile.
                    let answer = answers.next().unwrap();
                    self.ms += 10;
                    for event in answer["during"].as_array().unwrap() {
                        self.voice(&event["said"]);
                    }
                    self.ms += 20;
                    if answer["fails"].is_null() {
                        let whole_answer = answer.get("body").cloned();
                        fed_owned =
                            whole_answer.unwrap_or_else(|| json!({ "output": answer["output"] }));
                        fed = Fed::Answer(&fed_owned);
                    } else {
                        self.counted.failed += 1;
                        fed = Fed::Failed;
                    }
                }
                Step::Write(payload) => {
                    provenance = payload;
                    self.counted.written += 1;
                    let spoken = meanwhile.shift_remove("<written>").unwrap_or(json!([]));
                    for event in spoken.as_array().unwrap() {
                        self.voice(&event["said"]);
                    }
                    fed = Fed::Written("<written>");
                }
                Step::Do {
                    name: tool,
                    arguments,
                    call_id,
                    admitted,
                } => {
                    let recorded = executed.next().expect("a tool was asked once more here");
                    let ours = json!({
                        "name": tool, "arguments": arguments, "call_id": call_id,
                        "provenance": provenance.take(),
                    });
                    assert_eq!(ours, whole(recorded), "{name}");
                    self.counted.done += 1;
                    let spoken = meanwhile.shift_remove(tool.as_str()).unwrap_or(json!([]));
                    for event in spoken.as_array().unwrap() {
                        self.voice(&event["said"]);
                    }
                    // A tool that is not in the story is none there is, and a text is what it
                    // refuses with. Whatever else it answers was its answer.
                    match step["tools"].get(tool.as_str()) {
                        Some(answer) if !answer.is_string() => {
                            let row = self.admissions.get_mut(&admitted).unwrap();
                            row["status"] = json!("completed");
                            row["result"] = whole(answer);
                            fed_owned = whole(answer);
                            fed = Fed::Receipt(&fed_owned);
                        }
                        _ => {
                            self.counted.failed += 1;
                            fed = Fed::Failed;
                        }
                    }
                }
            }
        }
        // A routing that is over stays over, and is counted as over once.
        let (book, now, routing) = (self.book(), self.now(), self.call.routing());
        let again = self
            .call
            .route_step(&mut route, Fed::Begin, &book, &mut self.told, now);
        assert_eq!(
            (again, self.call.routing()),
            (Step::Over, routing),
            "{name}"
        );
        assert!(
            asked.next().is_none(),
            "{name}: the Python Host asked its router once more"
        );
        assert!(
            executed.next().is_none(),
            "{name}: the Python Host asked a tool once more"
        );
        assert_eq!(json!(answers.count()), step["answers_left"], "{name}");
        // What was written down as begun, and what came of it.
        let recorded = step["admissions"].as_array().unwrap().iter();
        let recorded: Vec<Value> = recorded
            .map(|row| {
                let mut row = row.as_object().unwrap().clone();
                let result = row.shift_remove("result_json").unwrap();
                let result = result
                    .as_str()
                    .map(|text| serde_json::from_str(text).unwrap());
                row.insert("result".to_string(), result.unwrap_or(Value::Null));
                Value::Object(row)
            })
            .collect();
        let ours: Vec<&Value> = self.admissions.values().collect();
        assert_eq!(json!(ours), json!(recorded), "{name}");
    }
}

#[test]
fn a_call_goes_as_the_python_hosts_goes_story_by_story() {
    let vectors = vectors();
    let mut counted = Counted::default();
    let stories = vectors["stories"].as_array().unwrap();
    for story in stories {
        let name = story["name"].as_str().unwrap();
        let mut played = Played::begun(&vectors, &story["opened"]);
        for (number, step) in story["steps"].as_array().unwrap().iter().enumerate() {
            let step = &whole(step);
            let at = format!("{name}, step {number} ({})", step["do"].as_str().unwrap());
            played.told = Told::default();
            played.step(step, &at);
            if let Some(at_ms) = step.get("at_ms") {
                assert_eq!(json!(played.ms), *at_ms, "{at}");
            }
            assert_eq!(
                json!(played.told.voice),
                whole(&step["voice"]),
                "{at}: to the voice"
            );
            assert_eq!(
                json!(played.told.phone),
                whole(&step["phone"]),
                "{at}: to the phone"
            );
            let kept = played.call.to_keep().unwrap_or(Value::Null);
            assert_eq!(kept, whole(&step["saved"]), "{at}: what is kept");
            assert_eq!(
                state(&played.call),
                step["state"],
                "{at}: how the call stands"
            );
            played.counted.steps += 1;
        }
        let of_story = played.counted;
        counted = Counted {
            steps: counted.steps + of_story.steps,
            routings: counted.routings + of_story.routings,
            asked: counted.asked + of_story.asked,
            done: counted.done + of_story.done,
            written: counted.written + of_story.written,
            again: counted.again + of_story.again,
            failed: counted.failed + of_story.failed,
            claims: counted.claims + of_story.claims,
            landed: counted.landed + of_story.landed,
        };
    }
    assert_eq!(stories.len(), 80);
    assert_eq!(
        counted,
        Counted {
            steps: 1623,
            routings: 73,
            asked: 56,
            done: 33,
            written: 21,
            again: 3,
            failed: 4,
            claims: 10,
            landed: 1,
        }
    );
}

#[test]
fn a_piece_of_sound_from_a_phone_is_taken_up_to_its_longest() {
    let vectors = vectors();
    let mut taken = 0;
    for case in vectors["longest_sound"].as_array().unwrap() {
        let chars = usize::try_from(case["chars"].as_u64().unwrap()).unwrap();
        let raw = json!({ "type": "session.input_audio.append", "audio": "A".repeat(chars) });
        let event = phone::event(&raw.to_string());
        assert_eq!(json!(event.is_some()), case["taken"], "{chars}");
        taken += usize::from(event.is_some());
    }
    assert_eq!(taken, 2);
    assert_eq!(json!(phone::SOUND_SIGNS), json!(262_144));
}

#[test]
fn the_voice_is_told_what_the_python_host_tells_it_when_a_call_begins() {
    let vectors = vectors();
    let configs = vectors["configs"].as_array().unwrap();
    for case in configs {
        let languages: Vec<String> = case["languages"]
            .as_array()
            .unwrap()
            .iter()
            .map(|code| code.as_str().unwrap().to_string())
            .collect();
        let history = case["history"].as_array().unwrap();
        let told = Beginning {
            roster: Some(&case["roster"]).filter(|roster| !roster.is_null()),
            direct: case["delegation_strategy"] == "direct_contact",
            vocabulary: case["voice_context"].get("terms"),
            history,
            languages: &languages,
        };
        assert_eq!(voice::beginning(&told), case["config"], "{case}");
    }
    assert_eq!(configs.len(), 14);
    // What the two models are told as it is, is the Python Host's own.
    assert_eq!(*voice::router(), vectors["router"]);
    assert_eq!(json!(voice::narration(true)), vectors["narration_off"]);
    assert_eq!(json!(voice::narration(false)), vectors["narration_on"]);
}

#[test]
fn a_call_is_begun_as_it_is_asked_for() {
    // How what the user asks becomes work is named by a call: a name there is not is none.
    assert_eq!(Strategy::named(None), Some(Strategy::Operator));
    assert_eq!(Strategy::named(Some("operator")), Some(Strategy::Operator));
    assert_eq!(
        Strategy::named(Some("direct_contact")),
        Some(Strategy::DirectContact)
    );
    assert_eq!(Strategy::named(Some("anything")), None);
    assert_eq!(
        (Strategy::Operator.word(), Strategy::DirectContact.word()),
        ("operator", "direct_contact")
    );
    // A Host whose owner wants no cues plays none, whatever was kept of the thread.
    let saved = json!({ "earcons": true });
    let thread = Thread {
        thread_id: "a-thread",
        saved: &saved,
        work: &[],
        contexts: &[],
        told: &[],
    };
    let begin = Begin {
        provider_session: "conn-1",
        principal: "a-phone",
        strategy: Strategy::Operator,
        contact: "theo",
        direct_prefix: "prefix",
        thread,
        ids: Ids::new("e-"),
    };
    let mut call = Conversation::begin(begin, WALL);
    let now = Now {
        ms: 0,
        wall_ms: WALL,
    };
    let mut told = Told::default();
    call.cue("result", "", &mut told, now);
    assert_eq!(told.phone.len(), 3, "a cue, its sound, and that it is over");
    call.without_earcons();
    let mut told = Told::default();
    call.cue("result", "", &mut told, now);
    assert!(told.phone.is_empty() && call.cues().is_empty() && !call.earcons());
}

#[test]
fn what_waits_for_the_user_is_shown_to_a_voice_as_the_python_host_shows_it() {
    let vectors = whole(&vectors());
    let listed = vectors["listed"].as_array().unwrap();
    let (mut shown, mut refused) = (0, 0);
    for case in vectors["pending"].as_array().unwrap() {
        // A number that is none is none a Host of this kind can be handed.
        let Some(most) = case["limit"].as_u64().filter(|_| case["limit"].is_u64()) else {
            assert_eq!(
                case["raises"], "limit must be a non-negative integer",
                "{case}"
            );
            refused += 1;
            continue;
        };
        let pending = attention::pending(listed, usize::try_from(most).unwrap());
        assert_eq!(
            python::dumps(&json!(pending)),
            python::dumps(&case["pending"]),
            "{most}"
        );
        shown += pending.len();
    }
    assert_eq!((shown, refused, attention::PENDING_MOST), (53, 4, 20));
}

#[test]
fn the_languages_an_owner_wrote_are_taken_as_the_python_host_takes_them() {
    let vectors = vectors();
    let mut taken = 0;
    for case in vectors["languages"].as_array().unwrap() {
        let codes = voice::languages(&case["written"]);
        assert_eq!(json!(codes), case["taken"], "{case}");
        taken += codes.len();
    }
    assert_eq!(
        (vectors["languages"].as_array().unwrap().len(), taken),
        (11, 13)
    );
}
