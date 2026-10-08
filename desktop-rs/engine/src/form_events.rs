//! HTML form telemetry (iOS 2741+, Host feature `html_form_events`): the
//! loopback page's `window.clarpForm.log(event)` lands in a durable journal
//! per form (`clarp_core::form_events`), and the engine sends it to the Host
//! it was logged against with the app's own credential: batches a second
//! after a burst, retries with backoff, and again after a restart. It never
//! starts a turn or submits answers. A form whose Host follows a draft array
//! (`clarp-agent-artifacts form-events ID --draft-key KEY`) has that array's
//! new entries imported the same way.

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::{Arc, Mutex, MutexGuard};

use clarp_core::form_events::{Batch, EventLog, directory, is_permanent, retry_delay};
use clarp_core::json::{self, Object};
use serde_json::Value;

use crate::{Engine, Message};

struct Entry {
    log: EventLog,
    /// The send out now, by its token.
    in_flight: Option<(u64, Batch)>,
    /// The send waiting for its timer, by its token.
    scheduled: Option<u64>,
    failures: u32,
    /// Refused for good on this connection; kept for its recovery.
    stopped: bool,
    draft_key: Option<String>,
    /// The page's latest draft, imported once the Host's policy is known.
    last_draft: Option<Value>,
    /// The card says the event log failed.
    error_shown: bool,
}

/// Every form's journal, shared with the loopback page's threads, which
/// queue events while the engine sends them.
pub struct FormEvents {
    root: Option<PathBuf>,
    logs: HashMap<String, Entry>,
}

pub type FormEventStore = Arc<Mutex<FormEvents>>;

pub(crate) fn store(root: Option<PathBuf>) -> FormEventStore {
    Arc::new(Mutex::new(FormEvents { root, logs: HashMap::new() }))
}

fn lock(store: &FormEventStore) -> MutexGuard<'_, FormEvents> {
    store.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
}

fn now_ms() -> u64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map_or(0, |d| d.as_millis() as u64)
}

/// `CLARP_FORM_EVENTS` (`off` for none), else the user's data dir.
pub(crate) fn default_root() -> Option<PathBuf> {
    match std::env::var("CLARP_FORM_EVENTS") {
        Ok(value) if value == "off" => None,
        Ok(value) if !value.is_empty() => Some(value.into()),
        _ => clarp_core::dirs::data_home().map(|data| data.join("clarp").join("form-events")),
    }
}

impl FormEvents {
    /// The page logged `event`: queued on disk before this returns. The
    /// engine is told separately (`Engine::form_events_changed`).
    pub fn log(&mut self, key: &str, event: &Value, event_id: &str, origin: &str) -> Result<(), String> {
        let entry = self.logs.get_mut(key).ok_or("Reopen the form to resume event logging.")?;
        entry.log.enqueue(event, event_id, origin, now_ms()).map(|_| ())
    }

    /// The page saved a draft: kept for the Host's policy, and its followed
    /// array's new entries queued when there is one. How many were.
    pub fn draft(&mut self, key: &str, values: &Value, origin: &str) -> Result<usize, String> {
        let entry = self.logs.get_mut(key).ok_or("Reopen the form to resume event logging.")?;
        entry.last_draft = Some(values.clone());
        match entry.draft_key.clone() {
            Some(draft_key) => entry.log.capture_draft(values, &draft_key, origin, now_ms()),
            None => Ok(0),
        }
    }
}

impl Engine {
    /// The journals the loopback page writes to.
    pub fn form_event_store(&self) -> FormEventStore {
        self.form_events.clone()
    }

    /// The Host events are logged against: its id and address, as iOS
    /// names a connection.
    pub fn form_event_origin(&self) -> String {
        if self.server_id.is_empty() { self.base_url.clone() } else { format!("{}|{}", self.server_id, self.base_url) }
    }

    /// The connected Host keeps form events (`html_form_events`).
    pub fn form_events_supported(&self) -> bool {
        self.form_events_supported
    }

