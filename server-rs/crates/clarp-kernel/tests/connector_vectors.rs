//! The connection relay and the rules of trust, against the Python Host's own modules.
//!
//! `fixtures/connector-vectors.json` is written by `server-rs/tools/connector-vectors.py`: the
//! frames the Python connector writes, the headers it hands a request on with, when it dials
//! again, which settings it takes and why not, what it does with each frame through scripts
//! of frames made up there, which requests the Python Host trusts as local, whose failure a
//! failed authentication is, and when failures are answered with a wait. The same must come
//! of the same here.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_kernel::connector::{self, Limits, Said, Settings, Step, Table};
use clarp_kernel::trust::{self, FailureLimiter};
use serde_json::{Map, Value, json};

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/connector-vectors.json")).unwrap()
}

fn rows<'a>(vectors: &'a Value, name: &str) -> &'a Vec<Value> {
    vectors[name].as_array().unwrap()
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

fn unhex(text: &str) -> Vec<u8> {
    let signs = text.as_bytes();
    let pairs = signs.chunks(2);
    pairs
        .map(|pair| u8::from_str_radix(std::str::from_utf8(pair).unwrap(), 16).unwrap())
        .collect()
}

/// Pairs of a name and a value, as a vector writes headers.
fn pairs(row: &Value) -> Vec<(String, String)> {
    let listed = row.as_array().unwrap().iter();
    listed
        .map(|pair| {
            let (name, value) = (pair[0].as_str().unwrap(), pair[1].as_str().unwrap());
            (name.to_string(), value.to_string())
        })
        .collect()
}

/// What Python said went wrong, without the name of its kind.
fn words(refused: &Value) -> (&str, &str) {
    refused.as_str().unwrap().split_once(": ").unwrap()
}

#[test]
fn the_limits_and_the_waits_are_the_python_connectors() {
    let real = &vectors()["real_limits"];
    assert_eq!(real["chunk"], json!(connector::CHUNK));
    assert_eq!(real["max_body"], json!(connector::MAX_BODY));
    assert_eq!(real["max_streams"], json!(connector::MAX_STREAMS));
    let mut hop: Vec<&str> = connector::HOP.to_vec();
    hop.sort_unstable();
    assert_eq!(real["hop"], json!(hop));
    let waits = [
        connector::REDIAL_FAST_S,
        connector::REDIAL_MIN_S,
        connector::REDIAL_MAX_S,
        connector::REDIAL_REPLACED_S,
    ];
    assert_eq!(real["reconnect"], json!(waits));
    assert_eq!(real["stream_gone"], json!(connector::STREAM_GONE.0));
}

#[test]
fn a_frame_is_written_as_the_python_connector_writes_it() {
    let vectors = vectors();
    for row in rows(&vectors, "frames") {
        let kind = u8::try_from(row["kind"].as_u64().unwrap()).unwrap();
        let sid = u32::try_from(row["sid"].as_u64().unwrap()).unwrap();
        let payload = unhex(row["payload"].as_str().unwrap());
        let frame = connector::frame(kind, sid, &payload);
        assert_eq!(hex(&frame), row["frame"].as_str().unwrap());
        assert_eq!(
            connector::parsed(&frame),
            Some((kind, sid, payload.as_slice()))
        );
    }
    for row in rows(&vectors, "json_frames") {
        let kind = u8::try_from(row["kind"].as_u64().unwrap()).unwrap();
        let sid = u32::try_from(row["sid"].as_u64().unwrap()).unwrap();
        let frame = connector::json_frame(kind, sid, &row["said"]);
        assert_eq!(
            hex(&frame),
            row["frame"].as_str().unwrap(),
            "{}",
            row["said"]
        );
    }
    // The frames the connector closes a phone's WebSocket with.
    let gone = connector::closing(9, connector::STREAM_GONE);
    assert_eq!(
        String::from_utf8_lossy(&gone[5..]),
        r#"{"code":1012,"reason":"Host stream gone"}"#
    );
    for short in [&b""[..], b"\x01", b"\x01\x00\x00\x00"] {
        assert_eq!(connector::parsed(short), None);
    }
}

