//! What is kept and shown of a Codex account's limits, held to the Python Host's own keeping
//! and showing of the same records (`tests/fixtures/codex-usage-vectors.json`, written by
//! `server-rs/tools/codex-usage-vectors.py`): what is told when a record is seen, and what an
//! app is shown when it asks, on whichever login the Host is then.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::BTreeMap;

use clarp_kernel::quota::{self, Kept, Login, Tool};
use serde_json::{Value, json};

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/codex-usage-vectors.json")).unwrap()
}

/// The ids that are drawn, not derived (an episode's, an event's), named by the order they
/// first appear in: two tellings of a story are the same when they name the same things in
/// the same places, whatever the ids are.
#[derive(Default)]
struct Named {
    names: BTreeMap<String, String>,
}

impl Named {
    fn of(&mut self, value: &Value) -> Value {
        match value {
            Value::String(text) if text.starts_with("ple-") || text.starts_with("plp-") => {
                let next = format!("{}{}", &text[..4], self.names.len() + 1);
                json!(self.names.entry(text.clone()).or_insert(next))
            }
            Value::Array(items) => items.iter().map(|item| self.of(item)).collect(),
            Value::Object(fields) => {
                // In the order of the names, whichever order a side wrote them in: the two
                // sides must come upon the drawn ids in the same order.
                let mut keys: Vec<&String> = fields.keys().collect();
                keys.sort();
                let mut out = serde_json::Map::new();
                for key in keys {
                    // A key that follows from drawn ids is checked by itself.
                    if key != "dedupe_key" {
                        out.insert(key.clone(), self.of(&fields[key]));
                    }
                }
                Value::Object(out)
            }
            plain => plain.clone(),
        }
    }
}

/// The keys of events, checked as they are told: an event's key follows from the ids the
/// event carries, the login its episode began under and the event told of that episode
/// before it. Both sides' events go through it (their ids are never the same).
#[derive(Default)]
struct Keys {
    /// The login an episode began under.
    began_under: BTreeMap<String, String>,
    /// The event last told of an episode.
    last_of: BTreeMap<String, String>,
}

impl Keys {
    fn check(&mut self, told: &[Value], under: Option<&str>, what: &str) {
        for event in told {
            let episode = event["episode_id"].as_str().unwrap().to_string();
            let began = self.began_under.entry(episode.clone());
            // An episode is first told of at the record that began it.
            let generation = began.or_insert_with(|| under.expect(what).to_string());
            let key = quota::canonical_id(
                "pld-",
                &json!({
                    "schema_version": 1,
                    "episode_id": event["episode_id"],
                    "provider_instance_id": event["provider_instance_id"],
                    "auth_generation_id": generation,
                    "window_id": event["window_id"],
                    "kind": event["kind"],
                    "threshold_id": event["threshold_id"],
                    "predecessor_event_id": self.last_of.get(&episode),
                }),
                48,
            );
            assert_eq!(event["dedupe_key"], key, "{what}: the key of an event");
            let id = event["provider_limit_event_id"].as_str().unwrap();
            self.last_of.insert(episode, id.to_string());
        }
    }
}

/// A login as a story names it: which login, and whose account where that is known.
fn login(named: &Value) -> Option<Login<'_>> {
    Some(Login {
        generation: named.get(0)?.as_str()?,
        account: named[1].as_str(),
    })
}

