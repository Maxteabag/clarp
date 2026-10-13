//! The real Host's summary-v1 responses (`tests/fixtures/artifact_summary_v1_host.json`,
//! captured from feat/artifact-summary-v1 @ ee0fb54b) read the way the desktop
//! reads them. A Host change that moves a field, a name or a type fails here.

use clarp_core::artifact_summary::{self as summary, Check};
use serde_json::Value;

fn fixture() -> Value {
    let path = concat!(env!("CARGO_MANIFEST_DIR"), "/../tests/fixtures/artifact_summary_v1_host.json");
    serde_json::from_str(&std::fs::read_to_string(path).expect("the fixture")).expect("valid JSON")
}

fn rows<'a>(f: &'a Value, capture: &str) -> &'a Vec<Value> {
    f[capture]["body"]["artifacts"].as_array().unwrap_or_else(|| panic!("{capture} lists artifacts"))
}

fn id<'a>(f: &'a Value, name: &str) -> &'a str {
    f["ids"][name].as_str().unwrap()
}

fn row<'a>(f: &'a Value, capture: &str, name: &str) -> &'a Value {
    rows(f, capture).iter().find(|r| r["artifact_id"] == id(f, name)).unwrap_or_else(|| panic!("{name} in {capture}"))
}

fn item<'a>(f: &'a Value, name: &str, shape: &str) -> &'a Value {
    &f["items"][name][shape]["body"]["artifact"]
}

/// The row without what summary-v1 leaves out and its markers.
fn comparable(row: &Value) -> Value {
    let mut row = row.clone();
    let object = row.as_object_mut().unwrap();
    object.remove("content");
    object.remove("content_omitted");
    if let Some(goal) = object.get_mut("plan").and_then(|p| p.get_mut("goal")).and_then(Value::as_object_mut) {
        goal.remove("history");
        goal.remove("history_omitted");
    }
    row
}

#[test]
fn the_feature_is_advertised_as_the_desktop_reads_it() {
    let f = fixture();
    assert_eq!(f["server_info"]["status"], 200);
    assert!(summary::advertised(f["server_info"]["body"].as_object().unwrap()));
}

#[test]
fn the_old_value_refusals_are_exact() {
    let f = fixture();
    for capture in ["refused", "pinned_refused"] {
        let error = f[capture]["body"]["error"].as_str().unwrap();
        assert!(summary::is_unsupported_representation(f[capture]["status"].as_u64().unwrap() as u16, error), "{capture}: {error}");
    }
    // The inbox says something else: it is gated on the feature alone.
    assert_eq!((f["inbox_refused"]["status"].as_u64(), f["inbox_refused"]["body"]["error"].as_str()), (Some(400), Some("invalid attention query")));
    assert!(!summary::is_unsupported_representation(400, "invalid attention query"));
    assert_eq!((f["missing"]["status"].as_u64(), f["missing"]["body"]["error"].as_str()), (Some(404), Some("artifact not found")));
}

#[test]
fn summary_rows_are_flat_rows_less_the_bodies() {
    let f = fixture();
    for (summary_capture, flat_capture) in [("summary", "flat"), ("pinned_summary", "pinned_flat")] {
        let (summary_rows, flat_rows) = (rows(&f, summary_capture), rows(&f, flat_capture));
        let ids = |rows: &Vec<Value>| rows.iter().map(|r| r["artifact_id"].clone()).collect::<Vec<_>>();
        assert_eq!(ids(summary_rows), ids(flat_rows), "{summary_capture}: the same rows, in order");
        for (s, flat) in summary_rows.iter().zip(flat_rows) {
            assert_eq!(comparable(s), comparable(flat), "{summary_capture}: {}", s["artifact_id"]);
            let html_form = s["type"] == "html_form";
            assert_eq!(s.get("content").is_none(), html_form, "only html_form content is left out: {}", s["type"]);
            assert_eq!(summary::complete(s), !html_form && summary::history_marker(s).is_none());
            assert!(summary::complete(flat));
        }
    }
    assert_eq!(row(&f, "summary", "decision")["decision"], row(&f, "flat", "decision")["decision"]);
    assert_eq!(row(&f, "summary", "question")["decision"], row(&f, "flat", "question")["decision"]);
}

