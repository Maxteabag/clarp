use clarp_core::message_search::{SearchIndex, Source, snippet};
use clarp_core::protocol::Message;

fn row(id: &str, role: &str, text: &str, timestamp: &str, revision: i64) -> Message {
    Message { id: id.into(), role: role.into(), text: text.into(), timestamp: timestamp.into(), revision, ..Message::default() }
}

const NOW: i64 = 1_790_000_000_000; // 2026-09-21

fn day(n: i64) -> String {
    let ms = NOW - n * 86_400_000;
    chrono::DateTime::from_timestamp_millis(ms).unwrap().format("%Y-%m-%dT%H:%M:%SZ").to_string()
}

#[test]
fn finds_text_in_any_chat_with_every_term() {
    let mut index = SearchIndex::default();
    index.update_chat("rachel", &[row("r1", "user", "Hello there", &day(1), 1), row("r2", "assistant", "Run cargo test before you push", &day(1), 2)], Source::Memory);
    index.update_chat("mike", &[row("m1", "assistant", "The test for cargo is green", &day(2), 1)], Source::Cache);
    let hits = index.search("cargo test", 10, NOW);
    let found: Vec<(&str, &str)> = hits.iter().map(|h| (h.session.as_str(), h.id.as_str())).collect();
    assert_eq!(found, [("rachel", "r2"), ("mike", "m1")], "the phrase outranks scattered terms");
    assert_eq!(hits[1].source, Source::Cache);
    assert!(index.search("cargo nothing", 10, NOW).is_empty(), "every term must match");
    assert!(index.search("  ", 10, NOW).is_empty(), "no query, no results");
}

#[test]
fn matching_ignores_case_and_skips_activity_and_live_rows() {
    let mut index = SearchIndex::default();
    let mut tool = row("t1", "assistant", "pelican tool output", &day(0), 1);
    tool.activity = true;
    let mut live = row("l1", "assistant", "pelican live", &day(0), 2);
    live.kind = "live".into();
    let mut shown = row("d1", "assistant", "raw", &day(0), 3);
    shown.display_text = "The PELICAN report".into();
    index.update_chat("rachel", &[tool, live, shown], Source::Memory);
    let hits = index.search("Pelican", 10, NOW);
    assert_eq!(hits.iter().map(|h| h.id.as_str()).collect::<Vec<_>>(), ["d1"], "display text is searched, activity and live rows are not");
}

#[test]
fn equal_matches_rank_newest_first_and_better_matches_beat_recency() {
    let mut index = SearchIndex::default();
    index.update_chat("a", &[row("old", "user", "deploy the host", &day(30), 1), row("new", "user", "deploy the host", &day(1), 2)], Source::Memory);
    index.update_chat("b", &[row("partial", "user", "redeployment of the hosting", &day(0), 1)], Source::Memory);
    let ids: Vec<String> = index.search("deploy host", 10, NOW).into_iter().map(|h| h.id).collect();
    assert_eq!(ids, ["new", "old", "partial"], "word-start matches beat a newer mid-word match; equal ones go newest first");
    assert_eq!(index.search("deploy host", 1, NOW).len(), 1, "the limit holds");
}

#[test]
fn updates_are_incremental_and_a_chat_can_be_dropped() {
    let mut index = SearchIndex::default();
    let rows = vec![row("1", "user", "alpha", &day(1), 1), row("2", "assistant", "beta", &day(1), 2)];
    assert_eq!(index.update_chat("rachel", &rows, Source::Memory), 2, "the first pass indexes every row");
    assert_eq!(index.update_chat("rachel", &rows, Source::Memory), 0, "unchanged rows are not indexed again");
    let mut grown = rows.clone();
    grown[1] = row("2", "assistant", "beta gamma", &day(1), 3);
    grown.push(row("3", "user", "delta", &day(0), 4));
    assert_eq!(index.update_chat("rachel", &grown, Source::Memory), 2, "only the changed and the new row");
    assert_eq!(index.search("gamma", 10, NOW).len(), 1);
    assert_eq!(index.update_chat("rachel", &grown[1..], Source::Memory), 0);
    assert!(index.search("alpha", 10, NOW).is_empty(), "a row the chat no longer holds is gone");
    assert_eq!(index.chats(Source::Memory), 1);
    index.remove_chat("rachel");
    assert!(index.search("delta", 10, NOW).is_empty());
    assert_eq!(index.chats(Source::Memory), 0);
}

#[test]
fn a_loaded_chat_is_not_replaced_by_its_older_cached_copy() {
    let mut index = SearchIndex::default();
    index.update_chat("mike", &[row("m9", "user", "fresh words", &day(0), 9)], Source::Memory);
    index.update_chat("mike", &[row("m1", "user", "stale words", &day(5), 1)], Source::Cache);
    assert_eq!(index.search("words", 10, NOW).iter().map(|h| h.id.as_str()).collect::<Vec<_>>(), ["m9"]);
    assert_eq!((index.chats(Source::Memory), index.chats(Source::Cache)), (1, 0));
}

#[test]
fn the_snippet_centres_the_match_and_marks_every_term() {
    let text = format!("{} the build plan says run Cargo test\n\nfirst, then cargo fmt {}", "word ".repeat(40), "tail ".repeat(40));
    let segments = snippet(&text, &["cargo".into(), "test".into()], 100);
    let plain: String = segments.iter().map(|s| s.text.as_str()).collect();
    let marked: Vec<&str> = segments.iter().filter(|s| s.mark).map(|s| s.text.as_str()).collect();
    assert_eq!(marked, ["Cargo", "test", "cargo"], "matches keep their case and every one is marked");
    assert!(plain.starts_with('…') && plain.ends_with('…'), "a cut is shown: {plain}");
    assert!(!plain.contains('\n'), "the snippet is one line: {plain}");
    assert!(plain.chars().count() <= 102, "about the asked width: {}", plain.chars().count());
    let short = snippet("Ünïcode İs fine", &["is".into()], 100);
    assert_eq!(short.iter().filter(|s| s.mark).map(|s| s.text.as_str()).collect::<Vec<_>>(), ["İs"], "case folding keeps character places");
}
