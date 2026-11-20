//! Who an agent is when nobody named it, against the Python Host's own functions.
//!
//! `fixtures/contact-vectors.json` is written by `server-rs/tools/contact-vectors.py`: what
//! `contact_minting`, `contact_assignment`, `roster` and `policies/agent_spec` answer for
//! made-up rosters, in an isolated store. The same must be answered here.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_kernel::contacts::portrait_prompt;
use clarp_kernel::contacts::{
    Assigned, Assignee, Contact, Launch, Wearer, allows, assign, given_name, launch, personality,
    pool, tier_of_tool, tool_of_tier,
};
use clarp_kernel::personas::{Owner, builtins};
use serde_json::{Value, json};

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/contact-vectors.json")).unwrap()
}

fn texts(value: &Value) -> Vec<String> {
    value
        .as_array()
        .unwrap()
        .iter()
        .map(|text| text.as_str().unwrap().to_string())
        .collect()
}

fn contacts(vectors: &Value) -> Vec<Contact<'_>> {
    vectors["personas"]
        .as_array()
        .unwrap()
        .iter()
        .map(|row| Contact {
            name: row["name"].as_str().unwrap(),
            voice_id: row["voice_id"].as_str().unwrap(),
            tier: row["tier"].as_str().unwrap(),
        })
        .collect()
}

fn wearers(agents: &Value) -> Vec<Wearer<'_>> {
    agents
        .as_array()
        .unwrap()
        .iter()
        .map(|row| Wearer {
            agent_id: row["agent_id"].as_str().unwrap(),
            persona: row["persona"].as_str().unwrap(),
            voice_id: row["voice_id"].as_str().unwrap(),
        })
        .collect()
}

#[test]
fn a_tool_has_the_tier_and_a_contact_the_tool_the_python_host_gives_them() {
    let vectors = vectors();
    for case in vectors["tiers"].as_array().unwrap() {
        let backend = case["backend"].as_str().unwrap();
        assert_eq!(tier_of_tool(backend), case["tier"], "{case}");
    }
    for (tier, tool) in vectors["tier_backends"].as_object().unwrap() {
        assert_eq!(tool_of_tier(tier), tool.as_str(), "{tier}");
    }
    assert_eq!(tool_of_tier("nobody"), None);
    let allowed = vectors["allowed"].as_array().unwrap();
    assert!(allowed.len() >= 80);
    for case in allowed {
        let (name, backend, tier) = (
            case["name"].as_str().unwrap(),
            case["backend"].as_str().unwrap(),
            case["tier"].as_str().unwrap(),
        );
        let (ok, tool) = allows(name, backend, tier);
        assert_eq!(json!([ok, tool]), case["answer"], "{case}");
    }
}

#[test]
fn a_minted_contact_has_the_name_and_the_way_of_speaking_the_python_host_gives_it() {
    let vectors = vectors();
    // The personas there were when names were picked: the built-in ones, which this Host lists
    // in the order the Python Host writes them.
    let built: Vec<String> = builtins(&Owner::default())
        .iter()
        .map(|persona| persona["name"].as_str().unwrap().to_string())
        .collect();
    let listed: Vec<String> = vectors["personas"].as_array().unwrap()[..built.len()]
        .iter()
        .map(|row| row["name"].as_str().unwrap().to_string())
        .collect();
    assert_eq!(
        built, listed,
        "the built-in personas, in the order both Hosts list them"
    );
    let names = vectors["names"].as_array().unwrap();
    assert!(names.len() >= 14);
    for case in names {
        let mut existing = texts(&case["existing"]);
        existing.extend(built.iter().cloned());
        let picked = given_name(
            case["backend"].as_str().unwrap(),
            &texts(&case["occupied"]),
            &existing,
        );
        let (name, tier) = picked.unwrap();
        assert_eq!(
            (json!(name), json!(tier)),
            (case["name"].clone(), case["tier"].clone()),
            "{case}"
        );
    }
    let prompts = vectors["prompts"].as_array().unwrap();
    assert!(prompts.len() >= 13);
    for case in prompts {
        let (name, tier, backend) = (
            case["name"].as_str().unwrap(),
            case["tier"].as_str().unwrap(),
            case["backend"].as_str().unwrap(),
        );
        assert_eq!(
            portrait_prompt(name, tier, backend),
            case["prompt"],
            "{case}"
        );
    }
    for case in vectors["personalities"].as_array().unwrap() {
        let said = personality(
            case["name"].as_str().unwrap(),
            case["tier"].as_str().unwrap(),
        );
        assert_eq!(said, case["personality"], "{case}");
    }
}

