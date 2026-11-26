//! One agent's publications, newest first: the images it published
//! (`GET /media`) and its artifacts (`GET /artifacts`), as one list. An
//! artifact that names a published file (`url` `/media/<id>` or `asset_id`)
//! and the file itself are one item. Both lists are read a page at a time;
//! the merged list shows an item only once neither list can still hold a
//! newer one, so paging older never puts something above what is shown.
//!
//! Files are only ever read through this engine's own Host connection (its
//! token, its origin), the agent's Host for a paired one: a peer's chat is
//! the peer engine's to load.

use std::collections::{HashMap, HashSet};

use clarp_core::json::{self, Object};
use clarp_core::update_feeds;
use serde_json::Value;

use crate::{Change, Engine};

/// Images asked for per page (`GET /media`).
pub const MEDIA_PAGE: usize = 60;
/// Artifacts asked for per page (`GET /artifacts`).
pub const ARTIFACT_PAGE: usize = 40;
/// Items a load (the first, or "older") wants to show, of the current kind.
pub const WANT_STEP: usize = 40;

/// What an item is, for the filter and for how it opens.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Default)]
pub enum Kind {
    Image,
    Video,
    Audio,
    /// Documents, research, reports, forms and files.
    Document,
    Data,
    /// Decisions and questions.
    Decision,
    #[default]
    Other,
}

impl Kind {
    /// The filter's order (after All).
    pub const ALL: [Kind; 7] = [Kind::Image, Kind::Video, Kind::Audio, Kind::Document, Kind::Data, Kind::Decision, Kind::Other];

    pub fn name(self) -> &'static str {
        match self {
            Kind::Image => "image",
            Kind::Video => "video",
            Kind::Audio => "audio",
            Kind::Document => "document",
            Kind::Data => "data",
            Kind::Decision => "decision",
            Kind::Other => "other",
        }
    }

    pub fn named(name: &str) -> Option<Kind> {
        Kind::ALL.into_iter().find(|kind| kind.name() == name)
    }

    /// The filter's label.
    pub fn label(self) -> &'static str {
        match self {
            Kind::Image => "Images",
            Kind::Video => "Videos",
            Kind::Audio => "Audio",
            Kind::Document => "Documents",
            Kind::Data => "Data",
            Kind::Decision => "Decisions",
            Kind::Other => "Other",
        }
    }
}

/// One item of the list. `key` stays the same across pages and reloads:
/// `media:<asset_id>` for anything backed by a published file, else
/// `artifact:<artifact_id>`.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct Publication {
    pub key: String,
    pub kind: Kind,
    pub title: String,
    /// The caption, or the artifact's summary.
    pub summary: String,
    /// The artifact's type ("" for a file published without one).
    pub artifact_type: String,
    pub mime_type: String,
    pub file_name: String,
    /// Milliseconds since the epoch.
    pub created_at: i64,
    /// The agent's session as this engine's Host knows it.
    pub session: String,
    pub artifact_id: String,
    pub asset_id: String,
    /// Where its Host serves the file (its own `/media/` path as the Host
    /// gave it: `/media/<id>`, or an older Host's `/media/files/<id>`);
    /// "" without a file.
    pub media_url: String,
    pub bytes: i64,
    /// A recording's or film's length as its artifact gives it (0: unknown).
    pub duration_ms: i64,
}

impl Publication {
    /// The Host path its file is read from, through the agent's own Host
    /// connection; None without a published file.
    pub fn media_path(&self) -> Option<String> {
        (!self.media_url.is_empty()).then(|| self.media_url.clone())
    }
}

/// The list's state for the view.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct State {
    /// The agent's list was asked for on this Host (false: not loaded, or
    /// its Host is not read now).
    pub known: bool,
    pub loading: bool,
    /// Older items may exist: false only once both lists said they ended.
    /// A list that failed has not ended.
    pub has_more: bool,
    /// Why the images' or the artifacts' next page could not be read ("":
    /// it could); "load older" asks again.
    pub media_error: String,
    pub artifacts_error: String,
    /// The filter (None: everything).
    pub kind: Option<Kind>,
    /// The agent the list is of (its `agent_id`; "" when the roster had
    /// none and the list is read by session).
    pub agent_id: String,
}

/// One of the two lists read from the Host.
#[derive(Default)]
struct Feed {
    rows: Vec<Value>,
    seen: HashSet<String>,
    /// The tag of the page in flight.
    pending: Option<String>,
    /// The Host said there is nothing older (a short page, no cursor, or
    /// no such list at all).
    done: bool,
    /// Why its last page failed; it is not asked again until the user
    /// asks for older ones (or reloads).
    error: String,
    /// `/media`'s `next_before`.
    cursor: String,
    /// The oldest `created_at` read, and how many rows have it (the
    /// artifacts' next page starts below it: `created_to` and `offset`).
    oldest: Option<i64>,
    at_oldest: usize,
}

