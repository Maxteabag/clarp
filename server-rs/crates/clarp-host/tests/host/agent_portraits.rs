//! The pictures a client can choose to display for a model and a maintenance role.
use super::*;

#[test]
fn the_picture_display_preference_survives_restart_and_uses_local_art() {
    let place = Place::new("portrait-preference");
    let root = place.root.join("static");
    let config = place.root.join("owner.toml");
    std::fs::write(&config, "[agents]\nclaude_model='claude-sonnet-test'\n").unwrap();
    std::fs::create_dir_all(root.join("avatars/models")).unwrap();
    std::fs::create_dir_all(root.join("janitor-avatars")).unwrap();
    std::fs::write(root.join("avatars/models/sample.sonnet.png"), b"model art").unwrap();
    std::fs::write(
        root.join("janitor-avatars/delegator-message-dispatcher.jpg"),
        b"role art",
    )
    .unwrap();
    std::fs::write(
        root.join("janitor-avatars/janitor-default.jpg"),
        b"fallback",
    )
    .unwrap();
    let args = [
        "--static",
        root.to_str().unwrap(),
        "--speech-keys",
        config.to_str().unwrap(),
    ];
    let host = place.start_without("fixture", &args, &[], &["OPENAI_API_KEY"]);
    let settings = "/avatar-settings";
    // The preference is off initially. Readers still get the URL of any bundled model art.
    assert_eq!(host.get(settings), json!({"model_avatars":false}));
    let made = host.post("/agents", json!({"name":"Sample","backend":"claude"}));
    let session = made["session"].as_str().unwrap();
    let row = |snapshot: &Value, session: &str| -> Value {
        snapshot["agents"]
            .as_array()
            .unwrap()
            .iter()
            .find(|a| a["session"] == session)
            .unwrap()
            .clone()
    };
    let snapshot = host.get("/agents/snapshot");
    assert_eq!(snapshot["model_avatars"], false);
    let drawn = row(&snapshot, session)["model_avatar_url"]
        .as_str()
        .unwrap()
        .to_string();
    assert!(
        drawn.starts_with("/static/avatars/models/sample.sonnet.png?v="),
        "{drawn}"
    );
    assert_eq!(fetch(&host, "", "GET", &drawn, &[], b"").body, b"model art");
    // A janitor wears the bundled picture for its role, with the generic picture as fallback.
    for (sid, file, bytes) in [
        (
            "clarp-message-delegator",
            "delegator-message-dispatcher.jpg",
            "role art",
        ),
        ("clarp-goal-keeper", "janitor-default.jpg", "fallback"),
    ] {
        let janitor = row(&snapshot, sid);
        let url = janitor["avatar_url"].as_str().unwrap();
        assert!(
            url.starts_with(&format!("/static/janitor-avatars/{file}?v=")),
            "{url}"
        );
        assert_eq!(
            fetch(&host, "", "GET", url, &[], b"").body,
            bytes.as_bytes()
        );
    }
    // When this specific role loses its bundled JPEG it falls back to the generic art.
    std::fs::remove_file(root.join("janitor-avatars/delegator-message-dispatcher.jpg")).unwrap();
    let after = row(&host.get("/agents/snapshot"), "clarp-message-delegator");
    assert!(
        after["avatar_url"]
            .as_str()
            .unwrap()
            .starts_with("/static/janitor-avatars/janitor-default.jpg?v=")
    );
    // The settings belong to a full-access caller. Each refused update preserves the old value.
    let (limited, _) = pair(&host, "Limited", "limited");
    for method in ["GET", "POST"] {
        assert_eq!(
            host.ask_as(
                &limited,
                method,
                settings,
                Some(&json!({"model_avatars":true}))
            )
            .0,
            403
        );
    }
    for body in [
        json!({}),
        json!({"model_avatars":1}),
        json!({"model_avatars":"true"}),
    ] {
        assert_eq!(
            host.ask("POST", settings, Some(&body)),
            (400, json!({"error":"model_avatars required"}))
        );
    }
    assert_eq!(
        host.post(settings, json!({"model_avatars":true})),
        json!({"model_avatars":true})
    );
    assert_eq!(host.get("/agents/snapshot")["model_avatars"], true);
    host.kill();
    let host = place.start_without("fixture", &args, &[], &["OPENAI_API_KEY"]);
    assert_eq!(host.get(settings), json!({"model_avatars":true}));
    assert_eq!(
        host.post(settings, json!({"model_avatars":false})),
        json!({"model_avatars":false})
    );
    assert_eq!(host.get("/agents/snapshot")["model_avatars"], false);
}

