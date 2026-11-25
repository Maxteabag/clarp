//! The words of an agent bound to a model provider of its own are read by its owner, by itself
//! and the agents bound as it is, and by the agents its owner lets in (feature
//! `bound_privacy`). Every other agent, another Host and a tool that cannot say whose turn it is
//! is refused its log, given its row without words, given its events blank, refused its goals
//! and artifacts in the agents' tools, and given that a message of it came and not what it said.
//! An agent asks for a reader on a card, and the owner's yes lets it in. Every provider here is
//! made up, and every Codex home is a throwaway directory of the test's own.

use super::*;

/// A stand-in Codex for a bound agent: it reports the agent's made-up provider `stand-in` in
/// its rollout, and answers with the words it was sent and a phrase only it says.
pub(crate) const CODEX: &str = r#"#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

home = Path(os.environ.get("CODEX_HOME", "/nowhere"))
thread = "019a0000-0000-7000-8000-0000000000b1"
assert sys.argv[1] == "exec", sys.argv
told = sys.stdin.read().split()
day = home / "sessions" / "2026" / "11" / "25"
day.mkdir(parents=True, exist_ok=True)
rollout = day / ("rollout-2026-11-25T08-00-00-" + thread + ".jsonl")
if not rollout.exists():
    meta = {"type": "session_meta", "payload": {"id": thread, "model_provider": "stand-in"}}
    rollout.write_text(json.dumps(meta) + "\n")
print(json.dumps({"type": "thread.started", "thread_id": thread}), flush=True)
print(json.dumps({"type": "turn.started"}), flush=True)
print(json.dumps({"type": "item.completed", "item": {
    "id": "item_0", "type": "agent_message",
    "text": "customer-ledger-figures " + (told[-1] if told else "")}}), flush=True)
print('{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}', flush=True)
"#;

/// What only the bound agent says.
pub(crate) const SECRET: &str = "customer-ledger-figures";

pub(crate) fn own_home(place: &Place, name: &str) -> PathBuf {
    let home = place.root.join(name);
    std::fs::create_dir_all(&home).unwrap();
    std::fs::write(
        home.join("config.toml"),
        "model_provider = \"stand-in\"\nmodel = \"made-up-model\"\n\n\
         [model_providers.stand-in]\nname = \"A stand-in\"\n\
         base_url = \"http://127.0.0.1:9/v1\"\nwire_api = \"responses\"\n",
    )
    .unwrap();
    home
}

pub(crate) fn bound_agent(host: &Host, name: &str, home: &std::path::Path) -> String {
    let made = host.post(
        "/agents",
        json!({
            "name": name, "backend": "codex", "model": "a-deployment-name",
            "codex_home": home, "codex_provider": "stand-in",
        }),
    );
    made["session"]
        .as_str()
        .unwrap_or_else(|| panic!("{made}"))
        .to_string()
}

pub(crate) fn agent_id(host: &Host, session: &str) -> String {
    host.get("/agents/snapshot")["agents"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| row["session"] == session)
        .and_then(|row| row["agent_id"].as_str())
        .unwrap()
        .to_string()
}

/// The token of an agent's first turn, as the Host makes it with its own key.
pub(crate) fn first_turn_token(place: &Place, id: &str) -> String {
    let kept = kept_records(place, "turn-identity");
    let key = kept["key"]["key"].as_str().unwrap();
    let key: Vec<u8> = (0..key.len() / 2)
        .map(|at| u8::from_str_radix(&key[at * 2..at * 2 + 2], 16).unwrap())
        .collect();
    clarp_kernel::turn_identity::make(&key, id, 1)
}

/// A read as an agent's turn (the administrator's token with the turn's), or as another Host.
fn read_as(host: &Host, headers: &[(&str, &str)], path: &str) -> (u16, Value) {
    let answered = fetch(host, &host.token, "GET", path, headers, b"");
    let said = serde_json::from_slice(&answered.body).unwrap_or(Value::Null);
    (answered.status, said)
}

