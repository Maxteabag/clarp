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
            Self::Cached(name) => phrases::event(name,phrase,session,
                (!speaker.agent_id.is_empty()).then_some(speaker.agent_id.as_str()), &speaker.voice_id),
            Self::Ordinary(name) => wire::announcement((!session.is_empty()).then_some(session),name),
            Self::Retry | Self::Silent => return,
        };
        app.hub.publish(event,app.clock.now_ms());
    }
}

#[derive(Default)]
pub(crate) struct Desk {
    book: Mutex<Option<Vec<Clip>>>,
    making: Mutex<HashMap<(Key, bool), watch::Receiver<Option<Playback>>>>,
}

fn name(bytes: &[u8]) -> String {
    format!("phrase-{:x}.mp3", Sha256::digest(bytes))
}

fn verified(app: &App, row: &Clip) -> Option<Vec<u8>> {
    if !(1..=MAX_CLIP).contains(&row.size) || !phrases::blob_name(&row.name) {
        return None;
    }
    let bytes = app.blobs.read(&row.name, 0, MAX_CLIP + 1).ok()?;
    (bytes.len() == row.size && name(&bytes) == row.name).then_some(bytes)
}

async fn write(app: &App, rows: &[Clip]) -> bool {
    let bytes = match serde_json::to_vec(rows) {
        Ok(bytes) => bytes,
        Err(why) => {
            app.fleet.unkept(KIND, &why.to_string());
            return false;
        }
    };
    if let Err(why) = app.fleet.keep(KIND, BOOK.to_owned(), Some(bytes)).await {
        app.fleet.unkept(KIND, &why.0);
        return false;
    }
    true
}

fn forget(app: &App, name: &str) -> bool {
    if let Err(why) = app.blobs.remove(name) {
        app.fleet.unkept(KIND, &why.to_string());
        return false;
    }
    true
}

async fn load(app: &App) -> Option<Vec<Clip>> {
    let raw = match app.fleet.kept(KIND, BOOK.to_owned()).await {
        Ok(bytes) => bytes,
        Err(why) => {
            app.fleet.unkept(KIND, &why.0);
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
                        app.fleet.unkept(KIND, "invalid phrase record");
                    }
                }
            }
            Err(_) => {
                changed = true;
                app.fleet.unkept(KIND, "invalid phrase book");
            }
        }
    }
    let excess = phrases::excess(&rows);
    rows.drain(..excess);
    if (changed || excess > 0) && !write(app, &rows).await {
        return None;
    }
    // A crash between the bytes and the record, or between eviction and unlink,
    // leaves only unreferenced files in this cache's private namespace.
    let live: HashSet<_> = rows.iter().map(|row| row.name.as_str()).collect();
    match app.blobs.names_with_prefix("phrase-") {
        Ok(names) => {
            for name in names {
                if phrases::blob_name(&name) && !live.contains(name.as_str()) && !forget(app, &name) {
                    return None;
                }
            }
        }
        Err(why) => {
            app.fleet.unkept(KIND, &why.to_string());
            return None;
        }
    }
    Some(rows)
}

impl Desk {
    async fn held(&self, app: &App) -> tokio::sync::MutexGuard<'_, Option<Vec<Clip>>> {
        let mut book = self.book.lock().await;
        if book.is_none() {
            *book = load(app).await;
        }
        book
    }

    async fn drop_audio(app: &App, book: &mut Option<Vec<Clip>>, name: &str) {
        if let Some(rows) = book {
            rows.retain(|r| r.name != name);
            if !write(app,rows).await || !forget(app,name) { *book=None; }
        }
    }

    pub(crate) async fn audio(&self, app: &App, name: &str) -> Option<Vec<u8>> {
        let mut book = self.held(app).await;
        let row = book.as_ref()?.iter().find(|r| r.name == name)?;
        if let Some(bytes) = verified(app,row) { return Some(bytes); }
        app.fleet.unkept(KIND,"unreadable phrase clip");
        Self::drop_audio(app,&mut book,name).await;
        None
    }

    async fn lookup(&self, app: &App, key: &Key, text: &str) -> Option<String> {
        let mut book = self.held(app).await;
        let rows = book.as_mut()?;
        let i = rows.iter().position(|row| &row.key == key)?;
        if rows[i].text == text {
            if verified(app,&rows[i]).is_some() {return Some(rows[i].name.clone());}
            app.fleet.unkept(KIND,"unreadable phrase clip");
            let name = rows[i].name.clone();
            Self::drop_audio(app,&mut book,&name).await;
        }
        None
    }

    async fn keep(&self, app: &App, key: Key, text: String, bytes: &[u8]) {
        if !(1..=MAX_CLIP).contains(&bytes.len()) {
            return;
        }
        let mut book = self.held(app).await;
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
        if let Err(why) = app.blobs.put(&file, bytes) {
            app.fleet.unkept(KIND, &why.to_string());
            *book = None;
            return;
        }
        if !write(app, &next).await {
            if !rows.iter().any(|r| r.name == file) {
                forget(app, &file);
            }
            *book = None;
            return;
        }
        let live: HashSet<_> = next.iter().map(|r| r.name.as_str()).collect();
        for row in rows {
            if !live.contains(row.name.as_str()) && !forget(app, &row.name) {
                *book = None;
                return;
            }
        }
        *book = Some(next);
    }

    /// Concurrent requests share an attempt in the same producing voice. Only the primary
    /// can hit or populate the store. After a retryable refusal each caller selects its own fallback.
    pub(super) async fn say(&self, app: &App, speaker: &AgentRecord, text: &str, phrase: &'static str,
        (voice, primary): (Voice,bool), owners: &Table) -> bool {
        let flight = (Key {phrase: phrase.into(), voice}, primary);
        let mut making = self.making.lock().await;
        let (mut rx, first) = if let Some(existing) = making.get(&flight) {
            (existing.clone(),false)
        } else {
            let (tx, rx) = watch::channel(None);
            making.insert(flight.clone(),rx.clone());
            let app = app.clone();
            let who = speaker.clone();
            let text = text.to_owned();
            let owners = owners.clone();
            tokio::spawn(async move {
                let desk = &app.router.phrases;
                let key = &flight.0;
                let cached = if primary { desk.lookup(&app,key,&text).await } else { None };
                let (playback,bytes) = match cached {
                    Some(name) => (Playback::Cached(name),None),
                    None => match app.mouth.make_announcement(&who,text.clone(),&owners,&key.voice,primary).await {
                        Ok(Some((name, bytes))) => (Playback::Ordinary(name),bytes),
                        Err(why) if why.may_be_said_by_another() => (Playback::Retry,None),
                        _ => (Playback::Silent,None),
                    }
                };
                playback.announce(&app,&who,phrase);
                tx.send_replace(Some(playback));
                if let Some(bytes) = bytes { desk.keep(&app,key.clone(),text,&bytes).await; }
                desk.making.lock().await.remove(&flight);
            });
            (rx,true)
        };
        drop(making);
        let Ok(answer) = rx.wait_for(Option::is_some).await else {return false;};
        if let Some(clip) = &*answer {
            if !first { clip.announce(app,speaker,phrase); }
            return matches!(clip, Playback::Retry);
        }
        false
    }
}
