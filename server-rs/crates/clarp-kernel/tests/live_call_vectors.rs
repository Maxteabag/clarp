//! What a live call is told, against the Python Host's own answers.
//!
//! `fixtures/live-call-vectors.json` is written by `server-rs/tools/live-call-vectors.py` from
//! `server/lib/agent_ring_live.py`, `agent_live_settings.py` and
//! `agent_ring_live_socket.instructions`: how a call is asked to be held, what names a
//! request, the one form of a value, whether a text is an event's time, what a
//! producer says of its event, the tools a profile opts into, the snapshot of a ring and
//! what its voice is told, the context of a call the user starts, and which agents take
//! one. The same must come out here.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::BTreeMap;

use clarp_kernel::calls::Refused;
use clarp_kernel::live_call::{self as live, Agent, Folder, Found, Scan};
use clarp_kernel::python;
use serde_json::{Value, json};

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/live-call-vectors.json")).unwrap()
}

/// A value of the fixture as it was before its long runs of one sign were written short:
/// `{"~run": [before, sign, count, after]}`.
fn whole(value: &Value) -> Value {
    match value {
        Value::Object(fields) if fields.len() == 1 && fields.contains_key("~run") => {
            let run = &fields["~run"];
            let count = usize::try_from(run[2].as_u64().unwrap()).unwrap();
            let after = whole(&run[3]);
            json!(format!(
                "{}{}{}",
                run[0].as_str().unwrap(),
                run[1].as_str().unwrap().repeat(count),
                after.as_str().unwrap()
            ))
        }
        Value::Object(fields) => {
            let fields = fields.iter().map(|(key, item)| (key.clone(), whole(item)));
            Value::Object(fields.collect())
        }
        Value::Array(items) => Value::Array(items.iter().map(whole).collect()),
        plain => plain.clone(),
    }
}

fn rows(vectors: &Value, name: &str) -> Vec<Value> {
    let rows = vectors[name].as_array().unwrap();
    rows.iter().map(whole).collect()
}

/// What a refusal says, as the fixture writes one.
fn said(refused: &Refused) -> Value {
    json!({ "status": refused.status, "error": refused.body["error"] })
}

/// What was answered or refused, beside what the Python Host answered or refused.
fn held<T: Into<Value>>(ours: Result<T, Refused>, (theirs, refused): (&Value, &Value), name: &str) {
    match ours {
        Ok(ours) => {
            assert!(
                refused.is_null(),
                "{name}: taken here, refused there with {refused}"
            );
            let ours: Value = ours.into();
            assert_eq!(&ours, theirs, "{name}");
        }
        Err(why) => assert_eq!(&said(&why), refused, "{name}"),
    }
}

/// An agent as a case has it: what is no text is what Python would make of it.
fn text(value: &Value) -> String {
    match value {
        Value::Null => String::new(),
        Value::String(text) => text.clone(),
        other => other.to_string(),
    }
}

#[test]
fn the_names_and_bounds_are_the_python_hosts() {
    let vectors = vectors();
    let (limits, names) = (&vectors["limits"], &vectors["names"]);
    assert_eq!(
        json!({
            "context_bytes": live::CONTEXT_BYTES, "snapshot_bytes": live::SNAPSHOT_BYTES,
            "log_lines": live::LOG_LINES, "log_line_signs": live::LOG_LINE_SIGNS,
            "opening_signs": live::OPENING_SIGNS, "profiles_scanned": live::PROFILES_SCANNED,
            "request_id_bytes": live::REQUEST_ID_BYTES, "team_most": live::TEAM_MOST,
            "request_signs": live::REQUEST_SIGNS,
        }),
        *limits
    );
    assert_eq!(
        json!({
            "feature": live::FEATURE, "model": live::MODEL, "live_models": live::LIVE_MODELS,
            "live_model_default": live::LIVE_MODEL_DEFAULT,
            "min_client": live::AGENT_LIVE_MODEL_MIN_CLIENT.iter().map(|(name, at)| (name.to_string(), json!(at))).collect::<serde_json::Map<_, _>>(),
            "voice": live::VOICE,
            "provider": live::PROVIDER, "protocol": live::PROTOCOL, "stories": live::STORIES,
            "sources": live::SOURCES, "bindings_key": live::BINDINGS_KEY, "phases": live::PHASES,
            "close_reasons": live::CLOSE_REASONS,
        }),
        *names
    );
}

#[test]
fn a_text_is_an_events_time_exactly_when_the_python_host_takes_one() {
    // Since its PR 449 (R90): one stated grammar, the same on every interpreter, then the day
    // and the zone. The Python Host's own check decided each text of the table.
    let vectors = vectors();
    let mut counted = Vec::new();
    for (kind, taken) in [("refused", false), ("taken", true)] {
        let texts = vectors["moments"][kind].as_array().unwrap();
        for text in texts {
            let text = text.as_str().unwrap();
            assert_eq!(live::event_time(text), taken, "{text:?}");
        }
        counted.push(texts.len());
    }
    // Both kinds are there: the table holds texts the Host takes and texts it refuses.
    assert!(counted.iter().all(|count| *count > 0), "{counted:?}");
}

