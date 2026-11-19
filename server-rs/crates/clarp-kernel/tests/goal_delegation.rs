#![allow(clippy::unwrap_used)]
use clarp_kernel::goals::delegation::{self, Participant};
use serde_json::Value;

fn participant(row: &Value) -> Participant<'_> {
    Participant {
        id: row["agent_id"].as_str().unwrap(),
        parent: row["parent_agent_id"].as_str(),
        role: row["role"].as_str().unwrap(),
        backend: row["backend"].as_str().unwrap(),
        archived_or_deleted: row["archived_at"].is_i64() || row["deleted_at"].is_i64(),
    }
}

#[test]
fn enabling_bookkeeping_uses_the_reference_checks_in_order() {
    let fixture: Value =
        serde_json::from_str(include_str!("fixtures/goal-delegation.json")).unwrap();
    for case in fixture["enable"].as_array().unwrap() {
        let n = &case["baseline"];
        let baseline = n
            .as_i64()
            .or_else(|| n.as_bool().map(i64::from))
            .unwrap_or(-1);
        let got = delegation::enable(
            &participant(&case["principal"]),
            &participant(&case["helper"]),
            case["reason"].as_str().unwrap(),
            baseline,
        );
        if let Some(error) = case["error"].as_str() {
            assert_eq!(got.unwrap_err(), error, "{case}");
        } else {
            assert_eq!(
                got.unwrap(),
                case["reason_stored"].as_str().unwrap(),
                "{case}"
            );
        }
    }
}

#[test]
fn guarded_goal_writes_have_the_authority_the_reference_grants() {
    let fixture: Value =
        serde_json::from_str(include_str!("fixtures/goal-delegation.json")).unwrap();
    for case in fixture["guards"].as_array().unwrap() {
        let answer = delegation::guard(
            case["kind"].as_str().unwrap(),
            case["verified"].as_bool().unwrap(),
            case["descendant"].as_bool().unwrap(),
            case["backend"].as_str(),
        );
        assert_eq!(answer.err(), case["error"].as_str(), "{case}");
    }
}
