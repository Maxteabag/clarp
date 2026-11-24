//! An agent whose folder is gone (issue 758): a helper's worktree removed after its branch
//! landed. A send to it fails once and at once, as `folder_missing`, with a sentence that names
//! the folder and says what to do; nothing is tried again. Once the folder is back, the next
//! send runs.

use super::*;

#[test]
fn a_send_to_an_agent_whose_folder_is_gone_fails_at_once_and_runs_once_it_is_back() {
    let place = Place::new("folder-missing");
    let host = place.start();
    let folder = place.root.join("helper-worktree");
    std::fs::create_dir_all(&folder).unwrap();
    let made = host.post(
        "/agents",
        json!({ "name": "Helper", "backend": "scripted", "cwd": folder }),
    );
    let session = made["session"].as_str().unwrap().to_string();
    let heard = host.listen();
    std::fs::remove_dir_all(&folder).unwrap();

    let began = Instant::now();
    host.send(&session, "hello", "f1");
    let marker = || {
        host.turns(&session)
            .into_iter()
            .find(|row| row["id"] == "marker-u-f1")
    };
    wait_for("the send to fail", || {
        marker().is_some() && host.state(&session) == "interrupted"
    });
    assert!(
        began.elapsed() < Duration::from_secs(5),
        "it failed at once, not after tries: {:?}",
        began.elapsed()
    );
    let dir = folder.display();
    let sentence = format!(
        "This agent's folder {dir} no longer exists. Recreate it, or make a new agent in \
         another folder"
    );
    assert_eq!(
        marker().unwrap()["text"],
        format!("A message you sent was never run.\n\n{sentence}, then send the message again."),
    );

    let ours = |event: &&Value| event["session"] == session.as_str();
    let events = heard.lock().unwrap().clone();
    let failed: Vec<&Value> = events
        .iter()
        .filter(ours)
        .filter(|event| event["type"] == "agent-state" && event["detail"]["reason"].is_string())
        .collect();
    let last = failed.last().expect("the failure is told to the apps");
    assert_eq!(last["detail"]["reason"], "folder_missing", "{last}");
    assert_eq!(last["detail"]["message"], sentence, "{last}");
    assert_eq!(
        last["detail"]["summary"],
        format!("{sentence}. Your message is saved."),
        "{last}"
    );
    assert!(
        !events
            .iter()
            .filter(ours)
            .any(|event| event["detail"]["action"] == "reconnecting"),
        "nothing is tried again"
    );

    // The folder is back: the next send runs in it.
    std::fs::create_dir_all(&folder).unwrap();
    host.send(&session, "again", "f2");
    wait_for("the answer", || {
        host.conversation(&session)
            .contains(&said("assistant", "again"))
    });
}
