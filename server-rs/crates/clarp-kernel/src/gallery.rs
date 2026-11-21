//! The gallery's mutable record and its ordered views, independent of the store and clock.

use std::collections::{BTreeSet, HashMap};
use std::ops::Bound::{Excluded, Included, Unbounded};

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use crate::media::{Asset, is_image};

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Deletion {
    pub at: i64,
    pub by: String,
}

/// The current record for one published ID. The agent's log still owns its publication history.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Image {
    pub asset: Asset,
    pub agent_id: String,
    #[serde(default)]
    pub favorite_at: Option<i64>,
    #[serde(default)]
    pub deletion: Option<Deletion>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Refusal {
    Unknown,
    NotImage,
    Removed(i64),
    BadBefore,
    BadFavorite,
    BadIds,
    BadSince,
    BadLimit,
}

impl Refusal {
    pub fn words(self) -> &'static str {
        match self {
            Self::Unknown => "not_found",
            Self::NotImage => "not_an_image",
            Self::Removed(_) => "removed",
            Self::BadBefore => "bad before",
            Self::BadFavorite => "favorite must be true or false",
            Self::BadIds => "asset_ids must list 1 to 200 asset ids",
            Self::BadSince => "since must be a time in ms",
            Self::BadLimit => "limit must be a number from 1 to 1000",
        }
    }
}

impl Image {
    pub fn new(asset: Asset, agent_id: String) -> Self {
        Self {
            asset,
            agent_id,
            favorite_at: None,
            deletion: None,
        }
    }

    pub fn row(&self, used_as: Vec<Value>) -> Value {
        let mut row = self.asset.public();
        row["favorite"] = json!(self.favorite_at.is_some());
        row["favorite_at"] = json!(self.favorite_at);
        row["used_as"] = json!(used_as);
        row
    }

    pub fn favorite(&self, wanted: bool, at: i64) -> Result<Option<Self>, Refusal> {
        self.writable()?;
        if let Some(deletion) = &self.deletion {
            return Err(Refusal::Removed(deletion.at));
        }
        if self.favorite_at.is_some() == wanted {
            return Ok(None);
        }
        let mut updated = self.clone();
        updated.favorite_at = wanted.then_some(at);
        Ok(Some(updated))
    }

    pub fn delete(&self, by: &str, at: i64) -> Result<Option<Self>, Refusal> {
        self.writable()?;
        if self.deletion.is_some() {
            return Ok(None);
        }
        let mut updated = self.clone();
        updated.deletion = Some(Deletion {
            at,
            by: by.chars().take(120).collect(),
        });
        Ok(Some(updated))
    }

    fn writable(&self) -> Result<(), Refusal> {
        if is_image(&self.asset.mime_type) {
            Ok(())
        } else {
            Err(Refusal::NotImage)
        }
    }
}

pub fn wanted(body: &Value) -> Result<bool, Refusal> {
    body["favorite"].as_bool().ok_or(Refusal::BadFavorite)
}

pub fn ids(body: &Value) -> Result<Vec<&str>, Refusal> {
    let ids = body["asset_ids"]
        .as_array()
        .filter(|ids| (1..=200).contains(&ids.len()))
        .ok_or(Refusal::BadIds)?;
    ids.iter()
        .map(|id| {
            id.as_str()
                .filter(|id| !id.is_empty())
                .ok_or(Refusal::BadIds)
        })
        .collect()
}

pub fn deletion_query(since: &str, limit: Option<&str>) -> Result<(i64, usize), Refusal> {
    let since = since
        .trim()
        .parse::<i64>()
        .ok()
        .filter(|since| *since >= 0)
        .ok_or(Refusal::BadSince)?;
    let limit = match limit {
        None => 1000,
        Some(raw) => crate::python::int(crate::python::strip(raw))
            .filter(|limit| (1..=1000).contains(limit))
            .ok_or(Refusal::BadLimit)?,
    };
    Ok((
        since,
        usize::try_from(limit).map_err(|_| Refusal::BadLimit)?,
    ))
}

type Key = (i64, String);
type Order = BTreeSet<Key>;

fn tag(favorites: bool) -> char {
    if favorites { 'f' } else { 'c' }
}

fn cursor(before: &str, favorites: bool) -> Result<Key, Refusal> {
    let (at, id) = before
        .trim()
        .strip_prefix(tag(favorites))
        .and_then(|s| s.split_once('.'))
        .ok_or(Refusal::BadBefore)?;
    let stamp = at
        .parse::<i64>()
        .ok()
        .filter(|_| at.bytes().all(|b| b.is_ascii_digit()))
        .ok_or(Refusal::BadBefore)?;
    if id.is_empty()
        || id.chars().count() > 180
        || !id
            .chars()
            .all(|c| c.is_alphanumeric() || c == '_' || c == '-')
    {
        return Err(Refusal::BadBefore);
    }
    Ok((stamp, id.to_owned()))
}

