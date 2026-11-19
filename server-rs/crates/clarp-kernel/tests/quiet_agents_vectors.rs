//! Which agents are quiet, held to the Python Host's one definition (`tests/fixtures/quiet-agents-vectors.json`,
//! written by `server-rs/tools/quiet-agents-vectors.py` from `lib.quiet_agents.is_quiet` over agent rows of every
//! shape). A record here always has `is_janitor`; a row there may lack it or have it empty, which is no janitor.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_kernel::roster;
use serde_json::Value;

#[test]
fn an_agent_is_quiet_exactly_when_the_python_host_says_so() {
    let vectors: Value =
        serde_json::from_str(include_str!("fixtures/quiet-agents-vectors.json")).unwrap();
    let rows = vectors["rows"].as_array().unwrap();
    for case in rows {
        let row = &case["row"];
        // The column as a record keeps it: a janitor where it says one, nothing else.
        let is_janitor = match &row["is_janitor"] {
            Value::Number(number) => number.as_i64() != Some(0),
            Value::Bool(yes) => *yes,
            _ => false,
        };
        let theirs = case["quiet"].as_bool().unwrap();
        assert_eq!(roster::quiet(is_janitor), theirs, "{}", case["what"]);
    }
    assert_eq!(rows.len(), 8);
}
