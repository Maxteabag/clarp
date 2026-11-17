//! The account pool, held to what the Python Host's own coordinator did in made-up situations
//! (`tests/fixtures/account-pool-vectors.json`, written by
//! `server-rs/tools/account-pool-vectors.py`): what each call answered, what was done at each
//! step and in which order, and how the pool stood afterwards.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::BTreeMap;

use clarp_kernel::accounts::{
    self, Checked, Decision, Effect, Pool, Starting, Timer, Verdict, Work,
};
use serde_json::{Value, json};

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/account-pool-vectors.json")).unwrap()
}

/// Seconds, as the Python Host counts them, in this Host's milliseconds.
fn ms(seconds: &Value) -> i64 {
    let seconds = seconds.as_f64().unwrap();
    let ms = (seconds * 1000.0).round();
    assert!(
        (ms - seconds * 1000.0).abs() < 1e-6,
        "{seconds} is no whole millisecond"
    );
    format!("{ms:.0}").parse().unwrap()
}

/// A situation as it is played to the pool: the clock, the timers the pool set, and what was
/// done at the step under way, in the words of the vectors.
struct Played {
    pool: Pool,
    now: i64,
    timers: BTreeMap<u64, Timer>,
    set: u64,
    did: Vec<Value>,
}

impl Played {
    fn new() -> Self {
        Self {
            pool: Pool::new(),
            now: 1_000_000,
            timers: BTreeMap::new(),
            set: 0,
            did: Vec::new(),
        }
    }

    /// Writes down what the pool wants done, and does what a Host would do at once: a turn
    /// that is to be stopped is stopped (the Python coordinator stops it before it goes on).
    /// Answers the models the selector is to be asked for, where it is.
    fn done(&mut self, effects: Vec<Effect>, command: &Value) -> Option<Vec<String>> {
        let mut asked = None;
        for effect in effects {
            match effect {
                Effect::Pause { agent } => self.did.push(json!({ "pause": agent })),
                Effect::Hold { agent } => self.did.push(json!({ "hold": agent })),
                Effect::End { agent } => self.did.push(json!({ "end": agent })),
                Effect::Resume { agent, relapsed } => self.did.push(if relapsed {
                    json!({ "resume": agent, "relapsed": true })
                } else {
                    json!({ "resume": agent })
                }),
                // The Python coordinator stops a turn in place, and sets no timer for it.
                Effect::Timer {
                    timer: Timer::Stops,
                    ..
                } => {}
                Effect::Timer { timer, after_ms } => {
                    self.set += 1;
                    self.timers.insert(self.set, timer);
                    let what = match timer {
                        Timer::Recovery => "recovery",
                        Timer::Held(_) | Timer::Stops => "held",
                    };
                    let after = f64::from(i32::try_from(after_ms).unwrap()) / 1000.0;
                    self.did
                        .push(json!({ "timer": self.set, "for": what, "after": after }));
                }
                Effect::Ask { models } => {
                    self.did.push(json!({ "ask": models, "command": command }));
                    asked = Some(models);
                }
                Effect::Stop { agent } => {
                    self.did.push(json!({ "stop": agent }));
                    let more = self.pool.stopped(&agent, self.now);
                    asked = self.done(more, command).or(asked);
                }
            }
        }
        asked
    }