impl Feed {
    /// The newest time it may still hold unread rows at: none once done.
    fn frontier(&self) -> Option<i64> {
        if self.done { None } else { Some(self.oldest.unwrap_or(i64::MAX)) }
    }
}

#[derive(Default)]
struct Book {
    generation: u64,
    agent_id: String,
    media: Feed,
    artifacts: Feed,
    /// The Host refused `representation=flat-v1`: ask in full.
    plain: bool,
    kind: Option<Kind>,
    /// How many items of `kind` the list should show before it stops
    /// reading older pages by itself.
    want: usize,
    items: Vec<Publication>,
}

#[derive(Default)]
pub(crate) struct Publications {
    books: HashMap<String, Book>,
    generation: u64,
    /// tag -> (session, generation)
    requests: HashMap<String, (String, u64)>,
    /// tag -> (session, asset id)
    images: HashMap<String, (String, String)>,
}

fn text<'a>(value: &'a Value, key: &str) -> &'a str {
    value.get(key).and_then(Value::as_str).unwrap_or_default()
}

fn number(value: &Value, key: &str) -> i64 {
    value.get(key).and_then(|v| v.as_i64().or_else(|| v.as_f64().map(|f| f as i64))).unwrap_or(0)
}

fn plain_id(id: &str) -> bool {
    !id.is_empty() && id.len() <= 180 && id.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'_' || b == b'-')
}

/// A path the Host gave for a published file, if it is one of its own
/// `/media/` paths: `/media/<id>`, or an older Host's `/media/files/<id>`.
/// Gives the asset id and the path (without query or fragment).
pub fn host_media_path(url: &str) -> Option<(String, String)> {
    let path = url.split(['?', '#']).next().unwrap_or_default();
    let rest = path.strip_prefix("/media/")?;
    let id = rest.strip_prefix("files/").unwrap_or(rest);
    plain_id(id).then(|| (id.to_owned(), path.to_owned()))
}

/// The canonical path of an asset on its Host.
fn canonical_media_path(asset_id: &str) -> String {
    format!("/media/{}", clarp_core::endpoint::percent_encode_segment(asset_id))
}

/// The published file an artifact stands for (its asset id): its `url` on
/// its Host's `/media/`, else its `asset_id`.
pub fn media_link(artifact: &Value) -> Option<String> {
    if let Some((id, _)) = host_media_path(text(artifact, "url")) {
        return Some(id);
    }
    let id = text(artifact, "asset_id");
    plain_id(id).then(|| id.to_owned())
}

/// Where the Host serves an asset: the row's own `/media/` path when it
/// names this asset, else the canonical one.
fn media_url_of(row: &Value, asset_id: &str) -> String {
    match host_media_path(text(row, "url")) {
        Some((id, path)) if id == asset_id => path,
        _ => canonical_media_path(asset_id),
    }
}

/// What kind an artifact is, by its type and (for a file) its MIME type.
pub fn kind_of(artifact_type: &str, mime_type: &str) -> Kind {
    let mime = mime_type.to_ascii_lowercase();
    match artifact_type {
        "video" => Kind::Video,
        "audio" => Kind::Audio,
        "image" | "image_gallery" => Kind::Image,
        "file" if mime.starts_with("image/") => Kind::Image,
        "file" if mime.starts_with("video/") => Kind::Video,
        "file" if mime.starts_with("audio/") => Kind::Audio,
        "document" | "research" | "html_form" | "file" => Kind::Document,
        "data" => Kind::Data,
        "decision" | "question" => Kind::Decision,
        "" if mime.starts_with("image/") => Kind::Image,
        "" if mime.starts_with("video/") => Kind::Video,
        "" if mime.starts_with("audio/") => Kind::Audio,
        "" => Kind::Document,
        _ => Kind::Other,
    }
}

fn from_media(row: &Value) -> Option<Publication> {
    let id = text(row, "asset_id");
    if id.is_empty() {
        return None;
    }
    let caption = text(row, "caption");
    let name = [text(row, "file_name"), text(row, "source_name")].into_iter().find(|n| !n.is_empty()).unwrap_or_default();
    let mime = text(row, "mime_type");
    Some(Publication {
        key: format!("media:{id}"),
        kind: kind_of("", mime),
        title: [caption, name].into_iter().find(|t| !t.is_empty()).unwrap_or("Image").to_owned(),
        summary: if caption.is_empty() { String::new() } else { name.to_owned() },
        artifact_type: String::new(),
        mime_type: mime.to_owned(),
        file_name: name.to_owned(),
        created_at: number(row, "created_at"),
        session: text(row, "session").to_owned(),
        artifact_id: String::new(),
        asset_id: id.to_owned(),
        media_url: media_url_of(row, id),
        bytes: number(row, "size_bytes").max(number(row, "bytes")),
        duration_ms: 0,
    })
}

