//! The names a provider of the router of spoken messages is known by, held to the Python Host's
//! (`tests/fixtures/router-tools-vectors.json`, written by `server-rs/tools/router-tools-vectors.py` from its
//! own `normalize_provider`): a tool by its own name or another it goes by, the two that are no tool, and what
//! was said, in small letters and without blanks at its ends, for whoever asked to refuse. Which of them this
//! Host asks is the Host's (`clarp-host` `router.rs`): its tools, and not the two that are no tool.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_kernel::router;
use serde_json::Value;

#[test]
fn a_provider_is_known_by_the_names_the_python_host_knows_it_by() {
    let vectors: Value =
        serde_json::from_str(include_str!("fixtures/router-tools-vectors.json")).unwrap();
    let names = vectors["names"].as_array().unwrap();
    for case in names {
        let name = case["name"].as_str().unwrap();
        assert_eq!(
            router::provider(Some(name)),
            case["provider"].as_str().unwrap(),
            "{name:?}"
        );
    }
    assert_eq!(router::provider(None), "");
    assert_eq!(names.len(), 33);
}
