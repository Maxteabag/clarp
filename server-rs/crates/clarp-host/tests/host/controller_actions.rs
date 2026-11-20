use super::*;

#[test]
fn controller_actions_are_defined_mapped_and_run_once_through_the_door() {
    use std::sync::mpsc;
    let place = Place::new("controller-actions");
    let listener = TcpListener::bind("127.0.0.2:0").unwrap();
    listener.set_nonblocking(true).unwrap();
    let prefix = format!("http://{}", listener.local_addr().unwrap());
    let target = format!("{prefix}/hook");
    let requests = Arc::new(Mutex::new(Vec::new()));
    let kept = requests.clone();
    let (release, hold) = mpsc::channel::<()>();
    let stop = Arc::new(AtomicBool::new(false));
    let ended = stop.clone();
    let server = std::thread::spawn(move || {
        while !ended.load(Ordering::SeqCst) {
            let Ok((mut stream, _)) = listener.accept() else {
                std::thread::sleep(Duration::from_millis(5));
                continue;
            };
            stream
                .set_read_timeout(Some(Duration::from_secs(5)))
                .unwrap();
            let raw = whole_request(&mut stream);
            let path = raw.split_whitespace().nth(1).unwrap().to_string();
            let first = {
                let mut all = kept.lock().unwrap();
                all.push(raw);
                all.len() == 1
            };
            if first {
                hold.recv_timeout(Duration::from_secs(4)).unwrap();
            }
            let (status, body, extra) = match path.as_str() {
                "/redirect" => ("302 Found", "moved", "Location: /hook\r\n"),
                "/failure" => ("503 Busy", "down", ""),
                _ => ("200 OK", "done", ""),
            };
            let _ = write!(
                stream,
                "HTTP/1.1 {status}\r\n{extra}Content-Length: {}\r\nConnection: close\r\n\r\n{body}",
                body.len()
            );
        }
    });
    let cfg = place.root.join("actions.toml");
    let configure = |allowed: bool| {
        std::fs::write(
            &cfg,
            if allowed {
                format!("[controller_actions]\nallowed_urls=['{prefix}']\n")
            } else {
                String::new()
            },
        )
        .unwrap()
    };
    std::fs::write(
        &cfg,
        "[controller_actions]\nallowed_urls=['invalid-private-fixture']\n",
    )
    .unwrap();
    let start = || place.start_with("admin", &["--speech-keys", cfg.to_str().unwrap()]);
    let host = start();
    let (limited, device) = pair(&host, "Watch", "limited");
    let (full, _) = pair(&host, "Phone", "full");
    let ask = |host: &Host, token: &str, path: &str, body: Value| {
        host.ask_as(token, "POST", path, Some(&body))
    };
    let base = "/controller-actions";
    let events = host.listen_at("/events?token=admin");
    let initial = host.get(base);
    assert_eq!(initial["revision"], 0);
    assert_eq!(initial["actions"], json!([]));
    assert_eq!(initial["mappings"], json!([]));
    assert_eq!(
        host.get("/server-info")["contract"]["features"]["controller_actions"],
        63
    );
    let def =
        json!({"id":"line","type":"http_post","title":"Line","url":target,"body":{"stored":true}});
    for token in [&limited, &full] {
        assert_eq!(ask(&host, token, base, def.clone()).0, 403);
    }
    assert_eq!(ask(&host, "", base, def.clone()).0, 401);
    assert_eq!(
        ask(&host, "admin", base, def.clone()),
        (400, json!({"error":"url is not allow-listed"}))
    );
    wait_for("invalid configured address logged without its text", || {
        host.heard.lock().unwrap().lines().any(|line| {
            line == "config.controller_action_urls refused 1 allowed URL(s) that do not \
                         parse as a plain http(s) destination"
        })
    });
    assert!(
        !host
            .heard
            .lock()
            .unwrap()
            .contains("invalid-private-fixture")
    );
    configure(true);
    let (code, created) = ask(&host, "admin", base, def.clone());
    assert_eq!(code, 201);
    assert_eq!(created["action"]["enabled"], true);
    assert_eq!(ask(&host, "admin", base, def.clone()).0, 409);
    let mapping =
        json!({"device":"*","button":"secondary","gesture":"swipe-up","action_id":"line"});
    assert_eq!(
        ask(
            &host,
            &limited,
            "/controller-actions/mappings",
            mapping.clone()
        )
        .0,
        403
    );
    assert_eq!(
        ask(&host, &full, "/controller-actions/mappings", mapping).0,
        200
    );
    let rev = host.get(base)["revision"].clone();
    wait_for("registry change reaches a connected device", || {
        events
            .lock()
            .unwrap()
            .iter()
            .any(|e| *e == json!({"type":"controller-actions-updated","revision":rev}))
    });
    let invoke = "/controller-actions/line/invoke";
    assert_eq!(
        ask(&host, &limited, invoke, json!({})),
        (400, json!({"error":"request_id required"}))
    );
    assert_eq!(
        ask(
            &host,
            &limited,
            "/controller-actions/missing/invoke",
            json!({"request_id":"r"})
        )
        .0,
        404
    );
    std::thread::scope(|threads| {
        let run = || {
            ask(
                &host,
                &limited,
                invoke,
                json!({"request_id":"first","device":"button","source_kind":"watch","url":"unused","body":{"bad":true},"text":"override"}),
            )
        };
        let first = threads.spawn(run);
        wait_for("stand-in holds the original POST", || {
            !requests.lock().unwrap().is_empty()
        });
        let duplicate = threads.spawn(run);
        let (code, bounced) = ask(&host, &limited, invoke, json!({"request_id":"bounce"}));
        assert_eq!(code, 200);
        assert_eq!(
            bounced,
            json!({"action_id":"line","title":"Line","type":"http_post","ok":true,"duration_ms":0,"debounced":true})
        );
        release.send(()).unwrap();
        let (code, result) = first.join().unwrap();
        assert_eq!(code, 200);
        assert_eq!(result["status"], 200);
        assert_eq!(result["response"], "done");
        let mut expected = result;
        expected["duplicate"] = true.into();
        assert_eq!(duplicate.join().unwrap(), (200, expected));
    });
    {
        let req = requests.lock().unwrap();
        assert_eq!(req.len(), 1);
        assert!(req[0].starts_with("POST /hook "));
        let headers = req[0].to_ascii_lowercase();
        assert!(headers.contains("\r\ncontent-type: application/json\r\n"));
        assert!(headers.contains("\r\nuser-agent: clarp-controller-action\r\n"));
        assert_eq!(
            serde_json::from_str::<Value>(req[0].split_once("\r\n\r\n").unwrap().1).unwrap(),
            json!({"stored":true})
        );
    }
    // The debounced request repeated after the window is its answer again, with `duplicate`,
    // and nothing runs a second time (R132, the Python Host's PR 468).
    std::thread::sleep(Duration::from_millis(1_700));
    let again = ask(&host, &limited, invoke, json!({"request_id":"bounce"}));
    assert_eq!(
        again,
        (
            200,
            json!({"action_id":"line","title":"Line","type":"http_post","ok":true,"duration_ms":0,"debounced":true,"duplicate":true})
        )
    );
    assert_eq!(
        requests.lock().unwrap().len(),
        1,
        "nothing ran a second time"
    );
    let state = host.get(base);
    let audit = host
        .ask_as(&limited, "GET", "/controller-actions/audit", None)
        .1;
    assert_eq!(audit["entries"][0]["event"], "run");
    assert_eq!(audit["entries"][0]["actor"], format!("device:{device}"));
    assert_eq!(
        audit["entries"][0]["source"],
        json!({"device":"button","source_kind":"watch"})
    );
    assert_eq!(audit["entries"][1]["event"], "debounced");
    assert_eq!(
        host.ask("GET", "/controller-actions/audit?limit=invalid", None)
            .0,
        400
    );
    assert_eq!(
        host.get("/controller-actions/audit?limit=-1")["entries"],
        json!([])
    );
    assert_eq!(
        host.get("/controller-actions/audit?limit=1")["entries"]
            .as_array()
            .unwrap()
            .len(),
        1
    );
    host.kill();
    let host = start();
    assert_eq!(host.get(base), state);
    assert_eq!(host.get("/controller-actions/audit"), audit);
    let bounce = ask(&host, &limited, invoke, json!({"request_id":"bounce"})).1;
    assert_eq!(bounce["debounced"], true);
    assert_eq!(bounce["duplicate"], true);
    assert_eq!(
        ask(&host, &limited, invoke, json!({"request_id":"first"})).1["duplicate"],
        true
    );
    configure(false);
    let (code, failed) = ask(&host, &limited, invoke, json!({"request_id":"revoked"}));
    assert_eq!(code, 200);
    assert_eq!(failed["ok"], false);
    assert_eq!(failed["error"], "url is no longer allow-listed");
    assert_eq!(requests.lock().unwrap().len(), 1);
    configure(true);
    for (path, status) in [("failure", 503), ("redirect", 302)] {
        assert_eq!(
            ask(
                &host,
                "admin",
                "/controller-actions/update",
                json!({"id":"line","url":format!("{prefix}/{path}")})
            )
            .0,
            200
        );
        let (code, r) = ask(&host, &limited, invoke, json!({"request_id":path}));
        assert_eq!(code, 200);
        assert_eq!(r["ok"], false);
        assert_eq!(r["status"], status);
        assert_eq!(r["error"], format!("HTTP {status}"));
    }
    assert_eq!(requests.lock().unwrap().len(), 3);
    let repeated_failure = ask(&host, &limited, invoke, json!({"request_id":"failure"})).1;
    assert_eq!(repeated_failure["ok"], false);
    assert_eq!(repeated_failure["status"], 503);
    assert_eq!(repeated_failure["duplicate"], true);
    assert_eq!(requests.lock().unwrap().len(), 3);
    assert_eq!(
        ask(
            &host,
            "admin",
            "/controller-actions/update",
            json!({"id":"line","enabled":false})
        )
        .0,
        200
    );
    assert_eq!(
        ask(&host, &limited, invoke, json!({"request_id":"first"})).0,
        409
    );
    let cli = |args: &[&str]| {
        let mut a = vec!["clarp-admin", "controller-action"];
        a.extend_from_slice(args);
        tool(&host, &place.root, None, &a)
    };
    let agent = host.new_agent("Prompt fixture");
    assert_eq!(
        cli(&[
            "add",
            "ask",
            "--title",
            "Ask",
            "--agent",
            &agent,
            "--text",
            "fixture question"
        ])
        .0,
        0
    );
    assert_eq!(
        cli(&[
            "map",
            "--button",
            "primary",
            "--gesture",
            "hold",
            "--action",
            "ask"
        ])
        .0,
        0
    );
    let (code, listing, _) = cli(&["list"]);
    assert_eq!(code, 0);
    assert!(listing.contains("• ask — Ask"));
    assert!(listing.contains("← primary hold"));
    let (code, _, error) = cli(&["update", "ask"]);
    assert_eq!((code, error.trim()), (1, "nothing to update"));
    let (code, _, error) = cli(&[
        "add",
        "bad",
        "--title",
        "Bad",
        "--http-post",
        &target,
        "--body",
        "[]",
    ]);
    assert_eq!((code, error.trim()), (1, "--body must be a JSON object"));
    let (code, screen, _) = cli(&["list", "--json"]);
    assert_eq!(code, 0);
    assert_eq!(
        serde_json::from_str::<Value>(&screen).unwrap(),
        host.get(base)
    );
    assert_eq!(
        cli(&["unmap", "--button", "primary", "--gesture", "hold"]).0,
        0
    );
    assert_eq!(cli(&["update", "ask", "--title", "New"]).0, 0);
    let (code, result, _) = cli(&["run", "ask"]);
    assert_eq!(code, 0);
    assert_eq!(
        serde_json::from_str::<Value>(&result).unwrap()["title"],
        "New"
    );
    wait_for("the admitted prompt is answered", || {
        host.conversation(&agent)
            .contains(&("assistant".into(), "fixture question".into()))
    });
    assert_eq!(cli(&["audit", "--limit", "1"]).0, 0);
    let (code, deleted) = ask(
        &host,
        "admin",
        "/controller-actions/delete",
        json!({"id":"line"}),
    );
    assert_eq!(code, 200);
    assert_eq!(deleted["mappings_removed"], 1);
    assert_eq!(cli(&["remove", "ask"]).0, 0);
    assert_eq!(host.get(base)["actions"], json!([]));
    // The administrator prompt command and the action use the same admission:
    // a quiet agent is outside the ordinary roster, and a paused queue stays paused.
    let quiet = host.get("/agents/snapshot")["agents"]
        .as_array()
        .unwrap()
        .iter()
        .find(|a| a["is_janitor"] == true)
        .unwrap()["session"]
        .as_str()
        .unwrap()
        .to_string();
    assert_eq!(
        cli(&[
            "add", "quiet", "--title", "Quiet", "--agent", &quiet, "--text", "private"
        ])
        .0,
        0
    );
    let (code, reply, _) = cli(&["run", "quiet"]);
    assert_eq!(code, 1);
    assert!(
        serde_json::from_str::<Value>(&reply).unwrap()["error"]
            .as_str()
            .unwrap()
            .contains("HTTP 404")
    );
    host.post("/stop", json!({"session":agent}));
    assert_eq!(
        cli(&[
            "add",
            "paused",
            "--title",
            "Paused",
            "--agent",
            &agent,
            "--text",
            "after the stop"
        ])
        .0,
        0
    );
    assert_eq!(cli(&["run", "paused"]).0, 0);
    let queue = host.get(&format!("/turn-queue?session={agent}"));
    assert_eq!(queue["paused"], true, "{queue}");
    wait_for(
        "new direct prompt admitted under the existing queue hold",
        || {
            host.conversation(&agent)
                .contains(&("assistant".into(), "after the stop".into()))
        },
    );
    host.kill();
    // Seed a full audit through its storage port, then exercise one real
    // definition change. Exactly the oldest row leaves.
    let seeded: Vec<_> = (0..500).map(|n| json!({"event":"test","at":n})).collect();
    keep_record(&place, "controller-actions", "audit", &json!(seeded));
    let host = start();
    assert_eq!(
        ask(
            &host,
            "admin",
            "/controller-actions/delete",
            json!({"id":"paused"})
        )
        .0,
        200
    );
    let history = host.get("/controller-actions/audit?limit=900")["entries"].clone();
    assert_eq!(history.as_array().unwrap().len(), 500);
    assert_eq!(history[0]["event"], "delete");
    assert_eq!(history[499]["at"], 1);
    // The page limit can hide a surplus row. Verify the retained history too.
    host.kill();
    let records = kept_records(&place, "controller-actions");
    let stored = records["audit"].as_array().unwrap();
    assert!(stored.iter().rev().eq(history.as_array().unwrap()));
    stop.store(true, Ordering::SeqCst);
    server.join().unwrap();
}
