//! What the Host keeps, read back: one loader, with a declared policy, for every kind.
//!
//! A record that cannot be read never stops the Host: it is LEFT OUT of what the Host does,
//! never written over or deleted by the Host, counted by its kind, and named in the journal by
//! its kind and key, never by what is in it. The count is shown at the Host's own door (`GET
//! /host/start`, the administrator's) and in one line when the Host starts, so that an
//! installation still finds a changed shape: by a number, not by a Host that does not start.
//! Only the configuration, the listener, a store that does not answer and a core thread that
//! does not start stop a start.
//!
//! The one write a left-out key may get is the owner's own: a phone that registers the same
//! token again, a peer added again under its name. That write is the repair: it is said in the
//! journal, and the record is no longer counted. Nothing else writes or deletes such a key.
//!
//! The policies: `rows`, a kind whose every record stands alone (each is read alone, and a bad
//! one is left out); books, kept in one document, follow with their own piece.

use std::collections::{BTreeMap, BTreeSet};
use std::sync::{Arc, Mutex};

use clarp_ports::kv::{KeyValue, StoreError, Write};
use serde::de::DeserializeOwned;
use serde_json::Value;

use crate::unkept::Log;

/// What is said of a record: its kind, without the shelf the store files it under.
pub fn kind_of(table: &str) -> &str {
    table.strip_prefix("kept.").unwrap_or(table)
}

/// How a key is named in the journal: in full, but where the key itself is a secret (a push
/// token), only its first signs.
fn named(table: &str, key: &str) -> String {
    if kind_of(table) == "push" {
        let first: String = key.chars().take(8).collect();
        format!("{first}…")
    } else {
        key.to_string()
    }
}

/// What the Host left out, by the table and key it is under: what the door counts.
#[derive(Default)]
pub struct LeftOut {
    held: Mutex<BTreeMap<String, BTreeSet<String>>>,
}

impl LeftOut {
    /// A record that cannot be read: counted, and said once by its kind and key.
    pub fn left_out(&self, table: &str, key: &str, log: &Log) {
        let new = self.held.lock().is_ok_and(|mut held| {
            held.entry(table.to_string())
                .or_default()
                .insert(key.to_string())
        });
        if new {
            log.line(&format!(
                "clarp-host: a record of the kind {} could not be read and is left out: {}",
                kind_of(table),
                named(table, key)
            ));
        }
    }

    /// A write to a key that was left out: the owner's own, which repairs it. Answers whether
    /// it was one.
    pub fn written(&self, table: &str, key: &str, log: &Log) -> bool {
        let was = self.held.lock().is_ok_and(|mut held| {
            let Some(keys) = held.get_mut(table) else {
                return false;
            };
            let was = keys.remove(key);
            if keys.is_empty() {
                held.remove(table);
            }
            was
        });
        if was {
            log.line(&format!(
                "clarp-host: a record of the kind {} that could not be read was written anew: {}",
                kind_of(table),
                named(table, key)
            ));
        }
        was
    }

