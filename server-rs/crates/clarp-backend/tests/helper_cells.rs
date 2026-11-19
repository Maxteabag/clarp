#![allow(clippy::unwrap_used)]
use clarp_backend::history::claude_with_children;
use clarp_kernel::helper_cells::Children;
use serde_json::Value;
#[test]
fn claude_helpers_match_the_parent_transcripts_recorded_by_the_reference() {
    let cases: Vec<Value> =
        serde_json::from_str(include_str!("fixtures/helper-cells.json")).unwrap();
    for case in &cases {
        let mut children = Children::new();
        for pair in case["children"].as_array().unwrap() {
            if let Some(path) = pair[1].as_str() {
                children.insert(pair[0].as_str().unwrap().into(), path.into());
            }
        }
        let transcript = case["records"]
            .as_array()
            .unwrap()
            .iter()
            .map(ToString::to_string)
            .collect::<Vec<_>>()
            .join("\n");
        assert_eq!(
            serde_json::json!(claude_with_children(&transcript, &children)),
            case["expected"],
            "{}",
            case["title"]
        );
    }
    assert_eq!(cases.len(), 229);
}
