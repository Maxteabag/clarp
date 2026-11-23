//! The Host's tests of what a Stop holds, and of what lets it go again. One file a piece,
//! declared in `host.rs`.

use super::*;

#[test]
fn stop_resume_issue_308_admin_resume_releases_a_held_queue() {
    let place = Place::new("admin-resume-queue");
    let host = place.start();
    let session = host.new_agent("Helper");
    let owner = host.new_agent("Owner");
    let (code, help, err) = tool(&host, &place.root, None, &["clarp-admin", "stop", "--help"]);
    assert_eq!(code, 0, "{err}");
    assert!(help.contains("clarp-admin resume --session"));
    let (code, help, err) = tool(&host, &place.root, None, &["clarp-admin", "resume", "-h"]);
    assert_eq!(code, 0, "{err}");
    assert!(help.contains("usage: clarp-admin resume"));
    let commitment = json!({
        "outcome": "Finish work", "criteria": ["Done"], "limits": "Disposable Host only",
    })
    .to_string();
    let plan = goal(
        &host,
        &place.root,
        &["create", &session, "keep", "Keep it", "[]", &commitment],
    );
    let plan_id = plan["plan_id"].as_str().unwrap();
    let (code, text, err) = tool(
        &host,
        &place.root,
        None,
        &[
            "clarp-admin",
            "stop",
            "--session",
            &session,
            "--from",
            &owner,
            "--reason",
            "held for review",
        ],
    );
    assert_eq!(code, 0, "{err}");
    let stopped: Value = serde_json::from_str(&text).unwrap();
    assert_eq!(stopped["goals_paused"], 1);
    assert_eq!(
        goal(&host, &place.root, &["get", plan_id])["status"],
        "paused"
    );
    let queued = host.post(
        "/send",
        json!({
            "session": session, "text": "wait here", "client_msg_id": "wait-here",
            "origin": "agent", "sender": owner, "queue_if_busy": true,
        }),
    );
    assert_eq!(queued["queued"], true, "{queued}");
    assert!(host.conversation(&session).is_empty());
    // A fresh peer send is documented to run past a backlog held by an ordinary Stop.
    let peer = host.post(
        "/send",
        json!({
            "session": session, "text": "go past the held backlog", "client_msg_id": "fresh",
            "origin": "agent", "sender": owner,
        }),
    );
    assert_eq!(peer["queued"], false, "{peer}");
    wait_for("the fresh peer turn", || host.state(&session) == "done");
    let queue = host.get(&format!("/turn-queue?session={session}"));
    assert_eq!(queue["paused"], true, "{queue}");
    assert_eq!(queue["items"][0]["text"], "wait here");
    let (code, text, err) = tool(
        &host,
        &place.root,
        None,
        &[
            "clarp-admin",
            "resume",
            "--session",
            &session,
            "--from",
            &owner,
        ],
    );
    assert_eq!(code, 0, "{err}");
    let resumed: Value = serde_json::from_str(&text).unwrap();
    assert_eq!(resumed["action"], "unpause");
    assert_eq!(resumed["queue_paused"], false);
    wait_for("the resumed message to finish", || {
        host.conversation(&session).len() == 4 && host.state(&session) == "done"
    });
    assert_eq!(host.conversation(&session)[2], said("user", "wait here"));
    assert_eq!(
        goal(&host, &place.root, &["get", plan_id])["status"],
        "paused"
    );
}

