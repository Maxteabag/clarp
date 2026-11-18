//! A small durable table of named values, as a port: where the Host keeps the few facts that
//! belong to the fleet and to no single agent, such as which agents there are.
//!
//! It is deliberately small. An agent's history is its log; this is for what is looked up by
//! name and replaced as a whole. A value is bytes: who writes it owns what they mean.

use std::fmt;
use std::path::Path;

#[derive(Debug)]
pub struct StoreError(pub String);

impl fmt::Display for StoreError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "storage failed: {}", self.0)
    }
}

impl std::error::Error for StoreError {}

/// Named values in named tables. One owner writes it, which is why writing takes `&mut self`.
pub trait KeyValue: Send {
    fn get(&self, table: &str, key: &str) -> Result<Option<Vec<u8>>, StoreError>;

    /// Sets the value under a key, replacing the one that was there. When this returns `Ok`,
    /// the value survives a crash of the process.
    fn put(&mut self, table: &str, key: &str, value: &[u8]) -> Result<(), StoreError>;

    /// Commit related replacements atomically across named tables.
    fn put_together(&mut self, writes: &[(String, String, Vec<u8>)]) -> Result<(), StoreError>;

    /// Removes a key. Returns whether it was there.
    fn delete(&mut self, table: &str, key: &str) -> Result<bool, StoreError>;

    /// Every key of a table with its value, ordered by key.
    fn list(&self, table: &str) -> Result<Vec<(String, Vec<u8>)>, StoreError>;
}

/// A way of storing such tables.
pub trait KeyValueEngine: Send + Sync {
    /// The name this engine has in configuration.
    fn name(&self) -> &'static str;

    /// Opens the store at `path`, creating it when it is not there yet. Opening the same path
    /// again returns the same store.
    fn open(&self, path: &Path) -> Result<Box<dyn KeyValue>, StoreError>;
}
