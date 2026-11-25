//! Message search. A Host that lists `message_search` searches every chat
//! itself (`GET /search`): the engine asks it, and every paired Host read
//! beside it that lists the feature, pages each with its own cursor and
//! merges what they found newest first (`host_search_results`).
//!
//! On a Host without it, the search runs over what this client holds: the
//! chats loaded in this run, indexed again only where their rows changed,
//! and every chat in the transcript cache, read once off the UI thread when
//! the first search asks.

use std::collections::HashSet;

use clarp_core::message_search::{FEATURE, Hit, HostHit, HostPage, SearchIndex, Source};
use clarp_core::protocol;
use serde_json::Value;

use crate::{Change, Engine, Message};

#[derive(Debug, Default, Clone, PartialEq, Eq)]
enum CacheRead {
    #[default]
    NotStarted,
    Reading,
    Done,
    Failed(String),
}

#[derive(Debug, Default)]
pub(crate) struct SearchState {
    index: SearchIndex,
    /// Loaded chats whose rows changed since they were indexed.
    dirty: HashSet<String>,
    cache: CacheRead,
    /// The Host the index is of; another Host starts it afresh.
    base_url: String,
    /// This engine's Host's own search (`GET /search`).
    host: HostSearch,
}

/// Hits a page of the Host's search asks for.
pub const HOST_PAGE: usize = 20;

/// One Host's answers to the current words.
#[derive(Debug, Default)]
struct HostSearch {
    /// The Host lists `message_search`.
    offered: bool,
    query: String,
    /// Only this chat (its session on this Host); empty for every chat.
    session: String,
    /// Bumped by every new search: older answers are dropped.
    generation: u64,
    hits: Vec<HostHit>,
    next_before: Option<String>,
    partial: bool,
    pending: bool,
    /// The first page for `query` arrived (or failed).
    answered: bool,
    error: String,
}

impl SearchState {
    pub(crate) fn changed(&mut self, session: &str) {
        self.dirty.insert(session.to_owned());
    }
}

/// What a search found, and what it searched.
#[derive(Debug, Clone, Default)]
pub struct SearchResults {
    pub hits: Vec<Hit>,
    /// Chats searched as loaded in this run, and from the cache only.
    pub loaded_chats: usize,
    pub cached_chats: usize,
    /// The cache is still being read: more results may come
    /// (`Change::Search`).
    pub cache_reading: bool,
    /// Why the cache could not be read, if it could not.
    pub cache_error: String,
}

fn messages(snapshot: &clarp_core::json::Object) -> Vec<protocol::Message> {
    snapshot.get("turns").and_then(Value::as_array).into_iter().flatten().filter_map(Value::as_object).map(protocol::Message::from_json).collect()
}

impl Engine {
    /// The best `limit` messages holding every word of `query`, in any chat
    /// this client has loaded or cached that it still knows.
    pub fn search_messages(&mut self, query: &str, limit: usize) -> SearchResults {
        if self.search.base_url != self.base_url {
            let host = std::mem::take(&mut self.search.host);
            self.search = SearchState { base_url: self.base_url.clone(), dirty: self.conversations.keys().cloned().collect(), host, ..SearchState::default() };
        }
        for session in std::mem::take(&mut self.search.dirty) {
            match self.conversations.get(&session) {
                Some(conversation) if !conversation.is_empty() => {
                    self.search.index.update_chat(&session, conversation.rows(), Source::Memory);
                }
                Some(_) => {}
                None => self.search.index.remove_chat(&session),
            }
        }
        self.read_search_cache();
        let now = chrono::Utc::now().timestamp_millis();
        // More than asked: hits in chats this client no longer knows go.
        let hits = self
            .search
            .index
            .search(query, limit.saturating_mul(2).max(limit + 20), now)
            .into_iter()
            .filter(|hit| self.knows_chat(&hit.session))
            .take(limit)
            .collect();
        SearchResults {
            hits,
            loaded_chats: self.search.index.chats(Source::Memory),
            cached_chats: self.search.index.chats(Source::Cache),
            cache_reading: self.search.cache == CacheRead::Reading,
            cache_error: match &self.search.cache {
                CacheRead::Failed(error) => error.clone(),
                _ => String::new(),
            },
        }
    }

    /// A chat search can open: an agent (archived too), a room, or one
    /// loaded in this run.
    fn knows_chat(&self, session: &str) -> bool {
        self.roster.find(session).is_some() || self.archived.find(session).is_some() || self.room(session).is_some() || self.conversations.contains_key(session)
    }

