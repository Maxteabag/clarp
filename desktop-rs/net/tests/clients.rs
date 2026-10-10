//! Clients against a scripted local HTTP server. Includes ports of
//! tst_native_core::apiClientRejectsCrossOriginAuthenticatedMedia and
//! apiClientDropsRepliesFromPreviousEndpointGeneration.

use std::sync::{Arc, Mutex};
use std::time::Duration;

use clarp_core::endpoint::SseTiming;
use clarp_net::{ApiClient, ApiReply, SseClient, SseSignal};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::mpsc::{UnboundedReceiver, unbounded_channel};
use tokio::time::timeout;
use url::Url;

async fn read_request(socket: &mut TcpStream) -> String {
    let mut buffer = Vec::new();
    let mut chunk = [0u8; 4096];
    while !buffer.windows(4).any(|w| w == b"\r\n\r\n") {
        match socket.read(&mut chunk).await {
            Ok(0) | Err(_) => break,
            Ok(n) => buffer.extend_from_slice(&chunk[..n]),
        }
    }
    String::from_utf8_lossy(&buffer).into_owned()
}

fn json_response(body: &str) -> String {
    format!(
        "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    )
}

fn header<'a>(request: &'a str, name: &str) -> Option<&'a str> {
    request.lines().find_map(|line| {
        let (key, value) = line.split_once(':')?;
        key.eq_ignore_ascii_case(name).then(|| value.trim())
    })
}

async fn listen() -> (TcpListener, Url) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = Url::parse(&format!("http://{}", listener.local_addr().unwrap())).unwrap();
    (listener, url)
}

fn api_client() -> (ApiClient, UnboundedReceiver<ApiReply>) {
    let (tx, rx) = unbounded_channel();
    (ApiClient::new(tokio::runtime::Handle::current(), move |reply| { let _ = tx.send(reply); }), rx)
}

async fn next<T>(rx: &mut UnboundedReceiver<T>) -> T {
    timeout(Duration::from_secs(3), rx.recv()).await.expect("reply within 3s").expect("channel open")
}

#[tokio::test]
async fn api_client_rejects_cross_origin_authenticated_media() {
    let (client, mut replies) = api_client();
    client.set_endpoint(Url::parse("https://clarp.example.test").unwrap(), "secret-token");
    client.get_bytes("avatar:external", "https://evil.example.test/portrait.png");
    match next(&mut replies).await {
        ApiReply::Failed { tag, message, .. } => {
            assert_eq!(tag, "avatar:external");
            assert!(message.contains("cross-origin"), "{message}");
        }
        other => panic!("unexpected {other:?}"),
    }

    // A same-origin request that redirects elsewhere must fail before the
    // credential ever reaches the foreign server.
    let (foreign, foreign_url) = listen().await;
    let foreign_connections = Arc::new(Mutex::new(0));
    let counter = foreign_connections.clone();
    tokio::spawn(async move {
        while let Ok((socket, _)) = foreign.accept().await {
            *counter.lock().unwrap() += 1;
            drop(socket);
        }
    });
    let (redirect, redirect_url) = listen().await;
    let location = foreign_url.join("portrait.png").unwrap();
    tokio::spawn(async move {
        while let Ok((mut socket, _)) = redirect.accept().await {
            read_request(&mut socket).await;
            let response = format!("HTTP/1.1 302 Found\r\nLocation: {location}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n");
            let _ = socket.write_all(response.as_bytes()).await;
        }
    });
    client.set_endpoint(redirect_url, "secret-token");
    client.get_bytes("avatar:redirect", "/avatar.png");
    match next(&mut replies).await {
        ApiReply::Failed { tag, message, .. } => {
            assert_eq!(tag, "avatar:redirect");
            assert!(message.contains("redirect"), "{message}");
        }
        other => panic!("unexpected {other:?}"),
    }
    tokio::time::sleep(Duration::from_millis(100)).await;
    assert_eq!(*foreign_connections.lock().unwrap(), 0);
}

