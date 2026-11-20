//! The router's fixed sentences. Only ordinary speech can supply a clip.
use std::collections::{HashMap, HashSet};

use clarp_kernel::router::{
    flow::phrase_key,
    phrases::{self, Clip, Key, MAX_CLIP, Voice},
};
use serde_json::Value;
use sha2::{Digest, Sha256};
use tokio::sync::{Mutex, watch};
use toml::Table;

use crate::edge::App;
use crate::fleet::AgentRecord;
use crate::wire;

const KIND: &str = "router-phrase";
const BOOK: &str = "clips";

#[derive(Clone)]
enum Playback {
    Cached(String),
    Ordinary(String),
    Retry,
    Silent,
}
impl Playback {
    fn announce(&self, app: &App, speaker: &AgentRecord, phrase: &str) {
        let session = speaker.session.as_str();
        let event = match self {
            Self::Cached(name) => phrases::event(
                name,
                phrase,
                session,
                (!speaker.agent_id.is_empty()).then_some(speaker.agent_id.as_str()),
                &speaker.voice_id,
            ),
            Self::Ordinary(name) => {
                wire::announcement((!session.is_empty()).then_some(session), name)
            }
            Self::Retry | Self::Silent => return,
        };
        app.hub.publish(event, app.clock.now_ms());
    }
}

type InFlight = HashMap<(Key, bool), watch::Receiver<Option<Playback>>>;

#[derive(Default)]
pub(crate) struct Desk {
    book: Mutex<Option<Vec<Clip>>>,
    making: Mutex<InFlight>,
}

#[derive(Clone, Copy)]
struct Store<'a> {
    fleet: &'a crate::fleet::FleetHandle,
    blobs: &'a dyn clarp_ports::blobs::Blobs,
}

impl<'a> From<&'a App> for Store<'a> {
    fn from(app: &'a App) -> Self {
        Self {
            fleet: &app.fleet,
            blobs: app.blobs.as_ref(),
        }
    }
}

fn name(bytes: &[u8]) -> String {
    format!("phrase-{:x}.mp3", Sha256::digest(bytes))
}

fn verified(store: Store<'_>, row: &Clip) -> Option<Vec<u8>> {
    if !(1..=MAX_CLIP).contains(&row.size) || !phrases::blob_name(&row.name) {
        return None;
    }
    let bytes = store.blobs.read(&row.name, 0, MAX_CLIP + 1).ok()?;
    (bytes.len() == row.size && name(&bytes) == row.name).then_some(bytes)
}

async fn write(store: Store<'_>, rows: &[Clip]) -> bool {
    let bytes = match serde_json::to_vec(rows) {
        Ok(bytes) => bytes,
        Err(why) => {
            store.fleet.unkept(KIND, &why.to_string());
            return false;
        }
    };
    if let Err(why) = store.fleet.keep(KIND, BOOK.to_owned(), Some(bytes)).await {
        store.fleet.unkept(KIND, &why.0);
        return false;
    }
    true
}

fn forget(store: Store<'_>, name: &str) -> bool {
    if let Err(why) = store.blobs.remove(name) {
        store.fleet.unkept(KIND, &why.to_string());
        return false;
    }
    true
}

async fn load(store: Store<'_>) -> Option<Vec<Clip>> {
    let raw = match store.fleet.kept(KIND, BOOK.to_owned()).await {
        Ok(bytes) => bytes,
        Err(why) => {
            store.fleet.unkept(KIND, &why.0);
            return None;
        }
    };
    let mut changed = false;
    let mut rows = Vec::new();
    let mut keys = HashSet::new();
    if let Some(raw) = raw {
        match serde_json::from_slice::<Vec<Value>>(&raw) {
            Ok(items) => {
                for item in items {
                    if let Ok(row) = serde_json::from_value::<Clip>(item)
                        && !row.key.voice.provider.is_empty()
                        && !row.key.voice.voice.is_empty()
                        && (1..=MAX_CLIP).contains(&row.size)
                        && phrases::blob_name(&row.name)
                        && keys.insert(row.key.clone())
                    {
                        if phrase_key(&row.text) == Some(row.key.phrase.as_str()) {
                            rows.push(row);
                        } else {
                            changed = true;
                        }
                    } else {
                        changed = true;
                        store.fleet.unkept(KIND, "invalid phrase record");
                    }
                }
            }
            Err(_) => {
                changed = true;
                store.fleet.unkept(KIND, "invalid phrase book");
            }
        }
    }
    let excess = phrases::excess(&rows);
    rows.drain(..excess);
    if (changed || excess > 0) && !write(store, &rows).await {
        return None;
    }
    // A crash between the bytes and the record, or between eviction and unlink,
    // leaves only unreferenced files in this cache's private namespace.
    let live: HashSet<_> = rows.iter().map(|row| row.name.as_str()).collect();
    match store.blobs.names_with_prefix("phrase-") {
        Ok(names) => {
            for name in names {
                if phrases::blob_name(&name)
                    && !live.contains(name.as_str())
                    && !forget(store, &name)
                {
                    return None;
                }
            }
        }
        Err(why) => {
            store.fleet.unkept(KIND, &why.to_string());
            return None;
        }
    }
    Some(rows)
}