    fn read_search_cache(&mut self) {
        if self.search.cache != CacheRead::NotStarted {
            return;
        }
        let Some(cache) = self.transcript_cache.clone() else {
            self.search.cache = CacheRead::Done;
            return;
        };
        self.search.cache = CacheRead::Reading;
        let (sender, wake, base) = (self.sender.clone(), self.wake.clone(), self.base_url.clone());
        self.runtime.spawn_blocking(move || {
            let chats = cache.all(&base).map(|chats| chats.into_iter().map(|(session, snapshot)| (session, messages(&snapshot))).collect());
            if sender.send(Message::SearchCache(chats)).is_ok() {
                wake();
            }
        });
    }

    pub(crate) fn search_cache_read(&mut self, chats: Result<Vec<(String, Vec<protocol::Message>)>, String>) {
        match chats {
            Ok(chats) => {
                for (session, rows) in chats {
                    self.search.index.update_chat(&session, &rows, Source::Cache);
                }
                self.search.cache = CacheRead::Done;
            }
            Err(error) => {
                eprintln!("clarp-engine: message search: transcript cache: {error}");
                self.search.cache = CacheRead::Failed(error);
            }
        }
        self.changes.push(Change::Search);
    }
}

/// What every Host searched found for the current words, merged.
#[derive(Debug, Clone, Default)]
pub struct HostResults {
    /// The words the Hosts were asked (trimmed).
    pub query: String,
    /// Newest first; a peer's sessions qualified as its chats are. Only
    /// down to the oldest hit every Host with more pages has passed, so a
    /// later page never lands above what shows.
    pub hits: Vec<HostHit>,
    /// A Host has another page (`search_host_more`).
    pub more: bool,
    /// A Host is still answering.
    pub pending: bool,
    /// At least one Host answered the words.
    pub answered: bool,
    /// Hosts still reading older history into their index (their names).
    pub partial: Vec<String>,
    /// "<Host>: why" for each Host whose search failed.
    pub errors: Vec<String>,
    /// Hosts read beside the primary that cannot search (their names).
    pub without: Vec<String>,
    /// How many Hosts were searched.
    pub hosts: usize,
}

impl Engine {
    /// `/server-info` answered: whether this Host searches messages itself.
    pub(crate) fn search_server_info(&mut self, info: &clarp_core::json::Object) {
        let offered = info
            .get("capabilities")
            .and_then(|c| c.get("features"))
            .and_then(Value::as_array)
            .is_some_and(|features| features.iter().any(|f| f.as_str() == Some(FEATURE)));
        if offered != self.search.host.offered {
            self.search.host = HostSearch { offered, ..HostSearch::default() };
            self.changes.push(Change::Search);
        }
    }

    /// The primary or a paired Host read beside it searches messages
    /// itself: message search asks the Hosts (`search_host`).
    pub fn host_search_offered(&self) -> bool {
        self.search.host.offered || self.peers.list.iter().any(|p| !p.server_id.is_empty() && p.engine.search.host.offered)
    }

    /// The Host holding `session` searches messages itself.
    pub fn host_search_offered_for(&self, session: &str) -> bool {
        match self.peer_ref(session) {
            Some(Some((engine, _))) => engine.search.host.offered,
            Some(None) => false,
            None => self.search.host.offered,
        }
    }

    /// Asks every Host that searches for `query`'s words, from the newest
    /// row; `session` (empty: every chat) keeps it to one chat, on that
    /// chat's Host alone. Empty words clear the results.
    pub fn search_host(&mut self, query: &str, session: &str) {
        let query = query.trim();
        let (scope, local) = match clarp_core::all_hosts::split(session) {
            Some((server_id, local)) => (Some(server_id.to_owned()), local.to_owned()),
            None if session.is_empty() => (None, String::new()),
            None => (Some(self.server_id.clone()), session.to_owned()),
        };
        let primary = scope.as_ref().is_none_or(|id| *id == self.server_id);
        self.start_host_search(query, if primary { &local } else { "" }, primary);
        for peer in &mut self.peers.list {
            let mine = scope.as_ref().is_none_or(|id| *id == peer.server_id) && !peer.server_id.is_empty();
            peer.engine.start_host_search(query, if mine { &local } else { "" }, mine);
        }
        self.changes.push(Change::Search);
    }

    /// The next page from every Host that has one.
    pub fn search_host_more(&mut self) {
        self.more_host_search();
        for peer in &mut self.peers.list {
            peer.engine.more_host_search();
        }
        self.changes.push(Change::Search);
    }