    /// A record a desk left out is used after all (a call's binding recovered once its agent
    /// answered): no longer counted, and said.
    pub fn recovered(&self, table: &str, key: &str, log: &Log) {
        let was = self.held.lock().is_ok_and(|mut held| {
            let was = held.get_mut(table).is_some_and(|keys| keys.remove(key));
            held.retain(|_, keys| !keys.is_empty());
            was
        });
        if was {
            log.line(&format!(
                "clarp-host: a record of the kind {} that was left out is used after all: {}",
                kind_of(table),
                named(table, key)
            ));
        }
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
            return "clarp-host: every record it keeps could be read".to_string();
        }
        let total: u64 = counts.values().sum();
        let each: Vec<String> = counts
            .iter()
            .map(|(kind, count)| format!("{kind} {count}"))
            .collect();
        format!(
            "clarp-host: {total} records could not be read and are left out ({})",
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

/// What a kind of rows came to: the records that could be read, by key, and the names held by
/// those that could not.
pub struct Rows<T> {
    pub read: Vec<(String, T)>,
    pub names: Vec<Name>,
}

/// Reads a kind whose every record stands alone. A store that does not answer is an error (it
/// stops a start); a record that cannot be read is left out, counted and said.
pub fn rows<T: DeserializeOwned>(
    store: &dyn KeyValue,
    table: &str,
    left: &LeftOut,
    log: &Log,
) -> Result<Rows<T>, StoreError> {
    Ok(listed(store.list(table)?, table, left, log))
}

/// The same, of rows a desk already has as they were kept (`FleetHandle::kept_all`): `table`
/// is the table they were listed from.
pub fn listed<T: DeserializeOwned>(
    kept: Vec<(String, Vec<u8>)>,
    table: &str,
    left: &LeftOut,
    log: &Log,
) -> Rows<T> {
    let mut rows = Rows {
        read: Vec::new(),
        names: Vec::new(),
    };
    for (key, stored) in kept {
        match serde_json::from_slice::<T>(&stored) {
            Ok(record) => rows.read.push((key, record)),
            Err(_) => {
                left.left_out(table, &key, log);
                rows.names.extend(Name::of(&stored));
            }
        }
    }
    rows
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

impl KeyValue for Watched {
    fn get(&self, table: &str, key: &str) -> Result<Option<Vec<u8>>, StoreError> {
        self.inner.get(table, key)
    }

    fn put(&mut self, table: &str, key: &str, value: &[u8]) -> Result<(), StoreError> {
        self.inner.put(table, key, value)?;
        self.left.written(table, key, &self.log);
        Ok(())
    }

    fn put_together(&mut self, writes: &[Write]) -> Result<(), StoreError> {
        self.inner.put_together(writes)?;
        for (table, key, _) in writes {
            self.left.written(table, key, &self.log);
        }
        Ok(())
    }

    fn delete(&mut self, table: &str, key: &str) -> Result<bool, StoreError> {
        let was = self.inner.delete(table, key)?;
        self.left.written(table, key, &self.log);
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

    use super::{LeftOut, Name, Watched, rows};
    use crate::unkept::Log;

    #[derive(Debug, Deserialize, PartialEq)]
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
        store.put("kept.thing", "a", br#"{"count":1}"#).unwrap();
        store.put("kept.thing", "b", b"no json").unwrap();
        store
            .put("kept.thing", "c", br#"{"session":"s-1","persona":"P"}"#)
            .unwrap();
        store.put("kept.thing", "d", br#"{"count":4}"#).unwrap();
        let read = rows::<Row>(store.as_ref(), "kept.thing", &left, &log).unwrap();
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
        assert_eq!(left.counts().get("thing"), Some(&2));
        // Said once each, by kind and key, and never what is in it; read again, said no more.
        let lines = said.lock().unwrap().clone();
        assert_eq!(
            lines,
            [
                "clarp-host: a record of the kind thing could not be read and is left out: b",
                "clarp-host: a record of the kind thing could not be read and is left out: c",
            ]
        );
        rows::<Row>(store.as_ref(), "kept.thing", &left, &log).unwrap();
        assert_eq!(said.lock().unwrap().len(), 2);
        assert_eq!(
            left.line(),
            "clarp-host: 2 records could not be read and are left out (thing 2)"
        );
    }

    #[test]
    fn nothing_left_out_counts_nothing() {
        let left = LeftOut::default();
        assert!(left.counts().is_empty());
        assert_eq!(
            left.line(),
            "clarp-host: every record it keeps could be read"
        );
    }

    #[test]
    fn a_push_token_is_said_by_its_first_signs_only() {
        let (log, said) = log();
        let left = LeftOut::default();
        left.left_out("push", "0123456789abcdef", &log);
        let lines = said.lock().unwrap().clone();
        assert_eq!(
            lines,
            ["clarp-host: a record of the kind push could not be read and is left out: 01234567…"]
        );
    }

    #[test]
    fn the_owners_write_repairs_a_record_and_the_rest_stays_as_it_came() {
        let (log, said) = log();
        let left = Arc::new(LeftOut::default());
        let mut inner = store();
        inner.put("kept.thing", "b", b"no json").unwrap();
        inner.put("kept.thing", "c", b"nor this").unwrap();
        rows::<Row>(inner.as_ref(), "kept.thing", &left, &log).unwrap();
        let mut store = Watched::new(inner, left.clone(), log);
        // Another key written: what was left out is untouched, and still counted.
        store.put("kept.thing", "a", br#"{"count":1}"#).unwrap();
        assert_eq!(
            store.get("kept.thing", "c").unwrap().as_deref(),
            Some(&b"nor this"[..])
        );
        assert_eq!(left.counts().get("thing"), Some(&2));
        // The owner writes one anew: said, and no longer counted.
        store.put("kept.thing", "b", br#"{"count":2}"#).unwrap();
        assert_eq!(left.counts().get("thing"), Some(&1));
        assert!(!left.is_left_out("kept.thing", "b") && left.is_left_out("kept.thing", "c"));
        let writes = vec![(
            "kept.thing".to_string(),
            "c".to_string(),
            Some(b"{}".to_vec()),
        )];
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
