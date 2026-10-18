//! The canonical summary-v1 fixtures (`tests/fixtures/summary-v1/`, Dagger's
//! one synthetic snapshot of Host dfc489bb, shared with iOS) read the way the
//! desktop reads them. Every file is checked against the manifest's sha256
//! before use; a Host change that moves a field, a name or a type fails here.

use clarp_core::artifact_summary::{self as summary, Check};
use serde_json::Value;
use sha2::{Digest, Sha256};

const HOST_SHA: &str = "dfc489bba1d8ae6a47a3037eb352aa8a043ae228";

fn dir() -> std::path::PathBuf {
    std::path::Path::new(concat!(env!("CARGO_MANIFEST_DIR"), "/../tests/fixtures/summary-v1")).to_owned()
}

/// The v1 manifest (its seed's ids).
fn manifest() -> Value {
    serde_json::from_slice(&std::fs::read(dir().join("manifest.json")).expect("the manifest")).expect("valid JSON")
}

/// The v2 manifest: every file's sha256, v1's (and its manifest) included.
fn manifest_v2() -> Value {
    serde_json::from_slice(&std::fs::read(dir().join("manifest-v2.json")).expect("the v2 manifest")).expect("valid JSON")
}

/// One fixture file's JSON, after checking its bytes against the manifest.
fn file(name: &str) -> Value {
    let bytes = std::fs::read(dir().join(name)).unwrap_or_else(|e| panic!("{name}: {e}"));
    let manifest = manifest_v2();
    let entry = manifest["files"].as_array().unwrap().iter().find(|f| f["file"] == name).unwrap_or_else(|| panic!("{name} is not in the manifest"));
    let sha: String = Sha256::digest(&bytes).iter().map(|b| format!("{b:02x}")).collect();
    assert_eq!(entry["sha256"].as_str(), Some(sha.as_str()), "{name} does not match the manifest");
    serde_json::from_slice(&bytes).unwrap_or_else(|e| panic!("{name}: {e}"))
}

fn rows(name: &str) -> Vec<Value> {
    file(name)["artifacts"].as_array().unwrap_or_else(|| panic!("{name} lists artifacts")).clone()
}

fn id(name: &str) -> String {
    manifest()["db_seed"]["ids"][name].as_str().unwrap_or_else(|| panic!("no id {name}")).to_owned()
}

fn find(rows: &[Value], id: &str) -> Value {
    rows.iter().find(|r| r["artifact_id"] == id).cloned().unwrap_or_else(|| panic!("{id} not listed"))
}

