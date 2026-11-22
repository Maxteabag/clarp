use super::*;

const SOURCE_TOKEN: &str = "test-import-source";
const DESTINATION_TOKEN: &str = "test-import-destination";
const QUEUE: &str = "/turn-queue?session=mover";

#[derive(Clone, Copy, PartialEq, Eq)]
enum Source {
    Legacy,
    Rejects,
    Marks,
    Retries,
}

fn exercise(source_kind: Source, take_back: bool) {
    let place = Place::new("import-record-move");
    let host = place.start_with(DESTINATION_TOKEN, &["--name", "Rust destination"]);
    let identity = host.get("/pairing/identity");
    assert_eq!(identity["name"], "Rust destination");
    let target = json!({"host": identity["name"], "id": identity["server_id"]});
    assert!(target["id"].is_string());
    let expected_target = target.clone();
    let address = host.address.clone();
    let cwd = place.root.join("work");
    let moved = Arc::new(Mutex::new(Value::Null));
    let source_mark = Arc::clone(&moved);
    let first_mark = std::sync::atomic::AtomicBool::new(true);
    let (source, requests) = answers_using(move |path, request| {
        let json = match path.split('?').next().unwrap() {
            "/server-info" => {
                let features: &[&str] = if source_kind == Source::Legacy {
                    &[]
                } else {
                    &["agent_moved"]
                };
                json!({"capabilities":{"features":features}})
            }
            "/agents/snapshot" => json!({"agents":[{
                "agent_id":"move-agent-id","session":"mover","persona":"Mover",
                "backend":"scripted","cwd":cwd,"busy":false,"moved":*source_mark.lock().unwrap()
            }],"personas":[]}),
            "/log" => json!({"turns":[{"id":"row-1","role":"assistant","text":"Moved history"}]}),
            "/task-plans" => json!({"plans":[]}),
            "/artifacts" => json!({"artifacts":[]}),
            "/media" => json!({"assets":[]}),
            "/agent-portraits" => json!({"portraits":[]}),
            "/transcription-providers" => json!({}),
            "/agent-moved" => {
                let (head, body) = request.split_once("\r\n\r\n").unwrap();
                assert!(head.starts_with("POST /agent-moved "));
                assert!(head.lines().any(|line| {
                    line.eq_ignore_ascii_case(&format!("authorization: bearer {SOURCE_TOKEN}"))
                }));
                let body: Value = serde_json::from_str(body).unwrap();
                assert_eq!(body["session"], "mover");
                let queue = fetch_at(&address, DESTINATION_TOKEN, "GET", QUEUE, &[], b"");
                assert_eq!(queue.status, 200);
                let queue: Value = serde_json::from_slice(&queue.body).unwrap();
                assert_eq!(queue["paused"], true);
                let mut mark = source_mark.lock().unwrap();
                if body.get("moved_to") == Some(&Value::Null) {
                    assert_eq!(mark["to"], expected_target);
                    *mark = Value::Null;
                } else {
                    assert_eq!(body["moved_to"], expected_target);
                    assert!(mark.is_null());
                    match source_kind {
                        Source::Legacy => panic!("a legacy source must not receive a mark"),
                        Source::Rejects => {
                            return ("409 Conflict", json!({"error":"already_moved"}).to_string());
                        }
                        Source::Retries
                            if first_mark.swap(false, std::sync::atomic::Ordering::Relaxed) =>
                        {
                            return (
                                "503 Service Unavailable",
                                json!({"error": "try again"}).to_string(),
                            );
                        }
                        Source::Marks | Source::Retries => {
                            *mark = json!({"to":expected_target,"at":123});
                        }
                    }
                }
                json!({"ok":true,"session":"mover","changed":true,"moved":*mark})
            }
            _ => {
                return (
                    "404 Not Found",
                    json!({"error":"no such fixture"}).to_string(),
                );
            }
        };
        ("200 OK", json.to_string())
    });
    let run = |extra: &[&str]| {
        let output = place
            .host_command()
            .args([
                "import",
                "--from",
                &source,
                "--host",
                &host.address,
                "--session",
                "mover",
            ])
            .args(extra)
            .env("CLARP_SOURCE_TOKEN", SOURCE_TOKEN)
            .env("CLARP_HOST_TOKEN", DESTINATION_TOKEN)
            .ended();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        String::from_utf8(output.stdout).unwrap()
    };
    let report = run(&[]);
    assert_eq!(
        host.conversation("mover"),
        [said("assistant", "Moved history")]
    );

    match source_kind {
        Source::Legacy => {
            assert!(report.contains(&format!(
                "It is still on the Host at {source} too. Archive it there, so that only one of the two takes its turns.\nHere it is held until then: all messages and automatic work wait. Resume it here when it is archived there: the app's Resume, or POST /turn-queue/resume."
            )), "{report}");
        }
        Source::Rejects => {
            assert!(
                report.contains("409 to POST /agent-moved: already_moved"),
                "{report}"
            );
            assert!(
                report.contains(
                    "The copy here stays held. It is on both Hosts until marked moved or archived"
                ),
                "{report}"
            );
        }
        Source::Retries => {
            assert!(report.contains("The move is incomplete"), "{report}");
            assert!(moved.lock().unwrap().is_null());
            let sender = host.new_agent("Sender");
            let mut traces = Vec::new();
            for (id, text, by) in [
                ("peer-before-move", "first message", "agent"),
                ("user-before-move", "second message", "user"),
            ] {
                let mut message = json!({
                    "session": "mover", "text": text, "client_msg_id": id, "origin": by,
                });
                if by == "agent" {
                    message["sender"] = json!(sender);
                }
                let sent = host.post("/send", message);
                assert_eq!(sent["queued"], true, "{sent}");
                traces.push(sent["trace_id"].as_str().unwrap().to_string());
            }
            let queue = host.get(QUEUE);
            assert_eq!(queue["paused"], true);
            let items = queue["items"].as_array().unwrap();
            assert_eq!(items.len(), 2);
            assert_eq!(items[0]["text"], "second message");
            assert_eq!(items[1]["text"], "first message");
            assert_eq!(
                host.conversation("mover"),
                [said("assistant", "Moved history")]
            );
            let report = run(&["--files"]);
            assert!(report.contains("It is resumed here."), "{report}");
            assert_eq!(moved.lock().unwrap()["to"], target);
            wait_for("the two held messages to finish", || {
                host.conversation("mover").len() == 5 && host.state("mover") == "done"
            });
            let rows = host.turns("mover");
            assert_eq!(rows.len(), 5, "{rows:?}");
            assert_eq!(rows[0]["text"], "Moved history");
            // Scripted echoes the first prompt line, which can be the team context header.
            // Match each completed reply to its own send rather than the prompt text.
            let expected = [
                ("second message", &traces[1]),
                ("first message", &traces[0]),
            ];
            for (pair, (text, trace)) in rows[1..].as_chunks::<2>().0.iter().zip(expected) {
                assert_eq!(pair[0]["role"], "user");
                assert_eq!(pair[0]["text"], text);
                assert_eq!(pair[0]["trace_id"], trace.as_str());
                assert_eq!(pair[1]["role"], "assistant");
                assert_eq!(pair[1]["trace_id"], trace.as_str());
                assert_eq!(pair[1]["turn"]["status"], "completed");
            }
        }
        Source::Marks => {
            assert!(report.contains(&format!("It is marked moved on the Host at {source}. Its turn-start doors there now refuse it. It is resumed here.")), "{report}");
            assert_eq!(moved.lock().unwrap()["to"], target);
        }
    }
    assert_eq!(
        host.get(QUEUE)["paused"],
        matches!(source_kind, Source::Legacy | Source::Rejects)
    );
    if take_back {
        let report = run(&["--back"]);
        assert!(
            report.contains("mover is held here. What it did here since the move stays here."),
            "{report}"
        );
        assert!(
            report.contains(&format!(
                "The source Host at {source} goes on from where it was."
            )),
            "{report}"
        );
        assert_eq!(host.get(QUEUE)["paused"], true);
        assert!(moved.lock().unwrap().is_null());
        assert_eq!(
            host.conversation("mover"),
            [said("assistant", "Moved history")]
        );
    }
    let posts = requests
        .lock()
        .unwrap()
        .iter()
        .filter(|request| request.starts_with("POST /agent-moved "))
        .count();
    assert_eq!(
        posts,
        usize::from(source_kind != Source::Legacy)
            + usize::from(source_kind == Source::Retries)
            + usize::from(take_back)
    );
}

