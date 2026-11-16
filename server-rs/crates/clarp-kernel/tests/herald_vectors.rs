//! The herald, against the Python Host's own manager and classifier.
//!
//! `fixtures/herald-vectors.json` is written by `server-rs/tools/herald-vectors.py`: what
//! the Python Host's herald did, step by step, in scripts of what happens (clips arrive, the
//! user opens an agent, sends, speaks, time passes, settings change, the herald's voice
//! fails), and what its classifier made of things a user might say. The same must come of
//! the same here.
//!
//! One thing is each Host's own and is compared without it: when the user lets two agents
//! speak in one breath, which of the two is played first. The Python Host keeps the raised
//! hands in a set, whose order is another in every process; here it is the sessions' order.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::BTreeMap;

use clarp_kernel::herald::{
    AWAITING_MS, Herald, Moment, OTHER_WORDS_MOST, Play, SHORT_MOST, Settings, Taken, meant, words,
};
use serde_json::{Value, json};

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/herald-vectors.json")).unwrap()
}

fn texts(list: &Value) -> Vec<String> {
    let items = list.as_array().unwrap().iter();
    items
        .map(|item| item.as_str().unwrap().to_string())
        .collect()
}

#[test]
fn the_heralds_words_and_settings_are_the_python_hosts() {
    let vectors = vectors();
    assert_eq!(words("Rachel", "rachel"), vectors["words"]);
    assert_eq!(Settings::default().told(), vectors["defaults"]);
    assert_eq!(
        AWAITING_MS as f64,
        vectors["awaiting_s"].as_f64().unwrap() * 1000.0
    );
    assert_eq!(vectors["most_short"], SHORT_MOST);
    assert_eq!(vectors["residual"], OTHER_WORDS_MOST);
    // Kept and read back; what the Python Host kept is read as it reads it.
    let set = Settings {
        disabled: true,
        short: 7,
        bypass: false,
    };
    let kept: BTreeMap<String, String> = set.kept().into_iter().collect();
    let read = |key: &str| kept.get(key).map(String::as_str);
    assert_eq!(
        Settings::stored(
            read("herald.disabled"),
            read("herald.speak_if_short_chars"),
            read("herald.short_reply_bypass_enabled")
        ),
        set
    );
    assert_eq!(Settings::stored(None, None, None), Settings::default());
    assert_eq!(
        Settings::stored(Some(" ON "), Some("9000"), Some("no")),
        Settings {
            disabled: true,
            short: SHORT_MOST,
            bypass: false
        }
    );
    assert_eq!(
        Settings::stored(Some("off"), Some("many"), Some("1")),
        Settings::default()
    );
    // A change an app asks for, and what is no change.
    let usual = Settings::default();
    assert_eq!(usual.changed(&json!({})), Ok(usual));
    assert_eq!(
        usual.changed(&json!({ "disabled": true, "speak_if_short_chars": 0, "short_reply_bypass_enabled": false })),
        Ok(Settings {
            disabled: true,
            short: 0,
            bypass: false
        })
    );
    for (asked, why) in [
        (json!([]), "herald settings must be an object"),
        (json!({ "disabled": 1 }), "disabled must be a boolean"),
        (
            json!({ "speak_if_short_chars": true }),
            "speak_if_short_chars must be an integer",
        ),
        (
            json!({ "speak_if_short_chars": 5.0 }),
            "speak_if_short_chars must be an integer",
        ),
        (
            json!({ "speak_if_short_chars": "5" }),
            "speak_if_short_chars must be an integer",
        ),
        (
            json!({ "speak_if_short_chars": -1 }),
            "speak_if_short_chars must be between 0 and 2000",
        ),
        (
            json!({ "speak_if_short_chars": 2001 }),
            "speak_if_short_chars must be between 0 and 2000",
        ),
        (
            json!({ "short_reply_bypass_enabled": "yes" }),
            "short_reply_bypass_enabled must be a boolean",
        ),
        // The first thing that is wrong is said, and nothing is changed.
        (
            json!({ "disabled": true, "speak_if_short_chars": 9999 }),
            "speak_if_short_chars must be between 0 and 2000",
        ),
    ] {
        assert_eq!(usual.changed(&asked), Err(why.to_string()), "{asked}");
    }
}