    /// One step of a situation; what the call answered.
    fn step(&mut self, step: &Value, command: &Value, what: &str) -> Value {
        let text = |key: &str| step[key].as_str().unwrap();
        match text("do") {
            "register" => {
                let turn = Starting {
                    agent: text("agent"),
                    trace: text("trace"),
                    model: text("model"),
                    holds: step["hold"].as_bool().unwrap_or(true),
                    ends: step["fail"].as_bool().unwrap_or(true),
                };
                let (waits, effects) = self.pool.register(turn, self.now);
                assert_eq!(self.done(effects, command), None, "{what}");
                json!(waits)
            }
            "request" => {
                let selector = step.get("command").is_none_or(|own| own != &json!([]));
                let plan = step["plan"].as_bool().unwrap_or(false);
                let (taken, effects) =
                    self.pool
                        .request(text("agent"), text("trace"), selector, plan, self.now);
                assert_eq!(self.done(effects, command), None, "{what}");
                json!(taken)
            }
            "fire" => {
                let timer = self.timers.remove(&step["timer"].as_u64().unwrap());
                let timer = timer.unwrap_or_else(|| panic!("{what}: no such timer"));
                let effects = self.pool.fired(timer, self.now);
                let asked = self.done(effects, command);
                assert_eq!(
                    asked.is_some(),
                    step.get("verdict").is_some(),
                    "{what}: asked"
                );
                if let Some(models) = asked {
                    // What happens while the selector runs.
                    for inner in step["during"].as_array().into_iter().flatten() {
                        let at = self.did.len();
                        self.did.push(json!({ "meanwhile": inner }));
                        let answered = self.step(inner, command, what);
                        self.did[at]["answered"] = answered;
                    }
                    let verdict = match &step["verdict"] {
                        Value::Bool(true) => Verdict::All,
                        Value::Bool(false) => Verdict::NoAccount,
                        Value::Null => Verdict::NoAnswer,
                        Value::String(raised) if raised == "raise" => Verdict::Failed,
                        Value::Array(served) => Verdict::Only(
                            served
                                .iter()
                                .map(|m| m.as_str().unwrap().to_string())
                                .collect(),
                        ),
                        other => panic!("{what}: {other}"),
                    };
                    assert!(self.pool.checking(), "{what}: asked {models:?}");
                    let effects = self.pool.answered(verdict, self.now);
                    assert_eq!(self.done(effects, command), None, "{what}");
                }
                Value::Null
            }
            "advance" => {
                self.now += ms(&step["seconds"]);
                Value::Null
            }
            "disown" => {
                let effects = self.pool.disown(text("agent"), self.now);
                assert_eq!(self.done(effects, command), None, "{what}");
                Value::Null
            }
            "discard" => {
                let effects = self.pool.discard(text("agent"), text("trace"), self.now);
                assert_eq!(self.done(effects, command), None, "{what}");
                Value::Null
            }
            "served" => {
                let models = step["models"].as_array().unwrap();
                let serves = |model: &str| models.iter().any(|served| served == model);
                let (back, effects) = self.pool.served(serves, self.now);
                assert_eq!(self.done(effects, command), None, "{what}");
                json!(back)
            }
            "parked" => json!(self.pool.parked(text("agent"), text("trace"))),
            "carried" => {
                let agents: Vec<String> = step["agents"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|agent| agent.as_str().unwrap().to_string())
                    .collect();
                let since = self.now - ms(&step["since"]);
                let (taken, effects) = self.pool.carried(true, since, &agents, self.now);
                assert_eq!(self.done(effects, command), None, "{what}");
                json!(taken)
            }
            other => panic!("{what}: a step nobody knows: {other}"),
        }
    }

    /// How the pool stands, in the words of the vectors.
    fn stood(&self) -> Value {
        let mut stood = self.pool.stood(self.now);
        let left = stood["next_check_in_ms"].as_i64().unwrap();
        let fields = stood.as_object_mut().unwrap();
        fields.remove("next_check_in_ms");
        fields.insert(
            "next_check_in".to_string(),
            json!(f64::from(i32::try_from(left).unwrap()) / 1000.0),
        );
        let timers: Vec<u64> = self.timers.keys().copied().collect();
        fields.insert("timers".to_string(), json!(timers));
        stood
    }
}

#[test]
fn the_pool_does_what_the_python_coordinator_did_in_every_situation() {
    let vectors = vectors();
    let command = &vectors["command"];
    let (mut steps, mut did) = (0, 0);
    let situations = vectors["situations"].as_array().unwrap();
    for situation in situations {
        let name = situation["name"].as_str().unwrap();
        let mut played = Played::new();
        for (at, step) in situation["steps"].as_array().unwrap().iter().enumerate() {
            let what = format!("{name}, step {at} ({})", step["step"]);
            played.did.clear();
            let answered = played.step(&step["step"], command, &what);
            assert_eq!(answered, step["answered"], "{what}: what the call answered");
            assert_eq!(
                json!(played.did),
                step["did"],
                "{what}: what was done, in order"
            );
            assert_eq!(played.stood(), step["stood"], "{what}: how the pool stood");
            assert_eq!(played.now - 1_000_000, ms(&step["at"]), "{what}: the clock");
            assert!(!played.pool.checking(), "{what}: a pass was left under way");
            steps += 1;
            did += step["did"].as_array().unwrap().len();
        }
    }
    // Counted by the script that wrote the vectors.
    assert_eq!((situations.len(), steps, did), (37, 431, 457));
}

