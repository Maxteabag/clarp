//! Whole files as a port: where the Host keeps the bytes of what its agents publish, an
//! image, a recording, a document.
//!
//! A name stands for its bytes. Whoever keeps a file names it after what it is (a digest of
//! its bytes and the extension of its kind), so the same file kept twice is kept once, and
//! what is kept under a name is never written again. A file is kept whole and read back in
//! parts: a player asks for the part of a recording it is about to play.
//!
//! Many may read and keep at the same time, which is why nothing here takes `&mut self`.

use std::path::Path;
use std::sync::Arc;

use crate::kv::StoreError;

/// The longest name a store takes.
pub const NAME_MAX: usize = 200;

/// Whether this is a name a store takes: letters, digits, dots, dashes and underscores, not
/// beginning with a dot. Nothing that could be read as a path.
pub fn is_name(name: &str) -> bool {
    let plain = |byte: u8| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'-' | b'_');
    !name.is_empty() && name.len() <= NAME_MAX && !name.starts_with('.') && name.bytes().all(plain)
}

/// What a store answers to a name it does not take.
pub fn not_a_name(name: &str) -> StoreError {
    let shown: String = name.chars().take(40).collect();
    StoreError(format!("`{shown}` is not a name a file can be kept under"))
}

/// Files kept whole under names.
pub trait Blobs: Send + Sync {
    /// Keeps these bytes under this name. When this returns `Ok`, they survive a crash of the
    /// process. A name that is kept already is left as it is.
    fn put(&self, name: &str, bytes: &[u8]) -> Result<(), StoreError>;

    /// How many bytes are kept under a name: `None` when nothing is, and for what is no name.
    fn len(&self, name: &str) -> Result<Option<u64>, StoreError>;

    /// The bytes from `offset` on, `most` at most: fewer at the file's end, none past it, and
    /// none for a name under which nothing is kept.
    fn read(&self, name: &str, offset: u64, most: usize) -> Result<Vec<u8>, StoreError>;
}

/// A way of keeping such files.
pub trait BlobEngine: Send + Sync {
    /// The name this engine has in configuration.
    fn name(&self) -> &'static str;

    /// Opens the store at `path`, creating it when it is not there yet. Opening the same path
    /// again returns the same store.
    fn open(&self, path: &Path) -> Result<Arc<dyn Blobs>, StoreError>;
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_name_is_nothing_that_could_be_read_as_a_path() {
        for name in ["a", "ab12.png", "A-b_c.tar.gz", &"x".repeat(NAME_MAX)] {
            assert!(is_name(name), "{name}");
        }
        for name in [
            "",
            ".",
            "..",
            ".hidden",
            "a/b",
            "a\\b",
            "a b",
            "æ.png",
            "a\0b",
            &"x".repeat(NAME_MAX + 1),
        ] {
            assert!(!is_name(name), "{name:?}");
        }
        assert!(
            not_a_name("../../etc/passwd")
                .0
                .contains("../../etc/passwd")
        );
    }
}
