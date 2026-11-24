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
fn a_message_that_cut_a_reply_is_read_as_the_cut_reply_then_the_user_message() {
    // As agy 1.5.16 kept a cut (24 Nov 2026): no step of the cancelled reply; the reply the
    // driver gave back stands before the user's words, within agy's mark.
    let request = |said: &str| {
        format!(
            "<USER_REQUEST>\n{said}\n</USER_REQUEST>\n<ADDITIONAL_METADATA>\nThe current local time is: 2026-11-24T15:06:53+01:00.\n</ADDITIONAL_METADATA>"
        )
    };
    let user = |step: u64, said: &str, at: &str| {
        json!({"step_index": step, "source": "USER_EXPLICIT", "type": "USER_INPUT",
               "status": "DONE", "created_at": at, "content": request(said)})
    };
    let model = |step: u64, said: &str, at: &str| {
        json!({"step_index": step, "source": "MODEL", "type": "PLANNER_RESPONSE",
               "status": "DONE", "created_at": at, "content": said})
    };
    let reply = "1. **Pizza**: A baked flatbread.\n2. **Sushi**: Vinegared rice.\n";
    let words = "Change of plan: stop the list now and reply with only the word BANANA.";
    let sent = clarp_acp::agy::steer::with_reply(reply, words);
    let steps = [
        user(0, "Write a list of 50 foods.", "2026-11-24T14:06:53Z"),
        user(2, &sent, "2026-11-24T14:07:04Z"),
        model(3, "BANANA", "2026-11-24T14:07:05Z"),
    ];
    let transcript: String = steps.iter().map(|step| format!("{step}\n")).collect();
    let turn = |role: &str, text: &str, at: &str| json!({"role": role, "text": text, "tools": [], "timestamp": at});
    let cut = turn("assistant", reply.trim_end(), "2026-11-24T14:07:04Z");
    assert_eq!(
        history::agy(&transcript),
        vec![
            turn("user", "Write a list of 50 foods.", "2026-11-24T14:06:53Z"),
            cut.clone(),
            turn("user", words, "2026-11-24T14:07:04Z"),
            turn("assistant", "BANANA", "2026-11-24T14:07:05Z"),
        ]
    );

    // Where agy kept the start of the reply (what it wrote before a tool), that turn is the
    // reply, not a second turn beside it.
    let steps = [
        user(0, "Write a list of 50 foods.", "2026-11-24T14:06:53Z"),
        model(
            1,
            "1. **Pizza**: A baked flatbread.",
            "2026-11-24T14:06:58Z",
        ),
        user(2, &sent, "2026-11-24T14:07:04Z"),
    ];
    let transcript: String = steps.iter().map(|step| format!("{step}\n")).collect();
    let read = history::agy(&transcript);
    assert_eq!(read.len(), 3, "{read:?}");
    assert_eq!(read[1]["text"], reply.trim_end());
    assert_eq!(read[2], turn("user", words, "2026-11-24T14:07:04Z"));
}

#[test]
fn the_tools_of_a_cut_prompt_go_with_its_reply() {
    // agy 1.5 keeps the tools a cancelled prompt ran (24 Nov 2026), and no text of it: the
    // reply given back is what the agent said after them.
    let request = |said: &str| format!("<USER_REQUEST>\n{said}\n</USER_REQUEST>");
    let reply = "1. Pho\n2. Ramen\n";
    let sent = clarp_acp::agy::steer::with_reply(reply, "Stop the list.");
    let steps = [
        json!({"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE",
               "created_at": "2026-11-24T14:06:53Z",
               "content": request("Write note.txt, then a list of 50 foods.")}),
        json!({"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE",
               "created_at": "2026-11-24T14:06:55Z",
               "tool_calls": [{"name": "write_file",
                               "args": {"TargetFile": "\"note.txt\"", "CodeContent": "\"KIWI\""}}]}),
        json!({"step_index": 2, "source": "MODEL", "type": "GENERIC", "status": "DONE",
               "created_at": "2026-11-24T14:06:56Z",
               "content": "Created At: 2026-11-24T15:06:56+01:00\nCompleted At: 2026-11-24T15:06:56+01:00\nWrote note.txt"}),
        json!({"step_index": 4, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE",
               "created_at": "2026-11-24T14:07:04Z", "content": request(&sent)}),
        json!({"step_index": 5, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE",
               "created_at": "2026-11-24T14:07:05Z", "content": "BANANA"}),
    ];
    let transcript: String = steps.iter().map(|step| format!("{step}\n")).collect();
    let read = history::agy(&transcript);
    let roles: Vec<&str> = read
        .iter()
        .filter_map(|turn| turn["role"].as_str())
        .collect();
    assert_eq!(
        roles,
        ["user", "assistant", "user", "assistant"],
        "{read:?}"
    );
    assert_eq!(read[1]["text"], reply.trim_end());
    let tools = read[1]["tools"].as_array().unwrap();
    assert_eq!(tools.len(), 1, "{read:?}");
    assert_eq!(read[2]["text"], "Stop the list.");
}
