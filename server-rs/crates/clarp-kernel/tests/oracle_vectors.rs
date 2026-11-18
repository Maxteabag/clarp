//! The Oracle's contact and the voice it speaks with, against the Python Host's own modules.
//!
//! `fixtures/oracle-vectors.json` is written by `server-rs/tools/oracle-vectors.py` from
//! `server/lib/oracle_contact.py` and `oracle_voice_provider.py`: which agent a name means,
//! for many names and several sets of agents; what is told of the contact that is kept, and
//! what is kept after; what a chosen contact is answered; the contact a call uses; and the
//! voice a call runs on for every setting and choice. The same must come of the same here.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_kernel::oracle::{
    self, Agent, CONTACT_KEY, CONTACT_LONGEST, GEMINI_MODEL, VOICE_KEY, VOICES, Voices,
};
use clarp_kernel::python;
use serde_json::{Value, json};

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/oracle-vectors.json")).unwrap()
}

fn text<'a>(value: &'a Value, key: &str) -> &'a str {
    value[key].as_str().unwrap_or("")
}

/// The agents of a roster of the vectors.
fn agents(roster: &Value) -> Vec<Agent<'_>> {
    let rows = roster["agents"].as_array().unwrap().iter();
    rows.map(|row| Agent {
        session: row["session"].as_str().unwrap(),
        persona: row["persona"].as_str().unwrap(),
        archived: python::truthy(&row["archived_at"]),
        janitor: python::truthy(&row["is_janitor"]),
        helper: row["role"] == "helper",
        child: python::truthy(&row["parent_agent_id"]),
    })
    .collect()
}

#[test]
fn a_name_means_the_agent_it_means_to_the_python_host() {
    let vectors = vectors();
    let contact = &vectors["contact"];
    assert_eq!(json!(CONTACT_KEY), contact["key"]);
    assert_eq!(json!(CONTACT_LONGEST), contact["longest"]);
    for (name, roster) in contact["rosters"].as_object().unwrap() {
        let agents = agents(roster);
        let live: Vec<&str> = oracle::live(&agents).map(|agent| agent.session).collect();
        assert_eq!(json!(live), roster["live"], "{name}");
    }
    let (mut looked, mut found, mut unknown, mut ambiguous) = (0, 0, 0, 0);
    for case in contact["found"].as_array().unwrap() {
        let agents = agents(&contact["rosters"][text(case, "roster")]);
        // The name as it was said: the blanks around it are the rule's own to take off.
        let one = oracle::resolve(&oracle::said(&case["name"]), &agents);
        let ours = one.map_or(Value::Null, |agent| json!(agent.session));
        assert_eq!(ours, case["found"], "{case}");
        looked += 1;
        found += usize::from(one.is_some());
    }
    for case in contact["visible"].as_array().unwrap() {
        let agents = agents(&contact["rosters"][text(case, "roster")]);
        match oracle::resolve_visible(&oracle::said(&case["name"]), &agents) {
            Ok(agent) => assert_eq!(json!(agent.session), case["answer"], "{case}"),
            Err(why) => {
                assert_eq!(json!(why), case["refused"], "{case}");
                unknown += usize::from(why == "unknown agent");
                ambiguous += usize::from(why == "ambiguous agent");
            }
        }
    }
    assert_eq!((looked, found, unknown, ambiguous), (630, 48, 577, 1));
}

