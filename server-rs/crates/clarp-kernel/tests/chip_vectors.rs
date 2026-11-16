//! The "agents working" chip, against the Python Host's own module.
//!
//! `fixtures/chip-vectors.json` is written by `server-rs/tools/chip-vectors.py`: the name
//! that module shows an agent by, the line it says of what an agent does, what it makes of
//! fleets made up there, the register of activities a phone leaves, and what its pacer sends
//! through scripts of events on a clock of the script's own. The same must come of the same
//! here.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_kernel::chip::{
    self, Attention, Budget, EVERY_MS, Fleet, Left, MOST_ROWS, Pacer, Register, Row,
    SHAPE_FLOOR_MS, STALE_AFTER_S, Shown, Spent,
};
use serde_json::{Value, json};

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/chip-vectors.json")).unwrap()
}

fn text(value: &Value, key: &str) -> String {
    value[key].as_str().unwrap_or("").to_string()
}

/// An agent as the vectors wrote it: as the stream of turns told of it, or as the Host
/// knows it.
fn shown(row: &Value) -> Shown {
    let there = |key: &str| row.get(key).is_some_and(|value| !value.is_null());
    Shown {
        session: text(row, "session"),
        persona: text(row, "persona"),
        helper_of: if row["role"] == "helper" {
            text(row, "parent_agent_id")
        } else {
            String::new()
        },
        archived: there("archived_at"),
        janitor: there("is_janitor"),
        activity: row.get("activity").cloned().unwrap_or(Value::Null),
    }
}

fn fleet(agents: &Value) -> Fleet {
    let agents = agents.as_object().into_iter().flatten();
    agents
        .map(|(agent_id, row)| (agent_id.clone(), shown(row)))
        .collect()
}

fn attention(told: &Value) -> Attention {
    let texts = |list: &Value| -> Vec<String> {
        let list = list.as_array().into_iter().flatten();
        list.map(|item| item.as_str().unwrap().to_string())
            .collect()
    };
    Attention {
        count: told["count"].as_i64().unwrap_or(0),
        items: told.get("items").map(texts),
        agent_ids: texts(&told["agent_ids"]).into_iter().collect(),
    }
}

#[test]
fn an_agent_is_shown_by_the_name_and_the_line_the_python_host_gives_it() {
    let vectors = vectors();
    assert_eq!(vectors["most_rows"], json!(MOST_ROWS));
    assert_eq!(
        vectors["structural_floor_sec"].as_f64().unwrap() * 1000.0,
        SHAPE_FLOOR_MS as f64
    );
    assert_eq!(vectors["stale_after_sec"], json!(STALE_AFTER_S));
    let names = vectors["names"].as_array().unwrap();
    for row in names {
        let name = row["name"].as_str().unwrap();
        assert_eq!(json!(chip::shown_name(name)), row["shown"], "{name:?}");
    }
    let activities = vectors["activities"].as_array().unwrap();
    for case in activities {
        let activity = &case["activity"];
        assert_eq!(
            json!(chip::headline(activity)),
            case["headline"],
            "{activity}"
        );
        let theo = Shown {
            session: "theo".to_string(),
            persona: "Theo".to_string(),
            activity: activity.clone(),
            ..Shown::default()
        };
        assert_eq!(json!(Row::of("a1", &theo)), case["row"], "{activity}");
    }
    assert!(names.len() >= 26 && activities.len() >= 37);
}

#[test]
fn the_chip_says_of_a_fleet_what_the_python_host_says() {
    let vectors = vectors();
    let fleets = vectors["fleets"].as_array().unwrap();
    let (mut led, mut needing, mut rolled_up) = (0, 0, 0);
    for case in fleets {
        let name = case["name"].as_str().unwrap();
        let (seen, roster) = (fleet(&case["agents"]), fleet(&case["roster"]));
        let badge = attention(&case["attention"]);
        let now = case["state"]["updated_at_ms"].as_i64().unwrap();
        let state = chip::content(&seen, &roster, &badge, now);
        assert_eq!(state, case["state"], "{name}");
        led += usize::from(!state["lead"].is_null());
        needing += usize::from(state["needs_you"] != 0);
        let rows = state["agents"].as_array().unwrap().iter();
        rolled_up += rows
            .filter(|row| row["headline"].as_str().unwrap().contains("sub-agent"))
            .count();
    }
    assert!(
        fleets.len() >= 34 && led >= 20 && needing >= 10 && rolled_up >= 5,
        "{led} led, {needing} needing, {rolled_up} rolled up"
    );
}