impl Desk {
    async fn held(&self, store: Store<'_>) -> tokio::sync::MutexGuard<'_, Option<Vec<Clip>>> {
        let mut book = self.book.lock().await;
        if book.is_none() {
            *book = load(store).await;
        }
        book
    }

    async fn drop_audio(store: Store<'_>, book: &mut Option<Vec<Clip>>, name: &str) {
        if let Some(rows) = book {
            rows.retain(|r| r.name != name);
            if !write(store, rows).await || !forget(store, name) {
                *book = None;
            }
        }
    }

    pub(crate) async fn audio(&self, app: &App, name: &str) -> Option<Vec<u8>> {
        let store = Store::from(app);
        let mut book = self.held(store).await;
        let row = book.as_ref()?.iter().find(|r| r.name == name)?;
        if let Some(bytes) = verified(store, row) {
            return Some(bytes);
        }
        store.fleet.unkept(KIND, "unreadable phrase clip");
        Self::drop_audio(store, &mut book, name).await;
        None
    }

    async fn lookup(&self, store: Store<'_>, key: &Key, text: &str) -> Option<String> {
        let mut book = self.held(store).await;
        let rows = book.as_mut()?;
        let i = rows.iter().position(|row| &row.key == key)?;
        if rows[i].text == text {
            if verified(store, &rows[i]).is_some() {
                return Some(rows[i].name.clone());
            }
            store.fleet.unkept(KIND, "unreadable phrase clip");
            let name = rows[i].name.clone();
            Self::drop_audio(store, &mut book, &name).await;
        }
        None
    }

    async fn keep(&self, store: Store<'_>, key: Key, text: String, bytes: &[u8]) {
        if !(1..=MAX_CLIP).contains(&bytes.len()) {
            return;
        }
        let mut book = self.held(store).await;
        let Some(rows) = book.as_ref() else {
            return;
        };
        let file = name(bytes);
        let mut next = rows.clone();
        next.retain(|row| row.key != key);
        next.push(Clip {
            key,
            text,
            name: file.clone(),
            size: bytes.len(),
        });
        next.drain(..phrases::excess(&next));
        if let Err(why) = store.blobs.put(&file, bytes) {
            store.fleet.unkept(KIND, &why.to_string());
            *book = None;
            return;
        }
        if !write(store, &next).await {
            if !rows.iter().any(|r| r.name == file) {
                forget(store, &file);
            }
            *book = None;
            return;
        }
        let live: HashSet<_> = next.iter().map(|r| r.name.as_str()).collect();
        for row in rows {
            if !live.contains(row.name.as_str()) && !forget(store, &row.name) {
                *book = None;
                return;
            }
        }
        *book = Some(next);
    }

    /// Hand the completed clip to the first requester and waiters before touching either store.
    async fn deliver(
        &self,
        store: Store<'_>,
        key: Key,
        text: String,
        bytes: Option<Vec<u8>>,
        announce: impl FnOnce(),
    ) {
        announce();
        if let Some(bytes) = bytes {
            self.keep(store, key, text, &bytes).await;
        }
    }

    /// Concurrent requests share an attempt in the same producing voice. Only the primary
    /// can hit or populate the store. After a retryable refusal each caller selects its own fallback.
    pub(super) async fn say(
        &self,
        app: &App,
        speaker: &AgentRecord,
        text: &str,
        phrase: &'static str,
        (voice, primary): (Voice, bool),
        owners: &Table,
    ) -> bool {
        let flight = (
            Key {
                phrase: phrase.into(),
                voice,
            },
            primary,
        );
        let mut making = self.making.lock().await;
        let (mut rx, first) = if let Some(existing) = making.get(&flight) {
            (existing.clone(), false)
        } else {
            let (tx, rx) = watch::channel(None);
            making.insert(flight.clone(), rx.clone());
            let app = app.clone();
            let who = speaker.clone();
            let text = text.to_owned();
            let owners = owners.clone();
            tokio::spawn(async move {
                let desk = &app.router.phrases;
                let key = &flight.0;
                let cached = if primary {
                    desk.lookup(Store::from(&app), key, &text).await
                } else {
                    None
                };
                let (playback, bytes) = match cached {
                    Some(name) => (Playback::Cached(name), None),
                    None => match app
                        .mouth
                        .make_announcement(&who, text.clone(), &owners, &key.voice, primary)
                        .await
                    {
                        Ok(Some((name, bytes))) => (Playback::Ordinary(name), bytes),
                        Err(why) if why.may_be_said_by_another() => (Playback::Retry, None),
                        _ => (Playback::Silent, None),
                    },
                };
                desk.deliver(Store::from(&app), key.clone(), text, bytes, || {
                    playback.announce(&app, &who, phrase);
                    tx.send_replace(Some(playback));
                })
                .await;
                desk.making.lock().await.remove(&flight);
            });
            (rx, true)
        };
        drop(making);
        let Ok(answer) = rx.wait_for(Option::is_some).await else {
            return false;
        };
        if let Some(clip) = &*answer {
            if !first {
                clip.announce(app, speaker, phrase);
            }
            return matches!(clip, Playback::Retry);
        }
        false
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;
    use crate::unkept::at_the_port::place;
    use clarp_ports::blobs::BlobEngine as _;

    #[tokio::test]
    async fn a_failed_phrase_record_never_commits_audio_or_evicts_a_kept_clip() {
        let place = place("router-phrase-keeps");
        let blobs = clarp_store_mem::MemBlobs::new()
            .open(std::path::Path::new("test"))
            .unwrap();
        let store = Store {
            fleet: &place.fleet,
            blobs: blobs.as_ref(),
        };
        let desk = Desk::default();
        let text = "Was that for me?";
        let key = |voice: &str| Key {
            phrase: "clarify_target".into(),
            voice: Voice {
                provider: "cartesia".into(),
                voice: voice.into(),
                model: "model".into(),
            },
        };
        let orphan = name(b"orphan");
        blobs.put(&orphan, b"orphan").unwrap();
        blobs.put("phrase-not-a-digest.mp3", b"other file").unwrap();
        *place.refused.lock().unwrap() = Some(format!("read:{}", crate::fleet::shelf(KIND)));
        assert!(desk.lookup(store, &key("first"), text).await.is_none());
        assert_eq!(blobs.len(&orphan).unwrap(), Some(6));
        assert_eq!(place.said.lock().unwrap().len(), 1);
        *place.refused.lock().unwrap() = None;
        place.said.lock().unwrap().clear();
        desk.keep(store, key("first"), text.into(), b"kept").await;
        assert_eq!(blobs.len(&orphan).unwrap(), None);
        assert_eq!(blobs.len("phrase-not-a-digest.mp3").unwrap(), Some(10));
        *place.refused.lock().unwrap() = Some(crate::fleet::shelf(KIND));
        let mut announced = 0;
        desk.deliver(
            store,
            key("second"),
            text.into(),
            Some(b"absent".to_vec()),
            || {
                assert!(
                    place.said.lock().unwrap().is_empty(),
                    "write failed before the announcement"
                );
                announced += 1;
            },
        )
        .await;
        assert_eq!(announced, 1);
        assert_eq!(blobs.len(&name(b"absent")).unwrap(), None);
        assert_eq!(desk.lookup(store, &key("second"), text).await, None);
        assert_eq!(
            desk.lookup(store, &key("first"), text).await,
            Some(name(b"kept"))
        );
        // A failed write reusing another record's content cannot discard its audio.
        desk.keep(store, key("third"), text.into(), b"kept").await;
        assert_eq!(blobs.len(&name(b"kept")).unwrap(), Some(4));
        assert_eq!(desk.lookup(store, &key("third"), text).await, None);
        assert_eq!(place.said.lock().unwrap().len(), 1);
        assert!(place.said.lock().unwrap()[0].contains("router-phrase"));
        *place.refused.lock().unwrap() = None;
        desk.keep(store, key("second"), text.into(), b"valid").await;
        assert_eq!(
            Desk::default().lookup(store, &key("second"), text).await,
            Some(name(b"valid"))
        );
    }
}
