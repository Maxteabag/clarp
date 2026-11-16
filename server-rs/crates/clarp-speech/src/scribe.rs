//! ElevenLabs Scribe: an engine that listens, reached over the network.
//!
//! A clip is handed over as a form with one file in it, with the model, the language when it
//! is known, and each word to listen for. The answer is one text. The request is the Python
//! Host's (`server/lib/eleven_stt.py`), and so are the words it fails with.

use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

use serde_json::Value;

use crate::listen::{Asked, Clip, Deaf, Heard, Listener, file_name, terms};

/// Where ElevenLabs takes a clip.
pub const ENDPOINT: &str = "https://api.elevenlabs.io/v1/speech-to-text";

/// Where an engine's key is read when a clip is to be heard. It is asked every time, so a
/// key that was changed is followed, and none is kept by the engine.
pub type Key = Box<dyn Fn() -> Option<String> + Send + Sync>;

pub struct Scribe {
    key: Key,
    /// The model, by ElevenLabs' own name (`scribe_v2`).
    model: String,
    endpoint: String,
    timeout: Duration,
    forms: AtomicU64,
}

impl Scribe {
    pub fn new(model: impl Into<String>, key: Key) -> Self {
        Self {
            key,
            model: model.into(),
            endpoint: ENDPOINT.to_string(),
            timeout: Duration::from_secs(30),
            forms: AtomicU64::new(0),
        }
    }

    /// The same engine at another address: a stand-in that answers as ElevenLabs does.
    pub fn at(mut self, endpoint: impl Into<String>) -> Self {
        self.endpoint = endpoint.into();
        self
    }

    /// The fields of the form, before the file. A set of languages is not handed over: it
    /// would be taken as the one language to write in, and Scribe finds the language itself.
    /// Tagging of sounds is off: a tag ("(laughter)") would become part of the message.
    fn fields(&self, asked: &Asked<'_>) -> Vec<(&'static str, String)> {
        let mut fields = vec![
            ("model_id", self.model.clone()),
            ("tag_audio_events", "false".to_string()),
        ];
        let one = |language: &&str| !language.is_empty() && !language.contains(',');
        if let Some(language) = asked.language.filter(one) {
            fields.push(("language_code", language.to_string()));
        }
        // Each word is a field of its own, cut to what the provider takes of one.
        fields.extend(
            terms(asked.listen_for)
                .into_iter()
                .map(|term| ("keyterms", term.chars().take(49).collect())),
        );
        fields
    }
}

/// A form with fields and one file, as it goes over the wire.
pub fn form(
    boundary: &str,
    fields: &[(&str, String)],
    file: (&str, &str),
    content_type: &str,
    payload: &[u8],
) -> Vec<u8> {
    let mut out = Vec::with_capacity(payload.len() + 512);
    for (name, value) in fields {
        let part = format!(
            "--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n"
        );
        out.extend_from_slice(part.as_bytes());
    }
    let (field, name) = file;
    let head = format!(
        "--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; filename=\"{name}\"\r\n\
         Content-Type: {content_type}\r\n\r\n"
    );
    out.extend_from_slice(head.as_bytes());
    out.extend_from_slice(payload);
    out.extend_from_slice(format!("\r\n--{boundary}--\r\n").as_bytes());
    out
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
    format!("ElevenLabs HTTP {status}: {detail}")
}

/// What an answer says was heard: its text, and where the last word ends.
fn heard(answer: &Value) -> Result<Heard, Deaf> {
    let Some(text) = answer["text"].as_str() else {
        return Err(Deaf::Failed("ElevenLabs response had no text".to_string()));
    };
    let ends = answer["words"]
        .as_array()
        .and_then(|words| words.last())
        .and_then(|word| word["end"].as_f64());
    Ok(Heard::text(text, ends.unwrap_or(0.0)))
}