fn item(name: &str, shape: &str) -> Value {
    file(&format!("04-item-{name}-{shape}.body"))["artifact"].clone()
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

fn without(row: &Value, key: &str) -> Value {
    let mut row = row.clone();
    row.as_object_mut().unwrap().remove(key);
    row
}

#[test]
fn every_file_matches_the_manifest_of_the_final_host() {
    assert_eq!(manifest()["source_sha"], HOST_SHA);
    let manifest = manifest_v2();
    assert_eq!(manifest["source_sha"], HOST_SHA);
    let listed: Vec<&str> = manifest["files"].as_array().unwrap().iter().map(|f| f["file"].as_str().unwrap()).collect();
    let mut on_disk: Vec<String> = std::fs::read_dir(dir()).unwrap().map(|e| e.unwrap().file_name().to_string_lossy().into_owned()).filter(|n| n != "manifest-v2.json").collect();
    on_disk.sort();
    let mut expected: Vec<String> = listed.iter().map(|s| (*s).to_owned()).collect();
    expected.sort();
    assert_eq!(on_disk, expected, "the folder holds exactly the manifest's files");
    for name in listed {
        let bytes = std::fs::read(dir().join(name)).unwrap();
        let sha: String = Sha256::digest(&bytes).iter().map(|b| format!("{b:02x}")).collect();
        let entry = manifest["files"].as_array().unwrap().iter().find(|f| f["file"] == name).unwrap();
        assert_eq!(entry["sha256"].as_str(), Some(sha.as_str()), "{name}");
    }
    // The v1 manifest still vouches for its own 33 files, unchanged by v2.
    let v1 = self::manifest();
    for entry in v1["files"].as_array().unwrap() {
        let name = entry["file"].as_str().unwrap();
        let sha: String = Sha256::digest(std::fs::read(dir().join(name)).unwrap()).iter().map(|b| format!("{b:02x}")).collect();
        assert_eq!(entry["sha256"].as_str(), Some(sha.as_str()), "v1 {name}");
    }
    assert_eq!(v1["files"].as_array().unwrap().len(), 33);
}

#[test]
fn the_refusals_and_the_404_are_exact() {
    let refused = file("05-artifacts-bogus-400.body");
    assert_eq!(file("05-artifacts-bogus-400.meta.json")["status"], 400);
    assert!(summary::is_unsupported_representation(400, refused["error"].as_str().unwrap()));
    // The inbox says something else: it is gated on the feature alone.
    let inbox = file("05-attention-inbox-bogus-400.body");
    assert_eq!(inbox["error"], "invalid attention query");
    assert!(!summary::is_unsupported_representation(400, inbox["error"].as_str().unwrap()));
    assert_eq!(file("04-item-404.meta.json")["status"], 404);
    assert_eq!(file("04-item-404.body")["error"], "artifact not found");
}

#[test]
fn summary_rows_are_flat_rows_less_the_bodies() {
    let (summary_rows, flat_rows, legacy_rows) = (rows("01-artifacts-summary-v1.body"), rows("01-artifacts-flat-v1.body"), rows("01-artifacts-default.body"));
    let ids = |rows: &[Value]| rows.iter().map(|r| r["artifact_id"].clone()).collect::<Vec<_>>();
    assert_eq!(ids(&summary_rows), ids(&flat_rows), "the same rows, in order");
    assert_eq!(ids(&legacy_rows), ids(&flat_rows));
    for ((s, flat), legacy) in summary_rows.iter().zip(&flat_rows).zip(&legacy_rows) {
        assert_eq!(comparable(s), comparable(flat), "{}", s["artifact_id"]);
        assert_eq!(&without(legacy, "payload"), flat, "flat-v1 is the default less its payload");
        let html_form = s["type"] == "html_form";
        assert_eq!(s.get("content").is_some(), flat.get("content").is_some() && !html_form, "only html_form content is left out: {}", s["type"]);
        assert_eq!(summary::complete(s), !html_form && summary::history_marker(s).is_none());
        assert!(summary::complete(flat));
    }
    let decision = id("decision_pending");
    assert_eq!(find(&summary_rows, &decision)["decision"], find(&flat_rows, &decision)["decision"]);
    assert!(find(&summary_rows, &decision)["decision"].is_object());
    // Pinned rows are the list's rows; inbox rows add only their bucket.
    for row in rows("02-pinned-artifacts-summary-v1.body") {
        assert_eq!(row, find(&summary_rows, row["artifact_id"].as_str().unwrap()));
    }
    for row in rows("02-attention-inbox-summary-v1.body") {
        assert_eq!(without(&row, "attention_bucket"), find(&summary_rows, row["artifact_id"].as_str().unwrap()));
    }
}

#[test]
fn the_hosts_markers_carry_the_contract_vectors() {
    let summary_rows = rows("01-artifacts-summary-v1.body");
    let vectors = file("06-c1-vectors.json")["vectors"].as_array().unwrap().clone();
    assert_eq!(vectors.len(), 5);
    for vector in vectors {
        let content = vector["content"].as_str().unwrap();
        let row = find(&summary_rows, vector["artifact_id"].as_str().unwrap());
        assert_eq!(content.len() as u64, vector["utf8_bytes"].as_u64().unwrap(), "{}", vector["name"]);
        assert_eq!(summary::sha256_hex(content), vector["sha256"].as_str().unwrap(), "{}", vector["name"]);
        assert_eq!(row["content_omitted"]["sha256"].as_str(), Some(summary::content_hash_of(content).as_str()), "{}", vector["name"]);
        assert_eq!(row["content_omitted"]["bytes"].as_u64(), Some(content.len() as u64));
        let marker = summary::content_marker(&row).unwrap();
        assert_eq!(summary::check_content(marker.sha256.as_deref(), content), Check::Match);
    }
}

#[test]
fn a_content_marker_verifies_on_every_item_shape() {
    let report = id("html_form_report");
    let row = find(&rows("01-artifacts-summary-v1.body"), &report);
    let marker = summary::content_marker(&row).expect("a marker");
    assert!(row["content_omitted"]["sha256"].as_str().unwrap().starts_with("c1:"));
    for shape in ["default", "flat-v1", "bogus"] {
        let item = item("html_form_report", shape);
        let content = item["content"].as_str().unwrap();
        assert_eq!(summary::check_content(marker.sha256.as_deref(), content), Check::Match, "{shape}");
        assert_eq!(marker.bytes, Some(content.len() as u64), "{shape}: UTF-8 bytes");
        assert!(content.len() > content.chars().count(), "non-ASCII content");
        assert_eq!((marker.updated_at, &marker.version), (item["updated_at"].as_i64().unwrap(), &item["version"]), "{shape}");
    }
    let (default, flat, bogus) = (item("html_form_report", "default"), item("html_form_report", "flat-v1"), item("html_form_report", "bogus"));
    assert_eq!(without(&default, "payload"), flat, "flat-v1 drops only the payload");
    assert_eq!(bogus, default, "an unknown value is the legacy item");
}

#[test]
fn a_history_marker_is_opaque_and_counts_the_hydrated_history() {
    let plan = id("plan_artifact");
    let row = find(&rows("01-artifacts-summary-v1.body"), &plan);
    let marker = summary::history_marker(&row).expect("a marker");
    assert!(marker.digest.as_deref().is_some_and(|d| d.starts_with("h1:")));
    assert_eq!(file("06-plan-history-digest.json")["history_omitted"], row["plan"]["goal"]["history_omitted"]);
    for shape in ["default", "flat-v1", "bogus"] {
        let history = summary::goal_history(&item("plan_artifact", shape)).and_then(Value::as_array).cloned().unwrap();
        assert_eq!(marker.count, history.len() as u64, "{shape}");
        assert_eq!(marker.last_at, history.last().and_then(|h| h["at"].as_i64()), "{shape}");
    }
    // The same plan represented without its goal text: pending, not a digest.
    let pending = file("03-plan-without-goal-text-summary-v1.json")["artifact"].clone();
    let marker = summary::history_marker(&pending).unwrap();
    assert_eq!((marker.digest, pending["plan"]["goal"]["history_omitted"]["digest_pending"].as_bool()), (None, Some(true)));
    assert!(!summary::complete(&pending));
}

#[test]
fn over_budget_markers_are_exact_or_wholly_pending() {
    // A 16-byte budget: what fits is hashed exactly, the rest is pending in
    // full (never a hash without its size, or a size without its hash).
    let mut pending = 0;
    for row in rows("03-artifacts-summary-v1-budget-16-bytes.body") {
        if row["type"] == "html_form" {
            let m = &row["content_omitted"];
            let marker = summary::content_marker(&row).unwrap();
            if m["sha256"].is_null() {
                pending += 1;
                assert_eq!((m["hash_pending"].as_bool(), m["bytes"].is_null(), m["size_unknown"].as_bool()), (Some(true), true, Some(true)), "{row}");
                assert_eq!(marker.sha256, None);
            } else {
                assert!(m["sha256"].as_str().unwrap().starts_with("c1:") && marker.bytes.is_some_and(|b| b <= 16) && m.get("hash_pending").is_none(), "{row}");
            }
        }
        if row["type"] == "plan" {
            let m = &row["plan"]["goal"]["history_omitted"];
            assert_eq!((m["digest"].is_null(), m["digest_pending"].as_bool()), (true, Some(true)));
            assert_eq!(summary::history_marker(&row).unwrap().digest, None);
        }
    }
    assert!(pending >= 3, "the budget ran out: {pending} pending forms");
}

#[test]
fn some_fields_live_in_the_payload_only() {
    // answer_schema and plan_id never reach the top level, in any shape.
    for name in ["01-artifacts-summary-v1.body", "01-artifacts-flat-v1.body", "01-artifacts-default.body"] {
        assert!(rows(name).iter().all(|r| r.get("answer_schema").is_none() && r.get("plan_id").is_none()), "{name}");
    }
    assert!(item("html_form_report", "default")["payload"].get("answer_schema").is_some());
    assert!(item("plan_artifact", "default")["payload"].get("plan_id").is_some());
    // Dagger's mappings hold in every shape the desktop reads: the plan id is
    // the top-level reference_id (== plan.plan_id), report detection is the
    // top-level read_only (absent here: these forms take answers).
    let mut shapes: Vec<(String, Value)> = Vec::new();
    for list in ["01-artifacts-summary-v1.body", "01-artifacts-flat-v1.body", "01-artifacts-default.body"] {
        shapes.extend(rows(list).into_iter().map(|r| (list.to_owned(), r)));
    }
    for name in ["plan_artifact", "html_form_report"] {
        for shape in ["default", "flat-v1", "bogus"] {
            shapes.push((format!("{name} item {shape}"), item(name, shape)));
        }
    }
    for (shape, row) in &shapes {
        assert!(row.get("answer_schema").is_none() && row.get("plan_id").is_none(), "{shape}");
        if row["type"] == "plan" {
            assert_eq!(row["reference_id"], row["plan"]["plan_id"], "{shape}: reference_id is the plan id");
            assert!(row["reference_id"].as_str().is_some_and(|id| !id.is_empty()), "{shape}");
        }
        if row["type"] == "html_form" {
            let flagged = row.get("read_only") == Some(&Value::Bool(true)) || row.get("payload").and_then(|p| p.get("read_only")) == Some(&Value::Bool(true));
            assert!(!flagged && row.get("read_only").is_none(), "{shape}: a form, flagged nowhere");
        }
    }
    assert_eq!(shapes.iter().filter(|(_, r)| r["type"] == "plan").count(), 6, "the plan in 3 lists and 3 item shapes");
}

fn v2_id(name: &str) -> String {
    manifest_v2()["v2_db_seed"]["rows"][name].as_str().unwrap_or_else(|| panic!("no v2 id {name}")).to_owned()
}

#[test]
fn the_1_16_mb_form_verifies_and_flat_v1_halves_its_read() {
    let id = v2_id("html_form_1mb");
    let row = find(&rows("v2-01-artifacts-summary-v1-lone-surrogate.body"), &id);
    let marker = summary::content_marker(&row).expect("a marker");
    assert!(row.get("content").is_none() && marker.bytes.is_some_and(|b| b > 1_000_000), "{}", row["content_omitted"]);
    let (default, flat) = (file("v2-02-item-html-form-1mb-default.body")["artifact"].clone(), file("v2-02-item-html-form-1mb-flat-v1.body")["artifact"].clone());
    for (shape, item) in [("default", &default), ("flat-v1", &flat)] {
        let content = item["content"].as_str().unwrap();
        assert_eq!(summary::check_content(marker.sha256.as_deref(), content), Check::Match, "{shape}");
        assert_eq!(marker.bytes, Some(content.len() as u64), "{shape}");
    }
    assert_eq!(without(&default, "payload"), flat);
    let size = |name: &str| std::fs::metadata(dir().join(name)).unwrap().len();
    let (legacy, flat_bytes) = (size("v2-02-item-html-form-1mb-default.body"), size("v2-02-item-html-form-1mb-flat-v1.body"));
    assert!(flat_bytes * 19 < legacy * 10, "flat-v1 about halves the hydration: {flat_bytes} vs {legacy}");
    // Pinned in flat-v1, the same form is its flat-v1 item.
    assert_eq!(find(&rows("v2-04-pinned-artifacts-flat-v1.body"), &id), flat);
}

#[test]
fn a_question_reads_the_same_in_summary_and_flat_rows() {
    let id = v2_id("question");
    let (s, f) = (find(&rows("v2-03-artifacts-question-summary-v1.body"), &id), find(&rows("v2-03-artifacts-question-flat-v1.body"), &id));
    assert_eq!(s, f, "nothing is left out of a question");
    assert_eq!(s["type"], "question");
    assert!(s["decision"]["decision_id"].is_string() && summary::complete(&s));
}

#[test]
fn the_host_advertises_summary_lists_as_the_desktop_reads_them() {
    // Captured from a Host branch that still advertised 1; a landed Host
    // advertises the contract the feature landed in instead.
    let mut info = file("v2-05-server-info.body").as_object().unwrap().clone();
    assert!(summary::advertised(&info));
    info["contract"]["features"]["artifact_summary_v1"] = serde_json::json!(990);
    assert!(summary::advertised(&info));
}

/// KNOWN LATENT ISSUE (not hidden; the main-line guard was reverted): a row
/// whose content holds a lone surrogate. The Host's create route refuses one
/// (409); a row written directly is listed with a pending marker, which the
/// desktop reads (the summary list carries no content, so no surrogate).
/// The row's item GET would carry the content as a `\ud800` escape, which
/// serde_json refuses: that one card's hydration fails ("Not loaded"), and a
/// flat-v1 or default list holding such a row fails as a whole.
#[test]
fn a_lone_surrogate_row_is_listed_pending_and_its_body_cannot_be_read() {
    let id = v2_id("html_form_lone_surrogate");
    let row = find(&rows("v2-01-artifacts-summary-v1-lone-surrogate.body"), &id);
    let m = &row["content_omitted"];
    assert_eq!((m["bytes"].is_null(), m["size_unknown"].as_bool(), m["sha256"].is_null(), m["hash_pending"].as_bool()), (true, Some(true), true, Some(true)), "{m}");
    let marker = summary::content_marker(&row).unwrap();
    assert_eq!((marker.bytes, marker.sha256), (None, None));
    assert!(!summary::complete(&row) && row.get("content").is_none());
    let refused = file("v2-01-post-lone-surrogate-html-form.body");
    assert_eq!(file("v2-01-post-lone-surrogate-html-form.meta.json")["status"], 409);
    assert!(refused["error"].as_str().unwrap().contains("surrogates not allowed"));
    // Today's real behaviour for the body itself, as the Host would send it.
    let item = format!(r#"{{"artifact": {{"artifact_id": "{id}", "type": "html_form", "content": "bad \ud800 text"}}}}"#);
    assert!(serde_json::from_str::<Value>(&item).is_err(), "a lone surrogate escape is refused: the latent issue");
}