#[test]
fn what_is_kept_and_shown_of_an_accounts_limits_is_what_the_python_host_keeps_and_shows() {
    let vectors = vectors();
    let began = vectors["began_ms"].as_i64().unwrap();
    let seen_in = vectors["seen_in"].as_str().unwrap();
    let (mut steps, mut askings, mut events, mut logins) = (0, 0, 0, BTreeMap::new());
    let (mut ended, mut refused, mut used_up) = (0, 0, 0);
    let stories = vectors["stories"].as_array().unwrap();
    for story in stories {
        let name = story["name"].as_str().unwrap();
        // The tool on the Host the story was told on: every id of a window follows from it.
        let asked = story["steps"].as_array().unwrap().iter();
        let instance = asked
            .filter_map(|step| step["provider"]["provider_instance_id"].as_str())
            .next()
            .unwrap()
            .to_string();
        let tool = Tool {
            instance: &instance,
            provider: "codex",
        };
        let mut kept = Kept::default();
        let mut drawn = 0;
        let mut new_id = |prefix: &str| {
            drawn += 1;
            format!("{prefix}drawn-{drawn}")
        };
        let (mut ours, mut theirs) = (Named::default(), Named::default());
        let mut keys = Keys::default();
        for (at, step) in story["steps"].as_array().unwrap().iter().enumerate() {
            let what = format!("{name}, step {at}");
            let now = began + step["at"].as_i64().unwrap() * 1000;
            let told = step["limit_events"].as_array().cloned().unwrap_or_default();
            if let Some(how) = step.get("ended") {
                // A turn of an agent ended. The keeper is told of those of its own tool that
                // completed, and of no other: that is all a Host tells it. What ends with a
                // completed turn is what the Python Host's own ending of such a limit ends.
                let did = if how["tool"] == "codex" && how["kind"] == "done" {
                    kept.completed(tool, login(&step["active"]), now, &mut new_id)
                } else {
                    Vec::new()
                };
                keys.check(&did, None, &what);
                keys.check(&told, None, &what);
                let over = ours.of(&json!(did));
                assert_eq!(over, theirs.of(&json!(told)), "{what}: over with a turn");
                ended += 1;
            } else if step.get("failed_for_a_limit").is_some() {
                // A turn failed for a usage limit, on the login the Host is on, or on none.
                let on = login(&step["active"]);
                let under = on.map_or(quota::NO_LOGIN, |on| on.generation);
                let did = kept.refused(tool, on, now, &mut new_id);
                keys.check(&did, Some(under), &what);
                keys.check(&told, Some(under), &what);
                let limit = ours.of(&json!(did));
                assert_eq!(limit, theirs.of(&json!(told)), "{what}: a refused turn");
                // Told as a limit where none stood, and not again while it stands.
                let new = did
                    .last()
                    .is_some_and(|event| event["source"]["kind"] == quota::REFUSED);
                assert_eq!(json!(new), step["new"], "{what}: whether it is new");
                refused += 1;
            } else if step.get("record").is_some() {
                let on = login(&step["identity"]).unwrap();
                *logins.entry(step["identity"].to_string()).or_insert(0) += 1;
                let did = match &step["snapshot"] {
                    // No window in it: no record. The Python Host refuses it in these words.
                    Value::Null => {
                        let refused = "codex usage response had no usable windows";
                        assert_eq!(step["refused"], refused, "{what}");
                        Vec::new()
                    }
                    snapshot => kept.seen(tool, on, snapshot, seen_in, now, &mut new_id),
                };
                keys.check(&did, Some(on.generation), &what);
                keys.check(&told, Some(on.generation), &what);
                assert_eq!(
                    ours.of(&json!(did)),
                    theirs.of(&json!(told)),
                    "{what}: what is told"
                );
            } else {
                let active = login(&step["active"]);
                let shown = kept.asked(tool, active, now, &mut new_id);
                keys.check(&shown.events, None, &what);
                keys.check(&told, None, &what);
                let at_the_asking = ours.of(&json!(shown.events));
                assert_eq!(
                    at_the_asking,
                    theirs.of(&json!(told)),
                    "{what}: told at the asking"
                );
                let provider = ours.of(&shown.provider);
                assert_eq!(
                    provider,
                    theirs.of(&step["provider"]),
                    "{what}: the provider"
                );
                // The tool's row: as the Python Host's, but for when it says the record was
                // seen and where it says the figures are from.
                let (mut row, mut their_row) = (shown.row, step["row"].clone());
                let seen_any = !their_row["windows"].as_object().unwrap().is_empty();
                for own in ["fetched_at", "freshness", "source"] {
                    let (mine, python) = (row[own].take(), their_row[own].take());
                    if !seen_any {
                        assert_eq!(mine, python, "{what}: {own} where nothing was seen");
                    }
                }
                assert_eq!(row, their_row, "{what}: the tool's row");
                // Whether the account is used up, as an agent's row says it of its tool.
                let of_all = step["used_up"].as_object().unwrap();
                assert!(of_all.keys().all(|of| of == "codex"), "{what}: {of_all:?}");
                let theirs = of_all.get("codex").cloned();
                assert_eq!(kept.used_up(tool, active, now), theirs, "{what}: used up");
                used_up += usize::from(theirs.is_some());
                askings += 1;
            }
            events += told.len();
            steps += 1;
        }
    }
    // Counted by the script that wrote the vectors.
    assert_eq!((stories.len(), steps, askings, events), (59, 251, 133, 76));
    assert_eq!((ended, refused, used_up), (17, 13, 41));
    // And told on more than one login: four of them, one with no account known.
    assert_eq!(logins.len(), 4);
}

