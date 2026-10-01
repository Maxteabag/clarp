//! Agent portraits (the Qt controller's `avatarSource`): the agent's own
//! avatar, else the Host's bundled portrait for its name, fetched once,
//! rounded to 192 px and cached on disk. The UI asks for a session's file
//! and gets None until it is there.

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::mpsc::Sender;
use std::time::{Duration, Instant};

use crate::{Change, Engine};

#[derive(Default)]
pub(crate) struct Avatars {
    /// session → the avatar URL its cached file is for
    urls: HashMap<String, String>,
    /// session → cached `file://` URL
    sources: HashMap<String, String>,
    /// session → the URL that failed (not asked for again)
    failures: HashMap<String, String>,
    /// tag → (session, URL)
    requests: HashMap<String, (String, String)>,
    next: u64,
    revision: u64,
    /// Where portraits are cached; by default the Rust client's cache.
    directory: Option<std::path::PathBuf>,
    /// The portrait rounder's queue (started with the first portrait).
    rounder: Option<Sender<Round>>,
}

/// A downloaded portrait to round and write to `path`.
struct Round {
    tag: String,
    path: PathBuf,
    bytes: Vec<u8>,
}

/// A rounded portrait written to `path`, or why not.
pub(crate) struct Cached {
    tag: String,
    path: PathBuf,
    result: Result<(), String>,
}

/// How often the rounder hands finished portraits over: each hand-over
/// rebuilds the chat list once.
const HAND_OVER: Duration = Duration::from_millis(100);

impl Engine {
    /// Bumped whenever a portrait arrives.
    pub fn avatar_revision(&self) -> u64 {
        self.avatars.revision
    }

    /// Caches portraits in `directory` instead (tests).
    pub fn set_portrait_directory(&mut self, directory: std::path::PathBuf) {
        self.avatars.directory = Some(directory);
    }

    fn portrait_root(&self) -> Option<std::path::PathBuf> {
        self.avatars.directory.clone().or_else(clarp_core::media::cache_dir)
    }

    /// The agent's cached portrait (`file://`), requesting it when missing.
    pub fn avatar_source(&mut self, session: &str) -> Option<String> {
        let agent = self.roster.find(session)?;
        let url = clarp_core::media::avatar_url(&agent.avatar_url, clarp_core::protocol::display_name(agent));
        let avatars = &mut self.avatars;
        if url.is_empty() {
            avatars.urls.remove(session);
            avatars.sources.remove(session);
            return None;
        }
        if avatars.urls.get(session) != Some(&url) {
            avatars.urls.insert(session.to_owned(), url.clone());
            avatars.sources.remove(session);
            avatars.failures.remove(session);
        }
        if let Some(source) = avatars.sources.get(session) {
            return Some(source.clone());
        }
        if avatars.failures.get(session) == Some(&url) {
            return None;
        }
        let base = self.base_url.clone();
        let cached = self
            .portrait_root()
            .map(|root| clarp_core::media::portrait_cache_path(&root, &format!("{base}{url}")))
            .filter(|path| path.exists());
        if let Some(path) = cached {
            let source = url::Url::from_file_path(&path).map(|u| u.to_string()).unwrap_or_default();
            self.avatars.sources.insert(session.to_owned(), source.clone());
            return Some(source);
        }
        if self.avatars.requests.values().any(|(s, u)| s == session && *u == url) {
            return None;
        }
        self.avatars.next += 1;
        let tag = format!("avatar:{}", self.avatars.next);
        self.avatars.requests.insert(tag.clone(), (session.to_owned(), url.clone()));
        self.api.get_bytes(&tag, &url);
        None
    }

