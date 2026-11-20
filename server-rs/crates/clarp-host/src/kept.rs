//! What the Host keeps, read back: one loader, with a declared policy, for every kind.
//!
//! A record that cannot be read never stops the Host: it is LEFT OUT of what the Host does, and
//! no desk ever sees it; it is counted by its kind, and named in the journal by its kind and
//! key, never by what is in it. The count is shown at the Host's own door (`GET /host/start`,
//! the administrator's) and in one line when the Host starts, so that an installation still
//! finds a changed shape: by a number, not by a Host that does not start. Only the
//! configuration, the listener, a store that does not answer and a core thread that does not
//! start stop a start.
//!
//! The policies. `rows`: a kind whose every record stands alone, and whose keys an owner's
//! action alone writes. A row that cannot be read stays where it is, never written over or
//! deleted by the Host, and hidden from every desk (`FleetHandle::kept`). The one write its key
//! may get is the owner's own (a phone that registers the same token again, a peer added again
//! under its name): that is the repair, said in the journal, and the row is no longer counted.
//! The owner's own deletes (a phone unregistered) are said as deletes. Books: every kind whose
//! records the Host writes by itself. A book's record that cannot be read is MOVED aside whole,
//! in one write, never lost: the copy goes under `<key>.unreadable` (beside a copy that stands,
//! the next free name: a copy is never written over) and the key is let go, so its desk starts
//! without it and what it writes there next is new. A copy is counted at every start while it
//! stands, and a delete of its key takes it along, said as one delete.
//!
//! When a kind is judged is declared on it too. A kind some desk reads whole at the start anyway
//! is judged at the start, before the door opens: a start costs what the live fleet weighs. A
//! kind that grows with a history (a run, an event, an attempt) and is read only later, or by
//! key alone, is judged when it is read, by its desk's own type, and is read nowhere raw: by key
//! through `FleetHandle::kept_book`, whole through `FleetHandle::kept_listed`. One judge does
//! both (`judge`): the walk is it over the kinds judged at the start. The door names the kinds
//! judged when read, and the start's line claims only what it read. Every kind is the one or the
//! other (`kept_registry`'s test holds it).

use std::collections::{BTreeMap, BTreeSet};
use std::sync::{Arc, Mutex};

use clarp_ports::kv::{KeyValue, StoreError, Write};
use serde::de::DeserializeOwned;
use serde_json::Value;

use crate::unkept::Log;

/// How a kind's records are read back.
///
/// `Rows`: each record stands alone, and its key is written only from a record the Host read
/// or by an owner's action (a token registered, a peer named, a phone's call): a record that
/// cannot be read is left out, and the owner's next write to its key is its repair. `Book`:
/// a kind with any key the Host writes by itself (a fixed key, a provider's name, a document of
/// a desk): a record that cannot be read is set aside under `<key>.unreadable`, once, before
/// anything else, and the desk starts without it and says so; the Host's next write to that key
/// loses nothing, and is no repair.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Policy {
    Rows,
    Book,
}

/// Whether a kind's keys may be printed whole, or not at all (a secret, such as a push token or
/// an opening's token: never one sign of it, the record is said by its place in the count).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Key {
    Plain,
    Secret,
}

/// When a kind is judged: at the start, before the door opens (a kind some desk lists at the
/// start anyway), or when it is read (a kind that grows with a history: a start reads none of it).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Judged {
    AtStart,
    WhenRead,
}

/// What a read of one record found: nothing, a record of its desk's, or one that is not (or was
/// not, and was moved aside), which was judged (`judge`: a row left out, a book's record moved
/// aside) and counted. A desk that
/// would write over what it cannot see (a counter, a ledger's tail) tells the last from the first.
#[derive(Debug, PartialEq, Eq)]
pub enum Found {
    Absent,
    Read(Vec<u8>),
    Unfit,
}

impl Found {
    /// The record, where it is its desk's.
    pub fn read(self) -> Option<Vec<u8>> {
        match self {
            Self::Read(stored) => Some(stored),
            Self::Absent | Self::Unfit => None,
        }
    }
}

/// One kind the Host keeps, declared once beside its desk: its name, whether it is filed under a
/// shelf of its own (`kept.<name>`) or is one of the fleet's tables, its policy, how its keys may
/// be printed, when it is judged, and whether a record is one of its desk's.
#[derive(Clone, Copy)]
pub struct Kind {
    pub name: &'static str,
    pub shelved: bool,
    pub policy: Policy,
    pub key: Key,
    pub judged: Judged,
    pub fits: fn(&[u8]) -> bool,
    /// For a book read entry by entry: the entries of a record that are not its desk's, by
    /// where they are in it. They are carried through (`Carried`).
    pub entries: Option<Entries>,
}

/// Where a record's entries that are not its desk's are.
pub type Entries = fn(&[u8]) -> Vec<String>;

impl Kind {
    /// A kind filed under a shelf of its own.
    pub const fn book(name: &'static str, key: Key, fits: fn(&[u8]) -> bool) -> Self {
        Self {
            name,
            shelved: true,
            policy: Policy::Book,
            key,
            judged: Judged::AtStart,
            fits,
            entries: None,
        }
    }

    /// A kind of rows filed under a shelf of its own.
    pub const fn rows(name: &'static str, key: Key, fits: fn(&[u8]) -> bool) -> Self {
        Self {
            name,
            shelved: true,
            policy: Policy::Rows,
            key,
            judged: Judged::AtStart,
            fits,
            entries: None,
        }
    }

    /// One of the fleet's own tables.
    pub const fn table(
        name: &'static str,
        policy: Policy,
        key: Key,
        fits: fn(&[u8]) -> bool,
    ) -> Self {
        Self {
            name,
            shelved: false,
            policy,
            key,
            judged: Judged::AtStart,
            fits,
            entries: None,
        }
    }

    /// The same kind, read entry by entry.
    pub const fn by_entries(self, entries: Entries) -> Self {
        Self {
            entries: Some(entries),
            ..self
        }
    }

