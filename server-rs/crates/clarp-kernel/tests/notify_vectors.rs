//! Whether a turn tells the user, against the Python Host's rule.
//!
//! `fixtures/notify-vectors.json` is written by `server-rs/tools/notify-vectors.py` from
//! `server/lib/policies/notifications.py`: what it decides for every combination of what it
//! looks at. The same combinations must come to the same decisions here.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_kernel::notify::{Capabilities, Completed, Decision, decide};
use serde_json::Value;

#[test]
fn a_turn_tells_the_user_exactly_when_the_python_host_says_so() {
    let vectors: Value =
        serde_json::from_str(include_str!("fixtures/notify-vectors.json")).unwrap();
    let facts = &vectors["facts"];
    let names: Vec<&str> = facts
        .as_object()
        .unwrap()
        .keys()
        .map(String::as_str)
        .collect();
    let sizes: Vec<usize> = names
        .iter()
        .map(|name| facts[*name].as_array().unwrap().len())
        .collect();
    let decisions: Vec<&str> = vectors["decisions"]
        .as_array()
        .unwrap()
        .iter()
        .map(|decision| decision.as_str().unwrap())
        .collect();
    let table = vectors["table"].as_str().unwrap();
    let total: usize = sizes.iter().product();
    assert_eq!(total * 2, table.len(), "one decision for every combination");
    assert!(total > 80_000, "only {total} combinations");

    let mut wrong: Vec<String> = Vec::new();
    for case in 0..total {
        // The generator's order: the fact that is listed last changes fastest.
        let mut rest = case;
        let mut picked = vec![0; sizes.len()];
        for place in (0..sizes.len()).rev() {
            picked[place] = rest % sizes[place];
            rest /= sizes[place];
        }
        let fact = |name: &str| {
            let place = names.iter().position(|known| *known == name).unwrap();
            &facts[name][picked[place]]
        };
        let text = |name: &str| fact(name).as_str().unwrap();
        let flag = |name: &str| fact(name).as_bool().unwrap();
        let turn = Completed {
            agent_id: text("agent_id"),
            has_cause: flag("has_cause"),
            content: text("content"),
            interrupted: flag("interrupted"),
            client: text("client"),
        };
        let agent = Capabilities {
            present: flag("present"),
            can_chat: flag("can_chat"),
            muted: flag("muted"),
            is_team_leader: flag("is_team_leader"),
            quiet: flag("quiet"),
        };
        let ours = match decide(text("origin"), &turn, &agent, flag("special_automation")) {
            Decision::Notify {
                reason,
                kind,
                push,
                muted,
            } => format!(
                "notify|{reason}|{kind}|{}|{}",
                u8::from(push),
                u8::from(muted)
            ),
            Decision::Suppress { reason } => format!("suppress|{reason}"),
        };
        let index = usize::from_str_radix(&table[case * 2..case * 2 + 2], 16).unwrap();
        let theirs = decisions[index];
        if ours != theirs && wrong.len() < 12 {
            wrong.push(format!(
                "origin {:?}, {turn:?}, {agent:?}, special {}: here {ours}, the Python Host's {theirs}",
                text("origin"),
                flag("special_automation")
            ));
        }
    }
    assert!(wrong.is_empty(), "decisions differ:\n{}", wrong.join("\n"));
}
