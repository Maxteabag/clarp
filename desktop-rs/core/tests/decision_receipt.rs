//! The Host's resolved-decision prompt reads as a receipt: the question,
//! how it was resolved and the decision it answers; never its protocol
//! lines. The prompts are `artifacts.format_delivery_prompt`'s, verbatim.

use clarp_core::decision_receipt::{Outcome, parse};

fn prompt(question: &str, context: &str, outcome: &str) -> String {
    format!(
        "[Clarp decision resolved]\nDecision ID: dec-7\nArtifact ID: art-42\nQuestion: {question}\nContext: {context}\nReference: ref-1\nPayload: {{\"branch\": \"main\"}}\n{outcome}"
    )
}

#[test]
fn every_outcome_the_host_sends_reads_as_its_chip() {
    let cases = [
        ("The user chose: accepted. Approval applies only to the described action. Revalidate it before acting.", Outcome::Approved, "Approved", "success"),
        ("The user chose: rejected. Do not perform the protected action.", Outcome::Declined, "Declined", "danger"),
        (
            "The user answered this clarification: {\"option_id\": \"arnold\", \"label\": \"Arnold\"}. Continue using this answer. This does not grant approval for unrelated protected actions.",
            Outcome::Answered("Arnold".into()),
            "Arnold",
            "answer",
        ),
        (
            "The user answered this clarification: {\"text\": \"Ship it on Friday. Not before.\"}. Continue using this answer. This does not grant approval for unrelated protected actions.",
            Outcome::Answered("Ship it on Friday. Not before.".into()),
            "Ship it on Friday. Not before.",
            "answer",
        ),
        ("The user discarded this request. This is not an answer or approval. Do not guess permission or repeat the unchanged request. Continue independent work only.", Outcome::Discarded, "Discarded", "muted"),
        ("The request expired without an answer or approval. Do not infer a choice or perform the protected action. Continue independent work only.", Outcome::Expired, "Expired", "muted"),
    ];
    for (sentence, outcome, label, tone) in cases {
        let receipt = parse(&prompt("Deploy the release to production?", "CI is green.\nTwo reviews.", sentence)).expect(sentence);
        assert_eq!(receipt.decision_id, "dec-7");
        assert_eq!(receipt.artifact_id, "art-42");
        assert_eq!(receipt.question, "Deploy the release to production?");
        assert_eq!(receipt.outcome, outcome);
        assert_eq!(receipt.outcome.label(), label);
        assert_eq!(receipt.outcome.tone(), tone);
        assert!(!receipt.outcome.mark().is_empty(), "the status never rests on colour alone");
    }
}

#[test]
fn an_answer_with_unicode_and_quotes_reads_as_typed() {
    let sentence = "The user answered this clarification: {\"text\": \"Bruk «Østfold» og \\\"main\\\"\"}. Continue using this answer. This does not grant approval for unrelated protected actions.";
    let receipt = parse(&prompt("Which region?", "", sentence)).unwrap();
    assert_eq!(receipt.outcome, Outcome::Answered("Bruk «Østfold» og \"main\"".into()));
}

#[test]
fn anything_else_stays_a_message() {
    for text in [
        "",
        "The user chose: accepted.",
        "[Clarp decision resolved]",
        "Please look at [Clarp decision resolved]\nDecision ID: x",
        // The header without the lines that make it the Host's.
        "[Clarp decision resolved]\nDecision ID: d\nQuestion: q\nContext: \nThe user chose: accepted.",
    ] {
        assert_eq!(parse(text), None, "{text:?}");
    }
    // An outcome this client does not know still hides the protocol lines.
    let receipt = parse(&prompt("Q?", "", "The user did something new.")).unwrap();
    assert_eq!(receipt.outcome.label(), "Resolved");
    // A clarification whose answer does not parse still says answered.
    let receipt = parse(&prompt("Q?", "", "The user answered this clarification: not json. Continue using this answer.")).unwrap();
    assert_eq!(receipt.outcome, Outcome::Answered("Answered".into()));
}

/// One line of a reply or prompt for a preview (sidebar, overview,
/// notification): voice markup gone, a resolved decision read as one.
#[test]
fn a_preview_line_never_shows_voice_markup_or_the_decision_envelope() {
    use clarp_core::decision_receipt::preview_line;
    assert_eq!(preview_line("<speak>Done <break time=\"350ms\"/> pushed it</speak>"), "Done, pushed it");
    assert_eq!(preview_line("<vox>um</vox> **Fixed** the `parser`"), "Fixed the parser");
    let whole = prompt("Deploy?", "", "The user chose: accepted. Approval applies only to the described action.");
    assert_eq!(preview_line(&whole), "Deploy? · Approved");
    // The Host's own preview: one line, "You: ", cut at 80 characters.
    assert_eq!(preview_line("You: [Clarp decision resolved] Decision ID: dec_01 Artifact ID: art_02 Question: Dep…"), "You: Resolved a decision");
    assert_eq!(preview_line("You: please deploy"), "You: please deploy");
}
