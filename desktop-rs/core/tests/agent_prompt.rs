//! A message another agent sent into a chat shows folded to one line,
//! "<Name> prompted · <first line>", until it is opened; its key keeps it
//! open by message id. The user's own messages and replies are not prompts.

use clarp_core::agent_prompt::{self, AgentPrompt};
use clarp_core::protocol::Message;
use serde_json::{Value, json};

fn message(value: Value) -> Message {
    Message::from_json(value.as_object().unwrap())
}

fn prompt(text: &str) -> Option<AgentPrompt> {
    agent_prompt::of(&message(json!({"id": "u7", "role": "user", "origin": "agent", "sender_name": "Rachel", "sender_agent_id": "a1", "text": text})))
}

#[test]
fn an_agents_message_is_a_prompt_with_its_sender_first_line_and_key() {
    let shown = prompt("Run the build\nthen report back").expect("a prompt");
    assert_eq!(shown.sender, "Rachel");
    assert_eq!(shown.line, "Run the build");
    assert_eq!(shown.key, "a2a:u7");
    assert_eq!(shown.key, agent_prompt::key("u7"));
    assert_eq!(shown.label(), "Rachel prompted · Run the build");
}

#[test]
fn the_users_own_words_and_replies_are_not_prompts() {
    assert!(agent_prompt::of(&message(json!({"id": "u1", "role": "user", "text": "Hello"}))).is_none());
    assert!(agent_prompt::of(&message(json!({"id": "u2", "role": "user", "origin": "user", "sender_name": "Peter", "text": "Hi"}))).is_none());
    assert!(agent_prompt::of(&message(json!({"id": "a1", "role": "assistant", "origin": "agent", "sender_name": "Rachel", "text": "Done"}))).is_none());
}

#[test]
fn the_first_line_skips_blank_lines_and_markdown_syntax() {
    assert_eq!(prompt("\n\n   \n## Build **plan**\n\nStep one").unwrap().line, "Build plan");
    assert_eq!(prompt("```bash\ncargo test\n```").unwrap().line, "cargo test");
    assert_eq!(prompt("> Keep [the guide](https://example.com) open").unwrap().line, "Keep the guide open");
    assert_eq!(prompt("  - first item\n- second").unwrap().line, "first item");
}

#[test]
fn a_long_first_line_is_cut_with_an_ellipsis() {
    let long = "word ".repeat(200);
    let shown = prompt(&long).unwrap();
    assert!(shown.line.chars().count() <= agent_prompt::LINE_CHARS + 1, "{}", shown.line.len());
    assert!(shown.line.ends_with('…'));
    assert!(shown.line.starts_with("word word"));
}

#[test]
fn an_empty_prompt_still_names_its_sender() {
    let shown = prompt("").unwrap();
    assert_eq!(shown.line, "");
    assert_eq!(shown.label(), "Rachel prompted");
}

#[test]
fn a_sender_without_a_name_is_named_by_its_session_or_as_an_agent() {
    let by_session = message(json!({"id": "u8", "role": "user", "origin": "agent", "sender_session": "mike", "text": "Go"}));
    assert_eq!(agent_prompt::of(&by_session).unwrap().sender, "mike");
    let nameless = message(json!({"id": "u9", "role": "user", "origin": "agent", "text": "Go"}));
    assert_eq!(agent_prompt::of(&nameless).unwrap().sender, "An agent");
}

#[test]
fn voice_tags_are_not_part_of_the_line() {
    assert_eq!(prompt("<speak>Check the deploy</speak>").unwrap().line, "Check the deploy");
}
