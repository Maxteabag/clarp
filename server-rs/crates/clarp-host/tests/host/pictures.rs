//! The Host's tests of the pictures a Host makes: one file a piece, declared in `host.rs`.
//!
//! A contact nobody gave a picture is drawn one only where the installation said so
//! (`--contact-pictures`). Every test here gives the Host a stand-in for the provider of
//! pictures on this machine; none reaches a provider.

use super::*;

/// A picture as the stand-in answers it: made up, of nobody.
fn made_up_answer() -> String {
    let made_up = b"\xff\xd8\xff\xe0 a made-up picture of nobody";
    let encoded = clarp_kernel::python::b64encode(made_up);
    json!({ "data": [{ "b64_json": encoded }] }).to_string()
}

#[test]
fn a_host_not_told_to_draw_pictures_asks_the_provider_nothing_at_its_start_nor_for_a_minted_contact()
 {
    let (provider, asked) = answers(vec![("/images", "200 OK", made_up_answer())]);
    let endpoint = format!("{provider}/images");
    let world = [
        ("CLARP_PICTURE_ENDPOINT", endpoint.as_str()),
        ("OPENAI_API_KEY", "a-key-of-the-test"),
    ];
    // Without the word: a contact is minted, and nothing is asked of the provider.
    let place = Place::new("pictures-off");
    let host = place.start_in("", &[], &world);
    let (status, made) = host.ask(
        "POST",
        "/agents",
        Some(&json!({ "backend": "claude", "mint_contact": true })),
    );
    assert_eq!(status, 200, "{made}");
    std::thread::sleep(Duration::from_millis(500));
    assert!(
        asked.lock().unwrap().is_empty(),
        "{:?}",
        asked.lock().unwrap()
    );
    drop(host);
    // With the word, nothing is asked at the start either.
    let place = Place::new("pictures-on");
    let host = place.start_in("", &["--contact-pictures"], &world);
    std::thread::sleep(Duration::from_millis(500));
    assert!(
        asked.lock().unwrap().is_empty(),
        "{:?}",
        asked.lock().unwrap()
    );
    drop(host);
}

#[test]
fn the_install_tool_starts_the_host_with_contact_pictures_only_where_it_was_told_on_and_keeps_that()
{
    let tool = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tools/install-host");
    let place = Place::new("pictures-install");
    // Binaries of nothing, as a build would leave them, and a home of the test's own.
    let from = place.root.join("from");
    std::fs::create_dir_all(&from).unwrap();
    for name in ["clarp-host", "clarp-shim"] {
        program(&from.join(name), "#!/bin/sh\n");
    }
    let sums = Command::new("sha256sum")
        .args(["clarp-host", "clarp-shim"])
        .current_dir(&from)
        .output()
        .unwrap();
    std::fs::write(from.join("SHA256SUMS"), sums.stdout).unwrap();
    let home = place.root.join("home");
    let unit = home.join(".config/systemd/user/clarp-host.service");
    let kept = home.join(".config/clarp-host/install.conf");
    let install = |words: &[&str]| -> String {
        let done = Command::new("bash")
            .arg(&tool)
            .args(["--from", from.to_str().unwrap(), "--files-only"])
            .args(words)
            .env("HOME", &home)
            .env("XDG_CONFIG_HOME", home.join(".config"))
            .env("XDG_DATA_HOME", home.join(".local/share"))
            .output()
            .unwrap();
        assert!(
            done.status.success(),
            "{}",
            String::from_utf8_lossy(&done.stderr)
        );
        let started = std::fs::read_to_string(&unit).unwrap();
        let line = started
            .lines()
            .find(|line| line.starts_with("ExecStart="))
            .unwrap();
        line.to_string()
    };
    let told = |key: &str| {
        let kept = std::fs::read_to_string(&kept).unwrap_or_default();
        kept.lines().any(|line| line == key)
    };
    assert!(
        !install(&[]).contains("--contact-pictures"),
        "off where nothing was said"
    );
    assert!(install(&["--contact-pictures", "on"]).contains(" --contact-pictures"));
    assert!(told("contact-pictures=on"));
    assert!(
        install(&[]).contains(" --contact-pictures"),
        "kept with the installation"
    );
    assert!(!install(&["--contact-pictures", "off"]).contains("--contact-pictures"));
    assert!(!install(&["--contact-pictures", "default"]).contains("--contact-pictures"));
    let refused = Command::new("bash")
        .arg(&tool)
        .args([
            "--from",
            from.to_str().unwrap(),
            "--files-only",
            "--contact-pictures",
            "maybe",
        ])
        .env("HOME", &home)
        .env("XDG_CONFIG_HOME", home.join(".config"))
        .env("XDG_DATA_HOME", home.join(".local/share"))
        .output()
        .unwrap();
    assert_eq!(refused.status.code(), Some(2));
}

