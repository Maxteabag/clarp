//! Issue 839: the owner's message never waits behind a turn that is not a person's.
use super::*;

#[test]
fn the_owners_message_ends_another_agents_turn_and_the_agent_is_told_to_take_it_up() {
    let place = Place::new("owner-preempts");
    let host = place.start();
    let target = host.new_agent("Desk");
    let peer = host.new_agent("Peer");
    let peer_id = row_id(&host, &peer);
    let sent = host.post(
        "/send",
        json!({
            "session": target, "text": "sleep 60", "client_msg_id": "from-peer",
            "origin": "agent", "sender": peer_id,
        }),
    );
    assert_eq!(sent["queued"], false, "{sent}");
    wait_for("the peer's turn to run", || {
        host.state(&target) == "thinking"
    });

    host.send(&target, "owner says hi", "owner");
    let text_of = |row: &Value| row["text"].as_str().unwrap_or("").to_owned();
    // Far sooner than the peer's turn would have ended by itself.
    wait_for("the owner's answer", || {
        host.turns(&target)
            .iter()
            .any(|row| row["role"] == "assistant" && text_of(row) == "owner says hi")
    });
    // The agent is told to take the cut work up again, after the owner's turn. Work nobody
    // waits on may wait while the machine is under pressure: the note is then still in the
    // queue, and enters the conversation when its turn starts.
    let cut_short = "was cut short because the owner wrote to you";
    let note_text = || {
        let queue = host.get(&format!("/turn-queue?session={target}"));
        let queued = queue["items"].as_array().cloned().unwrap_or_default();
        let turns = host.turns(&target);
        let found = |rows: &[Value]| {
            rows.iter()
                .map(text_of)
                .find(|text| text.contains(cut_short))
        };
        found(&turns).or_else(|| found(&queued))
    };
    wait_for("the note to take the peer's message up again", || {
        note_text().is_some()
    });
    let note = note_text().unwrap();
    assert!(note.contains("Peer"), "{note}");
    assert!(note.contains("sleep 60"), "{note}");
    let turns = host.turns(&target);
    let answered = turns
        .iter()
        .position(|row| row["role"] == "assistant" && text_of(row) == "owner says hi")
        .unwrap();
    if let Some(noted) = turns
        .iter()
        .position(|row| text_of(row).contains(cut_short))
    {
        assert!(answered < noted, "{turns:#?}");
    }
    // The peer's turn was not answered: it made way.
    assert!(
        !turns
            .iter()
            .any(|row| row["role"] == "assistant" && text_of(row) == "sleep 60"),
        "{turns:#?}"
    );
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