#[test]
fn a_request_is_handed_on_with_the_headers_the_python_connector_hands_it_on_with() {
    let vectors = vectors();
    let (mut handed_on, mut of_the_apps) = (0, 0);
    for row in rows(&vectors, "forwarded") {
        let given = pairs(&row["headers"]);
        let mut meta = Map::new();
        for (name, value) in &given {
            meta.insert(name.clone(), json!(value));
        }
        let body = usize::try_from(row["body"].as_u64().unwrap()).unwrap();
        let passthrough = row["passthrough"].as_bool().unwrap();
        let mine = connector::handed_on(&meta, "127.0.0.1:7690", body, passthrough).unwrap();
        assert_eq!(mine, pairs(&row["handed"]), "{given:?}");
        // The headers the apps mark their own requests with go on. (Until 16 Nov 2026 the
        // Python connector dropped them with the rest of their family: reference defect
        // R25, found by this port and fixed there the same day.)
        let the_apps = |name: &str| {
            let name = name.to_lowercase();
            connector::THE_APPS_OWN.contains(&name.as_str())
        };
        let kept: Vec<_> = mine.iter().filter(|(name, _)| the_apps(name)).collect();
        let asked: Vec<_> = given.iter().filter(|(name, _)| the_apps(name)).collect();
        assert_eq!(kept, asked, "{given:?}");
        of_the_apps += kept.len();
        handed_on += 1;
    }
    assert_eq!(handed_on, 41);
    assert_eq!(of_the_apps, 4);
    // What is no text is no header.
    let mut odd = Map::new();
    odd.insert("x-count".to_string(), json!(5));
    assert_eq!(connector::handed_on(&odd, "127.0.0.1:1", 0, false), None);
}

#[test]
fn an_answers_head_goes_without_what_belongs_to_a_hop() {
    let header = |name: &str, value: &str| (name.to_string(), value.to_string());
    let headers = [
        header("content-type", "application/json"),
        header("Content-Length", "17"),
        header("connection", "close"),
        header("cache-control", "no-cache"),
        header("Transfer-Encoding", "chunked"),
        header("x-a", "1"),
        header("cache-control", "no-store"),
    ];
    let head = connector::head(404, &headers);
    assert_eq!(
        serde_json::to_string(&head).unwrap(),
        r#"{"status":404,"headers":{"content-type":"application/json","cache-control":"no-store","x-a":"1"}}"#
    );
}

#[test]
fn the_relay_is_dialled_again_when_the_python_connector_dials_again() {
    let vectors = vectors();
    for row in rows(&vectors, "delays") {
        let close = &row["close"];
        let received = match (close["rcvd_code"].as_u64(), close["rcvd_reason"].as_str()) {
            (Some(code), Some(reason)) => Some((u16::try_from(code).unwrap(), reason)),
            _ => None,
        };
        let delay = connector::redial_after(
            row["previous"].as_f64(),
            row["was_connected"].as_bool().unwrap(),
            received,
        );
        assert_eq!(json!(delay), row["delay"], "{row}");
    }
    // What a Host that cannot reach the relay waits, attempt after attempt.
    let mut waits = Vec::new();
    let mut previous = None;
    for _ in 0..8 {
        let wait = connector::redial_after(previous, false, None);
        waits.push(wait);
        previous = Some(wait);
    }
    assert_eq!(waits, [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0]);
}

/// The one address this Host reads otherwise, and says so (`trust::urlsplit`): a host named
/// in letters outside ASCII.
const OTHER_LETTERS: &str = "https://r\u{e9}lay.example";