/// A Host told to draw pictures, with a stand-in for the provider at `provider`.
fn drawing(place: &Place, provider: &str) -> Host {
    let endpoint = format!("{provider}/images");
    let world = [
        ("CLARP_PICTURE_ENDPOINT", endpoint.as_str()),
        ("OPENAI_API_KEY", "a-key-of-the-test"),
    ];
    place.start_in("", &["--contact-pictures"], &world)
}

/// The picture's job of the persona of this name, as the routes show it.
fn picture_job(host: &Host, name: &str) -> Value {
    let personas = host.get("/agents/snapshot")["personas"].clone();
    let persona = personas
        .as_array()
        .unwrap()
        .iter()
        .find(|persona| persona["name"] == name)
        .cloned()
        .unwrap();
    let id = clarp_host::pictures::job_id(persona["id"].as_str().unwrap());
    host.get(&format!("/background-jobs/{id}"))
}

#[test]
fn a_minted_contact_is_drawn_its_picture_once_and_its_agent_wears_it() {
    let (provider, asked) = answers(vec![("/images", "200 OK", made_up_answer())]);
    let place = Place::new("pictures-made");
    let host = drawing(&place, &provider);
    let events = host.listen();
    let (status, made) = host.ask(
        "POST",
        "/agents",
        Some(&json!({ "backend": "claude", "mint_contact": true })),
    );
    assert_eq!((status, &made["name"]), (200, &json!("Clara")), "{made}");
    wait_for("the job to end", || {
        picture_job(&host, "Clara")["job"]["status"] == "succeeded"
    });
    // Asked once, as the Python Host asks: its model, its prompt for the contact, the key.
    let asked = asked.lock().unwrap().clone();
    assert_eq!(asked.len(), 1, "{asked:?}");
    assert!(
        asked[0].contains("gpt-image-2") && asked[0].contains("of Clara"),
        "{}",
        asked[0]
    );
    assert!(asked[0].contains("Bearer a-key-of-the-test"));
    let job = picture_job(&host, "Clara");
    assert_eq!(job["job"]["title"], "Generate avatar for Clara");
    assert_eq!(job["job"]["owner_kind"], "computer");
    // Kept on the contact and on its agent, which had none of its own.
    let snapshot = host.get("/agents/snapshot");
    let clara = snapshot["personas"]
        .as_array()
        .unwrap()
        .iter()
        .find(|persona| persona["name"] == "Clara")
        .cloned()
        .unwrap();
    assert!(!clara["avatar_url"].as_str().unwrap().is_empty(), "{clara}");
    let session = made["session"].as_str().unwrap();
    let avatar = fetch(&host, "", "GET", &format!("/avatars/{session}"), &[], b"");
    assert_eq!(avatar.status, 200);
    assert!(
        avatar.body.starts_with(b"\xff\xd8\xff"),
        "the agent wears the picture made"
    );
    wait_for("the apps told", || {
        let heard = events.lock().unwrap();
        heard
            .iter()
            .any(|event| event["kind"] == "persona-updated" && event["persona"] == "Clara")
    });
}

#[test]
fn a_picture_that_failed_waits_and_its_reason_holds_no_key() {
    let (provider, asked) = answers(vec![(
        "/images",
        "500 Internal Server Error",
        "no picture for a-key-of-the-test".to_string(),
    )]);
    let place = Place::new("pictures-failed");
    let host = drawing(&place, &provider);
    let events = host.listen();
    let (status, kit) = host.ask("POST", "/personas", Some(&json!({ "name": "Kit" })));
    assert_eq!(status, 201, "{kit}");
    wait_for("the job to end", || {
        picture_job(&host, "Kit")["job"]["status"] == "failed"
    });
    let reason = picture_job(&host, "Kit")["job"]["terminal_reason"].clone();
    let reason = reason.as_str().unwrap();
    assert!(
        reason.contains("HTTP 500") && !reason.contains("a-key-of-the-test"),
        "{reason}"
    );
    wait_for("the apps told", || {
        let heard = events.lock().unwrap();
        heard
            .iter()
            .any(|event| event["kind"] == "persona-updated" && event["persona"] == "Kit")
    });
    // Its contact launched again inside its wait: nothing is asked.
    let (status, made) = host.ask(
        "POST",
        "/agents",
        Some(&json!({ "name": "Kit", "backend": "claude" })),
    );
    assert_eq!(status, 200, "{made}");
    std::thread::sleep(Duration::from_millis(800));
    assert_eq!(asked.lock().unwrap().len(), 1);
    assert_eq!(picture_job(&host, "Kit")["job"]["generation"], 1);
}