impl Listener for Scribe {
    fn hear(&self, clip: Clip<'_>, asked: Asked<'_>) -> Result<Heard, Deaf> {
        let Some(key) = (self.key)().filter(|key| !key.is_empty()) else {
            return Err(Deaf::Failed(
                "ElevenLabs API key is not configured".to_string(),
            ));
        };
        let content_type = match clip.content_type {
            "" => "audio/webm",
            given => given,
        };
        let number = self.forms.fetch_add(1, Ordering::Relaxed);
        let boundary = format!("----clarp{:016x}{number:08x}", clip.audio.len());
        let body = form(
            &boundary,
            &self.fields(&asked),
            ("file", file_name(content_type)),
            content_type,
            clip.audio,
        );
        let config = ureq::Agent::config_builder()
            .timeout_global(Some(self.timeout))
            .http_status_as_error(false)
            .build();
        let agent: ureq::Agent = config.into();
        let mut response = agent
            .post(&self.endpoint)
            .header("xi-api-key", key.as_str())
            .header(
                "Content-Type",
                format!("multipart/form-data; boundary={boundary}"),
            )
            .send(&body[..])
            // How it failed, and nothing of what was sent: the error of a transport can
            // name the address, never the key or the clip.
            .map_err(|error| Deaf::Failed(format!("ElevenLabs request failed: {error}")))?;
        let status = response.status().as_u16();
        let said = response.body_mut().read_to_string().unwrap_or_default();
        if !(200..300).contains(&status) {
            return Err(Deaf::Failed(refused(status, &said)));
        }
        let answer: Value = serde_json::from_str(&said)
            .map_err(|error| Deaf::Failed(format!("ElevenLabs request failed: {error}")))?;
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
    fn a_form_is_written_as_the_python_host_writes_it() {
        let fields = vec![
            ("model_id", "scribe_v2".to_string()),
            ("tag_audio_events", "false".to_string()),
        ];
        let made = form(
            "----clarpB",
            &fields,
            ("file", "audio.m4a"),
            "audio/mp4",
            b"\x00\x01clip",
        );
        let expected = b"------clarpB\r\nContent-Disposition: form-data; name=\"model_id\"\r\n\r\nscribe_v2\r\n\
------clarpB\r\nContent-Disposition: form-data; name=\"tag_audio_events\"\r\n\r\nfalse\r\n\
------clarpB\r\nContent-Disposition: form-data; name=\"file\"; filename=\"audio.m4a\"\r\n\
Content-Type: audio/mp4\r\n\r\n\x00\x01clip\r\n------clarpB--\r\n";
        assert_eq!(made, expected);
    }

    #[test]
    fn the_fields_say_the_model_the_language_and_each_word_to_listen_for() {
        let scribe = Scribe::new("scribe_v2", with_key());
        let long = "x".repeat(60);
        let line = format!("Clarp, Maximus, {long}");
        let fields = scribe.fields(&asked(&line, Some("en")));
        let named: Vec<(&str, &str)> = fields
            .iter()
            .map(|(name, value)| (*name, value.as_str()))
            .collect();
        assert_eq!(
            named,
            [
                ("model_id", "scribe_v2"),
                ("tag_audio_events", "false"),
                ("language_code", "en"),
                ("keyterms", "Clarp"),
                ("keyterms", "Maximus"),
                ("keyterms", &long[..49]),
            ]
        );
        // A set of languages is left to the engine, and so is none.
        for language in [Some("en,nb"), None, Some("")] {
            let fields = scribe.fields(&asked("", language));
            assert_eq!(fields.len(), 2, "{language:?}");
        }
    }

    #[test]
    fn an_answer_is_read_and_a_refusal_is_said_in_the_python_hosts_words() {
        let said = heard(&json!({"text": "Open the log.", "words": [{"end": 0.4}, {"end": 1.25}]}))
            .unwrap();
        assert_eq!(said, Heard::text("Open the log.", 1.25));
        assert_eq!(heard(&json!({"text": ""})).unwrap(), Heard::text("", 0.0));
        assert_eq!(
            heard(&json!({"detail": "nothing"})),
            Err(Deaf::Failed("ElevenLabs response had no text".to_string()))
        );
        assert_eq!(
            refused(401, &"k".repeat(300)),
            format!("ElevenLabs HTTP 401: {}", "k".repeat(200))
        );
        let no_key = Scribe::new("scribe_v2", Box::new(|| None));
        let clip = Clip {
            audio: b"clip",
            content_type: "audio/wav",
        };
        assert_eq!(
            no_key.hear(clip, asked("", None)),
            Err(Deaf::Failed(
                "ElevenLabs API key is not configured".to_string()
            ))
        );
    }

    /// A stand-in for ElevenLabs on the loopback: takes one request, hands what it was sent
    /// to the test, and answers with what the test gave it.
    fn stand_in(status: &'static str, answer: &'static str) -> (String, mpsc::Receiver<String>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = format!(
            "http://{}/v1/speech-to-text",
            listener.local_addr().unwrap()
        );
        let (sent, received) = mpsc::channel();
        std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut seen = Vec::new();
            let mut buffer = [0u8; 4096];
            // The head, then as many bytes of body as the head says.
            let (head_end, length) = loop {
                let read = stream.read(&mut buffer).unwrap();
                seen.extend_from_slice(&buffer[..read]);
                let text = String::from_utf8_lossy(&seen).to_string();
                if let Some(at) = text.find("\r\n\r\n") {
                    let length = text
                        .lines()
                        .find_map(|line| {
                            let (name, value) = line.split_once(':')?;
                            name.eq_ignore_ascii_case("content-length")
                                .then(|| value.trim().parse::<usize>().ok())?
                        })
                        .unwrap_or(0);
                    break (at + 4, length);
                }
                assert!(read > 0, "the request ended before its head did");
            };
            while seen.len() < head_end + length {
                let read = stream.read(&mut buffer).unwrap();
                assert!(read > 0, "the request ended before its body did");
                seen.extend_from_slice(&buffer[..read]);
            }
            sent.send(String::from_utf8_lossy(&seen).to_string())
                .unwrap();
            let reply = format!(
                "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\
                 Connection: close\r\n\r\n{answer}",
                answer.len()
            );
            stream.write_all(reply.as_bytes()).unwrap();
        });
        (address, received)
    }

    #[test]
    fn a_clip_is_sent_with_the_key_and_what_was_heard_comes_back() {
        let (address, received) = stand_in(
            "200 OK",
            r#"{"text":" Open the log. ","words":[{"end":2.5}]}"#,
        );
        let scribe = Scribe::new("scribe_v2", with_key()).at(address);
        let clip = Clip {
            audio: b"made-up audio",
            content_type: "audio/mp4",
        };
        let said = scribe.hear(clip, asked("Clarp", Some("en"))).unwrap();
        assert_eq!(said, Heard::text(" Open the log. ", 2.5));
        let request = received.recv().unwrap();
        let lower = request.to_lowercase();
        assert!(request.starts_with("POST /v1/speech-to-text "), "{request}");
        assert!(lower.contains("xi-api-key: key-for-the-test"));
        assert!(lower.contains("content-type: multipart/form-data; boundary=----clarp"));
        assert!(request.contains("name=\"model_id\"\r\n\r\nscribe_v2\r\n"));
        assert!(request.contains("name=\"language_code\"\r\n\r\nen\r\n"));
        assert!(request.contains("name=\"keyterms\"\r\n\r\nClarp\r\n"));
        assert!(request.contains(
            "filename=\"audio.m4a\"\r\nContent-Type: audio/mp4\r\n\r\nmade-up audio\r\n"
        ));
    }

    #[test]
    fn a_provider_that_refuses_is_quoted_and_one_that_is_not_there_is_said_so() {
        let (address, _received) = stand_in("401 Unauthorized", r#"{"detail":"invalid key"}"#);
        let scribe = Scribe::new("scribe_v2", with_key()).at(address);
        let clip = Clip {
            audio: b"clip",
            content_type: "",
        };
        assert_eq!(
            scribe.hear(clip, asked("", None)),
            Err(Deaf::Failed(
                r#"ElevenLabs HTTP 401: {"detail":"invalid key"}"#.to_string()
            ))
        );
        // Nobody listens there.
        let gone = TcpListener::bind("127.0.0.1:0").unwrap();
        let address = format!("http://{}/v1/speech-to-text", gone.local_addr().unwrap());
        drop(gone);
        let scribe = Scribe::new("scribe_v2", with_key()).at(address);
        let Err(Deaf::Failed(why)) = scribe.hear(clip, asked("", None)) else {
            panic!("a request to nobody fails");
        };
        assert!(why.starts_with("ElevenLabs request failed: "), "{why}");
        assert!(!why.contains("key-for-the-test"));
    }
}
