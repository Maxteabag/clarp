use super::*;

const SOURCE_TOKEN: &str = "test-import-source";
const DESTINATION_TOKEN: &str = "test-import-destination";
const QUEUE: &str = "/turn-queue?session=mover";

#[derive(Clone, Copy, PartialEq, Eq)]
enum Source {
    Legacy,
    Rejects,
    Marks,
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
                        Source::Marks => *mark = json!({"to":expected_target,"at":123}),
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
                "It is still on the Host at {source} too. Archive it there, so that only one of the two takes its turns.\nHere it is held until then: it answers what you say to it, and what would start by itself (a goal that is due) waits. Resume it here when it is archived there: the app's Resume, or POST /turn-queue/resume."
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
        Source::Marks => {
            assert!(report.contains(&format!("It is marked moved on the Host at {source}. Its turn-start doors there now refuse it. It is resumed here.")), "{report}");
            assert_eq!(moved.lock().unwrap()["to"], target);
        }
    }
    assert_eq!(host.get(QUEUE)["paused"], source_kind != Source::Marks);
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
        usize::from(source_kind != Source::Legacy) + usize::from(take_back)
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
