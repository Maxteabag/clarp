//! What Claude Code says of its account's windows as it runs, and what is read of it.
//!
//! The shape is what Claude Code printed on the machine this was written on, on 17 Nov 2026
//! (the names and kinds of what a `rate_limit_event` carries; in four recordings what is used
//! of a window was a part of the whole, never a percentage). The figures here are made up.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_backend::claude::ClaudeInterpreter;
use clarp_backend::{Interpreter, Signal, limits};
use clarp_spool::{Entry, Record};
use serde_json::{Value, json};

fn info() -> Value {
    json!({
        "status": "allowed_warning", "rateLimitType": "seven_day", "resetsAt": 1_900_400_000,
        "utilization": 0.82, "overageStatus": "rejected",
        "overageDisabledReason": "out_of_credits", "isUsingOverage": false,
        "unifiedWindows": {
            "five_hour": { "utilization": 0.375, "resetsAt": 1_900_014_400, "status": "allowed" },
            "seven_day": { "utilization": 0.82, "resetsAt": 1_900_400_000, "status": "allowed_warning" },
        },
    })
}

#[test]
fn the_windows_claude_code_says_are_read_in_percent_with_their_resets() {
    let read = limits::claude(&info());
    let five = json!({ "used_percentage": 37.5, "resets_at": "2030-03-17T21:46:40Z", "window_minutes": 300 });
    let week = json!({ "used_percentage": 82.0, "resets_at": "2030-03-22T08:53:20Z", "window_minutes": 10_080 });
    assert_eq!(read, json!({ "five_hour": five, "seven_day": week }));
    // The five hours first, as an app lists them.
    let kinds: Vec<&String> = read.as_object().unwrap().keys().collect();
    assert_eq!(kinds, ["five_hour", "seven_day"]);
}

#[test]
fn a_part_of_the_whole_is_said_in_percent_as_a_person_would_write_it() {
    let said = |part: Value| {
        let window = json!({ "utilization": part, "resetsAt": 1_900_014_400 });
        let read = limits::claude(&json!({ "unifiedWindows": { "five_hour": window } }));
        read["five_hour"]["used_percentage"].clone()
    };
    for (part, percent) in [
        (json!(0), json!(0.0)),
        (json!(0.07), json!(7.0)),
        (json!(0.57), json!(57.0)),
        (json!(0.29), json!(29.0)),
        (json!(0.123456789), json!(12.3457)),
        (json!(1), json!(100.0)),
        (json!(1.0), json!(100.0)),
        (json!(1.045), json!(104.5)),
    ] {
        assert_eq!(said(part.clone()), percent, "{part}");
    }
}

#[test]
fn what_is_not_said_is_left_out() {
    let read = |windows: Value| limits::claude(&json!({ "unifiedWindows": windows }));
    // A window that does not say how much is used: left out. Said as a text, or as nothing
    // a number is: the same.
    let week = json!({ "utilization": 0.5, "resetsAt": 1_900_400_000 });
    for five in [
        json!({ "resetsAt": 1_900_014_400 }),
        json!({ "utilization": null, "resetsAt": 1_900_014_400 }),
        json!({ "utilization": "0.4", "resetsAt": 1_900_014_400 }),
        json!("all of it"),
    ] {
        let read = read(json!({ "five_hour": five, "seven_day": week }));
        let kinds: Vec<&String> = read.as_object().unwrap().keys().collect();
        assert_eq!(kinds, ["seven_day"], "{five}");
    }
    // One that does not say when it begins anew says nothing for that.
    for unsaid in [
        json!({ "utilization": 0.4 }),
        json!({ "utilization": 0.4, "resetsAt": "soon" }),
    ] {
        let read = read(json!({ "five_hour": unsaid }));
        let window = json!({ "used_percentage": 40.0, "resets_at": "", "window_minutes": 300 });
        assert_eq!(read, json!({ "five_hour": window }), "{unsaid}");
    }
    // A window of another name is none of the two.
    let other =
        read(json!({ "seven_day_opus": { "utilization": 0.4, "resetsAt": 1_900_400_000 } }));
    assert_eq!(other, json!({}));
    // And where nothing of the kind is said, nothing is read.
    for nothing in [
        json!({}),
        json!({ "status": "allowed", "rateLimitType": "five_hour" }),
        json!({ "unifiedWindows": [] }),
        json!({ "unifiedWindows": "none" }),
        json!(null),
        json!("a text"),
    ] {
        assert_eq!(limits::claude(&nothing), json!({}), "{nothing}");
    }
}

#[test]
fn claude_code_tells_its_windows_where_it_says_them_and_not_otherwise() {
    let mut interpreter = ClaudeInterpreter::new(1, "m1");
    let mut out = Vec::new();
    // As the recordings of the fixtures have it, without windows; then with them; then as
    // the account being out, with them.
    let bare = json!({ "status": "allowed", "rateLimitType": "five_hour" });
    let mut out_of_it = info();
    out_of_it["status"] = json!("rejected");
    for (t, said) in [bare, info(), out_of_it.clone()].into_iter().enumerate() {
        let line =
            json!({ "type": "rate_limit_event", "session_id": "s", "rate_limit_info": said });
        let record = Record {
            t: i64::try_from(t).unwrap(),
            entry: Entry::Out(line.to_string()),
        };
        interpreter.on_record(&record, &mut out);
    }
    let told: Vec<&Signal> = out
        .iter()
        .filter(|signal| matches!(signal, Signal::Limits(_)))
        .collect();
    assert_eq!(told, [&Signal::Limits(info()), &Signal::Limits(out_of_it)]);
}
