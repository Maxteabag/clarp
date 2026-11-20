//! A computer's durable jobs have no agent owner.
use super::*;
use clarp_kernel::jobs::{JobBook, JobCommand, Worker};

#[test]
fn computer_job_transitions_and_wire_rows_match_the_reference() {
    let rows: Vec<Value> = serde_json::from_str(include_str!(
        "../../../clarp-kernel/tests/fixtures/computer-jobs.json"
    ))
    .unwrap();
    let mut book = JobBook::new();
    let mut revision = 0;
    let mut events = std::collections::HashMap::<String, Vec<clarp_kernel::jobs::JobRecord>>::new();
    for case in rows {
        let action = case["action"].as_str().unwrap();
        let id = case["id"].as_str().unwrap().to_string();
        let now = case["now"].as_i64().unwrap();
        let command = match action {
            "register" | "join" | "restart" => JobCommand::Register {
                queued: true,
                id: id.clone(),
                kind: case["kind"].as_str().unwrap().into(),
                title: case["title"].as_str().unwrap().into(),
                detail: "Preparing".into(),
                worker: Worker::none(),
                restart_cancelled: true,
                started_trace_id: String::new(),
                heartbeat_timeout_ms: Some(120000),
                metadata: json!({"fixture":true}),
            },
            "start" => JobCommand::Start {
                id: id.clone(),
                generation: Some(1),
                worker: Worker::none(),
            },
            "finish" | "stale_finish" => JobCommand::Finish {
                id: id.clone(),
                generation: 2,
                worker: Worker::none(),
                failed: None,
            },
            "absent" | "cancel" | "cancel_again" | "stale_cancel" | "late_cancel" => {
                JobCommand::Cancel {
                    id: id.clone(),
                    expected_generation: match action {
                        "cancel" => Some(1),
                        "stale_cancel" | "late_cancel" => Some(2),
                        _ => None,
                    },
                    reason: "user_cancelled".into(),
                }
            }
            _ => panic!("unknown fixture step {action}"),
        };
        let change = book.decide(command, now, revision + 1).unwrap();
        let changed = change.record.is_some();
        if let Some(entry) = change.record {
            revision += 1;
            book.apply(&entry);
            events.entry(id.clone()).or_default().push(entry);
        }
        if let Some(beat) = change.beat {
            book.beat(&beat);
        }
        let mut listed: Vec<_> = book.jobs().collect();
        listed.sort_by_key(|job| {
            (
                job.status.is_terminal(),
                std::cmp::Reverse(job.updated_at_ms),
            )
        });
        let rows: Vec<_> = listed
            .into_iter()
            .map(|job| clarp_host::wire::computer_job("computer-fixture", job, now))
            .collect();
        assert_eq!(json!(rows), case["listing"]["jobs"], "list after {action}");
        assert_eq!(case["listing"]["snapshot_revision"], revision);
        let Some(job) = book.get(&id) else {
            assert_eq!(case["receipt"], json!([404,{"error":"job not found"}]));
            continue;
        };
        let wire = clarp_host::wire::computer_job("computer-fixture", job, now);
        assert_eq!(wire, case["detail"]["job"], "{action}");
        assert_eq!(job.handle(), case["detail"]["handle"]);
        assert_eq!(
            json!(clarp_host::wire::job_timeline(&events[&id])),
            case["detail"]["timeline"]
        );
        if !case["receipt"].is_null() {
            let receipt = &case["receipt"][1];
            assert_eq!(receipt["job"], wire);
            assert_eq!(receipt["changed"], changed);
            assert_eq!(
                receipt["error"] == "generation_mismatch",
                change.generation_mismatch
            );
        }
    }
}