#[test]
fn what_a_user_says_means_what_the_python_host_takes_it_to_mean() {
    let vectors = vectors();
    let (mut utterances, mut signals) = (0, 0);
    for room in vectors["said"].as_array().unwrap() {
        let names = texts(&room["names"]);
        let names: Vec<&str> = names.iter().map(String::as_str).collect();
        for row in room["rows"].as_array().unwrap() {
            let text = row["text"].as_str().unwrap();
            let found = meant(text, &names);
            utterances += 1;
            signals += usize::from(!found.grants.is_empty() || !found.declines.is_empty());
            assert_eq!(
                found.grants,
                texts(&row["grants"]),
                "{text:?} among {names:?}: who may speak"
            );
            assert_eq!(
                found.declines,
                texts(&row["declines"]),
                "{text:?} among {names:?}: who is to wait"
            );
        }
    }
    assert!(
        utterances >= 100 && signals >= 60,
        "{utterances}, {signals}"
    );
}

/// Something that was played, as both Hosts' records are compared: which clip (`None`: the
/// herald's own), whose, and whether across agents; or that the herald's clip was made.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
enum Heard {
    Made(String),
    Clip(String, Option<u64>, bool),
}

fn heard_here(plays: Vec<Play<u64>>) -> Vec<Heard> {
    let each = plays.into_iter().map(|play| match play {
        Play::Clip {
            session,
            clip,
            across,
        } => Heard::Clip(session, Some(clip), across),
        Play::Herald { session } => Heard::Clip(session, None, true),
    });
    each.collect()
}

fn heard_there(events: &Value) -> Vec<Heard> {
    let each = events.as_array().unwrap().iter().map(|event| {
        let session = event["session"].as_str().unwrap().to_string();
        if event["type"] == "herald-made" {
            return Heard::Made(session);
        }
        assert_eq!(event["type"], "audio");
        Heard::Clip(session, event["clip_id"].as_u64(), event["herald"] == true)
    });
    each.collect()
}

/// By session, each session's own order kept: for a step in which two agents were let speak.
fn by_session(mut heard: Vec<Heard>) -> Vec<Heard> {
    heard.sort_by_key(|one| match one {
        Heard::Made(session) | Heard::Clip(session, _, _) => session.clone(),
    });
    heard
}

struct Room {
    herald: Herald<u64>,
    open: String,
    now_ms: i64,
    settings: Settings,
    voice_works: bool,
    names: BTreeMap<String, String>,
}

impl Room {
    /// Does one step; gives what was played and what the Python Host's record calls the result.
    fn act(&mut self, step: &Value) -> (Vec<Heard>, Value) {
        let said = |key: &str| step[key].as_str().unwrap().to_string();
        match step["do"].as_str().unwrap() {
            "focus" => {
                self.open = said("sid");
                let open = self.open.clone();
                (heard_here(self.herald.opened(&open)), Value::Null)
            }
            "open" => {
                self.open = said("sid");
                (Vec::new(), Value::Null)
            }
            "awaiting" => (
                heard_here(self.herald.sent(&said("sid"), self.now_ms)),
                Value::Null,
            ),
            "tick" => {
                self.now_ms += step["s"].as_i64().unwrap() * 1000;
                (Vec::new(), Value::Null)
            }
            "settings" => {
                let mut asked = step.clone();
                asked.as_object_mut().unwrap().remove("do");
                self.settings = self.settings.changed(&asked).unwrap();
                (Vec::new(), Value::Null)
            }
            "voice" => {
                self.voice_works = step["works"].as_bool().unwrap();
                (Vec::new(), Value::Null)
            }
            "say" => {
                let names = self.names.clone();
                let name_of = move |session: &str| names.get(session).cloned().unwrap_or_default();
                let (mut granted, mut declined, plays) = self.herald.said(&said("text"), &name_of);
                granted.sort();
                declined.sort();
                (
                    heard_here(plays),
                    json!({ "granted": granted, "declined": declined }),
                )
            }
            "clip" => {
                let (session, n) = (said("session"), step["n"].as_u64().unwrap());
                // How long what it says is: a number, a text that is one, or not known.
                let signs = match &step["len"] {
                    Value::Number(signs) => signs.as_i64(),
                    Value::String(signs) => signs.parse().ok(),
                    _ => None,
                };
                let known = self.names.contains_key(&session);
                let open = self.open.clone();
                let at = Moment {
                    open: &open,
                    now_ms: self.now_ms,
                };
                let taken = self
                    .herald
                    .ingest((&session, n), (signs, known), at, self.settings);
                let mut heard = Vec::new();
                match taken {
                    Taken::Decided(plays) => heard.extend(heard_here(plays)),
                    Taken::Raise(attempt) => {
                        // What happens while the herald's clip is being made.
                        if let Some(during) = step.get("during") {
                            heard.extend(self.act(during).0);
                        }
                        if self.voice_works {
                            heard.push(Heard::Made(session.clone()));
                        }
                        let open = self.open.clone();
                        let at = Moment {
                            open: &open,
                            now_ms: self.now_ms,
                        };
                        heard.extend(heard_here(self.herald.made(&attempt, self.voice_works, at)));
                    }
                }
                let herald_emitted = heard.contains(&Heard::Clip(session.clone(), None, true));
                let broadcast = heard
                    .iter()
                    .any(|one| matches!(one, Heard::Clip(whose, Some(clip), _) if *whose == session && *clip == n));
                (
                    heard,
                    json!({ "broadcast": broadcast, "herald_emitted": herald_emitted }),
                )
            }
            other => panic!("a step this test does not know: {other}"),
        }
    }
}