#[test]
fn an_import_marks_its_source_before_resuming_here() {
    exercise(Source::Marks, false);
}

#[test]
fn a_refused_mark_leaves_the_import_held_and_names_the_refusal() {
    exercise(Source::Rejects, false);
}

#[test]
fn an_older_source_keeps_the_manual_archive_and_resume_instructions() {
    exercise(Source::Legacy, false);
}

#[test]
fn taking_a_move_back_holds_the_import_before_clearing_the_source() {
    exercise(Source::Marks, true);
}

#[test]
fn an_import_holds_messages_until_the_files_retry_finishes_the_move() {
    exercise(Source::Retries, false);
}

fn import_with_optional_avatar(
    place: &Place,
    host: &Host,
    cwd: PathBuf,
    source_picture: Option<String>,
) -> String {
    let (source, _) = answers_using(move |path, _| {
        let response = match path.split('?').next().unwrap() {
            "/agents/snapshot" => json!({"agents": [{
                "agent_id": "optional-import", "session": "mover", "persona": "Mover",
                "backend": "scripted", "cwd": cwd, "busy": false,
                "avatar_url": source_picture.as_ref().map(|_| "/avatars/move-picture"),
            }]}),
            "/avatars/move-picture" => {
                return ("200 OK", source_picture.clone().unwrap());
            }
            "/log" => json!({"turns": [{"id": "row-1", "role": "assistant", "text": "Moved history"}]}),
            "/task-plans" => json!({"plans": []}),
            "/artifacts" => json!({"artifacts": []}),
            "/media" => json!({"assets": []}),
            "/agent-portraits" => json!({"portraits": []}),
            "/server-info" | "/transcription-providers" => json!({}),
            _ => return ("404 Not Found", "{}".into()),
        };
        ("200 OK", response.to_string())
    });
    let output = place
        .host_command()
        .args(["import", "--from", &source, "--host", &host.address, "--session", "mover"])
        .env("CLARP_SOURCE_TOKEN", SOURCE_TOKEN)
        .env("CLARP_HOST_TOKEN", DESTINATION_TOKEN)
        .ended();
    assert!(output.status.success(), "{}", String::from_utf8_lossy(&output.stderr));
    String::from_utf8(output.stdout).unwrap()
}

