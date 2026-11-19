#![allow(clippy::unwrap_used)]
use clarp_kernel::janitors::label_audit::{Evidence, findings, report};
use serde_json::{Value, json};
#[test]
fn the_reference_decides_which_labels_to_report() {
    let rows: Vec<Value> =
        serde_json::from_str(include_str!("fixtures/label-verdicts.json")).unwrap();
    for row in &rows {
        let evidence: Vec<Evidence> = serde_json::from_value(row["evidence"].clone()).unwrap();
        let found = findings(&evidence, &row["answers"]).unwrap();
        assert_eq!(json!(found), row["found"]);
        let (mark, doc) = report(
            "checker",
            "test-run",
            evidence.len(),
            &found,
            row["previous"].as_str().unwrap(),
        );
        assert_eq!(mark, row["fingerprint"]);
        assert_eq!(json!(doc), row["document"]);
        if !evidence.is_empty() {
            assert!(findings(&evidence, &json!({})).is_none());
        }
    }
    assert_eq!(rows.len(), 138);
}
