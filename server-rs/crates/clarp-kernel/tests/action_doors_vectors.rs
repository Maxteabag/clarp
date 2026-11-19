//! Which changes under four doors the Python Host has a route for: its own dispatcher's
//! answers (`tools/action-doors-vectors.py`), 480 `POST` paths.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_kernel::doors;
use serde_json::Value;

#[test]
fn a_change_under_the_four_doors_is_a_route_where_the_python_host_registers_it() {
    let said: Value =
        serde_json::from_str(include_str!("fixtures/action-doors-vectors.json")).unwrap();
    let (mut routes, mut none) = (0, 0);
    for case in said["cases"].as_array().unwrap() {
        let path = case["path"].as_str().unwrap();
        let routed = !case["route"].is_null();
        assert_eq!(doors::registered(path), routed, "{path}");
        if routed {
            routes += 1;
        } else {
            assert_eq!(case["status"], 404, "{path}");
            none += 1;
        }
    }
    assert_eq!((routes, none), (22, 458));
    // Every path of the list was asked, and found.
    for path in doors::REGISTERED {
        assert!(
            said["cases"]
                .as_array()
                .unwrap()
                .iter()
                .any(|case| case["path"] == path && !case["route"].is_null()),
            "{path}"
        );
    }
}
