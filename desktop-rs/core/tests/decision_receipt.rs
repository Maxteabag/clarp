//! The Host's resolved-decision prompt reads as a receipt: the question,
//! how it was resolved and the decision it answers; never its protocol
//! lines. The prompts are `artifacts.format_delivery_prompt`'s, verbatim
//! (server/lib/artifacts.py; the Rust Host writes the same, see
//! server-rs/conformance/golden-again/core/approval.json and question.json).

use clarp_core::decision_receipt::{Outcome, parse};

/// The Host's envelope today: `Resolved by:` sits between the artifact and
/// the question.
fn prompt_by(resolved_by: &str, question: &str, context: &str, outcome: &str) -> String {
    format!(
        "[Clarp decision resolved]\nDecision ID: dec-7\nArtifact ID: art-42\nResolved by: {resolved_by}\nQuestion: {question}\nContext: {context}\nReference: ref-1\nPayload: {{\"branch\": \"main\"}}\n{outcome}"
    )
}

fn prompt(question: &str, context: &str, outcome: &str) -> String {
    prompt_by("user", question, context, outcome)
}

/// The envelope before the Host added `Resolved by:` (older chats).
fn earlier_prompt(question: &str, context: &str, outcome: &str) -> String {
    format!(
        "[Clarp decision resolved]\nDecision ID: dec-7\nArtifact ID: art-42\nQuestion: {question}\nContext: {context}\nReference: ref-1\nPayload: {{\"branch\": \"main\"}}\n{outcome}"
    )
}

/// The outcome sentences `format_delivery_prompt` writes, with the actor it
/// names ("The user" or "The user, through agent <id>").
fn sentences(actor: &str, question: &str) -> Vec<(String, Outcome, &'static str, &'static str)> {
    vec![
        (
            format!("{actor} approved this exact action: {question} Approved scope/context: CI is green.\nTwo reviews. Proceed with exactly that action and scope after revalidating the target and payload. This does not approve any broader, additional, or repeated action."),
            Outcome::Approved,
            "Approved",
            "success",
        ),
        (
            format!("{actor} rejected this exact action: {question} Do not perform it or a broader version of it."),
            Outcome::Declined,
            "Declined",
            "danger",
        ),
        (
            format!("{actor} answered this clarification: {{\"option_id\": \"arnold\", \"label\": \"Arnold\"}}. Continue using this answer. This does not grant approval for unrelated protected actions."),
            Outcome::Answered("Arnold".into()),
            "Arnold",
            "answer",
        ),
        (
            format!("{actor} answered this clarification: {{\"text\": \"Ship it on Friday. Not before.\"}}. Continue using this answer. This does not grant approval for unrelated protected actions."),
            Outcome::Answered("Ship it on Friday. Not before.".into()),
            "Ship it on Friday. Not before.",
            "answer",
        ),
        (
            "The user discarded this request. This is not an answer or approval. Do not guess permission or repeat the unchanged request. Continue independent work only.".into(),
            Outcome::Discarded,
            "Discarded",
            "muted",
        ),
        (
            "The request expired without an answer or approval. Do not infer a choice or perform the protected action. Continue independent work only.".into(),
            Outcome::Expired,
            "Expired",
            "muted",
        ),
    ]
}

#[test]
fn every_outcome_the_host_sends_reads_as_its_chip() {
    let question = "Deploy the release to production?";
    for (resolved_by, actor) in [("user", "The user"), ("agent:koko-8255", "The user, through agent koko-8255")] {
        for (sentence, outcome, label, tone) in sentences(actor, question) {
            let receipt = parse(&prompt_by(resolved_by, question, "CI is green.\nTwo reviews.", &sentence)).expect(&sentence);
            assert_eq!(receipt.decision_id, "dec-7");
            assert_eq!(receipt.artifact_id, "art-42");
            assert_eq!(receipt.question, question);
            assert_eq!(receipt.outcome, outcome, "{sentence}");
            assert_eq!(receipt.outcome.label(), label);
            assert_eq!(receipt.outcome.tone(), tone);
            assert!(!receipt.outcome.mark().is_empty(), "the status never rests on colour alone");
        }
    }
}

/// An approval repeats the question; one that runs over lines still reads.
#[test]
fn a_question_over_several_lines_still_reads() {
    let question = "Deploy the release?\nOnly the web tier.";
    let sentence = format!("The user approved this exact action: {question} Proceed with exactly that action and scope after revalidating the target and payload.");
    let receipt = parse(&prompt(question, "", &sentence)).unwrap();
    assert_eq!(receipt.question, question);
    assert_eq!(receipt.outcome, Outcome::Approved);
}

/// Chats from before the Host's current wording still read.
#[test]
fn the_earlier_envelope_and_wording_still_read() {
    let cases = [
        ("The user chose: accepted. Approval applies only to the described action. Revalidate it before acting.", Outcome::Approved),
        ("The user chose: rejected. Do not perform the protected action.", Outcome::Declined),
        (
            "The user answered this clarification: {\"option_id\": \"arnold\", \"label\": \"Arnold\"}. Continue using this answer. This does not grant approval for unrelated protected actions.",
            Outcome::Answered("Arnold".into()),
        ),
    ];
    for (sentence, outcome) in cases {
        assert_eq!(parse(&earlier_prompt("Deploy?", "", sentence)).expect(sentence).outcome, outcome);
        assert_eq!(parse(&prompt("Deploy?", "", sentence)).expect(sentence).outcome, outcome);
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
        // No payload line.
        "[Clarp decision resolved]\nDecision ID: d\nArtifact ID: a\nResolved by: user\nQuestion: q\nContext: c\nThe user approved this exact action: q",
        // A line before the question that is not a header line.
        "[Clarp decision resolved]\nDecision ID: d\nArtifact ID: a\nsomething else\nQuestion: q\nContext: \nReference: \nPayload: {}\nThe user approved this exact action: q",
    ] {
        assert_eq!(parse(text), None, "{text:?}");
    }
    // An outcome this client does not know still hides the protocol lines.
    let receipt = parse(&prompt("Q?", "", "The user did something new.")).unwrap();
    assert_eq!(receipt.outcome.label(), "Resolved");
    // A clarification whose answer does not parse still says answered.
    let receipt = parse(&prompt("Q?", "", "The user answered this clarification: not json. Continue using this answer.")).unwrap();
    assert_eq!(receipt.outcome, Outcome::Answered(String::new()));
    assert_eq!(receipt.outcome.label(), "Answered");
}

/// One line of a reply or prompt for a preview (sidebar, overview,
/// notification): voice markup gone, a resolved decision read as one.
#[test]
fn a_preview_line_never_shows_voice_markup_or_the_decision_envelope() {
    use clarp_core::decision_receipt::preview_line;
    assert_eq!(preview_line("<speak>Done <break time=\"350ms\"/> pushed it</speak>"), "Done, pushed it");
    assert_eq!(preview_line("<vox>um</vox> **Fixed** the `parser`"), "Fixed the parser");
    let whole = prompt("Deploy?", "", "The user approved this exact action: Deploy? Proceed with exactly that action and scope after revalidating the target and payload.");
    assert_eq!(preview_line(&whole), "Deploy? · Approved");
    // The Host's own preview: one line, "You: ", cut at 80 characters.
    assert_eq!(preview_line("You: [Clarp decision resolved] Decision ID: dec_01 Artifact ID: art_02 Resolved by: user Question: Dep…"), "You: Resolved a decision");
    assert_eq!(preview_line("You: please deploy"), "You: please deploy");
}
