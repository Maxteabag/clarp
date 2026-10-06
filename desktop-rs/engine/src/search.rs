//! Message search over what this client holds (the Host has no search):
//! the chats loaded in this run, indexed again only where their rows
//! changed, and every chat in the transcript cache, read once off the UI
//! thread when the first search asks.

use std::collections::HashSet;

use clarp_core::message_search::{Hit, SearchIndex, Source};
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
            self.search = SearchState { base_url: self.base_url.clone(), dirty: self.conversations.keys().cloned().collect(), ..SearchState::default() };
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
