//! What the user's compaction of an agent did, on its row (`last_compaction`, the feature
//! `compaction_outcome`): the Python Host's PR 483 (R148). One file a piece, declared in
//! `host.rs`.

use super::*;

/// A stand-in for Claude Code. Given its own word for compacting as its whole message, it does
/// what the next of `modes` says: `record` compacts and says so with both sizes, as the tool
/// does (recorded on 17 Nov 2026); `slow` does the same, slowly; `none` ends well without
/// a record; `stall` says it is compacting and waits to be stopped; `fail` ends on an error
/// result; `die` ends without a result. Given anything else it answers, and says how much its
/// conversation holds; given words with `auto` in them it first compacts by itself, as the
/// tool does when its conversation is full. It writes down what it was given.
const CLAUDE: &str = r#"#!/usr/bin/env python3
import json, os, sys, time
here = os.path.dirname(os.path.abspath(__file__))
args = sys.argv[1:]
blocks = [part.get("text", "") for part in json.loads(sys.stdin.readline())["message"]["content"]]
named = "--resume" if "--resume" in args else "--session-id"
session = args[args.index(named) + 1]
with open(os.path.join(here, "started"), "a") as out:
    out.write(json.dumps({"blocks": blocks}) + "\n")
with open(os.path.join(here, "started")) as seen:
    given = [json.loads(line)["blocks"] for line in seen]
def say(**event):
    print(json.dumps({**event, "session_id": session}), flush=True)
def boundary(trigger, pre, post):
    held = {"trigger": trigger, "pre_tokens": pre, "post_tokens": post}
    say(type="system", subtype="compact_boundary", compact_metadata=held)
def done():
    say(type="result", subtype="success", is_error=False, result="", num_turns=0,
        usage={"input_tokens": 0, "output_tokens": 0})
if blocks == ["/compact"]:
    with open(os.path.join(here, "modes")) as modes:
        mode = json.load(modes)[sum(1 for said in given if said == ["/compact"]) - 1]
    say(type="system", subtype="init", model="stand-in")
    say(type="system", subtype="status", status="compacting")
    if mode == "stall":
        time.sleep(60)
    if mode == "slow":
        time.sleep(1)
    if mode == "die":
        sys.exit(0)
    if mode == "fail":
        say(type="result", subtype="error_during_execution", is_error=True,
            result="the stand-in could not compact", num_turns=0)
        sys.exit(1)
    say(type="system", subtype="status", status=None, compact_result="success")
    if mode in ("record", "slow"):
        boundary("manual", 30729, 8957)
    if mode == "slow":
        time.sleep(4)
    done()
    sys.exit(0)
say(type="system", subtype="init", model="stand-in")
if "auto" in blocks[-1]:
    boundary("auto", 99999, 11)
usage = {"input_tokens": 29, "cache_read_input_tokens": 30000, "cache_creation_input_tokens": 700, "output_tokens": 3}
answer = {"id": "msg_%d" % len(given), "role": "assistant", "model": "stand-in",
          "content": [{"type": "text", "text": "on it"}], "usage": usage}
say(type="assistant", message=answer)
say(type="result", subtype="success", is_error=False, result="on it", num_turns=1,
    usage={"input_tokens": 30729, "output_tokens": 3})
"#;