#[test]
fn computer_jobs_can_be_seen_and_cancelled_without_an_agent_across_restart() {
    let place = Place::new("computer-owned-jobs");
    let host = place.start_with_token("admin");
    let (limited, _) = pair(&host, "Reader", "limited");
    host.kill();
    let id = "transcription-model-install-fixture";
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_millis() as i64;
    let registered = JobBook::new()
        .decide(
            JobCommand::Register {
                id: id.into(),
                queued: true,
                kind: "transcription-model-install".into(),
                title: "Install model".into(),
                detail: "Preparing model download".into(),
                worker: Worker::none(),
                restart_cancelled: true,
                started_trace_id: String::new(),
                heartbeat_timeout_ms: Some(120000),
                metadata: json!({"model_id":"faster-whisper:small","expire_queued":true}),
            },
            now,
            ((now + 1000000) as u64) * 1000,
        )
        .unwrap()
        .record
        .unwrap();
    keep_record(
        &place,
        "computer-jobs",
        id,
        &json!({"job":registered.job,"timeline":[registered]}),
    );
    // A clock can step backwards while the Host is stopped. Previously published
    // revisions still bound the next revision it may give out.
    let high_water = registered.job.revision;
    let mut ancient = registered.clone();
    ancient.job.id = "long-running".into();
    ancient.job.updated_at_ms = now - 2 * 86_400_000;
    keep_record(
        &place,
        "computer-jobs",
        "long-running",
        &json!({"job":ancient.job,"timeline":[]}),
    );

    for n in 0..102 {
        let mut old = registered.clone();
        old.job.id = format!("prior-{n:03}");
        old.job.status = clarp_kernel::jobs::JobStatus::Succeeded;
        old.job.updated_at_ms = if n == 101 {
            now - 2 * 86_400_000
        } else {
            now - n
        };
        keep_record(
            &place,
            "computer-jobs",
            &old.job.id,
            &json!({"job":old.job,"timeline":[]}),
        );
    }
    let host = place.start_with_token("admin");
    for unknown in ["claude-task:missing", "ordinary-missing"] {
        assert_eq!(
            host.ask("DELETE", &format!("/background-jobs/{unknown}"), None),
            (404, json!({"error":"job not found"}))
        );
    }
    let path = format!("/background-jobs/{id}");
    let list = host.get("/background-jobs");
    let detail = host.get(&path);
    let job = &detail["job"];
    // Age and elapsed time are measured independently by the two GET requests.
    for field in ["job_id", "owner_kind", "computer_id", "revision", "status"] {
        assert_eq!(list["jobs"][0][field], job[field]);
    }
    assert_eq!(list["jobs"].as_array().unwrap().len(), 102);
    assert_eq!(list["jobs"][1]["job_id"], "long-running");
    assert_eq!(list["jobs"][2]["job_id"], "prior-000");
    assert_eq!(list["jobs"][101]["job_id"], "prior-099");
    assert!(list["snapshot_revision"].as_u64().unwrap() >= high_water);
    assert_eq!(job["owner_kind"], "computer");
    assert_eq!(job["agent_name"], "Computer");
    assert!(job["computer_id"].as_str().is_some_and(|id| !id.is_empty()));
    assert_eq!(job["status"], "queued");
    assert_eq!(detail["owner_session"], "");
    assert_eq!(detail["helper_session"], "");
    assert_eq!(detail["owner_agent_id"], "");
    assert_eq!(detail["is_process"], true);
    assert_eq!(detail["timeline"][0]["status"], "queued");
    assert_eq!(detail["log"]["reason"], "no_log");
    let limited_detail = host.ask_as(&limited, "GET", &path, None);
    assert_eq!(limited_detail.0, 200);
    assert_eq!(limited_detail.1["log"]["reason"], "forbidden");
    let cancel = |generation| {
        host.ask(
            "DELETE",
            &path,
            Some(&json!({"expected_generation":generation})),
        )
    };
    let (code, stale) = cancel(2);
    assert_eq!(code, 409);
    assert_eq!(stale["error"], "generation_mismatch");
    assert_eq!(stale["changed"], false);
    assert_eq!(stale["current_generation"], 1);
    assert_eq!(stale["expected_generation"], 2);
    assert_eq!(stale["job_handle"], format!("bg1:1:{id}"));
    let (code, ended) = cancel(1);
    assert_eq!(code, 200);
    assert_eq!(ended["changed"], true);
    assert_eq!(ended["job"]["status"], "cancelled");
    assert_eq!(ended["job"]["terminal_reason"], "user_cancelled");
    assert_eq!(cancel(1).1["changed"], false);
    host.kill();
    let host = place.start_with_token("admin");
    let reopened = host.get(&path);
    assert_eq!(reopened["job"]["generation"], 1);
    assert_eq!(reopened["job"]["status"], "cancelled");
    assert_eq!(reopened["timeline"].as_array().unwrap().len(), 2);
    assert_eq!(reopened["timeline"][1]["status"], "cancelled");
    assert_eq!(
        host.get("/background-jobs")["jobs"][1]["status"],
        "cancelled"
    );
    assert!(reopened["job"]["revision"].as_u64().unwrap() > high_water);
}