/// Holds each edit until the test has examined its bytes and supplied its reply.
type EditRequest = (String, std::sync::mpsc::Sender<(u16, String)>);

fn edit_service() -> (String, std::sync::mpsc::Receiver<EditRequest>) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = format!("http://{}/images", listener.local_addr().unwrap());
    let (requests, shown) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        for stream in listener.incoming() {
            let Ok(mut socket) = stream else {
                break;
            };
            let raw = whole_request(&mut socket);
            let (answer, wait) = std::sync::mpsc::channel::<(u16, String)>();
            if requests.send((raw, answer)).is_err() {
                break;
            }
            std::thread::spawn(move || {
                if let Ok((status, body)) = wait.recv_timeout(Duration::from_secs(30)) {
                    let _ = write!(
                        socket,
                        "HTTP/1.1 {status} Fixture\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                        body.len()
                    );
                }
            });
        }
    });
    (addr, shown)
}

#[test]
fn the_owner_asks_for_two_portrait_edits_and_only_the_current_generation_keeps_them() {
    let place = Place::new("portrait-launch");
    let (url, requests) = edit_service();
    let env = [
        ("OPENAI_API_KEY", "made-up-key"),
        ("CLARP_PICTURE_ENDPOINT", url.as_str()),
    ];
    let launch = || place.start_in("fixture", &[], &env);
    // A bare Host and a Host that has no image credential cannot spend from this route.
    let bare = Place::new("portrait-bare");
    let open = bare.start_in("", &[], &env);
    assert_eq!(
        open.ask("POST", "/agent-portrait-generation", Some(&json!({})))
            .0,
        401
    );
    open.kill();
    let no_key = bare.start_without(
        "fixture",
        &[],
        &[("CLARP_PICTURE_ENDPOINT", url.as_str())],
        &["OPENAI_API_KEY"],
    );
    let absent = no_key.new_agent("No key");
    assert_eq!(
        no_key.get(&format!("/agent-portrait-generation?session={absent}"))["reason"],
        "Configure an OpenAI API key on this Computer"
    );
    assert_eq!(
        no_key
            .ask(
                "POST",
                "/agent-portrait-generation",
                Some(&json!({"session":absent}))
            )
            .0,
        409
    );
    no_key.kill();
    let host = launch();
    assert_eq!(
        host.get("/server-info")["contract"]["features"]["agent_portraits"],
        1
    );
    let key = |host: &Host, s: &str| host.get(&format!("/agent-portrait-generation?session={s}"));
    let post = |host: &Host, s: &str| {
        host.ask(
            "POST",
            "/agent-portrait-generation",
            Some(&json!({"session":s})),
        )
    };
    let job = |host: &Host, id: &str| host.get(&format!("/background-jobs/{id}"))["job"].clone();
    let image = |tag: &str| {
        clarp_kernel::python::b64encode(&[b"\x89PNG\r\n\x1a\n", tag.as_bytes()].concat())
    };
    let reply = |tag: &str| json!({"data":[{"b64_json":image(tag)}]}).to_string();
    // Reading capability, an unknown agent, a missing primary and a forbidden controller spend nothing.
    let plain = host.new_agent("Plain");
    assert_eq!(
        key(&host, &plain)["reason"],
        "Add or select a primary portrait first"
    );
    assert_eq!(post(&host, &plain).0, 409);
    assert_eq!(
        post(&host, "absent"),
        (409, json!({"error":"unknown Agent session"}))
    );
    assert_eq!(post(&host, "clarp-message-delegator").0, 403);
    let made = host.post(
        "/agents",
        json!({"backend":"scripted","name":"Canvas","avatar_base64":image("primary")}),
    );
    let sid = made["session"].as_str().unwrap();
    let (limited, _) = pair(&host, "fixture reader", "limited");
    let cap_route = format!("/agent-portrait-generation?session={sid}");
    assert_eq!(
        host.ask_as(&limited, "GET", &cap_route, None).1["available"],
        true
    );
    assert_eq!(
        host.ask_as(
            &limited,
            "POST",
            "/agent-portrait-generation",
            Some(&json!({"session":sid}))
        )
        .0,
        403
    );
    assert!(requests.try_recv().is_err());
    let gallery = |host: &Host| host.get(&format!("/agent-portraits?session={sid}"));
    let original = gallery(&host);
    // The duplicate request reuses a Computer job. The second request is not sent until the first edit succeeds.
    let (status, first) = post(&host, sid);
    assert_eq!(status, 202, "{first}");
    let id = first["job"]["job_id"].as_str().unwrap();
    assert_eq!(first["capability"]["job"], Value::Null);
    assert_eq!(first["job"]["kind"], "portrait-generation");
    assert_eq!(first["job"]["title"], "Generate portraits for Canvas");
    assert_eq!(
        first["job"]["detail"],
        "Generating two character-consistent portrait alternatives"
    );
    assert_eq!(first["job"]["heartbeat_timeout_ms"], 300_000);
    assert_eq!(
        first["job"]["metadata"],
        json!({"session":sid,"provider":"openai","model":"gpt-image-2","expire_queued":true})
    );
    assert_eq!(first["job"]["owner_kind"], "computer");
    assert_eq!(first["job"]["generation"], 1);
    let receive = || {
        requests
            .recv_timeout(Duration::from_secs(20))
            .expect("a portrait edit")
    };
    let (body, finish) = receive();
    assert!(
        body.to_ascii_lowercase()
            .contains("authorization: bearer made-up-key")
    );
    assert!(body.contains("name=\"quality\"\r\n\r\nmedium"));
    assert!(body.contains("name=\"size\"\r\n\r\n1024x1024"));
    assert!(body.starts_with("POST /images/edits "));
    assert!(body.contains("a warm") || body.contains("warm close-up"));
    assert!(body.contains("name=\"image[]\"; filename=\"portrait.png\""));
    assert!(body.contains("primary"));
    assert_eq!(post(&host, sid).1["job"]["generation"], 1);
    assert_eq!(gallery(&host), original);
    assert!(requests.try_recv().is_err());
    finish.send((200, reply("one"))).unwrap();
    let (body, finish) = receive();
    assert!(body.contains("everyday-life scene") && body.contains("primary"));
    assert_eq!(gallery(&host), original);
    finish.send((200, reply("two"))).unwrap();
    wait_for("both alternatives and their publication", || {
        gallery(&host)["portraits"].as_array().unwrap().len() == 3
            && host.get(&format!("/media?session={sid}"))["assets"]
                .as_array()
                .unwrap()
                .len()
                == 2
    });
    let kept = gallery(&host);
    let images = host.get(&format!("/media?session={sid}"));
    let mut captions = Vec::new();
    for pic in images["assets"].as_array().unwrap() {
        assert_eq!(pic["session"], sid);
        assert_eq!(pic["mime_type"], "image/png");
        assert_eq!(pic["created_by"], "portrait_generation:openai");
        captions.push(pic["caption"].as_str().unwrap());
        let id = pic["asset_id"].as_str().unwrap();
        assert!(
            kept["portraits"]
                .as_array()
                .unwrap()
                .iter()
                .any(|p| p["media_asset_id"] == id)
        );
    }
    captions.sort_unstable();
    assert_eq!(
        captions,
        [
            "Generated portrait alternative 1",
            "Generated portrait alternative 2"
        ]
    );
    assert_eq!(kept["primary_portrait_id"], original["primary_portrait_id"]);
    assert_eq!(job(&host, id)["status"], "succeeded");
    // A failed second edit and a cancellation with a request already in flight keep the earlier pair.
    assert_eq!(post(&host, sid).1["job"]["generation"], 2);
    receive().1.send((200, reply("unkept"))).unwrap();
    receive().1.send((500, "made-up-key".into())).unwrap();
    wait_for("the failed pair", || job(&host, id)["status"] == "failed");
    assert!(!job(&host, id).to_string().contains("made-up-key"));
    assert_eq!(gallery(&host), kept);
    assert_eq!(
        host.get(&format!("/media?session={sid}"))["assets"]
            .as_array()
            .unwrap()
            .len(),
        2
    );
    assert_eq!(post(&host, sid).1["job"]["generation"], 3);
    let (_, late) = receive();
    assert_eq!(
        host.ask(
            "DELETE",
            &format!("/background-jobs/{id}?expected_generation=3"),
            None
        )
        .0,
        200
    );
    late.send((200, reply("late"))).unwrap();
    assert_eq!(job(&host, id)["status"], "cancelled");
    assert_eq!(gallery(&host), kept);
    // A Host restart during the first edit fails the saved run. Starting the Host never starts another edit.
    assert_eq!(post(&host, sid).1["job"]["generation"], 4);
    let (_, late) = receive();
    host.kill();
    let host = launch();
    let _ = late.send((200, reply("after restart")));
    assert_eq!(job(&host, id)["status"], "failed");
    assert_eq!(
        job(&host, id)["terminal_reason"],
        "portrait_worker_interrupted"
    );
    assert_eq!(gallery(&host), kept);
    assert!(requests.try_recv().is_err());
    // The worker used the original source. Selecting a different primary during its second
    // request must preserve that selection and retire the two alternates present at commit.
    assert_eq!(post(&host, sid).1["job"]["generation"], 5);
    receive().1.send((200, reply("replacement-one"))).unwrap();
    let (_, finish) = receive();
    let chosen = kept["portraits"][1]["portrait_id"].as_str().unwrap();
    let selected = host.post(
        "/agent-portraits",
        json!({"action":"select_primary","session":sid,"portrait_id":chosen}),
    );
    assert_eq!(selected["primary_portrait_id"], chosen);
    finish.send((200, reply("replacement-two"))).unwrap();
    wait_for("the replacement pair to be materialized", || {
        job(&host, id)["status"] == "succeeded"
            && host.get(&format!("/media?session={sid}"))["assets"]
                .as_array()
                .unwrap()
                .len()
                == 4
    });
    let replaced = gallery(&host);
    assert_eq!(replaced["primary_portrait_id"], chosen);
    let rows = replaced["portraits"].as_array().unwrap();
    assert_eq!(rows.len(), 3);
    assert_eq!(rows.iter().filter(|p| p["role"] == "alternate").count(), 2);
    for prior in kept["portraits"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|p| p["portrait_id"] != chosen)
    {
        assert!(
            rows.iter()
                .all(|p| p["portrait_id"] != prior["portrait_id"])
        );
    }
    // Each attempted launch counts once, including the interrupted one. A fresh agent shares the bound.
    for n in 6..=20 {
        assert_eq!(post(&host, sid).1["job"]["generation"], n);
        let (_, finish) = receive();
        if n == 20 {
            assert_eq!(post(&host, sid).1["job"]["generation"], 20);
        }
        finish.send((503, "fixture refusal".into())).unwrap();
        wait_for("the refused first edit", || {
            job(&host, id)["status"] == "failed"
        });
    }
    let other = host.post(
        "/agents",
        json!({"backend":"scripted","name":"Another","avatar_base64":image("other")}),
    );
    let other = other["session"].as_str().unwrap();
    for who in [sid, other] {
        let cap = key(&host, who);
        assert_eq!(cap["available"], false);
        assert_eq!(
            cap["reason"],
            clarp_kernel::portrait_generation::DAILY_REASON
        );
        assert_eq!(post(&host, who), (409, json!({"error":cap["reason"]})));
    }
    assert!(requests.try_recv().is_err());
}

