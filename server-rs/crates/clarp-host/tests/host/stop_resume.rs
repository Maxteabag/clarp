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
