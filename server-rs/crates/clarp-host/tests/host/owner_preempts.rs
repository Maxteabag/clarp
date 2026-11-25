//! Issue 839: the owner's message never waits behind a turn that is not a person's. Where
//! that turn takes it on its input, it is steered in; where it cannot, the turn makes way.
use super::*;

/// A Claude Code stand-in: the peer's work reads one more message on its input, says it read
/// it, and answers both; anything else is answered at once with its last line.
const CLAUDE: &str = r#"#!/usr/bin/env python3
import json, pathlib, sys

if "--version" in sys.argv:
    print("2.1.321 (Claude Code)")
    raise SystemExit(0)

root = pathlib.Path(__file__).resolve().parent
flag = "--resume" if "--resume" in sys.argv else "--session-id"
session = sys.argv[sys.argv.index(flag) + 1]

def say(kind, **fields):
    print(json.dumps({"type": kind, "session_id": session, **fields}), flush=True)

def prompt():
    record = json.loads(sys.stdin.readline())
    return record["uuid"], record["message"]["content"][0]["text"]

def answer(text, *uids):
    say("assistant", message={"id": "msg_" + uids[-1], "role": "assistant",
        "content": [{"type": "text", "text": text}]})
    say("result", subtype="success", is_error=False, result=text, user_message_uuids=list(uids))

uid, text = prompt()
say("system", subtype="init")
say("user", isReplay=True, uuid=uid, message={"role": "user", "content": []})
if "work for the peer" in text:
    say("assistant", message={"id": "msg_peer_work", "role": "assistant",
        "content": [{"type": "text", "text": "working for the peer"}]})
    (root / "peer-working").touch()
    steered, words = prompt()
    (root / "steered").write_text(words)
    say("user", isReplay=True, uuid=steered, message={"role": "user", "content": []})
    answer("answered: owner says hi" if "owner says hi" in words else "answered", uid, steered)
    raise SystemExit(0)
answer("answered: " + text.strip().splitlines()[-1], uid)
"#;

/// A Grok stand-in, a tool whose running turn takes no other message: the peer's work runs
/// until it is stopped; anything else is answered at once.
const GROK: &str = r#"#!/usr/bin/env python3
import json, pathlib, sys, time
args = sys.argv[1:]
if args == ["--version"]:
    print("stand-in 1")
    raise SystemExit(0)
if args == ["models"]:
    print("synthetic")
    raise SystemExit(0)
root = pathlib.Path(__file__).resolve().parent
flag = "--resume" if "--resume" in args else "--session-id"
sid = args[args.index(flag) + 1]
prompt = open(args[args.index("--prompt-file") + 1]).read() if "--prompt-file" in args else args[-1]
def emit(**event):
    print(json.dumps(event), flush=True)
emit(type="turn_started", sessionId=sid)
if "work for the peer" in prompt:
    (root / "peer-working").touch()
    time.sleep(120)
    raise SystemExit(0)
emit(type="text", data="answered: owner says hi" if "owner says hi" in prompt else "answered")
emit(type="end", sessionId=sid, usage={"input_tokens": 1, "output_tokens": 1})
"#;

/// The rows of the agent's log, coordination shown or hidden as `query` asks.
fn log_of(host: &Host, session: &str, query: &str) -> Vec<Value> {
    host.get(&format!("/log?session={session}&limit=200{query}"))["turns"]
        .as_array()
        .cloned()
        .unwrap_or_default()
}

fn text_of(row: &Value) -> String {
    row["text"].as_str().unwrap_or("").to_owned()
}

const CUT_SHORT: &str = "was cut short because the owner wrote to you";

/// The peer asks the agent for work, and the agent's turn on it is at work.
fn peer_at_work(place: &Place, host: &Host, target: &str, backend: &str) -> String {
    host.post(
        "/agents",
        json!({"name": "Desk", "session": target, "backend": backend}),
    );
    let peer = host.new_agent("Peer");
    let peer_id = row_id(host, &peer);
    let sent = host.post(
        "/send",
        json!({
            "session": target, "text": "work for the peer", "client_msg_id": "from-peer",
            "origin": "agent", "sender": peer_id,
        }),
    );
    assert_eq!(sent["queued"], false, "{sent}");
    wait_for("the peer's turn at work", || {
        place.root.join("peer-working").exists()
    });
    peer
}

