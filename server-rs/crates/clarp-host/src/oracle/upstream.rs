//! The Oracle's voice transports. Resumption is driven outside the call select so another
//! socket becoming readable cannot cancel a replacement handshake.
use super::super::calls::{Voice, dial};
use super::elevenlabs;
use crate::edge::App;
use crate::ids;
use clarp_kernel::oracle::conversation::Hook;
use clarp_kernel::oracle::gemini::{self, Effect, Gemini};
use futures_util::{SinkExt, StreamExt};
use serde_json::Value;
use std::collections::VecDeque;
use std::time::{Duration, Instant};
use tokio_tungstenite::tungstenite::Message;

const ADDRESS: &str = "wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent";
pub struct Upstream {
    pub socket: Voice,
    protocol: Protocol,
    pub events: VecDeque<Value>,
}
enum Protocol {
    GptLive,
    Gemini(Box<Second>),
    ElevenLabs(Box<elevenlabs::Session>),
}
struct Second {
    machine: Gemini,
    address: String,
    opened: Instant,
}
impl Second {
    fn now(&self) -> f64 {
        self.opened.elapsed().as_secs_f64()
    }
}
async fn connect(address: &str) -> Option<Voice> {
    let trust = crate::connector::trust_of_the_web();
    let dial = tokio_tungstenite::connect_async_tls_with_config(address, None, true, trust);
    tokio::time::timeout(Duration::from_secs(20), dial)
        .await
        .ok()?
        .ok()
        .map(|v| v.0)
}
impl Upstream {
    pub async fn open(
        app: &App,
        key: &str,
        direct: bool,
        norwegian_transcription: bool,
    ) -> Option<Self> {
        let choice = super::voice_told(app, &app.fleet.directory());
        if choice["provider"] == "elevenlabs" {
            let secret = app.ears.key("elevenlabs")?;
            let model = choice["elevenlabs"]["model"].as_str()?;
            let voice = choice["elevenlabs"]["voice"].as_str()?;
            let socket = elevenlabs::connect(
                &app.config.eleven_realtime_endpoint,
                secret,
                model,
                voice,
                direct,
                norwegian_transcription,
            )
            .await?;
            return Some(Self {
                socket,
                protocol: Protocol::ElevenLabs(Box::new(elevenlabs::Session::new(
                    model.into(),
                    voice.into(),
                    direct,
                ))),
                events: VecDeque::new(),
            });
        }
        if choice["provider"] == "gemini"
            && app
                .oracle
                .calls
                .healthy(Duration::from_millis(app.config.second_voice_hold_ms))
            && let Some(secret) = app.ears.key("gemini")
        {
            let endpoint = &app.config.gemini_live_endpoint;
            let endpoint = if endpoint.is_empty() {
                ADDRESS
            } else {
                endpoint
            };
            let encoded: String = secret.bytes().map(|b| format!("%{b:02X}")).collect();
            let address = format!("{endpoint}?key={encoded}");
            if let Some(socket) = connect(&address).await {
                let model = choice["gemini"]["model"].as_str()?.into();
                let voice = choice["gemini"]["voice"].as_str()?.into();
                return Some(Self {
                    socket,
                    protocol: Protocol::Gemini(Box::new(Second {
                        machine: Gemini::new(model, voice, 0.),
                        opened: Instant::now(),
                        address,
                    })),
                    events: VecDeque::new(),
                });
            }
            app.oracle.calls.health(false);
        }
        Some(Self {
            socket: dial(app, key).await?,
            protocol: Protocol::GptLive,
            events: VecDeque::new(),
        })
    }
    pub fn machine(&mut self) -> Option<&mut Gemini> {
        match &mut self.protocol {
            Protocol::Gemini(second) => Some(&mut second.machine),
            _ => None,
        }
    }
    pub fn eleven_model(&self) -> Option<&str> {
        match &self.protocol {
            Protocol::ElevenLabs(eleven) => Some(eleven.model.as_str()),
            _ => None,
        }
    }
    pub fn adapted(&self) -> bool {
        !matches!(&self.protocol, Protocol::GptLive)
    }
    pub fn used(&self) -> Option<Value> {
        match &self.protocol {
            Protocol::Gemini(second) => Some(second.machine.usage(second.now())),
            _ => None,
        }
    }
    pub fn due(&self) -> bool {
        matches!(&self.protocol, Protocol::Gemini(second) if second.machine.due(second.now()))
    }
    pub async fn resume(&mut self, reason: &str) -> Result<bool, ()> {
        let Protocol::Gemini(s) = &mut self.protocol else {
            return Ok(false);
        };
        let Some(setup) = s.machine.resume_setup() else {
            return Ok(false);
        };
        let _ = self.socket.close(None).await;
        let mut socket = connect(&s.address).await.ok_or(())?;
        let ready = async {
            socket.send(Message::text(setup.to_string())).await.ok()?;
            while let Some(Ok(message)) = socket.next().await {
                match message {
                    Message::Text(raw)
                        if serde_json::from_str::<Value>(&raw)
                            .ok()?
                            .get("setupComplete")
                            .is_some() =>
                    {
                        return Some(());
                    }
                    Message::Close(_) => break,
                    _ => {}
                }
            }
            None
        };
        tokio::time::timeout(Duration::from_secs(gemini::SETUP_SECONDS), ready)
            .await
            .ok()
            .flatten()
            .ok_or(())?;
        self.socket = socket;
        self.events.push_back(s.machine.resumed(reason));
        Ok(true)
    }
    pub async fn receive(&mut self, app: &App, event: Value) -> Result<(), ()> {
        let effects = match &mut self.protocol {
            Protocol::GptLive => {
                self.events.push_back(event);
                return Ok(());
            }
            Protocol::Gemini(s) => {
                let began = s.machine.began;
                let effects = s.machine.message(&event, s.now(), || ids::hex(16));
                if !began && s.machine.began {
                    app.oracle.calls.health(true);
                }
                effects
            }
            Protocol::ElevenLabs(eleven) => eleven.receive(&event),
        };
        self.apply(effects).await
    }
    pub async fn send(&mut self, event: Value) -> Result<(), ()> {
        let effects = match &mut self.protocol {
            Protocol::GptLive => {
                return self
                    .socket
                    .send(Message::text(clarp_kernel::python::dumps_ascii(&event)))
                    .await
                    .map_err(|_| ());
            }
            Protocol::Gemini(s) => s.machine.send(&event, s.now()),
            Protocol::ElevenLabs(eleven) => eleven.send(&event),
        };
        self.apply(effects).await
    }
    pub async fn hook(&mut self, hook: Hook) -> Result<(), ()> {
        let effects = match &mut self.protocol {
            Protocol::GptLive => return Ok(()),
            Protocol::ElevenLabs(eleven) => eleven.hook(hook),
            Protocol::Gemini(s) => match hook {
                Hook::LocalInterrupt => {
                    s.machine.interrupt();
                    return Ok(());
                }
                Hook::DelegationRouting(id) => {
                    s.machine.routing(&id);
                    return Ok(());
                }
                Hook::DelegationFinished(id) => s.machine.finished(&id),
            },
        };
        self.apply(effects).await
    }
    async fn apply(&mut self, effects: Vec<Effect>) -> Result<(), ()> {
        for effect in effects {
            let audio = matches!(effect, Effect::Audio(_));
            match effect {
                Effect::Event(event) => self.events.push_back(event),
                Effect::Send(value) | Effect::Audio(value) => {
                    if matches!(&self.protocol, Protocol::Gemini(s) if s.machine.closed) {
                        continue;
                    }
                    let may_drop_audio = audio
                        && matches!(&self.protocol,
                        Protocol::Gemini(s) if s.machine.may_drop_audio());
                    if self
                        .socket
                        .send(Message::text(value.to_string()))
                        .await
                        .is_err()
                        && !may_drop_audio
                    {
                        return Err(());
                    }
                }
            }
        }
        Ok(())
    }
}