    /// A form's page opens: its journal, the Host's draft policy (cached,
    /// then fetched) and any send it still owes. The key the page uses.
    pub fn open_form_events(&mut self, artifact_id: &str, version: &Value) -> Result<String, String> {
        let origin = self.form_event_origin();
        let key = {
            let mut store = lock(&self.form_events);
            let root = store.root.clone().ok_or("event logging is off")?;
            let dir = directory(&root, artifact_id, version);
            let key = dir.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
            if !store.logs.contains_key(&key) {
                let log = EventLog::open(&dir, artifact_id, version)?;
                store.logs.insert(key.clone(), Entry { log, in_flight: None, scheduled: None, failures: 0, stopped: false, draft_key: None, last_draft: None, error_shown: false });
            }
            let entry = store.logs.get_mut(&key).expect("inserted above");
            if let Some(cached) = entry.log.cached_draft_key(&origin) {
                entry.draft_key = cached;
            }
            key
        };
        let path = format!("/artifacts/{}/events-config", clarp_core::endpoint::percent_encode_segment(artifact_id));
        self.api.get(&format!("form-events-config:{key}"), &path, &[]);
        self.form_events_changed(&key);
        Ok(key)
    }

    /// The page queued something: send it once the burst is over.
    pub fn form_events_changed(&mut self, key: &str) {
        if !self.form_events_supported {
            return;
        }
        let origin = self.form_event_origin();
        let mut store = lock(&self.form_events);
        let Some(entry) = store.logs.get_mut(key) else { return };
        if entry.stopped || entry.in_flight.is_some() || entry.scheduled.is_some() || entry.log.pending() == 0 || entry.log.origin() != Some(origin.as_str()) {
            return;
        }
        self.form_event_token += 1;
        let token = self.form_event_token;
        entry.scheduled = Some(token);
        let delay = retry_delay(entry.failures);
        drop(store);
        self.after(delay, Message::FormEventsDue { key: key.to_owned(), token });
    }

    pub(crate) fn form_events_due(&mut self, key: &str, token: u64) {
        let origin = self.form_event_origin();
        let mut store = lock(&self.form_events);
        let Some(entry) = store.logs.get_mut(key) else { return };
        if entry.scheduled != Some(token) {
            return;
        }
        entry.scheduled = None;
        if !self.form_events_supported || entry.stopped || entry.log.origin() != Some(origin.as_str()) {
            return;
        }
        let Some(batch) = entry.log.batch() else { return };
        let path = format!("/artifacts/{}/events", clarp_core::endpoint::percent_encode_segment(entry.log.artifact_id()));
        let body = batch.body.clone();
        entry.in_flight = Some((token, batch));
        drop(store);
        self.api.post_json(&format!("form-events:{key}:{token}"), &path, body, None);
    }

    /// `/server-info` answered: whether this Host keeps form events, and
    /// the journals a previous run left unsent go out to it.
    pub(crate) fn form_events_server_info(&mut self, info: &Object) {
        self.form_events_supported = info
            .get("capabilities")
            .and_then(|c| c.get("features"))
            .and_then(Value::as_array)
            .is_some_and(|features| features.iter().any(|f| f.as_str() == Some("html_form_events")));
        self.server_id = json::string(info, "server_id");
        if !self.form_events_supported {
            return;
        }
        let keys: Vec<String> = {
            let mut store = lock(&self.form_events);
            if let Some(root) = store.root.clone() {
                let listed: Vec<std::fs::DirEntry> = match std::fs::read_dir(&root) {
                    Ok(listed) => listed.flatten().collect(),
                    Err(error) if error.kind() == std::io::ErrorKind::NotFound => Vec::new(),
                    Err(error) => {
                        eprintln!("Engine: cannot list the form event journals in {}: {error}", root.display());
                        Vec::new()
                    }
                };
                for dir in listed {
                    let key = dir.file_name().to_string_lossy().into_owned();
                    if store.logs.contains_key(&key) {
                        continue;
                    }
                    match EventLog::reopen(&dir.path()) {
                        Ok(Some(log)) if log.pending() > 0 => {
                            store.logs.insert(key, Entry { log, in_flight: None, scheduled: None, failures: 0, stopped: false, draft_key: None, last_draft: None, error_shown: false });
                        }
                        Ok(_) => {}
                        Err(error) => eprintln!("Engine: a form event journal cannot be resumed: {error}"),
                    }
                }
            }
            store.logs.keys().cloned().collect()
        };
        for key in keys {
            self.form_events_changed(&key);
        }
    }

