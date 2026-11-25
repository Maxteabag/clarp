//! A reply's spoken part and its written part (#823).

use clarp_core::text::reply_parts;

fn parts(text: &str, streaming: bool) -> (String, String) {
    reply_parts(text, streaming)
}

#[test]
fn a_gist_before_the_answer_is_the_summary_and_the_rest_the_markdown() {
    let (spoken, written) = parts("<speak>Sure, the gist: X.</speak>\n## Heading\n\nThe answer.", false);
    assert_eq!(spoken, "Sure, the gist: X.");
    assert_eq!(written, "## Heading\n\nThe answer.");
}

#[test]
fn a_gist_written_right_against_a_heading_leaves_the_heading_a_heading() {
    let (spoken, written) = parts("Intro.\n<speak>Gist.</speak>## Heading", false);
    assert_eq!(spoken, "Gist.");
    assert_eq!(written, "Intro.\n## Heading");
}

#[test]
fn a_reply_that_is_only_spoken_shows_its_words_once_as_its_text() {
    assert_eq!(parts("<speak>All done, the build is green.</speak>", false), (String::new(), "All done, the build is green.".into()));
}

#[test]
fn a_reply_with_no_speech_is_all_written() {
    assert_eq!(parts("## Plain\n\nNo voice here.", false), (String::new(), "## Plain\n\nNo voice here.".into()));
}

#[test]
fn several_speak_blocks_join_into_one_summary() {
    let (spoken, written) = parts("<speak>First.</speak>\nBody one.\n<SPEAK>Second.</SPEAK>\nBody two.", false);
    assert_eq!(spoken, "First. Second.");
    assert_eq!(written, "Body one.\n\nBody two.");
}

#[test]
fn a_block_inside_a_line_keeps_the_words_around_it_apart() {
    let (spoken, written) = parts("Before<speak>said</speak>after.", false);
    assert_eq!(spoken, "said");
    assert_eq!(written, "Before after.");
}

#[test]
fn an_empty_speak_block_leaves_no_summary() {
    assert_eq!(parts("<speak> </speak>\nThe answer.", false), (String::new(), "The answer.".into()));
}

#[test]
fn an_unfinished_speak_while_streaming_is_spoken_so_far() {
    // Only the gist has arrived: it is the reply's text for now.
    assert_eq!(parts("<speak>Sure, the gi", true), (String::new(), "Sure, the gi".into()));
    // A closing tag still arriving is not shown.
    assert_eq!(parts("<speak>Sure.</spe", true), (String::new(), "Sure.".into()));
    // An opening tag still arriving after the written part is not shown.
    assert_eq!(parts("<speak>Gist.</speak>\nAnswer <spe", true), ("Gist.".into(), "Answer".into()));
}

#[test]
fn fillers_and_pauses_inside_the_spoken_part_are_cleaned() {
    let (spoken, written) = parts("<speak>Sure <break time=\"300ms\"/> here it is, <vox>um</vox>, the plan.</speak>\nThe answer.", false);
    assert_eq!(spoken, "Sure, here it is, the plan.");
    assert_eq!(written, "The answer.");
}
