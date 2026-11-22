//! File arguments and the read-only Python snapshot shared by the offline import commands.

use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};

use rusqlite::{Connection, OpenFlags};

pub(super) struct Files {
    pub source: PathBuf,
    pub target: PathBuf,
    pub dry_run: bool,
}

impl Files {
    pub fn parse(
        mut args: impl Iterator<Item = String>,
        command: &str,
        offers_dry_run: bool,
    ) -> Result<Self, String> {
        let (mut source, mut data) = (None, None);
        let mut dry_run = false;
        while let Some(arg) = args.next() {
            let path = match arg.as_str() {
                "--from-db" if source.is_none() => &mut source,
                "--data" if data.is_none() => &mut data,
                "--dry-run" if offers_dry_run && !dry_run => {
                    dry_run = true;
                    continue;
                }
                _ => return Err(format!("invalid or repeated {command} argument")),
            };
            *path = Some(PathBuf::from(
                args.next()
                    .ok_or_else(|| format!("a {command} path is missing"))?,
            ));
        }
        let source = source.ok_or_else(|| format!("{command} requires --from-db PATH"))?;
        let target = data
            .ok_or_else(|| format!("{command} requires --data DIR"))?
            .join("fleet.db");
        let source = source
            .canonicalize()
            .map_err(|_| "the Python state database was not found")?;
        let before = source
            .metadata()
            .map_err(|_| "the Python state database could not be inspected")?;
        let after = target
            .metadata()
            .map_err(|_| "the Rust fleet.db was not found")?;
        if !before.is_file() || !after.is_file() {
            return Err("the source and destination must be database files".into());
        }
        if (before.dev(), before.ino()) == (after.dev(), after.ino()) {
            return Err(
                "the Python state database and Rust fleet.db must be different files".into(),
            );
        }
        Ok(Self {
            source,
            target,
            dry_run,
        })
    }
}

pub(super) fn source(path: &Path) -> Result<Connection, String> {
    // Python has stopped. Use an in-memory WAL index to read uncheckpointed commits
    // without opening or creating its -shm file.
    let conn = Connection::open_with_flags_and_vfs(
        path,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
        "unix-none",
    )
    .map_err(|_| "the Python state database could not be opened read-only")?;
    conn.execute_batch("PRAGMA locking_mode=EXCLUSIVE;")
        .map_err(|_| "the Python WAL could not be opened without shared memory")?;
    Ok(conn)
}