/// Peter, 25 Nov 2026: the owner's message is steered into the running turn of another agent's
/// message, which nothing cuts; from its reading on the turn is his, and its answer is shown in
/// his chat. The peer's message stays the agents'.
#[test]
fn the_owners_message_steers_into_another_agents_turn_and_its_answer_is_his() {
    let place = Place::new("owner-steers");
    let claude = place.root.join("claude");
    program(&claude, CLAUDE);
    let home = place.root.join("home");
    let world = [
        ("HOME", home.to_str().unwrap()),
        ("CLARP_CLAUDE_PROGRAM", claude.to_str().unwrap()),
    ];
    let host = place.start_in("", &[], &world);
    let target = "desk";
    peer_at_work(&place, &host, target, "claude");

    let sent = host.send(target, "owner says hi", "owner");
    assert_eq!(
        sent["queued"], true,
        "it is steered, not run in the peer turn's place: {sent}"
    );
    wait_for("the owner's words read into the peer's turn", || {
        place.root.join("steered").exists()
    });
    let steered = std::fs::read_to_string(place.root.join("steered")).unwrap();
    assert!(steered.contains("owner says hi"), "{steered}");
    let answered = |row: &Value| text_of(row) == "answered: owner says hi";
    wait_for("the answer in the owner's chat", || {
        log_of(&host, target, "&coordination=hide")
            .iter()
            .any(answered)
    });
    wait_for("the turn to end", || host.state(target) == "done");

    let shown = log_of(&host, target, "&coordination=hide");
    let all = log_of(&host, target, "&coordination=show");
    // Nothing was cut, and the agent is left no note to take the work up again.
    assert!(
        !all.iter().any(|row| text_of(row).contains(CUT_SHORT)),
        "{all:#?}"
    );
    // His message is in his chat, read into the peer's turn.
    let own = shown
        .iter()
        .find(|row| text_of(row) == "owner says hi")
        .unwrap_or_else(|| panic!("{shown:#?}"));
    assert!(own["read_into"]["turn"].is_u64(), "{own}");
    let answer = all.iter().find(|row| answered(row)).unwrap();
    assert_eq!(answer["audience"], "user", "{answer}");
    // What the turn said for the peer before his words stays the agents'.
    let peer_row = all.iter().find(|row| row["id"] == "u-from-peer").unwrap();
    assert_eq!(peer_row["audience"], "agents", "{peer_row}");
    assert!(
        !shown
            .iter()
            .any(|row| text_of(row) == "working for the peer")
    );
    assert!(!shown.iter().any(|row| row["id"] == "u-from-peer"));
    // One turn ran, the peer's, and it ended by itself with the answer to both.
    assert_eq!(answer["turn"]["status"], "completed", "{answer}");
}

/// Where the running turn takes no other message, it makes way as before (issue 839).
#[test]
fn the_owners_message_ends_another_agents_turn_where_it_cannot_be_steered() {
    let place = Place::new("owner-preempts");
    let grok = place.root.join("grok");
    program(&grok, GROK);
    let home = place.root.join("home");
    let world = [
        ("HOME", home.to_str().unwrap()),
        ("CLARP_GROK_PROGRAM", grok.to_str().unwrap()),
    ];
    let host = place.start_in("", &[], &world);
    let target = "desk";
    let peer = peer_at_work(&place, &host, target, "grok");

    host.send(target, "owner says hi", "owner");
    // Long before the peer's work would have ended by itself.
    let answered =
        |row: &Value| row["role"] == "assistant" && text_of(row) == "answered: owner says hi";
    wait_for("the owner's answer", || {
        host.turns(target).iter().any(answered)
    });
    // The agent is told to take the cut work up again, after the owner's turn.
    let log = |query: &str| log_of(&host, target, query);
    let is_note = |row: &Value| text_of(row).contains(CUT_SHORT);
    wait_for("the note to take the peer's message up again", || {
        log("&coordination=show").iter().any(is_note)
    });
    let all = log("&coordination=show");
    let note = all.iter().find(|row| is_note(row)).unwrap();
    let text = text_of(note);
    assert!(text.contains("Peer"), "{text}");
    assert!(text.contains("work for the peer"), "{text}");
    assert!(
        text.contains(&format!(
            "answer Peer ({peer}): `clarp-admin reply` for a request"
        )),
        "the note names whom to answer, with the session: {text}"
    );
    let owner = all.iter().position(answered).unwrap();
    let noted = all.iter().position(is_note).unwrap();
    assert!(owner < noted, "{all:#?}");
    // The peer's turn was the agents' (hidden coordination): the note that takes it up again
    // is too, and never reads as the owner's own words in his chat.
    let peer_row = all.iter().find(|row| row["id"] == "u-from-peer").unwrap();
    assert_eq!(peer_row["audience"], "agents", "{peer_row}");
    assert_eq!(note["audience"], "agents", "{note}");
    assert!(!log("&coordination=hide").iter().any(is_note));
    // A chat read without automation leaves the note out whatever its audience.
    assert!(
        !log("&coordination=show&include_automated=0")
            .iter()
            .any(is_note)
    );
    host.post("/stop", json!({"session": target}));
}

#[test]
fn behind_the_owners_own_turn_the_owners_next_message_waits_as_before() {
    let place = Place::new("owner-waits");
    let host = place.start();
    let target = host.new_agent("Desk");
    assert_eq!(host.send(&target, "sleep 60", "first")["queued"], false);
    wait_for("the owner's turn to run", || {
        host.state(&target) == "thinking"
    });
    let second = host.send(&target, "second", "second");
    assert_eq!(second["queued"], true, "{second}");
    let queue = host.get(&format!("/turn-queue?session={target}"));
    assert!(
        queue["items"]
            .as_array()
            .unwrap()
            .iter()
            .any(|item| item["id"] == "second"),
        "{queue}"
    );
    assert_eq!(host.state(&target), "thinking", "the owner's turn runs on");
    host.post("/stop", json!({"session": target}));
}
