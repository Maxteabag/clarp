#![allow(clippy::unwrap_used)]

use std::collections::BTreeMap;

use clarp_kernel::teams::{
    Change, Team, Teams,
    runtime::{self, Brief, Deliveries, Person},
};
use serde_json::{Value, json};

const NOW: i64 = 1_795_000_000_000;

fn person(id: &str) -> Option<Person> {
    let (name, session, state) = match id {
        "a-leader" => ("Leader", "leader", "done"),
        "a-worker" => ("Worker", "worker", "interrupted"),
        "a-other" => ("Other", "other", "done"),
        _ => return None,
    };
    Some(Person {
        name: name.into(),
        session: session.into(),
        state: state.into(),
    })
}

#[test]
fn teams_runtime_matches_the_reference_fanout_digest_protocol_and_wake_rules() {
    let vectors: Value =
        serde_json::from_str(include_str!("fixtures/teams-runtime-vectors.json")).unwrap();
    let mut teams = Teams::new();
    for (id, name, live) in [("team-ops", "Ops", true), ("team-muted", "Silent", false)] {
        let mut team = Team::create(
            &teams,
            id.into(),
            json!({"name":name}).as_object().unwrap(),
            NOW,
        )
        .unwrap();
        team.add("a-leader".into(), NOW);
        team.add("a-worker".into(), NOW);
        if live {
            let change = Change::read(
                json!({"communication_enabled":true, "leader":"a-leader"})
                    .as_object()
                    .unwrap(),
            )
            .unwrap();
            team = team.update(&teams, change, NOW).unwrap();
            team.nudge_enabled = true;
        }
        teams.insert(id.into(), team);
    }
    for row in vectors["blocks"].as_array().unwrap() {
        assert_eq!(
            json!(runtime::blocks(row["text"].as_str().unwrap())),
            row["blocks"]
        );
    }
    let blocks = runtime::blocks(vectors["raw"].as_str().unwrap());
    let feed = runtime::fanout(&teams, "a-worker", "reply-1", "trace-1", &blocks, NOW);
    assert_eq!(json!(feed.len()), vectors["inserted"]);
    let mut deliveries = Deliveries::new();
    let mut ids = Vec::new();
    for (order, mut row) in feed.into_iter().enumerate() {
        assert_eq!(row.message.team_id, "team-ops");
        assert_eq!(row.inbox, BTreeMap::from([("a-leader".into(), None)]));
        row.order = order as u64 + 1;
        let key = row.message.team_message_id.clone();
        ids.push(key.clone());
        deliveries
            .entry(row.message.team_id.clone())
            .or_default()
            .insert(key, row);
    }
    ids.sort();
    assert_eq!(json!(ids), vectors["inbox"]);
    let (digest, mut inbox) = runtime::pending(&teams, &deliveries, "a-leader", &person);
    inbox.sort();
    assert_eq!(json!(inbox), vectors["inbox"]);
    assert_eq!(digest, vectors["digest"]);
    for row in vectors["protocols"].as_array().unwrap() {
        let agent = row["agent"].as_str().unwrap();
        let origin = row["origin"].as_str().unwrap();
        let text = runtime::protocol(
            &teams,
            agent,
            &person(agent).unwrap().session,
            origin,
            &person,
            || panic!("this vector is not an ordinary leader turn"),
        );
        assert_eq!(text, row["text"]);
    }
    let brief = Brief {
        protocol: runtime::protocol(&teams, "a-leader", "leader", "leader_tick", &person, || {
            panic!("leader ticks use their lean brief")
        }),
        digest,
        ids: inbox.clone(),
    };
    assert_eq!(brief.prompt("User asks"), vectors["prompt"]);
    let allowed: Vec<_> = ["a-leader", "a-worker", "a-other"]
        .iter()
        .map(|id| runtime::nudge_allowed(&teams, id))
        .collect();
    assert_eq!(json!(allowed), vectors["nudge_allowed"]);
    assert_eq!(
        json!(runtime::wake_reason(1, true)),
        vectors["ticks"][0]["reason"]
    );
    assert_eq!(runtime::wake_reason(0, false), None);
    assert_eq!(
        json!(runtime::latest_activity(
            &teams,
            &deliveries,
            "a-leader",
            |_| 0
        )),
        vectors["before"]
    );
    for delivery in deliveries.get_mut("team-ops").unwrap().values_mut() {
        delivery.inbox.insert("a-leader".into(), Some(NOW + 20));
    }
    assert_eq!(
        json!(runtime::latest_activity(
            &teams,
            &deliveries,
            "a-leader",
            |_| 0
        )),
        vectors["after"]
    );
    assert_eq!(
        json!(runtime::pending(&teams, &deliveries, "a-leader", &person)),
        vectors["after_digest"]
    );
    assert!(!runtime::has_unread(&teams, &deliveries, "a-leader"));
    teams.get_mut("team-ops").unwrap().nudge_enabled = false;
    assert!(!runtime::nudge_allowed(&teams, "a-leader"));
    assert_eq!(vectors["disabled_ticks"], json!([]));
    for row in vectors["noop"].as_array().unwrap() {
        let text = row["text"].as_str().unwrap();
        let normalized = runtime::leader_reply(text);
        assert_eq!(
            json!([normalized.is_none(), normalized.unwrap_or_default()]),
            row["result"]
        );
    }
}
