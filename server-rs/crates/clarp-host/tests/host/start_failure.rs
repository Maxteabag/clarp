use super::*;

/// One request to the Host, without a token: its status and body, or nothing when it does not
/// answer within two seconds.
fn answer(address: &str, path: &str) -> Option<(u16, Value)> {
    let mut stream = TcpStream::connect(address).ok()?;
    stream.set_read_timeout(Some(Duration::from_secs(2))).ok()?;
    let asked = format!("GET {path} HTTP/1.1\r\nHost: test\r\nConnection: close\r\n\r\n");
    stream.write_all(asked.as_bytes()).ok()?;
    let mut said = String::new();
    stream.read_to_string(&mut said).ok()?;
    let (head, body) = said.split_once("\r\n\r\n")?;
    let status = head.split_whitespace().nth(1)?.parse().ok()?;
    Some((status, serde_json::from_str(body).unwrap_or(Value::Null)))
}

/// A start that fails after the Host has begun to answer ends the Host with a failure (issue
/// 935). Its liveness was answered and said which run it is; that run never said `started`, nor
/// that it listens: an installer that waits for the run it saw to say `started` holds it failed.
#[test]
fn a_start_that_fails_after_the_host_answers_ends_it_and_never_says_started() {
    let place = Place::new("late-start-failure");
    let port = TcpListener::bind("127.0.0.1:0")
        .unwrap()
        .local_addr()
        .unwrap()
        .port();
    let address = format!("127.0.0.1:{port}");
    let fail = place.root.join("fail-now");
    let mut host = place
        .host_command()
        .args(["--data", place.root.join("data").to_str().unwrap()])
        .args(["--run", place.root.join("run").to_str().unwrap()])
        .args(["--cwd", place.root.join("work").to_str().unwrap()])
        .args(["--listen", &address])
        .arg("--network-down-file")
        .arg(place.network_down())
        .args(["--lane-limits-default", "fixed"])
        .arg("--no-lane-pressure")
        .env("CLARP_HOST_TOKEN", "")
        .env("CLARP_TEST_START_FAILS_WHEN", &fail)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let mut said = host.stderr.take().unwrap();
    let heard = std::thread::spawn(move || {
        let mut all = String::new();
        let _ = said.read_to_string(&mut all);
        all
    });

    // The Host answers that it is there before its start is done, and says how far it is.
    let deadline = Instant::now() + Duration::from_secs(30);
    let started = loop {
        if let Some((200, started)) = answer(&address, "/host/start") {
            break started;
        }
        assert!(Instant::now() < deadline, "the Host never answered its liveness");
        std::thread::sleep(Duration::from_millis(20));
    };
    let run = started["instance"].as_str().unwrap().to_string();
    assert_ne!(started["stage"], "started", "{started}");
    assert_eq!(answer(&address, "/server-info").map(|(status, _)| status), Some(200));

    // A step of its start fails: the Host ends, failed, and that run never said `started`.
    std::fs::write(&fail, []).unwrap();
    let deadline = Instant::now() + Duration::from_secs(30);
    let status = loop {
        if let Some(status) = host.try_wait().unwrap() {
            break status;
        }
        if let Some((200, now)) = answer(&address, "/host/start") {
            assert_eq!(now["instance"], run.as_str());
            assert_ne!(now["stage"], "started", "{now}");
        }
        assert!(Instant::now() < deadline, "the Host did not end when its start failed");
        std::thread::sleep(Duration::from_millis(20));
    };
    assert!(!status.success());
    let said = heard.join().unwrap();
    assert!(said.contains("a step of the start failed"), "{said}");
    assert!(!said.contains("clarp-host listens on"), "{said}");
}
