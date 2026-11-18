//! Gemini Live in the event vocabulary of an Oracle call. Times are monotonic seconds.
use super::said;
use super::usage::round;
use crate::python::truthy;
use serde_json::{Value, json};
use std::collections::BTreeMap;

pub const SETUP_SECONDS: u64 = 10;
pub fn healthy(now: f64, failed_at: Option<f64>) -> bool {
    failed_at.is_none_or(|at| now - at >= 600.)
}

#[derive(Debug)]
pub enum Effect {
    Send(Value),
    Audio(Value),
    Event(Value),
}
#[derive(Default)]
struct Request {
    parts: Vec<String>,
    speak: bool,
    cancelled: bool,
}
pub struct Gemini {
    model: String,
    voice: String,
    pub languages: Vec<String>,
    opened: f64,
    setup: Option<Value>,
    pub began: bool,
    pub closed: bool,
    handle: Value,
    deadline: Option<f64>,
    reconnects: usize,
    calls: BTreeMap<String, Request>,
    routing: Option<String>,
    speaking: bool,
    muted: bool,
    tokens: BTreeMap<String, i64>,
    estimate: f64,
}
fn list(value: &Value) -> &[Value] {
    value.as_array().map_or(&[], Vec::as_slice)
}
const DELEGATE: &str = "delegate_to_agent";