fn from_artifact(row: &Value) -> Option<Publication> {
    let id = text(row, "artifact_id");
    if id.is_empty() {
        return None;
    }
    let kind_name = text(row, "type");
    let mime = text(row, "mime_type");
    let asset = media_link(row).unwrap_or_default();
    let file_name = text(row, "file_name");
    let title = [text(row, "title"), file_name].into_iter().find(|t| !t.is_empty()).unwrap_or("Artifact");
    Some(Publication {
        key: if asset.is_empty() { format!("artifact:{id}") } else { format!("media:{asset}") },
        kind: kind_of(kind_name, mime),
        title: title.to_owned(),
        summary: text(row, "summary").to_owned(),
        artifact_type: kind_name.to_owned(),
        mime_type: mime.to_owned(),
        file_name: file_name.to_owned(),
        created_at: number(row, "created_at"),
        session: text(row, "session").to_owned(),
        artifact_id: id.to_owned(),
        media_url: if asset.is_empty() { String::new() } else { media_url_of(row, &asset) },
        asset_id: asset,
        bytes: number(row, "size_bytes"),
        duration_ms: number(row, "duration_ms"),
    })
}

/// The two lists as one, newest first, without what an unread page could
/// still put above it: rows older than `horizon` wait. A file and the
/// artifact naming it are one item: the artifact's words, the file's kind
/// when the file is a listed image; of two artifacts naming one file the
/// newer stands.
pub fn merge(media: &[Value], artifacts: &[Value], horizon: Option<i64>) -> Vec<Publication> {
    let mut items: Vec<Publication> = Vec::new();
    let mut at: HashMap<String, usize> = HashMap::new();
    let shown = |item: &Publication| horizon.is_none_or(|h| item.created_at >= h);
    for item in media.iter().filter_map(from_media) {
        if at.contains_key(&item.key) {
            continue;
        }
        at.insert(item.key.clone(), items.len());
        items.push(item);
    }
    for item in artifacts.iter().filter_map(from_artifact) {
        match at.get(&item.key) {
            None => {
                at.insert(item.key.clone(), items.len());
                items.push(item);
            }
            Some(&index) => {
                let held = &mut items[index];
                if held.artifact_id.is_empty() {
                    // The listed file, now with the artifact's words.
                    let image = held.kind == Kind::Image;
                    let created_at = held.created_at.max(item.created_at);
                    let bytes = held.bytes.max(item.bytes);
                    let media_url = std::mem::take(&mut held.media_url);
                    *held = Publication { kind: if image { Kind::Image } else { item.kind }, created_at, bytes, media_url, ..item };
                } else if item.created_at > held.created_at {
                    let kind = held.kind;
                    *held = Publication { kind: if kind == Kind::Image { kind } else { item.kind }, ..item };
                }
            }
        }
    }
    items.retain(|item| shown(item));
    items.sort_by(|a, b| b.created_at.cmp(&a.created_at).then_with(|| b.key.cmp(&a.key)));
    items
}

/// A page to ask for: the list, and its query.
#[derive(Debug, Clone, PartialEq)]
struct Ask {
    media: bool,
    query: Vec<(String, String)>,
}

/// How a failed page is taken.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Failed {
    /// The Host does not know flat-v1: the page is asked again in full.
    Retry,
    /// This Host keeps no such list: it has nothing (more) of it.
    Absent,
    /// It stops there, with its error, until the user asks again.
    Stopped,
}

impl Book {
    /// Rows older than this wait for the other list (None: all show). A
    /// list that failed still holds it: what it has not read could be newer
    /// than what the other list read.
    fn horizon(&self) -> Option<i64> {
        [self.media.frontier(), self.artifacts.frontier()].into_iter().flatten().max()
    }

    fn rebuild(&mut self) {
        self.items = merge(&self.media.rows, &self.artifacts.rows, self.horizon());
    }

    fn of_kind(&self) -> usize {
        self.items.iter().filter(|item| self.kind.is_none_or(|kind| item.kind == kind)).count()
    }

    fn loading(&self) -> bool {
        self.media.pending.is_some() || self.artifacts.pending.is_some()
    }

    /// Both lists said they ended. Until then (a list failed, or a filter
    /// found nothing yet) older items may still come.
    fn exhausted(&self) -> bool {
        self.media.done && self.artifacts.done
    }

