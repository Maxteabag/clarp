//! Deepgram speaks the line as MP3. Flux and Aura use different API versions.
use std::io::Read;
use std::time::Duration;

use serde_json::json;

use crate::chirp::form_encoded;
use crate::markup::strip_ssml_for_plain_tts;
use crate::scribe::Key;
use crate::speak::{Line, Mute, Speaker};

const API: &str = "https://api.deepgram.com";

pub struct DeepgramVoice {
    pub key: Key,
    /// Empty in production, otherwise the Host's validated stand-in base URL.
    pub base: String,
}

fn url(base: &str, voice: &str) -> String {
    let version = if voice.starts_with("flux-") { "v2" } else { "v1" };
    let query = form_encoded(&[("model", voice), ("encoding", "mp3"), ("container", "none")]);
    format!("{}/{version}/speak?{query}", base.trim_end_matches('/'))
}

impl Speaker for DeepgramVoice {
    fn speak(&self, line: Line<'_>, sink: &mut dyn FnMut(&[u8])) -> Result<u64, Mute> {
        let key=(self.key)().filter(|key| !key.is_empty()).ok_or_else(||Mute::Unavailable("Deepgram API key is not configured".into()))?;
        if line.voice.is_empty() {return Err(Mute::Unavailable("Deepgram voice is not configured".into()));}
        let endpoint=url(if self.base.is_empty() {API} else {&self.base},line.voice);
        let agent:ureq::Agent=ureq::Agent::config_builder().max_redirects(0).timeout_global(Some(Duration::from_secs(30))).http_status_as_error(false).build().into();
        let failed=|why:String,begun:bool| Mute::Failed{why,begun};
        let mut response=agent.post(endpoint).header("Authorization",format!("Token {key}")).header("Accept","audio/mpeg")
            .send_json(json!({"text":strip_ssml_for_plain_tts(line.text)}))
            .map_err(|e|failed(format!("Deepgram request failed: {e}"),false))?;
        if !response.status().is_success() {return Err(failed(format!("Deepgram HTTP {}",response.status()),false));}
        let mut reader=response.body_mut().as_reader();
        let mut buf=[0u8;16*1024];
        let mut produced=0;
        loop {
            let n=reader.read(&mut buf).map_err(|e|failed(format!("Deepgram request failed: {e}"),produced>0))?;
            if n==0 {break;}
            sink(&buf[..n]);
            produced+=n as u64;
        }
        if produced==0 {return Err(failed("Deepgram returned empty audio".into(),false));}
        Ok(produced)
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;
    #[test]
    fn both_deepgram_versions_send_the_reference_request() {
        let fixture:serde_json::Value=serde_json::from_str(include_str!("../../clarp-kernel/tests/fixtures/speech-engines-vectors.json")).unwrap();
        for case in fixture["deepgram"].as_array().unwrap() {
            assert_eq!(url(API,case["voice"].as_str().unwrap()),case["request"]["url"]);
            assert_eq!(strip_ssml_for_plain_tts(case["text"].as_str().unwrap()),case["request"]["body"]["text"]);
        }
    }
}