#[test]
fn a_phone_leaves_an_activity_and_takes_it_back_as_on_the_python_host() {
    let vectors = vectors();
    let mut register = Register::default();
    let steps = vectors["registered"].as_array().unwrap();
    let mut refused = 0;
    for row in steps {
        let step = &row["step"];
        let said = |key: &str| step[key].as_str().unwrap_or("");
        match said("do") {
            "register" => {
                let left = Left {
                    token: said("token"),
                    activity_id: said("activity_id"),
                    kind: said("kind"),
                    environment: said("environment"),
                    principal: said("principal"),
                };
                match register.register(&left, 1_800_000_000_000) {
                    Ok(activity) => {
                        let mut told = json!(activity);
                        told["registered_at"] = json!("<now>");
                        assert_eq!(told, row["result"], "{step}");
                        assert_eq!(row["refused"], Value::Null, "{step}");
                    }
                    Err(why) => {
                        refused += 1;
                        assert_eq!(json!(why), row["refused"], "{step}");
                    }
                }
            }
            _ => {
                let removed = register.unregister(said("activity_id"), said("token"));
                assert_eq!(json!(removed), row["result"], "{step}");
            }
        }
        let kept: Vec<Value> = register
            .activities()
            .map(|activity| {
                let mut kept = json!(activity);
                kept["registered_at"] = json!("<now>");
                kept
            })
            .collect();
        assert_eq!(json!(kept), row["kept"], "after {step}");
        // What is kept is read back as it was.
        let again = Register::kept(register.activities().cloned().collect());
        assert_eq!(again, register);
    }
    assert!(steps.len() >= 17 && refused >= 5, "{refused} refused");
}

#[test]
fn the_chip_is_pushed_when_the_python_host_pushes_it() {
    let vectors = vectors();
    let roster = fleet(&vectors["known"]);
    let (mut steps, mut pushes, mut held) = (0, 0, 0);
    for script in vectors["paced"].as_array().unwrap() {
        let name = script["name"].as_str().unwrap();
        let mut pacer = Pacer::default();
        let mut now: i64 = 1_800_000_000_000;
        for (place, row) in script["steps"].as_array().unwrap().iter().enumerate() {
            let step = &row["step"];
            let mut sent = Vec::new();
            match step["do"].as_str().unwrap() {
                "observe" => sent.extend(pacer.observe(&step["event"], &roster, now)),
                "attention" => {
                    let badge = attention(&step["attention"]);
                    sent.extend(pacer.attention_is(badge, &roster, now));
                }
                _ => {
                    let seconds = step["seconds"].as_i64().unwrap();
                    let until = now + seconds * 1000;
                    // What was held back goes when its time has come.
                    while let Some(wake) = pacer.wake_at().filter(|wake| *wake <= until) {
                        now = wake;
                        sent.extend(pacer.woken(&roster, now));
                    }
                    now = until;
                }
            }
            let here = format!("{name}, step {place}: {step}");
            assert_eq!(
                json!((now as f64) / 1000.0),
                row["now"],
                "the clock of the script, {here}"
            );
            let sent: Vec<Value> = sent
                .iter()
                .map(|push| {
                    let state = &push.state;
                    let at = state["updated_at_ms"].as_i64().unwrap();
                    json!({
                        "payload": chip::payload(state, at), "priority": push.priority,
                        "high": push.high,
                    })
                })
                .collect();
            assert_eq!(json!(sent), row["sent"], "{here}");
            let waits = pacer.wake_at().map(|wake| (wake - now) as f64 / 1000.0);
            assert_eq!(json!(waits), row["timer_in"], "{here}");
            steps += 1;
            pushes += sent.len();
            held += usize::from(waits.is_some());
        }
    }
    assert!(
        steps >= 60 && pushes >= 20 && held >= 15,
        "{steps} steps, {pushes} pushes, {held} held"
    );
    assert_eq!(EVERY_MS, 15_000);
}

