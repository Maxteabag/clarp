//! Gemini as an engine that speaks.
//!
//! Gemini streams sound without a header (16 bits, one channel, 24 kHz) as events over one
//! answer. The apps play MP3, so the sound is piped through an encoder as it comes and the
//! encoder's output is handed on piece by piece. The request is the Python Host's
//! (`server/lib/gemini_tts.py`), through either of Google's two doors: the Gemini API, or
//! Vertex AI with an express key.
//!
//! When the provider says it is busy before anything was spoken, the request is tried again
//! after a moment; nothing was heard yet, so nothing is said twice.

use std::io::{BufRead, BufReader, Read, Write};
use std::process::{ChildStdin, Stdio};
use std::time::Duration;

use serde_json::{Value, json};

use crate::scribe::Key;
use crate::speak::{Line, Mute, Speaker};

/// Which of Google's two doors a request goes through.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Surface {
    GeminiApi,
    Vertex,
}

impl Surface {
    /// The door by the name the owner's settings have for it.
    pub fn by_name(name: &str) -> Option<Self> {
        match name {
            "gemini_api" => Some(Self::GeminiApi),
            "vertex" => Some(Self::Vertex),
            _ => None,
        }
    }

    pub fn name(self) -> &'static str {
        match self {
            Self::GeminiApi => "gemini_api",
            Self::Vertex => "vertex",
        }
    }

    fn address(self, model: &str) -> String {
        let models = match self {
            Self::GeminiApi => "https://generativelanguage.googleapis.com/v1beta/models",
            // The regional doors do not know the model: the global one does.
            Self::Vertex => "https://aiplatform.googleapis.com/v1/publishers/google/models",
        };
        format!("{models}/{model}:streamGenerateContent?alt=sse")
    }
}

/// The sound Gemini sends: how many samples a second.
const SAMPLE_RATE: &str = "24000";

/// What the provider answers when it is busy. Tried again while nothing was spoken.
const BUSY: [u16; 3] = [429, 500, 503];

pub struct Gemini {
    key: Key,
    model: String,
    surface: Surface,
    /// A stand-in's address in place of Google's.
    endpoint: Option<String>,
    /// The program that turns the engine's sound into MP3, and what it is given.
    encoder: Vec<String>,
    /// How long the provider may take to begin its answer.
    timeout: Duration,
    /// How long is waited before each further try while the provider is busy.
    tried_again_after: Vec<Duration>,
}

/// The voice as the request names it: one of Google's own by its name, or one that was
/// designed, by its id.
fn voice_config(voice: &str) -> Value {
    if voice.starts_with("voice_") {
        json!({ "voice": voice })
    } else {
        json!({ "prebuiltVoiceConfig": { "voiceName": voice } })
    }
}

/// The bytes a text in base 64 stands for. A sign that is none of its alphabet is passed
/// over, as the Python Host's reader passes it over.
fn from_base64(text: &str) -> Vec<u8> {
    let value = |sign: u8| -> Option<u32> {
        Some(u32::from(match sign {
            b'A'..=b'Z' => sign - b'A',
            b'a'..=b'z' => sign - b'a' + 26,
            b'0'..=b'9' => sign - b'0' + 52,
            b'+' => 62,
            b'/' => 63,
            _ => return None,
        }))
    };
    let mut out = Vec::with_capacity(text.len() / 4 * 3);
    let (mut held, mut bits) = (0u32, 0u32);
    for six in text.bytes().filter_map(value) {
        held = (held << 6) | six;
        bits += 6;
        if bits >= 8 {
            bits -= 8;
            out.push(((held >> bits) & 0xff) as u8);
            held &= (1 << bits) - 1;
        }
    }
    out
}

