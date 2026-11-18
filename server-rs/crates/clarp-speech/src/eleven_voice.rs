//! ElevenLabs accepts three text frames, then streams base64 MP3 until its final frame.
use std::time::Duration;

use base64::{Engine, engine::general_purpose::STANDARD};
use serde_json::{Value, json};
use tungstenite::Message;

use crate::{
    scribe::Key,
    speak::{Line, Mute, Speaker},
};

pub struct ElevenVoice {
    pub key: Key,
    pub model: String,
    pub speed: f64,
    pub file:bool,
    /// Empty for ElevenLabs itself; an explicit base is a loopback test server.
    pub base: String,
}

fn frames(text: &str, key: &str, speed: f64) -> [Value; 3] {
    [
        json!({"text":" ","xi_api_key":key,"voice_settings":{"stability":0.5,"similarity_boost":0.75,"speed":speed},
        "generation_config":{"chunk_length_schedule":[50,90,120,150,200]}}),
        json!({"text":text}),
        json!({"text":""}),
    ]
}

impl Speaker for ElevenVoice {
    fn speak(&self, line: Line<'_>, sink: &mut dyn FnMut(&[u8])) -> Result<u64, Mute> {
        let key = (self.key)()
            .filter(|k| !k.is_empty())
            .ok_or_else(|| Mute::Unavailable("api_key required".into()))?;
        if line.voice.is_empty() {
            return Err(Mute::Unavailable("voice_id required".into()));
        }
        if self.file {
            let base=if self.base.is_empty(){"https://api.elevenlabs.io"}else{self.base.trim_end_matches('/')};
            let url=format!("{base}/v1/text-to-speech/{}",line.voice);
            let agent:ureq::Agent=ureq::Agent::config_builder().max_redirects(0).http_status_as_error(false).timeout_global(Some(Duration::from_secs(20))).build().into();
            let fail=|why:String|Mute::Failed{why,begun:false};
            let body=json!({"text":line.text,"model_id":self.model,"voice_settings":{"stability":0.5,"similarity_boost":0.75,"speed":self.speed}});
            let mut r=agent.post(url).header("xi-api-key",key).header("Accept","audio/mpeg").send_json(body).map_err(|e|fail(format!("ElevenLabs request failed: {e}")))?;
            if !r.status().is_success(){return Err(fail(format!("ElevenLabs HTTP {}",r.status())));}
            let bytes=r.body_mut().with_config().limit(64*1024*1024).read_to_vec().map_err(|_|fail("ElevenLabs audio could not be read".into()))?;
            if bytes.is_empty(){return Err(fail("ElevenLabs returned empty body".into()));}
            sink(&bytes);
            return Ok(bytes.len() as u64);
        }
        let base = if self.base.is_empty() {
            "wss://api.elevenlabs.io".into()
        } else {
            self.base.replacen("http://", "ws://", 1)
        };
        let url = format!(
            "{}/v1/text-to-speech/{}/stream-input?model_id={}&output_format=mp3_44100_128",
            base.trim_end_matches('/'),
            line.voice,
            self.model
        );
        let failed = |why: &str, begun| Mute::Failed {
            why: why.into(),
            begun,
        };
        let request = tungstenite::client::IntoClientRequest::into_client_request(url.as_str())
            .map_err(|_| failed("invalid ElevenLabs endpoint", false))?;
        let mut ws = crate::speech_socket::open(request, Duration::from_secs(30))
            .map_err(|why| failed(why, false))?;
        for frame in frames(line.text, &key, self.speed) {
            ws.send(Message::Text(frame.to_string().into()))
                .map_err(|_| failed("WS send failed", false))?;
        }
        let mut produced = 0;
        loop {
            let begun = produced > 0;
            let frame = ws
                .read()
                .map_err(|_| failed("WS closed before isFinal", begun))?;
            let bytes = match frame {
                Message::Text(s) => s.as_bytes().to_vec(),
                Message::Binary(b) => b.to_vec(),
                Message::Ping(_) | Message::Pong(_) => continue,
                _ => return Err(failed("WS closed before isFinal", begun)),
            };
            if bytes.is_empty() {
                return Err(failed("empty frame from server", begun));
            }
            let msg: Value = serde_json::from_slice(&bytes)
                .map_err(|_| failed("server sent non-JSON frame", begun))?;
            let audio = msg
                .get("audio")
                .ok_or_else(|| failed("ElevenLabs refused synthesis", begun))?;
            if let Some(encoded) = audio.as_str().filter(|s| !s.is_empty()) {
                let cleaned: Vec<u8> = encoded
                    .bytes()
                    .filter(|c| c.is_ascii_alphanumeric() || matches!(c, b'+' | b'/' | b'='))
                    .collect();
                let decoded = STANDARD
                    .decode(cleaned)
                    .map_err(|_| failed("bad base64 from server", begun))?;
                sink(&decoded);
                produced += decoded.len() as u64;
            }
            if msg["isFinal"] == true {
                break;
            }
        }
        let _ = ws.close(None);
        Ok(produced)
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;
    #[test]
    fn elevenlabs_frames_match_the_reference() {
        let v: Value = serde_json::from_str(include_str!(
            "../../clarp-kernel/tests/fixtures/speech-engines-vectors.json"
        ))
        .unwrap();
        assert_eq!(
            json!(frames("A line.", "unit", 1.1)),
            v["eleven"][0]["sent"]
        );
    }
}
