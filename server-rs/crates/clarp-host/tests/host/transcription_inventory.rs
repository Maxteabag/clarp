use super::*;

#[test]
fn installed_transcription_models_are_selected_and_removed_by_their_own_records() {
    use clarp_ports::kv::KeyValueEngine as _;
    use std::os::unix::fs::symlink;
    const ID: &str = "faster-whisper:small";
    const ROUTE: &str = "/transcription-models/remove";
    let fixture: Value = serde_json::from_str(include_str!(
        "../../../clarp-kernel/tests/fixtures/transcription-inventory.json"
    ))
    .unwrap();
    let file_set = |path: &std::path::Path| {
        std::fs::create_dir_all(path).unwrap();
        for name in ["config.json", "model.bin", "tokenizer.json"] {
            std::fs::write(path.join(name), name).unwrap();
        }
    };
    let save = |place: &Place, records: &Value| {
        std::fs::create_dir_all(place.root.join("data")).unwrap();
        clarp_store_sqlite::SqliteKeyValue
            .open(&place.root.join("data/fleet.db"))
            .unwrap()
            .put(
                "kept.transcription-models",
                "records",
                &serde_json::to_vec(records).unwrap(),
            )
            .unwrap();
    };
    let place = Place::new("installed-workers");
    let script = place.root.join("worker.py");
    let loads = place.root.join("loads");
    std::fs::write(&script, r#"import sys, json, os
from pathlib import Path
for line in sys.stdin:
    msg = json.loads(line)
    if msg['op'] == 'load':
        source = Path(msg['model_source'])
        if source.is_absolute():
            assert os.environ['HF_HUB_OFFLINE'] == '1'
            assert (source / 'tokenizer.json').is_file()
        with open(os.environ['MODEL_LOG'], 'a') as out:
            print(source.name, os.getpid(), file=out)
        if (source / 'fail-load').exists():
            print(json.dumps(dict(event='load_error',error='test refusal')),flush=True)
            break
        print('{"event":"ready"}', flush=True)
    elif msg['op'] == 'transcribe':
        if Path(msg['path']).read_bytes() == b'share-inference':
            from time import sleep
            marker = Path(os.environ['MODEL_LOG']).with_suffix('.hearing')
            try:
                marker.touch(exist_ok=False)
            except FileExistsError:
                print(json.dumps(dict(id=msg['id'],error='overlapping local inference')),flush=True)
                continue
            marker.with_suffix('.entered').touch()
            sleep(0.2)
            marker.unlink()
        text = f"The {source.name} model heard this clip."
        print(json.dumps(dict(id=msg['id'], segments=[dict(text=text,no_speech_prob=0)])), flush=True)
    else:
        break
"#).unwrap();
    let model = |name: &str| {
        let path = place.root.join(name);
        file_set(&path);
        json!({"id":format!("faster-whisper:{name}"),"provider":"faster-whisper","model":name,
               "name":name,"weight":"medium","_local_path":path})
    };
    save(&place, &json!([model("small"), model("medium")]));
    let host = place.start_in(
        "admin",
        &[
            "--whisper-python",
            "python3",
            "--whisper-worker",
            script.to_str().unwrap(),
        ],
        &[
            ("MODEL_LOG", loads.to_str().unwrap()),
            ("HOME", place.root.to_str().unwrap()),
            ("HF_HOME", place.root.to_str().unwrap()),
        ],
    );
    let (phone, _) = pair(&host, "Phone", "full");
    let (_, listed) = host.ask("GET", "/transcription-capabilities", None);
    assert_eq!(listed["default_model"], "faster-whisper:small.en");
    assert_eq!(listed["models"][0]["id"], listed["default_model"]);
    assert_eq!(listed["models"][1]["id"], ID);
    assert!(
        listed["models"]
            .as_array()
            .unwrap()
            .iter()
            .all(|m| m.get("_local_path").is_none())
    );
    let row = listed["catalog"]
        .as_array()
        .unwrap()
        .iter()
        .find(|r| r["id"] == ID)
        .unwrap();
    assert_eq!(row["installed"], true);
    assert_eq!(row["status"], "installed");
    for name in ["small", "small", "medium", "small"] {
        let id = format!("faster-whisper:{name}");
        let result = fetch(
            &host,
            &phone,
            "POST",
            "/transcribe",
            &[("X-Transcription-Model", &id)],
            b"clip",
        );
        assert_eq!(result.status, 200, "{}", result.text());
        assert!(
            result
                .text()
                .contains(&format!("The {name} model heard this clip."))
        );
    }
    let log = std::fs::read_to_string(&loads).unwrap();
    let alternatives: Vec<_> = log
        .lines()
        .map(|s| s.split_once(' ').unwrap())
        .filter(|(m, _)| *m != "small.en")
        .collect();
    assert_eq!(
        alternatives.iter().map(|(m, _)| *m).collect::<Vec<_>>(),
        ["small", "medium", "small"]
    );
    for (_, pid) in &alternatives[..2] {
        assert!(!std::path::Path::new(&format!("/proc/{pid}")).exists());
    }
    let (status, settings) = host.ask(
        "POST",
        "/transcription-providers",
        Some(&json!({
            "long_form_model":ID,"long_form_threshold_sec":1
        })),
    );
    assert_eq!(status, 200);
    assert_eq!(settings["long_form_model"], ID);
    // PCM mono, one unsigned byte per frame, eight frames at eight Hz.
    let wav = b"RIFF,\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x08\x00\x00\x00\x08\x00\x00\x00\x01\x00\x08\x00data\x08\x00\x00\x00abcdefgh";
    let long = fetch(
        &host,
        &phone,
        "POST",
        "/transcribe",
        &[("Content-Type", "audio/wav")],
        wav,
    );
    assert_eq!(long.status, 200, "{}", long.text());
    assert!(long.text().contains("The small model heard this clip."));
    let first = host.new_agent("Theo");
    let second = host.new_agent("Mike");
    let started = host
        .ask_as(
            &phone,
            "POST",
            "/calls",
            Some(&json!({"agents":[first,second]})),
        )
        .1;
    let call_id = started["call"]["call_id"].as_str().unwrap();
    let spoken = fetch(
        &host,
        &phone,
        "POST",
        "/transcribe",
        &[
            ("X-Clarp-Call-Id", call_id),
            ("X-Transcription-Session", &first),
            ("Content-Type", "audio/wav"),
        ],
        wav,
    );
    assert_eq!(spoken.status, 200, "{}", spoken.text());
    let reply: Value = serde_json::from_slice(&spoken.body).unwrap();
    assert_eq!(reply["stt"]["effective_engine"], ID);
    assert_eq!(reply["stt"]["attempts"][0]["language"], "auto");
    assert_eq!(
        host.ask(
            "POST",
            "/transcription-providers",
            Some(&json!({"long_form_model":""}))
        )
        .0,
        200
    );
    let (limited, _) = pair(&host, "Viewer", "limited");
    assert_eq!(
        host.ask_as(&limited, "POST", ROUTE, Some(&json!({"model_id":ID})))
            .0,
        403
    );
    assert_eq!(
        host.ask("POST", ROUTE, Some(&json!([]))),
        (400, json!({"error":"bad json"}))
    );
    assert_eq!(
        host.ask("POST", ROUTE, Some(&json!({"model_id":ID}))),
        (200, json!({"ok":true,"model_id":ID}))
    );
    assert!(place.root.join("small/model.bin").is_file());
    assert!(!std::path::Path::new(&format!("/proc/{}", alternatives[2].1)).exists());
    assert_eq!(
        fetch(
            &host,
            &phone,
            "POST",
            "/transcribe",
            &[("X-Transcription-Model", ID)],
            b"clip"
        )
        .status,
        400
    );
    let fail = place.root.join("medium/fail-load");
    std::fs::write(&fail, "").unwrap();
    let ask_medium = || {
        fetch(
            &host,
            &phone,
            "POST",
            "/transcribe",
            &[("X-Transcription-Model", "faster-whisper:medium")],
            b"clip",
        )
    };
    let failed = ask_medium();
    assert_eq!(failed.status, 500);
    assert!(failed.text().contains("test refusal"));
    std::fs::remove_file(fail).unwrap();
    assert_eq!(ask_medium().status, 200);
    std::thread::scope(|scope| {
        let call = |model| {
            fetch(
                &host,
                &phone,
                "POST",
                "/transcribe",
                &[("X-Transcription-Model", model)],
                b"share-inference",
            )
        };
        let variant = scope.spawn(move || call("faster-whisper:medium"));
        wait_for("variant in inference", || {
            loads.with_extension("entered").exists()
        });
        assert_eq!(call("faster-whisper:small.en").status, 200);
        assert_eq!(variant.join().unwrap().status, 200);
    });
    host.kill();

    let mut cases = fixture["removal"].as_array().unwrap().clone();
    for kind in [
        "root",
        "sibling",
        "nested",
        "link",
        "linked-parent",
        "empty",
        "null",
        "missing",
        "owned-file",
    ] {
        cases.push(json!({"kind":kind,"error":null,"registered":false}));
    }
    for kind in ["owned-link", "owned-dangling", "stage-link"] {
        cases.push(json!({"kind":kind,"error":"R112","registered":true}));
    }
    for case in cases {
        let kind = case["kind"].as_str().unwrap();
        let place = Place::new("remove-model");
        let root = place.root.join("data/models");
        let own = root.join("faster-whisper--small");
        let external = place.root.join("external");
        let sibling = root.join("faster-whisper--medium");
        file_set(&own);
        file_set(&external);
        file_set(&sibling);
        symlink(&external, own.join("link-to-external")).unwrap();
        let target = match kind {
            "root" => root.clone(),
            "sibling" => sibling.clone(),
            "nested" => {
                file_set(&sibling.join("child"));
                sibling.join("child")
            }
            "link" => {
                symlink(&own, root.join("alias")).unwrap();
                root.join("alias")
            }
            "linked-parent" => {
                symlink(&root, place.root.join("alias")).unwrap();
                place.root.join("alias/faster-whisper--small")
            }
            "outside" | "external" | "active" | "incomplete" | "unknown" => external.clone(),
            _ => own.clone(),
        };
        let mut row = json!({"id":ID,"provider":"faster-whisper","model":"small","_local_path":if kind=="external" || kind=="outside" { &external } else { &own }});
        if !matches!(kind, "unknown" | "external" | "incomplete" | "active") {
            row["_managed_root"] = json!(target);
        }
        match kind {
            "stage-link" => {
                symlink(&external, root.join(".install-faster-whisper--small")).unwrap()
            }
            "null" => row["_managed_root"] = Value::Null,
            "empty" => row["_managed_root"] = json!(""),
            "missing" => row["_managed_root"] = json!(root.join("absent")),
            "outside" => std::fs::remove_dir_all(&own).unwrap(),
            "owned-link" | "owned-dangling" | "owned-file" => {
                std::fs::remove_dir_all(&own).unwrap();
                if kind == "owned-file" {
                    std::fs::write(&own, b"managed").unwrap();
                } else if kind == "owned-link" {
                    symlink(&external, &own).unwrap();
                } else {
                    symlink(root.join("absent"), &own).unwrap();
                }
                row["_local_path"] = json!(external);
            }
            _ => (),
        }
        if kind == "incomplete" {
            std::fs::remove_file(own.join("model.bin")).unwrap();
        }
        save(
            &place,
            &if kind == "unknown" {
                json!([])
            } else {
                json!([row])
            },
        );
        let args: &[&str] = if kind == "active" {
            &["--whisper-python", "/bin/false", "--whisper-model", "small"]
        } else {
            &[]
        };
        let host = place.start_in("admin", args, &[]);
        let (status, body) = host.ask("POST", ROUTE, Some(&json!({"model_id":format!(" {ID} ")})));
        if let Some(why) = case["error"].as_str() {
            let why = if why == "R112" {
                format!(
                    "refusing to remove {ID}: its folder is a link or outside the managed models folder"
                )
            } else {
                why.replace("<target>", target.to_str().unwrap())
            };
            assert_eq!((status, body), (400, json!({"error":why})), "{kind}");
        } else {
            assert_eq!(
                (status, body),
                (200, json!({"ok":true,"model_id":ID})),
                "{kind}"
            );
        }
        let log = host.heard.clone();
        host.kill();
        let logged = log.lock().unwrap().clone();
        let bad_stored = matches!(
            kind,
            "root"
                | "sibling"
                | "nested"
                | "link"
                | "linked-parent"
                | "null"
                | "empty"
                | "missing"
                | "outside"
        );
        assert_eq!(
            logged
                .lines()
                .filter(|l| l.contains(&format!("model={ID} reason=stored_path")))
                .count(),
            usize::from(bad_stored),
            "{kind}"
        );
        for line in logged
            .lines()
            .filter(|l| l.contains("model removal") || l.contains("model refused"))
        {
            assert!(!line.contains(place.root.to_str().unwrap()), "{kind}");
        }
        if kind == "stage-link" {
            assert!(own.join("model.bin").is_file());
            assert!(root.join(".install-faster-whisper--small").is_symlink());
        } else if matches!(kind, "owned-link" | "owned-dangling") {
            assert!(own.is_symlink());
            assert!(logged.contains(&format!("model={ID} reason=link")));
        } else if bad_stored || matches!(kind, "managed" | "owned-file") {
            assert!(!own.exists(), "{kind}");
        }
        if let Some(expected) = case["files_survive"].as_bool() {
            assert_eq!(target.exists(), expected, "{kind}");
        }
        assert!(external.join("model.bin").is_file());
        assert!(sibling.join("model.bin").is_file());
        let store = clarp_store_sqlite::SqliteKeyValue
            .open(&place.root.join("data/fleet.db"))
            .unwrap();
        let rows: Vec<Value> = serde_json::from_slice(
            &store
                .get("kept.transcription-models", "records")
                .unwrap()
                .unwrap(),
        )
        .unwrap();
        assert_eq!(
            rows.iter().any(|r| r["id"] == ID),
            case["registered"].as_bool().unwrap(),
            "{kind}"
        );
    }
    // Make unlink fail in a real registered directory after the pending record is durable.
    use std::os::unix::fs::PermissionsExt;
    let place = Place::new("inventory-recovery");
    let root = place.root.join("data/models");
    let folder = root.join("faster-whisper--small");
    let other = root.join("faster-whisper--medium");
    file_set(&folder);
    file_set(&other);
    let record = json!({"id":ID,"provider":"faster-whisper","model":"small","_local_path":folder,"_managed_root":folder});
    let stored = |key: &str| {
        clarp_store_sqlite::SqliteKeyValue
            .open(&place.root.join("data/fleet.db"))
            .unwrap()
            .get("kept.transcription-models", key)
            .unwrap()
            .unwrap()
    };
    let rows = || serde_json::from_slice::<Value>(&stored("records")).unwrap();
    let listed = |host: &Host| {
        host.get("/transcription-capabilities")["models"]
            .as_array()
            .unwrap()
            .iter()
            .any(|r| r["id"] == ID)
    };
    save(&place, &json!([record]));
    let host = place.start_with_token("admin");
    assert!(listed(&host));
    std::fs::set_permissions(&folder, std::fs::Permissions::from_mode(0o555)).unwrap();
    let (code, error) = host.ask("POST", ROUTE, Some(&json!({"model_id":ID})));
    std::fs::set_permissions(&folder, std::fs::Permissions::from_mode(0o755)).unwrap();
    assert_eq!(code, 500);
    assert!(
        error["error"]
            .as_str()
            .unwrap()
            .contains("Permission denied")
    );
    assert!(!listed(&host));
    host.kill();
    let mut marked = record.clone();
    marked["_remove_pending"] = json!(true);
    assert_eq!(rows(), json!([marked]));
    assert!(folder.join("model.bin").exists());
    place.start_with_token("admin").kill();
    assert!(!folder.exists());
    assert_eq!(rows(), json!([]));
    save(&place, &json!([marked]));
    place.start_with_token("admin").kill();
    assert_eq!(rows(), json!([]));
    let moved_root = place.root.join("moved-models");
    std::fs::rename(&root, &moved_root).unwrap();
    save(&place, &json!([marked]));
    place.start_with_token("admin").kill();
    assert_eq!(rows(), json!([]));
    std::fs::rename(moved_root, &root).unwrap();
    file_set(&folder);
    let mut unsafe_row = marked.clone();
    unsafe_row["_managed_root"] = json!(other);
    save(&place, &json!([unsafe_row]));
    let host = place.start_with_token("admin");
    assert!(!listed(&host));
    host.kill();
    assert_eq!(rows(), json!([]));
    assert!(!folder.exists());
    assert!(other.join("model.bin").is_file());
    file_set(&folder);
    save(&place, &json!([marked]));
    let host = place.start_with(
        "admin",
        &["--whisper-python", "/bin/false", "--whisper-model", "small"],
    );
    assert!(
        host.get("/transcription-capabilities")["catalog"]
            .as_array()
            .unwrap()
            .iter()
            .any(|r| r["id"] == ID && r["installed"] == true)
    );
    host.kill();
    assert_eq!(rows(), json!([record]));
    place.start_with_token("admin").kill();
    assert!(folder.join("model.bin").is_file());
    assert_eq!(rows(), json!([record]));
    let sentinel = "private-corrupt-record";
    clarp_store_sqlite::SqliteKeyValue
        .open(&place.root.join("data/fleet.db"))
        .unwrap()
        .put("kept.transcription-models", "records", sentinel.as_bytes())
        .unwrap();
    let host = place.start_with_token("admin");
    assert!(!listed(&host));
    let log = host.heard.lock().unwrap().clone();
    assert_eq!(
        log.lines()
            .filter(|s| s.contains("a record of the kind transcription-models could not be read and is set aside as records.unreadable"))
            .count(),
        1
    );
    assert!(!log.contains(sentinel));
    host.kill();
    assert_eq!(stored("records.unreadable"), sentinel.as_bytes());
    // Moved aside whole: its key is free.
    let freed = clarp_store_sqlite::SqliteKeyValue
        .open(&place.root.join("data/fleet.db"))
        .unwrap()
        .get("kept.transcription-models", "records")
        .unwrap();
    assert_eq!(freed, None);
    let host = place.start_with_token("admin");
    assert_eq!(
        host.ask("POST", ROUTE, Some(&json!({"model_id":ID}))).0,
        200
    );
    host.kill();
    assert_eq!(stored("records.unreadable"), sentinel.as_bytes());
    assert_eq!(rows(), json!([]));
    assert!(folder.join("model.bin").is_file());
}
