//! What an agent's tool is told, against the Python Host's own words.
//!
//! `fixtures/told-vectors.json` is written by `server-rs/tools/told-vectors.py`: what the
//! Python Host's functions say to the tool of agents made up for it, in rooms with different
//! settings. The same must be said here, sign for sign. One clause is added on purpose: how
//! much an agent says while it works is told to Claude Code too (reference defect R21), and
//! the test says where.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_kernel::told::{
    Humanness, Sound, at_head, beside, headed, narration, narration_options, natural, unheaded,
    who, whose,
};
use serde_json::Value;

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/told-vectors.json")).unwrap()
}

/// A level that is one: a whole number. The Python Host's functions also take texts and
/// truth values, which no setting ever holds.
fn whole(level: &Value) -> Option<i64> {
    match level {
        Value::Number(number) if !number.is_f64() => number.as_i64(),
        _ => None,
    }
}

#[test]
fn every_level_of_sounding_human_and_of_saying_what_one_does_is_put_as_the_python_host_puts_it() {
    let vectors = vectors();
    let mut levels = 0;
    for row in vectors["levels"].as_array().unwrap() {
        let Some(level) = whole(&row["level"]) else {
            continue;
        };
        levels += 1;
        assert_eq!(
            natural(level, false),
            row["plain"].as_str().unwrap(),
            "level {level}"
        );
        assert_eq!(
            natural(level, true),
            row["gemini"].as_str().unwrap(),
            "level {level}, with tags"
        );
    }
    assert!(levels >= 14, "{levels} levels");
    let mut clauses = 0;
    for row in vectors["narration"].as_array().unwrap() {
        let Some(level) = whole(&row["level"]) else {
            continue;
        };
        clauses += 1;
        assert_eq!(
            narration(level),
            row["clause"].as_str().unwrap(),
            "level {level}"
        );
    }
    assert!(clauses >= 6, "{clauses} levels");
    assert_eq!(narration_options(), vectors["options"]);
}

#[test]
fn a_prompt_gets_the_head_the_python_host_gives_it_and_loses_it_as_there() {
    let vectors = vectors();
    let written = vectors["written"].as_array().unwrap();
    assert!(written.len() >= 5);
    for row in written {
        let text = row["text"].as_str().unwrap();
        let given = headed(&at_head("", "", None), None, text);
        assert_eq!(given, row["given"].as_str().unwrap(), "{text:?}");
        assert_eq!(unheaded(&given), text);
        assert_eq!(
            unheaded(text),
            row["read_plain"].as_str().unwrap(),
            "{text:?} as it is"
        );
        assert_eq!(
            unheaded(&format!("\"{given}\"")),
            row["read_quoted"].as_str().unwrap(),
            "{text:?} between quotation marks"
        );
        let spoken = headed(&at_head("", "", Some(Sound::default())), None, text);
        assert_eq!(unheaded(&spoken), row["read"].as_str().unwrap());
    }
}

#[test]
fn the_setting_of_how_human_voices_sound_changes_as_on_the_python_host() {
    let vectors = vectors();
    let changes = vectors["changes"].as_array().unwrap();
    assert!(changes.len() >= 20);
    let mut setting = Humanness::default();
    assert_eq!(setting.told(), changes[0]["answered"]);
    let mut refused = 0;
    for change in &changes[1..] {
        let given = &change["given"];
        match setting.changed(given) {
            Ok(after) => {
                assert_eq!(after.told(), change["answered"], "after {given}");
                assert!(change["refused"].is_null(), "{given} is refused there");
                setting = after;
            }
            Err(why) => {
                assert_eq!(
                    why,
                    change["refused"].as_str().unwrap_or("<taken>"),
                    "{given}"
                );
                refused += 1;
            }
        }
        assert_eq!(setting.told(), change["then"], "what it is after {given}");
        // What is kept for it reads back as it was.
        let (usual, of_agents) = setting.kept();
        assert_eq!(Humanness::stored(Some(&usual), Some(&of_agents)), setting);
    }
    assert!(refused >= 10, "{refused} changes refused");
}

#[test]
fn an_agents_tool_is_told_what_the_python_host_tells_it() {
    let vectors = vectors();
    let cases = vectors["cases"].as_array().unwrap();
    assert!(cases.len() >= 9);
    let (mut with_narration, mut with_tags) = (0, 0);
    for case in cases {
        let name = case["name"].as_str().unwrap();
        let (agent, told) = (&case["agent"], &case["told"]);
        let said = |key: &str| agent[key].as_str().unwrap();
        let (persona, session) = (said("persona"), said("session"));
        let expected = |path: &[&str]| {
            let mut at = told;
            for key in path {
                at = &at[*key];
            }
            at.as_str().unwrap().to_string()
        };

        // Who it is.
        let personalities = case["personalities"].as_bool().unwrap_or(true);
        let identity = who(persona, said("personality"), personalities);
        assert_eq!(
            identity.clone().unwrap_or_default(),
            expected(&["who"]),
            "{name}: who"
        );
        assert_eq!(
            whose(persona, session),
            expected(&["whose"]),
            "{name}: whose turn"
        );

        // How it sounds.
        let setting = match case.get("humanness") {
            Some(change) => Humanness::default().changed(change).unwrap(),
            None => Humanness::default(),
        };
        let humanness = setting.of(&[persona, session]);
        assert_eq!(
            humanness,
            told["level"].as_i64().unwrap(),
            "{name}: how human"
        );
        let level = agent["voice_verbosity"].as_i64().unwrap();
        let clause = narration(level);
        assert_eq!(clause, expected(&["narration"]), "{name}: how much it says");
        let tags = told["gemini"].as_bool().unwrap();
        let sound = Sound {
            humanness,
            tags,
            narration: level,
        };
        with_narration += usize::from(!clause.is_empty());
        with_tags += usize::from(tags);

        // A tool that is told beside its prompt: Claude Code, by its hook, on the Python Host.
        assert_eq!(
            beside(persona, session, None),
            expected(&["hook", "silent"]),
            "{name}"
        );
        let quiet = Sound {
            narration: 0,
            ..sound
        };
        let by_hook = expected(&["hook", "spoken"]);
        assert_eq!(
            beside(persona, session, Some(quiet)),
            by_hook,
            "{name}: spoken"
        );
        // The one clause this Host adds: the Python Host's hook never says it.
        let more = match clause {
            "" => by_hook,
            clause => format!("{by_hook}\n\n{clause}"),
        };
        assert_eq!(
            beside(persona, session, Some(sound)),
            more,
            "{name}: spoken, and how much"
        );

        // A tool that is told in the words before the user's.
        let silent = at_head(persona, session, None);
        let spoken = at_head(persona, session, Some(sound));
        assert_eq!(silent, expected(&["head", "silent"]), "{name}: at the head");
        assert_eq!(
            spoken,
            expected(&["head", "spoken"]),
            "{name}: at the head, spoken"
        );
        let who = identity.as_deref();
        assert_eq!(
            headed(&silent, who, "Hello there."),
            expected(&["prompt", "silent"]),
            "{name}: the prompt"
        );
        assert_eq!(
            headed(&spoken, who, "Hello there."),
            expected(&["prompt", "spoken"]),
            "{name}: the prompt, spoken"
        );
    }
    assert!(
        with_narration >= 3 && with_tags >= 3,
        "{with_narration}, {with_tags}"
    );
}