#[tokio::test]
async fn api_client_drops_replies_from_previous_endpoint_generation() {
    let (old, old_url) = listen().await;
    let (current, current_url) = listen().await;
    let (release_tx, release_rx) = tokio::sync::oneshot::channel::<()>();
    let (accepted_tx, accepted_rx) = tokio::sync::oneshot::channel::<()>();
    tokio::spawn(async move {
        let (mut socket, _) = old.accept().await.unwrap();
        read_request(&mut socket).await;
        let _ = accepted_tx.send(());
        let _ = release_rx.await;
        let _ = socket.write_all(json_response(r#"{"source":"stale"}"#).as_bytes()).await;
    });
    tokio::spawn(async move {
        while let Ok((mut socket, _)) = current.accept().await {
            read_request(&mut socket).await;
            let _ = socket.write_all(json_response(r#"{"source":"current"}"#).as_bytes()).await;
        }
    });
    let (client, mut replies) = api_client();
    client.set_endpoint(old_url, "");
    client.get("old", "/slow", &[]);
    timeout(Duration::from_secs(2), accepted_rx).await.unwrap().unwrap();
    client.set_endpoint(current_url, "");
    release_tx.send(()).unwrap();
    tokio::time::sleep(Duration::from_millis(100)).await;
    assert!(replies.try_recv().is_err(), "stale reply must be dropped");

    client.get("current", "/now", &[]);
    match next(&mut replies).await {
        ApiReply::Json { tag, object } => {
            assert_eq!(tag, "current");
            assert_eq!(object["source"], "current");
        }
        other => panic!("unexpected {other:?}"),
    }
}

#[tokio::test]
async fn api_requests_carry_auth_and_report_host_errors() {
    let (server, url) = listen().await;
    let requests = Arc::new(Mutex::new(Vec::new()));
    let seen = requests.clone();
    tokio::spawn(async move {
        let mut index = 0;
        while let Ok((mut socket, _)) = server.accept().await {
            let request = read_request(&mut socket).await;
            seen.lock().unwrap().push(request);
            let response = match index {
                0 => json_response(r#"{"ok":true}"#),
                1 => {
                    let body = r#"{"message":"agent is busy"}"#;
                    format!("HTTP/1.1 409 Conflict\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}", body.len())
                }
                _ => json_response("[1,2]"),
            };
            index += 1;
            let _ = socket.write_all(response.as_bytes()).await;
        }
    });
    let (client, mut replies) = api_client();
    client.set_endpoint(url.join("base").unwrap(), "tok");
    client.post_json("send", "/send", serde_json::json!({"text": "hi"}), Some(Duration::from_secs(5)));
    assert!(matches!(next(&mut replies).await, ApiReply::Json { tag, .. } if tag == "send"));
    client.delete("stop", "stop");
    match next(&mut replies).await {
        ApiReply::Failed { message, status, .. } => assert_eq!((message.as_str(), status), ("agent is busy", 409)),
        other => panic!("unexpected {other:?}"),
    }
    client.get("list", "/log", &[("session", "rachel"), ("limit", "50")]);
    assert!(matches!(next(&mut replies).await, ApiReply::Failed { message, .. } if message.contains("not an object")));
    let requests = requests.lock().unwrap();
    assert!(requests[0].starts_with("POST /base/send "), "{}", requests[0]);
    assert_eq!(header(&requests[0], "authorization"), Some("Bearer tok"));
    assert_eq!(header(&requests[0], "content-type"), Some("application/json"));
    assert!(requests[1].starts_with("DELETE /base/stop "));
    assert!(requests[2].starts_with("GET /base/log?session=rachel&limit=50 "), "{}", requests[2]);
}

fn fast_timing() -> SseTiming {
    SseTiming {
        watchdog: Duration::from_millis(300),
        initial_reconnect: Duration::from_millis(20),
        maximum_reconnect: Duration::from_millis(100),
    }
}

fn sse_client() -> (SseClient, UnboundedReceiver<SseSignal>) {
    let (tx, rx) = unbounded_channel();
    (SseClient::new(tokio::runtime::Handle::current(), fast_timing(), move |s| { let _ = tx.send(s); }), rx)
}

const SSE_HEADERS: &str = "HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-cache\r\n\r\n";

#[tokio::test]
async fn sse_reconnects_and_resumes_from_the_last_event_id() {
    let (server, url) = listen().await;
    let requests = Arc::new(Mutex::new(Vec::new()));
    let seen = requests.clone();
    tokio::spawn(async move {
        let mut index = 0;
        while let Ok((mut socket, _)) = server.accept().await {
            let request = read_request(&mut socket).await;
            seen.lock().unwrap().push(request);
            let body = if index == 0 {
                ": connected\n\nid: 1\ndata: {\"type\":\"agent-state\"}\n\nid: 2\ndata: {\"type\":\"transcript-updated\"}\n\n"
            } else {
                "id: 3\ndata: {\"type\":\"agent-roster\"}\n\n"
            };
            index += 1;
            let _ = socket.write_all(format!("{SSE_HEADERS}{body}").as_bytes()).await;
            if index > 1 {
                // Keep the resumed stream open.
                tokio::time::sleep(Duration::from_secs(5)).await;
            }
        }
    });
    let (mut client, mut signals) = sse_client();
    client.set_endpoint(url, "tok");
    client.start();
    let mut events = Vec::new();
    let mut connects = 0;
    while events.len() < 3 {
        match next(&mut signals).await {
            SseSignal::Event(event) => events.push(event["event_id"].clone()),
            SseSignal::Connected(true) => connects += 1,
            _ => {}
        }
    }
    assert_eq!(events, [1, 2, 3]);
    assert_eq!(connects, 2);
    assert_eq!(client.last_event_id(), "3");
    let requests = requests.lock().unwrap();
    assert!(requests[0].starts_with("GET /events "));
    assert_eq!(header(&requests[0], "accept"), Some("text/event-stream"));
    assert_eq!(header(&requests[0], "authorization"), Some("Bearer tok"));
    assert_eq!(header(&requests[0], "last-event-id"), None);
    assert_eq!(header(&requests[1], "last-event-id"), Some("2"));
    drop(requests);
    client.stop();
}

#[tokio::test]
async fn sse_watchdog_reopens_a_silent_stream() {
    let (server, url) = listen().await;
    let connections = Arc::new(Mutex::new(0));
    let counter = connections.clone();
    tokio::spawn(async move {
        while let Ok((mut socket, _)) = server.accept().await {
            read_request(&mut socket).await;
            *counter.lock().unwrap() += 1;
            let _ = socket.write_all(SSE_HEADERS.as_bytes()).await;
            tokio::spawn(async move {
                tokio::time::sleep(Duration::from_secs(10)).await;
                drop(socket);
            });
        }
    });
    let (mut client, mut signals) = sse_client();
    client.set_endpoint(url, "");
    client.start();
    assert_eq!(next(&mut signals).await, SseSignal::Connected(true));
    assert_eq!(next(&mut signals).await, SseSignal::Connected(false));
    assert!(matches!(next(&mut signals).await, SseSignal::Error(m) if m.contains("silent")));
    assert_eq!(next(&mut signals).await, SseSignal::Connected(true));
    assert!(*connections.lock().unwrap() >= 2);
    client.stop();
}

#[tokio::test]
async fn stopped_stream_reports_disconnect_once_and_never_delivers_late_events() {
    let (server, url) = listen().await;
    tokio::spawn(async move {
        while let Ok((mut socket, _)) = server.accept().await {
            read_request(&mut socket).await;
            let _ = socket.write_all(SSE_HEADERS.as_bytes()).await;
            tokio::time::sleep(Duration::from_millis(150)).await;
            let _ = socket.write_all(b"id: 9\ndata: {\"type\":\"late\"}\n\n").await;
            tokio::time::sleep(Duration::from_secs(5)).await;
        }
    });
    let (mut client, mut signals) = sse_client();
    client.set_endpoint(url, "");
    assert!(!client.running());
    client.start();
    assert!(client.running());
    assert_eq!(next(&mut signals).await, SseSignal::Connected(true));
    client.stop();
    assert!(!client.connected() && !client.running());
    assert_eq!(next(&mut signals).await, SseSignal::Connected(false));
    tokio::time::sleep(Duration::from_millis(400)).await;
    assert!(signals.try_recv().is_err(), "no signal after stop");
    // Stopping again is silent.
    client.stop();
    assert!(signals.try_recv().is_err());
}

#[tokio::test]
async fn refused_connections_back_off_and_report_errors() {
    let (server, url) = listen().await;
    drop(server);
    let (mut client, mut signals) = sse_client();
    client.set_endpoint(url, "");
    client.start();
    let started = tokio::time::Instant::now();
    for _ in 0..3 {
        assert!(matches!(next(&mut signals).await, SseSignal::Error(_)));
    }
    // 20 + 40 ms of backoff between the three attempts.
    assert!(started.elapsed() >= Duration::from_millis(60));
    assert!(!client.connected());
    client.stop();
}

/// The Host gzips JSON for clients that accept it (a `/log` page shrinks
/// about 4x); the API client asks for gzip and reads the compressed reply.
#[tokio::test]
async fn api_client_asks_for_gzip_and_reads_a_compressed_reply() {
    let (listener, url) = listen().await;
    let (client, mut replies) = api_client();
    client.set_endpoint(url, "token");
    client.get("log", "/log", &[("session", "rachel")]);
    let (mut socket, _) = listener.accept().await.unwrap();
    let request = read_request(&mut socket).await;
    assert!(
        header(&request, "accept-encoding").is_some_and(|v| v.contains("gzip")),
        "the request accepts gzip: {request}"
    );
    let body = include_bytes!("fixtures/log.json.gz");
    let head = format!(
        "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Encoding: gzip\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
        body.len()
    );
    socket.write_all(head.as_bytes()).await.unwrap();
    socket.write_all(body).await.unwrap();
    match next(&mut replies).await {
        ApiReply::Json { tag, object } => {
            assert_eq!(tag, "log");
            assert_eq!(object["latest_revision"], 7);
            assert_eq!(object["turns"].as_array().map(Vec::len), Some(20));
        }
        other => panic!("expected the decompressed JSON, got {other:?}"),
    }
}

#[tokio::test]
async fn a_get_with_a_timeout_fails_when_the_body_stalls() {
    let (server, url) = listen().await;
    tokio::spawn(async move {
        while let Ok((mut socket, _)) = server.accept().await {
            read_request(&mut socket).await;
            // The headers come at once; the megabyte never finishes.
            let _ = socket.write_all(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 1000000\r\n\r\n{\"agents\":[").await;
            tokio::time::sleep(Duration::from_secs(10)).await;
        }
    });
    let (client, mut replies) = api_client();
    client.set_endpoint(url, "");
    client.get_with_timeout("snapshot:1", "/agents/snapshot", Duration::from_millis(300));
    match next(&mut replies).await {
        ApiReply::Failed { tag, message, .. } => {
            assert_eq!(tag, "snapshot:1");
            assert!(message.contains("did not answer within 0.3 s"), "{message}");
        }
        other => panic!("unexpected {other:?}"),
    }
}