    /// The same kind, judged when it is read and not at the start: read nowhere raw, only
    /// through `FleetHandle::kept_book` and `FleetHandle::kept_listed`.
    pub const fn when_read(self) -> Self {
        Self {
            judged: Judged::WhenRead,
            ..self
        }
    }

    /// The table the store files it under.
    pub fn table_name(&self) -> String {
        if self.shelved {
            format!("kept.{}", self.name)
        } else {
            self.name.to_string()
        }
    }
}

/// Whether bytes are a record of this type.
pub fn fits<T: DeserializeOwned>(stored: &[u8]) -> bool {
    serde_json::from_slice::<T>(stored).is_ok()
}

/// Whether bytes are JSON at all: for a desk that reads its records as JSON values.
pub fn json(stored: &[u8]) -> bool {
    serde_json::from_slice::<serde::de::IgnoredAny>(stored).is_ok()
}

/// Anything at all: for a kind whose values are never read, only its keys.
pub fn any(_: &[u8]) -> bool {
    true
}

/// Whether bytes are text: for a kind kept as plain words.
pub fn text(stored: &[u8]) -> bool {
    std::str::from_utf8(stored).is_ok()
}

/// The entries of a book's document that are not its desk's, taken out of it as they came, to be
/// put back unchanged with every save: left out of what the desk does, never lost.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Carried {
    /// A document that is one list: its items that do not fit.
    items: Vec<Value>,
    /// Of a document that is an object: by field, the items of a list and the entries of a map
    /// that do not fit.
    lists: BTreeMap<String, Vec<Value>>,
    maps: BTreeMap<String, serde_json::Map<String, Value>>,
}

impl Carried {
    /// Where each entry carried is, for the journal and the count: `[<at>]`, `<field>[<at>]`,
    /// `<field>.<key>`.
    pub fn labels(&self) -> Vec<String> {
        let items = (0..self.items.len()).map(|at| format!("[{at}]"));
        let lists = self
            .lists
            .iter()
            .flat_map(|(field, items)| (0..items.len()).map(move |at| format!("{field}[{at}]")));
        let maps = self
            .maps
            .iter()
            .flat_map(|(field, entries)| entries.keys().map(move |key| format!("{field}.{key}")));
        items.chain(lists).chain(maps).collect()
    }

    /// The entries of a map carried whose key the desk's own document holds again: the desk
    /// wrote that key since, and its write is the entry's repair (`FleetHandle::written_over`).
    pub fn written_over(&self, document: &Value) -> Vec<(String, String)> {
        let Some(fields) = document.as_object() else {
            return Vec::new();
        };
        self.maps
            .iter()
            .flat_map(|(field, entries)| {
                let held = fields.get(field).and_then(Value::as_object);
                entries
                    .keys()
                    .filter(move |key| held.is_some_and(|map| map.contains_key(*key)))
                    .map(move |key| (field.clone(), key.clone()))
            })
            .collect()
    }

    /// A map's entry is carried no more.
    pub fn forget(&mut self, field: &str, key: &str) {
        if let Some(entries) = self.maps.get_mut(field) {
            entries.remove(key);
            if entries.is_empty() {
                self.maps.remove(field);
            }
        }
    }

    /// Puts what was carried back into a document about to be saved, as it came. A list's items
    /// go back at its end: their place among the items its desk wrote is not kept (a book whose
    /// order means something, the schedules' list, gets its unfit ones after its own). A map's
    /// entry whose key the desk wrote since is not put back: that write is its repair.
    pub fn put_back(&self, document: &mut Value) {
        if let Value::Array(items) = document {
            items.extend(self.items.iter().cloned());
            return;
        }
        let Some(fields) = document.as_object_mut() else {
            return;
        };
        for (field, items) in &self.lists {
            match fields.get_mut(field) {
                Some(Value::Array(list)) => list.extend(items.iter().cloned()),
                _ => {
                    fields.insert(field.clone(), Value::Array(items.clone()));
                }
            }
        }
        for (field, entries) in &self.maps {
            let map = fields
                .entry(field.clone())
                .or_insert_with(|| Value::Object(serde_json::Map::new()));
            if let Some(map) = map.as_object_mut() {
                for (key, value) in entries {
                    map.entry(key.clone()).or_insert_with(|| value.clone());
                }
            }
        }
    }
}

/// Takes out of a book's document every entry that is not its desk's: each item of a document
/// that is a list, tried as `I`; each item and entry of the named fields of a document that is an
/// object, tried as the book `B` holding that entry alone.
pub fn carry<B: DeserializeOwned, I: DeserializeOwned>(
    document: &mut Value,
    fields: &[&str],
) -> Carried {
    let mut carried = Carried::default();
    if let Value::Array(items) = document {
        let (fit, unfit): (Vec<Value>, Vec<Value>) = std::mem::take(items)
            .into_iter()
            .partition(|item| serde_json::from_value::<I>(item.clone()).is_ok());
        *items = fit;
        carried.items = unfit;
        return carried;
    }
    let Some(object) = document.as_object_mut() else {
        return carried;
    };
    let alone = |field: &str, entry: Value| {
        let mut book = serde_json::Map::new();
        book.insert(field.to_string(), entry);
        serde_json::from_value::<B>(Value::Object(book)).is_ok()
    };
    for field in fields {
        match object.get_mut(*field) {
            Some(Value::Array(items)) => {
                let (fit, unfit): (Vec<Value>, Vec<Value>) = std::mem::take(items)
                    .into_iter()
                    .partition(|item| alone(field, Value::Array(vec![item.clone()])));
                *items = fit;
                if !unfit.is_empty() {
                    carried.lists.insert((*field).to_string(), unfit);
                }
            }
            Some(Value::Object(entries)) => {
                let keys: Vec<String> = entries.keys().cloned().collect();
                for key in keys {
                    let mut one = serde_json::Map::new();
                    one.insert(key.clone(), entries[&key].clone());
                    if !alone(field, Value::Object(one))
                        && let Some(value) = entries.remove(&key)
                    {
                        let carried_map = carried.maps.entry((*field).to_string()).or_default();
                        carried_map.insert(key, value);
                    }
                }
            }
            _ => {}
        }
    }
    carried
}

