//! The janitors' rules, held to what the Python Host's own modules made of the same things
//! (`tests/fixtures/janitor-vectors.json`, written by `server-rs/tools/janitor-vectors.py`).

#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_kernel::janitors::{self, Kept, Named, Refused};
use serde_json::{Map, Value, json};

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/janitor-vectors.json")).unwrap()
}

/// The time zones the vectors name that a machine has.
const ZONES: [&str; 4] = [
    "UTC",
    "Europe/Oslo",
    "America/Argentina/Buenos_Aires",
    "Etc/GMT+5",
];

/// Whoever keeps attachments, for rules that are asked about nothing that is kept.
struct Nothing;

impl Kept for Nothing {
    fn attachment_free(&self, _attachment_id: &str, _agent_id: &str) -> bool {
        true
    }

    fn zone(&self, name: &str) -> bool {
        ZONES.contains(&name)
    }

    fn draw(&mut self) -> String {
        "<drawn>".to_string()
    }
}

fn told(refused: &Refused) -> Value {
    json!([refused.status, refused.code, refused.message])
}

/// What the Python Host made of it: taken, refused, or broken on (an error of its own, which
/// this Host answers by refusing).
fn held(row: &Value, ours: Result<Value, Refused>, what: &str) -> (usize, usize) {
    match (ours, row.get("taken"), row.get("refused"), row.get("broke")) {
        (Ok(ours), Some(theirs), _, _) => {
            assert_eq!(&ours, theirs, "{what}");
            (1, 0)
        }
        (Err(ours), _, Some(theirs), _) => {
            assert_eq!(&told(&ours), theirs, "{what}");
            (0, 1)
        }
        (Err(ours), _, _, Some(_)) => {
            assert_eq!(ours.status, 400, "{what}");
            (0, 1)
        }
        (ours, theirs, refused, broke) => {
            panic!("{what}: here {ours:?}, there {theirs:?} {refused:?} {broke:?}")
        }
    }
}

#[test]
fn the_catalogue_is_the_python_hosts() {
    let vectors = vectors();
    assert_eq!(json!(janitors::templates()), vectors["templates"]);
    assert_eq!(json!(janitors::triggers()), vectors["triggers"]);
    assert_eq!(json!(janitors::turn_templates()), vectors["turn_templates"]);
    assert_eq!(json!(janitors::roles()), vectors["roles"]);
    assert_eq!(janitors::templates().len(), 10);
    assert_eq!(janitors::triggers().len(), 11);
    for (template, file) in vectors["portraits"].as_object().unwrap() {
        assert_eq!(janitors::portrait(template), file.as_str().unwrap());
    }
    assert_eq!(janitors::portrait("goal-keeper"), "janitor-default.jpg");
    // Each of the Host's own janitors has the id the Python Host gave it.
    for janitor in vectors["first_day"].as_array().unwrap() {
        let role = janitor["builtin_role"].as_str().unwrap();
        assert_eq!(
            janitors::own_id(role),
            janitor["agent_id"].as_str(),
            "{role}"
        );
    }
    assert_eq!(janitors::own_id("task-labels"), None);
}

#[test]
fn options_are_taken_or_refused_in_the_same_words() {
    let vectors = vectors();
    let rows = vectors["options"].as_array().unwrap();
    let (mut taken, mut refused) = (0, 0);
    for row in rows {
        let template = row["template"].as_str().unwrap();
        let kept = row["kept"].as_object();
        let what = format!("{template} {} over {}", row["given"], row["kept"]);
        let patched = janitors::option_patch(template, &row["given"], kept).map(Value::Object);
        let (yes, no) = held(&row["patched"], patched, &what);
        (taken, refused) = (taken + yes, refused + no);
        let shown = janitors::option_values(template, Some(&row["kept"])).map(Value::Object);
        held(&row["shown"], shown, &format!("{what}, as shown"));
    }
    assert_eq!((taken, refused), (28, 47), "of {} cases", rows.len());
}

#[test]
fn attachments_are_taken_or_refused_in_the_same_words() {
    let vectors = vectors();
    let rows = vectors["attachments"].as_array().unwrap();
    let (mut taken, mut refused) = (0, 0);
    for row in rows {
        let template = row["template"].as_str().unwrap();
        let what = format!("{template} {}", row["given"]);
        let ours = janitors::attachment_values(&row["given"], "id-worker", template, &mut Nothing);
        let ours = ours.map(|all| {
            json!(
                all.iter()
                    .map(janitors::Attachment::shown)
                    .collect::<Vec<_>>()
            )
        });
        let (yes, no) = held(row, ours, &what);
        (taken, refused) = (taken + yes, refused + no);
    }
    assert_eq!((taken, refused), (17, 49), "of {} cases", rows.len());
}