    /// The pages to ask for now: of each list that holds the horizon back
    /// and has not failed, while fewer than `want` items of the kind show.
    fn asks(&self, session: &str) -> Vec<Ask> {
        if self.loading() || self.exhausted() || self.of_kind() >= self.want {
            return Vec::new();
        }
        let horizon = self.horizon();
        let mut asks = Vec::new();
        let holds = |feed: &Feed| feed.error.is_empty() && feed.frontier().is_some() && feed.frontier() == horizon;
        if holds(&self.media) {
            let mut query = vec![("session".to_owned(), session.to_owned()), ("limit".to_owned(), MEDIA_PAGE.to_string())];
            if !self.media.cursor.is_empty() {
                query.push(("before".into(), self.media.cursor.clone()));
            }
            asks.push(Ask { media: true, query });
        }
        if holds(&self.artifacts) {
            let mut query = vec![("order".to_owned(), "created".to_owned()), ("limit".to_owned(), ARTIFACT_PAGE.to_string())];
            if self.agent_id.is_empty() {
                query.push(("session".into(), session.to_owned()));
            } else {
                query.push(("agent_id".into(), self.agent_id.clone()));
            }
            // Below the oldest read: rows at that very time already read
            // are skipped by count (the Host orders ties by id).
            if let Some(oldest) = self.artifacts.oldest {
                query.push(("created_to".into(), (oldest + 1).to_string()));
                query.push(("offset".into(), self.artifacts.at_oldest.to_string()));
            }
            if !self.plain {
                query.push(("representation".into(), update_feeds::FLAT_REPRESENTATION.into()));
            }
            asks.push(Ask { media: false, query });
        }
        asks
    }

    /// A page arrived for one list.
    fn apply(&mut self, media: bool, object: &Object) {
        let feed = if media { &mut self.media } else { &mut self.artifacts };
        feed.pending = None;
        feed.error.clear();
        let rows = json::array(object, if media { "assets" } else { "artifacts" });
        let asked = if media { MEDIA_PAGE } else { ARTIFACT_PAGE };
        let id_key = if media { "asset_id" } else { "artifact_id" };
        let count = rows.len();
        let mut fresh = 0;
        for row in rows {
            let id = text(&row, id_key).to_owned();
            if id.is_empty() || !feed.seen.insert(id) {
                continue;
            }
            let at = number(&row, "created_at");
            match feed.oldest {
                Some(oldest) if at > oldest => {}
                Some(oldest) if at == oldest => feed.at_oldest += 1,
                _ => {
                    feed.oldest = Some(at);
                    feed.at_oldest = 1;
                }
            }
            fresh += 1;
            feed.rows.push(row);
        }
        if media {
            feed.cursor = json::string(object, "next_before");
            // Without a cursor there is no older page (an older Host may
            // not page at all: one page is all it has).
            feed.done = feed.cursor.is_empty() || count == 0;
        } else {
            // A short page is the last; a full one that brought nothing
            // new could only repeat itself.
            feed.done = count < asked || fresh == 0;
        }
        self.rebuild();
    }

    /// A page of one list failed.
    fn fail(&mut self, media: bool, message: &str, status: u16, detail: &str) -> Failed {
        let unsupported = !media && !self.plain && update_feeds::is_unsupported_representation(status, message);
        let feed = if media { &mut self.media } else { &mut self.artifacts };
        feed.pending = None;
        let failed = if unsupported {
            Failed::Retry
        } else if status == 404 {
            feed.done = true;
            Failed::Absent
        } else {
            feed.error = detail.to_owned();
            Failed::Stopped
        };
        if failed == Failed::Retry {
            self.plain = true;
        }
        self.rebuild();
        failed
    }

    /// The user asks for more: failed lists are asked again.
    fn retry(&mut self) {
        self.media.error.clear();
        self.artifacts.error.clear();
    }

    fn state(&self) -> State {
        State {
            known: true,
            loading: self.loading(),
            has_more: !self.exhausted(),
            media_error: self.media.error.clone(),
            artifacts_error: self.artifacts.error.clone(),
            kind: self.kind,
            agent_id: self.agent_id.clone(),
        }
    }
}

impl Engine {
    // ---- queries ---------------------------------------------------------

    /// The agent's publications, newest first (the filter is the view's).
    pub fn publications(&self, session: &str) -> &[Publication] {
        on_peer!(self.peer_ref(session), |engine, local| engine.publications(local));
        self.publications.books.get(session).map_or(&[][..], |book| book.items.as_slice())
    }

    pub fn publications_state(&self, session: &str) -> State {
        on_peer!(self.peer_ref(session), |engine, local| engine.publications_state(local));
        self.publications.books.get(session).map_or_else(State::default, Book::state)
    }