#[test]
fn a_tools_pool_is_the_contacts_the_python_host_hands_out() {
    let vectors = vectors();
    let contacts = contacts(&vectors);
    let pools = vectors["pools"].as_array().unwrap();
    assert_eq!(pools.len(), 4);
    for case in pools {
        let agents = wearers(&case["agents"]);
        for (backend, theirs) in case["pools"].as_object().unwrap() {
            let ours = pool(&contacts, &agents, backend, "");
            assert_eq!(json!(ours), *theirs, "{} / {backend}", case["roster"]);
        }
        let ours = pool(&contacts, &agents, "claude", "a2");
        assert_eq!(
            json!(ours),
            case["excluding_a2"],
            "{} excluding a2",
            case["roster"]
        );
    }
}

#[test]
fn an_agent_that_is_made_takes_the_contact_the_python_host_gives_it() {
    let vectors = vectors();
    let launches = vectors["launches"].as_array().unwrap();
    assert!(launches.len() >= 140);
    let mut seen = 0;
    for case in launches {
        // A janitor is not made by `POST /agents` here: its own way gives it its name.
        if case["janitor"] == true {
            continue;
        }
        seen += 1;
        let asked = case["data"].as_object().unwrap();
        let pooled = case["pool"]
            .as_array()
            .unwrap()
            .first()
            .and_then(Value::as_str);
        let minted = case["minted"].as_str();
        let outcome = &case["outcome"];
        let given = asked.get("name").and_then(Value::as_str).unwrap_or("");
        // What the Python Host's parse answers, from what the rule says and the mint gives.
        let ours = match launch(asked, pooled, false) {
            Launch::AsNamed if given.is_empty() => json!({ "refused": [400, "name required", ""] }),
            Launch::AsNamed => {
                let session = asked.get("session").and_then(Value::as_str).unwrap_or("");
                json!({ "name": given, "session": session })
            }
            Launch::Pooled(name) => json!({ "name": name, "session": "" }),
            Launch::Minted => match minted {
                None => json!({ "mint": "claude" }),
                Some("") => json!({ "refused": [409, "contact_pool_empty", ""] }),
                Some(name) => json!({ "name": name, "session": "" }),
            },
            Launch::Refused(status, word) => json!({ "refused": [status, word, ""] }),
        };
        assert_eq!(ours, *outcome, "{case}");
    }
    assert_eq!(seen, 72);
}

#[test]
fn an_agent_is_assigned_the_contact_the_python_host_assigns_it() {
    let vectors = vectors();
    let contacts = contacts(&vectors);
    let assigns = vectors["assigns"].as_array().unwrap();
    assert_eq!(assigns.len(), 68);
    let rosters = vectors["pools"].as_array().unwrap();
    for case in assigns {
        let roster = rosters
            .iter()
            .find(|roster| roster["roster"] == case["roster"])
            .unwrap();
        let agents = wearers(&roster["agents"]);
        let asked = &case["asked"];
        let session = asked["session"].as_str().unwrap();
        let row = roster["agents"]
            .as_array()
            .unwrap()
            .iter()
            .find(|row| row["session"] == session);
        let agent = row.map(|row| Assignee {
            agent_id: row["agent_id"].as_str().unwrap(),
            persona: row["persona"].as_str().unwrap(),
            backend: row["backend"].as_str().unwrap(),
            janitor: row["is_janitor"] == 1,
        });
        let ours = assign(
            asked["mode"].as_str().unwrap(),
            asked.get("name"),
            agent,
            &contacts,
            &agents,
        );
        match ours {
            Err(refused) => {
                let body = json!({ "error": refused.error, "message": refused.message });
                assert_eq!(
                    json!({ "status": refused.status, "body": body }),
                    case["refused"],
                    "{case}"
                );
            }
            Ok(Assigned::Options(names)) => {
                let listed: Vec<Value> = names.iter().map(|name| json!({ "name": name })).collect();
                assert_eq!(json!(listed), case["answer"]["contacts"], "{case}");
            }
            Ok(Assigned::Take(name)) => {
                assert_eq!(json!(name), case["answer"]["name"], "{case}");
                assert_eq!(case["written"][0]["persona"], json!(name), "{case}");
                assert!(
                    case["made"].as_array().unwrap().is_empty()
                        && case["minted"].as_array().unwrap().is_empty()
                );
            }
            Ok(Assigned::Mint) => {
                assert_eq!(case["minted"], json!([row.unwrap()["backend"]]), "{case}");
                assert_eq!(case["answer"]["name"], "Minted", "{case}");
            }
            Ok(Assigned::Create(name)) => {
                assert_eq!(
                    case["made"],
                    json!([{ "name": name, "voice_id": "" }]),
                    "{case}"
                );
                assert_eq!(json!(name), case["answer"]["name"], "{case}");
            }
        }
    }
}