#[test]
fn the_portrait_job_handles_each_recorded_provider_result() {
    let place = Place::new("portrait-results");
    let (url, requests) = edit_service();
    let host = place.start_in(
        "fixture",
        &[],
        &[
            ("OPENAI_API_KEY", "made-up"),
            ("CLARP_PICTURE_ENDPOINT", &url),
        ],
    );
    let vectors: Value = serde_json::from_str(include_str!(
        "../../../clarp-kernel/tests/fixtures/portrait-generation.json"
    ))
    .unwrap();
    let image = |n: u8| [b"\x89PNG\r\n\x1a\n".as_slice(), &[n]].concat();
    let made=host.post("/agents",json!({"name":"Canvas","backend":"scripted","avatar_base64":clarp_kernel::python::b64encode(&image(0))}));
    let session = made["session"].as_str().unwrap();
    assert_eq!(
        host.ask("POST", "/agent-portrait-generation", Some(&json!([]))),
        (400, json!({"error":"bad json"}))
    );
    let mut published = 0;
    for row in vectors["edits"].as_array().unwrap() {
        let (status, started) = host.ask(
            "POST",
            "/agent-portrait-generation",
            Some(&json!({"session":session})),
        );
        assert_eq!(status, 202, "{started}");
        let job = started["job"]["job_id"].as_str().unwrap();
        let responses = row[0].as_array().unwrap();
        assert_eq!(responses.len(), row[1].as_u64().unwrap() as usize);
        for (idx, response) in responses.iter().enumerate() {
            let (request, tx) = requests.recv_timeout(Duration::from_secs(20)).unwrap();
            assert!(request.starts_with("POST /images/edits "));
            let (status, bytes) = match response.as_str().unwrap() {
                "ok" => (200, image((idx + 1) as u8)),
                "empty" => (200, Vec::new()),
                "gif" => (200, b"GIF89afiction".to_vec()),
                "invalid" => (200, b"not an image".to_vec()),
                "duplicate" => (200, image(1)),
                "raise" => (503, Vec::new()),
                other => panic!("unknown result {other}"),
            };
            tx.send((
                status,
                json!({"data":[{"b64_json":clarp_kernel::python::b64encode(&bytes)}]}).to_string(),
            ))
            .unwrap();
        }
        let succeeds = row[3].as_bool().unwrap();
        let terminal = if succeeds { "succeeded" } else { "failed" };
        wait_for("the recorded result to settle", || {
            host.get(&format!("/background-jobs/{job}"))["job"]["status"] == terminal
        });
        if succeeds {
            published += row[2].as_array().unwrap().len();
        }
        wait_for("the recorded gallery size", || {
            host.get(&format!("/media?session={session}"))["assets"]
                .as_array()
                .unwrap()
                .len()
                == published
        });
        if let Some(error) = row[4].as_str() {
            let why =
                host.get(&format!("/background-jobs/{job}"))["job"]["terminal_reason"].clone();
            if error == "fixture refusal" {
                assert_eq!(why, "OpenAI image edit failed: HTTP 503");
            } else {
                assert_eq!(why, error);
            }
        }
        assert!(requests.try_recv().is_err());
    }
}
