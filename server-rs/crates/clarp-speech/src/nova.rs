//! Deepgram Nova: an engine that listens, reached over the network.
//!
//! A clip is posted as it is, and what the engine is asked stands in the address: the
//! model, the language, and each word to listen for. The answer has one text and how long
//! the clip is. The request is the Python Host's (`server/lib/deepgram_stt.py`), and so
//! are the words it fails with.

use std::time::Duration;

use serde_json::Value;

use crate::listen::{Asked, Clip, Deaf, Heard, Listener, terms};
use crate::scribe::Key;

/// Where Deepgram takes a clip.
pub const ENDPOINT: &str = "https://api.deepgram.com/v1/listen";

pub struct Nova {
    key: Key,
    /// The model, by Deepgram's own name (`nova-3`).
    model: String,
    endpoint: String,
    timeout: Duration,
}

/// A text as it stands in an address after a name: letters, digits and four other signs as
/// they are, a space as a plus, every other byte as its number (Python's `urlencode`).
fn in_an_address(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for byte in text.bytes() {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'_' | b'.' | b'-' | b'~' => {
                out.push(char::from(byte));
            }
            b' ' => out.push('+'),
            other => out.push_str(&format!("%{other:02X}")),
        }
    }
    out
}

impl Nova {
    pub fn new(model: impl Into<String>, key: Key) -> Self {
        Self {
            key,
            model: model.into(),
            endpoint: ENDPOINT.to_string(),
            timeout: Duration::from_secs(30),
        }
    }

    /// The same engine at another address: a stand-in that answers as Deepgram does.
    pub fn at(mut self, endpoint: impl Into<String>) -> Self {
        self.endpoint = endpoint.into();
        self
    }

    /// What the engine is asked, as it stands in the address: the model, that it writes
    /// numbers and marks as a reader would, the language (English where none is known),
    /// and each word to listen for under a name of its own.
    fn asked_for(&self, asked: &Asked<'_>) -> String {
        let mut named = vec![
            ("model", self.model.clone()),
            ("smart_format", "true".to_string()),
            ("language", asked.language.unwrap_or("en").to_string()),
        ];
        named.extend(
            terms(asked.listen_for)
                .into_iter()
                .map(|term| ("keyterm", term)),
        );
        let named: Vec<String> = named
            .iter()
            .map(|(name, value)| format!("{name}={}", in_an_address(value)))
            .collect();
        named.join("&")
    }
}

/// What is said of an answer that is no success: the status and the beginning of what came
/// with it.
fn refused(status: u16, said: &str) -> String {
    let detail: String = said.chars().take(200).collect();
    let detail = if detail.is_empty() {
        "no reason given"
    } else {
        detail.as_str()
    };
    format!("Deepgram HTTP {status}: {detail}")
}

/// What an answer says was heard: the text of its first reading, and how long the clip is.
fn heard(answer: &Value) -> Result<Heard, Deaf> {
    let reading = &answer["results"]["channels"][0]["alternatives"][0];
    if !reading.is_object() {
        let none = "Deepgram response had no transcript".to_string();
        return Err(Deaf::Failed(none));
    }
    let text = reading["transcript"].as_str().unwrap_or("");
    let seconds = answer["metadata"]["duration"].as_f64().unwrap_or(0.0);
    Ok(Heard::text(text, seconds))
}

