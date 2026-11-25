//! What a lane process keeps for itself, whatever tool it holds: the options the Host gave the
//! lane rather than the tool, its locks, the running turn's identity, and its idle clock.

use std::fs::{self, File, OpenOptions};
use std::io::{self, Write};
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use clarp_shim::lane::{key, owner_lock};
use serde_json::Value;

/// What the Host starts a lane with that is the lane's own, taken out of the tool's options.
pub(super) struct Own {
    /// Where the locks of native threads are: the thread a lane starts is locked there.
    pub(super) thread_locks: Option<PathBuf>,
    /// A thread's lock, named by the Host for a lane that resumes it.
    pub(super) thread_lock: Option<PathBuf>,
    /// How long the lane lives with no claim and no work that may continue.
    pub(super) idle: Option<Duration>,
    /// Where the identity of the lane's running turn is kept for the Clarp tools it calls.
    pub(super) turn_file: Option<PathBuf>,
    /// The Host's digest of the options, told back in every snapshot.
    pub(super) digest: String,
    /// The only provider the lane's tool may run on, for an agent bound to one of its own.
    pub(super) provider: Option<String>,
}

impl Own {
    pub(super) fn take(options: &mut Value) -> Self {
        let Some(fields) = options.as_object_mut() else {
            return Self {
                thread_locks: None,
                thread_lock: None,
                idle: None,
                turn_file: None,
                digest: String::new(),
                provider: None,
            };
        };
        let path = |value: Option<Value>| value?.as_str().map(PathBuf::from);
        Self {
            thread_locks: path(fields.remove(key::THREAD_LOCK_DIR)),
            thread_lock: path(fields.remove(key::THREAD_LOCK)),
            idle: fields
                .remove(key::IDLE_MS)
                .and_then(|ms| ms.as_u64())
                .filter(|ms| *ms > 0)
                .map(Duration::from_millis),
            turn_file: path(fields.remove(key::TURN_FILE)),
            digest: fields
                .remove(key::OPTIONS_DIGEST)
                .and_then(|digest| digest.as_str().map(str::to_owned))
                .unwrap_or_default(),
            provider: fields
                .remove(key::MODEL_PROVIDER)
                .and_then(|provider| provider.as_str().map(str::to_owned))
                .filter(|provider| !provider.is_empty()),
        }
    }

    /// The identity of the turn the lane now runs, for the Clarp tools it calls: written whole
    /// by rename, readable by the owner only, so a tool never reads half of one. A file that
    /// cannot be written leaves the turn to run: its tools then present the one before.
    pub(super) fn keep_token(&self, token: Option<&str>, tool: &str) {
        let (Some(path), Some(token)) = (self.turn_file.as_deref(), token) else {
            return;
        };
        if let Err(error) = write_whole(path, token) {
            eprintln!("clarp-shim: {tool} lane: the turn's identity was not written: {error}");
        }
    }
}

fn write_whole(path: &Path, token: &str) -> io::Result<()> {
    let partial = path.with_extension(format!("{}.part", std::process::id()));
    // A file left by a lane of the same pid before a crash is not written through.
    match fs::remove_file(&partial) {
        Ok(()) => {}
        Err(error) if error.kind() == io::ErrorKind::NotFound => {}
        Err(error) => return Err(error),
    }
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&partial)?;
    file.write_all(token.as_bytes())?;
    file.sync_all()?;
    fs::rename(&partial, path)
}

/// A lock file taken exclusive, without waiting: a lock someone else holds is an error.
pub(super) fn lock_exclusive(path: &Path) -> io::Result<File> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .mode(0o600)
        .open(path)?;
    file.try_lock().map_err(io::Error::other)?;
    Ok(file)
}