    /// An item's cached image (`file://`); None until it arrives, and for
    /// anything but an image.
    pub fn publication_image(&self, session: &str, asset_id: &str) -> Option<&str> {
        on_peer!(self.peer_ref(session), |engine, local| engine.publication_image(local, asset_id));
        self.media_source(asset_id)
    }

    /// Any listed agent's publication artifact by its id (for viewers that
    /// know only the id); peers' rows name their qualified session.
    pub fn held_publication_artifact(&self, artifact_id: &str) -> Option<Value> {
        let here = self.publications.books.values().flat_map(|book| book.artifacts.rows.iter()).find(|row| text(row, "artifact_id") == artifact_id);
        if let Some(row) = here {
            return Some(row.clone());
        }
        self.peers_publication_artifact(artifact_id)
    }

    /// Whether this engine holds a publication artifact (a peer's owner).
    pub(crate) fn holds_publication_artifact(&self, artifact_id: &str) -> bool {
        !artifact_id.is_empty() && self.publications.books.values().any(|book| book.artifacts.rows.iter().any(|row| text(row, "artifact_id") == artifact_id))
    }

    // ---- commands --------------------------------------------------------

    /// Reads the agent's newest publications again, from the top, as the
    /// agent the roster has under `session` now.
    pub fn load_publications(&mut self, session: &str) {
        on_peer!(self.peer_mut(session), |engine, local| engine.load_publications(local));
        if session.is_empty() {
            return;
        }
        let agent_id = self.roster.find(session).map(|agent| agent.agent_id.clone()).unwrap_or_default();
        self.publications.generation += 1;
        let previous = self.publications.books.remove(session);
        // The same agent keeps its filter; another starts on everything.
        let same = previous.as_ref().is_some_and(|b| b.agent_id == agent_id);
        let book = Book {
            generation: self.publications.generation,
            agent_id,
            plain: previous.as_ref().is_some_and(|b| b.plain),
            kind: previous.as_ref().filter(|_| same).and_then(|b| b.kind),
            want: WANT_STEP,
            ..Book::default()
        };
        self.publications.requests.retain(|_, (s, _)| s != session);
        self.publications.books.insert(session.to_owned(), book);
        self.request_publications(session);
        self.changes.push(Change::Publications);
    }

    /// Loads the list when this Host has none for `session` (a peer read
    /// anew, a reconnect) or holds another agent's under it (the roster now
    /// names another agent for the session). True when it loaded.
    pub fn ensure_publications(&mut self, session: &str) -> bool {
        on_peer!(self.peer_mut(session), |engine, local| engine.ensure_publications(local));
        if session.is_empty() {
            return false;
        }
        let agent_id = self.roster.find(session).map(|agent| agent.agent_id.clone()).unwrap_or_default();
        let stale = match self.publications.books.get(session) {
            None => true,
            // A list read by session before the roster knew the agent stays
            // the agent's: the session named it then.
            Some(book) => !agent_id.is_empty() && !book.agent_id.is_empty() && book.agent_id != agent_id,
        };
        if stale {
            self.load_publications(session);
        }
        stale
    }

    /// Shows `WANT_STEP` more of the current kind, reading older pages
    /// until it has them or both lists end; a list that failed is asked
    /// again.
    pub fn load_older_publications(&mut self, session: &str) {
        on_peer!(self.peer_mut(session), |engine, local| engine.load_older_publications(local));
        let Some(book) = self.publications.books.get_mut(session) else { return };
        book.retry();
        if book.exhausted() {
            self.changes.push(Change::Publications);
            return;
        }
        book.want = book.of_kind() + WANT_STEP;
        self.request_publications(session);
        self.changes.push(Change::Publications);
    }

    /// Shows one kind (None: all); older pages are read until enough of it
    /// shows or both lists end.
    pub fn set_publication_kind(&mut self, session: &str, kind: Option<Kind>) {
        on_peer!(self.peer_mut(session), |engine, local| engine.set_publication_kind(local, kind));
        let Some(book) = self.publications.books.get_mut(session) else { return };
        if book.kind == kind {
            return;
        }
        book.kind = kind;
        book.want = WANT_STEP;
        self.request_publications(session);
        self.changes.push(Change::Publications);
    }

    fn request_publications(&mut self, session: &str) {
        let Some(book) = self.publications.books.get_mut(session) else { return };
        let generation = book.generation;
        let asks = book.asks(session);
        let mut sends = Vec::new();
        for ask in asks {
            let (prefix, path) = if ask.media { ("publications-media", "/media") } else { ("publications-artifacts", "/artifacts") };
            let tag = format!("{prefix}:{}", uuid::Uuid::new_v4().simple());
            let feed = if ask.media { &mut book.media } else { &mut book.artifacts };
            feed.pending = Some(tag.clone());
            sends.push((tag, path, ask.query));
        }
        for (tag, path, query) in sends {
            self.publications.requests.insert(tag.clone(), (session.to_owned(), generation));
            let pairs: Vec<(&str, &str)> = query.iter().map(|(k, v)| (k.as_str(), v.as_str())).collect();
            self.api.get(&tag, path, &pairs);
        }
    }

