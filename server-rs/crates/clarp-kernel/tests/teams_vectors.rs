//! The Python Host's team hierarchy and update rules.
#![allow(clippy::unwrap_used)]

use clarp_kernel::teams::{Change, Team, Teams};
use serde_json::Value;

#[test]
fn teams_follow_the_reference_hierarchy_and_update_rules() {
    let rows: Vec<Value> =
        serde_json::from_str(include_str!("fixtures/teams-vectors.json")).unwrap();
    let mut teams = Teams::new();
    for row in &rows {
        let id = row["id"].as_str().unwrap();
        let body = row["body"].as_object().unwrap();
        let now = row["now"].as_i64().unwrap();
        let result = if row["operation"] == "make" {
            Team::create(&teams, id.to_owned(), body, now)
        } else {
            Change::read(body).and_then(|change| teams[id].update(&teams, change, now))
        };
        match result {
            Ok(team) => {
                let mut actual = serde_json::to_value(&team).unwrap();
                actual.as_object_mut().unwrap().remove("members");
                assert_eq!(actual, row["team"], "{row}");
                teams.insert(id.to_owned(), team);
            }
            Err(error) => assert_eq!(error, row["error"], "{row}"),
        }
    }
    assert_eq!(rows.len(), 16);
}
