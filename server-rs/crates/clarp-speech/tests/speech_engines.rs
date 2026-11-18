//! Recorded engine exchanges with disposable loopback peers.
#![allow(clippy::unwrap_used, clippy::expect_used)]
use clarp_speech::{
    eleven_voice::ElevenVoice,
    ink::Ink,
    listen::{Asked, Clip, Deaf, Listener},
    speak::{Line, Mute, Speaker, said_by_the_first_that_can},
};
use serde_json::{Value, json};
use std::net::{TcpListener, TcpStream};
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tungstenite::Message;

fn vectors() -> Value {
    serde_json::from_str(include_str!(
        "../../clarp-kernel/tests/fixtures/speech-engines-vectors.json"
    ))
    .unwrap()
}

fn peer(f: impl FnOnce(TcpStream) + Send + 'static) -> (String, std::thread::JoinHandle<()>) {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let base = format!("http://{}", listener.local_addr().unwrap());
    (
        base,
        std::thread::spawn(move || {
            let (socket, _) = listener.accept().unwrap();
            socket
                .set_read_timeout(Some(Duration::from_secs(3)))
                .unwrap();
            socket
                .set_write_timeout(Some(Duration::from_secs(3)))
                .unwrap();
            f(socket)
        }),
    )
}

#[test]
fn elevenlabs_reads_every_recorded_sequence() {
    let fixture = vectors();
    for case in fixture["eleven"].as_array().unwrap() {
        let events = case["events"].as_array().unwrap().clone();
        let sent = case["sent"].clone();
        let (base, running) = peer(move |s| {
            let mut ws = tungstenite::accept(s).unwrap();
            for expected in sent.as_array().unwrap() {
                let line = ws.read().unwrap().into_text().unwrap();
                assert_eq!(serde_json::from_str::<Value>(&line).unwrap(), *expected);
            }
            for e in events {
                let raw = e
                    .as_str()
                    .map(str::to_string)
                    .unwrap_or_else(|| e.to_string());
                ws.send(Message::Text(raw.into())).unwrap();
            }
        });
        let engine = ElevenVoice {
            key: Box::new(|| Some("unit".into())),
            model: "model".into(),
            speed: 1.1,
            file: false,
            base,
        };
        let mut chunks = Vec::new();
        use base64::{Engine, engine::general_purpose::STANDARD};
        let result = engine.speak(
            Line {
                text: "A line.",
                voice: "example",
            },
            &mut |b| chunks.push(json!([chunks.len(), STANDARD.encode(b)])),
        );
        assert_eq!(json!(chunks), case["chunks"]);
        if let Some(size) = case["size"].as_u64() {
            assert_eq!(result, Ok(size));
        } else {
            let err = result.unwrap_err();
            assert_eq!(err.may_be_said_by_another(), chunks.is_empty());
            let Mute::Failed { why, .. } = err else {
                panic!("expected transport failure")
            };
            let raw = case["error"].as_str().unwrap();
            let expected = if raw.starts_with("server sent non-JSON") {
                "server sent non-JSON frame"
            } else if raw.starts_with("bad base64") {
                "bad base64 from server"
            } else if matches!(raw, "busy" | "rejected" | "closed") {
                "ElevenLabs refused synthesis"
            } else {
                raw
            };
            assert_eq!(why, expected);
        }
        running.join().unwrap();
    }
}

#[test]
fn ink_sends_the_recorded_chunks_and_interprets_socket_messages() {
    for case in vectors()["ink_streams"].as_array().unwrap() {
        let messages = case["events"].as_array().unwrap().clone();
        let sent = case["sent"].as_array().unwrap().clone();
        let (base, server) = peer(move |s| {
            let mut ws = tungstenite::accept(s).unwrap();
            for expected in sent {
                let frame = ws.read().unwrap();
                if let Some(size) = expected.as_u64() {
                    assert!(frame.is_binary());
                    assert_eq!(frame.len(), size as usize);
                } else {
                    assert_eq!(
                        frame.into_text().unwrap().as_str(),
                        expected.as_str().unwrap()
                    );
                }
            }
            for e in messages {
                let raw = e
                    .as_str()
                    .map(str::to_string)
                    .unwrap_or_else(|| e.to_string());
                ws.send(Message::Text(raw.into())).unwrap();
            }
            let _ = ws.close(None);
        });
        let ink = Ink::new("ink-2", Box::new(|| Some("unit".into())), base, "cat");
        let audio = vec![b'I'; case["bytes"].as_u64().unwrap() as usize];
        let seen = ink.hear(
            Clip {
                audio: &audio,
                content_type: "audio/webm",
            },
            Asked {
                language: Some("nb"),
                listen_for: "one,two",
                wait: Duration::from_secs(2),
            },
        );
        if let Some(why) = case["error"].as_str() {
            assert_eq!(seen.unwrap_err(), Deaf::Failed(why.into()));
        } else {
            assert_eq!(seen.unwrap().segments[0].text, case["result"]);
        }
        server.join().unwrap();
    }
}