    /// Fetches the images that show and are not cached yet.
    fn fetch_publication_images(&mut self, session: &str) {
        let Some(book) = self.publications.books.get(session) else { return };
        let wanted: Vec<(String, String)> = book
            .items
            .iter()
            .filter(|item| item.kind == Kind::Image && !item.asset_id.is_empty() && !item.media_url.is_empty())
            .filter(|item| !self.media_cached(&item.asset_id))
            .map(|item| (item.asset_id.clone(), item.media_url.clone()))
            .collect();
        for (asset, path) in wanted {
            if self.publications.images.values().any(|(_, id)| *id == asset) {
                continue;
            }
            let tag = format!("publications-image:{}", uuid::Uuid::new_v4().simple());
            self.publications.images.insert(tag.clone(), (session.to_owned(), asset));
            // Only ever this engine's own Host: get_bytes refuses another origin.
            self.api.get_bytes(&tag, &path);
        }
    }

    // ---- replies ---------------------------------------------------------

    /// The book a page's tag is still current for, and which list it is.
    fn page_book(&mut self, tag: &str) -> Option<(String, bool)> {
        let (session, generation) = self.publications.requests.remove(tag)?;
        let book = self.publications.books.get(&session).filter(|b| b.generation == generation)?;
        let media = tag.starts_with("publications-media:");
        let feed = if media { &book.media } else { &book.artifacts };
        (feed.pending.as_deref() == Some(tag)).then_some((session, media))
    }

    fn publications_image(&mut self, tag: &str, bytes: &[u8], content_type: &str) {
        let Some((session, asset)) = self.publications.images.remove(tag) else { return };
        if !self.publications.books.contains_key(&session) || !clarp_core::media::mime(content_type).starts_with("image/") {
            return;
        }
        if self.cache_media_file(&asset, bytes) {
            self.changes.push(Change::Publications);
        }
    }

    pub(crate) fn publications_bytes(&mut self, tag: &str, bytes: &[u8], content_type: &str) -> bool {
        if !tag.starts_with("publications-image:") {
            return false;
        }
        self.publications_image(tag, bytes, content_type);
        true
    }

    pub(crate) fn publications_json(&mut self, tag: &str, object: &Object) -> bool {
        if !tag.starts_with("publications-media:") && !tag.starts_with("publications-artifacts:") {
            return false;
        }
        let Some((session, media)) = self.page_book(tag) else { return true };
        if let Some(book) = self.publications.books.get_mut(&session) {
            book.apply(media, object);
        }
        self.changes.push(Change::Publications);
        self.fetch_publication_images(&session);
        self.request_publications(&session);
        true
    }

