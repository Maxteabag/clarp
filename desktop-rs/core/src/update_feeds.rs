//! How the Updates lists (attention, background jobs, artifacts) follow the
//! Host without refetching on every event: a minimum interval between
//! event-driven reloads with one trailing reload, and an artifact list kept
//! current one item at a time from `artifact-updated` events.
//!
//! On 2026-10-13 a desktop refetched the 50-artifact list (median 5.9 MB
//! gzipped) on every artifact and attention event: 492 times in two days.

use std::time::{Duration, Instant};

use serde_json::Value;

/// The list the Updates surface asks for: the 50 most recently updated.
pub const ARTIFACT_LIST_LIMIT: usize = 50;

/// Types the Host's list leaves out (`artifacts.list_artifacts`): an item of
/// one of these never joins the list from an event.
pub const UNLISTED_TYPES: &[&str] = &["image", "image_gallery", "live_task", "event"];

/// Without the nested `payload`, which repeats every top-level field (the
/// form's whole document twice). Host contract 18, feature
/// `flat_artifact_responses`; older Hosts ignore it and answer in full.
pub const FLAT_REPRESENTATION: &str = "flat-v1";

/// The least time between two event-driven reloads of each list. A user's
/// action reloads at once. The artifact list is the heavy one (the
/// incident's 1-8 MB) and its items arrive one by one between reloads; a
/// busy Host sends an attention event with every artifact event (about one
/// every 18 s on 2026-10-13) and a job event several times a minute.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Intervals {
    pub artifacts: Duration,
    pub attention: Duration,
    pub jobs: Duration,
}

impl Default for Intervals {
    fn default() -> Self {
        Intervals { artifacts: Duration::from_secs(30), attention: Duration::from_secs(5), jobs: Duration::from_secs(2) }
    }
}

/// What a reload request may do now.
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Gate {
    /// Reload now.
    Now,
    /// Too soon: arm one timer for this long; the reload follows it.
    After(Duration),
    /// Too soon, and a trailing reload is already armed.
    Armed,
}

/// The minimum interval between one list's event-driven reloads.
#[derive(Debug, Clone)]
pub struct Throttle {
    interval: Duration,
    last: Option<Instant>,
    trailing: bool,
}

impl Throttle {
    pub fn new(interval: Duration) -> Self {
        Throttle { interval, last: None, trailing: false }
    }

    pub fn set_interval(&mut self, interval: Duration) {
        self.interval = interval;
    }

    /// An event asks for a reload at `now`.
    pub fn request(&mut self, now: Instant) -> Gate {
        let Some(next) = self.last.map(|last| last + self.interval).filter(|next| *next > now) else {
            return Gate::Now;
        };
        if self.trailing {
            return Gate::Armed;
        }
        self.trailing = true;
        Gate::After(next - now)
    }

    /// A reload started (for any reason): it satisfies the armed one.
    pub fn started(&mut self, now: Instant) {
        self.last = Some(now);
        self.trailing = false;
    }

    /// The armed timer fired: true when its reload is still owed (no other
    /// reload started meanwhile).
    pub fn fire(&mut self) -> bool {
        std::mem::take(&mut self.trailing)
    }
}

/// `GET /artifacts` for the Updates list.
pub fn artifact_list_path(flat: bool) -> String {
    let mut path = format!("/artifacts?limit={ARTIFACT_LIST_LIMIT}&order=updated");
    if flat {
        path.push_str("&representation=");
        path.push_str(FLAT_REPRESENTATION);
    }
    path
}

/// `GET /artifacts/<id>`: one artifact, as the list has it.
pub fn artifact_item_path(artifact_id: &str, flat: bool) -> String {
    let mut path = format!("/artifacts/{}", crate::endpoint::percent_encode_segment(artifact_id));
    if flat {
        path.push_str("?representation=");
        path.push_str(FLAT_REPRESENTATION);
    }
    path
}

/// The Host's 400 for a representation it does not know.
pub fn is_unsupported_representation(status: u16, message: &str) -> bool {
    status == 400 && message.contains("unsupported artifact representation")
}

/// Leaves the item as a flat-v1 list returns it. `GET /artifacts/<id>`
/// always answers in full; an item patched into a flat list matches it.
pub fn flatten(item: &mut Value) {
    if let Some(object) = item.as_object_mut() {
        object.remove("payload");
    }
}

fn id(item: &Value) -> &str {
    item.get("artifact_id").and_then(Value::as_str).unwrap_or_default()
}

fn updated_at(item: &Value) -> Option<i64> {
    item.get("updated_at").and_then(Value::as_i64)
}

/// Whether the Host's list would hold the item at all.
pub fn listed(item: &Value) -> bool {
    let kind = item.get("type").and_then(Value::as_str).unwrap_or_default();
    let deleted = item.get("deleted_at").is_some_and(|d| !d.is_null());
    !id(item).is_empty() && !deleted && !UNLISTED_TYPES.contains(&kind)
}

/// How a patch changed the list.
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Patch {
    /// Nothing to change (an unlisted item that was not there).
    Unchanged,
    /// The item is in the list now, at its place by `updated_at`. With
    /// `overflow` the list holds more than the Host's would: the oldest
    /// left the Host's list, and only a reload says which.
    Placed { overflow: bool },
    /// The item left the list. When it was full the Host's list now has
    /// another item at the end that this one lacks: reload it.
    Removed { was_full: bool },
}

impl Patch {
    /// The list differs from the Host's in a way only a reload repairs.
    pub fn needs_reload(self) -> bool {
        matches!(self, Patch::Placed { overflow: true } | Patch::Removed { was_full: true })
    }
}

/// One artifact as the Host has it now (`None`: gone, a 404) applied to the
/// list, which is ordered as the Host orders it: newest `updated_at` first,
/// then the higher id. An item that is new or updated moves to its place;
/// one changed without a new `updated_at` (a pin) stays where it was; one
/// deleted or unlisted leaves. `cap` is how many the Host's list holds.
pub fn patch_artifact(list: &mut Vec<Value>, artifact_id: &str, item: Option<Value>, cap: usize) -> Patch {
    let at = list.iter().position(|a| id(a) == artifact_id);
    let Some(item) = item.filter(listed) else {
        return match at {
            Some(at) => {
                let was_full = list.len() >= cap;
                list.remove(at);
                Patch::Removed { was_full }
            }
            None => Patch::Unchanged,
        };
    };
    if let Some(at) = at
        && updated_at(&list[at]) == updated_at(&item)
    {
        list[at] = item;
        return Patch::Placed { overflow: false };
    }
    if let Some(at) = at {
        list.remove(at);
    }
    let place = match updated_at(&item) {
        // Before the first item it sorts ahead of; an item with no time is
        // treated as newer (the fixtures and a Host that sends none).
        Some(time) => list
            .iter()
            .position(|other| match updated_at(other) {
                Some(other_time) => other_time < time || (other_time == time && id(other) < id(&item)),
                None => false,
            })
            .unwrap_or(list.len()),
        None => 0,
    };
    list.insert(place, item);
    Patch::Placed { overflow: list.len() > cap }
}
