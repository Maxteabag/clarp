//! `--check turn-errors`, BEFORE: the contract fixture's failure rows as the
//! desktop drew them before the Host explained them (screenshots only).

use std::time::Duration;

use serde_json::{Value, json};

use super::{Stage, check, control, run_stages, shot};

const SESSION: &str = "rachel";

fn rows_through(last: &str) -> Vec<Value> {
    let fixture: Value = serde_json::from_str(include_str!("../../tests/fixtures/turn-errors.json")).expect("the fixture is JSON");
    let rows = fixture["log_rows"].as_array().cloned().unwrap_or_default();
    let end = rows.iter().position(|r| r["id"] == last).map_or(rows.len(), |i| i + 1);
    rows[..end].to_vec()
}

fn shown(id: &str) -> bool {
    crate::window().is_some_and(|w| super::rows(&w).iter().any(|r| r.id == id))
}

pub fn turn_errors_check(out: String) {
    let (o1, o2) = (out.clone(), out.clone());
    let stages: Vec<Stage> = vec![
        ("ready", Box::new(|app, _, _| {
            if app.engine.borrow().selected_session() != SESSION {
                app.engine.borrow_mut().select(SESSION);
            }
            app.engine.borrow().conversation(SESSION).is_some_and(|c| !c.rows().is_empty())
        })),
        ("three failures", Box::new(|_, _, _| {
            check(control("/__control/turns", &json!({"session": SESSION, "turns": rows_through("marker-u3")})).is_ok(), "the first three failures");
            true
        })),
        ("shot three", Box::new(move |_, _, elapsed| {
            if (!shown("marker-u3") && elapsed < Duration::from_secs(8)) || elapsed < Duration::from_millis(800) {
                return false;
            }
            shot(&o1, "before-01-three-failures");
            check(control("/__control/turns", &json!({"session": SESSION, "turns": rows_through("marker-u6")})).is_ok(), "the whole fixture");
            true
        })),
        ("shot all", Box::new(move |_, _, elapsed| {
            if (!shown("marker-u6") && elapsed < Duration::from_secs(8)) || elapsed < Duration::from_millis(800) {
                return false;
            }
            shot(&o2, "before-02-usage-auth-too-long");
            true
        })),
    ];
    run_stages(stages);
}