    pub(crate) fn publications_failure(&mut self, tag: &str, message: &str, status: u16, detail: &str) -> bool {
        if tag.starts_with("publications-image:") {
            self.publications.images.remove(tag);
            eprintln!("Engine: {tag} failed: {detail}");
            return true;
        }
        if !tag.starts_with("publications-media:") && !tag.starts_with("publications-artifacts:") {
            return false;
        }
        let Some((session, media)) = self.page_book(tag) else { return true };
        if let Some(book) = self.publications.books.get_mut(&session) {
            let failed = book.fail(media, message, status, detail);
            if failed == Failed::Stopped {
                eprintln!("Engine: {tag} failed: {detail}");
            }
        }
        self.changes.push(Change::Publications);
        self.request_publications(&session);
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn image(id: &str, at: i64) -> Value {
        json!({"asset_id": id, "mime_type": "image/png", "caption": format!("pic {id}"), "created_at": at, "session": "s"})
    }

    fn artifact(id: &str, kind: &str, at: i64, extra: Value) -> Value {
        let mut row = json!({"artifact_id": id, "type": kind, "title": format!("t {id}"), "created_at": at, "session": "s"});
        if let (Some(row), Some(extra)) = (row.as_object_mut(), extra.as_object()) {
            row.extend(extra.clone());
        }
        row
    }

    #[test]
    fn a_file_and_its_artifact_are_one_item_under_the_files_key() {
        let media = [image("a1", 10)];
        let artifacts = [artifact("x", "file", 12, json!({"url": "/media/a1", "mime_type": "image/png", "file_name": "p.png"}))];
        let items = merge(&media, &artifacts, None);
        assert_eq!(items.len(), 1);
        assert_eq!(items[0].key, "media:a1");
        assert_eq!(items[0].kind, Kind::Image);
        assert_eq!(items[0].artifact_id, "x");
        assert_eq!(items[0].title, "t x");
    }

    #[test]
    fn two_artifacts_naming_one_recording_keep_the_newer() {
        let artifacts = [
            artifact("old", "audio", 5, json!({"url": "/media/r", "mime_type": "audio/mpeg"})),
            artifact("new", "audio", 9, json!({"url": "/media/r", "mime_type": "audio/mpeg"})),
        ];
        let items = merge(&[], &artifacts, None);
        assert_eq!(items.len(), 1);
        assert_eq!(items[0].artifact_id, "new");
        assert_eq!(items[0].kind, Kind::Audio);
    }

    #[test]
    fn newest_first_and_nothing_below_the_horizon() {
        let media = [image("a", 30), image("b", 10)];
        let artifacts = [artifact("d", "document", 20, json!({"content": "x"}))];
        let all = merge(&media, &artifacts, None);
        assert_eq!(all.iter().map(|i| i.key.as_str()).collect::<Vec<_>>(), ["media:a", "artifact:d", "media:b"]);
        let held = merge(&media, &artifacts, Some(20));
        assert_eq!(held.iter().map(|i| i.key.as_str()).collect::<Vec<_>>(), ["media:a", "artifact:d"]);
    }

    #[test]
    fn kinds_follow_type_then_mime() {
        assert_eq!(kind_of("file", "application/pdf"), Kind::Document);
        assert_eq!(kind_of("file", "video/mp4"), Kind::Video);
        assert_eq!(kind_of("research", ""), Kind::Document);
        assert_eq!(kind_of("question", ""), Kind::Decision);
        assert_eq!(kind_of("plan", ""), Kind::Other);
        assert_eq!(kind_of("", "image/jpeg"), Kind::Image);
    }

    #[test]
    fn media_links_are_this_hosts_media_only() {
        assert_eq!(media_link(&json!({"url": "/media/abc-1"})).as_deref(), Some("abc-1"));
        assert_eq!(media_link(&json!({"asset_id": "z9"})).as_deref(), Some("z9"));
        assert_eq!(media_link(&json!({"url": "https://example.com/media/abc"})), None);
        assert_eq!(media_link(&json!({"url": "/media/../x"})), None);
        assert_eq!(media_link(&json!({"url": "/media/files/m1"})).as_deref(), Some("m1"), "an older Host's path");
        assert_eq!(media_link(&json!({"url": "//evil.example/media/x"})), None);
    }

    #[test]
    fn a_file_is_read_where_its_host_said_and_is_one_item_either_way() {
        let media = [json!({"asset_id": "m1", "mime_type": "image/png", "url": "/media/files/m1", "created_at": 5})];
        let artifacts = [artifact("x", "file", 9, json!({"url": "/media/m1", "mime_type": "image/png"}))];
        let items = merge(&media, &artifacts, None);
        assert_eq!(items.len(), 1);
        assert_eq!(items[0].media_path().as_deref(), Some("/media/files/m1"), "the listed file's own path stays");
        let alone = merge(&[], &[artifact("v", "video", 3, json!({"url": "/media/files/v1?x=1", "mime_type": "video/mp4"}))], None);
        assert_eq!(alone[0].key, "media:v1");
        assert_eq!(alone[0].media_path().as_deref(), Some("/media/files/v1"));
        let bare = merge(&[json!({"asset_id": "z", "mime_type": "image/png", "created_at": 1})], &[], None);
        assert_eq!(bare[0].media_path().as_deref(), Some("/media/z"));
    }

    #[test]
    fn the_horizon_is_the_newest_unfinished_frontier() {
        let mut book = Book::default();
        assert_eq!(book.horizon(), Some(i64::MAX), "nothing shows before both lists answered");
        book.media.oldest = Some(50);
        book.artifacts.oldest = Some(80);
        assert_eq!(book.horizon(), Some(80));
        book.artifacts.done = true;
        assert_eq!(book.horizon(), Some(50));
        book.media.done = true;
        assert_eq!(book.horizon(), None);
        assert!(book.exhausted());
    }

    fn page(key: &str, rows: Vec<Value>, cursor: &str) -> Object {
        let mut object = Object::new();
        object.insert(key.into(), Value::Array(rows));
        object.insert("next_before".into(), json!(cursor));
        object
    }

    fn book(kind: Option<Kind>) -> Book {
        Book { agent_id: "agent-1".into(), kind, want: WANT_STEP, ..Book::default() }
    }

    fn query(ask: &Ask, key: &str) -> Option<String> {
        ask.query.iter().find(|(k, _)| k == key).map(|(_, v)| v.clone())
    }

    #[test]
    fn a_listed_image_takes_a_newer_artifacts_words_under_the_same_key() {
        let mut book = book(None);
        book.apply(true, &page("assets", vec![image("a1", 10)], ""));
        let docs: Vec<Value> = (0..ARTIFACT_PAGE as i64).map(|i| artifact(&format!("d{i}"), "document", 100 - i, json!({}))).collect();
        book.apply(false, &page("artifacts", docs, ""));
        assert!(book.items.iter().all(|i| i.key != "media:a1"), "the image waits below the artifacts' horizon");
        let older = vec![artifact("f", "file", 20, json!({"url": "/media/a1", "mime_type": "image/png", "file_name": "a1.png"}))];
        book.apply(false, &page("artifacts", older, ""));
        let shown: Vec<&Publication> = book.items.iter().filter(|i| i.key == "media:a1").collect();
        assert_eq!(shown.len(), 1, "one item for the image and the artifact naming it");
        assert_eq!(shown[0].artifact_id, "f");
        assert_eq!(shown[0].title, "t f");
        assert_eq!(shown[0].kind, Kind::Image);
        assert_eq!(shown[0].created_at, 20, "it sorts by the newer of the two");
        assert!(book.exhausted());
    }

    #[test]
    fn the_artifacts_next_page_starts_below_the_oldest_read() {
        let mut book = book(None);
        book.media.done = true;
        let rows: Vec<Value> = (0..ARTIFACT_PAGE as i64).map(|i| artifact(&format!("x{i}"), "data", if i < 37 { 100 - i } else { 60 }, json!({}))).collect();
        book.apply(false, &page("artifacts", rows, ""));
        book.want = book.of_kind() + WANT_STEP;
        let asks = book.asks("s");
        assert_eq!(asks.len(), 1);
        assert_eq!(query(&asks[0], "created_to").as_deref(), Some("61"));
        assert_eq!(query(&asks[0], "offset").as_deref(), Some("3"), "the three at the oldest time are skipped");
        assert_eq!(query(&asks[0], "agent_id").as_deref(), Some("agent-1"));
        assert_eq!(query(&asks[0], "session"), None);
    }

    #[test]
    fn a_filter_with_nothing_yet_keeps_reading_until_both_lists_end() {
        let mut book = book(Some(Kind::Image));
        book.apply(true, &page("assets", vec![], ""));
        assert!(book.media.done);
        let docs: Vec<Value> = (0..ARTIFACT_PAGE as i64).map(|i| artifact(&format!("d{i}"), "document", 100 - i, json!({}))).collect();
        book.apply(false, &page("artifacts", docs, ""));
        assert_eq!(book.of_kind(), 0);
        assert!(book.state().has_more, "no image yet is not the end while the artifacts go on");
        let asks = book.asks("s");
        assert_eq!(asks.len(), 1, "the artifacts are read on by themselves");
        assert!(!asks[0].media);
        book.apply(false, &page("artifacts", vec![artifact("v", "video", 5, json!({"url": "/media/v1", "mime_type": "video/mp4"}))], ""));
        assert!(book.exhausted());
        assert!(!book.state().has_more);
        assert!(book.asks("s").is_empty());
    }

    #[test]
    fn a_failed_list_is_not_the_end_and_holds_back_what_is_older() {
        let mut book = book(None);
        book.media.pending = Some("m".into());
        book.artifacts.pending = Some("a".into());
        book.apply(true, &page("assets", vec![image("a", 50), image("b", 5)], "c1"));
        assert_eq!(book.fail(false, "HTTP 500", 500, "Host error (HTTP 500)"), Failed::Stopped);
        let state = book.state();
        assert!(state.has_more);
        assert_eq!(state.artifacts_error, "Host error (HTTP 500)");
        assert_eq!(state.media_error, "");
        assert!(book.items.is_empty(), "nothing shows above what the failed list may still hold");
        assert!(book.asks("s").iter().all(|a| a.media), "the failed list is not asked again by itself");
        book.retry();
        assert!(book.asks("s").iter().any(|a| !a.media), "asking for older ones asks it again");
    }

    #[test]
    fn an_old_host_without_flat_rows_is_asked_in_full_and_one_without_media_has_none() {
        let mut book = book(None);
        assert_eq!(book.fail(false, "unsupported artifact representation", 400, "x"), Failed::Retry);
        assert!(book.plain);
        assert!(book.asks("s").iter().any(|a| !a.media && query(a, "representation").is_none()));
        assert_eq!(book.fail(true, "not found", 404, "x"), Failed::Absent);
        assert!(book.media.done);
        assert_eq!(book.state().media_error, "");
    }
}
