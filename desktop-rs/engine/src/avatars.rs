//! Agent portraits (the Qt controller's `avatarSource`): the agent's own
//! avatar, else the Host's bundled portrait for its name, fetched once,
//! rounded to 192 px and cached on disk. The UI asks for a session's file
//! and gets None until it is there.

use std::collections::HashMap;

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
}

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
        let Some((session, url)) = self.avatars.requests.remove(tag) else { return true };
        let mime = clarp_core::media::mime(content_type);
        if bytes.is_empty() || bytes.len() > clarp_core::media::MAX_PORTRAIT_BYTES || !mime.starts_with("image/") {
            eprintln!("Engine: {session}'s portrait is not an image ({mime}, {} bytes)", bytes.len());
            self.avatars.failures.insert(session, url);
            return true;
        }
        let Some(path) = self.portrait_root().map(|root| clarp_core::media::portrait_cache_path(&root, &format!("{}{url}", self.base_url)))
        else {
            eprintln!("Engine: no cache folder for portraits");
            self.avatars.failures.insert(session, url);
            return true;
        };
        let portrait = clarp_core::media::rounded_portrait(bytes).unwrap_or_else(|| bytes.to_vec());
        let written = path.parent().map_or(Ok(()), std::fs::create_dir_all).and_then(|()| std::fs::write(&path, portrait));
        if let Err(error) = written {
            eprintln!("Engine: could not cache {}: {error}", path.display());
            self.avatars.failures.insert(session, url);
            return true;
        }
        if self.avatars.urls.get(&session) == Some(&url) {
            let source = url::Url::from_file_path(&path).map(|u| u.to_string()).unwrap_or_default();
            self.avatars.sources.insert(session, source);
            self.avatars.revision += 1;
            self.changes.push(Change::Avatars);
        }
        true
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
