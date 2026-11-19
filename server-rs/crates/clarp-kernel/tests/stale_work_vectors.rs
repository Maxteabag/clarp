//! When a work label has outlived the work behind it: the Python Host's three rules
//! (`tools/stale-work-vectors.py`, its `lib/policies/stale_work.py`), over their edges.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_kernel::helpers::{self, Doing};
use clarp_kernel::jobs::{self, Verdict};
use serde_json::Value;

fn doing(case: &Value, idle_since_ms: i64) -> Doing {
    Doing {
        busy: case["busy"].as_bool().unwrap(),
        active_jobs: usize::try_from(case["active_jobs"].as_u64().unwrap()).unwrap(),
        running_children: usize::try_from(case["running_children"].as_u64().unwrap()).unwrap(),
        idle_since_ms,
    }
}

#[test]
fn a_label_that_outlived_its_work_is_retired_as_the_python_host_retires_it() {
    let said: Value =
        serde_json::from_str(include_str!("fixtures/stale-work-vectors.json")).unwrap();
    // The thresholds this Host runs with are the reference's own (the Host's job thresholds
    // are 15 and 30 minutes: crates/clarp-host/src/agent.rs).
    let defaults = &said["defaults"];
    assert_eq!(defaults["job_stale_after_ms"], 15 * 60_000);
    assert_eq!(defaults["job_grace_ms"], 15 * 60_000);
    assert_eq!(defaults["helper_idle_after_ms"], helpers::IDLE_AFTER_MS);
    assert_eq!(defaults["custom_status_ttl_ms"], helpers::STATUS_TTL_MS);
    let mut counted = [0usize; 3];
    for case in said["jobs"].as_array().unwrap() {
        let ours = jobs::heartbeat_verdict(
            case["age_ms"].as_i64().unwrap(),
            case["verified"].as_bool().unwrap(),
            case["stale_ms"].as_i64().unwrap(),
            case["grace_ms"].as_i64().unwrap(),
        );
        let theirs = match case["verdict"].as_str().unwrap() {
            "keep" => Verdict::Keep,
            "stale" => Verdict::Stale,
            "lost" => Verdict::Lost,
            other => panic!("{other}"),
        };
        assert_eq!(ours, theirs, "{case}");
        counted[0] += 1;
    }
    for case in said["helpers"].as_array().unwrap() {
        let ours = helpers::idle_due(
            case["state"].as_str(),
            &doing(case, case["idle_since_ms"].as_i64().unwrap()),
            case["now_ms"].as_i64().unwrap(),
            case["limit_ms"].as_i64().unwrap(),
        );
        assert_eq!(ours, case["due"].as_bool().unwrap(), "{case}");
        counted[1] += 1;
    }
    for case in said["statuses"].as_array().unwrap() {
        let ours = helpers::status_due(
            case["status"].as_str().unwrap(),
            case["status_at"].as_i64(),
            case["janitor_owned"].as_bool().unwrap(),
            &doing(case, 0),
            case["now_ms"].as_i64().unwrap(),
            case["ttl_ms"].as_i64().unwrap(),
        );
        assert_eq!(ours, case["due"].as_bool().unwrap(), "{case}");
        counted[2] += 1;
    }
    assert_eq!(counted, [126, 560, 1792]);
}