#[test]
fn an_oversized_avatar_does_not_prevent_an_agent_import() {
    let place = Place::new("import-oversized-avatar");
    let host = place.start_with_token(DESTINATION_TOKEN);
    let cwd = place.root.join("work");
    let too_large = clarp_kernel::personas::AVATAR_MOST + 1;
    let report = import_with_optional_avatar(&place, &host, cwd.clone(), Some("x".repeat(too_large)));
    assert!(report.contains("avatar not carried: too large"), "{report}");
    assert_eq!(host.conversation("mover"), [said("assistant", "Moved history")]);

    // An older importer can still include the oversized picture in its request.
    let answer = host.post("/agents/import", json!({
        "agent": {"agent_id": "older-import-id", "session": "older-mover",
            "persona": "Older Mover", "backend": "scripted", "cwd": cwd, "busy": false},
        "from": "http://old-host",
        "picture": clarp_kernel::python::b64encode(&vec![b'x'; too_large]),
        "turns": [{"id": "row-2", "role": "assistant", "text": "Older history"}],
    }));
    assert_eq!(answer["avatar_not_carried"], "too large");
    assert_eq!(host.conversation("older-mover"), [said("assistant", "Older history")]);
}

#[test]
fn an_import_creates_its_missing_working_directory() {
    let place = Place::new("import-missing-cwd");
    let host = place.start_with_token(DESTINATION_TOKEN);
    let cwd = place.root.join("absent").join("workspace");
    assert!(!cwd.exists());
    let report = import_with_optional_avatar(&place, &host, cwd.clone(), None);
    assert!(report.contains(&format!("created working directory: {}", cwd.display())), "{report}");
    assert!(cwd.is_dir());
    assert!(std::fs::read_dir(&cwd).unwrap().next().is_none());
    assert_eq!(host.conversation("mover"), [said("assistant", "Moved history")]);
}
