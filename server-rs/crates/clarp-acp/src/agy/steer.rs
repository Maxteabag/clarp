//! A message sent while a turn of Antigravity runs, as the Host hands it to the driver and as
//! the driver tells the Host what became of it: the same lines the Host reads of Claude Code
//! (`clarp-kernel` `steering.rs`), so that the Host keeps one way of steering a turn.
//!
//! The Host writes one line on the driver's standard input for each message
//! ([`steered`]). The driver sends its text to Antigravity as a prompt of its own on the
//! conversation that is open, and prints:
//!
//! - the read ([`read_line`]) once Antigravity has taken the message into its work, and never
//!   on the sending alone;
//! - one settled line ([`settled_line`]) for each answer, naming every message it covers.
//!
//! What agy 1.5 does with a prompt sent while another runs (seen on 23 Nov 2026): it answers the
//! running prompt at once as ended, and goes on under the new one with both messages; the
//! newer prompt's answer answers both. It sends no echo of the new message. So the driver
//! counts a message as taken when Antigravity is at work after the earlier prompt was answered
//! (its first update since then), or when the new prompt is itself answered; an error to the
//! new prompt before either is a refusal, and the message is never read.

use serde_json::{Value, json};

/// The longest answer text a settled line carries. The Host reads it only for a failure's
/// words; the answer itself reaches the conversation through Antigravity's own updates.
pub const SETTLED_TEXT_MOST: usize = 4_000;

/// The Host's line for a message sent while the turn runs: its id and its text. `None` for a
/// line that is no such message, or that carries anything but text: a message with files is
/// never steered.
pub fn steered(line: &Value) -> Option<(String, String)> {
    if line["type"] != "user" {
        return None;
    }
    let uuid = line["uuid"].as_str().filter(|uuid| !uuid.is_empty())?;
    let content = &line["message"]["content"];
    let text = match content {
        Value::String(text) => text.clone(),
        Value::Array(blocks) => {
            let mut texts = Vec::with_capacity(blocks.len());
            for block in blocks {
                if block["type"] != "text" {
                    return None;
                }
                texts.push(block["text"].as_str()?);
            }
            texts.join("\n\n")
        }
        _ => return None,
    };
    (!text.trim().is_empty()).then(|| (uuid.to_string(), text))
}

/// The read of a message: Antigravity has taken it into its work.
pub fn read_line(uuid: &str) -> Value {
    json!({"type": "user", "isReplay": true, "uuid": uuid})
}

/// The id of the message a printed line says was read.
pub fn read_of(line: &Value) -> Option<&str> {
    (line["type"] == "user" && line["isReplay"] == true)
        .then(|| line["uuid"].as_str())
        .flatten()
        .filter(|uuid| !uuid.is_empty())
}

/// An answer, settled: whether it failed, its text (cut), and the messages it covers.
pub fn settled_line(is_error: bool, text: &str, uuids: &[String]) -> Value {
    let mut end = text.len().min(SETTLED_TEXT_MOST);
    while !text.is_char_boundary(end) {
        end -= 1;
    }
    json!({
        "type": "result",
        "subtype": "success",
        "is_error": is_error,
        "result": &text[..end],
        "user_message_uuids": uuids,
    })
}

/// The driver's environment says `off` here to send a message that comes while Antigravity
/// writes at once, as it sends one while a tool runs, rather than cut the reply for it: for an
/// agy that answers a cancel otherwise than 1.5.16 does.
pub const CUT_ENV: &str = "CLARP_AGY_ACP_CUT";

/// The line before the reply given back with a message that cut it ([`with_reply`]).
pub const REPLY_BEGINS: &str = "<<< Your reply so far, cut off when the message below arrived >>>";
/// The line after it.
pub const REPLY_ENDS: &str = "<<< End of your reply so far >>>";
/// The most of a reply given back, in bytes: of a longer one, the end is kept.
pub const REPLY_MOST: usize = 24 * 1024;
/// The line in place of the start of a reply too long to give back whole.
pub const REPLY_LEFT_OUT: &str = "[The start of your reply is left out here.]";

/// A message that cut Antigravity's reply short, with the reply it had written so far before
/// it. Antigravity forgets the text of a prompt that is cancelled (seen with agy 1.5.16 on
/// 24 Nov 2026: asked, it said it had written nothing), so the driver gives it back.
pub fn with_reply(reply: &str, message: &str) -> String {
    let mut start = reply.len().saturating_sub(REPLY_MOST);
    while !reply.is_char_boundary(start) {
        start += 1;
    }
    let left_out = if start > 0 {
        format!("{REPLY_LEFT_OUT}\n")
    } else {
        String::new()
    };
    let kept = reply[start..].trim_end_matches('\n');
    format!("{REPLY_BEGINS}\n{left_out}{kept}\n{REPLY_ENDS}\n\n{message}")
}

