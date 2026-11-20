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

#[test]
fn the_app_installs_a_pinned_model_only_after_asking_for_it() {
    use std::fs;
    const ID: &str = "faster-whisper:base.en";
    const ROUTE: &str = "/transcription-models/install";
    let place = Place::new("install-speech");
    let manifest = place.root.join("huggingface_hub");
    fs::create_dir(&manifest).unwrap();
    fs::write(manifest.join("__init__.py"),r#"from pathlib import Path
from types import SimpleNamespace as NS
import hashlib,os,time
root=Path(os.environ['INSTALL_FIXTURE'])
files={n:(n*4).encode() for n in ('config.json','model.bin','tokenizer.json')}
class HfApi:
    def __init__(self,endpoint,token):
        assert endpoint=='https://huggingface.co' and token is False
    def model_info(self,repo,revision,files_metadata):
        assert repo=='Systran/faster-whisper-base.en' and revision=='3d3d5dee26484f91867d81cb899cfcf72b96be6c' and files_metadata
        with (root/'requests').open('a') as f:print(os.getpid(),file=f)
        if (root/'spawn-child').exists() and os.fork()==0:
            (root/'descendant').write_text(str(os.getpid()))
            for _ in range(3000):
                if not root.exists():break
                time.sleep(.01)
            os._exit(0)
        while root.exists() and not (root/'release').exists():time.sleep(.01)
        return NS(sha=revision,siblings=[NS(rfilename=n,size=len(b),blob_id=None,lfs=NS(sha256=hashlib.sha256(b).hexdigest())) for n,b in files.items()])
def hf_hub_url(repo,name,revision,endpoint):return name
"#).unwrap();
    fs::write(manifest.join("utils.py"),r#"from . import files,root
from contextlib import contextmanager
import time
class Response:
    status_code=200
    headers={}
    def raise_for_status(self):pass
    def iter_bytes(self,chunk_size):
        if (root/'pause').exists() and self.name=='model.bin':
            yield self.data[:4]
            (root/'partial').touch()
            while (root/'pause').exists():time.sleep(.01)
            yield self.data[4:]
        else:yield self.data
class Session:
    @contextmanager
    def stream(self,method,url,headers,follow_redirects):
        r=Response();r.name=url;r.data=files[url]
        if 'Range' in headers:
            offset=int(headers['Range'][6:-1])
            (root/'offset').write_text(str(offset))
            r.status_code=206;r.headers={'Content-Range':f'bytes {offset}-{len(r.data)-1}/{len(r.data)}'}
            r.data=r.data[offset:]
        yield r
def get_session():return Session()
"#).unwrap();
    let root = place.root.to_str().unwrap();
    let worker = place.root.join("speech.py");
    fs::copy(
        concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../../tools/transcription-download.py"
        ),
        place.root.join("transcription-download.py"),
    )
    .unwrap();
    fs::write(&worker,r#"import json,sys
from pathlib import Path
for line in sys.stdin:
    msg=json.loads(line)
    if msg['op']=='load':
        model=Path(msg['model_source']).name
        print('{"event":"ready"}',flush=True)
    elif msg['op']=='transcribe':
        print(json.dumps(dict(id=msg['id'],segments=[dict(text="Heard by "+model+".",no_speech_prob=0)])),flush=True)
    else:break
"#).unwrap();
    let start = || {
        place.start_in(
            "admin",
            &[
                "--whisper-python",
                "python3",
                "--whisper-worker",
                worker.to_str().unwrap(),
            ],
            &[
                ("CLARP_WHISPER_FAKE", "1"),
                ("HOME", root),
                ("HF_HOME", root),
                ("PYTHONPATH", root),
                ("INSTALL_FIXTURE", root),
            ],
        )
    };
    let requests = || {
        fs::read_to_string(place.root.join("requests"))
            .unwrap_or_default()
            .lines()
            .count()
    };
    fs::write(place.root.join("spawn-child"), "").unwrap();
    let host = start();
    let key = "/background-jobs/transcription-model-install-344af2724f588da1";
    let row = |host: &Host| {
        host.get("/transcription-capabilities")["catalog"]
            .as_array()
            .unwrap()
            .iter()
            .find(|r| r["id"] == ID)
            .unwrap()
            .clone()
    };
    let fixture: Vec<Value> = serde_json::from_str(include_str!(
        "../../../clarp-kernel/tests/fixtures/transcription-install.json"
    ))
    .unwrap();
    assert_eq!(requests(), 0);
    let (limited, _) = pair(&host, "Visitor", "limited");
    assert_eq!(
        host.ask_as(&limited, "POST", ROUTE, Some(&json!({"model_id":ID})))
            .0,
        403
    );
    for sample in fixture {
        let action = sample["action"].as_str().unwrap();
        if action == "cancel" {
            wait_for("download child started", || {
                requests() == 1 && place.root.join("descendant").is_file()
            });
            fs::remove_file(place.root.join("spawn-child")).unwrap();
            assert_eq!(
                host.ask(
                    "POST",
                    "/transcription-models/remove",
                    Some(&json!({"model_id":ID}))
                ),
                (
                    400,
                    json!({"error":format!("model installation is still in progress: {ID}")})
                )
            );
            let (code, receipt) = host.ask("DELETE", key, Some(&json!({"expected_generation":2})));
            assert_eq!(code, 409);
            assert_eq!(receipt["changed"], false);
            let (code, receipt) = host.ask("DELETE", key, Some(&sample["body"]));
            assert_eq!(code, sample["reply"][0].as_u64().unwrap() as u16);
            for field in ["ok", "changed", "job_handle"] {
                assert_eq!(receipt[field], sample["reply"][1][field]);
            }
            for field in [
                "job_id",
                "kind",
                "status",
                "owner_kind",
                "generation",
                "terminal_reason",
            ] {
                assert_eq!(receipt["job"][field], sample["reply"][1]["job"][field]);
            }
            let pid = fs::read_to_string(place.root.join("requests")).unwrap();
            assert!(!std::path::Path::new(&format!("/proc/{}", pid.trim())).exists());
            let descendant = fs::read_to_string(place.root.join("descendant")).unwrap();
            wait_for("download process group stopped", || {
                fs::read_to_string(format!("/proc/{descendant}/stat"))
                    .map_or(true, |stat| stat.contains(") Z "))
            });
            assert!(
                !place
                    .root
                    .join("data/models/.install-faster-whisper--base.en")
                    .exists()
            );
        } else {
            if action == "already-installed" {
                wait_for("second download started", || requests() == 2);
                fs::write(place.root.join("release"), "").unwrap();
                wait_for("download published", || row(&host)["installed"] == true);
            }
            let (code, mut reply) = if action == "start" {
                std::thread::scope(|scope| {
                    let first = scope.spawn(|| host.ask("POST", ROUTE, Some(&sample["body"])));
                    let mut a = host.ask("POST", ROUTE, Some(&sample["body"]));
                    let mut b = first.join().unwrap();
                    if a.1.get("provisional_id").is_none() {
                        std::mem::swap(&mut a, &mut b);
                    }
                    assert_eq!(b.0, 202);
                    assert!(b.1.get("provisional_id").is_none());
                    assert_eq!(a.1["job_handle"], b.1["job_handle"]);
                    a
                })
            } else {
                host.ask("POST", ROUTE, Some(&sample["body"]))
            };
            if let Some(token) = reply.get("provisional_id") {
                assert!(
                    token
                        .as_str()
                        .is_some_and(|s| s.len() == 32 && s.bytes().all(|b| b.is_ascii_hexdigit()))
                );
                reply["provisional_id"] = json!("<token>");
            }
            assert_eq!(json!([code, reply]), sample["reply"], "{action}");
        }
        assert_eq!(row(&host), sample["catalog"], "{action}");
    }
    assert_eq!(
        host.ask(
            "POST",
            "/transcription-providers",
            Some(&json!({"engine":ID}))
        )
        .0,
        200
    );
    let (phone, _) = pair(&host, "Speaker", "full");
    let ordinary = fetch(
        &host,
        &phone,
        "POST",
        "/transcribe",
        &[],
        b"synthetic default clip",
    );
    assert_eq!(ordinary.status, 200, "{}", ordinary.text());
    assert!(
        ordinary
            .text()
            .contains("Heard by faster-whisper--base.en.")
    );
    let first = host.new_agent("Kestrel");
    let second = host.new_agent("Cedar");
    let call = host
        .ask_as(
            &phone,
            "POST",
            "/calls",
            Some(&json!({"agents":[first,second]})),
        )
        .1;
    let in_call = fetch(
        &host,
        &phone,
        "POST",
        "/transcribe",
        &[
            ("X-Clarp-Call-Id", call["call"]["call_id"].as_str().unwrap()),
            ("X-Transcription-Session", &first),
        ],
        b"synthetic call clip",
    );
    assert_eq!(in_call.status, 200, "{}", in_call.text());
    assert_eq!(
        serde_json::from_slice::<Value>(&in_call.body).unwrap()["stt"]["effective_engine"],
        ID
    );
    let data = place.root.join("data/models/faster-whisper--base.en");
    assert_eq!(
        fs::read(data.join("model.bin")).unwrap(),
        b"model.bin".repeat(4)
    );
    assert_eq!(host.get(key)["job"]["status"], "succeeded");
    assert_eq!(requests(), 2);
    host.kill();
    let host = start();
    assert_eq!(row(&host)["installed"], true);
    assert_eq!(
        host.get("/transcription-capabilities")["default_model"],
        "faster-whisper:small.en"
    );
    assert_eq!(requests(), 2);
    assert_eq!(
        host.ask(
            "POST",
            "/transcription-models/remove",
            Some(&json!({"model_id":ID}))
        )
        .0,
        200
    );
    assert!(!data.exists());
    assert_eq!(row(&host)["status"], "available");
    fs::write(place.root.join("pause"), "").unwrap();
    assert_eq!(
        host.ask("POST", ROUTE, Some(&json!({"model_id":ID}))).0,
        202
    );
    wait_for("partial model staged", || {
        place.root.join("partial").is_file()
    });
    let pid = fs::read_to_string(place.root.join("requests"))
        .unwrap()
        .lines()
        .last()
        .unwrap()
        .to_owned();
    host.kill();
    wait_for("download ended with Host", || {
        fs::read_to_string(format!("/proc/{pid}/stat")).map_or(true, |s| s.contains(") Z "))
    });
    let host = start();
    assert_eq!(requests(), 3);
    assert_eq!(host.get(key)["job"]["terminal_reason"], "worker_vanished");
    assert_eq!(
        fs::read(
            place
                .root
                .join("data/models/.install-faster-whisper--base.en/model.bin")
        )
        .unwrap(),
        b"mode"
    );
    fs::remove_file(place.root.join("pause")).unwrap();
    assert_eq!(
        host.ask("POST", ROUTE, Some(&json!({"model_id":ID}))).0,
        202
    );
    wait_for("owner resumed interrupted snapshot", || {
        row(&host)["installed"] == true
    });
    assert_eq!(fs::read_to_string(place.root.join("offset")).unwrap(), "4");
    assert_eq!(requests(), 4);
}

#[test]
fn the_download_adapter_bounds_and_verifies_offline_streams() {
    let result = std::process::Command::new("python3")
        .args([
            "-IB",
            concat!(
                env!("CARGO_MANIFEST_DIR"),
                "/../../tools/test-transcription-download.py"
            ),
        ])
        .output()
        .unwrap();
    assert!(
        result.status.success(),
        "{}",
        String::from_utf8_lossy(&result.stderr)
    );
}

#[test]
fn installer_restart_reconciles_published_files_without_starting_a_download() {
    use std::fs;
    use std::os::unix::fs::{MetadataExt, symlink};
    const ID: &str = "faster-whisper:base.en";
    const KEY: &str = "transcription-model-install-344af2724f588da1";
    for boundary in [
        "queued",
        "partial",
        "intent",
        "renamed",
        "committed",
        "superseded",
        "unsafe",
        "cancelled",
        "orphan-old",
        "orphan-young",
    ] {
        let place = Place::new("model-restore");
        let seed = place.start_with_token("admin");
        seed.kill();
        let model_root = place.root.join("data/models");
        let stage = model_root.join(".install-faster-whisper--base.en");
        let final_path = model_root.join("faster-whisper--base.en");
        fs::create_dir_all(&stage).unwrap();
        for file in ["config.json", "model.bin", "tokenizer.json"] {
            fs::write(stage.join(file), b"resume me").unwrap();
        }
        let meta = fs::metadata(&stage).unwrap();
        let outside = place.root.join("external");
        fs::create_dir(&outside).unwrap();
        fs::write(outside.join("keep"), "external").unwrap();
        if boundary == "unsafe" {
            fs::rename(&stage, model_root.join("legitimate")).unwrap();
            symlink(&outside, &stage).unwrap();
        }
        let mut registered = JobBook::new()
            .decide(
                JobCommand::Register {
                    id: KEY.into(),
                    kind: "transcription-model-install".into(),
                    queued: boundary == "queued",
                    title: "Install test".into(),
                    detail: String::new(),
                    restart_cancelled: true,
                    started_trace_id: String::new(),
                    worker: Worker::none(),
                    heartbeat_timeout_ms: Some(120000),
                    metadata: json!({"model_id":ID,"expire_queued":true}),
                },
                1000,
                1,
            )
            .unwrap()
            .record
            .unwrap();
        if boundary == "superseded" {
            registered.job.generation = 2;
        }
        if boundary == "cancelled" {
            registered.job.status = clarp_kernel::jobs::JobStatus::Cancelled;
        }
        if boundary == "committed" {
            registered.job.status = clarp_kernel::jobs::JobStatus::Succeeded;
            let record = json!({"id":ID,"model":"base.en","provider":"faster-whisper","_local_path":final_path,
                "_managed_root":final_path,"_install_generation":1});
            keep_record(&place, "transcription-models", "records", &json!([record]));
        }
        if !boundary.starts_with("orphan") {
            keep_record(
                &place,
                "computer-jobs",
                KEY,
                &json!({"job":registered.job,"timeline":[registered]}),
            );
        }
        if matches!(boundary, "intent" | "renamed" | "unsafe" | "superseded") {
            keep_record(
                &place,
                "model-publication",
                ID,
                &json!({"generation":1,"device":meta.dev(),"inode":meta.ino()}),
            );
        }
        if matches!(boundary, "renamed" | "committed") {
            fs::rename(&stage, &final_path).unwrap();
        }
        if boundary == "orphan-old" {
            fs::File::open(&stage)
                .unwrap()
                .set_modified(std::time::SystemTime::UNIX_EPOCH)
                .unwrap();
        }
        // Any attempt to invoke the downloader writes an observable marker. No hub is involved.
        fs::write(
            place.root.join("transcription-download.py"),
            "import os,pathlib\npathlib.Path(os.environ['DOWNLOADER_CALLED']).touch()\n",
        )
        .unwrap();
        let speech = place.root.join("fake-worker.py");
        fs::write(&speech,"import sys\nsys.stdin.readline()\nprint('{\"event\":\"ready\"}',flush=True)\nsys.stdin.readline()\n").unwrap();
        let marker = place.root.join("unexpected");
        let host = place.start_in(
            "admin",
            &[
                "--whisper-python",
                "python3",
                "--whisper-worker",
                speech.to_str().unwrap(),
            ],
            &[
                ("DOWNLOADER_CALLED", marker.to_str().unwrap()),
                ("HOME", place.root.to_str().unwrap()),
            ],
        );
        let cap = host.get("/transcription-capabilities");
        let row = cap["catalog"]
            .as_array()
            .unwrap()
            .iter()
            .find(|r| r["id"] == ID)
            .unwrap();
        assert_eq!(row["installed"], boundary == "committed", "{boundary}");
        if !boundary.starts_with("orphan") {
            let job = &host.get(&format!("/background-jobs/{KEY}"))["job"];
            let expected = match boundary {
                "committed" => "succeeded",
                "cancelled" => "cancelled",
                _ => "failed",
            };
            assert_eq!(job["status"], expected, "{boundary}");
            if expected == "failed" {
                assert_eq!(
                    job["terminal_reason"],
                    if boundary == "queued" {
                        "worker_never_started"
                    } else {
                        "worker_vanished"
                    }
                );
            }
        }
        assert_eq!(
            stage.exists(),
            !matches!(boundary, "committed" | "cancelled" | "orphan-old"),
            "{boundary}"
        );
        if matches!(boundary, "renamed" | "partial" | "intent" | "superseded") {
            assert_eq!(fs::read(stage.join("model.bin")).unwrap(), b"resume me");
        }
        assert_eq!(final_path.exists(), boundary == "committed", "{boundary}");
        assert!(outside.join("keep").is_file());
        if boundary == "orphan-young" {
            assert_eq!(
                host.ask(
                    "POST",
                    "/transcription-models/remove",
                    Some(&json!({"model_id":ID}))
                )
                .0,
                200
            );
            assert!(!stage.exists());
        }
        assert!(!marker.exists(), "{boundary} called the downloader on open");
        host.kill();
        assert_eq!(
            kept_records(&place, "model-publication").contains_key(ID),
            boundary == "unsafe"
        );
    }
}