/// An agent's own tool, called from its turn.
fn tool_as(host: &Host, dir: &std::path::Path, turn: &str, args: &[&str]) -> (i32, String, String) {
    let mut command = sealed_command(dir, env!("CARGO_BIN_EXE_clarp-host"));
    command
        .arg("tool")
        .args(args)
        .current_dir(dir)
        .env("CLARP_HOST_ADDR", &host.address)
        .env("CLARP_HOST_TOKEN", &host.token)
        .env("CLARP_PROVIDER_TURN", turn)
        .env_remove("CLARP_SESSION")
        .env_remove("CLAUDE_PWA_SESSION");
    let out = command.ended();
    (
        out.status.code().unwrap_or(-1),
        String::from_utf8_lossy(&out.stdout).into_owned(),
        String::from_utf8_lossy(&out.stderr).into_owned(),
    )
}

/// What the Host streams to a connection with these headers for a while.
fn heard_as(host: &Host, headers: &[(&str, &str)], during: impl FnOnce()) -> String {
    let mut stream = TcpStream::connect(&host.address).unwrap();
    stream
        .set_read_timeout(Some(Duration::from_millis(500)))
        .unwrap();
    let mut request = format!(
        "GET /events HTTP/1.1\r\nHost: test\r\nAuthorization: Bearer {}\r\n",
        host.token
    );
    for (name, value) in headers {
        request.push_str(&format!("{name}: {value}\r\n"));
    }
    request.push_str("\r\n");
    stream.write_all(request.as_bytes()).unwrap();
    during();
    let mut heard = Vec::new();
    let until = std::time::Instant::now() + Duration::from_secs(3);
    let mut buffer = [0_u8; 8192];
    while std::time::Instant::now() < until {
        match stream.read(&mut buffer) {
            Ok(0) => break,
            Ok(read) => heard.extend_from_slice(&buffer[..read]),
            Err(_) => {}
        }
    }
    String::from_utf8_lossy(&heard).into_owned()
}

fn said_by(host: &Host, session: &str) -> String {
    host.get(&format!(
        "/log?session={session}&coordination=show&limit=100"
    ))
    .to_string()
}