#[test]
fn stop_resume_issue_386_due_self_prompts_wait_for_resume() {
    let place = Place::new("paused-self-prompt");
    let host = place.start();
    let session = host.new_agent("Helper");
    let stopped = host.post("/stop", json!({"session": session}));
    assert_eq!(stopped["queue_paused"], true, "{stopped}");
    // The timer runs this plain clarp-admin command later, without the old turn token.
    for (message, from) in [
        ("named wake", Some(session.as_str())),
        ("unattributed wake", None),
    ] {
        let mut args = vec!["clarp-admin", "prompt", "--to", &session, "--text", message];
        if let Some(from) = from {
            args.extend(["--from", from]);
        }
        let (code, text, err) = tool(&host, &place.root, None, &args);
        assert_eq!(code, 0, "{err}");
        let sent: Value = serde_json::from_str(&text).unwrap();
        assert_eq!(sent["queued"], true, "{sent}");
    }
    let queue = host.get(&format!("/turn-queue?session={session}"));
    assert_eq!(queue["paused"], true, "{queue}");
    let rows = queue["items"].as_array().unwrap();
    assert_eq!(rows.len(), 2, "{queue}");
    assert_eq!(rows[0]["text"], "named wake");
    assert_eq!(rows[1]["text"], "unattributed wake");
    assert!(host.conversation(&session).is_empty());
    host.post("/turn-queue/resume", json!({"session": session}));
    wait_for("both prompt deliveries after Resume", || {
        host.conversation(&session).len() == 4 && host.state(&session) == "done"
    });
}

#[test]
fn a_turn_stopped_while_it_waits_for_an_account_ends_under_its_message() {
    // Issue 729: a turn that waited between two attempts was settled by a Stop with no end
    // written, and its message stood in the conversation with nothing under it.
    let place = Place::new("stop-parked");
    let (claude, out_of_usage, given) = claude_that_is_out(&place);
    let home = place.root.join("home");
    let login = home.join(".claude").join(".credentials.json");
    std::fs::create_dir_all(login.parent().unwrap()).unwrap();
    std::fs::write(&login, "the first account").unwrap();
    let host = place.start_in(
        "",
        &[
            "--accounts",
            "follow",
            "--logins-checked-every-ms",
            "600000",
        ],
        &[
            ("CLARP_CLAUDE_PROGRAM", claude.to_str().unwrap()),
            ("HOME", home.to_str().unwrap()),
        ],
    );
    let made = host.post("/agents", json!({ "name": "Worker", "backend": "claude" }));
    let agent = made["session"].as_str().unwrap().to_string();
    let row = || -> Value {
        host.get("/agents/snapshot")["agents"]
            .as_array()
            .unwrap()
            .iter()
            .find(|row| row["session"] == agent)
            .cloned()
            .unwrap()
    };
    std::fs::write(&out_of_usage, "").unwrap();
    let sent = host.send(&agent, "work on this", "parked-1");
    let trace = sent["trace_id"].as_str().unwrap().to_string();
    wait_for("the turn to wait for an account", || {
        row()["live_activity"]["state"] == "limited"
    });
    let tried = std::fs::read_to_string(&given).unwrap().lines().count();

    let (status, _) = host.ask("POST", "/stop", Some(&json!({ "session": agent })));
    assert_eq!(status, 200);
    wait_for("the agent to be free", || row()["busy"] == false);
    let rows = host.turns(&agent);
    let ends: Vec<&Value> = rows.iter().filter(|row| row["turn"].is_object()).collect();
    assert_eq!(ends.len(), 1, "{rows:#?}");
    assert_eq!(
        (
            &ends[0]["turn"]["turn_id"],
            &ends[0]["turn"]["status"],
            &ends[0]["turn"]["reason"]
        ),
        (
            &json!(trace),
            &json!("interrupted"),
            &json!("Stopped by its user.")
        ),
        "{rows:#?}"
    );
    let messages = rows.iter().filter(|row| row["id"] == "u-parked-1").count();
    assert_eq!(messages, 1, "{rows:#?}");
    assert_eq!(host.state(&agent), "interrupted");
    // Nothing of the stopped turn is tried again, also when the account is back.
    std::fs::remove_file(&out_of_usage).unwrap();
    std::thread::sleep(Duration::from_millis(500));
    assert_eq!(
        std::fs::read_to_string(&given).unwrap().lines().count(),
        tried
    );
    assert_eq!(host.turns(&agent).len(), rows.len());
}
