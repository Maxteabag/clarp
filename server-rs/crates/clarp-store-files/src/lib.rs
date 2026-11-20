//! Whole files on disk: one file for each name, in a directory named after the first two
//! characters of the name, so that no directory grows without bound.
//!
//! A file is written beside its place under a name nothing reads, forced to disk, moved
//! into place, and the directory is forced to disk after it: when `put` returns, the file is
//! there after a crash, and it is whole. What was being written when the process died stays
//! beside its place, is never read, and is removed when the store is next opened.

use std::fs::{self, File};
use std::io::{self, ErrorKind, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};

use clarp_ports::blobs::{BlobEngine, Blobs, is_name, not_a_name};
use clarp_ports::kv::StoreError;

/// How a file that is still being written ends. No name a store takes begins with a dot,
/// and every file in the making does.
const PART: &str = ".part";

#[derive(Debug, Default, Clone, Copy)]
pub struct FileBlobs;

impl FileBlobs {
    pub fn new() -> Self {
        Self
    }
}

fn failed(what: &Path, error: &io::Error) -> StoreError {
    StoreError(format!("{}: {error}", what.display()))
}

impl BlobEngine for FileBlobs {
    fn name(&self) -> &'static str {
        "files"
    }

    fn open(&self, path: &Path) -> Result<Arc<dyn Blobs>, StoreError> {
        fs::create_dir_all(path).map_err(|error| failed(path, &error))?;
        let store = Directory {
            root: path.to_path_buf(),
            written: AtomicU64::new(0),
        };
        store.sweep();
        Ok(Arc::new(store))
    }
}

struct Directory {
    root: PathBuf,
    /// How many files this store has begun to write: no two are written under one name.
    written: AtomicU64,
}

impl Directory {
    /// Where the file with this name lies.
    fn place(&self, name: &str) -> PathBuf {
        let shard: String = name.chars().take(2).collect();
        self.root.join(shard).join(name)
    }

    /// The name a file of this process is written under before it is moved into place.
    fn in_the_making(&self, name: &str) -> String {
        let count = self.written.fetch_add(1, Ordering::Relaxed);
        format!(".{name}.{}-{count}{PART}", std::process::id())
    }

    /// Removes what a process that is gone left half written. What this process is writing
    /// is left alone.
    fn sweep(&self) {
        let ours = format!(".{}-", std::process::id());
        let Ok(shards) = fs::read_dir(&self.root) else {
            return;
        };
        for shard in shards.flatten() {
            let Ok(files) = fs::read_dir(shard.path()) else {
                continue;
            };
            for file in files.flatten() {
                let name = file.file_name();
                let name = name.to_string_lossy();
                let left = name.starts_with('.') && name.ends_with(PART);
                if left && !name.contains(&ours) {
                    // Best effort: a leftover that stays is in nobody's way.
                    let _ = fs::remove_file(file.path());
                }
            }
        }
    }

    fn write(&self, name: &str, bytes: &[u8], place: &Path) -> io::Result<()> {
        let no_parent = || io::Error::new(ErrorKind::InvalidInput, "a file has a directory");
        let dir = place.parent().ok_or_else(no_parent)?;
        if !dir.is_dir() {
            fs::create_dir_all(dir)?;
            // The directory itself has to be there after a crash, too.
            File::open(&self.root)?.sync_all()?;
        }
        let part = dir.join(self.in_the_making(name));
        let moved = (|| {
            let mut file = File::create(&part)?;
            file.write_all(bytes)?;
            file.sync_all()?;
            fs::rename(&part, place)?;
            File::open(dir)?.sync_all()
        })();
        if moved.is_err() {
            let _ = fs::remove_file(&part);
        }
        moved
    }
}

impl Blobs for Directory {
    fn names_with_prefix(&self, prefix: &str) -> Result<Vec<String>, StoreError> {
        if prefix.len() < 2 || !is_name(prefix) {
            return Err(not_a_name(prefix));
        }
        let dir = self.root.join(&prefix[..2]);
        let entries = match fs::read_dir(&dir) {
            Ok(entries) => entries,
            Err(e) if e.kind() == ErrorKind::NotFound => return Ok(Vec::new()),
            Err(e) => return Err(failed(&dir, &e)),
        };
        let mut names = Vec::new();
        for entry in entries {
            let entry = entry.map_err(|e| failed(&dir, &e))?;
            if let Some(name) = entry.file_name().to_str()
                && name.starts_with(prefix) && is_name(name) {
                names.push(name.to_owned());
            }
        }
        Ok(names)
    }

    fn remove(&self, name: &str) -> Result<(), StoreError> {
        if !is_name(name) { return Err(not_a_name(name)); }
        let file = self.place(name);
        match fs::remove_file(&file) {
            Ok(()) => Ok(()),
            Err(e) if e.kind() == ErrorKind::NotFound => Ok(()),
            Err(e) => Err(failed(&file, &e)),
        }
    }

    fn put(&self, name: &str, bytes: &[u8]) -> Result<(), StoreError> {
        if !is_name(name) {
            return Err(not_a_name(name));
        }
        let place = self.place(name);
        if place.is_file() {
            return Ok(());
        }
        self.write(name, bytes, &place)
            .map_err(|error| failed(&place, &error))
    }

    fn len(&self, name: &str) -> Result<Option<u64>, StoreError> {
        if !is_name(name) {
            return Ok(None);
        }
        let place = self.place(name);
        match fs::metadata(&place) {
            Ok(found) if found.is_file() => Ok(Some(found.len())),
            Ok(_) => Ok(None),
            Err(error) if error.kind() == ErrorKind::NotFound => Ok(None),
            Err(error) => Err(failed(&place, &error)),
        }
    }

    fn read(&self, name: &str, offset: u64, most: usize) -> Result<Vec<u8>, StoreError> {
        if !is_name(name) || most == 0 {
            return Ok(Vec::new());
        }
        let place = self.place(name);
        let mut file = match File::open(&place) {
            Ok(file) => file,
            Err(error) if error.kind() == ErrorKind::NotFound => return Ok(Vec::new()),
            Err(error) => return Err(failed(&place, &error)),
        };
        let mut part = Vec::new();
        file.seek(SeekFrom::Start(offset))
            .and_then(|_| {
                Read::by_ref(&mut file)
                    .take(most as u64)
                    .read_to_end(&mut part)
            })
            .map_err(|error| failed(&place, &error))?;
        Ok(part)
    }
}
pub mod diagnostics;