#[test]
fn a_picture_being_made_when_the_host_stopped_ends_failed_as_a_try_and_nothing_is_asked_at_the_start()
 {
    // A provider that takes the question and never answers.
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let provider = format!("http://{}", listener.local_addr().unwrap());
    let asked = Arc::new(AtomicU64::new(0));
    let counting = asked.clone();
    std::thread::spawn(move || {
        let mut held = Vec::new();
        for stream in listener.incoming() {
            counting.fetch_add(1, Ordering::SeqCst);
            held.push(stream);
        }
    });
    let place = Place::new("pictures-stopped");
    let host = drawing(&place, &provider);
    let (status, made) = host.ask(
        "POST",
        "/agents",
        Some(&json!({ "backend": "claude", "mint_contact": true })),
    );
    assert_eq!(status, 200, "{made}");
    wait_for("the provider asked", || asked.load(Ordering::SeqCst) == 1);
    assert_eq!(picture_job(&host, "Clara")["job"]["status"], "running");
    // While it is under way, the contact and its agent say so.
    let snapshot = host.get("/agents/snapshot");
    let listed = snapshot["personas"].as_array().unwrap();
    let clara = listed
        .iter()
        .find(|persona| persona["name"] == "Clara")
        .unwrap();
    assert_eq!(clara["avatar_generation_status"], "running", "{clara}");
    let rows = snapshot["agents"].as_array().unwrap();
    let row = rows
        .iter()
        .find(|row| row["session"] == made["session"])
        .unwrap();
    assert_eq!(row["avatar_generation_status"], "running", "{row}");
    host.kill();
    let host = drawing(&place, &provider);
    wait_for("the job ended", || {
        picture_job(&host, "Clara")["job"]["status"] == "failed"
    });
    let job = picture_job(&host, "Clara");
    assert_eq!(
        job["job"]["terminal_reason"],
        "persona_avatar_worker_interrupted"
    );
    assert_eq!(job["job"]["generation"], 1);
    let personas = host.get("/agents/snapshot")["personas"].clone();
    let listed = personas.as_array().unwrap();
    let clara = listed
        .iter()
        .find(|persona| persona["name"] == "Clara")
        .unwrap();
    assert_eq!(clara["avatar_generation_status"], Value::Null, "{clara}");
    std::thread::sleep(Duration::from_millis(800));
    assert_eq!(
        asked.load(Ordering::SeqCst),
        1,
        "nothing is asked at a start"
    );
}

#[test]
fn a_picture_cancelled_while_it_is_made_is_not_kept() {
    // A provider that answers only when the test lets it.
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let provider = format!("http://{}", listener.local_addr().unwrap());
    let (release, released) = std::sync::mpsc::channel::<()>();
    let asked = Arc::new(AtomicU64::new(0));
    let counting = asked.clone();
    std::thread::spawn(move || {
        for stream in listener.incoming() {
            let Ok(mut stream) = stream else { break };
            let _ = whole_request(&mut stream);
            counting.fetch_add(1, Ordering::SeqCst);
            let _ = released.recv();
            let body = made_up_answer();
            let reply = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\
                 Connection: close\r\n\r\n{body}",
                body.len()
            );
            let _ = stream.write_all(reply.as_bytes());
        }
    });
    let place = Place::new("pictures-cancelled");
    let host = drawing(&place, &provider);
    let (status, made) = host.ask(
        "POST",
        "/agents",
        Some(&json!({ "backend": "claude", "mint_contact": true })),
    );
    assert_eq!(status, 200, "{made}");
    wait_for("the provider asked", || asked.load(Ordering::SeqCst) == 1);
    let job = picture_job(&host, "Clara");
    let id = job["job"]["job_id"].as_str().unwrap().to_string();
    let (status, ended) = host.ask(
        "DELETE",
        &format!("/background-jobs/{id}"),
        Some(&json!({ "expected_generation": 1 })),
    );
    assert_eq!(status, 200, "{ended}");
    release.send(()).unwrap();
    std::thread::sleep(Duration::from_millis(1500));
    // The answer came after the cancel: this try is no more, and nothing of it is kept.
    let job = picture_job(&host, "Clara");
    assert_eq!(job["job"]["status"], "cancelled", "{job}");
    let personas = host.get("/agents/snapshot")["personas"].clone();
    let listed = personas.as_array().unwrap();
    let clara = listed
        .iter()
        .find(|persona| persona["name"] == "Clara")
        .unwrap();
    assert_eq!(clara["avatar_url"], "", "{clara}");
}