    fn start_host_search(&mut self, query: &str, session: &str, searched: bool) {
        let host = &mut self.search.host;
        host.generation += 1;
        host.query = query.to_owned();
        host.session = session.to_owned();
        host.hits.clear();
        host.next_before = None;
        host.partial = false;
        host.error.clear();
        let ask = host.offered && searched && !query.is_empty();
        host.pending = ask;
        // A Host left out of the search has nothing more to say.
        host.answered = !ask;
        if ask {
            self.ask_host_search(None);
        }
    }

    fn more_host_search(&mut self) {
        let host = &self.search.host;
        if host.pending || host.query.is_empty() {
            return;
        }
        if let Some(before) = host.next_before.clone() {
            self.search.host.pending = true;
            self.ask_host_search(Some(&before));
        }
    }

    fn ask_host_search(&self, before: Option<&str>) {
        let host = &self.search.host;
        let limit = HOST_PAGE.to_string();
        let mut query: Vec<(&str, &str)> = vec![("q", host.query.as_str()), ("limit", limit.as_str())];
        if !host.session.is_empty() {
            query.push(("session", host.session.as_str()));
        }
        if let Some(before) = before {
            query.push(("before", before));
        }
        self.api.get(&format!("msearch:{}", host.generation), "/search", &query);
    }

    /// The generation an answer of the current search carries.
    fn host_search_tag(&self, tag: &str) -> Option<bool> {
        let generation = tag.strip_prefix("msearch:")?;
        Some(generation.parse::<u64>().ok() == Some(self.search.host.generation))
    }

    pub(crate) fn host_search_json(&mut self, tag: &str, object: &clarp_core::json::Object) -> bool {
        match self.host_search_tag(tag) {
            None => return false,
            Some(false) => return true,
            Some(true) => {}
        }
        let page = HostPage::from_json(object);
        let host = &mut self.search.host;
        for hit in page.hits {
            if !host.hits.iter().any(|h| h.session == hit.session && h.message_id == hit.message_id) {
                host.hits.push(hit);
            }
        }
        host.next_before = page.next_before;
        host.partial = page.partial;
        host.pending = false;
        host.answered = true;
        host.error.clear();
        self.changes.push(Change::Search);
        true
    }

    pub(crate) fn host_search_failure(&mut self, tag: &str, message: &str, status: u16) -> bool {
        match self.host_search_tag(tag) {
            None => return false,
            Some(false) => return true,
            Some(true) => {}
        }
        let host = &mut self.search.host;
        host.pending = false;
        host.answered = true;
        host.error = match status {
            403 if message.contains("bound_conversation") || message.contains("bound") => "this chat's words are not searched for this device".to_owned(),
            0 => format!("could not be reached ({message})"),
            _ => message.to_owned(),
        };
        self.changes.push(Change::Search);
        true
    }

    /// What the Hosts found for the current words, merged newest first.
    pub fn host_search_results(&self) -> HostResults {
        let mut hosts: Vec<(String, String, &HostSearch)> = vec![(String::new(), self.primary_label(), &self.search.host)];
        for peer in &self.peers.list {
            if !peer.server_id.is_empty() {
                hosts.push((peer.server_id.clone(), peer.label.clone(), &peer.engine.search.host));
            }
        }
        let mut results = HostResults { query: self.search.host.query.clone(), ..HostResults::default() };
        // The oldest hit shown: none older than what a Host with more pages
        // has reached.
        let frontier = hosts
            .iter()
            .filter(|(_, _, h)| h.offered && h.next_before.is_some())
            .filter_map(|(_, _, h)| h.hits.iter().map(|hit| hit.epoch_ms).min())
            .max()
            .unwrap_or(i64::MIN);
        for (server_id, label, host) in &hosts {
            if !host.offered {
                if !server_id.is_empty() {
                    results.without.push(label.clone());
                }
                continue;
            }
            results.hosts += 1;
            results.more |= host.next_before.is_some();
            results.pending |= host.pending;
            results.answered |= host.answered && host.query == results.query;
            if host.partial {
                results.partial.push(label.clone());
            }
            if !host.error.is_empty() {
                results.errors.push(format!("{label}: {}", host.error));
            }
            for hit in host.hits.iter().filter(|hit| hit.epoch_ms >= frontier) {
                let mut hit = hit.clone();
                if !server_id.is_empty() {
                    hit.session = clarp_core::all_hosts::qualify(server_id, &hit.session);
                }
                results.hits.push(hit);
            }
        }
        results.hits.sort_by(|a, b| b.epoch_ms.cmp(&a.epoch_ms).then(b.revision.cmp(&a.revision)).then(a.session.cmp(&b.session)));
        results
    }
}