#[test]
fn the_tools_row_says_when_its_record_was_seen() {
    let tool = Tool {
        instance: "host:codex",
        provider: "codex",
    };
    let on = Login {
        generation: "a-login",
        account: Some("an-account"),
    };
    let mut kept = Kept::default();
    let mut new_id = |prefix: &str| format!("{prefix}one");
    let resets_at = "2030-03-17T21:46:40Z";
    let window = json!({ "used_percentage": 12.5, "resets_at": resets_at, "window_minutes": 300 });
    let snapshot = json!({
        "used_percentage": 12.5,
        "resets_at": resets_at,
        "windows": { "primary": window },
    });
    let seen_at = 1_900_000_000_000;
    let told = kept.seen(tool, on, &snapshot, "a rollout", seen_at, &mut new_id);
    assert_eq!(told, Vec::<Value>::new());
    let row = kept
        .asked(tool, Some(on), seen_at + 60_000, &mut new_id)
        .row;
    assert_eq!(
        (
            &row["fetched_at"],
            &row["freshness"],
            &row["source"],
            &row["used_percentage"]
        ),
        (
            &json!("2030-03-17T17:46:40Z"),
            &json!("fresh"),
            &json!(quota::SOURCE),
            &json!(12.5)
        )
    );
    // An hour on it is stale; the window itself was stale after a quarter of an hour.
    let later = kept.asked(
        tool,
        Some(on),
        seen_at + quota::ROW_FRESH_MS + 1,
        &mut new_id,
    );
    assert_eq!(later.row["freshness"], "stale");
    assert_eq!(later.provider["windows"][0]["freshness"], "stale");
    // What is kept survives being written down and read again.
    let written = serde_json::to_string(&kept).unwrap();
    let read: Kept = serde_json::from_str(&written).unwrap();
    assert_eq!(read, kept);
}

/// Sixty days of records, one every ten minutes, of windows that say another second for
/// their reset with most records (as Codex does), over a warning in every third window of
/// five hours and under it again: what is kept stays small.
#[test]
fn what_is_kept_does_not_grow_with_the_days() {
    let tool = Tool {
        instance: "host:codex",
        provider: "codex",
    };
    let on = Login {
        generation: "a-login",
        account: Some("an-account"),
    };
    let mut kept = Kept::default();
    let mut drawn = 0;
    let mut new_id = |prefix: &str| {
        drawn += 1;
        format!("{prefix}{drawn}")
    };
    let (began, minute, hour) = (1_900_000_000_000_i64, 60_000_i64, 3_600_000_i64);
    let (mut told, mut most) = (0, (0, 0));
    for record in 0..(60 * 24 * 6) {
        let now = began + record * 10 * minute;
        // The window of five hours this moment is in, its reset a second or two off.
        let window = (now - began) / (5 * hour);
        let five_resets = began + (window + 1) * 5 * hour + (record % 3) * 1_000;
        let week_resets =
            began + ((now - began) / (168 * hour) + 1) * 168 * hour + (record % 7) * 1_000;
        let into = (now - began) % (5 * hour);
        let used = match (window % 3, into > 3 * hour, into > 4 * hour) {
            (0, true, false) => 85.0,
            (0, true, true) => 100.0,
            _ => 20.0,
        };
        let five = json!({ "used_percentage": used, "resets_at": quota::moment(five_resets), "window_minutes": 300 });
        let week = json!({ "used_percentage": 40.0, "resets_at": quota::moment(week_resets), "window_minutes": 10_080 });
        let snapshot = json!({ "windows": { "primary": five, "secondary": week } });
        told += kept
            .seen(tool, on, &snapshot, "a rollout", now, &mut new_id)
            .len();
        told += kept
            .asked(tool, Some(on), now + minute, &mut new_id)
            .events
            .len();
        // Looked at four times a day.
        if record % 36 == 0 {
            let written = serde_json::to_value(&kept).unwrap();
            let (windows, episodes) = (&written["windows"], &written["episodes"]);
            let now_kept = (
                windows.as_array().unwrap().len(),
                episodes.as_array().unwrap().len(),
            );
            most = (most.0.max(now_kept.0), most.1.max(now_kept.1));
        }
    }
    // 96 windows of five hours with a warning, a hard limit and a recovery each: 288 events
    // told, and never more kept than a week's worth of limits that are over.
    assert_eq!(told, 288);
    assert!(most.0 <= 20 && most.1 <= 12, "{most:?}");
}