/// Whether a book's document is its desk's once the entries that are not are carried out of it.
pub fn fits_carried<B: DeserializeOwned, I: DeserializeOwned>(
    stored: &[u8],
    fields: &[&str],
) -> bool {
    let Ok(mut document) = serde_json::from_slice::<Value>(stored) else {
        return false;
    };
    carry::<B, I>(&mut document, fields);
    serde_json::from_value::<B>(document).is_ok()
}

/// A book's document as it is saved: the book, and what was carried put back as it came.
pub fn saved<B: serde::Serialize>(book: &B, carried: &Carried) -> Vec<u8> {
    let mut document = serde_json::to_value(book).unwrap_or(Value::Null);
    carried.put_back(&mut document);
    serde_json::to_vec(&document).unwrap_or_default()
}

/// What the walk asks of a book read entry by entry: where its entries that do not fit are.
pub fn unfit<B: DeserializeOwned, I: DeserializeOwned>(
    stored: &[u8],
    fields: &[&str],
) -> Vec<String> {
    let Ok(mut document) = serde_json::from_slice::<Value>(stored) else {
        return Vec::new();
    };
    carry::<B, I>(&mut document, fields).labels()
}

/// What a set-aside copy's key ends in, before the number of a second copy beside the first
/// (`<key>.unreadable`, `<key>.unreadable.2`, ...). A desk never sees such a key.
pub const SET_ASIDE: &str = ".unreadable";

/// The register of the copies that stand, of every kind (one entry each, written with the move
/// that made the copy and let go with it): what the start counts, whether the copy's own kind is
/// judged at the start or when read. Its keys hold others' keys: none is ever printed.
pub(crate) const KEPT: &[Kind] = &[Kind::book(PARKED, Key::Secret, any)];

/// The register's own kind (`KEPT`).
const PARKED: &str = "set-aside";

/// The register's table.
fn register() -> String {
    format!("kept.{PARKED}")
}

/// The register's key for a copy: its table and its key.
fn parked_key(table: &str, copy: &str) -> String {
    format!("{table}\u{1f}{copy}")
}

/// The name of the `n`th copy of a key (from 1): a copy is never written over.
pub fn copy_name(key: &str, n: usize) -> String {
    if n <= 1 {
        format!("{key}{SET_ASIDE}")
    } else {
        format!("{key}{SET_ASIDE}.{n}")
    }
}

/// The key a set-aside copy was moved from, where this key is a copy.
pub fn copy_of(key: &str) -> Option<&str> {
    if let Some(base) = key.strip_suffix(SET_ASIDE) {
        return Some(base);
    }
    let (first, number) = key.rsplit_once('.')?;
    let numbered = !number.is_empty() && number.bytes().all(|b| b.is_ascii_digit());
    first.strip_suffix(SET_ASIDE).filter(|_| numbered)
}

/// The copies set aside from a key, in their order: they are made one after another and go
/// together with their key, so the first name that is free ends them.
pub fn copies(store: &dyn KeyValue, table: &str, key: &str) -> Result<Vec<String>, StoreError> {
    let mut found = Vec::new();
    while store
        .get(table, &copy_name(key, found.len() + 1))?
        .is_some()
    {
        found.push(copy_name(key, found.len() + 1));
    }
    Ok(found)
}

/// The kind a table is, where the registry has it.
pub fn kind(table: &str) -> Option<&'static Kind> {
    crate::kept_registry::kinds().find(|kind| kind.table_name() == table)
}

/// What is said of a record: its kind, without the shelf the store files it under.
pub fn kind_of(table: &str) -> &str {
    table.strip_prefix("kept.").unwrap_or(table)
}

/// How a key is named in the journal: in full where its kind declares it plain; a secret (a push
/// token, the token of an opening) and a key of a kind the registry does not know are named by
/// nothing of the key, only by their place among the records of that kind so far.
fn named(table: &str, key: &str, place: usize) -> String {
    if kind(table).is_some_and(|kind| kind.key == Key::Plain) {
        key.to_string()
    } else {
        format!("{place} of this kind so far")
    }
}

/// Where a set-aside copy is, as the journal says it: by its key where the key is plain, and
/// beside the record otherwise (a copy's key holds the record's).
fn aside(table: &str, copy: &str, place: usize) -> String {
    if kind(table).is_some_and(|kind| kind.key == Key::Plain) {
        format!("as {copy}")
    } else {
        format!("beside it, {place} of this kind so far")
    }
}

/// The kinds judged, and counted, when they are read, not at the start.
pub fn judged_when_read() -> Vec<&'static str> {
    crate::kept_registry::kinds()
        .filter(|kind| kind.judged == Judged::WhenRead)
        .map(|kind| kind.name)
        .collect()
}

/// The one judge: a record against its kind's own type and policy. One that fits is its desk's
/// (a book's entries that do not are counted: its desk carries them through). One that does not
/// is a row left out (hidden from every desk until the owner repairs or deletes it), or a book's
/// record moved aside whole to `<key>.unreadable` (the next free name beside it where a copy
/// stands) in one write, before anything else (a store that refuses it is the answer), counted,
/// and said once. A record of a
/// kind the registry does not have is taken as it is (its test finds none).
fn judge(
    store: &mut dyn KeyValue,
    kind: Option<&Kind>,
    table: &str,
    key: &str,
    stored: Vec<u8>,
    left: &LeftOut,
    log: &Log,
) -> Result<Found, StoreError> {
    let Some(kind) = kind else {
        return Ok(Found::Read(stored));
    };
    if (kind.fits)(&stored) {
        for at in kind
            .entries
            .map(|entries| entries(&stored))
            .unwrap_or_default()
        {
            left.left_out(table, &format!("{key}#{at}"), log);
        }
        return Ok(Found::Read(stored));
    }
    match kind.policy {
        Policy::Rows => left.left_out(table, key, log),
        Policy::Book => {
            // Moved, not copied: the copy under the first free name beside it and the record's
            // own key let go, in one write. The key is free; what its desk writes next is new.
            let aside = copy_name(key, copies(store, table, key)?.len() + 1);
            let moved = [
                (table.to_string(), aside.clone(), Some(stored)),
                (table.to_string(), key.to_string(), None),
                (register(), parked_key(table, &aside), Some(Vec::new())),
            ];
            store.put_together(&moved)?;
            left.set_aside(table, &aside, log);
        }
    }
    Ok(Found::Unfit)
}

