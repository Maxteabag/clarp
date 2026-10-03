//! Port of the C++ client's `SseClient`: one `/events` stream per Host,
//! resumed with `Last-Event-ID`, aborted after a silent watchdog interval and
//! reopened with capped exponential backoff.

use std::sync::{Arc, Mutex};

use clarp_core::endpoint::{SseCursor, SseTiming, normalize_base};
use clarp_core::json::Object;
use clarp_core::sse::SseParser;
use reqwest::header::{CACHE_CONTROL, HeaderValue};
use tokio::runtime::Handle;
use tokio::task::JoinHandle;
use url::Url;

use crate::USER_AGENT;
use crate::api::auth_headers;

#[derive(Debug, Clone, PartialEq)]
pub enum SseSignal {
    /// Emitted only when the connected state actually changes.
    Connected(bool),
    Event(Object),
    Error(String),
}

#[derive(Default)]
struct State {
    base: Option<Url>,
    token: String,
    /// Extra query on `/events` (`live=…`), none when empty.
    query: Vec<(String, String)>,
    cursor: SseCursor,
    connected: bool,
    /// Bumped by stop() and start(); a stream task from an older run may
    /// finish its current await but never emits again.
    run: u64,
}

type Sink = Arc<dyn Fn(SseSignal) + Send + Sync>;

pub struct SseClient {
    state: Arc<Mutex<State>>,
    http: reqwest::Client,
    runtime: Handle,
    timing: SseTiming,
    sink: Sink,
    task: Option<JoinHandle<()>>,
}

impl SseClient {
    pub fn new(runtime: Handle, timing: SseTiming, sink: impl Fn(SseSignal) + Send + Sync + 'static) -> Self {
        let http = reqwest::Client::builder()
            .user_agent(USER_AGENT)
            .build()
            .expect("HTTP client builds");
        Self { state: Arc::default(), http, runtime, timing, sink: Arc::new(sink), task: None }
    }

    pub fn set_endpoint(&self, base: Url, token: &str) {
        let base = normalize_base(base);
        let mut state = self.state.lock().expect("sse state");
        state.cursor.set_endpoint(&base);
        state.base = Some(base);
        state.token = token.to_owned();
    }

    /// The query `/events` is opened with from the next connection on
    /// (`[("live", "rachel,mike")]`); empty for none.
    pub fn set_query(&self, query: Vec<(String, String)>) {
        self.state.lock().expect("sse state").query = query;
    }

    pub fn query(&self) -> Vec<(String, String)> {
        self.state.lock().expect("sse state").query.clone()
    }

    /// Reopens a running stream with the current query, resuming from the
    /// last event id. Unlike stop and start it reports no disconnect: the
    /// stream is only changing what it asks for.
    pub fn resubscribe(&mut self) {
        let Some(task) = self.task.take() else { return };
        task.abort();
        let run = {
            let mut state = self.state.lock().expect("sse state");
            state.run += 1;
            state.run
        };
        let (state, http, timing, sink) = (self.state.clone(), self.http.clone(), self.timing, self.sink.clone());
        self.task = Some(self.runtime.spawn(stream_loop(state, http, timing, sink, run)));
    }

    pub fn connected(&self) -> bool {
        self.state.lock().expect("sse state").connected
    }

    /// Whether the stream is meant to be running: started and not stopped.
    /// `Connected(false)` reaches the sink after `stop()` returns, so callers
    /// use this to tell a deliberate stop from a dropped stream.
    pub fn running(&self) -> bool {
        self.task.is_some()
    }

    pub fn last_event_id(&self) -> String {
        self.state.lock().expect("sse state").cursor.last_event_id().to_owned()
    }

    pub fn set_last_event_id(&self, id: &str) {
        self.state.lock().expect("sse state").cursor.set_last_event_id(id);
    }