    pub(crate) fn avatar_bytes(&mut self, tag: &str, bytes: &[u8], content_type: &str) -> bool {
        if !tag.starts_with("avatar:") {
            return false;
        }
        // The request stays listed until the portrait is cached, so it is
        // not asked for again meanwhile.
        let Some((session, url)) = self.avatars.requests.get(tag).cloned() else { return true };
        let mime = clarp_core::media::mime(content_type);
        if bytes.is_empty() || bytes.len() > clarp_core::media::MAX_PORTRAIT_BYTES || !mime.starts_with("image/") {
            eprintln!("Engine: {session}'s portrait is not an image ({mime}, {} bytes)", bytes.len());
            self.avatars.requests.remove(tag);
            self.avatars.failures.insert(session, url);
            return true;
        }
        let Some(path) = self.portrait_root().map(|root| clarp_core::media::portrait_cache_path(&root, &format!("{}{url}", self.base_url)))
        else {
            eprintln!("Engine: no cache folder for portraits");
            self.avatars.requests.remove(tag);
            self.avatars.failures.insert(session, url);
            return true;
        };
        // Decoding, scaling and encoding a Host portrait (512 px) takes
        // milliseconds, a hundred of them at a first launch: one thread
        // does them in turn, leaving the UI thread and the other cores be.
        let round = Round { tag: tag.to_owned(), path, bytes: bytes.to_vec() };
        let rounder = self.avatars.rounder.get_or_insert_with(|| start_rounder(self.sender.clone(), self.wake.clone()));
        if let Err(error) = rounder.send(round) {
            eprintln!("Engine: the portrait rounder stopped");
            self.avatars.rounder = None;
            let round = error.0;
            self.avatars.requests.remove(&round.tag);
        }
        true
    }

    pub(crate) fn portrait_cached(&mut self, cached: Cached) {
        let Cached { tag, path, result } = cached;
        let Some((session, url)) = self.avatars.requests.remove(&tag) else { return };
        if let Err(error) = result {
            eprintln!("Engine: could not cache {}: {error}", path.display());
            self.avatars.failures.insert(session, url);
            return;
        }
        if self.avatars.urls.get(&session) == Some(&url) {
            let source = url::Url::from_file_path(&path).map(|u| u.to_string()).unwrap_or_default();
            self.avatars.sources.insert(session, source);
            self.avatars.revision += 1;
            self.changes.push(Change::Avatars);
        }
    }

    pub(crate) fn avatar_failure(&mut self, tag: &str, detail: &str) -> bool {
        if !tag.starts_with("avatar:") {
            return false;
        }
        if let Some((session, url)) = self.avatars.requests.remove(tag) {
            eprintln!("Engine: {session}'s portrait failed: {detail}");
            self.avatars.failures.insert(session, url);
        }
        true
    }
}

/// The rounder thread: rounds what is queued, handing results over every
/// `HAND_OVER` and whenever the queue runs dry.
fn start_rounder(sender: Sender<crate::Message>, wake: std::sync::Arc<dyn Fn() + Send + Sync>) -> Sender<Round> {
    let (queue, rounds) = std::sync::mpsc::channel::<Round>();
    let hand_over = move |cached: &mut Vec<Cached>| {
        if !cached.is_empty() && sender.send(crate::Message::PortraitsCached(std::mem::take(cached))).is_ok() {
            wake();
        }
    };
    let spawned = std::thread::Builder::new().name("portrait-rounder".into()).spawn(move || {
        while let Ok(first) = rounds.recv() {
            let (mut cached, mut since) = (Vec::new(), Instant::now());
            let mut next = Some(first);
            while let Some(Round { tag, path, bytes }) = next {
                let portrait = clarp_core::media::rounded_portrait(&bytes).unwrap_or(bytes);
                let result = path.parent().map_or(Ok(()), std::fs::create_dir_all).and_then(|()| std::fs::write(&path, portrait));
                cached.push(Cached { tag, path, result: result.map_err(|e| e.to_string()) });
                if since.elapsed() >= HAND_OVER {
                    hand_over(&mut cached);
                    since = Instant::now();
                }
                next = rounds.try_recv().ok();
            }
            hand_over(&mut cached);
        }
    });
    if let Err(error) = spawned {
        eprintln!("Engine: no portrait rounder: {error}");
    }
    queue
}