/// One record of a table, judged as it is read. A key whose record was moved aside, and nothing
/// written there since, is answered as unfit, not absent: a desk that tells the two apart (a
/// counter, a ledger's tail) does not begin anew over what it kept (the copies that stand are
/// known without a read: counted at the start and as they are made).
pub fn judged(
    store: &mut dyn KeyValue,
    table: &str,
    key: &str,
    left: &LeftOut,
    log: &Log,
) -> Result<Found, StoreError> {
    let Some(stored) = store.get(table, key)? else {
        if left.is_left_out(table, &copy_name(key, 1)) {
            return Ok(Found::Unfit);
        }
        return Ok(Found::Absent);
    };
    judge(store, kind(table), table, key, stored, left, log)
}

/// What a whole table holds, each record judged as it is read: those that fit, and the names the
/// others go by (a left-out agent's names are not another's).
#[derive(Default)]
pub struct Listed {
    pub read: Vec<(String, Vec<u8>)>,
    pub names: Vec<Name>,
}

/// A whole table, each record judged (`judge`); a set-aside copy is no record of its desk's.
pub fn listed(
    store: &mut dyn KeyValue,
    table: &str,
    left: &LeftOut,
    log: &Log,
) -> Result<Listed, StoreError> {
    match kind(table) {
        Some(declared) => listed_as(store, table, declared.fits, left, log),
        // A kind the registry does not have is taken as it is (its test finds none).
        None => listed_as(store, table, any, left, log),
    }
}

/// The kinds judged at the start, each whole, before the door opens: the judge over them, so
/// that what the start reads is counted whole (a kind judged when read is not read here).
pub fn walk(store: &mut dyn KeyValue, left: &LeftOut, log: &Log) -> Result<(), StoreError> {
    // First every copy that stands, of a kind judged when read too: a copy is known by the
    // register, not by its name (a key of the desk's own may end so); an entry whose copy
    // somebody removed is let go.
    for (entry, _) in store.list(&register())? {
        let Some((table, copy)) = entry.split_once('\u{1f}') else {
            continue;
        };
        if store.get(table, copy)?.is_some() {
            left.parked(table, copy);
        } else {
            store.delete(&register(), &entry)?;
        }
    }
    for kind in crate::kept_registry::kinds().filter(|kind| kind.judged == Judged::AtStart) {
        listed(store, &kind.table_name(), left, log)?;
    }
    Ok(())
}

/// What the Host left out, by the table and key it is under: what the door counts.
#[derive(Default)]
pub struct LeftOut {
    held: Mutex<BTreeMap<String, BTreeSet<String>>>,
    not_run: Mutex<BTreeMap<String, u64>>,
}

impl LeftOut {
    /// Counts a record of a table: its place among that table's so far, where it is new.
    fn add(&self, table: &str, key: &str) -> Option<usize> {
        let mut held = self.held.lock().ok()?;
        let keys = held.entry(table.to_string()).or_default();
        keys.insert(key.to_string()).then_some(keys.len())
    }

    /// Counts a record no more: its place among that table's before, where it was counted.
    fn remove(&self, table: &str, key: &str) -> Option<usize> {
        let mut held = self.held.lock().ok()?;
        let keys = held.get_mut(table)?;
        let place = keys.len();
        let was = keys.remove(key);
        held.retain(|_, keys| !keys.is_empty());
        was.then_some(place)
    }

    /// A record that cannot be read: counted, and said once by its kind and key.
    pub fn left_out(&self, table: &str, key: &str, log: &Log) {
        if let Some(place) = self.add(table, key) {
            log.line(&format!(
                "clarp-host: a record of the kind {} could not be read and is left out: {}",
                kind_of(table),
                named(table, key, place)
            ));
        }
    }

    /// A write to a key that was left out: the owner's own, which repairs it. Answers whether
    /// it was one.
    pub fn written(&self, table: &str, key: &str, log: &Log) -> bool {
        let Some(place) = self.remove(table, key) else {
            return false;
        };
        log.line(&format!(
            "clarp-host: a record of the kind {} that could not be read was written anew: {}",
            kind_of(table),
            named(table, key, place)
        ));
        true
    }

    /// A key that was left out and is now deleted: said as what it is, and no longer counted.
    pub fn deleted(&self, table: &str, key: &str, log: &Log) {
        if let Some(place) = self.remove(table, key) {
            log.line(&format!(
                "clarp-host: a record of the kind {} that could not be read was deleted: {}",
                kind_of(table),
                named(table, key, place)
            ));
        }
    }

    /// A record a desk left out is used after all (a call's binding recovered once its agent
    /// answered): no longer counted, and said.
    pub fn recovered(&self, table: &str, key: &str, log: &Log) {
        if let Some(place) = self.remove(table, key) {
            log.line(&format!(
                "clarp-host: a record of the kind {} that was left out is used after all: {}",
                kind_of(table),
                named(table, key, place)
            ));
        }
    }

    /// A book's record that could not be read, moved aside to `copy`: the copy counted while it
    /// stands, and said once.
    pub fn set_aside(&self, table: &str, copy: &str, log: &Log) {
        if let Some(place) = self.add(table, copy) {
            log.line(&format!(
                "clarp-host: a record of the kind {} could not be read and is set aside {}: its desk starts without it",
                kind_of(table),
                aside(table, copy, place)
            ));
        }
    }

    /// A copy set aside before, still standing: counted at every start, until somebody removes it.
    pub fn parked(&self, table: &str, copy: &str) {
        self.add(table, copy);
    }

    /// A key deleted with the copies set aside beside it: one delete, said as one, and the copies
    /// no longer counted.
    pub fn deleted_with(&self, table: &str, key: &str, copies: &[String], log: &Log) {
        let mut place = self.remove(table, key);
        for copy in copies {
            place = place.or(self.remove(table, copy));
        }
        log.line(&format!(
            "clarp-host: a record of the kind {} was deleted with the {} set aside beside it: {}",
            kind_of(table),
            copies.len(),
            named(table, key, place.unwrap_or(copies.len()))
        ));
    }

