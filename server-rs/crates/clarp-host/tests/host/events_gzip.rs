use super::*;

/// What a stream said its encoding was, once its head was read.
type Encoding = Arc<Mutex<String>>;
/// The events a stream was sent, each with the number of the HTTP chunk it was decoded from.
type Heard = Arc<Mutex<Vec<(usize, Value)>>>;

/// Reads `/events` off a raw socket as asked, gzip decoded where the answer says so: its
/// `Content-Encoding` and every event, each with the HTTP chunk it was decoded from.
fn raw_listen(host: &Host, path: &str, accept_gzip: bool) -> (Encoding, Heard) {
    let encoding = Arc::new(Mutex::new(String::from("?")));
    let heard = Arc::new(Mutex::new(Vec::new()));
    let mut stream = TcpStream::connect(&host.address).unwrap();
    let accept = if accept_gzip {
        "Accept-Encoding: gzip\r\n"
    } else {
        ""
    };
    let asked =
        format!("GET {path} HTTP/1.1\r\nHost: test\r\nAccept: text/event-stream\r\n{accept}\r\n");
    stream.write_all(asked.as_bytes()).unwrap();
    let (told, collected) = (encoding.clone(), heard.clone());
    std::thread::spawn(move || {
        let mut reader = BufReader::new(stream);
        let mut gzipped = false;
        loop {
            let mut line = String::new();
            if reader.read_line(&mut line).unwrap_or(0) == 0 {
                return;
            }
            let line = line.trim_end().to_ascii_lowercase();
            if line.is_empty() {
                break;
            }
            if let Some(value) = line.strip_prefix("content-encoding:") {
                gzipped = value.trim() == "gzip";
            }
        }
        *told.lock().unwrap() = if gzipped {
            "gzip".into()
        } else {
            String::new()
        };
        let mut decoder = flate2::write::GzDecoder::new(Vec::new());
        let mut text = String::new();
        for chunk in 0.. {
            // One chunk of the chunked body.
            let mut size = String::new();
            if reader.read_line(&mut size).unwrap_or(0) == 0 {
                return;
            }
            let Ok(size) = usize::from_str_radix(size.trim(), 16) else {
                return;
            };
            let mut part = vec![0; size + 2];
            if size == 0 || reader.read_exact(&mut part).is_err() {
                return;
            }
            part.truncate(size);
            if gzipped {
                decoder.write_all(&part).unwrap();
                decoder.flush().unwrap();
                part = std::mem::take(decoder.get_mut());
            }
            text.push_str(&String::from_utf8(part).unwrap());
            while let Some(end) = text.find("\n\n") {
                let block: String = text.drain(..end + 2).collect();
                for data in block.lines().filter_map(|line| line.strip_prefix("data:")) {
                    if let Ok(event) = serde_json::from_str::<Value>(data.trim()) {
                        collected.lock().unwrap().push((chunk, event));
                    }
                }
            }
        }
    });
    (encoding, heard)
}

/// Whether a key of an event, at any depth, names a secret.
fn secret_keys(value: &Value, out: &mut Vec<String>) {
    // Named exactly, or as the end of a name (`device_token`, `api_key`).
    const EXACT: [&str; 2] = ["key", "sig"];
    const ENDS: [&str; 8] = [
        "token",
        "secret",
        "password",
        "nonce",
        "signature",
        "credential",
        "api_key",
        "private_key",
    ];
    match value {
        Value::Object(fields) => {
            for (key, inner) in fields {
                let lower = key.to_ascii_lowercase();
                let named = EXACT.contains(&lower.as_str())
                    || ENDS
                        .iter()
                        .any(|end| lower == *end || lower.ends_with(&format!("_{end}")));
                if named && !inner.is_null() && inner != "" {
                    out.push(key.clone());
                }
                secret_keys(inner, out);
            }
        }
        Value::Array(items) => items.iter().for_each(|item| secret_keys(item, out)),
        _ => {}
    }
}

/// Issue 879: `/events?gzip=1` from a client that takes gzip is one gzip stream, flushed at
/// each event, through every layer of the Host; it carries the same events as the plain stream,
/// and one that only says it takes gzip is sent the plain stream as before.
#[test]
fn an_event_stream_asked_with_gzip_comes_packed_event_by_event_and_carries_no_secret() {
    let place = Place::new("events-gzip");
    let host = place.start();
    let features = host.get("/server-info")["capabilities"]["features"].clone();
    assert!(
        features.as_array().unwrap().contains(&json!("events_gzip")),
        "{features}"
    );
    let agent = host.new_agent("Packed");
    let (packed_encoding, packed) =
        raw_listen(&host, &format!("/events?live={agent}&gzip=1"), true);
    let (plain_encoding, plain) = raw_listen(&host, &format!("/events?live={agent}"), true);
    let (unasked_encoding, _) = raw_listen(&host, &format!("/events?live={agent}&gzip=1"), false);
    wait_for("the streams to open", || {
        [&packed, &plain].iter().all(|heard| {
            heard
                .lock()
                .unwrap()
                .iter()
                .any(|(_, e)| e["type"] == "agent-roster")
        })
    });
    assert_eq!(*packed_encoding.lock().unwrap(), "gzip");
    assert_eq!(
        *plain_encoding.lock().unwrap(),
        "",
        "a client that takes gzip but did not ask with gzip=1 is sent the stream plain"
    );
    assert_eq!(
        *unasked_encoding.lock().unwrap(),
        "",
        "gzip=1 without Accept-Encoding: gzip is plain"
    );
    for n in 0..3 {
        host.send(&agent, &format!("packed {n}"), &format!("p{n}"));
        wait_for("the echo", || {
            host.turns(&agent)
                .iter()
                .filter(|row| row["role"] == "assistant")
                .count()
                > n
        });
    }
    let numbered = |heard: &Heard| -> Vec<u64> {
        heard
            .lock()
            .unwrap()
            .iter()
            .filter_map(|(_, event)| event["event_id"].as_u64())
            .collect()
    };
    wait_for("the same events on both", || {
        let (a, b) = (numbered(&packed), numbered(&plain));
        !a.is_empty() && a == b
    });
    // Each event was whole in the chunk it came in: none waited for the next one.
    let chunks: Vec<usize> = packed
        .lock()
        .unwrap()
        .iter()
        .map(|(chunk, _)| *chunk)
        .collect();
    let mut distinct = chunks.clone();
    distinct.dedup();
    assert_eq!(
        distinct.len(),
        chunks.len(),
        "one event per chunk: {chunks:?}"
    );
    // And no event carries a secret, which a shared compression window could let a watcher of
    // the lengths guess.
    let mut found = Vec::new();
    for (_, event) in packed.lock().unwrap().iter() {
        secret_keys(event, &mut found);
    }
    assert!(found.is_empty(), "secret-named fields in events: {found:?}");
}
