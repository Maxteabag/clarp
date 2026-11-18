//! Google Chirp 3 HD returns a complete base64 MP3 in one response. Validate the
//! complete response before exposing any bytes to a listener.
use std::io::Read;
use std::sync::LazyLock;
use std::time::{Duration, Instant};

use base64::{Engine, engine::general_purpose::STANDARD};
use regex::Regex;
use serde_json::{Value, json};

use crate::markup::strip_emotion_tags;
use crate::speak::{Line, Mute, Speaker};

pub const ENDPOINT: &str = "https://texttospeech.googleapis.com/v1/text:synthesize";
const AUDIO_MAX: usize = 8 * 1024 * 1024;
const RESPONSE_MAX: usize = AUDIO_MAX * 4 / 3 + 64 * 1024;

#[allow(clippy::expect_used)]
static TAG: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?i)<(/?)(break|speed|volume|emotion)\b([^>]*)>").expect("speech markup")
});
#[allow(clippy::expect_used)]
static TIME: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r#"(?i)\btime\s*=\s*["'](\d{1,6}(?:\.\d{1,3})?)\s*(ms|s)["']"#)
        .expect("speech pause")
});

fn escape(text: &str) -> String {
    text.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
}

fn spoken(text: &str) -> Value {
    let text = strip_emotion_tags(text);
    let mut body = String::new();
    let mut at = 0;
    let mut paused = false;
    for tag in TAG.captures_iter(&text) {
        let Some(span) = tag.get(0) else { continue };
        body.push_str(&escape(&text[at..span.start()]));
        at = span.end();
        let millis = if tag[1].is_empty() && tag[2].eq_ignore_ascii_case("break") {
            TIME.captures(&tag[3]).and_then(|c| {
                c[1].parse::<f64>().ok().map(|v| {
                    let scale = if c[2].eq_ignore_ascii_case("s") {
                        1000.0
                    } else {
                        1.0
                    };
                    (v * scale).round_ties_even().min(3000.0) as u32
                })
            })
        } else {
            None
        };
        if let Some(ms) = millis.filter(|ms| *ms > 0) {
            paused = true;
            body.push_str(&format!("<break time=\"{ms}ms\"/>"));
        } else {
            body.push(' ');
        }
    }
    if paused {
        body.push_str(&escape(&text[at..]));
        let clean = body.replace(|c: char| c < ' ' && !matches!(c, '\t' | '\n' | '\r'), " ");
        json!({"ssml":format!("<speak>{}</speak>",clean.trim())})
    } else {
        let plain = TAG.replace_all(&text, " ");
        json!({"text":plain.split_whitespace().collect::<Vec<_>>().join(" ")})
    }
}

fn status_ok(status: u16) -> Result<(), &'static str> {
    match status {
        200 => Ok(()),
        401 | 403 => Err("google_auth_failed"),
        429 => Err("google_quota"),
        _ => Err("google_http_error"),
    }
}

fn decode(raw: &[u8]) -> Result<Vec<u8>, &'static str> {
    if raw.len() > RESPONSE_MAX {
        return Err("google_audio_too_large");
    }
    let data: Value = serde_json::from_slice(raw).map_err(|_| "google_bad_response")?;
    let encoded = data["audioContent"].as_str().ok_or("google_bad_response")?;
    if encoded.len() / 4 * 3 > AUDIO_MAX {
        return Err("google_audio_too_large");
    }
    let bytes = STANDARD
        .decode(encoded)
        .map_err(|_| "google_bad_response")?;
    if bytes.is_empty() {
        return Err("google_empty_audio");
    }
    Ok(bytes)
}

pub struct GoogleVoice {
    pub endpoint: String,
    pub token_url: String,
    pub account: Box<dyn Fn() -> Result<(std::path::PathBuf, String), String> + Send + Sync>,
    pub allowed: fn(&str) -> bool,
}

