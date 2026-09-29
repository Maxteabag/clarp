use clarp_core::transcript_rows::{Change, Source, TranscriptRows, split_markdown};

fn table(rows: usize) -> String {
    let mut text = String::from("| n | name |\n|---|---|");
    for i in 0..rows {
        text += &format!("\n| {i} | row {i} |");
    }
    text
}

struct Message {
    id: String,
    body: String,
    kind: String,
}

fn message(id: &str, body: &str, kind: &str) -> Message {
    Message { id: id.into(), body: body.into(), kind: kind.into() }
}

fn sources(messages: &[Message]) -> Vec<Source<'_>> {
    messages
        .iter()
        .map(|m| Source { id: &m.id, body: &m.body, kind: &m.kind, author: "assistant", activity: false })
        .collect()
}

fn key(rows: &TranscriptRows, messages: &[Message], row: usize) -> String {
    let part = &rows.rows()[row];
    part.row_key(&messages[part.source].id)
}

#[test]
fn big_tables_split_into_chunks_that_repeat_the_header() {
    let parts = split_markdown(&format!("Intro.\n\n{}", table(140)));
    assert!(parts.len() >= 9);
    for part in &parts[1..] {
        assert!(part.starts_with("| n | name |\n|---|---|"), "{}", &part[..40.min(part.len())]);
    }
    // Every table row appears exactly once across the parts.
    assert_eq!(parts.iter().map(|p| p.matches("| row ").count()).sum::<usize>(), 140);
}

#[test]
fn small_messages_stay_whole() {
    assert_eq!(split_markdown(&format!("Short.\n\n{}", table(10))).len(), 1);
}

#[test]
fn long_text_splits_at_block_boundaries() {
    let mut text = String::new();
    for i in 0..40 {
        text += &format!("Paragraph {i} {}\n\n", "x".repeat(300));
    }
    let text = text.trim();
    let parts = split_markdown(text);
    assert!(parts.len() >= 4);
    assert_eq!(parts.join("\n\n"), text);
}

#[test]
fn finishing_message_splits_without_resetting() {
    let mut messages = vec![message("a", "Hello.", "final"), message("b", &table(140), "live")];
    let mut rows = TranscriptRows::default();
    rows.rebuild(&sources(&messages));
    assert_eq!(rows.count(), 2, "a live message is never split");
    messages[1].kind = "final".into();
    let changes = rows.data_changed(1, 1, &sources(&messages), true, false);
    assert!(!changes.contains(&Change::Reset));
    assert!(changes.iter().any(|c| matches!(c, Change::Insert { .. })));
    assert!(rows.count() > 9);
    assert_eq!(key(&rows, &messages, 1), "b#0");
    assert_eq!(key(&rows, &messages, 2), "b#1");
    assert_eq!(rows.rows()[1].parts, rows.count() - 1);
    // The first part kept its row and was updated in place.
    assert_eq!(changes[0], Change::Update { first: 1, last: 1, all_roles: true });
}

#[test]
fn inserts_and_removes_map_around_split_messages() {
    let mut messages = vec![message("a", "One.", "final"), message("big", &table(100), "final"), message("c", "Three.", "final")];
    let mut rows = TranscriptRows::default();
    rows.rebuild(&sources(&messages));
    let big_parts = rows.count() - 2;
    assert!(big_parts > 1);
    messages.insert(0, message("older", "Zero.", "final"));
    assert_eq!(rows.inserted(0, 0, &sources(&messages)), [Change::Insert { at: 0, count: 1 }]);
    assert_eq!(rows.count(), big_parts + 3);
    assert_eq!(key(&rows, &messages, 0), "older");
    assert_eq!(rows.row_for_source(2), Some(2));
    assert_eq!(rows.row_for_source(3), Some(2 + big_parts));
    assert_eq!(key(&rows, &messages, rows.row_for_source(3).unwrap()), "c");
    messages.remove(2); // the split message
    assert_eq!(rows.removed(2, 2), [Change::Remove { at: 2, count: big_parts }]);
    assert_eq!(rows.count(), 3);
    assert_eq!(key(&rows, &messages, 2), "c");
    assert_eq!(rows.source_row(2), Some(2));
}

#[test]
fn unsplittable_rows_stay_whole_and_role_only_changes_keep_parts() {
    let big = table(100);
    let user = Source { id: "u", body: &big, kind: "final", author: "user", activity: false };
    let activity = Source { id: "t", body: &big, kind: "final", author: "assistant", activity: true };
    let mut rows = TranscriptRows::default();
    rows.rebuild(&[user, activity]);
    assert_eq!(rows.count(), 2, "only assistant replies split");
    let messages = vec![message("big", &big, "final")];
    rows.rebuild(&sources(&messages));
    let parts = rows.count();
    assert_eq!(rows.data_changed(0, 0, &sources(&messages), false, false), [Change::Update { first: 0, last: parts - 1, all_roles: false }]);
    assert_eq!(rows.count(), parts);
}