#[test]
fn a_bound_agents_words_are_read_by_its_owner_its_binding_and_the_readers_its_owner_lets_in() {
    let place = Place::new("bound-privacy");
    let codex = place.root.join("codex");
    program(&codex, CODEX);
    let host = place.start_in(
        "bound-owner-token",
        &[],
        &[("CLARP_CODEX_PROGRAM", codex.to_str().unwrap())],
    );
    let dir = place.root.clone();

    // Ada is bound; Tia is bound as she is; Ben and Cid run on the machine's own tools.
    let home = own_home(&place, "home-a");
    let ada = bound_agent(&host, "Ada", &home);
    let tia = bound_agent(&host, "Tia", &home);
    let (ben, cid) = (host.new_agent("Ben"), host.new_agent("Cid"));
    for (session, id) in [(&ada, "u-1"), (&tia, "u-2"), (&ben, "u-3"), (&cid, "u-4")] {
        host.send(session, "hello", id);
    }
    for session in [&ada, &tia, &ben, &cid] {
        wait_for("each first turn", || host.state(session) == "done");
    }
    assert!(said_by(&host, &ada).contains(SECRET));
    let token = |session: &str| first_turn_token(&place, &agent_id(&host, session));
    let (ada_turn, tia_turn, ben_turn, cid_turn) =
        (token(&ada), token(&tia), token(&ben), token(&cid));
    let as_ben = [("X-Clarp-Turn", ben_turn.as_str())];

    // The owner reads it; so do Ada herself and Tia, who is bound as she is.
    let log = format!("/log?session={ada}&coordination=show");
    for turn in [&ada_turn, &tia_turn] {
        let (status, said) = read_as(&host, &[("X-Clarp-Turn", turn)], &log);
        assert_eq!(status, 200, "{said}");
        assert!(said.to_string().contains(SECRET));
    }

    // Ben is refused its log, and another Host too.
    for headers in [&as_ben[..], &[("X-Clarp-Peer", "elsewhere")][..]] {
        let (status, said) = read_as(&host, headers, &log);
        assert_eq!(status, 403, "{said}");
        assert_eq!(said["error"], "bound_conversation", "{said}");
        assert!(said["message"].as_str().unwrap().contains("Ada"), "{said}");
    }
    // Its row in the snapshot is there with its name and state, and none of its words; the
    // others' rows are as they were.
    for headers in [&as_ben[..], &[("X-Clarp-Peer", "elsewhere")][..]] {
        let (status, snapshot) = read_as(&host, headers, "/agents/snapshot");
        assert_eq!(status, 200, "{snapshot}");
        let rows = snapshot["agents"].as_array().unwrap();
        let row = rows.iter().find(|row| row["session"] == ada).unwrap();
        assert_eq!(row["words_withheld"], true, "{row}");
        assert_eq!(row["persona"], "Ada", "{row}");
        assert_eq!(row["latest_state"], "done", "{row}");
        assert!(!row.to_string().contains(SECRET), "{row}");
        let other = rows.iter().find(|row| row["session"] == ben).unwrap();
        assert!(other.get("words_withheld").is_none(), "{other}");
    }
    let (_, owners) = read_as(&host, &[], "/agents/snapshot");
    let row = owners["agents"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| row["session"] == ada);
    assert!(row.unwrap().get("words_withheld").is_none());
    assert_eq!(row.unwrap()["readers"], json!([]));

    // A stream of events to Ben carries Ada's events blank.
    let heard = heard_as(&host, &as_ben, || {
        host.send(&ada, "again", "u-5");
        wait_for("Ada's second answer", || {
            said_by(&host, &ada).matches(SECRET).count() >= 2
        });
    });
    assert!(heard.contains(&ada), "Ada's events reach Ben: {heard}");
    assert!(!heard.contains(SECRET), "{heard}");

    // In the agents' tools, Ben is refused Ada's goals and artifacts.
    for args in [
        &["clarp-goal", "list", ada.as_str()][..],
        &["clarp-agent-artifacts", "list", ada.as_str()][..],
    ] {
        let (code, out, err) = tool_as(&host, &dir, &ben_turn, args);
        assert_eq!(code, 77, "{args:?}: {out} {err}");
        assert!(err.contains("bound_conversation"), "{err}");
    }
    let (code, _, err) = tool_as(&host, &dir, &tia_turn, &["clarp-goal", "list", &ada]);
    assert_eq!(code, 0, "Tia reads Ada's goals: {err}");

    // What Ada sends Ben arrives without its words; what she sends Tia arrives whole.
    let send = |to: &str, text: &str| {
        let (code, out, err) = tool_as(
            &host,
            &dir,
            &ada_turn,
            &["clarp-admin", "prompt", "--to", to, "--text", text],
        );
        assert_eq!(code, 0, "{err}");
        serde_json::from_str::<Value>(&out).unwrap()
    };
    let sent = send(&ben, "the-figures-for-ben");
    assert_eq!(sent["words_withheld"], true, "{sent}");
    let sent = send(&tia, "the-figures-for-tia");
    assert!(sent.get("words_withheld").is_none(), "{sent}");
    wait_for("both messages", || {
        said_by(&host, &tia).contains("the-figures-for-tia")
            && said_by(&host, &ben).contains("Ada sent you a message")
    });
    assert!(!said_by(&host, &ben).contains("the-figures-for-ben"));
    // To another Host, nothing of hers goes at all.
    let (code, _, err) = tool_as(
        &host,
        &dir,
        &ada_turn,
        &[
            "clarp-admin",
            "prompt",
            "--server",
            "elsewhere",
            "--to",
            "x",
            "--text",
            "t",
        ],
    );
    assert_eq!(code, 4, "{err}");
    assert!(err.contains("bound_conversation"), "{err}");

    // Ben asks for Cid to read Ada: a card in Ben's chat, and nothing changes until the owner
    // answers. Ben cannot stop a reader.
    let (code, out, err) = tool_as(
        &host,
        &dir,
        &ben_turn,
        &[
            "clarp-admin",
            "agent",
            "provider",
            "--session",
            &ada,
            "--reader",
            &cid,
        ],
    );
    assert_eq!(code, 0, "{err}");
    let asked: Value = serde_json::from_str(&out).unwrap();
    assert_eq!(asked["readers"], json!([]), "{asked}");
    let decision = asked["asked"][0]["decision_id"]
        .as_str()
        .unwrap()
        .to_string();
    let as_cid = [("X-Clarp-Turn", cid_turn.as_str())];
    assert_eq!(read_as(&host, &as_cid, &log).0, 403);
    let (code, _, err) = tool_as(
        &host,
        &dir,
        &ben_turn,
        &[
            "clarp-admin",
            "agent",
            "provider",
            "--session",
            &ada,
            "--no-reader",
            &cid,
        ],
    );
    assert_eq!(code, 1, "{err}");
    assert!(err.contains("owner_only"), "{err}");
    // Asked again, it is the same card.
    let (_, again, _) = tool_as(
        &host,
        &dir,
        &ben_turn,
        &[
            "clarp-admin",
            "agent",
            "provider",
            "--session",
            &ada,
            "--reader",
            &cid,
        ],
    );
    let again: Value = serde_json::from_str(&again).unwrap();
    assert_eq!(again["asked"][0]["decision_id"], json!(decision), "{again}");

    // The owner says yes: Cid reads Ada, and her row names Cid among its readers.
    host.post(
        &format!("/decisions/{decision}/resolve"),
        json!({"choice": "accepted", "expected_revision": 1}),
    );
    let (status, said) = read_as(&host, &as_cid, &log);
    assert_eq!(status, 200, "{said}");
    assert!(said.to_string().contains(SECRET));
    let (_, owners) = read_as(&host, &[], "/agents/snapshot");
    let row = owners["agents"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| row["session"] == ada);
    assert_eq!(row.unwrap()["readers"], json!([agent_id(&host, &cid)]));

    // The owner's own word is done at once.
    let (code, out, err) = tool(
        &host,
        &dir,
        None,
        &[
            "clarp-admin",
            "agent",
            "provider",
            "--session",
            &ada,
            "--no-reader",
            &cid,
        ],
    );
    assert_eq!(code, 0, "{err}");
    let done: Value = serde_json::from_str(&out).unwrap();
    assert_eq!(done["readers"], json!([]), "{done}");
    assert_eq!(read_as(&host, &as_cid, &log).0, 403);
    // The card's yes sent again does nothing: Cid stays out.
    let replayed = host.ask(
        "POST",
        &format!("/decisions/{decision}/resolve"),
        Some(&json!({"choice": "accepted"})),
    );
    assert_eq!(read_as(&host, &as_cid, &log).0, 403, "{replayed:?}");

    // Only the owner changes Ada's binding. Ben's --clear is a card, and Ada stays bound;
    // so does a change of her cloud speech.
    let provider_of = |host: &Host| {
        let (_, owners) = read_as(host, &[], "/agents/snapshot");
        let rows = owners["agents"].as_array().unwrap().clone();
        let row = rows.into_iter().find(|row| row["session"] == ada).unwrap();
        (row["codex_provider"].clone(), row["cloud_speech"].clone())
    };
    let ask = |args: &[&str]| {
        let mut all = vec!["clarp-admin", "agent", "provider", "--session", &ada];
        all.extend_from_slice(args);
        let (code, out, err) = tool_as(&host, &dir, &ben_turn, &all);
        assert_eq!(code, 0, "{args:?}: {err}");
        serde_json::from_str::<Value>(&out).unwrap()
    };
    // A card an agent makes itself changes nothing when the owner says yes, whatever it
    // carries: the Host acts only on cards it keeps a question for.
    let (ada_id, ben_id) = (agent_id(&host, &ada), agent_id(&host, &ben));
    for payload in [
        json!({"bound_change": {"agent_id": ada_id, "codex_home": "", "codex_provider": ""}}),
        json!({"bound_reader": {"agent_id": ada_id, "reader_id": ben_id}}),
    ] {
        let payload = payload.to_string();
        let (code, out, err) = tool_as(
            &host,
            &dir,
            &ben_turn,
            &[
                "clarp-agent-artifacts",
                "decision",
                &ben,
                "Ship build 141?",
                "Ship it now?",
                "Yes",
                "No",
                &payload,
            ],
        );
        assert_eq!(code, 0, "{err}");
        let planted: Value = serde_json::from_str(&out).unwrap();
        let id = planted["decision"]["decision_id"].as_str().unwrap();
        host.post(
            &format!("/decisions/{id}/resolve"),
            json!({"choice": "accepted", "expected_revision": 1}),
        );
    }
    assert_eq!(provider_of(&host).0, json!("stand-in"));
    assert_eq!(read_as(&host, &as_ben, &log).0, 403);
    let speech = ask(&["--cloud-speech", "on"]);
    assert!(speech["asked"][0]["decision_id"].is_string(), "{speech}");
    let unbind = ask(&["--clear"]);
    let card = unbind["asked"][0]["decision_id"]
        .as_str()
        .unwrap()
        .to_string();
    assert_eq!(unbind["codex_provider"], "stand-in", "{unbind}");
    assert_eq!(provider_of(&host), (json!("stand-in"), json!(false)));
    // Asked again, it is the same card.
    assert_eq!(ask(&["--clear"])["asked"][0]["decision_id"], json!(card));
    // Started again in place by an agent, a bound agent keeps its binding; by the owner, it
    // takes the binding he gives it.
    let elsewhere = own_home(&place, "home-b");
    let again = json!({
        "replace_sid": ada, "name": "Ada", "backend": "codex",
        "codex_home": elsewhere, "codex_provider": "stand-in",
    })
    .to_string();
    let answered = fetch(
        &host,
        &host.token,
        "POST",
        "/agents",
        &as_ben,
        again.as_bytes(),
    );
    let said: Value = serde_json::from_slice(&answered.body).unwrap_or(Value::Null);
    assert_eq!(answered.status, 409, "{said}");
    assert_eq!(said["error"], "own_provider", "{said}");
    let home_of = |host: &Host| {
        let (_, owners) = read_as(host, &[], "/agents/snapshot");
        let rows = owners["agents"].as_array().unwrap().clone();
        let row = rows.into_iter().find(|row| row["session"] == ada).unwrap();
        row["codex_home"].clone()
    };
    assert_eq!(home_of(&host), json!(home));
    let answered = fetch(&host, &host.token, "POST", "/agents", &[], again.as_bytes());
    assert_eq!(
        answered.status,
        200,
        "{}",
        String::from_utf8_lossy(&answered.body)
    );
    assert_eq!(home_of(&host), json!(elsewhere));
    // The owner says yes to the unbinding: Ada is let go.
    host.post(
        &format!("/decisions/{card}/resolve"),
        json!({"choice": "accepted", "expected_revision": 1}),
    );
    assert_eq!(provider_of(&host).0, Value::Null);
    // The owner's own word binds her again at once.
    let (code, out, err) = tool(
        &host,
        &dir,
        None,
        &[
            "clarp-admin",
            "agent",
            "provider",
            "--session",
            &ada,
            "--codex-home",
            home.to_str().unwrap(),
            "--codex-provider",
            "stand-in",
        ],
    );
    assert_eq!(code, 0, "{err}");
    let done: Value = serde_json::from_str(&out).unwrap();
    assert!(done.get("asked").is_none(), "{done}");
    assert_eq!(provider_of(&host).0, json!("stand-in"));

    // A helper made with Ada as its parent is bound as she is; one on another tool is refused.
    let hal = host.post(
        "/agents",
        json!({"name": "Hal", "backend": "codex", "parent": ada, "role": "helper"}),
    );
    let hal = hal["session"].as_str().unwrap().to_string();
    let (_, owners) = read_as(&host, &[], "/agents/snapshot");
    let rows = owners["agents"].as_array().unwrap().clone();
    let row = rows.into_iter().find(|row| row["session"] == hal).unwrap();
    assert_eq!(
        (&row["codex_home"], &row["codex_provider"]),
        (&json!(home), &json!("stand-in")),
        "{row}"
    );
    let (status, said) = host.ask(
        "POST",
        "/agents",
        Some(&json!({"name": "Sal", "backend": "scripted", "parent": ada, "role": "helper"})),
    );
    assert_eq!(status, 409, "{said}");
    assert_eq!(said["error"], "own_provider", "{said}");
}