#[test]
fn settings_are_taken_and_refused_as_the_python_host_takes_and_refuses_them() {
    let vectors = vectors();
    let (mut taken, mut refused) = (0, 0);
    for row in rows(&vectors, "settings") {
        let field = |name: &str| row[name].as_str().unwrap();
        let (url, host_id, key) = (field("url"), field("host_id"), field("key"));
        let mine = Settings::new(url, host_id, key);
        if url == OTHER_LETTERS {
            assert_eq!(mine.unwrap_err(), "a host is named in ASCII");
            assert!(row.get("taken").is_some(), "the Python Host takes it");
            continue;
        }
        match (mine, row.get("taken")) {
            (Ok(mine), Some(theirs)) => {
                taken += 1;
                assert_eq!(json!(mine.url), theirs["url"], "{url:?}");
                assert_eq!(json!(mine.pairing_url()), theirs["pairing_url"], "{url:?}");
                assert_eq!(
                    json!(mine.websocket_url()),
                    theirs["websocket_url"],
                    "{url:?}"
                );
                assert_eq!(mine.status(), theirs["status"], "{url:?}");
                let shown = format!("{mine:?}");
                assert!(!shown.contains(key), "the key is never shown");
                if !url.contains('\\') {
                    assert_eq!(json!(shown), theirs["shown"]);
                }
                let dialled = mine.connect_url();
                let after = format!("/connect?host={host_id}&key={key}");
                assert!(
                    dialled.starts_with("wss://") && dialled.ends_with(&after),
                    "{url:?}"
                );
            }
            (Err(mine), None) => {
                refused += 1;
                assert_eq!(mine, words(&row["refused"]).1, "{url:?} {host_id:?}");
            }
            (mine, theirs) => panic!("{url:?} {host_id:?}: {mine:?} and {theirs:?}"),
        }
    }
    assert_eq!((taken, refused), (33, 55));
}

#[test]
fn a_file_of_settings_is_read_as_the_python_host_reads_it() {
    let vectors = vectors();
    let (mut taken, mut refused) = (0, 0);
    for row in rows(&vectors, "files") {
        let name = row["name"].as_str().unwrap();
        // Whether there is a file at all is the Host's to see.
        let Some(text) = row["text"].as_str() else {
            assert_eq!(row["refused"], "FileNotFoundError");
            continue;
        };
        let mode = u32::try_from(row["mode"].as_u64().unwrap()).unwrap();
        match (Settings::read(text.as_bytes(), mode), row.get("taken")) {
            (Ok(mine), Some(theirs)) => {
                taken += 1;
                assert_eq!(&mine.status(), theirs, "{name}");
            }
            (Err(mine), None) => {
                refused += 1;
                assert_eq!(mine, words(&row["refused"]).1, "{name}");
            }
            (mine, theirs) => panic!("{name}: {mine:?} and {theirs:?}"),
        }
    }
    assert_eq!((taken, refused), (4, 17));
}

