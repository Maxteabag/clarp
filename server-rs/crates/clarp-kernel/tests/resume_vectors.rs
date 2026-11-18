//! Continuing stopped work and letting a held queue go from the desk of the account seats,
//! held to what the Python Host answers (`tests/fixtures/resume-vectors.json`, written by
//! `server-rs/tools/resume-vectors.py`): what a continued agent is told, and stories of
//! agents that were put away, are janitors, are held, ended a turn or work, of desks that
//! list them as stopped or do not, and of sends that work, fail, or cannot be confirmed.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::BTreeMap;

use clarp_kernel::resume::{self, Agent, Continued, Delivery, Standing, Wanted};
use serde_json::{Value, json};

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/resume-vectors.json")).unwrap()
}

#[test]
fn a_continued_agent_is_told_what_the_python_host_tells_it() {
    let vectors = vectors();
    let prompts = vectors["prompts"].as_array().unwrap();
    let mut with_a_note = 0;
    for one in prompts {
        let told = resume::prompt(
            &one["row"],
            one["observed_at"].as_i64(),
            one["now"].as_i64().unwrap(),
        );
        assert_eq!(told, one["text"].as_str().unwrap(), "{}", one["row"]);
        assert!(!told.contains('\n'), "{told}");
        with_a_note += usize::from(told.contains("hotseat_note"));
    }
    assert_eq!(prompts.len(), 30);
    assert!(
        with_a_note > 5 && with_a_note < prompts.len() - 5,
        "{with_a_note}"
    );
}

/// What became of the send, as the story's way of sending says it.
fn delivery<'a>(step: &'a Value) -> Delivery<'a> {
    let said = &step["delivered"][0];
    Delivery {
        ok: said["ok"] == true,
        queued: said["queued"] == true,
        unconfirmed: said["unconfirmed"] == true,
        error: said["error"].as_str().unwrap_or(""),
        saved: step["saved_after"] == true,
    }
}

#[test]
fn an_asking_from_the_desk_is_answered_as_the_python_host_answers_it() {
    let vectors = vectors();
    let stories = vectors["stories"].as_array().unwrap();
    let (mut askings, mut prompts, mut let_go) = (0, 0, 0);
    let mut answered: BTreeMap<u64, usize> = BTreeMap::new();
    for story in stories {
        let name = story["story"].as_str().unwrap();
        let steps = story["steps"].as_array().unwrap().iter().enumerate();
        for (index, step) in steps.filter(|(_, step)| step["op"] == "ask") {
            askings += 1;
            let at = format!("{name}, step {index}");
            let asked = |key: &str| step["asked"][key].as_str().unwrap_or("");
            let wanted = resume::wanted(
                asked("id"),
                asked("kind"),
                asked("backend"),
                asked("action"),
            );
            // What the Python Host knew of the agent when it was asked: an agent that was
            // deleted is none.
            let knew = &step["knew"];
            let text = |key: &str| knew[key].as_str().unwrap_or("");
            let agent = Agent {
                session: text("session"),
                agent_id: text("agent_id"),
                name: text("name"),
                archived: knew["archived"] == true,
                janitor: knew["janitor"] == true,
                held: knew["held"] == true,
                works: knew["works"] == true,
            };
            let found = (knew.is_object() && knew["deleted"] != true).then_some(&agent);
            let (answer, sent) = match wanted {
                Wanted::Answered(answer) => (answer, None),
                Wanted::LetGo(_) | Wanted::Continue(_) if resume::in_the_way(found).is_some() => {
                    (resume::in_the_way(found).unwrap(), None)
                }
                Wanted::LetGo(_) => {
                    // The story lets the queue go, and says what it is after that.
                    let_go += 1;
                    let held = step["held_after"] == true;
                    (
                        resume::let_go(agent.session, held, knew["waiting"].as_u64().unwrap()),
                        None,
                    )
                }
                Wanted::Continue(_) => {
                    let stop = resume::stop_of(&agent, &knew["stopped_work"]);
                    let standing = Standing {
                        stop,
                        observed_at: knew["observed_at"].as_i64(),
                        can_send: step["sends"] != "none",
                        already_sent: knew["already_sent"] == true,
                        completed_at: knew["completed_at"].as_i64(),
                        now: step["at"].as_i64().unwrap(),
                    };
                    match resume::continued(&agent, &standing) {
                        Continued::Refused(answer) => (answer, None),
                        Continued::Send { text, message_id } => {
                            let stopped_at = stop.unwrap().1;
                            let how = delivery(step);
                            let answer =
                                resume::sent(&agent, stopped_at, standing.already_sent, &how);
                            (answer, Some(json!([agent.session, text, message_id])))
                        }
                    }
                }
            };
            assert_eq!(json!(answer.0), step["status"], "{at}: {}", answer.1);
            assert_eq!(answer.1, step["body"], "{at}");
            assert_eq!(json!(sent.iter().collect::<Vec<_>>()), step["sent"], "{at}");
            assert_eq!(
                matches!(wanted_again(step), Wanted::LetGo(_)) && answer.0 == 200,
                !step["let_go"].as_array().unwrap().is_empty(),
                "{at}"
            );
            prompts += usize::from(sent.is_some());
            *answered.entry(answer.0.into()).or_insert(0) += 1;
        }
    }
    // Counted by the script that wrote the vectors.
    assert_eq!((stories.len(), askings, prompts), (21, 102, 39));
    let by_status: Vec<(u64, usize)> = answered.into_iter().collect();
    assert_eq!(
        by_status,
        [
            (200, 43),
            (400, 12),
            (404, 6),
            (409, 36),
            (502, 4),
            (503, 1)
        ]
    );
    assert!(let_go > 2, "{let_go}");
}

fn wanted_again(step: &Value) -> Wanted {
    let asked = |key: &str| step["asked"][key].as_str().unwrap_or("");
    resume::wanted(
        asked("id"),
        asked("kind"),
        asked("backend"),
        asked("action"),
    )
}

#[test]
fn an_asking_to_switch_is_read_or_refused_as_the_python_host_reads_it() {
    let vectors = vectors();
    let askings = vectors["switches"].as_array().unwrap();
    let mut taken = 0;
    for one in askings {
        let (tool, alias) = (
            one["provider"].as_str().unwrap(),
            one["alias"].as_str().unwrap(),
        );
        match resume::switch_wanted(tool, alias) {
            Ok((tool, alias)) => {
                taken += 1;
                assert_eq!(json!([tool, alias]), one["taken"], "{one}");
            }
            Err((status, said)) => {
                assert_eq!(
                    (json!(status), &said["error"]),
                    (one["status"].clone(), &one["error"]),
                    "{one}"
                );
                assert_eq!(said["ok"], false);
            }
        }
    }
    assert_eq!((askings.len(), taken), (39, 9));
}

#[test]
fn the_prompt_of_a_stop_has_one_id_however_it_is_asked_for() {
    assert_eq!(
        resume::message_id("an-agent", 1_800_000_000_000),
        "hotseat-resume-an-agent-1800000000000"
    );
}