/// The roster events that carry their change (feature `roster_fields`) and the read of one
/// agent's row (feature `agent_row`) give a reader who may not read a bound agent what the
/// snapshot gives it of that agent, and no more (issue 809).
#[test]
fn a_withheld_agents_roster_fields_and_row_carry_what_its_snapshot_row_does() {
    let place = Place::new("bound-roster-fields");
    let codex = place.root.join("codex");
    program(&codex, CODEX);
    let host = place.start_in(
        "bound-owner-token",
        &["--helpers-checked-every-ms", "3600000"],
        &[("CLARP_CODEX_PROGRAM", codex.to_str().unwrap())],
    );
    let home = own_home(&place, "home-a");
    let ada = bound_agent(&host, "Ada", &home);
    let ben = host.new_agent("Ben");
    for (session, id) in [(&ada, "u-1"), (&ben, "u-2")] {
        host.send(session, "hello", id);
    }
    for session in [&ada, &ben] {
        wait_for("each first turn", || host.state(session) == "done");
    }
    // Ada's helper is bound as she is; the line of a maker at work names its helper.
    let made = host.post(
        "/agents",
        json!({
            "name": "Slice", "backend": "codex", "model": "a-deployment-name",
            "role": "helper", "parent": ada, "session": "slice",
        }),
    );
    assert_eq!(made["session"], "slice", "{made}");
    let ben_turn = first_turn_token(&place, &agent_id(&host, &ben));
    let as_ben = [("X-Clarp-Turn", ben_turn.as_str())];
    let mark = |state: &str| {
        let body = json!({ "session": "slice", "state": state });
        let (status, said) = host.ask("POST", "/agent-helper-state", Some(&body));
        assert_eq!(status, 200, "{said}");
    };
    mark("done");
    let owners = host.listen_at(&format!("/events?token={}", host.token));
    let heard = heard_as(&host, &as_ben, || {
        mark("running");
        wait_for("the owner hears the helper's move", || {
            owners.lock().unwrap().iter().any(|event| {
                event["kind"] == "helper-state" && event["fields"]["helper_state"] == "running"
            })
        });
    });
    let events: Vec<Value> = heard
        .lines()
        .filter_map(|line| {
            line.strip_prefix("data: ")
                .or_else(|| line.strip_prefix("data:"))
        })
        .filter_map(|data| serde_json::from_str(data).ok())
        .collect();
    let bens = events
        .iter()
        .find(|event| {
            // A stream without a cursor is given the last minutes first: the move to `done`
            // came before.
            event["kind"] == "helper-state"
                && event["session"] == "slice"
                && event["fields"]["helper_state"] == "running"
        })
        .unwrap_or_else(|| panic!("Ben hears of the helper's move: {heard}"));
    let owners = owners.lock().unwrap().clone();
    let full = owners
        .iter()
        .rev()
        .find(|event| event["kind"] == "helper-state" && event["session"] == "slice")
        .unwrap()
        .clone();

    // The owner is told Ada's line, which names her helper; Ben is told that she is at work and
    // how many helpers she has, and not the words of her line.
    assert!(
        full["parent"]["fields"]["status_text"]
            .as_str()
            .is_some_and(|line| line.starts_with("slice")),
        "{full}"
    );
    assert_eq!(bens["words_withheld"], true, "{bens}");
    assert_eq!(bens["fields"]["helper_state"], "running", "{bens}");
    let parent = &bens["parent"];
    assert_eq!(parent["session"], json!(ada), "{bens}");
    assert_eq!(parent["fields"]["status_text"], "", "{bens}");
    assert_eq!(parent["fields"]["running_children"], 1, "{bens}");
    assert_eq!(parent["fields"]["latest_state"], "background", "{bens}");
    assert!(!heard.contains(SECRET), "{heard}");

    // What Ben is told of each agent is what his snapshot says of it, field for field; and so
    // is the one row he reads.
    let (status, snapshot) = read_as(&host, &as_ben, "/agents/snapshot");
    assert_eq!(status, 200, "{snapshot}");
    let row = |session: &str| {
        snapshot["agents"]
            .as_array()
            .unwrap()
            .iter()
            .find(|row| row["session"] == session)
            .cloned()
            .unwrap()
    };
    let (rows_ada, rows_slice) = (row(&ada), row("slice"));
    assert_eq!(rows_ada["words_withheld"], true, "{rows_ada}");
    for (fields, row) in [
        (&bens["fields"], &rows_slice),
        (&parent["fields"], &rows_ada),
    ] {
        for (name, value) in fields.as_object().unwrap() {
            assert_eq!(value, &row[name], "{name}: {fields} against {row}");
        }
    }
    for (session, expected) in [(ada.as_str(), &rows_ada), ("slice", &rows_slice)] {
        let (status, one) = read_as(&host, &as_ben, &format!("/agents/{session}"));
        assert_eq!(status, 200, "{one}");
        assert_eq!(&one, expected);
        assert!(!one.to_string().contains(SECRET), "{one}");
    }
    // Her conversation's changes reach Ben as the revision and conversation his row of her has,
    // and nothing more.
    let heard = heard_as(&host, &as_ben, || {
        host.send(&ada, "more", "u-3");
        wait_for("Ada's next answer", || {
            said_by(&host, &ada).matches(SECRET).count() >= 2
        });
    });
    assert!(!heard.contains(SECRET), "{heard}");
    let (_, snapshot) = read_as(&host, &as_ben, "/agents/snapshot");
    let rows_ada = snapshot["agents"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| row["session"] == ada)
        .cloned()
        .unwrap();
    let told = heard
        .lines()
        .filter_map(|line| {
            line.strip_prefix("data: ")
                .or_else(|| line.strip_prefix("data:"))
        })
        .filter_map(|data| serde_json::from_str::<Value>(data).ok())
        .rev()
        .find(|event| event["type"] == "transcript-updated" && event["session"] == ada)
        .unwrap_or_else(|| panic!("Ben hears that Ada's conversation changed: {heard}"));
    assert_eq!(
        told["conversation_id"], rows_ada["conversation_id"],
        "{told}"
    );
    assert!(told["head_revision"].as_u64().unwrap() <= rows_ada["head_revision"].as_u64().unwrap());

    // The owner reads Ada's row whole.
    let (status, one) = read_as(&host, &[], &format!("/agents/{ada}"));
    assert_eq!(status, 200, "{one}");
    assert!(one.get("words_withheld").is_none(), "{one}");
}