impl Speaker for GoogleVoice {
    fn speak(&self, line: Line<'_>, sink: &mut dyn FnMut(&[u8])) -> Result<u64, Mute> {
        let (path, project) = (self.account)().map_err(Mute::Unavailable)?;
        let failed = |code: &str| Mute::Failed {
            why: code.into(),
            begun: false,
        };
        if !(self.allowed)(line.voice) {
            return Err(failed("voice_unavailable"));
        }
        let input = spoken(line.text);
        if input["text"]
            .as_str()
            .or(input["ssml"].as_str())
            .is_some_and(|s| s.len() > 5000)
        {
            return Err(failed("text_too_long"));
        }
        let until = Instant::now() + Duration::from_secs(30);
        let token_url = if self.token_url.is_empty() {
            crate::chirp::TOKEN_URL
        } else {
            &self.token_url
        };
        let (token, _) = crate::chirp::Chirp::refresh_token(&path, token_url).map_err(|error| {
            let code = match error
                .split_once("Google token HTTP ")
                .and_then(|(_, after)| after.get(..3))
                .and_then(|s| s.parse::<u16>().ok())
            {
                Some(429) => "google_quota",
                Some(500..) => "google_http_error",
                Some(_) => "google_auth_failed",
                None if error.contains("timed out") => "google_timeout",
                None if error.contains("request failed") => "google_request_failed",
                None => "google_auth_failed",
            };
            failed(code)
        })?;
        let Some(remaining) = until.checked_duration_since(Instant::now()) else {
            return Err(failed("google_timeout"));
        };
        let agent: ureq::Agent = ureq::Agent::config_builder()
            .max_redirects(0)
            .timeout_global(Some(remaining))
            .timeout_recv_body(Some(Duration::from_secs(10)))
            .http_status_as_error(false)
            .build()
            .into();
        let url = if self.endpoint.is_empty() {
            ENDPOINT
        } else {
            &self.endpoint
        };
        let lang = line
            .voice
            .rsplit_once("-Chirp3-HD-")
            .map_or(line.voice, |(lang, _)| lang);
        let request = json!({"input":input,"voice":{"languageCode":lang,"name":line.voice},"audioConfig":{"audioEncoding":"MP3"}});
        let mut response = agent
            .post(url)
            .header("Authorization", format!("Bearer {token}"))
            .header("x-goog-user-project", project)
            .send_json(request)
            .map_err(|e| {
                failed(if matches!(e, ureq::Error::Timeout(_)) {
                    "google_timeout"
                } else {
                    "google_request_failed"
                })
            })?;
        status_ok(response.status().as_u16()).map_err(failed)?;
        let mut raw = Vec::new();
        response
            .body_mut()
            .as_reader()
            .take(RESPONSE_MAX as u64 + 1)
            .read_to_end(&mut raw)
            .map_err(|_| failed("google_request_failed"))?;
        let bytes = decode(&raw).map_err(failed)?;
        for chunk in bytes.chunks(64 * 1024) {
            sink(chunk);
        }
        Ok(bytes.len() as u64)
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;
    #[test]
    fn google_input_matches_reference_markup_and_bounds() {
        let v: Value = serde_json::from_str(include_str!(
            "../../clarp-kernel/tests/fixtures/speech-engines-vectors.json"
        ))
        .unwrap();
        for case in v["google_input"].as_array().unwrap() {
            assert_eq!(
                spoken(case["text"].as_str().unwrap()),
                case["spoken"],
                "{case}"
            );
        }
    }
    #[test]
    fn google_http_results_follow_the_reference() {
        let v: Value = serde_json::from_str(include_str!(
            "../../clarp-kernel/tests/fixtures/speech-engines-vectors.json"
        ))
        .unwrap();
        for case in v["google_http"].as_array().unwrap() {
            let result = status_ok(case["status"].as_u64().unwrap() as u16)
                .and_then(|()| decode(case["payload"].to_string().as_bytes()));
            match result {
                Ok(bytes) => assert_eq!(case["chunks"][0], String::from_utf8(bytes).unwrap()),
                Err(code) => assert_eq!(case["error"], code),
            }
        }
        assert_eq!(decode(b"{"), Err("google_bad_response"));
        assert_eq!(
            decode(&vec![b'x'; RESPONSE_MAX + 1]),
            Err("google_audio_too_large")
        );
    }
}