#[test]
fn the_connector_does_with_each_frame_what_the_python_connector_does() {
    let vectors = vectors();
    let small = &vectors["limits_made_small"];
    let limits = Limits {
        max_body: usize::try_from(small["max_body"].as_u64().unwrap()).unwrap(),
        max_streams: usize::try_from(small["max_streams"].as_u64().unwrap()).unwrap(),
    };
    let (mut frames, mut ended) = (0, 0);
    for script in rows(&vectors, "scripts") {
        let name = script["name"].as_str().unwrap();
        let mut table = Table::new(limits);
        for (at, row) in script["steps"].as_array().unwrap().iter().enumerate() {
            let at = format!("{name}, step {at}");
            let mut sent: Vec<String> = Vec::new();
            let mut opened = json!([]);
            let mut heard: Option<(u32, Value)> = None;
            if let Some(sid) = row["done"].as_u64() {
                table.forget(u32::try_from(sid).unwrap());
                assert_eq!(row["error"], Value::Null, "{at}");
            } else {
                frames += 1;
                let data = unhex(row["data"].as_str().unwrap());
                match table.take(&data) {
                    Err(violation) => {
                        ended += 1;
                        let (class, said) = words(&row["error"]);
                        assert_eq!(violation.class, class, "{at}");
                        if class == "ValueError" {
                            assert_eq!(violation.words, said, "{at}");
                        }
                        // The session is over: nothing of it is looked at any more.
                        continue;
                    }
                    Ok(step) => {
                        assert_eq!(row["error"], Value::Null, "{at}: {step:?}");
                        match step {
                            Step::Nothing | Step::Body { .. } | Step::Whole { .. } => {}
                            Step::Request { sid, meta } | Step::Socket { sid, meta }
                                if row["opened"][0]["meta"].is_object() =>
                            {
                                let theirs = &row["opened"][0];
                                let kind = theirs["kind"].as_str().unwrap();
                                let said = &theirs["meta"];
                                assert_eq!(json!(meta.path), said["path"], "{at}");
                                assert_eq!(json!(meta.headers), said["headers"], "{at}");
                                let method = said.get("method").cloned();
                                assert_eq!(meta.method, method.unwrap_or(json!("GET")), "{at}");
                                assert!(!meta.passthrough, "{at}");
                                opened = json!([{ "sid": sid, "kind": kind, "meta": said }]);
                            }
                            Step::Request { .. } | Step::Socket { .. } => {
                                panic!("{at}: opened here and not there")
                            }
                            Step::Heard { sid, said } => {
                                let said = match said {
                                    Said::Text(text) => json!({ "text": text }),
                                    Said::Bytes(bytes) => json!({ "bytes": hex(bytes) }),
                                };
                                heard = Some((sid, said));
                            }
                            Step::Over { tell, .. } => {
                                if let Some(told) = tell {
                                    sent.push(hex(&told));
                                }
                            }
                            Step::Tell(frame) => sent.push(hex(&frame)),
                        }
                    }
                }
            }
            assert_eq!(json!(sent), row["sent"], "{at}");
            assert_eq!(opened, row["opened"], "{at}");
            let theirs = row["streams"].as_object().unwrap();
            let mine = table.shown();
            let numbers: Vec<String> = mine.iter().map(|shown| shown.0.to_string()).collect();
            let their_numbers: Vec<&String> = theirs.keys().collect();
            assert_eq!(json!(numbers), json!(their_numbers), "{at}");
            for (sid, is_request, has_ended, held) in mine {
                let stream = &theirs[&sid.to_string()];
                assert_eq!(json!(is_request), stream["http"], "{at}");
                assert_eq!(json!(has_ended), stream["ended"], "{at}");
                assert_eq!(stream["cancelled"], false, "{at}");
                let body = table.body_so_far(sid);
                assert_eq!(json!(hex(body)), stream["body"], "{at}");
                assert_eq!(held, body.len(), "{at}");
                let said = match &heard {
                    Some((to, said)) if *to == sid => json!([said]),
                    _ => json!([]),
                };
                assert_eq!(said, stream["heard"], "{at}");
            }
            assert_eq!(json!(table.held()), row["buffered"], "{at}");
        }
    }
    assert_eq!((frames, ended), (109, 20));
}

#[test]
fn a_whole_requests_body_is_handed_over_once_and_held_until_its_work_is_done() {
    let mut table = Table::new(Limits::default());
    let meta = br#"{"method":"POST","path":"/send","headers":{},"passthrough_encoding":true}"#;
    let begun = connector::frame(connector::REQ, 7, meta);
    let opened = table.take(&begun);
    let Ok(Step::Request { sid: 7, meta }) = opened else {
        panic!("{opened:?}")
    };
    assert!(meta.passthrough);
    assert_eq!(meta.method, json!("POST"));
    for part in [&b"{\"text\":"[..], b"\"hi\"}"] {
        let part = connector::frame(connector::REQ_BODY, 7, part);
        assert_eq!(table.take(&part), Ok(Step::Body { sid: 7 }));
    }
    let end = connector::frame(connector::REQ_END, 7, &[]);
    assert_eq!(table.take(&end), Ok(Step::Whole { sid: 7 }));
    assert_eq!(table.body(7), b"{\"text\":\"hi\"}");
    assert_eq!(table.body(7), b"");
    assert_eq!((table.held(), table.counted()), (13, (1, 0)));
    table.forget(7);
    assert_eq!((table.held(), table.counted()), (0, (0, 0)));
    // A phone that says more than is taken loses its WebSocket, and the relay is told.
    let socket = br#"{"path":"/terminal/x","headers":{}}"#;
    let asked = connector::frame(connector::WS_OPEN, 8, socket);
    assert!(matches!(
        table.take(&asked),
        Ok(Step::Socket { sid: 8, .. })
    ));
    assert_eq!(table.counted(), (0, 1));
    let told = table.heard_too_much(8);
    assert_eq!(told, connector::frame(connector::ABORT, 8, &[]));
    assert_eq!(table.counted(), (0, 0));
}

