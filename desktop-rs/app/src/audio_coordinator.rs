//! One voice player per user, Host and account (C++ `AudioCoordinator`).
//! Every window keeps its own session-bus connection and tries to own
//! `com.maxteabag.Clarp.Audio.h<digest>`; the owner plays, writes the shared
//! journal and serves `offer`/`control`/`snapshot` at the same path and
//! interface as the C++ client, so Rust and C++ windows share one player.
//! Other windows offer their clips to it and poll its state. Without a
//! session bus it fails closed: never a second, uncoordinated player.

use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use clarp_core::audio_journal::{self, BUS_INTERFACE, BUS_PATH, Journal, State, TICK_MS, clip_key};
use clarp_core::json::Object;
use serde_json::Value;

#[derive(Debug, Clone)]
pub enum Event {
    /// A clip this window, as owner, should play.
    ClipReady(Object),
    /// stop, pause, resume or toggle, for the owner's player.
    Command(String),
    State(State),
    Ownership(bool),
    Error(String),
}

pub type Sink = Arc<dyn Fn(Event) + Send + Sync>;

#[derive(Default)]
struct Shared {
    service: String,
    journal_path: Option<PathBuf>,
    journal: Journal,
    owner: bool,
    outbox: Vec<(String, Object)>,
    state: State,
    initially_muted: bool,
    sending: bool,
    polling: bool,
    generation: u64,
    connection: Option<zbus::Connection>,
}

#[derive(Clone)]
pub struct Coordinator {
    shared: Arc<Mutex<Shared>>,
    sink: Sink,
}

fn lock(shared: &Mutex<Shared>) -> std::sync::MutexGuard<'_, Shared> {
    shared.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
}

fn data_dir() -> Option<PathBuf> {
    let base = std::env::var_os("XDG_DATA_HOME")
        .filter(|v| !v.is_empty())
        .map(PathBuf::from)
        .or_else(|| std::env::var_os("HOME").map(|home| PathBuf::from(home).join(".local").join("share")))?;
    Some(base.join("MaxTeaBag").join("ClarpRust").join("audio-coordination"))
}

/// Written atomically and readable only by the user.
fn save(shared: &mut Shared, journal: Journal) -> Result<(), String> {
    use std::os::unix::fs::PermissionsExt;
    let path = shared.journal_path.clone().ok_or("Cannot persist shared audio state")?;
    let temporary = path.with_extension("json.tmp");
    let written = std::fs::write(&temporary, journal.to_json())
        .and_then(|()| std::fs::set_permissions(&temporary, std::fs::Permissions::from_mode(0o600)))
        .and_then(|()| std::fs::rename(&temporary, &path));
    if let Err(error) = written {
        eprintln!("AudioCoordinator: could not write {}: {error}", path.display());
        return Err("Cannot persist shared audio state".into());
    }
    shared.journal = journal;
    Ok(())
}

/// The owner's object on the bus.
struct AudioBus {
    shared: Arc<Mutex<Shared>>,
    sink: Sink,
}

fn offer_locally(shared: &Arc<Mutex<Shared>>, sink: &Sink, events: &[Object]) -> bool {
    let added = {
        let mut state = lock(shared);
        if !state.owner {
            return false;
        }
        let mut journal = state.journal.clone();
        let added = journal.offer(events);
        if added.is_empty() {
            return true;
        }
        if let Err(message) = save(&mut state, journal) {
            drop(state);
            sink(Event::Error(message));
            return false;
        }
        added
    };
    for event in added {
        sink(Event::ClipReady(event));
    }
    true
}

fn control_locally(shared: &Arc<Mutex<Shared>>, sink: &Sink, action: &str, muted: bool) {
    let announce = {
        let mut state = lock(shared);
        if !state.owner {
            return;
        }
        match action {
            "mute" => {
                let mut journal = state.journal.clone();
                journal.set_muted(muted);
                if let Err(message) = save(&mut state, journal) {
                    drop(state);
                    sink(Event::Error(message));
                    return;
                }
            }
            "pause" | "resume" | "toggle" | "stop" => sink(Event::Command(action.to_owned())),
            _ => return,
        }
        snapshot_of(&state)
    };
    sink(Event::State(announce));
}

