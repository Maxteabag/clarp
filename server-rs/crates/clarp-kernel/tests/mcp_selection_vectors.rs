#![allow(clippy::unwrap_used)]

use serde_json::{Value, json};

#[test]
fn mcp_selection_matches_the_python_agent_creation_policy() {
    let vectors: Value =
        serde_json::from_str(include_str!("fixtures/mcp-selection-vectors.json")).unwrap();
    let catalog = vectors["catalog"].as_object().unwrap();
    let cases = vectors["cases"].as_array().unwrap();
    for case in cases {
        let asked = &case["asked"];
        let selected = clarp_kernel::mcp::selection(asked.get("mcp_servers"))
            .map_err(|why| (why, why.to_string()))
            .and_then(|selected| {
                clarp_kernel::mcp::check(
                    selected.as_deref().unwrap_or_default(),
                    case["supports_mcp"].as_bool().unwrap(),
                    case["label"].as_str().unwrap(),
                    catalog,
                )?;
                Ok(selected)
            });
        let result = match selected {
            Ok(names) => json!({ "selection": names }),
            Err((code, message)) => json!({ "error": code, "message": message }),
        };
        assert_eq!(result, case["result"], "{asked}");
    }
    assert_eq!(cases.len(), 17);
}
