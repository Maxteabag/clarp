//! The same peers.json and writer lock used by the Python Host.

use std::collections::BTreeMap;
use std::fs::{self, OpenOptions};
use std::io::{self, Write};
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::path::Path;

use nix::fcntl::{Flock, FlockArg};
use serde_json::{Map, Value};

use crate::fleet::Peer;

pub(super) fn read(path: &Path, legacy: &BTreeMap<String, Peer>) -> io::Result<Map<String, Value>> {
    match fs::read(path) {
        Ok(bytes) => serde_json::from_slice(&bytes).map_err(io::Error::other),
        // Keep older Rust Hosts working. The first edit writes the whole list in the shared
        // format, and an existing peers.json (including an empty one) always takes precedence.
        Err(error) if error.kind() == io::ErrorKind::NotFound => legacy
            .iter()
            .map(|(name, peer)| Ok((name.clone(), serde_json::to_value(peer)?)))
            .collect::<Result<_, serde_json::Error>>()
            .map_err(io::Error::other),
        Err(error) => Err(error),
    }
}

pub(super) fn edit<T>(
    path: &Path,
    legacy: &BTreeMap<String, Peer>,
    change: impl FnOnce(&mut Map<String, Value>) -> T,
) -> io::Result<T> {
    let directory = path.parent().ok_or(io::ErrorKind::InvalidInput)?;
    fs::create_dir_all(directory)?;
    let lock = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .mode(0o600)
        .open(path.with_extension("json.lock"))?;
    let _lock =
        Flock::lock(lock, FlockArg::LockExclusive).map_err(|(_, error)| io::Error::from(error))?;
    let mut peers = read(path, legacy)?;
    let answer = change(&mut peers);
    let temporary = directory.join(format!(".peers.{}.next", crate::ids::hex(16)));
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&temporary)?;
    let written = (|| {
        file.set_permissions(fs::Permissions::from_mode(0o600))?;
        serde_json::to_writer_pretty(&mut file, &peers)?;
        file.write_all(b"\n")?;
        file.sync_all()?;
        fs::rename(&temporary, path)
    })();
    if written.is_err() {
        let _ = fs::remove_file(&temporary);
    }
    written.map(|()| answer)
}
