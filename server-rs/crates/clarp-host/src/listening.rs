//! The stream of speech (`GET /stt/stream`, a socket).
//!
//! A phone decides by itself where a turn of speech ends and posts the clip, unless the
//! engine that hears is one that finds the ends of turns itself. Then the phone streams
//! what it hears here, sixteen thousand measures a second, and the Host carries the sound
//! to the engine (Deepgram Flux) and tells the phone what the engine says of the turns:
//! that one started, what is understood so far, that it may be over, that it went on after
//! all, that it ended. The engine's key never leaves the Host: the phone talks to its Host
//! only.
//!
//! The Python Host's `stt_stream.py`. What is pure in it is the kernel's
//! (`clarp_kernel::speech_stream`): the engine's address by how a phone tunes the
//! listening, what a message of the engine means, and the books of one stream. Here is
//! the carrying: the engine is reached before the phone's socket is opened, so that a
//! phone is told in words when it cannot be; then sound goes up and events come down
//! until either side is done. A phone that is done says so, and the engine is given a
//! moment to say its last turn.

use std::collections::HashMap;
use std::future::pending;
use std::time::Duration;

use axum::Router;
use axum::extract::{Query, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use clarp_kernel::heard::{PROVIDERS, provider_of};
use clarp_kernel::python;
use clarp_kernel::speech_stream::{self as rules, Keeps, Ledger, Run, Tuning};
use futures_util::{SinkExt, StreamExt};
use serde_json::{Value, json};
use tokio::time::Instant;
use tokio_tungstenite::tungstenite::Message;
use tokio_tungstenite::tungstenite::client::IntoClientRequest;

use crate::edge::{ApiError, App};
use crate::line::{ENDED_WELL, Heard, Line, Socket, Told};

/// The engine's end of a stream.
type Engine =
    tokio_tungstenite::WebSocketStream<tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>>;

/// What the engine is told when the phone is done: to say what it still has.
const SAY_THE_REST: &str = r#"{"type": "CloseStream"}"#;
/// How long the engine is given to take its own socket's end.
const ENDS_WITHIN: Duration = Duration::from_secs(2);

/// Records completed turns and retains their WAV only while the setting is enabled.
struct Named {
    vocab: crate::vocabulary::Desk,
    session: String,
    model: String,
    fleet: crate::fleet::FleetHandle,
    root: std::path::PathBuf,
    clock: std::sync::Arc<dyn clarp_ports::clock::Clock + Send + Sync>,
}

impl Keeps for Named {
    fn new_trace(&mut self) -> String {
        crate::ids::hex(8)
    }

    fn record(&mut self, run: &Run<'_>) -> Option<i64> {
        tokio::runtime::Handle::current().block_on(crate::vocabulary::stream_run(
            &self.vocab,
            &self.fleet,
            run,
            self.clock.now_ms(),
        ))
    }

    fn retain(&mut self, trace_id: &str, wav: Vec<u8>) {
        if crate::vocabulary::audio::enabled(&self.fleet.directory()) {
            let _ = crate::vocabulary::audio::retain(
                &self.root,
                crate::vocabulary::audio::Clip {
                    trace: trace_id,
                    data: &wav,
                    kind: "audio/wav",
                    session: &self.session,
                    run: 0,
                    model: &self.model,
                },
                self.clock.now_ms(),
            );
        }
    }
}

/// Reaches the engine at its address, with the owner's key. `Err`: why it could not be
/// reached, in words that have nothing of the key in them.
async fn dial(app: &App, address: &str, key: &str) -> Result<Engine, String> {
    // A stand-in for the engine is on this machine, and stands where the engine listens.
    let address = match app.config.flux_endpoint.as_str() {
        "" => address.to_string(),
        stand_in => format!("{stand_in}/{}", &address[rules::FLUX_URL.len()..]),
    };
    let no_address = |_| "the engine's address cannot be asked".to_string();
    let mut request = address.into_client_request().map_err(no_address)?;
    let token = format!("Token {key}");
    let token = token.parse().map_err(|_| "its key is no key".to_string())?;
    request.headers_mut().insert("Authorization", token);
    let trust = crate::connector::trust_of_the_web();
    let dial = tokio_tungstenite::connect_async_tls_with_config(request, None, true, trust);
    let within = Duration::from_secs_f64(rules::OPENS_WITHIN_S);
    match tokio::time::timeout(within, dial).await {
        Ok(Ok((engine, _))) => Ok(engine),
        Ok(Err(refused)) => Err(why(&refused)),
        Err(_) => Err("it did not answer in time".to_string()),
    }
}

/// Why the engine could not be reached, without anything it was sent.
fn why(refused: &tokio_tungstenite::tungstenite::Error) -> String {
    use tokio_tungstenite::tungstenite::Error;
    match refused {
        Error::Http(answer) => format!("it answered {}", answer.status().as_u16()),
        Error::Io(failed) => format!("{}", failed.kind()),
        _ => "the socket could not be opened".to_string(),
    }
}

/// What a phone is told on a stream: a value as Python writes one.
fn said(value: &Value) -> Told {
    Told::Text(python::dumps_ascii(value))
}

/// Carries one stream: sound up, what the engine says of turns down, until either side is
/// done.
async fn carried(
    mut line: Line,
    engine: Engine,
    mut ledger: Ledger,
    names: (String, String),
    mut keeps: Named,
) {
    let (engine_name, stream_trace) = names;
    let (mut up, mut down) = engine.split();
    let began = Instant::now();
    let ready = rules::ready(&engine_name, ledger.keyterms.len(), &stream_trace);
    line.tell(said(&ready)).await;
    // Once the phone has said that it is done, it is heard no more, and the engine is
    // given until this moment for its last turn.
    let mut done_by: Option<Instant> = None;
    loop {
        let last_moment = async {
            match done_by {
                Some(by) => tokio::time::sleep_until(by).await,
                None => pending().await,
            }
        };
        tokio::select! {
            from_engine = down.next() => {
                let text = match from_engine {
                    Some(Ok(Message::Text(text))) => text,
                    // The engine is done, or gone.
                    Some(Ok(Message::Close(_)) | Err(_)) | None => break,
                    // Bytes, and its asking whether the Host is there.
                    Some(Ok(_)) => continue,
                };
                let Ok(message) = serde_json::from_str::<Value>(text.as_str()) else {
                    continue;
                };
                if let Some(failed) = rules::failed(&message) {
                    line.tell(said(&failed)).await;
                    continue;
                }
                let Some(event) = rules::meant(&message) else {
                    continue;
                };
                let at=began.elapsed().as_secs_f64();
                let told=if event.kind=="end" {
                    let Ok((next,book,told))=tokio::task::spawn_blocking(move||{let told=ledger.apply(&event,at,&mut keeps);(ledger,keeps,told)}).await else {return};
                    ledger=next;keeps=book;told
                }else{ledger.apply(&event,at,&mut keeps)};
                if !line.tell(said(&told)).await {
                    break;
                }
            }
            heard = line.heard(), if done_by.is_none() => match heard {
                Some(Heard::Bytes(sound)) => {
                    ledger.heard(&sound, began.elapsed().as_secs_f64(), &mut keeps);
                    if up.send(Message::Binary(sound.into())).await.is_err() {
                        break;
                    }
                }
                Some(Heard::Text(text)) => {
                    // The one thing a phone says in words: that it is done talking.
                    let message: Value = serde_json::from_str(&text).unwrap_or(Value::Null);
                    if message.get("type") == Some(&json!("close")) {
                        let _ = up.send(Message::Text(SAY_THE_REST.into())).await;
                        let grace = Duration::from_secs_f64(rules::FLUSH_GRACE_S);
                        done_by = Some(Instant::now() + grace);
                    }
                }
                // The phone is gone.
                None => break,
            },
            () = last_moment => break,
        }
    }
    let _ = tokio::time::timeout(ENDS_WITHIN, up.close()).await;
    line.tell(said(&rules::closed(ledger.turns_ended))).await;
    line.tell(Told::Close(ENDED_WELL, String::new())).await;
}

/// `GET /stt/stream`: a phone's stream of speech to the engine that finds the ends of
/// turns. Asked with which agent is spoken to (`session`), which engine hears (`model`:
/// else the one chosen for this Host) and how its listening is tuned.
async fn stream(
    State(app): State<App>,
    Query(asked): Query<HashMap<String, String>>,
    socket: Socket,
) -> Response {
    let refused = |status: StatusCode, why: String| ApiError::new(status, why).into_response();
    if !socket.asked() {
        let none = "upgrade required (websocket)".to_string();
        return refused(StatusCode::UPGRADE_REQUIRED, none);
    }
    let of = |name: &str| asked.get(name).map(String::as_str);
    let session = python::strip(of("session").unwrap_or(""));
    let chosen = app.ears.default_engine(&app.fleet.directory());
    let engine = of("model").filter(|model| !model.is_empty());
    let engine = python::strip(engine.unwrap_or(&chosen));
    let provider = provider_of(engine);
    let listed = PROVIDERS.iter().find(|listed| listed.id == provider);
    let Some(listed) = listed.filter(|listed| listed.turn_detection == "own") else {
        let engine = if engine.is_empty() { "local" } else { engine };
        let none = format!("engine does not detect turns on a stream: {engine}");
        return refused(StatusCode::CONFLICT, none);
    };
    let Some(key) = app.ears.key(&provider) else {
        let none = format!("{provider} API key is not configured");
        return refused(StatusCode::SERVICE_UNAVAILABLE, none);
    };
    let model = listed.turn_detection_model;
    let trace = crate::ids::hex(8);
    let (audit, _) = crate::vocabulary::compile(
        &app,
        session,
        &format!("{provider}:nova-3"),
        false,
        Some(&trace),
    )
    .await
    .unwrap_or_default();
    let keyterms = clarp_speech::listen::terms(audit["payload"].as_str().unwrap_or(""));
    let Some(address) = rules::flux_url(model, &Tuning::of(of), &keyterms) else {
        let none = "eot_timeout_ms is no number of milliseconds".to_string();
        return refused(StatusCode::BAD_REQUEST, none);
    };
    // The engine is reached before the phone's socket is opened: where it cannot be, the
    // phone is told so in words.
    let engine = match dial(&app, &address, &key).await {
        Ok(engine) => engine,
        Err(why) => {
            let none = format!("{provider} stream connect failed: {why}");
            return refused(StatusCode::BAD_GATEWAY, none);
        }
    };
    let capacity = listed.models.first().map_or(0, |model| model.capacity);
    let ledger = Ledger::new(session, &provider, model, keyterms, capacity);
    let names = (format!("{provider}:{model}"), trace);
    let keeps = Named {
        vocab: app.ears.vocab.clone(),
        session: session.into(),
        model: format!("{provider}:{model}"),
        fleet: app.fleet.clone(),
        root: app.config.data_dir.clone(),
        clock: app.clock.clone(),
    };
    socket.open(move |line| carried(line, engine, ledger, names, keeps))
}

pub fn routes() -> Router<App> {
    Router::new().route("/stt/stream", get(stream))
}
