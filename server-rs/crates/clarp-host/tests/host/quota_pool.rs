//! The usage warnings know the pool of seats (contract 176, feature `quota_pool`): an agent's
//! row says that its tool's account is used up only where no seat of the pool would take its
//! turn, and the usage screen is given a summary of each pool.

use super::*;

/// A Host that switches the Claude seats itself (`--accounts select`, the owner's selector and
/// a tool that knows the seats), whose seat in use, `work`, is used up until four hours from
/// now, beside a seat `spare` that the tool says as `spare` (its usage), and an agent on Claude
/// whose last turn found its account used up. The seats are not read yet, or, where
/// `read_fails`, cannot be: the tool fails to list them.
fn used_up_beside(name: &str, spare: Value, read_fails: bool) -> (Place, Host, String) {
    let place = Place::new(name);
    let tool = seat_tool(&place);
    if read_fails {
        std::fs::write(place.root.join("list.exit"), "1").unwrap();
    }
    let seconds = now_ms() / 1000;
    let work = json!({
        "status": "rejected", "limited": true, "used_5h": 1.0, "reset_5h": seconds + 4 * 3_600,
        "used_7d": 0.4, "reset_7d": seconds + 96 * 3_600, "scoped": [],
    });
    let list = json!({
        "accounts": [
            { "alias": "work", "is_active": true, "usage": work },
            { "alias": "spare", "is_active": false, "usage": spare },
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
    // The owner's selector for Claude, which this test never has run.
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
    // Claude Code, logged in, saying that all of its five hours is used, and failing the turn.
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
            "five_hour": window(1.0, seconds + 4 * 3_600),
            "seven_day": window(0.4, seconds + 96 * 3_600),
        },
    });
    std::fs::write(own.join("windows.json"), said.to_string()).unwrap();
    std::fs::write(own.join("fails"), "").unwrap();
    let claude = place.root.join("claude-says-its-windows");
    let script = r#"#!/usr/bin/env python3
import json, pathlib, sys
sys.stdin.readline()
own = pathlib.Path(__file__).resolve().parent / "claude-own"
def say(**line):
    print(json.dumps(line), flush=True)
say(type="system", subtype="init", session_id="stand-in", model="stand-in")
say(type="rate_limit_event", session_id="stand-in", rate_limit_info=json.loads((own / "windows.json").read_text()))
if (own / "fails").exists():
    sys.exit(1)
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
    let made = host.post("/agents", json!({ "name": "Ada", "backend": "claude" }));
    let ada = made["session"].as_str().unwrap().to_string();
    host.send(&ada, "one", "u-1");
    // The turn is over once what Claude Code said of its windows is kept. The row is not
    // waited for: the Host may read the seats as soon as it says the account is used up.
    wait_for("Claude Code's word to be kept", || {
        host.get("/backend-usage")["providers"]["claude"]["windows"][0]["used_percentage"] == 100.0
    });
    (place, host, ada)
}

fn quota_of(host: &Host, session: &str) -> Value {
    let snapshot = host.get("/agents/snapshot");
    let agents = snapshot["agents"].as_array().unwrap();
    let row = agents.iter().find(|agent| agent["session"] == session);
    row.unwrap()["backend_quota"].clone()
}

fn told_again(heard: &Arc<Mutex<Vec<Value>>>) -> usize {
    let all = heard.lock().unwrap();
    let again =
        |event: &&Value| event["type"] == "agent-roster" && event["kind"] == "backend-quota";
    all.iter().filter(again).count()
}

