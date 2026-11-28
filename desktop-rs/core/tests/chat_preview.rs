//! The chat list's line from the Host's preview parts, in English (the
//! cases of contract/fixtures/chat-preview.json the Host words as `en`).

use clarp_core::chat_preview::line;
use serde_json::json;

fn said(parts: serde_json::Value) -> Option<String> {
    line(parts.as_object().unwrap())
}

#[test]
fn each_kind_reads_as_the_hosts_english_line() {
    assert_eq!(said(json!({"kind": "text", "text": "Done. The build is green."})).as_deref(), Some("Done. The build is green."));
    assert_eq!(said(json!({"kind": "image", "caption": "Synthetic picker fixture"})).as_deref(), Some("Image: Synthetic picker fixture"));
    assert_eq!(said(json!({"kind": "image", "caption": ""})).as_deref(), Some("Image"));
    assert_eq!(said(json!({"kind": "images", "count": 3})).as_deref(), Some("3 images"));
    assert_eq!(said(json!({"kind": "file", "name": "report.pdf"})).as_deref(), Some("File: report.pdf"));
    assert_eq!(said(json!({"kind": "files", "count": 2})).as_deref(), Some("2 files"));
    assert_eq!(said(json!({"kind": "attachments", "count": 2})).as_deref(), Some("2 attachments"));
    assert_eq!(said(json!({"kind": "code"})).as_deref(), Some("Code"));
    assert_eq!(said(json!({"kind": "decision", "question": "Ship build 42?", "outcome": "approved"})).as_deref(), Some("Ship build 42? · Approved"));
    assert_eq!(said(json!({"kind": "decision", "question": "Which?", "outcome": "answered", "answer": "Review first"})).as_deref(), Some("Which? · Review first"));
    assert_eq!(said(json!({"kind": "tool", "category": "exec", "name": "Bash", "label": "cargo build"})).as_deref(), Some("Ran cargo build"));
    assert_eq!(said(json!({"kind": "tool", "category": "todo", "name": "TodoWrite", "label": ""})).as_deref(), Some("Updated the plan"));
    assert_eq!(said(json!({"kind": "tool", "category": "other", "name": "image_generation", "label": ""})).as_deref(), Some("Used image_generation"));
    assert_eq!(said(json!({"kind": "images", "count": 2, "own": true})).as_deref(), Some("You: 2 images"));
}

#[test]
fn an_unknown_kind_keeps_the_hosts_line() {
    assert_eq!(said(json!({"kind": "hologram"})), None);
}