#[test]
fn a_content_marker_verifies_on_both_item_shapes() {
    let f = fixture();
    let marker = summary::content_marker(row(&f, "summary", "small")).expect("a marker");
    assert!(row(&f, "summary", "small")["content_omitted"]["sha256"].as_str().unwrap().starts_with("c1:"));
    for shape in ["flat", "legacy", "other"] {
        let content = item(&f, "small", shape)["content"].as_str().unwrap();
        assert_eq!(summary::check_content(marker.sha256.as_deref(), content), Check::Match, "{shape}");
        assert_eq!(marker.bytes, Some(content.len() as u64));
        assert_eq!(marker.updated_at, item(&f, "small", shape)["updated_at"].as_i64().unwrap());
        assert_eq!(marker.version, item(&f, "small", shape)["version"]);
    }
    assert!(item(&f, "small", "flat").get("payload").is_none(), "flat-v1 drops only payload");
    assert!(item(&f, "small", "legacy").get("payload").is_some() && item(&f, "small", "other").get("payload").is_some(), "another value is the legacy item");
    let big = summary::content_marker(row(&f, "summary", "big116")).unwrap();
    assert_eq!(f["big116_content_c1"].as_str(), row(&f, "summary", "big116")["content_omitted"]["sha256"].as_str());
    assert!(big.bytes.is_some_and(|b| b > 1_100_000));
    let (flat, legacy) = (f["items"]["big116"]["flat"]["bytes"].as_u64().unwrap(), f["items"]["big116"]["legacy"]["bytes"].as_u64().unwrap());
    assert!(flat * 19 < legacy * 10, "flat-v1 about halves the hydration: {flat} vs {legacy}");
}

#[test]
fn a_history_marker_is_opaque_and_counts_the_hydrated_history() {
    let f = fixture();
    let marker = summary::history_marker(row(&f, "summary", "plan_artifact")).expect("a marker");
    let digest = marker.digest.as_deref().unwrap();
    assert!(digest.starts_with("h1:"), "{digest}");
    for shape in ["flat", "legacy"] {
        let history = summary::goal_history(item(&f, "plan_artifact", shape)).and_then(Value::as_array).unwrap();
        assert_eq!(marker.count, history.len() as u64);
        assert_eq!(marker.last_at, history.last().and_then(|h| h["at"].as_i64()));
    }
}

#[test]
fn over_budget_and_lone_surrogates_take_the_pending_paths() {
    let f = fixture();
    for row in rows(&f, "summary_pending") {
        if row["type"] == "html_form" {
            let m = &row["content_omitted"];
            assert_eq!((m["sha256"].is_null(), m["hash_pending"].as_bool(), m["bytes"].is_null(), m["size_unknown"].as_bool()), (true, Some(true), true, Some(true)), "{row}");
            assert_eq!(summary::content_marker(row).unwrap().sha256, None);
        }
        if row["type"] == "plan" {
            let m = &row["plan"]["goal"]["history_omitted"];
            assert_eq!((m["digest"].is_null(), m["digest_pending"].as_bool()), (true, Some(true)));
            assert_eq!(summary::history_marker(row).unwrap().digest, None);
        }
    }
    let m = &row(&f, "summary", "surrogate")["content_omitted"];
    assert_eq!((m["sha256"].is_null(), m["hash_pending"].as_bool(), m["bytes"].is_null(), m["size_unknown"].as_bool()), (true, Some(true), true, Some(true)));
    // The item itself cannot be read: a lone surrogate is no JSON string
    // serde_json accepts (the summary list is unaffected, its body is out).
    let raw = f["surrogate_item_flat_raw"].as_str().unwrap();
    assert!(raw.contains("\\ud800"));
    let error = serde_json::from_str::<Value>(raw).expect_err("a lone surrogate");
    assert!(error.to_string().contains("lone leading surrogate"), "{error}");
}