#[test]
fn the_herald_does_what_the_python_hosts_does_step_by_step() {
    let vectors = vectors();
    let names: BTreeMap<String, String> = vectors["agents"]
        .as_object()
        .unwrap()
        .iter()
        .map(|(session, name)| (session.clone(), name.as_str().unwrap().to_string()))
        .collect();
    let scripts = vectors["scripts"].as_array().unwrap();
    let (mut steps, mut raised, mut let_speak) = (0, 0, 0);
    for script in scripts {
        let name = script["name"].as_str().unwrap();
        let mut room = Room {
            herald: Herald::default(),
            open: String::new(),
            now_ms: 1_000_000,
            settings: Settings::default(),
            voice_works: true,
            names: names.clone(),
        };
        for (place, row) in script["steps"].as_array().unwrap().iter().enumerate() {
            let step = &row["step"];
            let (heard, result) = room.act(step);
            steps += 1;
            let there = heard_there(&row["heard"]);
            raised += usize::from(
                there
                    .iter()
                    .any(|one| matches!(one, Heard::Clip(_, None, _))),
            );
            // The herald's words are the agent's name, or its session when it has none.
            for event in row["heard"].as_array().unwrap() {
                if event["type"] == "herald-made" {
                    let session = event["session"].as_str().unwrap();
                    assert_eq!(event["text"], words(&names[session], session), "{name}");
                }
            }
            let two_at_once =
                step["do"] == "say" && row["result"]["granted"].as_array().unwrap().len() > 1;
            if two_at_once {
                assert_eq!(
                    by_session(heard),
                    by_session(there),
                    "{name}, step {place}: {step}"
                );
            } else {
                assert_eq!(heard, there, "{name}, step {place}: {step}");
            }
            let mut theirs = row["result"].clone();
            if step["do"] == "say" {
                for key in ["granted", "declined"] {
                    let mut list = texts(&theirs[key]);
                    let_speak += usize::from(key == "granted" && !list.is_empty());
                    list.sort();
                    theirs[key] = json!(list);
                }
            }
            assert_eq!(result, theirs, "{name}, step {place}: {step}");
            assert_eq!(
                json!(room.herald.pending()),
                row["pending"],
                "{name}, step {place}: whose hand is up"
            );
            // What is held, by the number of each clip in the name it has there.
            let held = row["held"].as_object().unwrap();
            for session in names.keys().map(String::as_str).chain(["stranger", ""]) {
                let there: Vec<u64> = held.get(session).map_or_else(Vec::new, |urls| {
                    let numbers = texts(urls).into_iter().map(|url| {
                        let file = url.rsplit('/').next().unwrap().to_string();
                        file.split("__").next().unwrap().parse::<u64>().unwrap() - 1000
                    });
                    numbers.collect()
                });
                assert_eq!(
                    room.herald.held(session),
                    there,
                    "{name}, step {place}: held of {session:?}"
                );
            }
        }
    }
    assert!(
        scripts.len() >= 15 && steps >= 120,
        "{} scripts, {steps} steps",
        scripts.len()
    );
    assert!(
        raised >= 20 && let_speak >= 5,
        "{raised} hands raised, {let_speak} let speak"
    );
}