/// What a helper bound otherwise than its maker says never reaches a reader of the maker who
/// may not read the helper: not in the maker's line in the snapshot, nor in the one-row read,
/// nor in an event's `parent` (issue 819). The owner reads it on the helper's own row.
#[test]
fn a_helper_bound_otherwise_than_its_maker_is_not_read_through_the_makers_line() {
    let place = Place::new("bound-maker-line");
    let codex = place.root.join("codex");
    program(&codex, CODEX);
    let host = place.start_in(
        "bound-owner-token",
        &["--helpers-checked-every-ms", "3600000"],
        &[("CLARP_CODEX_PROGRAM", codex.to_str().unwrap())],
    );
    let dir = place.root.clone();
    // Pat runs on the machine's own tools, and Ben reads it; its helper Hal is bound to a
    // provider of its own, and Ben does not read Hal. Sid is another helper of Pat's, unbound.
    let pat = host.new_agent("Pat");
    let ben = host.new_agent("Ben");
    host.send(&ben, "hello", "u-1");
    wait_for("Ben's first turn", || host.state(&ben) == "done");
    let home = own_home(&place, "home-h");
    let made = host.post(
        "/agents",
        json!({
            "name": "Hal", "backend": "codex", "model": "a-deployment-name", "role": "helper",
            "parent": pat, "session": "hal", "codex_home": home, "codex_provider": "stand-in",
        }),
    );
    assert_eq!(made["session"], "hal", "{made}");
    let made = host.post(
        "/agents",
        json!({"name": "Sid", "backend": "scripted", "role": "helper", "parent": pat, "session": "sid"}),
    );
    assert_eq!(made["session"], "sid", "{made}");
    const LINE: &str = "Ledger figures";
    let on = tool(&host, &dir, None, &["clarp-agent-bg", "hal", "on", LINE]);
    assert_eq!(on.0, 0, "{}", on.2);
    let row_of = |snapshot: &Value, session: &str| {
        snapshot["agents"]
            .as_array()
            .unwrap()
            .iter()
            .find(|row| row["session"] == session)
            .cloned()
            .unwrap()
    };
    wait_for("Hal's line on its own row", || {
        row_of(&host.get("/agents/snapshot"), "hal")["status_text"] == LINE
    });
    let ben_turn = first_turn_token(&place, &agent_id(&host, &ben));
    let as_ben = [("X-Clarp-Turn", ben_turn.as_str())];

    // Sid, whom Ben reads, is done: its event carries Pat's line, which is Hal's alone now and
    // names Hal by its session only.
    let heard = heard_as(&host, &as_ben, || {
        let body = json!({ "session": "sid", "state": "done" });
        assert_eq!(host.ask("POST", "/agent-helper-state", Some(&body)).0, 200);
    });
    let event = heard
        .lines()
        .filter_map(|line| {
            line.strip_prefix("data: ")
                .or_else(|| line.strip_prefix("data:"))
        })
        .filter_map(|data| serde_json::from_str::<Value>(data).ok())
        .rev()
        .find(|event| event["kind"] == "helper-state" && event["session"] == "sid")
        .unwrap_or_else(|| panic!("Ben hears of Sid's move: {heard}"));
    assert!(event.get("words_withheld").is_none(), "{event}");
    assert_eq!(event["parent"]["session"], json!(pat), "{event}");
    assert_eq!(event["parent"]["fields"]["status_text"], "hal", "{event}");
    assert!(!heard.contains(LINE), "{heard}");

    // The snapshot and the one-row read say the same of Pat, to Ben and to the owner.
    assert_eq!(
        row_of(&host.get("/agents/snapshot"), &pat)["status_text"],
        "hal"
    );
    let (_, snapshot) = read_as(&host, &as_ben, "/agents/snapshot");
    let pats = row_of(&snapshot, &pat);
    assert!(
        pats.get("words_withheld").is_none(),
        "Ben reads Pat: {pats}"
    );
    assert_eq!(pats["status_text"], "hal", "{pats}");
    assert!(!snapshot.to_string().contains(LINE), "{snapshot}");
    let (status, one) = read_as(&host, &as_ben, &format!("/agents/{pat}"));
    assert_eq!(status, 200, "{one}");
    assert_eq!(one, pats);
}