#[test]
fn a_call_is_asked_to_be_held_and_a_request_named_as_the_python_host_reads_it() {
    let vectors = vectors();
    let mut refused = 0;
    for row in rows(&vectors, "modes") {
        let default = match row["default"].as_str() {
            Some("classic") => Some(live::CLASSIC),
            _ => None,
        };
        let ours = live::mode_of(&row["asked"], default).map(|mode| json!(mode));
        refused += usize::from(ours.is_err());
        held(
            ours,
            (&row["mode"], &row["refused"]),
            &row["asked"].to_string(),
        );
    }
    let numbers = &vectors["numbers"];
    let mut named = 0;
    for row in numbers["request_ids"].as_array().unwrap() {
        let ours = live::request_id(&row["value"]);
        assert_eq!(json!(ours.is_some()), row["ok"], "{}", row["value"]);
        assert!(ours.is_none_or(|ours| row["value"] == ours));
        named += usize::from(ours.is_some());
    }
    let mut no_epoch = 0;
    for row in numbers["epochs"].as_array().unwrap() {
        let ours = live::epoch(Some(&row["value"])).map(|epoch| json!(epoch));
        no_epoch += usize::from(ours.is_err());
        held(
            ours,
            (&row["epoch"], &row["refused"]),
            &row["value"].to_string(),
        );
    }
    assert!(live::epoch(None).is_err());
    assert_eq!((refused, named, no_epoch), (16, 6, 8));
}

#[test]
fn a_value_has_the_one_form_and_the_digest_the_python_host_gives_it() {
    let vectors = vectors();
    let forms = vectors["forms"].as_array().unwrap();
    for row in forms {
        let name = row["canonical"].as_str().unwrap();
        assert_eq!(python::canonical(&row["value"]), name);
        assert_eq!(json!(live::digest(&row["value"])), row["digest"], "{name}");
    }
    assert_eq!(forms.len(), 28);
}

#[test]
fn what_a_producer_says_of_its_event_is_taken_as_the_python_host_takes_it() {
    let vectors = vectors();
    let (mut taken, mut refused) = (0, 0);
    for row in rows(&vectors, "contexts") {
        let name = row["name"].as_str().unwrap();
        let ours = live::context(&row["context"], row["opening_line"].as_str().unwrap());
        match &ours {
            Ok(ours) => {
                taken += 1;
                // The same, and its fields in the order of their names.
                assert_eq!(python::dumps(ours), python::dumps(&row["result"]), "{name}");
            }
            Err(_) => refused += 1,
        }
        held(ours, (&row["result"], &row["refused"]), name);
    }
    // As many taken and refused as the Python Host's own rows say: counted from them, so that a
    // reference that moves moves this with it.
    let contexts = rows(&vectors, "contexts");
    let theirs = |key: &str| contexts.iter().filter(|row| !row[key].is_null()).count();
    assert_eq!((taken, refused), (theirs("result"), theirs("refused")));
    assert_eq!(taken + refused, contexts.len());
}

#[test]
fn the_tools_of_a_profile_are_frozen_as_the_python_host_freezes_them() {
    let vectors = vectors();
    let offered = &vectors["offered"];
    let mut opted = 0;
    for row in rows(&vectors, "tools") {
        let name = row["name"].as_str().unwrap();
        let ours = live::tools(&row["profile"], row["story"].as_str().unwrap());
        let Some(ours) = ours else {
            assert!(
                row["config"].is_null(),
                "{name}: no tools here, {}",
                row["config"]
            );
            continue;
        };
        opted += 1;
        // The same, written in the same order.
        assert_eq!(
            python::dumps(&ours),
            python::dumps(&row["config"]),
            "{name}"
        );
        assert_eq!(json!(live::team(&ours)), row["team"], "{name}");
        let at = |kind: &str| usize::try_from(row[kind].as_u64().unwrap()).unwrap();
        let schema = &offered["schema"][at("schema")];
        assert_eq!(
            python::dumps(&live::offered(&ours)),
            python::dumps(schema),
            "{name}"
        );
        assert_eq!(
            json!(live::overlay(&ours)),
            offered["overlay"][at("overlay")],
            "{name}"
        );
    }
    assert_eq!(opted, 78);
    let kinds = |kind: &str| offered[kind].as_array().unwrap().len();
    assert_eq!((kinds("schema"), kinds("overlay")), (12, 6));
}

/// The bytes of a file of a profile, as a case describes it.
fn bytes_of(file: &Value) -> Vec<u8> {
    if let Some(text) = file["text"].as_str() {
        return text.as_bytes().to_vec();
    }
    if let Some(coded) = file["b64"].as_str() {
        return python::b64decode(coded).unwrap();
    }
    let count = usize::try_from(file["repeat"][1].as_u64().unwrap()).unwrap();
    file["repeat"][0]
        .as_str()
        .unwrap()
        .repeat(count)
        .into_bytes()
}

