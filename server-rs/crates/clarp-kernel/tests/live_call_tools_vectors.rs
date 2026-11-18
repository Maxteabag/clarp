#![allow(clippy::unwrap_used)]
use clarp_kernel::live_call::receipts;
use serde_json::{Value, json};

#[test]
fn the_voice_is_told_exactly_what_the_director_confirmed() {
    let fixture: Value =
        serde_json::from_str(include_str!("fixtures/live-call-tools-vectors.json")).unwrap();
    let mut answers = 0;
    for row in fixture["outcomes"].as_array().unwrap() {
        assert_eq!(
            receipts::director(&row["answer"], row["status"].as_i64()),
            row["receipt"],
            "{row}"
        );
        answers += 1;
    }
    let mut notes = 0;
    for row in fixture["notes"].as_array().unwrap() {
        assert_eq!(
            json!(receipts::notes(row["receipts"].as_array().unwrap())),
            row["notes"],
            "note case {notes}"
        );
        notes += 1;
    }
    assert_eq!((answers, notes), (129, 102));
}
#[test]
fn tool_proposals_and_direct_prompts_follow_the_frozen_team() {
    use clarp_kernel::live_call::team::{self, Action, Prompt, Team};
    let fixture: Value =
        serde_json::from_str(include_str!("fixtures/live-call-tools-vectors.json")).unwrap();
    let members = [json!({"session":"test-member","name":"Theo"})];
    let mut steps = 0;
    for story in fixture["calls"].as_array().unwrap() {
        let mut team = Team::default();
        let mut nonce = 100;
        for step in story["steps"].as_array().unwrap() {
            let name = step["name"].as_str().unwrap();
            let p = team.propose(&members, name, &step["args"]);
            let mut effects = Vec::new();
            let answer = match p.action {
                Action::Answer(answer) => answer,
                Action::Delegate { normalized, .. } => {
                    let id = team::operation(
                        "rq-",
                        &["host-test", "call-one", p.session, &nonce.to_string()],
                    );
                    nonce += 1;
                    if story["delivery"] == "closed" {
                        team::ended(name)
                    } else {
                        effects.push(json!([
                            "prompt",
                            p.session,
                            "guard",
                            team::prompt("Guard", &normalized)
                        ]));
                        let outcome = match story["delivery"].as_str().unwrap() {
                            "sent" => Prompt::Sent,
                            "refused" => Prompt::Refused("stand-in refusal"),
                            "uncertain" => Prompt::Uncertain("stand-in uncertain"),
                            _ => unreachable!(),
                        };
                        team::prompted(p.name, &id, outcome)
                    }
                }
                _ if story["delivery"] == "closed" => team::ended(name),
                Action::Status => {
                    effects.push(json!(["status", p.session]));
                    team::read(name, p.name, json!({"state":"idle","last_said":"fixture"}))
                }
                Action::Transcript(limit) => {
                    effects.push(json!(["transcript", p.session, limit]));
                    team::read(
                        name,
                        p.name,
                        json!({"messages":[{"role":"assistant","text":"fixture"}]}),
                    )
                }
            };
            assert_eq!(answer, step["result"], "{name}");
            assert_eq!(json!(effects), step["effects"], "{name}");
            steps += 1;
        }
    }
    assert_eq!(steps, 140);
}