#[derive(Debug, Default)]
struct Session {
    images: Order,
    favorites: Order,
    deleted: Order,
}

#[derive(Debug, Default)]
pub struct Index {
    images: HashMap<String, Image>,
    sessions: HashMap<String, Session>,
    deleted: Order,
    live_blobs: HashMap<String, usize>,
}

pub struct Page<'a> {
    pub rows: Vec<&'a Image>,
    pub next_before: Option<String>,
    pub total: usize,
    pub favorites_total: usize,
}

#[derive(Serialize)]
pub struct Tombstone<'a> {
    pub asset_id: &'a str,
    pub session: &'a str,
    pub deleted_at: i64,
}

#[derive(Serialize)]
pub struct DeletedPage<'a> {
    pub deleted: Vec<Tombstone<'a>>,
    pub next_since: Option<i64>,
    pub server_now: i64,
}

impl Index {
    pub fn get(&self, id: &str) -> Option<&Image> {
        self.images.get(id)
    }

    pub fn holds_blob(&self, name: &str) -> bool {
        self.live_blobs.contains_key(name)
    }

    /// Call only after the store accepted the row. Replacing one row updates only its own keys.
    pub fn apply(&mut self, image: Image) {
        let id = image.asset.asset_id.clone();
        if let Some(previous) = self.images.remove(&id) {
            self.forget(&previous);
        }
        let session = self
            .sessions
            .entry(image.asset.session.clone())
            .or_default();
        let is_gallery = !image.asset.named_only && is_image(&image.asset.mime_type);
        if let Some(deletion) = &image.deletion {
            self.deleted.insert((deletion.at, id.clone()));
            session.deleted.insert((deletion.at, id.clone()));
        } else {
            *self.live_blobs.entry(image.asset.blob()).or_default() += 1;
            if is_gallery {
                session.images.insert((image.asset.created_at, id.clone()));
                if let Some(at) = image.favorite_at {
                    session.favorites.insert((at, id.clone()));
                }
            }
        }
        self.images.insert(id, image);
    }

    fn forget(&mut self, image: &Image) {
        let id = &image.asset.asset_id;
        let Some(session) = self.sessions.get_mut(&image.asset.session) else {
            return;
        };
        if let Some(deletion) = &image.deletion {
            self.deleted.remove(&(deletion.at, id.clone()));
            session.deleted.remove(&(deletion.at, id.clone()));
        } else {
            session.images.remove(&(image.asset.created_at, id.clone()));
            if let Some(at) = image.favorite_at {
                session.favorites.remove(&(at, id.clone()));
            }
            let name = image.asset.blob();
            if let Some(count) = self.live_blobs.get_mut(&name) {
                *count -= 1;
                if *count == 0 {
                    self.live_blobs.remove(&name);
                }
            }
        }
    }

    pub fn page(
        &self,
        session: &str,
        favorites: bool,
        before: &str,
        limit: usize,
    ) -> Result<Page<'_>, Refusal> {
        let before = (!before.is_empty())
            .then(|| cursor(before, favorites))
            .transpose()?;
        let empty = Session::default();
        let session = self.sessions.get(session).unwrap_or(&empty);
        let order = if favorites {
            &session.favorites
        } else {
            &session.images
        };
        let end = before.map_or(Unbounded, Excluded);
        let mut range = order.range((Unbounded, end)).rev();
        let limit = limit.clamp(1, 500);
        let rows: Vec<_> = range
            .by_ref()
            .take(limit)
            .filter_map(|(_, id)| self.images.get(id))
            .collect();
        let next_before = rows.last().and_then(|last| {
            range.next()?;
            let at = if favorites {
                last.favorite_at?
            } else {
                last.asset.created_at
            };
            Some(format!("{}{at}.{}", tag(favorites), last.asset.asset_id))
        });
        Ok(Page {
            rows,
            next_before,
            total: session.images.len(),
            favorites_total: session.favorites.len(),
        })
    }

    pub fn deleted(&self, session: &str, since: i64, limit: usize, now: i64) -> DeletedPage<'_> {
        let mut page = DeletedPage {
            deleted: Vec::new(),
            next_since: None,
            server_now: now.saturating_sub(1),
        };
        let order = if session.is_empty() {
            &self.deleted
        } else if let Some(session) = self.sessions.get(session) {
            &session.deleted
        } else {
            return page;
        };
        let Some(first) = since.checked_add(1) else {
            return page;
        };
        let limit = limit.clamp(1, 1000);
        let mut boundary = None;
        for (at, id) in order.range((Included((first, String::new())), Unbounded)) {
            if boundary.is_some_and(|last| last != *at) {
                page.next_since = boundary;
                break;
            }
            if let Some(image) = self.images.get(id) {
                page.deleted.push(Tombstone {
                    asset_id: id,
                    session: &image.asset.session,
                    deleted_at: *at,
                });
                if page.deleted.len() == limit {
                    boundary = Some(*at);
                }
            }
        }
        page
    }
}
