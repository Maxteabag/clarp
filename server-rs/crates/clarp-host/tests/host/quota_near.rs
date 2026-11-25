//! An agent's account near a usage limit (issue 738): what its row says, and where another
//! seat would take its turns.

use super::*;

#[test]
fn an_agents_row_says_that_its_account_is_near_a_limit_until_it_is_another() {
    let place = Place::new("claude-near");
    let own = place.root.join("claude-own");
    std::fs::create_dir_all(&own).unwrap();
    // A login that holds, and who Claude Code says is logged in: both made up.
    let token = json!({ "accessToken": "a-token", "expiresAt": now_ms() + 86_400_000 });
    let login = json!({ "claudeAiOauth": token });
    std::fs::write(own.join(".credentials.json"), login.to_string()).unwrap();
    let logged_in = |account: &str| {
        let who = json!({ "accountUuid": account, "emailAddress": "someone@example.org" });
        let said = json!({ "oauthAccount": who });
        std::fs::write(own.join(".claude.json"), said.to_string()).unwrap();
    };
    logged_in("an-account");
    // A stand-in for Claude Code that says of its account's windows what the test wrote
    // down for it, and answers.
    let claude = place.root.join("claude-says-its-windows");
    let script = r#"#!/usr/bin/env python3
import json, pathlib, sys
sys.stdin.readline()
own = pathlib.Path(__file__).resolve().parent / "claude-own"
def say(**line):
    print(json.dumps(line), flush=True)
say(type="system", subtype="init", session_id="stand-in", model="stand-in")
said = own / "windows.json"
if said.exists():
    say(type="rate_limit_event", session_id="stand-in", rate_limit_info=json.loads(said.read_text()))
message = {"id": "msg_1", "role": "assistant", "model": "stand-in", "content": [{"type": "text", "text": "on it"}]}
say(type="assistant", session_id="stand-in", message=message)
say(type="result", subtype="success", is_error=False, session_id="stand-in", result="on it", num_turns=1,
    usage={"input_tokens": 12, "output_tokens": 3})
"#;
    program(&claude, script);
    let world = [
        ("CLARP_CLAUDE_PROGRAM", claude.to_str().unwrap()),
        ("CLAUDE_CONFIG_DIR", own.to_str().unwrap()),
    ];
    let seconds = now_ms() / 1000;
    let says = |five: f64, week: f64| {
        let window = |part: f64, resets: i64| json!({ "utilization": part, "resetsAt": resets, "status": "allowed" });
        let said = json!({
            "status": "allowed", "rateLimitType": "five_hour", "resetsAt": seconds + 4 * 3_600,
            "unifiedWindows": {
                "five_hour": window(five, seconds + 4 * 3_600),
                "seven_day": window(week, seconds + 96 * 3_600),
            },
        });
        std::fs::write(own.join("windows.json"), said.to_string()).unwrap();
    };
    let quota_of = |host: &Host, session: &str| -> Value {
        let snapshot = host.get("/agents/snapshot");
        let agents = snapshot["agents"].as_array().unwrap();
        let row = agents.iter().find(|agent| agent["session"] == session);
        row.unwrap()["backend_quota"].clone()
    };
    let told_again = |heard: &Arc<Mutex<Vec<Value>>>| -> usize {
        let all = heard.lock().unwrap();
        let again =
            |event: &&Value| event["type"] == "agent-roster" && event["kind"] == "backend-quota";
        all.iter().filter(again).count()
    };

    // A Host that switches no accounts (`--accounts none`): no other seat takes a turn.
    let host = place.start_in("", &[], &world);
    assert_eq!(
        host.get("/server-info")["contract"]["features"]["backend_quota_near"],
        167
    );
    let heard = host.listen();
    let made = host.post("/agents", json!({ "name": "Clara", "backend": "claude" }));
    let clara = made["session"].as_str().unwrap().to_string();
    let made = host.post("/agents", json!({ "name": "Cody", "backend": "codex" }));
    let cody = made["session"].as_str().unwrap().to_string();
    assert_eq!(quota_of(&host, &clara), Value::Null);

    // Below four fifths of the five hours, and below nine tenths of the week: nothing.
    says(0.5, 0.85);
    host.send(&clara, "one", "u-1");
    wait_for("the turn to end", || {
        host.get("/backend-usage")["providers"]["claude"]["windows"]
            .as_array()
            .is_some_and(|windows| windows.len() == 2)
    });
    assert_eq!(quota_of(&host, &clara), Value::Null);

    // At 84% of the five hours: near, with the window, how much and when it begins anew.
    says(0.84, 0.85);
    host.send(&clara, "two", "u-2");
    wait_for("the row to say that the account is near", || {
        !quota_of(&host, &clara).is_null()
    });
    let said = quota_of(&host, &clara);
    let resets = &host.get("/backend-usage")["providers"]["claude"]["windows"][0]["resets_at"];
    assert_eq!(
        (
            &said["state"],
            &said["provider_id"],
            &said["reason"],
            &said["window"],
            &said["used_percentage"],
            &said["resets_at"],
            &said["failover"],
        ),
        (
            &json!("near"),
            &json!("claude"),
            &json!("usage_limit"),
            &json!("five_hour"),
            &json!(84.0),
            resets,
            &json!("none"),
        ),
        "{said}"
    );
    assert!(said["observed_at"].is_string(), "{said}");
    assert_eq!(quota_of(&host, &cody), Value::Null);
    wait_for("the apps to be told", || told_again(&heard) == 1);

    // It grows by whole percents: told again, and the row says how much.
    says(0.87, 0.85);
    host.send(&clara, "three", "u-3");
    wait_for("the row to say 87", || {
        quota_of(&host, &clara)["used_percentage"] == 87.0
    });
    wait_for("the apps to be told again", || told_again(&heard) == 2);

    // Another account is logged in: what was said of the one before says nothing of it.
    logged_in("another-account");
    assert_eq!(quota_of(&host, &clara), Value::Null);
    wait_for("the apps to be told it cleared", || told_again(&heard) == 3);
    logged_in("an-account");
    assert_eq!(quota_of(&host, &clara)["used_percentage"], 87.0);
    host.kill();

    // A Host beside another that switches the accounts (`--accounts follow`): whether another
    // seat would take the turn is not this Host's to know, and a warning that cannot be known
    // to hold is not given.
    let host = place.start_in("", &["--accounts", "follow"], &world);
    assert_eq!(quota_of(&host, &clara), Value::Null);
}

