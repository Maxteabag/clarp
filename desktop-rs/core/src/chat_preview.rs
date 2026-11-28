//! The chat list's line from the Host's preview parts (feature
//! `chat_preview_parts`; docs/compatibility.md, held by
//! contract/fixtures/chat-preview.json): the Host sends what a message holds
//! as a kind and its values (`last_message_preview` on a roster row,
//! `preview_parts` on a notification), and the desktop words it in the app's
//! language when the row is drawn. A kind this desktop does not know, or a
//! Host that sends no parts, keeps the Host's English line (`last_message`).

use serde_json::Value;

use crate::json::{self, Object};
use crate::tr;

// i18n-screen: chat list

/// The line for `parts`, or None for a kind this desktop does not know.
/// `own` (a row's own message) puts "You: " before it.
pub fn line(parts: &Object) -> Option<String> {
    let words = words(parts)?;
    if parts.get("own").and_then(Value::as_bool) == Some(true) {
        // i18n: chat list preview of the user's own last message; {} is that message's preview
        Some(tr!("preview.own" => "You: {}", words))
    } else {
        Some(words)
    }
}

fn count(parts: &Object) -> i64 {
    parts.get("count").and_then(Value::as_i64).unwrap_or(0)
}

fn words(parts: &Object) -> Option<String> {
    let text = |key: &str| json::string(parts, key).trim().to_owned();
    Some(match json::string(parts, "kind").as_str() {
        "text" => text("text"),
        "image" => {
            let caption = text("caption");
            if caption.is_empty() {
                // i18n: chat list preview of a message that is one picture without a caption (noun)
                tr!("preview.image" => "Image")
            } else {
                // i18n: chat list preview of a message that is one picture; {} is its caption
                tr!("preview.image_caption" => "Image: {}", caption)
            }
        }
        // i18n: chat list preview of a message with several pictures; {n} is how many
        "images" => tr!("preview.images" => "{n} image" | "{n} images" % count(parts)),
        // i18n: chat list preview of a message that is one file; {} is the file's name
        "file" => tr!("preview.file" => "File: {}", text("name")),
        // i18n: chat list preview of a message with several files; {n} is how many
        "files" => tr!("preview.files" => "{n} file" | "{n} files" % count(parts)),
        // i18n: chat list preview of a message with pictures and files together; {n} is how many
        "attachments" => tr!("preview.attachments" => "{n} attachment" | "{n} attachments" % count(parts)),
        // i18n: chat list preview of a message that is only a block of code (noun)
        "code" => tr!("preview.code" => "Code"),
        "decision" => {
            let outcome = match json::string(parts, "outcome").as_str() {
                "approved" => crate::decision_receipt::Outcome::Approved,
                "declined" => crate::decision_receipt::Outcome::Declined,
                "answered" => crate::decision_receipt::Outcome::Answered(text("answer")),
                "discarded" => crate::decision_receipt::Outcome::Discarded,
                "expired" => crate::decision_receipt::Outcome::Expired,
                _ => crate::decision_receipt::Outcome::Resolved,
            }
            .label();
            let question = text("question");
            if question.is_empty() {
                outcome
            } else {
                // i18n: chat list preview of a resolved decision; {0} is the decision's question, {1} its outcome ("Approved")
                tr!("preview.decision" => "{0} · {1}", question, outcome)
            }
        }
        "tool" => tool(&json::string(parts, "category"), &text("name"), &text("label")),
        _ => return None,
    })
}

/// A tool the agent used last: "Ran cargo build", "Updated the plan".
fn tool(category: &str, name: &str, label: &str) -> String {
    // In every line below, {} is what the tool worked on (a command, a file,
    // a search, a web address or a helper's task), possibly empty.
    let said = match category {
        // i18n: chat list preview: the agent's last step was a shell command (past tense)
        "exec" => tr!("preview.tool.exec" => "Ran {}", label),
        // i18n: chat list preview: the agent's last step read a file (past tense)
        "read" => tr!("preview.tool.read" => "Read {}", label),
        // i18n: chat list preview: the agent's last step listed a folder (past tense)
        "list" => tr!("preview.tool.list" => "Listed {}", label),
        // i18n: chat list preview: the agent's last step was a search (past tense)
        "search" => tr!("preview.tool.search" => "Searched {}", label),
        // i18n: chat list preview: the agent's last step edited a file (past tense)
        "edit" => tr!("preview.tool.edit" => "Edited {}", label),
        // i18n: chat list preview: the agent's last step wrote a file (past tense)
        "write" => tr!("preview.tool.write" => "Wrote {}", label),
        // i18n: chat list preview: the agent's last step fetched a web page (past tense)
        "fetch" => tr!("preview.tool.fetch" => "Fetched {}", label),
        // i18n: chat list preview: the agent's last step called a tool server (MCP) (past tense)
        "mcp" => tr!("preview.tool.mcp" => "Called {}", label),
        // i18n: chat list preview: the agent's last step handed a task to a helper agent (past tense)
        "agent" => tr!("preview.tool.agent" => "Delegated {}", label),
        // i18n: chat list preview: the agent's last step updated its to-do plan (past tense)
        "todo" => tr!("preview.tool.todo" => "Updated the plan"),
        // i18n: chat list preview: the agent's last step used another tool; {} is the tool's name (past tense)
        _ => tr!("preview.tool.other" => "Used {}", name),
    };
    said.trim().to_owned()
}
