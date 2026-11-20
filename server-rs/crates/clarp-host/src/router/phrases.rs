//! The router's fixed sentences. Only ordinary speech can supply a clip.
use std::collections::{HashMap, HashSet};

use clarp_kernel::router::{flow::phrase_key, phrases::{self, Clip, Key, MAX_CLIP, Voice}};
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
}
type Answer = Option<Playback>;

#[derive(Default)]
pub(crate) struct Desk {
    book: Mutex<Option<Vec<Clip>>>,
    making: Mutex<HashMap<Key, watch::Receiver<Option<Answer>>>>,
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
        Err(why) => { app.fleet.unkept(KIND, &why.to_string()); return false; }
    };
    if let Err(why) = app.fleet.keep(KIND, BOOK.to_owned(), Some(bytes)).await {
        app.fleet.unkept(KIND, &why.0);
        return false;
    }
    true
}

fn forget(app: &App, name: &str) {
    if let Err(why) = app.blobs.remove(name) {
        app.fleet.unkept(KIND, &why.to_string());
    }
}

async fn load(app: &App) -> Option<Vec<Clip>> {
    let raw = match app.fleet.kept(KIND, BOOK.to_owned()).await {
        Ok(bytes) => bytes,
        Err(why) => { app.fleet.unkept(KIND, &why.0); return None; }
    };
    let mut changed = false;
    let mut rows = Vec::new();
    let mut keys = HashSet::new();
    if let Some(raw) = raw {
        match serde_json::from_slice::<Vec<Value>>(&raw) {
            Ok(items) => for item in items {
                if let Ok(row) = serde_json::from_value::<Clip>(item)
                    && !row.key.voice.provider.is_empty() && !row.key.voice.voice.is_empty()
                    && (1..=MAX_CLIP).contains(&row.size) && phrases::blob_name(&row.name)
                    && keys.insert(row.key.clone()) {
                    if phrase_key(&row.text) == Some(row.key.phrase.as_str()) {
                        rows.push(row);
                    } else { changed = true; }
                } else {
                    changed = true;
                    app.fleet.unkept(KIND, "invalid phrase record");
                }
            },
            Err(_) => {changed = true; app.fleet.unkept(KIND, "invalid phrase book");}
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
        Ok(names) => for name in names {
            if phrases::blob_name(&name) && !live.contains(name.as_str()) { forget(app, &name); }
        },
        Err(why) => { app.fleet.unkept(KIND, &why.to_string()); return None; }
    }
    Some(rows)
}

impl Desk {
    async fn held(&self, app: &App) -> tokio::sync::MutexGuard<'_, Option<Vec<Clip>>> {
        let mut book = self.book.lock().await;
        if book.is_none() { *book = load(app).await; }
        book
    }

    pub(crate) async fn audio(&self, app: &App, name: &str) -> Option<Vec<u8>> {
        let mut book = self.held(app).await;
        let rows = book.as_mut()?;
        let row = rows.iter().find(|r| r.name == name)?;
        if let Some(bytes) = verified(app, row) {return Some(bytes);}
        app.fleet.unkept(KIND, "unreadable phrase clip");
        rows.retain(|r| r.name != name);
        if write(app, rows).await {forget(app, name);} else {*book = None;}
        None
    }

    async fn lookup(&self, app: &App, key: &Key, text: &str) -> Option<String> {
        let mut book = self.held(app).await;
        let rows = book.as_mut()?;
        let i = rows.iter().position(|row| &row.key == key)?;
        if rows[i].text == text {
            if verified(app, &rows[i]).is_some() { return Some(rows[i].name.clone()); }
            app.fleet.unkept(KIND, "unreadable phrase clip");
        }
        let removed = rows.remove(i);
        if !write(app, rows).await { *book = None; }
        else if !rows.iter().any(|r| r.name == removed.name) { forget(app, &removed.name); }
        None
    }

    async fn keep(&self, app: &App, key: Key, text: String, bytes: &[u8]) {
        if !(1..=MAX_CLIP).contains(&bytes.len()) { return; }
        let mut book = self.held(app).await;
        let Some(rows) = book.as_ref() else { return; };
        let file = name(bytes);
        let mut next = rows.clone();
        next.retain(|row| row.key != key);
        next.push(Clip { key, text, name: file.clone(), size: bytes.len() });
        next.drain(..phrases::excess(&next));
        if let Err(why) = app.blobs.put(&file, bytes) {
            app.fleet.unkept(KIND, &why.to_string());
            *book = None;
            return;
        }
        if !write(app, &next).await {
            if !rows.iter().any(|r| r.name == file) { forget(app, &file); }
            *book = None;
            return;
        }
        let live: HashSet<_> = next.iter().map(|r| r.name.as_str()).collect();
        for row in rows {
            if !live.contains(row.name.as_str()) { forget(app, &row.name); }
        }
        *book = Some(next);
    }

    pub(super) async fn say(&self, app: &App, speaker: AgentRecord, text: String, phrase: &'static str, voice: Voice, owners: Table) {
        let key = Key {phrase: phrase.into(), voice};
        let mut making = self.making.lock().await;
        let mut rx = if let Some(existing) = making.get(&key) {
            existing.clone()
        } else {
            let (tx, rx) = watch::channel(None);
            making.insert(key.clone(), rx.clone());
            let app = app.clone();
            let who = speaker.clone();
            tokio::spawn(async move {
                let desk = &app.router.phrases;
                if let Some(name) = desk.lookup(&app, &key, &text).await {
                    tx.send_replace(Some(Some(Playback::Cached(name))));
                } else {
                    let made = app.mouth.make_announcement(&who, text.clone(), &owners, &key.voice).await;
                    let playback = made.as_ref().map(|(name, _)| Playback::Ordinary(name.clone()));
                    // Every asker can publish the ordinary clip before persistence begins.
                    tx.send_replace(Some(playback));
                    if let Some((_, Some(bytes))) = made { desk.keep(&app, key.clone(), text, &bytes).await; }
                }
                desk.making.lock().await.remove(&key);
            });
            rx
        };
        drop(making);
        let Ok(answer) = rx.wait_for(|value| value.is_some()).await else { return; };
        let session = speaker.session.as_str();
        let event = match answer.as_ref().and_then(Option::as_ref) {
            Some(Playback::Cached(name)) => phrases::event(name, phrase, session,
                (!speaker.agent_id.is_empty()).then_some(speaker.agent_id.as_str()), &speaker.voice_id),
            Some(Playback::Ordinary(name)) => wire::announcement((!session.is_empty()).then_some(session), name),
            None => return,
        };
        app.hub.publish(event, app.clock.now_ms());
    }
}