    pub fn start(&mut self) {
        self.stop();
        let run = {
            let mut state = self.state.lock().expect("sse state");
            state.run += 1;
            state.run
        };
        let (state, http, timing, sink) = (self.state.clone(), self.http.clone(), self.timing, self.sink.clone());
        self.task = Some(self.runtime.spawn(stream_loop(state, http, timing, sink, run)));
    }

    /// Stop streaming. Emits `Connected(false)` if it was connected; no event
    /// from the stopped stream is delivered afterwards.
    pub fn stop(&mut self) {
        if let Some(task) = self.task.take() {
            task.abort();
        }
        let was_connected = {
            let mut state = self.state.lock().expect("sse state");
            state.run += 1;
            std::mem::replace(&mut state.connected, false)
        };
        if was_connected {
            (self.sink)(SseSignal::Connected(false));
        }
    }
}

impl Drop for SseClient {
    fn drop(&mut self) {
        if let Some(task) = self.task.take() {
            task.abort();
        }
    }
}

/// Emit while holding the state lock so stop() cannot interleave between the
/// run check and the delivery.
fn emit(state: &Mutex<State>, sink: &Sink, run: u64, signal: impl FnOnce(&mut State) -> Option<SseSignal>) -> bool {
    let mut guard = state.lock().expect("sse state");
    if guard.run != run {
        return false;
    }
    if let Some(signal) = signal(&mut guard) {
        sink(signal);
    }
    true
}

fn set_connected(state: &Mutex<State>, sink: &Sink, run: u64, connected: bool) -> bool {
    emit(state, sink, run, |s| (std::mem::replace(&mut s.connected, connected) != connected).then_some(SseSignal::Connected(connected)))
}

async fn stream_loop(state: Arc<Mutex<State>>, http: reqwest::Client, timing: SseTiming, sink: Sink, run: u64) {
    let mut attempt = 0u32;
    loop {
        let request = {
            let guard = state.lock().expect("sse state");
            if guard.run != run {
                return;
            }
            let Some(mut url) = guard.base.as_ref().and_then(|base| base.join("events").ok()) else {
                return;
            };
            if !guard.query.is_empty() {
                url.query_pairs_mut().extend_pairs(guard.query.iter());
            }
            let mut headers = auth_headers(&guard.token, "text/event-stream");
            headers.insert(CACHE_CONTROL, HeaderValue::from_static("no-cache"));
            let last = guard.cursor.last_event_id();
            if !last.is_empty()
                && let Ok(value) = HeaderValue::from_str(last) {
                    headers.insert("Last-Event-ID", value);
                }
            http.get(url).headers(headers)
        };
        let failure = match tokio::time::timeout(timing.watchdog, request.send()).await {
            Err(_) => Some("Event stream timed out".to_owned()),
            Ok(Err(error)) => Some(error.to_string()),
            Ok(Ok(response)) if !response.status().is_success() => {
                Some(format!("Event stream failed: HTTP {}", response.status().as_u16()))
            }
            Ok(Ok(mut response)) => {
                if !set_connected(&state, &sink, run, true) {
                    return;
                }
                attempt = 0;
                let mut parser = SseParser::default();
                loop {
                    match tokio::time::timeout(timing.watchdog, response.chunk()).await {
                        Err(_) => break Some("Event stream went silent".to_owned()),
                        Ok(Err(error)) => break Some(error.to_string()),
                        Ok(Ok(None)) => break Some("Event stream closed".to_owned()),
                        Ok(Ok(Some(bytes))) => {
                            for message in parser.feed(&bytes) {
                                let alive = emit(&state, &sink, run, |s| Some(SseSignal::Event(s.cursor.accept(message))));
                                if !alive {
                                    return;
                                }
                            }
                        }
                    }
                }
            }
        };
        if !set_connected(&state, &sink, run, false) {
            return;
        }
        if let Some(message) = failure
            && !emit(&state, &sink, run, |_| Some(SseSignal::Error(message))) {
                return;
            }
        tokio::time::sleep(timing.reconnect_delay(attempt)).await;
        attempt += 1;
    }
}