/// The sound in one line of the provider's answer, when it carries any. `Err` is what the
/// provider said went wrong, or that the line cannot be read.
fn sound_in(line: &str) -> Result<Vec<Vec<u8>>, String> {
    let Some(data) = line.trim().strip_prefix("data:") else {
        return Ok(Vec::new());
    };
    let event: Value =
        serde_json::from_str(data).map_err(|error| format!("Gemini request failed: {error}"))?;
    if let Some(error) = event.get("error") {
        let said: String = error.to_string().chars().take(300).collect();
        return Err(said);
    }
    let mut pieces = Vec::new();
    for candidate in event["candidates"].as_array().into_iter().flatten() {
        for part in candidate["content"]["parts"]
            .as_array()
            .into_iter()
            .flatten()
        {
            if let Some(data) = part["inlineData"]["data"]
                .as_str()
                .filter(|data| !data.is_empty())
            {
                pieces.push(from_base64(data));
            }
        }
    }
    Ok(pieces)
}

impl Gemini {
    pub fn new(model: impl Into<String>, surface: Surface, key: Key) -> Self {
        let encoder = [
            "ffmpeg",
            "-loglevel",
            "error",
            "-f",
            "s16le",
            "-ar",
            SAMPLE_RATE,
            "-ac",
            "1",
            "-i",
            "pipe:0",
            "-ar",
            "44100",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "128k",
            "-flush_packets",
            "1",
            "-f",
            "mp3",
            "pipe:1",
        ];
        Self {
            key,
            model: model.into(),
            surface,
            endpoint: None,
            encoder: encoder.iter().map(|word| (*word).to_string()).collect(),
            timeout: Duration::from_secs(60),
            tried_again_after: vec![Duration::from_millis(1_000), Duration::from_millis(2_500)],
        }
    }

    /// The same engine at another address: a stand-in that answers as Google does.
    pub fn at(mut self, endpoint: impl Into<String>) -> Self {
        self.endpoint = Some(endpoint.into());
        self
    }

    /// The same engine with another program for its encoder.
    pub fn encoded_by(mut self, encoder: Vec<String>) -> Self {
        self.encoder = encoder;
        self
    }

    /// The same engine with other waits between its tries.
    pub fn tried_again_after(mut self, waits: Vec<Duration>) -> Self {
        self.tried_again_after = waits;
        self
    }

    /// Asks the provider and pours the sound it sends into the encoder. Answers whether any
    /// sound came. It runs on a thread of its own: the encoder's output is read meanwhile.
    fn pour(&self, key: &str, line: Line<'_>, mut encoder: ChildStdin) -> Result<bool, String> {
        let surface = self.surface.name();
        let address = match &self.endpoint {
            Some(endpoint) => endpoint.clone(),
            None => self.surface.address(&self.model),
        };
        let body = json!({
            "contents": [{ "role": "user", "parts": [{ "text": line.text }] }],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": { "voiceConfig": voice_config(line.voice) },
            },
        });
        let config = ureq::Agent::config_builder()
            .max_redirects(0)
            .timeout_connect(Some(Duration::from_secs(10)))
            .timeout_recv_response(Some(self.timeout))
            // A line is minutes at most: an answer that never ends is cut.
            .timeout_recv_body(Some(Duration::from_secs(600)))
            .http_status_as_error(false)
            .build();
        let agent: ureq::Agent = config.into();
        let mut waits = self.tried_again_after.iter();
        let mut response = loop {
            let mut response = agent
                .post(&address)
                .header("x-goog-api-key", key)
                .send_json(&body)
                .map_err(|error| format!("Gemini request failed: {error}"))?;
            let status = response.status().as_u16();
            if (200..300).contains(&status) {
                break response;
            }
            match waits.next().filter(|_| BUSY.contains(&status)) {
                Some(wait) => std::thread::sleep(*wait),
                None => {
                    let said = response.body_mut().read_to_string().unwrap_or_default();
                    let detail: String = said.chars().take(300).collect();
                    return Err(format!("Gemini ({surface}) HTTP {status}: {detail}"));
                }
            }
        };
        let mut any = false;
        for line in BufReader::new(response.body_mut().as_reader()).lines() {
            let line = line.map_err(|error| format!("Gemini request failed: {error}"))?;
            for piece in sound_in(&line)? {
                any = true;
                encoder
                    .write_all(&piece)
                    .map_err(|error| format!("MP3 encode failed: {error}"))?;
            }
        }
        Ok(any)
    }
}

