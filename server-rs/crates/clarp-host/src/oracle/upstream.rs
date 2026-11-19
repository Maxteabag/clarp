//! The two voice transports. Resumption is driven outside the call select so another
//! socket becoming readable cannot cancel a replacement handshake.
use super::super::calls::{Voice, dial};
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
    second: Option<Box<Second>>,
    pub events: VecDeque<Value>,
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
    pub async fn open(app: &App, key: &str) -> Option<Self> {
        let choice = super::voice_told(app, &app.fleet.directory());
        if choice["provider"] == "gemini"
            && app.oracle.calls.healthy()
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
                    second: Some(Box::new(Second {
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
            second: None,
            events: VecDeque::new(),
        })
    }
    pub fn machine(&mut self) -> Option<&mut Gemini> {
        self.second.as_mut().map(|s| &mut s.machine)
    }
    pub fn used(&self) -> Option<Value> {
        self.second.as_ref().map(|s| s.machine.usage(s.now()))
    }
    pub fn due(&self) -> bool {
        self.second.as_ref().is_some_and(|s| s.machine.due(s.now()))
    }
    pub async fn resume(&mut self, reason: &str) -> Result<bool, ()> {
        let Some(s) = &mut self.second else {
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
        let Some(s) = &mut self.second else {
            self.events.push_back(event);
            return Ok(());
        };
        let began = s.machine.began;
        let effects = s.machine.message(&event, s.now(), || ids::hex(16));
        if !began && s.machine.began {
            app.oracle.calls.health(true);
        }
        self.apply(effects).await
    }
    pub async fn send(&mut self, event: Value) -> Result<(), ()> {
        let Some(s) = &mut self.second else {
            return self
                .socket
                .send(Message::text(clarp_kernel::python::dumps_ascii(&event)))
                .await
                .map_err(|_| ());
        };
        let effects = s.machine.send(&event, s.now());
        self.apply(effects).await
    }
    pub async fn hook(&mut self, hook: Hook) -> Result<(), ()> {
        let Some(s) = &mut self.second else {
            return Ok(());
        };
        match hook {
            Hook::LocalInterrupt => s.machine.interrupt(),
            Hook::DelegationRouting(id) => s.machine.routing(&id),
            Hook::DelegationFinished(id) => {
                let effects = s.machine.finished(&id);
                self.apply(effects).await?;
            }
        }
        Ok(())
    }
    async fn apply(&mut self, effects: Vec<Effect>) -> Result<(), ()> {
        for effect in effects {
            let audio = matches!(effect, Effect::Audio(_));
            match effect {
                Effect::Event(event) => self.events.push_back(event),
                Effect::Send(value) | Effect::Audio(value) => {
                    let Some(s) = &self.second else {
                        return Err(());
                    };
                    if s.machine.closed {
                        continue;
                    }
                    if self
                        .socket
                        .send(Message::text(value.to_string()))
                        .await
                        .is_err()
                        && !(audio && s.machine.may_drop_audio())
                    {
                        return Err(());
                    }
                }
            }
        }
        Ok(())
    }
}