#[test]
fn a_used_up_account_is_said_only_where_no_seat_of_the_pool_would_take_the_turn() {
    let seconds = now_ms() / 1000;
    let work_resets = clarp_kernel::quota::moment((seconds + 4 * 3_600) * 1000);

    // A spare seat with room.
    let room = json!({
        "status": "allowed", "limited": false, "used_5h": 0.2, "reset_5h": seconds + 3_600,
        "used_7d": 0.3, "reset_7d": seconds + 96 * 3_600, "scoped": [],
    });
    let (_place, host, ada, heard) = host_listening("pool-room", room);
    assert_eq!(
        host.get("/server-info")["contract"]["features"]["quota_pool"],
        176
    );
    // The seats were never read: the row asks the Host to read them, and the spare seat
    // would take Ada's turn, so her row says nothing, and the apps are told to read it again.
    wait_for("the Host to read the seats and clear the row", || {
        quota_of(&host, &ada).is_null()
            && host.get("/hotseat/status")["providers"][0]["read_state"] == "ok"
    });
    wait_for("the apps to be told the pool takes the turn", || {
        told_again(&heard) >= 1
    });
    let pool = &host.get("/backend-usage")["providers"]["claude"]["pool"];
    assert_eq!(
        (
            &pool["seats"],
            &pool["with_room"],
            &pool["used_up"],
            &pool["active_alias"],
            &pool["next_reset_at"],
            &pool["regains_room_at"],
            &pool["stale"],
        ),
        (
            &json!(2),
            &json!(1),
            &json!(1),
            &json!("work"),
            &json!(work_resets),
            &Value::Null,
            &json!(false),
        ),
        "{pool}"
    );
    assert!(pool["observed_at"].is_string(), "{pool}");
    // Were the seat in use used up, the Host would move to the spare one.
    assert_eq!(pool["next_alias"], "spare", "{pool}");
    // Codex has no seats read: no pool.
    assert!(host.get("/backend-usage")["providers"]["codex"]["pool"].is_null());
    host.kill();

    // The spare seat is used up too, and back in an hour: the row says so, and names the hour,
    // the soonest a seat regains room, not the four hours of the seat in use.
    let spare_resets = clarp_kernel::quota::moment((seconds + 3_600) * 1000);
    let out = json!({
        "status": "rejected", "limited": true, "used_5h": 1.0, "reset_5h": seconds + 3_600,
        "used_7d": 0.3, "reset_7d": seconds + 96 * 3_600, "scoped": [],
    });
    let (_place, host, ada) = used_up_beside("pool-out", out, false);
    wait_for(
        "the Host to read the seats and name the pool's reset",
        || quota_of(&host, &ada)["failover"] == "none",
    );
    let said = quota_of(&host, &ada);
    assert_eq!(
        (
            &said["state"],
            &said["window"],
            &said["used_percentage"],
            &said["resets_at"]
        ),
        (
            &json!("exhausted"),
            &json!("five_hour"),
            &json!(100.0),
            &json!(spare_resets)
        ),
        "{said}"
    );
    let pool = &host.get("/backend-usage")["providers"]["claude"]["pool"];
    assert_eq!(
        (
            &pool["with_room"],
            &pool["used_up"],
            &pool["next_reset_at"],
            &pool["regains_room_at"]
        ),
        (
            &json!(0),
            &json!(2),
            &json!(spare_resets),
            &json!(spare_resets)
        ),
        "{pool}"
    );
    // No seat has room: none to move to.
    assert_eq!(pool["next_alias"], Value::Null, "{pool}");
}

/// The Host made, and listened to, before the turn that uses its account up, so that what the
/// apps are told of it is heard.
fn host_listening(name: &str, spare: Value) -> (Place, Host, String, Arc<Mutex<Vec<Value>>>) {
    let (place, host, ada) = used_up_beside(name, spare, false);
    let heard = host.listen();
    (place, host, ada, heard)
}

#[test]
fn where_the_seats_cannot_be_read_no_warning_is_given_and_a_used_up_account_stands() {
    let seconds = now_ms() / 1000;
    let work_resets = clarp_kernel::quota::moment((seconds + 4 * 3_600) * 1000);
    let room = json!({
        "status": "allowed", "limited": false, "used_5h": 0.2, "reset_5h": seconds + 3_600,
        "used_7d": 0.3, "reset_7d": seconds + 96 * 3_600, "scoped": [],
    });
    let (place, host, ada) = used_up_beside("pool-unread", room, true);
    // The Host tries to read the seats, and the tool fails: whether another seat would take the
    // turn is not known, and a used-up account is never hidden.
    wait_for("the Host to try to read the seats", || {
        // An app reads the agents' rows, as the apps do: what has the Host read the seats.
        quota_of(&host, &ada);
        std::fs::read_to_string(place.root.join("seat-tool.asked"))
            .is_ok_and(|asked| asked.lines().any(|line| line.starts_with("list")))
    });
    let said = quota_of(&host, &ada);
    assert_eq!(
        (&said["state"], &said["failover"], &said["resets_at"]),
        (&json!("exhausted"), &json!("unknown"), &json!(work_resets)),
        "{said}"
    );
    assert!(host.get("/backend-usage")["providers"]["claude"]["pool"].is_null());
    // A turn completes at 84% of the five hours: the account is near its limit, but whether
    // the pool is cannot be told, so no warning is given.
    let own = place.root.join("claude-own");
    std::fs::remove_file(own.join("fails")).unwrap();
    let window = |part: f64, resets: i64| json!({ "utilization": part, "resetsAt": resets, "status": "allowed" });
    let said = json!({
        "status": "allowed", "rateLimitType": "five_hour", "resetsAt": seconds + 4 * 3_600,
        "unifiedWindows": {
            "five_hour": window(0.84, seconds + 4 * 3_600),
            "seven_day": window(0.4, seconds + 96 * 3_600),
        },
    });
    std::fs::write(own.join("windows.json"), said.to_string()).unwrap();
    host.send(&ada, "two", "u-2");
    wait_for("the turn's word to be kept", || {
        host.get("/backend-usage")["providers"]["claude"]["windows"][0]["used_percentage"] == 84.0
    });
    assert_eq!(quota_of(&host, &ada), Value::Null);
}