    /// A deleted agent whose goals could not be retired: its record was read, and its ledger is
    /// retired at a later start. Counted until then, and said in its own words.
    pub fn retire_later(&self, table: &str, key: &str, log: &Log) {
        if self.add(table, key).is_some() {
            log.line(&format!(
                "clarp-host: a deleted agent's goals could not be retired now and are retired at a later start: {key}"
            ));
        }
    }

    /// An agent the Host keeps and does not run, for want of its tool here: counted by its tool
    /// (`GET /host/start`'s `not_run`), not as left out.
    pub fn not_run(&self, tool: &str) {
        if let Ok(mut not_run) = self.not_run.lock() {
            *not_run.entry(tool.to_string()).or_default() += 1;
        }
    }

    /// How many agents the Host keeps and does not run, by their tool.
    pub fn not_run_counts(&self) -> BTreeMap<String, u64> {
        self.not_run
            .lock()
            .map(|not_run| not_run.clone())
            .unwrap_or_default()
    }

    /// Whether this key is one that was left out.
    pub fn is_left_out(&self, table: &str, key: &str) -> bool {
        self.held
            .lock()
            .is_ok_and(|held| held.get(table).is_some_and(|keys| keys.contains(key)))
    }

    /// How many records of each kind are left out: only kinds that have one.
    pub fn counts(&self) -> BTreeMap<String, u64> {
        let held = self
            .held
            .lock()
            .map(|held| held.clone())
            .unwrap_or_default();
        held.into_iter()
            .filter(|(_, keys)| !keys.is_empty())
            .map(|(table, keys)| {
                let count = u64::try_from(keys.len()).unwrap_or(u64::MAX);
                (kind_of(&table).to_string(), count)
            })
            .collect()
    }

    /// The one line a start says of it.
    pub fn line(&self) -> String {
        let counts = self.counts();
        if counts.is_empty() {
            return "clarp-host: every record read at the start could be read".to_string();
        }
        let total: u64 = counts.values().sum();
        let each: Vec<String> = counts
            .iter()
            .map(|(kind, count)| format!("{kind} {count}"))
            .collect();
        format!(
            "clarp-host: {total} records read at the start could not be read and are left out ({})",
            each.join(", ")
        )
    }
}

/// The name a record that was left out keeps: its session and the name it goes by, where its
/// bytes are JSON that name them. A left-out agent's names are not another's.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Name {
    pub agent_id: String,
    pub session: String,
    pub persona: String,
    pub voice_id: String,
}

impl Name {
    /// The names in bytes that are JSON, where they name any.
    pub fn of(stored: &[u8]) -> Option<Self> {
        let value: Value = serde_json::from_slice(stored).ok()?;
        let text = |key: &str| value.get(key).and_then(Value::as_str).map(str::to_string);
        let name = Self {
            agent_id: text("agent_id").unwrap_or_default(),
            session: text("session").unwrap_or_default(),
            persona: text("persona").unwrap_or_default(),
            voice_id: text("voice_id").unwrap_or_default(),
        };
        (!name.session.is_empty() || !name.persona.is_empty()).then_some(name)
    }
}

/// The rows of a kind its desk could read as its type, and the names of those it could not.
pub struct Rows<T> {
    pub read: Vec<(String, T)>,
    pub names: Vec<Name>,
}

/// A kind of rows judged as its desk's type `T` (the one judge, with `T` for its type): a store
/// that does not answer is an error (it stops a start); a record that is not a `T` is left out,
/// counted and said.
pub fn rows<T: DeserializeOwned>(
    store: &mut dyn KeyValue,
    table: &str,
    left: &LeftOut,
    log: &Log,
) -> Result<Rows<T>, StoreError> {
    Ok(typed(listed_as(store, table, fits::<T>, left, log)?))
}

/// The same judge as `listed`, with the desk's own type as the kind's.
pub fn listed_as(
    store: &mut dyn KeyValue,
    table: &str,
    as_type: fn(&[u8]) -> bool,
    left: &LeftOut,
    log: &Log,
) -> Result<Listed, StoreError> {
    let declared = kind(table).copied().unwrap_or(UNDECLARED);
    let declared = Kind {
        fits: as_type,
        ..declared
    };
    let mut listed = Listed::default();
    for (key, stored) in store.list(table)? {
        // A copy set aside is no record of its desk's: counted while it stands, and known by
        // the register (read first), not by its name.
        if table != register() && copy_of(&key).is_some() && left.is_left_out(table, &key) {
            continue;
        }
        let names = Name::of(&stored);
        match judge(store, Some(&declared), table, &key, stored, left, log)? {
            Found::Read(stored) => listed.read.push((key, stored)),
            Found::Absent | Found::Unfit => listed.names.extend(names),
        }
    }
    Ok(listed)
}

/// What a desk reads as a kind of rows the registry does not have (its test finds none).
const UNDECLARED: Kind = Kind::rows("", Key::Secret, any);

/// Records judged as a `T`, read as one (each fits it).
pub fn typed<T: DeserializeOwned>(listed: Listed) -> Rows<T> {
    let read = listed
        .read
        .into_iter()
        .filter_map(|(key, stored)| Some((key, serde_json::from_slice::<T>(&stored).ok()?)))
        .collect();
    Rows {
        read,
        names: listed.names,
    }
}

/// The store, as the Host writes it: a write to a key that was left out is the owner's repair,
/// and is said; the record is no longer counted.
pub struct Watched {
    inner: Box<dyn KeyValue>,
    left: Arc<LeftOut>,
    log: Log,
}

impl Watched {
    pub fn new(inner: Box<dyn KeyValue>, left: Arc<LeftOut>, log: Log) -> Self {
        Self { inner, left, log }
    }
}

/// The deletes that let a key's copies go, with their entries in the register.
fn gone(table: &str, copies: &[String]) -> Vec<Write> {
    copies
        .iter()
        .flat_map(|copy| {
            [
                (table.to_string(), copy.clone(), None),
                (register(), parked_key(table, copy), None),
            ]
        })
        .collect()
}