impl Gemini {
    pub fn new(model: String, voice: String, opened: f64) -> Self {
        Self {
            model,
            voice,
            opened,
            languages: Vec::new(),
            setup: None,
            began: false,
            closed: false,
            handle: Value::Null,
            deadline: None,
            reconnects: 0,
            calls: BTreeMap::new(),
            routing: None,
            speaking: false,
            muted: false,
            tokens: BTreeMap::new(),
            estimate: 0.,
        }
    }
    fn setup(&self, session: &Value) -> Value {
        let mut setup: Value =
            serde_json::from_str(include_str!("gemini-setup.json")).unwrap_or_default();
        let mut words = said(&session["instructions"]);
        words.push_str(
            setup["systemInstruction"]["parts"][0]["text"]
                .as_str()
                .unwrap_or(""),
        );
        let history = list(&session["input"])
            .iter()
            .flat_map(|v| list(&v["content"]))
            .filter_map(|v| v["text"].as_str())
            .collect::<Vec<_>>()
            .join("\n\n");
        if !history.is_empty() {
            words.push_str("\n\n[Host, context] ");
            words.push_str(&history);
        }
        setup["model"] = json!(format!("models/{}", self.model));
        setup["generationConfig"]["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"]["voiceName"] =
            json!(self.voice);
        setup["systemInstruction"]["parts"][0]["text"] = json!(words);
        if !self.languages.is_empty() {
            setup["inputAudioTranscription"] = json!({"languageCodes": self.languages});
        }
        setup
    }
    pub fn send(&mut self, event: &Value, now: f64) -> Vec<Effect> {
        let effect = match event["type"].as_str() {
            Some("session.start") => {
                let setup = self.setup(&event["session"]);
                self.setup = Some(setup.clone());
                Effect::Send(json!({"setup":setup}))
            }
            Some("session.input_audio.append") => Effect::Audio(
                json!({"realtimeInput":{"audio":{"data":event["audio"],"mimeType":"audio/pcm;rate=24000"}}}),
            ),
            Some(
                kind @ ("session.commentary.append"
                | "session.thinking.append"
                | "session.instructions.append"),
            ) => {
                return self
                    .context(kind == "session.commentary.append", said(&event["content"]))
                    .into_iter()
                    .collect();
            }
            Some("session.close") => {
                self.closed = true;
                Effect::Event(
                    json!({"type":"session.closed","reason":"client_closed","usage":self.usage(now)}),
                )
            }
            _ => return vec![],
        };
        vec![effect]
    }
    fn context(&mut self, speak: bool, text: String) -> Option<Effect> {
        if let Some(request) = self.routing.as_ref().and_then(|id| self.calls.get_mut(id)) {
            request.parts.push(text);
            request.speak |= speak;
            return None;
        }
        let label = if speak {
            "[Host, speak] "
        } else {
            "[Host, context] "
        };
        Some(Effect::Send(
            json!({"clientContent":{"turns":[{"role":"user","parts":[{"text":format!("{label}{text}")}]}],"turnComplete":speak}}),
        ))
    }
    pub fn interrupt(&mut self) {
        self.muted |= self.speaking;
    }
    pub fn routing(&mut self, id: &str) {
        if self.calls.contains_key(id) {
            self.routing = Some(id.into());
        }
    }
    pub fn finished(&mut self, id: &str) -> Vec<Effect> {
        if self.routing.as_deref() == Some(id) {
            self.routing = None;
        }
        let Some(request) = self.calls.remove(id) else {
            return vec![];
        };
        if request.cancelled {
            return request
                .parts
                .into_iter()
                .filter_map(|text| self.context(request.speak, text))
                .collect();
        }
        let result = request.parts.join("\n\n");
        let result = if result.is_empty() {
            "Sent to the Host. Any agent work reports back later; do not say it is done."
        } else {
            &result
        };
        vec![response(
            id,
            &json!(DELEGATE),
            json!({"result":result,"scheduling":"WHEN_IDLE"}),
        )]
    }
    pub fn usage(&self, now: f64) -> Value {
        json!({"seconds":round(now-self.opened,1),"tokens":self.tokens,"voice_estimate_usd":round(self.estimate,6),"reconnects":self.reconnects})
    }
    fn count(&mut self, usage: &Value) {
        let mut cost = 0.;
        for side in ["prompt", "response"] {
            for row in list(&usage[format!("{side}TokensDetails")]) {
                let Some(count) = row["tokenCount"].as_i64() else {
                    continue;
                };
                let modality = said(&row["modality"]);
                let modality = if modality.is_empty() {
                    "TEXT"
                } else {
                    &modality
                };
                let key = format!("{side}_{}", modality.to_lowercase());
                let total = self.tokens.entry(key).or_default();
                *total = total.saturating_add(count);
                let price = match (side, modality) {
                    ("prompt", "AUDIO") => 3.,
                    ("prompt", _) => 0.75,
                    (_, "AUDIO") => 12.,
                    _ => 4.5,
                };
                cost += count as f64 * price / 1e6;
            }
        }
        if let Some(count) = usage["thoughtsTokenCount"].as_i64() {
            let total = self.tokens.entry("thoughts".into()).or_default();
            *total = total.saturating_add(count);
            cost += count as f64 * 4.5 / 1e6;
        }
        self.estimate += cost;
    }
    pub fn message(
        &mut self,
        message: &Value,
        now: f64,
        mut id: impl FnMut() -> String,
    ) -> Vec<Effect> {
        let mut effects = vec![];
        if message.get("setupComplete").is_some() && !self.began {
            self.began = true;
            effects.push(Effect::Event(json!({"type":"session.started","session":{"model":self.model,"voice":self.voice,"provider":"gemini"}})));
        }
        let update = &message["sessionResumptionUpdate"];
        if truthy(&update["resumable"]) && truthy(&update["newHandle"]) {
            self.handle = update["newHandle"].clone();
        }
        if let Some(go) = message.get("goAway") {
            let left = go["timeLeft"].as_str().and_then(seconds).unwrap_or(5.);
            self.deadline = Some(now + (left - 2.).max(0.));
        }
        let usage = &message["usageMetadata"];
        if usage.is_object() {
            self.count(&message["usageMetadata"]);
            effects.push(Effect::Event(
                json!({"type":"session.usage.updated","usage":self.usage(now)}),
            ));
        }
        let content = &message["serverContent"];
        if truthy(&content["interrupted"]) {
            self.speaking = false;
            self.muted = false;
            effects.push(Effect::Event(
                json!({"type":"session.output_audio.interrupted"}),
            ));
        }
        for part in list(&content["modelTurn"]["parts"]) {
            if let Some(data) = part["inlineData"]["data"]
                .as_str()
                .filter(|data| !data.is_empty())
            {
                self.speaking = true;
                if !self.muted {
                    effects.push(Effect::Event(
                        json!({"type":"session.output_audio.delta","delta":data}),
                    ));
                }
            }
        }
        for (field, kind) in [
            ("inputTranscription", "session.input_transcript.delta"),
            ("outputTranscription", "session.output_transcript.delta"),
        ] {
            if let Some(text) = content[field]["text"]
                .as_str()
                .filter(|text| !text.is_empty())
            {
                let ms = ((now - self.opened) * 1000.) as i64;
                effects.push(Effect::Event(
                    json!({"type":kind,"delta":text,"event_id":id(),"start_ms":ms,"end_ms":ms}),
                ));
            }
        }
        if truthy(&content["turnComplete"]) {
            self.speaking = false;
            self.muted = false;
        }
        for call in list(&message["toolCall"]["functionCalls"]) {
            let Some(id) = call["id"].as_str().filter(|s| !s.is_empty()) else {
                continue;
            };
            if call["name"] != DELEGATE {
                effects.push(response(
                    id,
                    &call["name"],
                    json!({"error":"Unknown function; use delegate_to_agent."}),
                ));
            } else {
                self.calls.insert(id.into(), Request::default());
                effects.push(Effect::Event(json!({"type":"session.delegation.created","delegation":{"id":id,"request":said(&call["args"]["request"])}})));
            }
        }
        for id in list(&message["toolCallCancellation"]["ids"])
            .iter()
            .filter_map(Value::as_str)
        {
            if let Some(call) = self.calls.get_mut(id) {
                call.cancelled = true;
            }
        }
        effects
    }
    pub fn due(&self, now: f64) -> bool {
        self.deadline.is_some_and(|at| !self.speaking || now >= at)
    }
    pub fn handle(&self) -> Option<&Value> {
        (!self.closed && self.setup.is_some() && truthy(&self.handle)).then_some(&self.handle)
    }
    pub fn may_drop_audio(&self) -> bool {
        truthy(&self.handle)
    }
    pub fn resume_setup(&mut self) -> Option<Value> {
        self.deadline = None;
        let handle = self.handle()?;
        let mut setup = self.setup.clone()?;
        setup["sessionResumption"] = json!({"handle":handle});
        Some(json!({"setup":setup}))
    }
    pub fn resumed(&mut self, reason: &str) -> Value {
        self.reconnects += 1;
        self.speaking = false;
        self.muted = false;
        json!({"type":"oracle_v2.provider_observation","source":{"type":"gemini.resumed","reason":reason,"reconnects":self.reconnects}})
    }
}
fn response(id: &str, name: &Value, value: Value) -> Effect {
    Effect::Send(
        json!({"toolResponse":{"functionResponses":[{"id":id,"name":name,"response":value}]}}),
    )
}
fn seconds(value: &str) -> Option<f64> {
    let text = value.trim().strip_suffix('s')?;
    let digits = |s: &str| !s.is_empty() && s.bytes().all(|b| b.is_ascii_digit());
    if !text
        .split_once('.')
        .map_or_else(|| digits(text), |(a, b)| digits(a) && digits(b))
    {
        return None;
    }
    text.parse().ok()
}