#[test]
fn the_snapshot_of_a_ring_and_what_its_voice_is_told_are_the_python_hosts() {
    let vectors = vectors();
    let host_id = vectors["host_id"].as_str().unwrap();
    let mut built = 0;
    for row in rows(&vectors, "snapshots") {
        let name = row["name"].as_str().unwrap();
        let files: BTreeMap<String, Vec<u8>> = row["files"]
            .as_object()
            .unwrap()
            .iter()
            .map(|(name, file)| (name.clone(), bytes_of(file)))
            .collect();
        let read = |name: &str| files.get(name).cloned();
        let folder = Folder {
            profile: row["profile"].as_str(),
            file: &read,
        };
        let (agent_id, session, persona) = (
            text(&row["agent"]["agent_id"]),
            text(&row["agent"]["session"]),
            text(&row["agent"]["persona"]),
        );
        let agent = Agent {
            agent_id: &agent_id,
            session: &session,
            persona: &persona,
        };
        let has_key = row["key"].as_bool().unwrap();
        let ours = live::snapshot(agent, &row["context"], folder, (host_id, has_key));
        if let Ok(ours) = &ours {
            built += 1;
            // Its fields in the order the Python Host puts them, its size and its digest in
            // its one form, and what its voice is told.
            assert_eq!(
                python::dumps(ours),
                python::dumps(&row["snapshot"]),
                "{name}"
            );
            assert_eq!(json!(python::canonical(ours).len()), row["size"], "{name}");
            assert_eq!(json!(live::digest(ours)), row["digest"], "{name}");
            assert_eq!(
                json!(live::instructions(ours)),
                row["instructions"],
                "{name}"
            );
        }
        held(ours, (&row["snapshot"], &row["refused"]), name);
    }
    assert_eq!(built, 27);
}

#[test]
fn a_call_of_the_user_has_the_context_the_python_host_gives_it() {
    let vectors = vectors();
    let now = vectors["now"].as_str().unwrap();
    let mut made = 0;
    for row in rows(&vectors, "user_calls") {
        let name = row["name"].as_str().unwrap();
        let (agent_id, session, persona) = (
            text(&row["agent"]["agent_id"]),
            text(&row["agent"]["session"]),
            text(&row["agent"]["persona"]),
        );
        let agent = Agent {
            agent_id: &agent_id,
            session: &session,
            persona: &persona,
        };
        let held_there = row["profile"].as_str();
        let ours = live::user_call(held_there, "pump-guard", agent, &row["request_id"], now);
        if let Ok(ours) = &ours {
            made += 1;
            assert_eq!(
                python::dumps(ours),
                python::dumps(&row["context"]),
                "{name}"
            );
        }
        held(ours, (&row["context"], &row["refused"]), name);
    }
    assert_eq!(made, 5);
}

#[test]
fn the_agents_that_take_the_users_calls_are_those_the_python_host_finds() {
    let vectors = vectors();
    let (mut refused_strict, mut refused_lenient) = (0, 0);
    for row in rows(&vectors, "registry") {
        let name = row["name"].as_str().unwrap();
        let found: Vec<Found> = row["folders"]
            .as_array()
            .unwrap()
            .iter()
            .map(|entry| Found {
                name: entry["name"].as_str().unwrap().to_string(),
                is_folder: entry["kind"] == "folder",
                profile: entry["profile"]
                    .as_str()
                    .map(|held| held.as_bytes().to_vec()),
            })
            .collect();
        let scan = if row["there"].as_bool().unwrap() {
            Scan::Folders(&found)
        } else {
            Scan::NotThere
        };
        let known = row["known"].as_str();
        for (kind, strict) in [("strict", true), ("lenient", false)] {
            let theirs = &row[kind];
            let ours = live::taking(known, scan, strict);
            let kept = ours.keep.as_deref().or(known);
            assert_eq!(json!(kept), theirs["kept"], "{name}, {kind}: what is kept");
            match (strict, ours.takes.is_err()) {
                (true, true) => refused_strict += 1,
                (false, true) => refused_lenient += 1,
                _ => {}
            }
            let takes = ours.takes.map(|takes| json!(takes));
            held(
                takes,
                (&theirs["takes"], &theirs["refused"]),
                &format!("{name}, {kind}"),
            );
        }
    }
    assert_eq!((refused_strict, refused_lenient), (22, 7));
    // A folder that cannot be read is as one in which something cannot be.
    let unread = live::taking(Some("[\"theo-97e5\"]"), Scan::Unreadable, false);
    assert_eq!(unread.takes.unwrap().len(), 1);
    assert!(live::taking(None, Scan::Unreadable, true).takes.is_err());
    assert_eq!(
        live::taking(None, Scan::Unreadable, true).keep.as_deref(),
        Some("[]")
    );
}
