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
    // The agent is told to take the cut work up again, after the owner's turn.
    wait_for("the note to take the peer's message up again", || {
        host.turns(&target)
            .iter()
            .any(|row| text_of(row).contains("was cut short because the owner wrote to you"))
    });
    let turns = host.turns(&target);
    let at = |found: &dyn Fn(&Value) -> bool| turns.iter().position(found).unwrap();
    let answered = at(&|row| row["role"] == "assistant" && text_of(row) == "owner says hi");
    let note = at(&|row| text_of(row).contains("was cut short because the owner wrote to you"));
    assert!(answered < note, "{turns:#?}");
    let note = text_of(&turns[note]);
    assert!(note.contains("Peer"), "{note}");
    assert!(note.contains("sleep 60"), "{note}");
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