fn snapshot_of(state: &Shared) -> State {
    State { muted: state.journal.muted(), ..state.state }
}

#[zbus::interface(name = "com.maxteabag.Clarp.Audio")]
impl AudioBus {
    // The C++ client's method names, exactly: zbus would export PascalCase.
    #[zbus(name = "offer")]
    fn offer(&self, events: Vec<u8>) -> bool {
        let Ok(Value::Array(events)) = serde_json::from_slice::<Value>(&events) else { return false };
        let events: Vec<Object> = events.into_iter().filter_map(|e| e.as_object().cloned()).collect();
        offer_locally(&self.shared, &self.sink, &events)
    }

    #[zbus(name = "control")]
    fn control(&self, action: String, muted: bool) {
        control_locally(&self.shared, &self.sink, &action, muted);
    }

    #[zbus(name = "snapshot")]
    fn snapshot(&self) -> Vec<u8> {
        snapshot_of(&lock(&self.shared)).to_json().into_bytes()
    }
}

impl Coordinator {
    pub fn new(sink: Sink) -> Self {
        Self { shared: Arc::default(), sink }
    }

    pub fn configured(&self) -> bool {
        !lock(&self.shared).service.is_empty()
    }

    pub fn owner(&self) -> bool {
        lock(&self.shared).owner
    }