#[test]
fn the_making_of_pictures_is_bounded_by_the_python_hosts_own_numbers() {
    use clarp_kernel::contacts::picture;
    let vectors = vectors();
    let theirs = &vectors["picture_bounds"];
    let ours = json!({
        "daily_limit": picture::DAILY_LIMIT,
        "daily_window_ms": picture::DAILY_WINDOW_MS,
        "max_attempts": picture::MAX_ATTEMPTS,
        "retry_backoff_ms": picture::RETRY_BACKOFF_MS,
        "heartbeat_timeout_ms": picture::HEARTBEAT_TIMEOUT_MS,
        "answer_most": picture::ANSWER_MOST,
        "made_most": picture::MADE_MOST,
        "kept_most": picture::KEPT_MOST,
        "model": picture::MODEL,
    });
    assert_eq!(&ours, theirs);
}

#[test]
fn a_pictures_job_is_begun_as_the_python_host_begins_it() {
    use clarp_kernel::contacts::picture::{Tried, admitted};
    let vectors = vectors();
    let cases = vectors["admissions"].as_array().unwrap();
    assert_eq!(cases.len(), 15);
    for case in cases {
        let before = &case["before"];
        let tried = before.as_object().map(|_| Tried {
            status: before["status"].as_str().unwrap(),
            generation: before["generation"].as_u64().unwrap(),
            ended_ago_ms: before["ended_ago_ms"].as_i64(),
        });
        let begun_today = u32::try_from(case["begun_today"].as_u64().unwrap()).unwrap();
        assert_eq!(
            json!(admitted(tried, begun_today)),
            case["generation"],
            "{case}"
        );
    }
}

#[test]
fn a_picture_kept_for_a_contact_changes_its_picture_and_nothing_else() {
    use clarp_kernel::personas::{Book, Made};
    // A contact kept with no voice, no symbol and its own words.
    let made = Made {
        persona_id: "persona-made-up".to_string(),
        name: "Kit".to_string(),
        voice_id: String::new(),
        avatar_symbol: String::new(),
        avatar: None,
        personality: "Personality: made up.".to_string(),
        created_at_ms: 1,
    };
    let mut book = Book::new(vec![made.clone()], Vec::new());
    let owner = Owner::default();
    let kept = book
        .set_picture(&owner, "persona-made-up", "d1gest")
        .unwrap();
    assert_eq!(
        kept,
        Made {
            avatar: Some("d1gest".to_string()),
            ..made
        }
    );
    assert_eq!(
        book.picture_of(&owner, "persona-made-up"),
        Some("d1gest".to_string())
    );
    let listed = book.named(&owner, "Kit").unwrap();
    assert_eq!(
        listed["voice_id"], "",
        "a contact without a voice still has none: {listed}"
    );
    // Only a persona of the owner's is given a picture so; a built-in one is not.
    assert!(book.set_picture(&owner, "builtin-mike", "d1gest").is_none());
    assert!(
        book.set_picture(&owner, "persona-nobody", "d1gest")
            .is_none()
    );
}
