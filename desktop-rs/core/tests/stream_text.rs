//! Streaming text: where a message's committed blocks end (closed by a
//! blank line outside a code fence) and how fast the paced reveal goes.

use clarp_core::stream_text::{commit_point, next_reveal};

#[test]
fn blocks_commit_at_the_last_blank_line_outside_a_fence() {
    assert_eq!(commit_point("one line, still going"), 0);
    let text = "First paragraph.\n\nSecond, still";
    assert_eq!(&text[..commit_point(text)], "First paragraph.\n\n");
    let fenced = "Intro.\n\n```sh\ncargo build\n\ncargo test\n";
    assert_eq!(&fenced[..commit_point(fenced)], "Intro.\n\n", "a blank line inside an open fence commits nothing");
    let closed = "Intro.\n\n```sh\ncargo build\n\ncargo test\n```\n\nAfter";
    assert_eq!(&closed[..commit_point(closed)], "Intro.\n\n```sh\ncargo build\n\ncargo test\n```\n\n");
    let tilde = "~~~\na\n\nb\n~~~\n\nc";
    assert_eq!(&tilde[..commit_point(tilde)], "~~~\na\n\nb\n~~~\n\n");
}

#[test]
fn the_reveal_catches_up_within_a_few_frames_and_stops_at_words() {
    let text = "The failure came from an off-by-one in the tokenizer's line counter.";
    let mut shown = 0;
    let mut frames = 0;
    while shown < text.len() {
        let next = next_reveal(shown, text);
        assert!(next > shown, "always moves");
        assert!(next == text.len() || text[next..].starts_with(' ') || text.as_bytes()[next - 1] == b' ', "ends at a word: {:?}", &text[..next]);
        shown = next;
        frames += 1;
    }
    assert!((3..=12).contains(&frames), "{frames} frames for {} chars", text.len());
    assert_eq!(next_reveal(5, "héllo wörld, ünïcode"), "héllo wörld, ünïcode".len().min(next_reveal(5, "héllo wörld, ünïcode")));
    assert!("héllo wörld".is_char_boundary(next_reveal(0, "héllo wörld")));
}
