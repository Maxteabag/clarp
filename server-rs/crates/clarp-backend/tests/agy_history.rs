//! What a conversation Antigravity kept says was said and done in it, held to what the
//! Python Host's own function reads from the same lines
//! (`tests/fixtures/agy-history-vectors.json`, written by
//! `server-rs/tools/agy-history-vectors.py`).

#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_backend::history;
use serde_json::{Value, json};

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/agy-history-vectors.json")).unwrap()
}

#[test]
fn a_transcript_of_antigravity_is_read_as_the_python_host_reads_it() {
    let vectors = vectors();
    let transcripts = vectors["transcripts"].as_array().unwrap();
    let (mut lines_read, mut turns, mut tools, mut shown, mut named) = (0, 0, 0, 0, 0);
    for transcript in transcripts {
        let what = format!("{}: {}", transcript["name"], transcript["what"]);
        let lines: Vec<&str> = transcript["lines"]
            .as_array()
            .unwrap()
            .iter()
            .map(|line| line.as_str().unwrap())
            .collect();
        let read = history::agy(&(lines.join("\n") + "\n"));
        let theirs = transcript["turns"].as_array().unwrap();
        for (number, (ours, theirs)) in read.iter().zip(theirs).enumerate() {
            let none = Vec::new();
            let cards = |turn: &Value| turn["tools"].as_array().unwrap().clone();
            for (at, (ours, theirs)) in cards(ours).iter().zip(cards(theirs)).enumerate() {
                assert_eq!(ours, &theirs, "{what}, turn {number}, tool {at}");
            }
            let ours_shown = ours["display_cells"].as_array().unwrap_or(&none);
            let theirs_shown = theirs["display_cells"].as_array().unwrap_or(&none);
            for (at, (ours, theirs)) in ours_shown.iter().zip(theirs_shown).enumerate() {
                assert_eq!(ours, theirs, "{what}, turn {number}, cell {at}");
            }
            assert_eq!(ours, theirs, "{what}, turn {number}");
            // In the same order too: a card and a cell are read by an app as they are written.
            assert_eq!(
                ours.to_string(),
                theirs.to_string(),
                "{what}, turn {number}, as text"
            );
            tools += cards(ours).len();
            shown += ours_shown.len();
        }
        assert_eq!(read.len(), theirs.len(), "{what}");
        // What each turn is in its conversation, by the rule every tool's turns are read by.
        let phases = history::phases(&read);
        assert_eq!(json!(phases), transcript["phases"], "{what}: the phases");
        named += phases.iter().filter(|phase| !phase.is_null()).count();
        lines_read += lines.len();
        turns += read.len();
    }
    // Counted by a script over the vectors.
    assert_eq!(
        (transcripts.len(), lines_read, turns, tools, shown, named),
        (13, 169, 55, 73, 55, 27)
    );
}

#[test]
fn a_transcript_that_is_no_transcript_holds_no_turns() {
    assert_eq!(history::agy(""), Vec::<Value>::new());
    assert_eq!(
        history::agy("\n\nnot JSON\n[1, 2]\n\"a text\"\n7\n"),
        Vec::<Value>::new()
    );
    // A step that is no object is passed over, and the steps beside it are read.
    let read = history::agy(
        "7\n{\"type\": \"USER_INPUT\", \"step_index\": 0, \"content\": \"<USER_REQUEST>Hi</USER_REQUEST>\"}\n",
    );
    assert_eq!(
        read,
        vec![json!({ "role": "user", "text": "Hi", "tools": [], "timestamp": "" })]
    );
}

#[test]
fn a_message_that_cut_a_reply_is_read_as_the_user_sent_it() {
    // As agy 1.5.16 kept it (24 Nov 2026): the reply the driver gave back stands before the
    // user's words, within agy's mark.
    let sent = clarp_acp::agy::steer::with_reply(
        "1. **Pizza**: A baked flatbread.\n2. **Sushi**: Vinegared rice.\n",
        "Change of plan: stop the list now and reply with only the word BANANA.",
    );
    let content = format!(
        "<USER_REQUEST>\n{sent}\n</USER_REQUEST>\n<ADDITIONAL_METADATA>\nThe current local time is: 2026-11-24T15:06:53+01:00.\n</ADDITIONAL_METADATA>"
    );
    let step = json!({"step_index": 2, "source": "USER_EXPLICIT", "type": "USER_INPUT",
                      "status": "DONE", "created_at": "2026-11-24T14:07:04Z", "content": content});
    let read = history::agy(&format!("{step}\n"));
    assert_eq!(
        read,
        vec![json!({
            "role": "user",
            "text": "Change of plan: stop the list now and reply with only the word BANANA.",
            "tools": [],
            "timestamp": "2026-11-24T14:07:04Z"
        })]
    );
}