#[test]
fn the_contact_is_kept_read_and_chosen_as_on_the_python_host() {
    let vectors = vectors();
    let contact = &vectors["contact"];
    let (mut stale, mut refused, mut chosen, mut calls) = (0, 0, 0, 0);
    let mut direct_calls = 0;
    // What is kept is read: a contact that names no one agent any more is let go.
    for case in contact["read"].as_array().unwrap() {
        let agents = agents(&contact["rosters"][text(case, "roster")]);
        let kept = text(case, "stored");
        let (told, let_go) = oracle::contact(kept, &agents);
        assert_eq!(told.told(), case["answer"], "{case}");
        let after = if let_go || kept.is_empty() {
            Value::Null
        } else {
            json!(kept)
        };
        assert_eq!(after, case["kept"], "{case}");
        stale += usize::from(let_go);
    }
    // A contact is chosen: over nothing, and over one that was there.
    for case in contact["set"].as_array().unwrap() {
        let agents = agents(&contact["rosters"][text(case, "roster")]);
        let before = text(case, "before");
        match oracle::choose_contact(&case["value"], &agents) {
            Ok((told, kept)) => {
                assert_eq!(told.told(), case["answer"], "{case}");
                assert_eq!(json!(kept), case["kept"], "{case}");
                chosen += usize::from(kept.is_some());
            }
            Err(why) => {
                assert_eq!(json!(why), case["refused"], "{case}");
                // What was kept stays as it was.
                let stays = if before.is_empty() {
                    Value::Null
                } else {
                    json!(before)
                };
                assert_eq!(stays, case["kept"], "{case}");
                refused += 1;
            }
        }
    }
    // The contact a call uses: the Host's own, else what the phone still sends along.
    for case in contact["used"].as_array().unwrap() {
        let agents = agents(&contact["rosters"][text(case, "roster")]);
        let kept = text(case, "stored");
        let (uses, let_go) = oracle::contact_of_a_call(kept, &case["requested"], &agents);
        assert_eq!(json!(uses), case["uses"], "{case}");
        let after = if let_go || kept.is_empty() {
            Value::Null
        } else {
            json!(kept)
        };
        assert_eq!(after, case["kept"], "{case}");
        calls += usize::from(!uses.is_empty());
        // And of a call in which everything goes to the contact: one that is none is no call.
        let direct = &case["direct"];
        let (uses, let_go) = oracle::contact_of_a_direct_call(kept, &case["requested"], &agents);
        match uses {
            Ok(session) => assert_eq!(json!(session), direct["answer"], "{case}"),
            Err(why) => assert_eq!(json!(why), direct["refused"], "{case}"),
        }
        let after = if let_go || kept.is_empty() {
            Value::Null
        } else {
            json!(kept)
        };
        assert_eq!(after, direct["kept"], "{case}");
        direct_calls += usize::from(direct.get("answer").is_some());
    }
    assert_eq!((stale, refused, chosen, calls), (121, 1016, 100, 61));
    assert_eq!(direct_calls, 36);
}

#[test]
fn a_call_runs_on_the_voice_the_python_host_would_run_it_on() {
    let vectors = vectors();
    let voice = &vectors["voice"];
    assert_eq!(json!(VOICE_KEY), voice["key"]);
    assert_eq!(json!(VOICES), voice["providers"]);
    assert_eq!(json!(GEMINI_MODEL), voice["usual_model"]);
    let (mut cases, mut fallen_back) = (0, 0);
    for case in voice["cases"].as_array().unwrap() {
        let settings = &case["settings"];
        let voices = Voices {
            kept: case["stored"].as_str(),
            configured: settings["provider"].as_str(),
            has_gemini_key: settings["has_key"] == true,
            model: text(settings, "model"),
            voice: text(settings, "voice"),
            of_oracle: settings["of_oracle"].as_str(),
            usual: text(settings, "usual"),
        };
        assert_eq!(voices.status(), case["status"], "{case}");
        assert_eq!(json!(voices.effective()), case["effective"], "{case}");
        cases += 1;
        fallen_back += usize::from(voices.selected() != voices.effective());
    }
    let mut refused = 0;
    for case in voice["chosen"].as_array().unwrap() {
        match oracle::choose_voice(&case["value"]) {
            Ok(kept) => assert_eq!(json!(kept), case["kept"], "{case}"),
            Err(why) => {
                assert_eq!(json!(why), case["refused"], "{case}");
                assert_eq!(case["kept"], Value::Null, "{case}");
                refused += 1;
            }
        }
    }
    assert_eq!((cases, fallen_back, refused), (112, 34, 5));
}

#[test]
fn the_case_of_a_text_is_folded_as_python_folds_it() {
    // By that interpreter's own table: a sign with no single small form becomes more than
    // one, and a sign folds the same wherever it stands in a word.
    for (text, folded) in [
        ("Stra\u{df}e", "strasse"),
        ("\u{3a3}\u{391}\u{3a3}", "\u{3c3}\u{3b1}\u{3c3}"),
        ("\u{130}stanbul", "i\u{307}stanbul"),
        ("\u{fb01}sh", "fish"),
        ("MIKE", "mike"),
        ("\u{1e9e}", "ss"),
        ("\u{212a}", "k"),
        ("\u{c5}se", "\u{e5}se"),
        ("theo-97E5", "theo-97e5"),
        ("\u{1c5}", "\u{1c6}"),
        ("", ""),
        ("already small 123", "already small 123"),
    ] {
        assert_eq!(python::casefold(text), folded, "{text}");
    }
}
