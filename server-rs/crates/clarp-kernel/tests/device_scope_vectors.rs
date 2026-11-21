//! What a device a user paired with limited access is refused, against the Python Host's own
//! rule.
//!
//! `fixtures/device-scope-vectors.json` is written by `server-rs/tools/device-scope-vectors.py`
//! from `Handler._device_forbidden` (server/server.py), as the handlers of each method ask it,
//! for every prefix of its lists, the routes it lets through, the paths next to them and every
//! method. A device with full access, and a caller that is no device, are refused none of it.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_kernel::devices;
use serde_json::Value;

#[test]
fn a_limited_device_is_refused_what_the_python_host_refuses_it() {
    let vectors: Value =
        serde_json::from_str(include_str!("fixtures/device-scope-vectors.json")).unwrap();
    let (mut refused, mut let_through, mut others) = (0, 0, 0);
    for case in vectors["cases"].as_array().unwrap() {
        let (method, path) = (
            case["method"].as_str().unwrap(),
            case["path"].as_str().unwrap(),
        );
        let theirs = case["refused"].as_bool().unwrap();
        if case["scope"] != "limited" {
            assert!(
                !theirs,
                "{method} {path} is refused there to a scope that is not limited"
            );
            others += 1;
            continue;
        }
        assert_eq!(
            devices::limited_refused(method, path),
            theirs,
            "{method} {path}"
        );
        if theirs {
            refused += 1;
        } else {
            let_through += 1;
        }
    }
    assert_eq!((refused, let_through, others), (661, 319, 1960));
    assert_eq!(devices::LIMITED_REFUSAL, "full device access required");
}