#[test]
fn an_attachment_another_has_is_not_taken() {
    struct Taken;
    impl Kept for Taken {
        fn attachment_free(&self, attachment_id: &str, agent_id: &str) -> bool {
            attachment_id != "anothers" || agent_id == "its-own"
        }
        fn zone(&self, _name: &str) -> bool {
            true
        }
        fn draw(&mut self) -> String {
            "drawn".to_string()
        }
    }
    let asked = json!([{ "attachment_id": "anothers", "trigger_id": "schedule" }]);
    let refused = janitors::attachment_values(&asked, "id-worker", "task-labels", &mut Taken);
    assert_eq!(
        told(&refused.unwrap_err()),
        json!([
            409,
            "attachment_conflict",
            "Trigger attachment belongs to another configuration"
        ])
    );
    let own = janitors::attachment_values(&asked, "its-own", "task-labels", &mut Taken).unwrap();
    assert_eq!(own[0].attachment_id, "anothers");
}

#[test]
fn a_scope_is_taken_or_refused_in_the_same_words() {
    let vectors = vectors();
    // The agents of the made-up Host, and its own janitors.
    let mut known: Map<String, Value> = Map::new();
    for agent in vectors["agents"].as_array().unwrap() {
        known.insert(
            agent["agent_id"].as_str().unwrap().to_string(),
            json!(false),
        );
    }
    let mut explainer = String::new();
    for janitor in vectors["first_day"].as_array().unwrap() {
        let id = janitor["agent_id"].as_str().unwrap();
        known.insert(id.to_string(), json!(true));
        if janitor["builtin_role"] == "tool-explainer" {
            explainer = id.to_string();
        }
    }
    let agent = |id: &str| {
        known.get(id).map(|is_janitor| Named {
            is_janitor: is_janitor == &json!(true),
        })
    };
    // `{worker}` stands for that agent's id, `{janitor}` for one that is a janitor.
    let filled = |given: &Value| -> Value {
        let mut text = given.to_string().replace("{janitor}", &explainer);
        for agent in vectors["agents"].as_array().unwrap() {
            let session = agent["session"].as_str().unwrap();
            text = text.replace(&format!("{{{session}}}"), &format!("id-{session}"));
        }
        serde_json::from_str(&text).unwrap()
    };
    // The Python Host sorts by the ids it drew; told here in the order of their names.
    let sorted = |scope: &Value| -> Value {
        let side = |key: &str| {
            let mut ids: Vec<String> = scope[key]
                .as_array()
                .unwrap()
                .iter()
                .map(|id| id.as_str().unwrap().to_string())
                .collect();
            ids.sort();
            ids
        };
        json!({ "agent_ids": side("agent_ids"), "exclude_agent_ids": side("exclude_agent_ids") })
    };
    let rows = vectors["scopes"].as_array().unwrap();
    let (mut taken, mut refused) = (0, 0);
    for row in rows {
        let ours = janitors::scope(&filled(&row["given"]), &agent).map(|scope| scope.shown());
        let mut theirs = row.clone();
        if let Some(scope) = row.get("taken") {
            theirs["taken"] = sorted(scope);
        }
        let (yes, no) = held(
            &theirs,
            ours.map(|scope| sorted(&scope)),
            &row["given"].to_string(),
        );
        (taken, refused) = (taken + yes, refused + no);
    }
    assert_eq!((taken, refused), (8, 16), "of {} cases", rows.len());
}

#[test]
fn how_a_job_is_run_is_taken_or_refused_in_the_same_words() {
    let vectors = vectors();
    let rows = vectors["executions"].as_array().unwrap();
    let (mut taken, mut refused) = (0, 0);
    for row in rows {
        let (template, backend) = (
            row["template"].as_str().unwrap(),
            row["backend"].as_str().unwrap(),
        );
        let what = format!("{template} on {backend}: {}", row["given"]);
        let ours = janitors::execution(template, Some(&row["given"]), backend).map(Value::Object);
        let (yes, no) = held(row, ours, &what);
        (taken, refused) = (taken + yes, refused + no);
    }
    assert_eq!(taken + refused, rows.len());
    assert_eq!((taken, refused), (69, 135), "of {} cases", rows.len());
}

#[test]
fn two_scopes_have_an_agent_in_common_or_not() {
    let scope = |ids: &[&str], out: &[&str]| janitors::Scope {
        agent_ids: ids.iter().map(|id| id.to_string()).collect(),
        exclude_agent_ids: out.iter().map(|id| id.to_string()).collect(),
    };
    // Both watch all: also the agents that are made later.
    assert!(scope(&[], &[]).overlaps(&scope(&[], &["a"])));
    assert!(scope(&["a"], &[]).overlaps(&scope(&[], &[])));
    assert!(!scope(&["a"], &[]).overlaps(&scope(&[], &["a"])));
    assert!(scope(&["a", "b"], &[]).overlaps(&scope(&["b"], &[])));
    assert!(!scope(&["a"], &[]).overlaps(&scope(&["b"], &[])));
    assert!(!scope(&["a", "b"], &["b"]).overlaps(&scope(&["b"], &[])));
}