    /// Joins the player election for `scope` (base URL, NUL, token).
    pub fn configure(&self, scope: &str, initially_muted: bool) {
        let service = audio_journal::service_name(scope);
        if lock(&self.shared).service == service {
            return;
        }
        self.stop();
        let Some(directory) = data_dir() else {
            (self.sink)(Event::Error("Cannot create the shared audio journal directory".into()));
            return;
        };
        let created = std::fs::create_dir_all(&directory).and_then(|()| {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&directory, std::fs::Permissions::from_mode(0o700))
        });
        if let Err(error) = created {
            eprintln!("AudioCoordinator: could not create {}: {error}", directory.display());
            (self.sink)(Event::Error("Cannot create the shared audio journal directory".into()));
            return;
        }
        let generation = {
            let mut state = lock(&self.shared);
            state.service = service;
            state.initially_muted = initially_muted;
            state.journal_path = Some(directory.join(audio_journal::journal_file_name(scope)));
            state.generation
        };
        let coordinator = self.clone();
        crate::runtime::handle().spawn(async move {
            let connection = match zbus::connection::Builder::session() {
                Ok(builder) => builder.build().await,
                Err(error) => Err(error),
            };
            let connection = match connection {
                Ok(connection) => connection,
                Err(error) => {
                    eprintln!("AudioCoordinator: no session bus: {error}");
                    // Fail closed: never create an uncoordinated second player.
                    (coordinator.sink)(Event::Error("Shared audio requires a desktop session bus".into()));
                    return;
                }
            };
            {
                let mut state = lock(&coordinator.shared);
                if state.generation != generation {
                    return;
                }
                state.connection = Some(connection);
            }
            loop {
                coordinator.tick(generation).await;
                tokio::time::sleep(Duration::from_millis(TICK_MS)).await;
                if lock(&coordinator.shared).generation != generation {
                    return;
                }
            }
        });
    }

    /// Leaves the election; an owner gives the name up for the next window.
    pub fn stop(&self) {
        let (connection, service, was_owner) = {
            let mut state = lock(&self.shared);
            state.generation += 1;
            let was_owner = std::mem::replace(&mut state.owner, false);
            let service = std::mem::take(&mut state.service);
            state.journal_path = None;
            state.journal = Journal::default();
            state.outbox.clear();
            state.sending = false;
            state.polling = false;
            state.state = State::default();
            (state.connection.take(), service, was_owner)
        };
        if was_owner {
            (self.sink)(Event::Ownership(false));
        }
        if let Some(connection) = connection {
            crate::runtime::handle().spawn(async move {
                if was_owner {
                    if let Err(error) = connection.object_server().remove::<AudioBus, _>(BUS_PATH).await {
                        eprintln!("AudioCoordinator: could not remove the audio object: {error}");
                    }
                    if let Ok(name) = zbus::names::WellKnownName::try_from(service.as_str())
                        && let Err(error) = connection.release_name(name).await
                    {
                        eprintln!("AudioCoordinator: could not release {service}: {error}");
                    }
                }
                drop(connection);
            });
        }
    }

    /// Gives up a half-taken ownership: the object, the name, and why.
    async fn abandon(&self, connection: &zbus::Connection, service: &str, owned: bool, message: Option<String>) {
        if let Err(error) = connection.object_server().remove::<AudioBus, _>(BUS_PATH).await {
            eprintln!("AudioCoordinator: could not remove the audio object: {error}");
        }
        if owned
            && let Ok(name) = zbus::names::WellKnownName::try_from(service)
            && let Err(error) = connection.release_name(name).await
        {
            eprintln!("AudioCoordinator: could not release {service}: {error}");
        }
        if let Some(message) = message {
            (self.sink)(Event::Error(message));
        }
    }

    async fn take_ownership(&self, connection: &zbus::Connection, generation: u64) {
        let (service, path, muted) = {
            let state = lock(&self.shared);
            (state.service.clone(), state.journal_path.clone(), state.initially_muted)
        };
        let Ok(name) = zbus::names::WellKnownName::try_from(service.as_str()) else { return };
        let bus = AudioBus { shared: self.shared.clone(), sink: self.sink.clone() };
        if !matches!(connection.object_server().at(BUS_PATH, bus).await, Ok(true)) {
            return;
        }
        let flags = zbus::fdo::RequestNameFlags::DoNotQueue.into();
        if !matches!(connection.request_name_with_flags(name, flags).await, Ok(zbus::fdo::RequestNameReply::PrimaryOwner)) {
            self.abandon(connection, &service, false, None).await;
            return;
        }
        let saved = match path.as_ref().filter(|p| p.exists()).map(std::fs::read_to_string) {
            Some(Err(error)) => {
                eprintln!("AudioCoordinator: could not read the journal: {error}");
                self.abandon(connection, &service, true, Some("Cannot read shared audio state".into())).await;
                return;
            }
            Some(Ok(text)) => Some(text),
            None => None,
        };
        let journal = match Journal::take_over(saved.as_deref(), muted) {
            Ok(taken) => taken,
            Err(message) => {
                self.abandon(connection, &service, true, Some(message)).await;
                return;
            }
        };
        let (journal, queued) = journal;
        let announce = {
            let mut state = lock(&self.shared);
            if state.generation != generation {
                return;
            }
            match save(&mut state, journal) {
                Ok(()) => {
                    state.owner = true;
                    Ok(snapshot_of(&state))
                }
                Err(message) => Err(message),
            }
        };
        let announce = match announce {
            Ok(announce) => announce,
            Err(message) => {
                self.abandon(connection, &service, true, Some(message)).await;
                return;
            }
        };
        (self.sink)(Event::Ownership(true));
        (self.sink)(Event::State(announce));
        for event in queued {
            (self.sink)(Event::ClipReady(event));
        }
    }

    async fn call(connection: &zbus::Connection, service: &str, method: &str, body: &(impl serde::Serialize + zbus::zvariant::DynamicType)) -> zbus::Result<zbus::Message> {
        let call = connection.call_method(Some(service), BUS_PATH, Some(BUS_INTERFACE), method, body);
        match tokio::time::timeout(Duration::from_millis(1_500), call).await {
            Ok(reply) => reply,
            Err(_) => Err(zbus::Error::InputOutput(std::sync::Arc::new(std::io::Error::other("timed out")))),
        }
    }

    async fn tick(&self, generation: u64) {
        let Some(connection) = lock(&self.shared).connection.clone() else { return };
        if !self.owner() {
            self.take_ownership(&connection, generation).await;
        }
        let (owner, service, outbox, send) = {
            let mut state = lock(&self.shared);
            let send = !state.outbox.is_empty() && !state.sending;
            if send && !state.owner {
                state.sending = true;
            }
            (state.owner, state.service.clone(), state.outbox.clone(), send)
        };
        if send {
            let events: Vec<Object> = outbox.iter().map(|(_, e)| e.clone()).collect();
            let delivered = if owner {
                offer_locally(&self.shared, &self.sink, &events)
            } else {
                let bytes = serde_json::to_vec(&events).unwrap_or_default();
                let reply = Self::call(&connection, &service, "offer", &(bytes,)).await;
                lock(&self.shared).sending = false;
                reply.ok().and_then(|m| m.body().deserialize::<bool>().ok()).unwrap_or(false)
            };
            if delivered {
                let sent: Vec<String> = outbox.iter().map(|(k, _)| k.clone()).collect();
                lock(&self.shared).outbox.retain(|(k, _)| !sent.contains(k));
            }
        }
        {
            let mut state = lock(&self.shared);
            if state.owner || state.polling || state.generation != generation {
                return;
            }
            state.polling = true;
        }
        let reply = Self::call(&connection, &service, "snapshot", &()).await;
        let received = reply.ok().and_then(|m| m.body().deserialize::<Vec<u8>>().ok());
        let owner = {
            let mut state = lock(&self.shared);
            state.polling = false;
            state.owner || state.generation != generation
        };
        if let Some(snapshot) = received.as_deref().and_then(|b| State::from_json(&String::from_utf8_lossy(b)))
            && !owner
        {
            (self.sink)(Event::State(snapshot));
        }
    }

    pub fn submit(&self, event: Object) {
        if !self.configured() {
            return;
        }
        let key = clip_key(&event);
        {
            let mut state = lock(&self.shared);
            match state.outbox.iter_mut().find(|(k, _)| *k == key) {
                Some(slot) => slot.1 = event,
                None => state.outbox.push((key, event)),
            }
        }
        let (coordinator, generation) = (self.clone(), lock(&self.shared).generation);
        crate::runtime::handle().spawn(async move { coordinator.tick(generation).await });
    }

    pub fn command(&self, action: &str, muted: bool) {
        if self.owner() {
            control_locally(&self.shared, &self.sink, action, muted);
            return;
        }
        let (connection, service, generation) = {
            let state = lock(&self.shared);
            (state.connection.clone(), state.service.clone(), state.generation)
        };
        let Some(connection) = connection.filter(|_| !service.is_empty()) else { return };
        let (coordinator, action) = (self.clone(), action.to_owned());
        crate::runtime::handle().spawn(async move {
            let reply = Self::call(&connection, &service, "control", &(action, muted)).await;
            if lock(&coordinator.shared).generation != generation {
                return;
            }
            if let Err(error) = reply {
                eprintln!("AudioCoordinator: control failed: {error}");
                (coordinator.sink)(Event::Error("The audio owner changed; please retry the playback control".into()));
            }
            coordinator.tick(generation).await;
        });
    }

    /// The owner starts a clip only once, ever.
    pub fn begin(&self, event: &Object) -> bool {
        let mut state = lock(&self.shared);
        if !state.owner {
            return false;
        }
        let mut journal = state.journal.clone();
        if !journal.begin(event) {
            return false;
        }
        match save(&mut state, journal) {
            Ok(()) => true,
            Err(message) => {
                drop(state);
                (self.sink)(Event::Error(message));
                false
            }
        }
    }

    pub fn finish(&self, event: &Object) {
        let mut state = lock(&self.shared);
        if !state.owner {
            return;
        }
        let mut journal = state.journal.clone();
        journal.finish(event);
        if let Err(message) = save(&mut state, journal) {
            drop(state);
            (self.sink)(Event::Error(message));
        }
    }

    pub fn publish(&self, playing: bool, paused: bool, available: bool) {
        let mut state = lock(&self.shared);
        if state.owner {
            state.state = State { muted: false, playing, paused, available };
        }
    }
}