#[test]
fn the_chips_share_of_the_push_budget_is_kept_as_the_python_host_keeps_it() {
    let vectors = vectors();
    let (mut steps, mut taken_in_all, mut refused, mut woken_in_all) = (0, 0, 0, 0);
    for script in vectors["budgeted"].as_array().unwrap() {
        let name = script["name"].as_str().unwrap();
        // A gate on a clock of the script's own, which starts at a midnight.
        let mut gate: Budget<(Value, String)> = Budget::new(Spent::Nothing);
        let mut now: f64 = 20_000.0 * 86_400.0;
        let mut taken: Vec<(String, (Value, String))> = Vec::new();
        for (place, row) in script["steps"].as_array().unwrap().iter().enumerate() {
            let step = &row["step"];
            let here = format!("{name}, step {place}: {step}");
            let (mut changed, mut woken) = (false, 0);
            let result = match step["do"].as_str().unwrap() {
                "offer" => {
                    let tokens: Vec<String> = step["tokens"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .map(|token| token.as_str().unwrap().to_string())
                        .collect();
                    let what = (step["state"].clone(), text(step, "priority"));
                    gate.offer(&tokens, &what);
                    Value::Null
                }
                "due" => {
                    taken = gate.due(now);
                    taken_in_all += taken.len();
                    let told = taken
                        .iter()
                        .map(|(token, (state, priority))| json!([token, state, priority]));
                    json!(told.collect::<Vec<_>>())
                }
                "reserve" => {
                    let allowed = gate.reserve(now, &mut changed);
                    refused += usize::from(!allowed);
                    json!(allowed)
                }
                "reserve_many" => {
                    let asked = step["count"].as_u64().unwrap();
                    let allowed = (0..asked).filter(|_| gate.reserve(now, &mut changed));
                    json!(allowed.count())
                }
                "delivered" => {
                    gate.delivered();
                    Value::Null
                }
                "limited" => {
                    let wait = step["retry_after"].as_f64().unwrap();
                    gate.rate_limited(wait, step["day"] == true, now, &mut changed);
                    Value::Null
                }
                "requeue" => {
                    let count = step["count"].as_u64().unwrap() as usize;
                    let back = taken[taken.len() - count..].to_vec();
                    gate.requeue(back, now);
                    Value::Null
                }
                _ => {
                    let seconds = step["seconds"].as_f64().unwrap();
                    let until = now + seconds;
                    // What the gate asked to be looked at for goes when its time has come.
                    while seconds > 0.0 && gate.wake_at().is_some_and(|wake| wake <= until) {
                        gate.woken();
                        woken += 1;
                    }
                    now = until;
                    Value::Null
                }
            };
            assert_eq!(json!(now), row["now"], "the clock of the script, {here}");
            assert_eq!(result, row["result"], "{here}");
            assert_eq!(json!(woken), row["woken"], "{here}");
            let waits = gate
                .wake_at()
                .map(|wake| ((wake - now) * 1e6).round() / 1e6);
            assert_eq!(json!(waits), row["timer_in"], "{here}");
            assert_eq!(json!(gate.paused(now)), row["paused"], "{here}");
            assert_eq!(json!(gate.floor_s()), row["floor"], "{here}");
            let waiting: Vec<&str> = gate.waiting().collect();
            assert_eq!(json!(waiting), row["waiting"], "{here}");
            assert_eq!(json!(gate.spent().day()), row["day"], "{here}");
            // What is kept of the day is read back as it was written.
            if let Some(day) = gate.spent().day() {
                let written = serde_json::to_vec(day).unwrap();
                assert_eq!(&Spent::kept(Some(&written)), gate.spent(), "{here}");
            }
            let _ = changed;
            steps += 1;
            woken_in_all += woken;
        }
    }
    assert!(
        steps >= 110 && taken_in_all >= 40 && refused >= 8 && woken_in_all >= 8,
        "{steps} steps, {taken_in_all} taken, {refused} refused, {woken_in_all} woken"
    );
    // What cannot be read, or says more than a day's budget holds, spends nothing.
    for kept in [
        &b"not json"[..],
        br#"{"count":301,"day":1,"last_wall":86400.0,"paused_until":0.0,"version":1}"#,
    ] {
        let mut gate: Budget<()> = Budget::new(Spent::kept(Some(kept)));
        let mut changed = false;
        assert!(!gate.reserve(86_500.0, &mut changed));
        assert!(!changed && gate.paused(86_559.0) && !gate.paused(86_560.0));
    }
    assert_eq!(Spent::kept(None), Spent::Nothing);
}