#[test]
fn an_engine_may_take_over_only_before_the_first_audio() {
    struct Speaks {
        name: String,
        mode: String,
        calls: Arc<Mutex<Vec<Value>>>,
    }
    impl Speaker for Speaks {
        fn speak(&self, line: Line<'_>, sink: &mut dyn FnMut(&[u8])) -> Result<u64, Mute> {
            self.calls
                .lock()
                .unwrap()
                .push(json!([self.name, line.text]));
            if self.mode != "failed" {
                sink(b"x");
            }
            if self.mode == "success" {
                Ok(1)
            } else {
                Err(Mute::Failed {
                    why: format!("{} refused", self.name),
                    begun: self.mode == "partial",
                })
            }
        }
    }
    struct Gemini(Speaks);
    impl Speaker for Gemini {
        fn speak(&self, line: Line<'_>, sink: &mut dyn FnMut(&[u8])) -> Result<u64, Mute> {
            self.0.speak(line, sink)
        }
        fn speak_turn(&self, line: Line<'_>, sink: &mut dyn FnMut(&[u8])) -> Result<u64, Mute> {
            self.speak(line, sink)
        }
    }
    for case in vectors()["ladders"].as_array().unwrap() {
        let calls = Arc::new(Mutex::new(Vec::new()));
        let engine = |id: &str, key: &str| Speaks {
            name: id.into(),
            mode: case[key].as_str().unwrap().into(),
            calls: calls.clone(),
        };
        let first = engine("deepgram", "primary");
        let next = Gemini(engine("gemini", "second"));
        let mut engines: Vec<(&dyn Speaker, &str)> = vec![(&first, "")];
        if case["fallback"] == "gemini" {
            engines.push((&next, ""));
        }
        let mut chunks = Vec::new();
        let result = said_by_the_first_that_can(&engines, "[happy] sound", &mut |b| {
            chunks.push(String::from_utf8(b.into()).unwrap())
        });
        assert_eq!(json!(chunks), case["chunks"]);
        assert_eq!(json!(*calls.lock().unwrap()), case["calls"]);
        match result {
            Ok((_, n)) => assert_eq!(case["size"], n),
            Err(Mute::Failed { why, .. }) => assert_eq!(case["error"], why),
            Err(e) => panic!("{e:?}"),
        }
    }
}

#[test]
fn ink_batch_matches_the_reference_form_and_answer() {
    use std::io::{BufRead, BufReader, Read, Write};
    for row in vectors()["ink_batch"].as_array().unwrap() {
        let case = row.clone();
        let (base, server) = peer(move |mut socket| {
            let mut input = BufReader::new(socket.try_clone().unwrap());
            let mut header = String::new();
            loop {
                let mut line = String::new();
                let n = input.read_line(&mut line).unwrap();
                assert_ne!(n, 0);
                if line == "\r\n" {
                    break;
                }
                header.push_str(&line);
            }
            let headers = header.to_lowercase();
            assert!(header.starts_with("POST /stt HTTP/1.1"));
            assert!(headers.contains("authorization: bearer unit"));
            assert!(headers.contains("cartesia-version: 2026-08-14"));
            let length: usize = headers
                .lines()
                .find_map(|s| s.strip_prefix("content-length: "))
                .unwrap()
                .trim()
                .parse()
                .unwrap();
            let mut body = vec![0; length];
            input.read_exact(&mut body).unwrap();
            let body = String::from_utf8(body).unwrap();
            let mut rest = body.as_str();
            for field in case["request"]["fields"].as_array().unwrap() {
                let name = format!("name=\"{}\"", field["name"].as_str().unwrap());
                rest = rest.split_once(&name).unwrap().1;
                if let Some(filename) = field["filename"].as_str() {
                    rest = rest
                        .split_once(&format!("filename=\"{filename}\""))
                        .unwrap()
                        .1;
                }
                rest = rest
                    .split_once(&format!("\r\n\r\n{}\r\n", field["body"].as_str().unwrap()))
                    .unwrap()
                    .1;
            }
            assert!(!body.contains("keyterm"));
            let reply = json!({"text":"heard","duration":case["duration"]}).to_string();
            write!(socket,"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{reply}",reply.len()).unwrap();
        });
        let engine = Ink::new("ink-whisper", Box::new(|| Some("unit".into())), base, "cat");
        let heard = engine
            .hear(
                Clip {
                    audio: b"clip",
                    content_type: row["ctype"].as_str().unwrap(),
                },
                Asked {
                    listen_for: "never sent",
                    language: Some("nb"),
                    wait: Duration::from_secs(2),
                },
            )
            .unwrap();
        assert_eq!(heard.segments[0].text, row["heard"][0]);
        assert_eq!(Some(heard.seconds), row["heard"][1].as_f64());
        server.join().unwrap();
    }
}
