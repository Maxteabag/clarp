//! Cartesia as an engine that speaks.
//!
//! One request with the whole line, answered with MP3 as it is made, which is handed on
//! piece by piece. The request is the Python Host's (`server/lib/cartesia_tts.py`), and so
//! are the words it fails with.

use std::io::Read;
use std::time::Duration;

use serde_json::json;

use crate::scribe::Key;
use crate::speak::{Line, Mute, Speaker};

/// Where Cartesia takes a line.
pub const ENDPOINT: &str = "https://api.cartesia.ai/tts/bytes";

/// The dated version of Cartesia's interface the request below is written for.
const VERSION: &str = "2025-11-04";

/// Where Cartesia lists its voices, and the version of its interface that list is read by.
pub const VOICES: &str = "https://api.cartesia.ai/voices";
const VOICES_VERSION: &str = "2026-08-14";

/// Cartesia's English voices as its interface lists them: a page at a time, eight pages at
/// most. The key is presented the three ways Cartesia has taken one, each tried when the
/// one before was refused (the Python Host's `cartesia_voices.py`).
pub fn voices(key: &str, endpoint: &str) -> Result<Vec<serde_json::Value>, String> {
    let config = ureq::Agent::config_builder()
        .max_redirects(0)
        .timeout_global(Some(Duration::from_secs(15)))
        .http_status_as_error(false)
        .build();
    let agent: ureq::Agent = config.into();
    let bearer = format!("Bearer {key}");
    let ways = [
        ("Authorization", bearer.as_str()),
        ("Authorization", key),
        ("X-API-Key", key),
    ];
    let mut voices = Vec::new();
    let mut after = String::new();
    for _ in 0..8 {
        let mut url = format!("{endpoint}?limit=100&language=en&expand%5B%5D=preview_file_url");
        if !after.is_empty() {
            url.push_str(&format!("&starting_after={after}"));
        }
        let mut page: Option<serde_json::Value> = None;
        for (place, (header, value)) in ways.iter().enumerate() {
            let mut response = agent
                .get(&url)
                .header(*header, *value)
                .header("Cartesia-Version", VOICES_VERSION)
                .call()
                .map_err(|error| format!("request failed: {error}"))?;
            let status = response.status().as_u16();
            if status == 401 && place + 1 < ways.len() {
                continue;
            }
            if !(200..300).contains(&status) {
                return Err(format!("HTTP {status}"));
            }
            let said = response
                .body_mut()
                .read_to_string()
                .map_err(|error| format!("request failed: {error}"))?;
            let read = serde_json::from_str(&said);
            page = Some(read.map_err(|error| format!("request failed: {error}"))?);
            break;
        }
        let Some(page) = page else { break };
        voices.extend(page["data"].as_array().cloned().unwrap_or_default());
        after = match &page["next_page"] {
            serde_json::Value::String(next) => next.clone(),
            serde_json::Value::Null => String::new(),
            other => other.to_string(),
        };
        if page["has_more"] != true || after.is_empty() {
            break;
        }
    }
    Ok(voices)
}

pub struct Cartesia {
    key: Key,
    /// The model, by Cartesia's own name (`sonic-3.5`).
    model: String,
    /// The language the line is in (`en`).
    language: String,
    endpoint: String,
    timeout: Duration,
}

impl Cartesia {
    pub fn new(model: impl Into<String>, language: impl Into<String>, key: Key) -> Self {
        Self {
            key,
            model: model.into(),
            language: language.into(),
            endpoint: ENDPOINT.to_string(),
            timeout: Duration::from_secs(30),
        }
    }

    /// The same engine at another address: a stand-in that answers as Cartesia does.
    pub fn at(mut self, endpoint: impl Into<String>) -> Self {
        self.endpoint = endpoint.into();
        self
    }
}