/// On a Host that switches the seats itself (`--accounts select`, the owner's selector and a
/// tool that knows the seats), an agent's row warns that its account is near a limit only
/// where no other seat would take its turn: for the model the agent runs, its own or the
/// tool's default, by the seats as they were last read.
#[test]
fn a_near_limit_is_hidden_where_another_seat_would_take_the_agents_model() {
    let place = Place::new("near-select");
    let tool = seat_tool(&place);
    let seconds = now_ms() / 1000;
    // The seats: the one in use, at 84% of its five hours; and a spare one with room, but
    // closed to Opus for the week.
    let usage = |five: f64, scoped: Value| {
        json!({
            "status": "allowed", "limited": false, "used_5h": five, "reset_5h": seconds + 4 * 3_600,
            "used_7d": 0.3, "reset_7d": seconds + 96 * 3_600, "scoped": scoped,
        })
    };
    let opus_full =
        json!([{ "model": "claude-opus", "used": 0.95, "reset": seconds + 96 * 3_600 }]);
    let list = json!({
        "accounts": [
            { "alias": "work", "is_active": true, "usage": usage(0.84, json!([])) },
            { "alias": "spare", "is_active": false, "usage": usage(0.1, opus_full) },
        ],
        "codex": { "accounts": [] },
    });
    let seat = |alias: &str, active: bool| {
        json!({
            "provider": "claude", "alias": alias, "display_alias": alias, "aliases": [alias],
            "plan": "", "active": active, "saved": true, "can_switch": true, "can_launch": true,
            "windows": [], "checked_at": seconds,
        })
    };
    let desk = json!({ "generated_at": seconds, "sessions": 0, "errors": [],
        "accounts": [seat("work", true), seat("spare", false)] });
    for (asking, said) in [
        ("tui", desk),
        ("list", list),
        ("bridge", json!({ "items": [] })),
    ] {
        std::fs::write(place.root.join(format!("{asking}.said")), said.to_string()).unwrap();
    }
    // The owner's selector for Claude, which this test never has run, and the model Claude runs
    // for an agent that names none.
    let selector = place.root.join("selector");
    program(
        &selector,
        "#!/bin/sh\ncat >/dev/null\nprintf '%s' '{\"available\":false}'\n",
    );
    let owner = place.root.join("owner.toml");
    let file = format!(
        "[agents]\nclaude_model = \"claude-opus-5-5\"\nclaude_account_switch_command = [\"{}\"]\n",
        selector.display()
    );
    std::fs::write(&owner, file).unwrap();
    // Claude Code, logged in, and saying its windows as it runs.
    let own = place.root.join("claude-own");
    std::fs::create_dir_all(&own).unwrap();
    let token = json!({ "accessToken": "a-token", "expiresAt": now_ms() + 86_400_000 });
    std::fs::write(
        own.join(".credentials.json"),
        json!({ "claudeAiOauth": token }).to_string(),
    )
    .unwrap();
    let who = json!({ "oauthAccount": { "accountUuid": "an-account" } });
    std::fs::write(own.join(".claude.json"), who.to_string()).unwrap();
    let window = |part: f64, resets: i64| json!({ "utilization": part, "resetsAt": resets, "status": "allowed" });
    let said = json!({
        "status": "allowed", "rateLimitType": "five_hour", "resetsAt": seconds + 4 * 3_600,
        "unifiedWindows": {
            "five_hour": window(0.84, seconds + 4 * 3_600),
            "seven_day": window(0.3, seconds + 96 * 3_600),
        },
    });
    std::fs::write(own.join("windows.json"), said.to_string()).unwrap();
    let claude = place.root.join("claude-says-its-windows");
    let script = r#"#!/usr/bin/env python3
import json, pathlib, sys
sys.stdin.readline()
own = pathlib.Path(__file__).resolve().parent / "claude-own"
def say(**line):
    print(json.dumps(line), flush=True)
say(type="system", subtype="init", session_id="stand-in", model="stand-in")
say(type="rate_limit_event", session_id="stand-in", rate_limit_info=json.loads((own / "windows.json").read_text()))
message = {"id": "msg_1", "role": "assistant", "model": "stand-in", "content": [{"type": "text", "text": "on it"}]}
say(type="assistant", session_id="stand-in", message=message)
say(type="result", subtype="success", is_error=False, session_id="stand-in", result="on it", num_turns=1,
    usage={"input_tokens": 12, "output_tokens": 3})
"#;
    program(&claude, script);
    let world = [
        ("CLARP_CLAUDE_PROGRAM", claude.to_str().unwrap()),
        ("CLAUDE_CONFIG_DIR", own.to_str().unwrap()),
    ];
    let flags = [
        "--accounts",
        "select",
        "--seats-tool",
        tool.to_str().unwrap(),
        "--speech-keys",
        owner.to_str().unwrap(),
    ];
    let host = place.start_in("", &flags, &world);
    let quota_of = |session: &str| -> Value {
        let snapshot = host.get("/agents/snapshot");
        let agents = snapshot["agents"].as_array().unwrap();
        let row = agents.iter().find(|agent| agent["session"] == session);
        row.unwrap()["backend_quota"].clone()
    };
    // One agent on the default model (Opus, by the owner's file), one on Sonnet.
    let made = host.post("/agents", json!({ "name": "Ada", "backend": "claude" }));
    let ada = made["session"].as_str().unwrap().to_string();
    let asked = json!({ "name": "Sam", "backend": "claude", "model": "claude-sonnet-5-5" });
    let sam = host.post("/agents", asked)["session"]
        .as_str()
        .unwrap()
        .to_string();
    assert_eq!(quota_of(&ada), Value::Null);
    let heard = host.listen();
    let told_again = || {
        let all = heard.lock().unwrap();
        let again =
            |event: &&Value| event["type"] == "agent-roster" && event["kind"] == "backend-quota";
        all.iter().filter(again).count()
    };

    // The account is near, and the seats were never read: the Host reads them itself, and
    // until then warns of nothing. The spare seat takes Sonnet's turns, so Sam's row says
    // nothing; it is closed to Opus, so Ada's row warns, knowing that no seat would take her
    // turn.
    host.send(&ada, "one", "u-1");
    wait_for("the Host to read the seats and warn", || {
        quota_of(&ada)["failover"] == "none"
    });
    let status = host.get("/hotseat/status");
    assert_eq!(status["providers"][0]["read_state"], "ok", "{status}");
    assert_eq!(quota_of(&sam), Value::Null);
    let said = quota_of(&ada);
    assert_eq!(
        (&said["state"], &said["window"], &said["used_percentage"]),
        (&json!("near"), &json!("five_hour"), &json!(84.0)),
        "{said}"
    );
    wait_for("the apps to be told of the warning", || told_again() >= 1);
}