impl Speaker for Gemini {
    fn speak_turn(&self, line: Line<'_>, sink: &mut dyn FnMut(&[u8])) -> Result<u64, Mute> {
        self.speak(line, sink)
    }

    fn speak(&self, line: Line<'_>, sink: &mut dyn FnMut(&[u8])) -> Result<u64, Mute> {
        let Some(key) = (self.key)().filter(|key| !key.is_empty()) else {
            return Err(Mute::Unavailable("api_key required".to_string()));
        };
        if line.voice.is_empty() {
            return Err(Mute::Unavailable("voice required".to_string()));
        }
        let Some((program, given)) = self.encoder.split_first() else {
            return Err(Mute::Unavailable(
                "ffmpeg is required for Gemini TTS".to_string(),
            ));
        };
        let mut encoder = match clarp_child::command(program)
            .args(given)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
        {
            Ok(encoder) => encoder,
            Err(_) => {
                return Err(Mute::Unavailable(
                    "ffmpeg is required for Gemini TTS".to_string(),
                ));
            }
        };
        let (Some(stdin), Some(mut stdout)) = (encoder.stdin.take(), encoder.stdout.take()) else {
            let _ = encoder.kill();
            let _ = encoder.wait();
            return Err(Mute::Unavailable(
                "ffmpeg is required for Gemini TTS".to_string(),
            ));
        };
        let mut produced = 0u64;
        // The provider is asked on a thread of its own, which pours what comes into the
        // encoder; what the encoder makes of it is handed on from here, as it appears.
        let poured = std::thread::scope(|scope| {
            let asking = scope.spawn(|| self.pour(&key, line, stdin));
            let mut buffer = [0u8; 16 * 1024];
            loop {
                match stdout.read(&mut buffer) {
                    Ok(0) | Err(_) => break,
                    Ok(read) => {
                        produced += read as u64;
                        sink(&buffer[..read]);
                    }
                }
            }
            asking.join().unwrap_or_else(|_| {
                Err("Gemini request failed: the request was cut off".to_string())
            })
        });
        let begun = produced > 0;
        let ended = encoder.wait();
        let failed = |why: String| Mute::Failed { why, begun };
        let any = poured.map_err(failed)?;
        match ended {
            Ok(status) if status.success() => {}
            Ok(status) => {
                let mut said = String::new();
                if let Some(mut stderr) = encoder.stderr.take() {
                    let _ = stderr.read_to_string(&mut said);
                }
                let said: String = said.trim().chars().take(300).collect();
                let code = status
                    .code()
                    .map_or_else(|| "?".to_string(), |code| code.to_string());
                return Err(failed(format!("ffmpeg exited {code}: {said}")));
            }
            Err(error) => return Err(failed(format!("MP3 encode failed: {error}"))),
        }
        if !any || produced == 0 {
            return Err(failed("Gemini returned no audio".to_string()));
        }
        Ok(produced)
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use std::net::TcpListener;
    use std::sync::{Arc, Mutex};

    use super::*;

    const ONE: &str = r#"data: {"candidates":[{"content":{"parts":[{"inlineData":{"mimeType":"audio/L16","data":"cGNtLW9uZQ=="}}]}}]}"#;
    const TWO: &str = r#"data: {"candidates":[{"content":{"parts":[{"text":"no sound here"},{"inlineData":{"data":"cGNtLXR3bw=="}}]}}]}"#;

    /// A stand-in for Google on the loopback: answers one request after another with what
    /// it was given, and keeps what each request sent.
    fn stand_in(answers: Vec<(&'static str, String)>) -> (String, Arc<Mutex<Vec<String>>>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = format!(
            "http://{}/models/tts:streamGenerateContent?alt=sse",
            listener.local_addr().unwrap()
        );
        let taken: Arc<Mutex<Vec<String>>> = Arc::new(Mutex::new(Vec::new()));
        let keeps = taken.clone();
        std::thread::spawn(move || {
            for (status, body) in answers {
                let Ok((mut stream, _)) = listener.accept() else {
                    break;
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
                let reply = format!(
                    "HTTP/1.1 {status}\r\nContent-Type: text/event-stream\r\nContent-Length: {}\r\n\
                     Connection: close\r\n\r\n{body}",
                    body.len()
                );
                let _ = stream.write_all(reply.as_bytes());
            }
        });
        (address, taken)
    }

    fn events(lines: &[&str]) -> String {
        lines.iter().map(|line| format!("{line}\n\n")).collect()
    }

    fn engine(address: &str) -> Gemini {
        Gemini::new(
            "tts",
            Surface::Vertex,
            Box::new(|| Some("key-for-the-test".to_string())),
        )
        .at(address)
        .encoded_by(vec!["cat".to_string()])
        .tried_again_after(vec![Duration::ZERO, Duration::ZERO])
    }

    fn say(engine: &Gemini, voice: &str) -> (Result<u64, Mute>, Vec<u8>) {
        let mut heard = Vec::new();
        let line = Line {
            text: "A line to say.",
            voice,
        };
        let said = engine.speak(line, &mut |piece| heard.extend_from_slice(piece));
        (said, heard)
    }

    #[test]
    fn speech_child_drops_the_host_token() {
        if crate::run_with_fixture_host_token("gemini::tests::speech_child_drops_the_host_token") {
            return;
        }
        let script = "test -z \"${CLARP_HOST_TOKEN+x}\" || exit 9; exec cat";
        let (address, _) = stand_in(vec![("200 OK", events(&[ONE]))]);
        let codec = engine(&address).encoded_by(vec!["/bin/sh".into(), "-c".into(), script.into()]);
        let (result, bytes) = say(&codec, "Kore");
        assert!(result.is_ok());
        assert_eq!(bytes, b"pcm-one");
    }

    #[test]
    fn a_line_is_asked_for_as_the_python_host_asks_and_its_sound_is_handed_on() {
        let (address, taken) = stand_in(vec![("200 OK", events(&[ONE, ": a comment", TWO]))]);
        let (said, heard) = say(&engine(&address), "Kore");
        assert_eq!(said, Ok(14));
        assert_eq!(heard, b"pcm-onepcm-two");
        let request = taken.lock().unwrap()[0].clone();
        assert!(
            request.starts_with("POST /models/tts:streamGenerateContent?alt=sse "),
            "{request}"
        );
        assert!(
            request
                .to_lowercase()
                .contains("x-goog-api-key: key-for-the-test")
        );
        let body: Value = serde_json::from_str(request.split("\r\n\r\n").nth(1).unwrap()).unwrap();
        assert_eq!(
            body,
            json!({
                "contents": [{ "role": "user", "parts": [{ "text": "A line to say." }] }],
                "generationConfig": {
                    "responseModalities": ["AUDIO"],
                    "speechConfig": { "voiceConfig": { "prebuiltVoiceConfig": { "voiceName": "Kore" } } },
                },
            })
        );
        // A voice that was designed is named by its id.
        assert_eq!(voice_config("voice_abc"), json!({ "voice": "voice_abc" }));
        assert_eq!(from_base64("AAEC/w=="), [0, 1, 2, 255]);
        assert_eq!(from_base64("cGNt\nLW9u ZQ"), b"pcm-one");
        assert_eq!(
            Surface::Vertex.address("m"),
            "https://aiplatform.googleapis.com/v1/publishers/google/models/m:streamGenerateContent?alt=sse"
        );
        assert_eq!(Surface::by_name("gemini_api"), Some(Surface::GeminiApi));
        assert_eq!(Surface::by_name("elsewhere"), None);
    }

    #[test]
    fn a_provider_that_is_busy_is_asked_again_while_nothing_was_spoken() {
        let busy = || {
            (
                "503 Service Unavailable",
                r#"{"error":"high demand"}"#.to_string(),
            )
        };
        let (address, taken) = stand_in(vec![busy(), busy(), ("200 OK", events(&[ONE]))]);
        let (said, heard) = say(&engine(&address), "Kore");
        assert_eq!((said, heard.as_slice()), (Ok(7), &b"pcm-one"[..]));
        assert_eq!(taken.lock().unwrap().len(), 3);
        // Busy a third time: that is the answer, in the Python Host's words.
        let (address, _taken) = stand_in(vec![busy(), busy(), busy()]);
        let (said, heard) = say(&engine(&address), "Kore");
        assert_eq!(
            said,
            Err(Mute::Failed {
                why: r#"Gemini (vertex) HTTP 503: {"error":"high demand"}"#.to_string(),
                begun: false
            })
        );
        assert!(heard.is_empty());
        // What is no matter of being busy is not asked again.
        let (address, taken) = stand_in(vec![("400 Bad Request", "no such voice".to_string())]);
        let (said, _) = say(&engine(&address), "Nobody");
        assert_eq!(
            said,
            Err(Mute::Failed {
                why: "Gemini (vertex) HTTP 400: no such voice".to_string(),
                begun: false
            })
        );
        assert_eq!(taken.lock().unwrap().len(), 1);
    }

    #[test]
    fn what_goes_wrong_says_whether_anything_was_spoken() {
        // The provider says so in its answer, before any sound.
        let refused = r#"data: {"error":{"code":429,"message":"quota"}}"#;
        let (address, _) = stand_in(vec![("200 OK", events(&[refused]))]);
        let (said, heard) = say(&engine(&address), "Kore");
        assert_eq!(
            said,
            Err(Mute::Failed {
                why: r#"{"code":429,"message":"quota"}"#.to_string(),
                begun: false
            })
        );
        assert!(heard.is_empty());
        assert!(said.unwrap_err().may_be_said_by_another());
        // And after some: what was spoken was heard, and is not begun again in another voice.
        let (address, _) = stand_in(vec![("200 OK", events(&[ONE, refused]))]);
        let (said, heard) = say(&engine(&address), "Kore");
        assert_eq!(heard, b"pcm-one");
        let mute = said.unwrap_err();
        assert!(
            matches!(&mute, Mute::Failed { begun: true, .. }),
            "{mute:?}"
        );
        assert!(!mute.may_be_said_by_another());
        // An answer without sound.
        let (address, _) = stand_in(vec![("200 OK", events(&[": nothing"]))]);
        let (said, _) = say(&engine(&address), "Kore");
        assert_eq!(
            said,
            Err(Mute::Failed {
                why: "Gemini returned no audio".to_string(),
                begun: false
            })
        );
    }

    #[test]
    fn an_engine_that_cannot_be_used_as_it_stands_asks_nobody() {
        let (address, taken) = stand_in(vec![("200 OK", events(&[ONE]))]);
        let no_key = Gemini::new("tts", Surface::Vertex, Box::new(|| None)).at(address.clone());
        assert_eq!(
            say(&no_key, "Kore").0,
            Err(Mute::Unavailable("api_key required".to_string()))
        );
        assert_eq!(
            say(&engine(&address), "").0,
            Err(Mute::Unavailable("voice required".to_string()))
        );
        let no_encoder = engine(&address).encoded_by(vec!["no-such-encoder-anywhere".to_string()]);
        assert_eq!(
            say(&no_encoder, "Kore").0,
            Err(Mute::Unavailable(
                "ffmpeg is required for Gemini TTS".to_string()
            ))
        );
        assert!(taken.lock().unwrap().is_empty(), "nobody was asked");
        // An encoder that breaks: said with what it wrote of itself.
        let breaks = [
            "sh",
            "-c",
            "cat > /dev/null; echo 'no such codec' >&2; exit 3",
        ];
        let broken =
            engine(&address).encoded_by(breaks.iter().map(|word| (*word).to_string()).collect());
        assert_eq!(
            say(&broken, "Kore").0,
            Err(Mute::Failed {
                why: "ffmpeg exited 3: no such codec".to_string(),
                begun: false
            })
        );
    }
}