impl Speaker for Cartesia {
    fn speak(&self, line: Line<'_>, sink: &mut dyn FnMut(&[u8])) -> Result<u64, Mute> {
        let Some(key) = (self.key)().filter(|key| !key.is_empty()) else {
            return Err(Mute::Unavailable("api_key required".to_string()));
        };
        if line.voice.is_empty() {
            return Err(Mute::Unavailable("voice_id required".to_string()));
        }
        // The same sound as every other engine's: 44.1 kHz, one channel, 128 kbit/s.
        let body = json!({
            "model_id": self.model,
            "transcript": line.text,
            "voice": { "mode": "id", "id": line.voice },
            "language": self.language,
            "output_format": { "container": "mp3", "sample_rate": 44_100, "bit_rate": 128_000 },
        });
        let config = ureq::Agent::config_builder()
            .max_redirects(0)
            .timeout_connect(Some(Duration::from_secs(10)))
            .timeout_recv_response(Some(self.timeout))
            .timeout_recv_body(Some(Duration::from_secs(600)))
            .http_status_as_error(false)
            .build();
        let agent: ureq::Agent = config.into();
        let failed = |why: String, begun: bool| Mute::Failed { why, begun };
        let mut response = agent
            .post(&self.endpoint)
            .header("X-API-Key", key.as_str())
            .header("Cartesia-Version", VERSION)
            .send_json(&body)
            .map_err(|error| failed(format!("request failed: {error}"), false))?;
        let status = response.status().as_u16();
        if !(200..300).contains(&status) {
            let said = response.body_mut().read_to_string().unwrap_or_default();
            let detail: String = said.chars().take(200).collect();
            let detail = match detail.as_str() {
                "" => "no reason given",
                detail => detail,
            };
            return Err(failed(format!("HTTP {status}: {detail}"), false));
        }
        let mut produced = 0u64;
        let mut buffer = [0u8; 16 * 1024];
        let mut sound = response.body_mut().as_reader();
        loop {
            match sound.read(&mut buffer) {
                Ok(0) => break,
                Ok(read) => {
                    produced += read as u64;
                    sink(&buffer[..read]);
                }
                Err(error) => {
                    return Err(failed(format!("request failed: {error}"), produced > 0));
                }
            }
        }
        if produced == 0 {
            return Err(failed("empty response body".to_string(), false));
        }
        Ok(produced)
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use std::io::Write;
    use std::net::TcpListener;
    use std::sync::{Arc, Mutex};

    use serde_json::Value;

    use super::*;

    /// A stand-in for Cartesia on the loopback: answers one request with what it was given
    /// and keeps what the request sent.
    fn stand_in(status: &'static str, body: &'static [u8]) -> (String, Arc<Mutex<Vec<String>>>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = format!("http://{}/tts/bytes", listener.local_addr().unwrap());
        let taken: Arc<Mutex<Vec<String>>> = Arc::new(Mutex::new(Vec::new()));
        let keeps = taken.clone();
        std::thread::spawn(move || {
            let Ok((mut stream, _)) = listener.accept() else {
                return;
            };
            let (mut seen, mut buffer) = (Vec::new(), [0u8; 4096]);
            let (head_end, length) = loop {
                let read = stream.read(&mut buffer).unwrap_or(0);
                seen.extend_from_slice(&buffer[..read]);
                let text = String::from_utf8_lossy(&seen).to_string();
                if let Some(at) = text.find("\r\n\r\n") {
                    let length = text.lines().find_map(|line| {
                        let (name, value) = line.split_once(':')?;
                        let named = name.eq_ignore_ascii_case("content-length");
                        named.then(|| value.trim().parse::<usize>().ok())?
                    });
                    break (at + 4, length.unwrap_or(0));
                }
                if read == 0 {
                    break (seen.len(), 0);
                }
            };
            while seen.len() < head_end + length {
                let read = stream.read(&mut buffer).unwrap_or(0);
                if read == 0 {
                    break;
                }
                seen.extend_from_slice(&buffer[..read]);
            }
            keeps
                .lock()
                .unwrap()
                .push(String::from_utf8_lossy(&seen).to_string());
            let head = format!(
                "HTTP/1.1 {status}\r\nContent-Type: audio/mpeg\r\nContent-Length: {}\r\n\
                 Connection: close\r\n\r\n",
                body.len()
            );
            let _ = stream.write_all(head.as_bytes());
            let _ = stream.write_all(body);
        });
        (address, taken)
    }

    fn engine(address: &str) -> Cartesia {
        let key: Key = Box::new(|| Some("key-for-the-test".to_string()));
        Cartesia::new("sonic-3.5", "en", key).at(address)
    }

    fn say(engine: &Cartesia, voice: &str) -> (Result<u64, Mute>, Vec<u8>) {
        let mut heard = Vec::new();
        let line = Line {
            text: "A line to say.",
            voice,
        };
        let said = engine.speak(line, &mut |piece| heard.extend_from_slice(piece));
        (said, heard)
    }

    #[test]
    fn a_line_is_asked_for_as_the_python_host_asks_and_its_sound_is_handed_on() {
        let (address, taken) = stand_in("200 OK", b"ID3 made-up mp3 bytes");
        let (said, heard) = say(&engine(&address), "voice-1");
        assert_eq!(said, Ok(21));
        assert_eq!(heard, b"ID3 made-up mp3 bytes");
        let request = taken.lock().unwrap()[0].clone();
        let lower = request.to_lowercase();
        assert!(request.starts_with("POST /tts/bytes "), "{request}");
        assert!(lower.contains("x-api-key: key-for-the-test"));
        assert!(lower.contains("cartesia-version: 2025-11-04"));
        let body: Value = serde_json::from_str(request.split("\r\n\r\n").nth(1).unwrap()).unwrap();
        assert_eq!(
            body,
            json!({
                "model_id": "sonic-3.5",
                "transcript": "A line to say.",
                "voice": { "mode": "id", "id": "voice-1" },
                "language": "en",
                "output_format": { "container": "mp3", "sample_rate": 44_100, "bit_rate": 128_000 },
            })
        );
    }

    #[test]
    fn what_goes_wrong_is_said_in_the_python_hosts_words() {
        let (address, _) = stand_in("402 Payment Required", b"out of credits");
        assert_eq!(
            say(&engine(&address), "voice-1").0,
            Err(Mute::Failed {
                why: "HTTP 402: out of credits".to_string(),
                begun: false
            })
        );
        let (address, _) = stand_in("200 OK", b"");
        assert_eq!(
            say(&engine(&address), "voice-1").0,
            Err(Mute::Failed {
                why: "empty response body".to_string(),
                begun: false
            })
        );
        // What cannot be used as it stands asks nobody.
        let (address, taken) = stand_in("200 OK", b"sound");
        let no_key = Cartesia::new("sonic-3.5", "en", Box::new(|| None)).at(address.clone());
        assert_eq!(
            say(&no_key, "voice-1").0,
            Err(Mute::Unavailable("api_key required".to_string()))
        );
        assert_eq!(
            say(&engine(&address), "").0,
            Err(Mute::Unavailable("voice_id required".to_string()))
        );
        assert!(taken.lock().unwrap().is_empty());
        // Nobody listens there.
        let gone = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = format!("http://{}/tts/bytes", gone.local_addr().unwrap());
        drop(gone);
        let Err(Mute::Failed { why, begun }) = say(&engine(&address), "voice-1").0 else {
            panic!("a request to nobody fails");
        };
        assert!(why.starts_with("request failed: ") && !begun, "{why}");
        assert!(!why.contains("key-for-the-test"));
    }
}