/// The lane's owner lock, held by its process until its tool has been reaped: the Host knows a
/// lane has left by this lock being free. Its socket's parent is made first, and the process's
/// id is written into it.
pub(super) fn hold_owner(socket: &Path) -> io::Result<File> {
    if let Some(parent) = socket.parent() {
        fs::create_dir_all(parent)?;
    }
    let lock = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .mode(0o600)
        .open(owner_lock(socket))?;
    lock.try_lock().map_err(io::Error::other)?;
    // Its own id, written once the lock is held: the Host ends a lane that does not let go
    // when asked by this id and no other (issue #772).
    lock.set_len(0)?;
    (&lock).write_all(format!("{}\n", std::process::id()).as_bytes())?;
    Ok(lock)
}

/// How long the lane has been without a claim, and when it leaves for it.
pub(super) struct Idle {
    limit: Option<Duration>,
    since: Instant,
}

impl Idle {
    pub(super) fn new(limit: Option<Duration>) -> Self {
        Self {
            limit,
            since: Instant::now(),
        }
    }

    /// A claim was taken or let go: the idle time counts from now.
    pub(super) fn reset(&mut self) {
        self.since = Instant::now();
    }

    /// When a lane that may leave (`retirable`) leaves by itself.
    pub(super) fn deadline(&self, retirable: bool) -> Option<Instant> {
        self.limit
            .filter(|_| retirable)
            .map(|limit| self.since + limit)
    }

    /// True: a lane that may leave has been idle for its time.
    pub(super) fn over(&self, retirable: bool) -> bool {
        retirable
            && self
                .limit
                .is_some_and(|limit| self.since.elapsed() >= limit)
    }

    /// How long it has been idle, in milliseconds, as a status says it: none while it holds a
    /// claim.
    pub(super) fn ms(&self, claimed: bool) -> u64 {
        if claimed {
            0
        } else {
            u64::try_from(self.since.elapsed().as_millis()).unwrap_or(u64::MAX)
        }
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::{Idle, Own};
    use serde_json::json;
    use std::time::Duration;

    #[test]
    fn the_lanes_own_options_are_taken_out_and_the_tools_left() {
        let mut options = json!({
            "cwd": "/w",
            "_clarpThreadLockDir": "/run/codex-threads",
            "_clarpIdleMs": 900_000,
            "_clarpTurnFile": "/run/a-turn.token",
            "_clarpOptionsDigest": "d",
            "_clarpModelProvider": "",
        });
        let own = Own::take(&mut options);
        assert_eq!(options, json!({"cwd": "/w"}));
        assert_eq!(
            own.thread_locks.unwrap().to_str(),
            Some("/run/codex-threads")
        );
        assert_eq!(own.thread_lock, None);
        assert_eq!(own.idle, Some(Duration::from_secs(900)));
        assert_eq!(own.turn_file.unwrap().to_str(), Some("/run/a-turn.token"));
        assert_eq!(own.digest, "d");
        // An empty provider binds to none.
        assert_eq!(own.provider, None);
        // No idle time, or zero, keeps the lane until it is retired.
        assert_eq!(Own::take(&mut json!({"_clarpIdleMs": 0})).idle, None);
    }

    #[test]
    fn the_turn_file_is_written_whole_and_rewritten() {
        let dir = std::env::temp_dir().join(format!("lane-own-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("a-turn.token");
        let own = Own::take(&mut json!({"_clarpTurnFile": path}));
        own.keep_token(Some("one"), "Test");
        own.keep_token(None, "Test");
        assert_eq!(std::fs::read_to_string(&path).unwrap(), "one");
        own.keep_token(Some("two"), "Test");
        assert_eq!(std::fs::read_to_string(&path).unwrap(), "two");
        assert_eq!(std::fs::read_dir(&dir).unwrap().count(), 1);
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn an_idle_lane_leaves_only_when_it_may() {
        let idle = Idle::new(Some(Duration::ZERO));
        assert!(idle.over(true));
        assert!(!idle.over(false));
        assert!(idle.deadline(false).is_none());
        assert!(idle.deadline(true).is_some());
        assert_eq!(idle.ms(true), 0);
        let forever = Idle::new(None);
        assert!(!forever.over(true));
        assert!(forever.deadline(true).is_none());
    }
}
