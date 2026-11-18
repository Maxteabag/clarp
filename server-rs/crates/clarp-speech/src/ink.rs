//! Ink-Whisper listens to a file. Ink-2 accepts vocabulary over its PCM socket.
use serde_json::Value;
use std::time::{Duration, Instant};
use tungstenite::{Message, client::IntoClientRequest};

use crate::{
    chirp,
    listen::{Asked, Clip, Deaf, Heard, Listener, file_name, terms},
    scribe,
};

pub struct Ink {
    pub model: &'static str,
    pub key: scribe::Key,
    /// Base URL of a local stand-in. Empty selects the provider.
    pub base: String,
    pub decoder: Vec<String>,
}

const VERSION: &str = "2026-08-14";

impl Listener for Ink {
    fn hear(&self, clip: Clip<'_>, asked: Asked<'_>) -> Result<Heard, Deaf> {
        let key = (self.key)()
            .filter(|s| !s.is_empty())
            .ok_or_else(|| Deaf::Failed("Cartesia API key is not configured".into()))?;
        let language = asked.language.unwrap_or("en");
        if self.model == "ink-2" {
            return self.socket(clip, language, asked.listen_for, &key);
        }
        let content = if clip.content_type.is_empty() {
            "audio/webm"
        } else {
            clip.content_type
        };
        let boundary = format!("----clarp{:016x}", clip.audio.len());
        let body = scribe::form(
            &boundary,
            &[("model", self.model.into()), ("language", language.into())],
            ("file", file_name(content)),
            content,
            clip.audio,
        );
        let base = if self.base.is_empty() {
            "https://api.cartesia.ai"
        } else {
            self.base.trim_end_matches('/')
        };
        let agent: ureq::Agent = ureq::Agent::config_builder()
            .max_redirects(0)
            .timeout_global(Some(Duration::from_secs(60)))
            .http_status_as_error(false)
            .build()
            .into();
        let mut res = agent
            .post(format!("{base}/stt"))
            .header("Authorization", format!("Bearer {key}"))
            .header("Cartesia-Version", VERSION)
            .header(
                "Content-Type",
                format!("multipart/form-data; boundary={boundary}"),
            )
            .send(body.as_slice())
            .map_err(|e| Deaf::Failed(format!("Cartesia request failed: {e}")))?;
        if !res.status().is_success() {
            return Err(Deaf::Failed(format!("Cartesia HTTP {}", res.status())));
        }
        let data: Value = res
            .body_mut()
            .read_json()
            .map_err(|e| Deaf::Failed(format!("Cartesia request failed: {e}")))?;
        let text = data["text"]
            .as_str()
            .ok_or_else(|| Deaf::Failed("Cartesia response had no text".into()))?;
        let duration = data["duration"]
            .as_f64()
            .or_else(|| data["duration"].as_str()?.trim().parse().ok())
            .unwrap_or(0.0);
        Ok(Heard::text(text, duration))
    }
}

impl Ink {
    pub fn new(model: &'static str, key: scribe::Key, base: String, decoder: &str) -> Self {
        let decoder = if decoder.is_empty() {
            chirp::pcm_decoder()
        } else {
            vec![decoder.into()]
        };
        Self {
            model,
            key,
            base,
            decoder,
        }
    }
    fn url(&self,language:&str,listen_for:&str)->String{
        let words = terms(listen_for);
        let mut fields = vec![
            ("model", self.model),
            ("encoding", "pcm_s16le"),
            ("sample_rate", "16000"),
            ("cartesia_version", VERSION),
            ("language", language),
        ];
        let mut chars = 0;
        for word in words
            .iter()
            .map(|s| s.trim())
            .filter(|s| !s.is_empty())
            .take(100)
        {
            chars += word.chars().count();
            if chars > 1200 {
                break;
            }
            fields.push(("keyterm", word));
        }
        let base = if self.base.is_empty() {
            "wss://api.cartesia.ai".to_string()
        } else {
            self.base
                .trim_end_matches('/')
                .replacen("http://", "ws://", 1)
        };
        format!("{base}/stt/websocket?{}", chirp::form_encoded(&fields))
    }
    fn socket(
        &self,
        clip: Clip<'_>,
        language: &str,
        listen_for: &str,
        key: &str,
    ) -> Result<Heard, Deaf> {
        let pcm = chirp::pcm(&self.decoder, clip.audio).map_err(Deaf::Failed)?;
        let url=self.url(language,listen_for);
        let mut req = url
            .into_client_request()
            .map_err(|_| Deaf::Failed("invalid Cartesia speech endpoint".into()))?;
        req.headers_mut().insert(
            "X-API-Key",
            key.parse()
                .map_err(|_| Deaf::Failed("invalid Cartesia API key".into()))?,
        );
        let start = Instant::now();
        let mut ws = crate::speech_socket::open(req, Duration::from_secs(30))
            .map_err(|e| Deaf::Failed(e.into()))?;
        let failed = |_| Deaf::Failed("Cartesia speech socket failed".into());
        for chunk in pcm.chunks(3200) {
            ws.send(Message::Binary(chunk.to_vec().into()))
                .map_err(failed)?;
        }
        for op in ["finalize", "close"] {
            ws.send(Message::Text(op.into())).map_err(failed)?;
        }
        let mut text = String::new();
        loop {
            let frame = ws.read().map_err(failed)?;
            if frame.is_close() {
                break;
            }
            if !(frame.is_text() || frame.is_binary()) {
                continue;
            }
            let data = frame.into_data();
            if data.is_empty() {
                break;
            }
            let Ok(event) = serde_json::from_slice::<Value>(&data) else {
                continue;
            };
            match event["type"].as_str() {
                Some("done") => break,
                Some("error") => {
                    return Err(Deaf::Failed(
                        event["message"]
                            .as_str()
                            .or(event["title"].as_str())
                            .unwrap_or("cartesia stt stream error")
                            .into(),
                    ));
                }
                Some("transcript") if event["is_final"] == true => {
                    text.push_str(event["text"].as_str().unwrap_or(""))
                }
                _ => {}
            }
        }
        let _ = ws.close(None);
        Ok(Heard::text(text.trim(), start.elapsed().as_secs_f64()))
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests{
    use super::*;
    #[test]
    fn ink_urls_preserve_vocabulary_order_and_limits(){
        let v:Value=serde_json::from_str(include_str!("../../clarp-kernel/tests/fixtures/speech-engines-vectors.json")).unwrap();
        let ink=Ink::new("ink-2",Box::new(||None),String::new(),"cat");
        for case in v["ink_queries"].as_array().unwrap(){
            let terms=case["words"].as_array().unwrap().iter().map(|s|s.as_str().unwrap()).collect::<Vec<_>>().join(",");
            assert_eq!(ink.url("nb",&terms),case["url"]);
        }
    }
}