    /// Another Host or token: sends start over once it answers.
    pub(crate) fn reset_form_events(&mut self) {
        self.form_events_supported = false;
        self.server_id.clear();
        for entry in lock(&self.form_events).logs.values_mut() {
            entry.in_flight = None;
            entry.scheduled = None;
            entry.stopped = false;
            entry.failures = 0;
        }
    }

    fn form_event_sent(&mut self, tag: &str, outcome: Result<&Object, (&str, u16)>) {
        let Some((key, token)) = tag.strip_prefix("form-events:").and_then(|rest| rest.rsplit_once(':')) else { return };
        let token: u64 = token.parse().unwrap_or(0);
        let (artifact, status) = {
            let mut store = lock(&self.form_events);
            let Some(entry) = store.logs.get_mut(key) else { return };
            let Some((_, batch)) = entry.in_flight.take_if(|(sent, _)| *sent == token) else { return };
            let result = match outcome {
                Ok(receipt) => entry.log.acknowledge(&batch, &Value::Object(receipt.clone())),
                Err((message, status)) => {
                    if is_permanent(status) {
                        entry.stopped = true;
                    }
                    Err(if status > 0 { format!("{message} (HTTP {status})") } else { message.to_owned() })
                }
            };
            let status = match result {
                Ok(()) => {
                    entry.failures = 0;
                    std::mem::take(&mut entry.error_shown).then(|| "Event log synced".to_owned())
                }
                Err(error) => {
                    eprintln!("Engine: {} form events for {} not synced: {error}", batch.ids.len(), entry.log.artifact_id());
                    entry.failures += 1;
                    entry.error_shown = true;
                    Some(format!("Event log: {} awaiting sync ({error})", entry.log.pending()))
                }
            };
            (entry.log.artifact_id().to_owned(), status)
        };
        if let Some(status) = status {
            self.set_artifact_status(&artifact, &status);
        }
        self.form_events_changed(key);
    }

    fn form_events_config(&mut self, key: &str, config: &Object) {
        let origin = self.form_event_origin();
        {
            let mut store = lock(&self.form_events);
            let Some(entry) = store.logs.get_mut(key) else { return };
            if json::string(config, "artifact_id") != entry.log.artifact_id() || config.get("version") != Some(entry.log.version()) {
                eprintln!("Engine: the Host's event policy names another form: {config:?}");
                return;
            }
            let draft_key = config.get("draft_key").and_then(Value::as_str).map(str::to_owned);
            if let Err(error) = entry.log.save_draft_key(&origin, draft_key.as_deref()) {
                eprintln!("Engine: the form's event policy is not kept offline: {error}");
            }
            entry.draft_key = draft_key.clone();
            if let (Some(draft_key), Some(draft)) = (draft_key, entry.last_draft.clone())
                && let Err(error) = entry.log.capture_draft(&draft, &draft_key, &origin, now_ms())
            {
                eprintln!("Engine: the form's draft events are not queued: {error}");
                let artifact = entry.log.artifact_id().to_owned();
                drop(store);
                self.set_artifact_status(&artifact, &format!("Event log: {error}"));
                return;
            }
        }
        self.form_events_changed(key);
    }

    pub(crate) fn form_events_json(&mut self, tag: &str, object: &Object) -> bool {
        if let Some(key) = tag.strip_prefix("form-events-config:") {
            self.form_events_config(key, object);
            return true;
        }
        if tag.starts_with("form-events:") {
            self.form_event_sent(tag, Ok(object));
            return true;
        }
        false
    }

    pub(crate) fn form_events_failure(&mut self, tag: &str, message: &str, status: u16) -> bool {
        if tag.starts_with("form-events-config:") {
            // The cached policy (or none) stands; the failure is logged.
            return true;
        }
        if tag.starts_with("form-events:") {
            self.form_event_sent(tag, Err((message, status)));
            return true;
        }
        false
    }
}