impl Watched {
    /// The copies a delete of a book's key takes with it: none where the same write moves a
    /// record of that key aside (a move lets its key go and keeps the copy it makes).
    fn going_with(
        &self,
        table: &str,
        key: &str,
        writes: &[Write],
    ) -> Result<Vec<String>, StoreError> {
        let book = kind(table).is_some_and(|kind| kind.policy == Policy::Book);
        if !book || copy_of(key).is_some() || table == register() {
            return Ok(Vec::new());
        }
        let moved = writes.iter().any(|(written, copy, value)| {
            value.is_some() && written == table && copy_of(copy) == Some(key)
        });
        if moved {
            return Ok(Vec::new());
        }
        // Only what is counted as a copy goes: a key of the desk's own may end so.
        let mut counted = copies(self.inner.as_ref(), table, key)?;
        counted.retain(|copy| self.left.is_left_out(table, copy));
        Ok(counted)
    }
}

impl KeyValue for Watched {
    fn get(&self, table: &str, key: &str) -> Result<Option<Vec<u8>>, StoreError> {
        self.inner.get(table, key)
    }

    fn put(&mut self, table: &str, key: &str, value: &[u8]) -> Result<(), StoreError> {
        self.inner.put(table, key, value)?;
        // The Host writes a book's keys by itself: what it writes there repairs nothing (what
        // could not be read was moved aside).
        if kind(table).is_none_or(|kind| kind.policy == Policy::Rows) {
            self.left.written(table, key, &self.log);
        }
        Ok(())
    }

    fn put_together(&mut self, writes: &[Write]) -> Result<(), StoreError> {
        // A key deleted takes the copies set aside beside it, in the same write.
        let mut all = writes.to_vec();
        let mut going = Vec::new();
        for (table, key, value) in writes {
            if value.is_none() {
                let copies = self.going_with(table, key, writes)?;
                all.extend(gone(table, &copies));
                going.push(copies);
            }
        }
        self.inner.put_together(&all)?;
        let mut going = going.into_iter();
        for (table, key, value) in writes {
            if value.is_some() {
                if kind(table).is_none_or(|kind| kind.policy == Policy::Rows) {
                    self.left.written(table, key, &self.log);
                }
                continue;
            }
            match going.next().unwrap_or_default() {
                copies if copies.is_empty() => self.left.deleted(table, key, &self.log),
                copies => self.left.deleted_with(table, key, &copies, &self.log),
            }
        }
        Ok(())
    }

    fn delete(&mut self, table: &str, key: &str) -> Result<bool, StoreError> {
        let copies = self.going_with(table, key, &[])?;
        if copies.is_empty() {
            let was = self.inner.delete(table, key)?;
            self.left.deleted(table, key, &self.log);
            return Ok(was);
        }
        let was = self.inner.get(table, key)?.is_some();
        let mut all = vec![(table.to_string(), key.to_string(), None)];
        all.extend(gone(table, &copies));
        self.inner.put_together(&all)?;
        self.left.deleted_with(table, key, &copies, &self.log);
        Ok(was)
    }