#[test]
fn a_peer_is_this_machine_when_the_python_host_says_so() {
    let vectors = vectors();
    for row in rows(&vectors, "peers") {
        let peer = row["peer"].as_str().unwrap();
        assert_eq!(json!(trust::is_loopback(peer)), row["loopback"], "{peer:?}");
    }
}

#[test]
fn a_request_is_local_when_the_python_host_says_so() {
    let vectors = vectors();
    let (mut local, mut remote) = (0, 0);
    for row in rows(&vectors, "local") {
        let peer = row["peer"].as_str().unwrap();
        let headers = pairs(&row["headers"]);
        let mine = trust::local_request(peer, &headers);
        assert_eq!(json!(mine), row["local"], "{peer:?} {headers:?}");
        if mine {
            local += 1;
        } else {
            remote += 1;
        }
    }
    assert_eq!((local, remote), (34, 54));
    // A page's address in letters this Host does not read is no page of this machine.
    let other = [("Origin".to_string(), "http://loc\u{e4}lhost".to_string())];
    assert!(!trust::local_request("127.0.0.1", &other));
}

#[test]
fn a_failure_is_counted_for_whom_the_python_host_counts_it() {
    let vectors = vectors();
    for row in rows(&vectors, "sources") {
        let peer = row["peer"].as_str().unwrap();
        let headers = pairs(&row["headers"]);
        let mine = trust::failure_source(peer, &headers);
        assert_eq!(json!(mine), row["source"], "{peer:?} {headers:?}");
    }
}

#[test]
fn failures_are_answered_with_a_wait_when_the_python_host_answers_so() {
    let vectors = vectors();
    let (mut counted, mut told_to_wait) = (0, 0);
    for script in rows(&vectors, "limits") {
        let case = &script["case"];
        let name = case["name"].as_str().unwrap();
        let mut limiter = FailureLimiter::new(
            u32::try_from(case["limit"].as_u64().unwrap()).unwrap(),
            case["window"].as_f64().unwrap(),
            usize::try_from(case["max_sources"].as_u64().unwrap()).unwrap(),
        );
        // The clock the generator made up, moved as it moved it.
        let mut now = 1000.0;
        for (at, row) in script["steps"].as_array().unwrap().iter().enumerate() {
            now += row["gone"].as_f64().unwrap();
            let retry = limiter.failure(row["source"].as_str().unwrap(), now);
            assert_eq!(json!(retry), row["retry"], "{name}, step {at}");
            assert_eq!(
                json!(limiter.remembered()),
                row["kept"],
                "{name}, step {at}"
            );
            counted += 1;
            if retry > 0 {
                told_to_wait += 1;
            }
        }
    }
    assert_eq!(counted, 82);
    assert_eq!(told_to_wait, 23);
    // The budget of a Host: twenty failures in a minute, and the twenty-first waits.
    let mut limiter = FailureLimiter::default();
    for _ in 0..20 {
        assert_eq!(limiter.failure("203.0.113.7", 5.0), 0);
    }
    assert_eq!(limiter.failure("203.0.113.7", 5.5), 60);
    assert_eq!(limiter.failure("198.51.100.1", 5.5), 0);
}