impl Listener for Nova {
    fn hear(&self, clip: Clip<'_>, asked: Asked<'_>) -> Result<Heard, Deaf> {
        let Some(key) = (self.key)().filter(|key| !key.is_empty()) else {
            let none = "Deepgram API key is not configured".to_string();
            return Err(Deaf::Failed(none));
        };
        let content_type = match clip.content_type {
            "" => "audio/webm",
            given => given,
        };
        let config = ureq::Agent::config_builder()
            .timeout_global(Some(self.timeout))
            .http_status_as_error(false)
            .build();
        let agent: ureq::Agent = config.into();
        let address = format!("{}?{}", self.endpoint, self.asked_for(&asked));
        let mut response = agent
            .post(&address)
            .header("Authorization", format!("Token {key}"))
            .header("Content-Type", content_type)
            .send(clip.audio)
            // How it failed, and nothing of what was sent: the error of a transport can
            // name the address, never the key or the clip.
            .map_err(|error| Deaf::Failed(format!("Deepgram request failed: {error}")))?;
        let status = response.status().as_u16();
        let said = response.body_mut().read_to_string().unwrap_or_default();
        if !(200..300).contains(&status) {
            return Err(Deaf::Failed(refused(status, &said)));
        }
        let answer: Value = serde_json::from_str(&said)
            .map_err(|error| Deaf::Failed(format!("Deepgram request failed: {error}")))?;
        heard(&answer)
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use std::io::{Read, Write};
    use std::net::TcpListener;
    use std::sync::mpsc;

    use serde_json::json;

    use super::*;

    fn asked<'a>(listen_for: &'a str, language: Option<&'a str>) -> Asked<'a> {
        Asked {
            listen_for,
            language,
            wait: Duration::from_secs(1),
        }
    }

    fn with_key() -> Key {
        Box::new(|| Some("key-for-the-test".to_string()))
    }

    #[test]
    fn the_address_says_the_model_the_language_and_each_word_to_listen_for() {
        let nova = Nova::new("nova-3", with_key());
        assert_eq!(
            nova.asked_for(&asked("", Some("no"))),
            "model=nova-3&smart_format=true&language=no"
        );
        // English where no language is known, as the Python Host's engine has it.
        assert_eq!(
            nova.asked_for(&asked("Clarp, two words ,, a&b=c/d, \u{fc}ber~x", None)),
            "model=nova-3&smart_format=true&language=en&keyterm=Clarp&keyterm=two+words\
             &keyterm=a%26b%3Dc%2Fd&keyterm=%C3%BCber~x"
        );
    }

    #[test]
    fn an_answer_is_read_and_a_refusal_is_said_in_the_python_hosts_words() {
        let answer = json!({
            "metadata": { "duration": 2.5 },
            "results": { "channels": [{ "alternatives": [
                { "transcript": "hello there", "confidence": 0.9 },
                { "transcript": "another reading" },
            ] }] },
        });
        assert_eq!(heard(&answer), Ok(Heard::text("hello there", 2.5)));
        // A reading without a text is a clip in which nothing was said.
        let silent = json!({ "results": { "channels": [{ "alternatives": [{}] }] } });
        assert_eq!(heard(&silent), Ok(Heard::text("", 0.0)));
        let none = Err(Deaf::Failed(
            "Deepgram response had no transcript".to_string(),
        ));
        for answer in [
            json!({}),
            json!({ "results": {} }),
            json!({ "results": { "channels": [] } }),
            json!({ "results": { "channels": [{ "alternatives": [] }] } }),
            json!({ "results": { "channels": [{ "alternatives": ["a text"] }] } }),
        ] {
            assert_eq!(heard(&answer), none, "{answer}");
        }
        assert_eq!(
            refused(401, "{\"err_code\":\"INVALID_AUTH\"}"),
            "Deepgram HTTP 401: {\"err_code\":\"INVALID_AUTH\"}"
        );
        assert_eq!(
            refused(500, &"x".repeat(300)),
            format!("Deepgram HTTP 500: {}", "x".repeat(200))
        );
        assert_eq!(refused(503, ""), "Deepgram HTTP 503: no reason given");
    }

    /// A stand-in for Deepgram: answers one request, and hands on what it was sent.
    fn stand_in(status: &'static str, answer: &'static str) -> (String, mpsc::Receiver<String>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = format!("http://{}/v1/listen", listener.local_addr().unwrap());
        let (sent, received) = mpsc::channel();
        std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut request = Vec::new();
            let mut buffer = [0u8; 4096];
            // The head, then as many bytes as it says follow.
            let (head_end, length) = loop {
                let count = stream.read(&mut buffer).unwrap();
                request.extend_from_slice(&buffer[..count]);
                let end = request.windows(4).position(|window| window == b"\r\n\r\n");
                if let Some(end) = end {
                    let head = String::from_utf8_lossy(&request[..end]).to_lowercase();
                    let length = head
                        .lines()
                        .find_map(|line| line.strip_prefix("content-length:"))
                        .map_or(0, |length| length.trim().parse::<usize>().unwrap());
                    break (end + 4, length);
                }
            };
            while request.len() < head_end + length {
                let count = stream.read(&mut buffer).unwrap();
                request.extend_from_slice(&buffer[..count]);
            }
            let _ = sent.send(String::from_utf8_lossy(&request).into_owned());
            let reply = format!(
                "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{answer}",
                answer.len()
            );
            stream.write_all(reply.as_bytes()).unwrap();
        });
        (address, received)
    }

    #[test]
    fn a_clip_is_posted_with_the_key_and_what_was_heard_comes_back() {
        let (address, sent) = stand_in(
            "200 OK",
            r#"{"metadata":{"duration":1.25},"results":{"channels":[{"alternatives":[{"transcript":"hello Clarp"}]}]}}"#,
        );
        let nova = Nova::new("nova-3", with_key()).at(address);
        let clip = Clip {
            audio: b"the-sound-of-a-clip",
            content_type: "audio/mp4",
        };
        let heard = nova
            .hear(clip, asked("Clarp, Maximus", Some("en")))
            .unwrap();
        assert_eq!(heard, Heard::text("hello Clarp", 1.25));
        let request = sent.recv().unwrap();
        let (head, body) = request.split_once("\r\n\r\n").unwrap();
        let first = head.lines().next().unwrap();
        assert_eq!(
            first,
            "POST /v1/listen?model=nova-3&smart_format=true&language=en&keyterm=Clarp&keyterm=Maximus HTTP/1.1"
        );
        let head = format!("{}\r\n", head.to_lowercase());
        assert!(
            head.contains("\r\nauthorization: token key-for-the-test\r\n"),
            "{head}"
        );
        assert!(head.contains("\r\ncontent-type: audio/mp4\r\n"), "{head}");
        assert_eq!(body, "the-sound-of-a-clip");
    }

    #[test]
    fn a_provider_that_refuses_is_quoted_and_one_that_is_not_there_is_said_so() {
        let (address, _sent) = stand_in("402 Payment Required", r#"{"err_msg":"out of credit"}"#);
        let nova = Nova::new("nova-3", with_key()).at(address);
        let clip = Clip {
            audio: b"x",
            content_type: "",
        };
        assert_eq!(
            nova.hear(clip, asked("", Some("en"))),
            Err(Deaf::Failed(
                "Deepgram HTTP 402: {\"err_msg\":\"out of credit\"}".to_string()
            ))
        );
        // Nobody listens there.
        let gone = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = format!("http://{}/v1/listen", gone.local_addr().unwrap());
        drop(gone);
        let nova = Nova::new("nova-3", with_key()).at(address);
        let Err(Deaf::Failed(why)) = nova.hear(clip, asked("", Some("en"))) else {
            panic!("heard by nobody");
        };
        assert!(why.starts_with("Deepgram request failed: "), "{why}");
        assert!(!why.contains("key-for-the-test"), "{why}");
        // And without a key nothing is sent. (Asked at an address of this machine where
        // nobody listens: no test reaches an engine outside, whatever rule is wrong.)
        let gone = TcpListener::bind("127.0.0.1:0").unwrap();
        let nowhere = format!("http://{}/v1/listen", gone.local_addr().unwrap());
        drop(gone);
        let keyless = Nova::new("nova-3", Box::new(|| None)).at(nowhere);
        assert_eq!(
            keyless.hear(clip, asked("", Some("en"))),
            Err(Deaf::Failed(
                "Deepgram API key is not configured".to_string()
            ))
        );
    }
}