#[test]
fn what_the_users_compaction_did_is_said_on_the_agents_row_from_numbers() {
    let place = Place::new("compaction-outcome");
    let tools = place.root.join("tools");
    std::fs::create_dir_all(&tools).unwrap();
    let (claude, codex) = (tools.join("claude"), tools.join("codex"));
    program(&claude, CLAUDE);
    program(&codex, CODEX_AT_WORK);
    let modes = ["record", "none", "stall", "fail", "die", "slow"];
    std::fs::write(tools.join("modes"), json!(modes).to_string()).unwrap();
    // A home of its own, and every other tool at a path where there is none: the Host sees
    // nothing of the machine it runs on.
    let home = place.root.join("home");
    let at = |name: &str| home.join(name).to_str().unwrap().to_string();
    let (config, data, cache, state) = (
        at(".config"),
        at(".local/share"),
        at(".cache"),
        at(".local/state"),
    );
    let nothing = place.root.join("nothing").to_str().unwrap().to_string();
    let world = [
        ("HOME", home.to_str().unwrap()),
        ("XDG_CONFIG_HOME", config.as_str()),
        ("XDG_DATA_HOME", data.as_str()),
        ("XDG_CACHE_HOME", cache.as_str()),
        ("XDG_STATE_HOME", state.as_str()),
        ("CLARP_CLAUDE_PROGRAM", claude.to_str().unwrap()),
        ("CLARP_CODEX_PROGRAM", codex.to_str().unwrap()),
        ("CLARP_AGY_PROGRAM", nothing.as_str()),
        ("CLARP_GROK_PROGRAM", nothing.as_str()),
        ("CLARP_MUSE_PROGRAM", nothing.as_str()),
    ];
    let host = place.start_in("", &[], &world);
    let (_, made) = host.ask(
        "POST",
        "/agents",
        Some(&json!({ "name": "Shorty", "backend": "claude" })),
    );
    let agent = made["session"].as_str().unwrap().to_string();
    let row_of = |host: &Host, session: &str| -> Value {
        let snapshot = host.get("/agents/snapshot");
        let rows = snapshot["agents"].as_array().unwrap();
        rows.iter()
            .find(|row| row["session"] == session)
            .cloned()
            .unwrap()
    };
    let compact = |host: &Host| {
        let (status, told) = host.ask("POST", "/compact", Some(&json!({ "session": agent })));
        assert_eq!(
            (status, &told),
            (
                200,
                &json!({ "ok": true, "status": "started", "backend": "claude" })
            ),
            "the compaction was started"
        );
    };
    // The outcome of the compaction that ends after the one at `before`.
    let outcome = |host: &Host, before: &Value| -> Value {
        wait_for("the compaction to end and be said", || {
            let row = row_of(host, &agent);
            row["compacting"] == false && row["busy"] == false && row["last_compaction"] != *before
        });
        row_of(host, &agent)["last_compaction"].clone()
    };
    let said = |kind: &str, before: Value, after: Value, detail: &str, at: &Value| {
        json!({
            "outcome": kind, "before_tokens": before, "after_tokens": after,
            "after_is_estimate": false, "detail": detail, "backend": "claude", "at": at,
        })
    };

    // The Host says it has the feature, with the contract that brought it.
    let info = host.get("/server-info");
    assert_eq!(
        info["contract"]["features"]["compaction_outcome"], 107,
        "{info}"
    );
    let listed = info["capabilities"]["features"].as_array().unwrap();
    assert!(listed.contains(&json!("compaction_outcome")), "{info}");

    // Until the user compacts the agent, its row says nothing of it.
    let first = row_of(&host, &agent);
    assert_eq!(first.get("last_compaction"), Some(&Value::Null), "{first}");
    host.send(&agent, "first", "u-1");
    wait_for("the first turn to be over", || host.state(&agent) == "done");
    assert_eq!(row_of(&host, &agent)["last_compaction"], Value::Null);

    // Compacted, on the tool's record of it: its size before, and after, which is its own
    // number and no estimate.
    compact(&host);
    let one = outcome(&host, &Value::Null);
    assert!(one["at"].as_i64().unwrap() > 0, "{one}");
    let words = "Claude Code recorded the compaction";
    assert_eq!(
        one,
        said("compacted", json!(30_729), json!(8_957), words, &one["at"])
    );
    assert_eq!(row_of(&host, &agent)["context_tokens"], 8_957);

    // A turn of the user's in which the tool compacts by itself says nothing of the user's
    // compaction: the row keeps what that did.
    host.send(&agent, "second, auto", "u-2");
    wait_for("the second turn to be over", || {
        let row = row_of(&host, &agent);
        row["busy"] == false && row["context_tokens"] == 30_729
    });
    assert_eq!(row_of(&host, &agent)["last_compaction"], one);

    // No record: not compacted, the size before is how full the conversation was.
    compact(&host);
    let two = outcome(&host, &one);
    let words = "Claude Code recorded no compaction";
    assert_eq!(
        two,
        said(
            "not_compacted",
            json!(30_729),
            Value::Null,
            words,
            &two["at"]
        )
    );
    assert!(two["at"].as_i64() >= one["at"].as_i64(), "{two}");

    // A message sent while the tool compacts waits behind the compaction, and nothing of it
    // ends the compaction. Stopped before the tool recorded it: not compacted. The Stop holds
    // what waited, and a compaction asked for while that waits is refused in the Python
    // Host's words.
    compact(&host);
    wait_for("the row to say it is compacting", || {
        row_of(&host, &agent)["compacting"] == true
    });
    host.send(&agent, "third, sent while it compacts", "u-3");
    wait_for("the message to wait", || {
        row_of(&host, &agent)["queued_turn_count"] == 1
    });
    assert_eq!(
        row_of(&host, &agent)["compacting"],
        true,
        "the message ends nothing"
    );
    host.post("/stop", json!({ "session": agent }));
    let three = outcome(&host, &two);
    let words = "stopped before Claude Code recorded a compaction";
    assert_eq!(
        three,
        said(
            "not_compacted",
            json!(30_729),
            Value::Null,
            words,
            &three["at"]
        )
    );
    assert_eq!(
        row_of(&host, &agent)["queued_turn_count"],
        1,
        "the Stop holds it"
    );
    let (status, told) = host.ask("POST", "/compact", Some(&json!({ "session": agent })));
    assert_eq!(
        (status, &told["ok"], &told["error"]),
        (
            409,
            &json!(false),
            &json!("messages are waiting for this agent \u{2014} try again when they have run")
        )
    );
    assert_eq!(
        row_of(&host, &agent)["last_compaction"],
        three,
        "a refusal says nothing"
    );
    host.post("/turn-queue/resume", json!({ "session": agent }));
    wait_for("what waited to run", || {
        let row = row_of(&host, &agent);
        row["queued_turn_count"] == 0 && row["busy"] == false
    });

    // The tool said it failed: not compacted, in the words the chat shows of the failure.
    compact(&host);
    let four = outcome(&host, &three);
    assert_eq!(four["outcome"], "not_compacted", "{four}");
    assert_eq!(four["before_tokens"], 30_729, "{four}");
    let whole = host.get(&format!("/log?session={agent}&limit=200"));
    let shown = whole["turns"].as_array().unwrap();
    assert!(
        shown.iter().any(|row| row["text"] == four["detail"]),
        "the chat shows the same words: {four} {whole}"
    );

    // The tool ended without saying how it went: not known.
    compact(&host);
    let five = outcome(&host, &four);
    let words =
        "Claude Code ended without saying how it went, so whether it compacted is not known";
    assert_eq!(
        five,
        said("unknown", json!(30_729), Value::Null, words, &five["at"])
    );

    // The Host goes after the tool recorded the compaction and before its turn ended: the Host
    // that comes back reads again what the tool said, and says what it did.
    compact(&host);
    wait_for("the row to say it is compacting", || {
        row_of(&host, &agent)["compacting"] == true
    });
    wait_for("the tool's record to be read", || {
        let row = row_of(&host, &agent);
        row["busy"] == true && row["latest_state"] == "thinking"
    });
    host.kill();
    let host = place.start_in("", &[], &world);
    let six = outcome(&host, &five);
    let words = "Claude Code recorded the compaction";
    assert_eq!(
        six,
        said("compacted", json!(30_729), json!(8_957), words, &six["at"])
    );
    // And another Host after that still has it.
    host.kill();
    let host = place.start_in("", &[], &world);
    assert_eq!(row_of(&host, &agent)["last_compaction"], six);
    let given = std::fs::read_to_string(tools.join("started")).unwrap();
    let compactions = given
        .lines()
        .filter(|line| line.contains("\"/compact\""))
        .count();
    assert_eq!(
        compactions,
        modes.len(),
        "each compaction ran once: {given}"
    );

    // A tool this Host does not compact is refused in words, and its row says nothing.
    let (_, made) = host.ask(
        "POST",
        "/agents",
        Some(&json!({ "name": "Cody", "backend": "codex" })),
    );
    let cody = made["session"].as_str().unwrap().to_string();
    host.send(&cody, "run the tests", "u-9");
    wait_for("the turn of Codex to be over", || {
        let row = row_of(&host, &cody);
        row["busy"] == false && !row["backend_session_id"].as_str().unwrap_or("").is_empty()
    });
    let (status, told) = host.ask("POST", "/compact", Some(&json!({ "session": cody })));
    assert_eq!(
        (status, &told["error"]),
        (409, &json!("compaction unsupported for codex"))
    );
    assert_eq!(row_of(&host, &cody)["last_compaction"], Value::Null);
}