/// A user's words as they were sent: without a reply the driver gave back before them
/// ([`with_reply`]).
pub fn without_reply(text: &str) -> &str {
    let Some(rest) = text.strip_prefix(REPLY_BEGINS) else {
        return text;
    };
    match rest.find(&format!("\n{REPLY_ENDS}")) {
        Some(at) => rest[at + 1 + REPLY_ENDS.len()..].trim_start_matches(['\n', '\r']),
        None => text,
    }
}

/// Adds what one answer spent to what the answers before it in the same work spent: every
/// count the two have, field by field. agy says each prompt's usage apart, and an answer that
/// covers prompts Antigravity handed over to it is told with what all of them spent.
pub fn add_usage(into: &mut Value, more: &Value) {
    let Some(more) = more.as_object() else {
        return;
    };
    if !into.is_object() {
        *into = json!({});
    }
    let Some(into) = into.as_object_mut() else {
        return;
    };
    for (name, count) in more {
        let Some(count) = count.as_u64() else {
            continue;
        };
        let had = into.get(name).and_then(Value::as_u64).unwrap_or(0);
        into.insert(name.clone(), json!(had.saturating_add(count)));
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;

    #[test]
    fn a_steered_line_is_text_only_with_an_id() {
        let line = json!({"type": "user", "uuid": "u-1", "message": {"role": "user",
            "content": [{"type": "text", "text": "one"}, {"type": "text", "text": "two"}]}});
        assert_eq!(steered(&line), Some(("u-1".into(), "one\n\ntwo".into())));
        let plain = json!({"type": "user", "uuid": "u-2", "message": {"content": "hi"}});
        assert_eq!(steered(&plain), Some(("u-2".into(), "hi".into())));
        for odd in [
            json!({"type": "user", "message": {"content": "no id"}}),
            json!({"type": "user", "uuid": "", "message": {"content": "empty id"}}),
            json!({"type": "assistant", "uuid": "u", "message": {"content": "x"}}),
            json!({"type": "user", "uuid": "u", "message": {"content": "  "}}),
            json!({"type": "user", "uuid": "u", "message": {"content": [
                {"type": "text", "text": "see"},
                {"type": "image", "source": {"type": "base64", "data": "AA"}}]}}),
        ] {
            assert_eq!(steered(&odd), None, "{odd}");
        }
    }

    #[test]
    fn a_read_and_a_settled_answer_are_the_lines_the_host_reads() {
        assert_eq!(
            read_line("u-1"),
            json!({"type": "user", "isReplay": true, "uuid": "u-1"})
        );
        assert_eq!(read_of(&read_line("u-1")), Some("u-1"));
        assert_eq!(read_of(&json!({"type": "user", "uuid": "u-1"})), None);
        let settled = settled_line(false, "done", &["p".into(), "u-1".into()]);
        assert_eq!(settled["type"], "result");
        assert_eq!(settled["is_error"], false);
        assert_eq!(settled["user_message_uuids"], json!(["p", "u-1"]));
        let long = "\u{e9}".repeat(SETTLED_TEXT_MOST);
        let cut = settled_line(true, &long, &[]);
        assert!(cut["result"].as_str().unwrap().len() <= SETTLED_TEXT_MOST);
    }

    #[test]
    fn a_reply_given_back_is_marked_at_both_ends_and_taken_off_again() {
        let sent = with_reply("1. Pho\n2. Ramen\n", "Stop the list.");
        assert_eq!(
            sent,
            format!("{REPLY_BEGINS}\n1. Pho\n2. Ramen\n{REPLY_ENDS}\n\nStop the list.")
        );
        assert_eq!(without_reply(&sent), "Stop the list.");
        // Words that only look like it, or never close it, are the user's as they are.
        assert_eq!(without_reply("Stop the list."), "Stop the list.");
        let open = format!("{REPLY_BEGINS}\nno end");
        assert_eq!(without_reply(&open), open);
        // A long reply keeps its end, within the bound, and says the start is left out.
        let long = "é".repeat(REPLY_MOST);
        let sent = with_reply(&long, "B");
        assert!(sent.contains(REPLY_LEFT_OUT), "{}", &sent[..200]);
        assert!(sent.len() < REPLY_MOST + 300);
        assert!(sent.ends_with(&format!("é\n{REPLY_ENDS}\n\nB")));
        assert_eq!(without_reply(&sent), "B");
    }

    #[test]
    fn usage_is_added_field_by_field() {
        let mut sum = Value::Null;
        add_usage(&mut sum, &json!({"inputTokens": 10, "outputTokens": 5}));
        add_usage(
            &mut sum,
            &json!({"inputTokens": 1, "thoughtTokens": 2, "note": "x"}),
        );
        add_usage(&mut sum, &Value::Null);
        assert_eq!(
            sum,
            json!({"inputTokens": 11, "outputTokens": 5, "thoughtTokens": 2})
        );
    }
}