#[test]
fn the_rules_are_the_python_coordinators() {
    let vectors = vectors();
    let rules = &vectors["rules"];
    assert_eq!(ms(&rules["recheck_seconds"]), accounts::RECHECK_MS);
    assert_eq!(ms(&rules["max_park_seconds"]), accounts::MAX_PARK_MS);
    assert_eq!(ms(&rules["quiet_seconds"]), accounts::QUIET_MS);
    assert_eq!(rules["max_relapses"], json!(accounts::MAX_RELAPSES));
    assert_eq!(rules["plan_checks"], json!(accounts::PLAN_CHECKS));
    assert_eq!(
        ms(&rules["carried_wait_seconds"]),
        accounts::CARRIED_WAIT_MS
    );
    assert_eq!(
        ms(&rules["carried_poll_seconds"]),
        accounts::CARRIED_POLL_MS
    );
}

#[test]
fn the_plan_decides_as_the_python_plan_does() {
    let vectors = vectors();
    let decisions = vectors["decisions"].as_array().unwrap();
    let now = 10_000_000;
    for vector in decisions {
        let held: Vec<(String, bool)> = vector["work"]
            .as_array()
            .unwrap()
            .iter()
            .map(|item| {
                (
                    item[0].as_str().unwrap().to_string(),
                    item[1].as_bool().unwrap(),
                )
            })
            .collect();
        let work: Vec<Work<'_>> = held
            .iter()
            .map(|(model, stopped)| Work {
                model,
                stopped: *stopped,
            })
            .collect();
        let verdict = match vector["verdict"].as_str().unwrap() {
            "not checked" => Checked::NotYet,
            "failed" => Checked::Failed,
            "yes" => Checked::Yes,
            "no" => Checked::No,
            "nothing" => Checked::NoAnswer,
            other => panic!("{other}"),
        };
        let checked: Vec<String> = vector["checked"]
            .as_array()
            .unwrap()
            .iter()
            .map(|model| model.as_str().unwrap().to_string())
            .collect();
        let since = now - ms(&vector["parked_for"]);
        let next = now + ms(&vector["due_in"]);
        let ours = match accounts::decide(&work, verdict, since, next, &checked, now) {
            Decision::Kill { reason } => json!({ "kind": "kill", "reason": reason }),
            Decision::Check { models } => json!({ "kind": "check", "models": models }),
            Decision::Resume { reason } => json!({ "kind": "resume", "reason": reason }),
            Decision::Release { reason } => json!({ "kind": "release", "reason": reason }),
            Decision::Park { reason, delay_ms } => {
                let delay = f64::from(i32::try_from(delay_ms).unwrap()) / 1000.0;
                json!({ "kind": "park", "reason": reason, "delay": delay })
            }
        };
        assert_eq!(ours, vector["decision"], "{vector}");
    }
    assert_eq!(decisions.len(), 1080);
}

#[test]
fn a_selector_is_read_and_asked_as_the_python_host_does() {
    let vectors = vectors();
    let selectors = vectors["selectors"].as_array().unwrap();
    for vector in selectors {
        let models: Vec<String> = vector["models"]
            .as_array()
            .unwrap()
            .iter()
            .map(|model| model.as_str().unwrap().to_string())
            .collect();
        let exit = i32::try_from(vector["exit"].as_i64().unwrap()).unwrap();
        let ours = match accounts::read(Some(exit), vector["printed"].as_str().unwrap(), &models) {
            Verdict::All => json!(true),
            Verdict::NoAccount => json!(false),
            Verdict::NoAnswer => Value::Null,
            Verdict::Only(served) => json!({ "serves": served }),
            Verdict::Failed => json!("failed"),
        };
        assert_eq!(ours, vector["verdict"], "{vector}");
        assert_eq!(
            accounts::asked(&models),
            vector["given"].as_str().unwrap(),
            "{vector}"
        );
    }
    assert_eq!(selectors.len(), 30);
    // A selector that was ended for taking too long said nothing.
    let asked = ["m1".to_string()];
    assert_eq!(
        accounts::read(None, r#"{"available": true}"#, &asked),
        Verdict::NoAnswer
    );
}
