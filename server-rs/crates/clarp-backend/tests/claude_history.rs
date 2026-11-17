//! What a transcript of Claude Code says was said in its session, held to what the Python
//! Host's own function reads from the same lines
//! (`tests/fixtures/claude-history-vectors.json`, written by
//! `server-rs/tools/claude-history-vectors.py`).

#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_backend::history;
use serde_json::{Value, json};

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/claude-history-vectors.json")).unwrap()
}

#[test]
fn a_transcript_is_read_as_the_python_host_reads_it() {
    let vectors = vectors();
    let (mut turns, mut tools) = (0, 0);
    for transcript in vectors["transcripts"].as_array().unwrap() {
        let what = format!("{}: {}", transcript["name"], transcript["what"]);
        let lines: Vec<&str> = transcript["lines"]
            .as_array()
            .unwrap()
            .iter()
            .map(|line| line.as_str().unwrap())
            .collect();
        let read = history::claude(&(lines.join("\n") + "\n"));
        let theirs = transcript["turns"].as_array().unwrap();
        assert_eq!(read.len(), theirs.len(), "{what}");
        for (number, (ours, theirs)) in read.iter().zip(theirs).enumerate() {
            assert_eq!(ours, theirs, "{what}, turn {number}");
            // In the same order too: a card is read by an app as it is written.
            assert_eq!(
                ours.to_string(),
                theirs.to_string(),
                "{what}, turn {number}, as text"
            );
            tools += ours["tools"].as_array().unwrap().len();
        }
        turns += read.len();
    }
    assert_eq!((turns, tools), (14, 13));
}

#[test]
fn the_model_a_session_last_ran_on_is_read_as_the_python_host_reads_it() {
    let vectors = vectors();
    let of_their_own = vectors["models"].as_array().unwrap();
    let transcripts = vectors["transcripts"].as_array().unwrap();
    let (mut asked, mut named) = (0, 0);
    for row in of_their_own.iter().chain(transcripts) {
        let lines = row["lines"].as_array().unwrap();
        let transcript: String = lines
            .iter()
            .map(|line| format!("{}\n", line.as_str().unwrap()))
            .collect();
        let theirs = row["model"].as_str().unwrap();
        assert_eq!(
            history::model(&transcript),
            theirs,
            "{}: {}",
            row["name"],
            row["what"]
        );
        asked += 1;
        named += usize::from(!theirs.is_empty());
    }
    assert_eq!((asked, named), (8, 6));
}

#[test]
fn a_transcript_that_says_nothing_has_no_turns() {
    assert_eq!(history::claude(""), Vec::<Value>::new());
    assert_eq!(
        history::claude("no JSON\n[1, 2]\n\"a text\"\n\n"),
        Vec::<Value>::new()
    );
    // A line ends where a new one begins, however that is written.
    let two = "{\"type\":\"user\",\"message\":{\"content\":\"one\"}}\r\n{\"type\":\"assistant\",\"message\":{\"content\":\"two\"}}";
    let read = history::claude(two);
    let said: Vec<&Value> = read.iter().map(|turn| &turn["text"]).collect();
    assert_eq!(said, [&json!("one"), &json!("two")]);
}

#[test]
fn a_forked_transcript_is_the_same_under_the_name_of_another_session() {
    let lines = [
        r#"{"type":"user","sessionId":"old","uuid":"u1","message":{"role":"user","content":"one"}}"#,
        "no JSON",
        "",
        r#"{"type":"summary","summary":"says no session"}"#,
        r#"{"type":"assistant","sessionId":"old","message":{"role":"assistant","content":[{"type":"text","text":"two"}]}}"#,
        r#"[1, 2]"#,
    ];
    let copy = history::claude_forked(&lines.join("\n"), "new-session");
    let written: Vec<Value> = copy
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect();
    assert_eq!(written.len(), 4);
    assert_eq!(written[0]["sessionId"], "new-session");
    assert_eq!(written[0]["uuid"], "u1");
    assert_eq!(
        written[1],
        json!({ "type": "summary", "summary": "says no session" })
    );
    assert_eq!(written[2]["sessionId"], "new-session");
    assert!(copy.ends_with('\n'));
    // What was said in it is what was said in the session it was copied from.
    assert_eq!(history::claude(&copy), history::claude(&lines.join("\n")));
    assert_eq!(history::claude(&copy).len(), 2);
}