    fn list(&self, table: &str) -> Result<Vec<(String, Vec<u8>)>, StoreError> {
        self.inner.list(table)
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use std::path::Path;
    use std::sync::{Arc, Mutex};

    use clarp_ports::clock::SystemClock;
    use clarp_ports::kv::{KeyValue, KeyValueEngine};
    use clarp_store_mem::MemKeyValue;
    use serde::Deserialize;

    use super::{LeftOut, Name, Watched, rows, walk};
    use crate::unkept::Log;

    #[derive(Debug, Deserialize, serde::Serialize, PartialEq)]
    struct Row {
        count: u64,
    }

    fn log() -> (Log, Arc<Mutex<Vec<String>>>) {
        let said: Arc<Mutex<Vec<String>>> = Arc::default();
        let heard = said.clone();
        let log = Log::to(
            move |line| heard.lock().unwrap().push(line.to_string()),
            Arc::new(SystemClock),
        );
        (log, said)
    }

    fn store() -> Box<dyn KeyValue> {
        MemKeyValue::new().open(Path::new("/kept-tests")).unwrap()
    }

    #[test]
    fn each_row_is_read_alone_and_one_that_cannot_be_is_left_out_counted_and_named() {
        let (log, said) = log();
        let left = LeftOut::default();
        let mut store = store();
        store.put("peer", "a", br#"{"count":1}"#).unwrap();
        store.put("peer", "b", b"no json").unwrap();
        store
            .put("peer", "c", br#"{"session":"s-1","persona":"P"}"#)
            .unwrap();
        store.put("peer", "d", br#"{"count":4}"#).unwrap();
        let read = rows::<Row>(store.as_mut(), "peer", &left, &log).unwrap();
        let keys: Vec<&str> = read.read.iter().map(|(key, _)| key.as_str()).collect();
        assert_eq!(keys, ["a", "d"]);
        assert_eq!(read.read[1].1, Row { count: 4 });
        // What names an agent keeps its names.
        let held = Name {
            session: "s-1".to_string(),
            persona: "P".to_string(),
            ..Name::default()
        };
        assert_eq!(read.names, [held]);
        assert_eq!(left.counts().get("peer"), Some(&2));
        // Said once each, by kind and key, and never what is in it; read again, said no more.
        let lines = said.lock().unwrap().clone();
        assert_eq!(
            lines,
            [
                "clarp-host: a record of the kind peer could not be read and is left out: b",
                "clarp-host: a record of the kind peer could not be read and is left out: c",
            ]
        );
        rows::<Row>(store.as_mut(), "peer", &left, &log).unwrap();
        assert_eq!(said.lock().unwrap().len(), 2);
        assert_eq!(
            left.line(),
            "clarp-host: 2 records read at the start could not be read and are left out (peer 2)"
        );
    }

    #[test]
    fn a_delete_of_a_key_left_out_is_said_as_a_delete_and_a_retire_in_its_own_words() {
        let (log, said) = log();
        let left = Arc::new(LeftOut::default());
        let mut inner = store();
        inner.put("peer", "b", b"no json").unwrap();
        rows::<Row>(inner.as_mut(), "peer", &left, &log).unwrap();
        let mut store = Watched::new(inner, left.clone(), log.clone());
        store.delete("peer", "b").unwrap();
        assert!(left.counts().is_empty());
        left.retire_later("agent-ledger", "gone-1", &log);
        left.not_run("opencode");
        left.not_run("opencode");
        assert_eq!(left.not_run_counts().get("opencode"), Some(&2));
        let lines = said.lock().unwrap().clone();
        assert!(
            lines
                .iter()
                .any(|s| s.ends_with("could not be read was deleted: b")),
            "{lines:?}"
        );
        assert!(
            lines.iter().any(|s| s
                .ends_with("could not be retired now and are retired at a later start: gone-1")),
            "{lines:?}"
        );
        assert!(
            !lines.iter().any(|s| s.contains("written anew")),
            "{lines:?}"
        );
    }

    #[derive(Debug, Default, serde::Deserialize, serde::Serialize, PartialEq)]
    #[serde(default)]
    struct Book {
        rows: Vec<Row>,
        by_name: std::collections::BTreeMap<String, Row>,
        note: String,
    }

    #[test]
    fn a_books_entries_that_do_not_fit_are_carried_through_and_put_back_as_they_came() {
        use super::{carry, fits_carried, saved, unfit};
        use serde_json::json;
        // A document that is an object: one item of a list and one entry of a map do not fit.
        let stored = json!({
            "rows": [{"count": 1}, {"count": "many"}, {"count": 3}],
            "by_name": {"a": {"count": 1}, "b": {"nothing": true}},
            "note": "kept",
        });
        let bytes = serde_json::to_vec(&stored).unwrap();
        assert!(!super::fits::<Book>(&bytes));
        assert!(fits_carried::<Book, serde_json::Value>(
            &bytes,
            &["rows", "by_name"]
        ));
        assert_eq!(
            unfit::<Book, serde_json::Value>(&bytes, &["rows", "by_name"]),
            ["rows[0]", "by_name.b"]
        );
        let mut document = stored.clone();
        let carried = carry::<Book, serde_json::Value>(&mut document, &["rows", "by_name"]);
        let book: Book = serde_json::from_value(document).unwrap();
        assert_eq!(book.rows, [Row { count: 1 }, Row { count: 3 }]);
        assert_eq!(book.note, "kept");
        // Saved, what was carried is there again as it came.
        let back: serde_json::Value = serde_json::from_slice(&saved(&book, &carried)).unwrap();
        assert!(
            back["rows"]
                .as_array()
                .unwrap()
                .contains(&json!({"count": "many"}))
        );
        assert_eq!(back["by_name"]["b"], json!({"nothing": true}));
        assert_eq!(back["by_name"]["a"], json!({"count": 1}));
        // A document that is one list.
        let list = serde_json::to_vec(&json!([{"count": 1}, "no row", {"count": 2}])).unwrap();
        assert_eq!(unfit::<Vec<Row>, Row>(&list, &[]), ["[0]"]);
        let mut document: serde_json::Value = serde_json::from_slice(&list).unwrap();
        let carried = carry::<Vec<Row>, Row>(&mut document, &[]);
        let rows: Vec<Row> = serde_json::from_value(document).unwrap();
        assert_eq!(rows.len(), 2);
        let back: serde_json::Value = serde_json::from_slice(&saved(&rows, &carried)).unwrap();
        assert_eq!(back.as_array().unwrap().len(), 3);
    }

    #[test]
    fn an_entry_its_desk_writes_anew_is_carried_no_more() {
        use super::carry;
        use serde_json::json;
        let mut document = json!({"rows": [], "by_name": {"b": {"nothing": true}}, "note": ""});
        let mut carried = carry::<Book, serde_json::Value>(&mut document, &["rows", "by_name"]);
        let mut book: Book = serde_json::from_value(document).unwrap();
        // Another key written: the entry carried stays.
        book.by_name.insert("a".into(), Row { count: 1 });
        let other = carried.written_over(&serde_json::to_value(&book).unwrap());
        assert!(other.is_empty());
        // Its own key written by the desk: that write is its repair.
        book.by_name.insert("b".into(), Row { count: 2 });
        let over = carried.written_over(&serde_json::to_value(&book).unwrap());
        assert_eq!(over, [("by_name".to_string(), "b".to_string())]);
        carried.forget("by_name", "b");
        assert!(carried.labels().is_empty());
    }

    #[test]
    fn a_desks_own_key_that_ends_like_a_copy_is_its_record() {
        let (log, _) = log();
        let left = Arc::new(LeftOut::default());
        let mut inner = store();
        // A peer named so, never set aside: read as the peer it is.
        inner
            .put("peer", "p.unreadable", br#"{"count":1}"#)
            .unwrap();
        let read = rows::<Row>(inner.as_mut(), "peer", &left, &log).unwrap();
        assert_eq!(read.read.len(), 1);
        // A place under such a key is no copy: the delete of its neighbour does not take it.
        inner.put("kept.place", "s", b"{}").unwrap();
        inner.put("kept.place", "s.unreadable", b"{}").unwrap();
        let mut store = Watched::new(inner, left, log);
        store.delete("kept.place", "s").unwrap();
        assert!(store.get("kept.place", "s.unreadable").unwrap().is_some());
    }

    #[test]
    fn nothing_left_out_counts_nothing() {
        let left = LeftOut::default();
        assert!(left.counts().is_empty());
        assert_eq!(
            left.line(),
            "clarp-host: every record read at the start could be read"
        );
    }

    #[test]
    fn no_sign_of_a_secret_key_reaches_the_log() {
        let (log, said) = log();
        let left = Arc::new(LeftOut::default());
        let secret = "0123456789abcdefghijklmnopqrstuv";
        let other = "zyxwvutsrqponmlkjihgfedcba987654";
        // A push token (declared secret), and a key of a kind nobody declared.
        left.left_out("push", secret, &log);
        left.left_out("push", other, &log);
        left.left_out("kept.nobody-declared-this", secret, &log);
        left.set_aside("kept.opening_by_token", secret, &log);
        let mut inner = store();
        inner.put("push", secret, b"[]").unwrap();
        let mut store = Watched::new(inner, left.clone(), log.clone());
        store.put("push", secret, b"{}").unwrap();
        store.delete("push", other).unwrap();
        let lines = said.lock().unwrap().clone();
        assert_eq!(
            lines[..2],
            [
                "clarp-host: a record of the kind push could not be read and is left out: 1 of this kind so far",
                "clarp-host: a record of the kind push could not be read and is left out: 2 of this kind so far",
            ]
        );
        assert_eq!(lines.len(), 6, "{lines:?}");
        // Not one sign of either key, not even the first.
        for line in &lines {
            for key in [secret, other] {
                assert!(!line.contains(&key[..2]), "{line}");
            }
        }
        // A plain key is printed whole.
        left.left_out("peer", "far-away", &log);
        assert!(
            said.lock()
                .unwrap()
                .last()
                .unwrap()
                .ends_with("left out: far-away")
        );
    }

    #[test]
    fn the_walk_counts_every_kind_and_a_books_own_write_repairs_nothing() {
        let (log, _) = log();
        let left = Arc::new(LeftOut::default());
        let mut inner = store();
        inner.put("agent", "agent-a", b"no json").unwrap();
        inner.put("kept.schedule", "all", b"{ not a list").unwrap();
        // The walk alone, no desk reading after it: the row is left out, the book moved aside.
        walk(inner.as_mut(), &left, &log).unwrap();
        assert!(left.is_left_out("agent", "agent-a"));
        assert!(left.is_left_out("kept.schedule", "all.unreadable"));
        // A row of a book its desk left out (a janitors' row not of its table) is written by the
        // Host itself: that repairs nothing, and it is still counted.
        left.left_out("kept.janitor-run", "r-1", &log);
        let mut store = Watched::new(inner, left.clone(), log);
        store.put("kept.janitor-run", "r-1", b"{}").unwrap();
        let writes = vec![(
            "kept.janitor-run".to_string(),
            "r-1".to_string(),
            Some(b"{}".to_vec()),
        )];
        store.put_together(&writes).unwrap();
        assert!(left.is_left_out("kept.janitor-run", "r-1"));
        assert_eq!(left.counts().get("janitor-run"), Some(&1));
    }

    #[test]
    fn a_books_record_is_moved_aside_never_over_a_copy_and_goes_with_its_key() {
        let (log, said) = log();
        let left = Arc::new(LeftOut::default());
        let mut inner = store();
        inner.put("kept.schedule", "all", b"first").unwrap();
        walk(inner.as_mut(), &left, &log).unwrap();
        // Moved: the key is free, the copy stands, and it is in the register.
        assert_eq!(inner.get("kept.schedule", "all").unwrap(), None);
        assert_eq!(
            inner
                .get("kept.schedule", "all.unreadable")
                .unwrap()
                .as_deref(),
            Some(&b"first"[..])
        );
        // A second under the same key takes the next free name: the first copy is not written over.
        inner.put("kept.schedule", "all", b"second").unwrap();
        walk(inner.as_mut(), &left, &log).unwrap();
        assert_eq!(
            inner
                .get("kept.schedule", "all.unreadable")
                .unwrap()
                .as_deref(),
            Some(&b"first"[..])
        );
        assert_eq!(
            inner
                .get("kept.schedule", "all.unreadable.2")
                .unwrap()
                .as_deref(),
            Some(&b"second"[..])
        );
        // At a later start the copies are counted again, as long as they stand.
        let later = Arc::new(LeftOut::default());
        walk(inner.as_mut(), &later, &log).unwrap();
        assert_eq!(later.counts().get("schedule"), Some(&2));
        // A delete of the key takes its copies along, in one delete said once.
        let mut store = Watched::new(inner, later.clone(), log);
        store.put("kept.schedule", "all", b"[]").unwrap();
        store.delete("kept.schedule", "all").unwrap();
        assert_eq!(store.get("kept.schedule", "all.unreadable").unwrap(), None);
        assert_eq!(
            store.get("kept.schedule", "all.unreadable.2").unwrap(),
            None
        );
        assert!(store.list("kept.set-aside").unwrap().is_empty());
        assert!(later.counts().is_empty());
        let lines = said.lock().unwrap().clone();
        let deleted = lines
            .iter()
            .filter(|line| line.contains("was deleted with the 2 set aside beside it: all"))
            .count();
        assert_eq!(deleted, 1);
    }

    #[test]
    fn the_owners_write_repairs_a_record_and_the_rest_stays_as_it_came() {
        let (log, said) = log();
        let left = Arc::new(LeftOut::default());
        let mut inner = store();
        inner.put("peer", "b", b"no json").unwrap();
        inner.put("peer", "c", b"nor this").unwrap();
        rows::<Row>(inner.as_mut(), "peer", &left, &log).unwrap();
        let mut store = Watched::new(inner, left.clone(), log);
        // Another key written: what was left out is untouched, and still counted.
        store.put("peer", "a", br#"{"count":1}"#).unwrap();
        assert_eq!(
            store.get("peer", "c").unwrap().as_deref(),
            Some(&b"nor this"[..])
        );
        assert_eq!(left.counts().get("peer"), Some(&2));
        // The owner writes one anew: said, and no longer counted.
        store.put("peer", "b", br#"{"count":2}"#).unwrap();
        assert_eq!(left.counts().get("peer"), Some(&1));
        assert!(!left.is_left_out("peer", "b") && left.is_left_out("peer", "c"));
        let writes = vec![("peer".to_string(), "c".to_string(), Some(b"{}".to_vec()))];
        store.put_together(&writes).unwrap();
        assert!(left.counts().is_empty());
        let lines = said.lock().unwrap().clone();
        assert!(
            lines
                .iter()
                .any(|s| s.ends_with("could not be read was written anew: b"))
        );
        assert!(
            lines
                .iter()
                .any(|s| s.ends_with("could not be read was written anew: c"))
        );
    }
}
