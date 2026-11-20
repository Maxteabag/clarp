//! A write that failed and that nothing waits on is said once in the Host's own log: the
//! kind of record and why, never what was to be written. A write a caller depends on returns
//! its failure instead; an answer to a caller who has gone is all that is left unsaid.
//!
//! The fleet says it ([`crate::fleet::FleetHandle::unkept`]), through the log it was started
//! with: the standard error, or what a test reads.
//!
//! Said once also where the store stays down: the same kind in the same words is said again
//! only a minute after it was last said, with how often it happened since. A full disk does
//! not fill the journal on it with a line for every write.

use std::collections::HashMap;
use std::sync::{Arc, Mutex, PoisonError};

use clarp_ports::clock::Clock;

/// How long the same failure is held back once it has been said.
pub const AGAIN_AFTER_MS: i64 = 60_000;
/// A failure not seen again for this long is forgotten: the next is new.
const FORGOTTEN_AFTER_MS: i64 = 10 * AGAIN_AFTER_MS;

/// A failure said, and how often it happened since.
struct Held {
    said_at: i64,
    since: u64,
}

/// Where the Host says what it could not keep.
#[derive(Clone)]
pub struct Log {
    say: Arc<dyn Fn(&str) + Send + Sync>,
    clock: Arc<dyn Clock + Send + Sync>,
    /// By kind and words.
    held: Arc<Mutex<HashMap<(String, String), Held>>>,
}

impl Log {
    /// The Host's own log: its standard error, beside its other lines.
    pub fn stderr(clock: Arc<dyn Clock + Send + Sync>) -> Self {
        Self::to(|line| eprintln!("{line}"), clock)
    }

    /// A log that hands each line to `said`, by the time `clock` tells.
    pub fn to(
        said: impl Fn(&str) + Send + Sync + 'static,
        clock: Arc<dyn Clock + Send + Sync>,
    ) -> Self {
        Self {
            say: Arc::new(said),
            clock,
            held: Arc::default(),
        }
    }

    pub(crate) fn line(&self, line: &str) {
        (self.say)(line);
    }

    /// The one way a failed write is said: the kind and the store's words. The first is said
    /// at once; the same again only a minute after, with how often it happened since.
    pub fn unkept(&self, kind: &str, why: &str) {
        let now = self.clock.now_ms();
        let line = format!("clarp-host: a record of the kind {kind} was not kept: {why}");
        let said = {
            let mut held = self.held.lock().unwrap_or_else(PoisonError::into_inner);
            held.retain(|_, failure| now - failure.said_at < FORGOTTEN_AFTER_MS);
            match held.get_mut(&(kind.to_string(), why.to_string())) {
                None => {
                    let first = Held {
                        said_at: now,
                        since: 0,
                    };
                    held.insert((kind.to_string(), why.to_string()), first);
                    Some(line)
                }
                Some(failure) if now - failure.said_at < AGAIN_AFTER_MS => {
                    failure.since += 1;
                    None
                }
                Some(failure) => {
                    let times = failure.since + 1;
                    failure.said_at = now;
                    failure.since = 0;
                    Some(if times > 1 {
                        format!("{line} ({times} times since it was last said)")
                    } else {
                        line
                    })
                }
            }
        };
        if let Some(line) = said {
            (self.say)(&line);
        }
    }
}

impl std::fmt::Debug for Log {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("Log")
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use std::path::Path;
    use std::sync::{Arc, Mutex};

    use clarp_ports::clock::ManualClock;

    /// The calls that write what the Host keeps. A `let _ =` that throws one's result away
    /// says nothing when the write fails.
    const WRITES: [&str; 14] = [
        "keep",
        "keep_register",
        "remove",
        "open",
        "set",
        "delete",
        "put",
        "put_together",
        "save",
        "noted",
        "amend_any",
        "finish",
        "greeting_admitted",
        "change_together",
    ];

    /// The writes a text throws away with `let _ =`, each by its line and what it called up
    /// to its first parenthesis: `app.fleet.keep`. A write is a method of what keeps, or the
    /// Oracle's `keep`; a function of another name of its own (`put` of a socket) is none,
    /// and so is an answer to whoever asked (`answer.send`).
    fn thrown_away(text: &str) -> Vec<(usize, String)> {
        let mut found = Vec::new();
        for (at, _) in text.match_indices("let _ =") {
            let line = text[..at].lines().count();
            let rest = &text[at + "let _ =".len()..];
            let called: String = rest
                .split('(')
                .next()
                .unwrap_or("")
                .chars()
                .filter(|sign| !sign.is_whitespace())
                .collect();
            let last = called.rsplit(['.', ':']).next().unwrap_or("");
            let method = called.contains('.') || called.contains("::");
            if (method && WRITES.contains(&last)) || called == "keep" {
                found.push((line, called));
            }
        }
        found
    }

    fn sources(dir: &Path, into: &mut Vec<std::path::PathBuf>) {
        for entry in std::fs::read_dir(dir).unwrap() {
            let path = entry.unwrap().path();
            if path.is_dir() {
                sources(&path, into);
            } else if path.extension().is_some_and(|extension| extension == "rs") {
                into.push(path);
            }
        }
    }

    #[test]
    fn no_write_of_the_host_has_its_failure_thrown_away() {
        let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("src");
        let mut files = Vec::new();
        sources(&root, &mut files);
        assert!(files.len() > 50, "{}", root.display());
        // This file names what it looks for.
        files.retain(|file| !file.ends_with("unkept.rs"));
        let mut thrown = Vec::new();
        for file in files {
            let text = std::fs::read_to_string(&file).unwrap();
            for (line, called) in thrown_away(&text) {
                thrown.push(format!("{}:{line} {called}", file.display()));
            }
        }
        assert!(thrown.is_empty(), "{thrown:#?}");
    }

    #[test]
    fn a_write_thrown_away_is_found_and_nothing_else() {
        let text = [
            "x",
            "        let _ = app",
            "            .fleet",
            "            .keep(KIND, k, None)",
            "            .await;",
            "        let _ = answer.send(Ok(self.keep(kind, &key, value)));",
            "        let _ = std::fs::remove_dir_all(&dir);",
            "        let _ = self.store.delete(CODES, &digest);",
            "        let _ = keep(app, CONTACT_KEY, String::new()).await;",
            "        let _ = Self::keep_register(app, &held.register).await;",
            "        let _ = put(&mut sink, Message::Close(None)).await;",
            "        let _ = app.fleet.amend_any(agent_id, amend).await?;",
            "        let _ = member.agent.stop(FROM_A_JANITOR.to_string()).await;",
        ]
        .join("\n");
        assert_eq!(
            thrown_away(&text),
            [
                (2, "app.fleet.keep".to_string()),
                (8, "self.store.delete".to_string()),
                (9, "keep".to_string()),
                (10, "Self::keep_register".to_string()),
                (12, "app.fleet.amend_any".to_string()),
            ]
        );
        let line = Arc::new(Mutex::new(Vec::new()));
        let said = line.clone();
        let clock = Arc::new(clarp_ports::clock::SystemClock);
        let log = super::Log::to(
            move |text| said.lock().unwrap().push(text.to_string()),
            clock,
        );
        log.unkept("used", "disk full");
        assert_eq!(
            *line.lock().unwrap(),
            ["clarp-host: a record of the kind used was not kept: disk full"]
        );
    }

    #[test]
    fn a_failure_that_goes_on_is_said_again_a_minute_after_with_how_often() {
        let clock = Arc::new(ManualClock::starting_at(1_795_000_000_000));
        let lines = Arc::new(Mutex::new(Vec::<String>::new()));
        let said = lines.clone();
        let log = super::Log::to(
            move |text| said.lock().unwrap().push(text.to_string()),
            clock.clone(),
        );
        let line = |kind: &str, why: &str| {
            format!("clarp-host: a record of the kind {kind} was not kept: {why}")
        };
        // The first at once; forty more within the minute are held back.
        log.unkept("used", "disk full");
        for _ in 0..40 {
            clock.advance_ms(1_000);
            log.unkept("used", "disk full");
        }
        // Another kind, and other words, are other failures: said at once.
        log.unkept("chip", "disk full");
        log.unkept("used", "database is locked");
        // A minute after it was said: again, with how often since, this one counted.
        clock.advance_ms(20_000);
        log.unkept("used", "disk full");
        // A minute later, once: nothing was held back.
        clock.advance_ms(60_000);
        log.unkept("used", "disk full");
        // Held back again, within its minute.
        clock.advance_ms(59_999);
        log.unkept("used", "disk full");
        let full = line("used", "disk full");
        assert_eq!(
            *lines.lock().unwrap(),
            [
                full.clone(),
                line("chip", "disk full"),
                line("used", "database is locked"),
                format!("{full} (41 times since it was last said)"),
                full.clone(),
            ]
        );
        // Not seen for ten minutes, it is new again: said at once, alone.
        clock.advance_ms(10 * super::AGAIN_AFTER_MS);
        log.unkept("used", "disk full");
        assert_eq!(lines.lock().unwrap().len(), 6);
        assert_eq!(lines.lock().unwrap()[5], full);
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod at_the_port {
    use std::path::{Path, PathBuf};
    use std::sync::{Arc, Mutex};

    use axum::http::StatusCode;
    use clarp_ports::blobs::BlobEngine;
    use clarp_ports::clock::SystemClock;
    use clarp_ports::kv::{KeyValue, KeyValueEngine, StoreError, Write};
    use clarp_store_mem::{MemBlobs, MemEngine, MemKeyValue};

    use super::Log;
    use crate::config::Config;
    use crate::edge::{ApiError, not_to_stay};
    use crate::fleet::{self, AgentRecord, FleetHandle};
    use crate::hub::Hub;
    use crate::launcher::Launcher;
    use crate::owner::OwnerFile;
    use crate::telling::Telling;

    type Refused = Arc<Mutex<Option<String>>>;

    /// The store, which refuses every write to the table it is told.
    struct Refusing {
        inner: Box<dyn KeyValue>,
        refused: Refused,
    }

    impl Refusing {
        fn refuses(&self, table: &str) -> Result<(), StoreError> {
            if self.refused.lock().unwrap().as_deref() == Some(table) {
                return Err(StoreError("refused here".into()));
            }
            Ok(())
        }
    }

    impl KeyValue for Refusing {
        fn get(&self, table: &str, key: &str) -> Result<Option<Vec<u8>>, StoreError> {
            self.refuses(&format!("read:{table}"))?;
            self.inner.get(table, key)
        }
        fn put(&mut self, table: &str, key: &str, value: &[u8]) -> Result<(), StoreError> {
            self.refuses(table)?;
            self.inner.put(table, key, value)
        }
        fn put_together(&mut self, writes: &[Write]) -> Result<(), StoreError> {
            for (table, _, _) in writes {
                self.refuses(table)?;
            }
            self.inner.put_together(writes)
        }
        fn delete(&mut self, table: &str, key: &str) -> Result<bool, StoreError> {
            self.refuses(table)?;
            self.inner.delete(table, key)
        }
        fn list(&self, table: &str) -> Result<Vec<(String, Vec<u8>)>, StoreError> {
            self.refuses(&format!("list:{table}"))?;
            self.inner.list(table)
        }
    }

    struct Place {
        dir: PathBuf,
        fleet: FleetHandle,
        hub: Arc<Hub>,
        refused: Refused,
        said: Arc<Mutex<Vec<String>>>,
    }

    impl Drop for Place {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.dir);
        }
    }

    fn place(name: &str) -> Place {
        seeded_place(name, |_| {}, None).unwrap()
    }

    fn seeded_place(
        name: &str,
        seed: impl FnOnce(&mut dyn KeyValue),
        refuse: Option<String>,
    ) -> Result<Place, String> {
        let dir = std::env::temp_dir().join(format!("clarp-unkept-{name}-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let at = dir.to_str().unwrap();
        let config = ["--data", at, "--run", at, "--cwd", at].map(String::from);
        let config = Config::from_args(config.into_iter()).unwrap();
        let said: Arc<Mutex<Vec<String>>> = Arc::default();
        let heard = said.clone();
        let hub = Arc::new(Hub::new(0));
        let works = fleet::Works {
            runtime: tokio::runtime::Handle::current(),
            opening_gate: crate::calls::opening_launch::channel().0,
            config: Arc::new(config),
            engine: Arc::new(MemEngine::new()),
            codecs: clarp_codec::standard_set("zstd").unwrap(),
            launcher: Arc::new(Launcher::by_name("direct", dir.join("unused-shim")).unwrap()),
            clock: Arc::new(SystemClock),
            hub: hub.clone(),
            revisions: Arc::default(),
            tool_env: Vec::new(),
            pusher: crate::pusher::pusher().0,
            blobs: MemBlobs::new().open(&dir).unwrap(),
            speaks: crate::voice::mouth().0,
            telling: Telling::new(OwnerFile::new(Some(dir.join("absent.toml")))),
            tally: crate::usage::tally().0,
            turns: Arc::default(),
            pools: Vec::new(),
            log: Log::to(
                move |line| heard.lock().unwrap().push(line.to_string()),
                Arc::new(SystemClock),
            ),
        };
        let refused: Refused = Arc::default();
        let mut store = Refusing {
            inner: MemKeyValue::new().open(Path::new(&dir)).unwrap(),
            refused: refused.clone(),
        };
        store
            .put("setting", "server_id", b"fixture-computer")
            .unwrap();
        seed(&mut store);
        *refused.lock().unwrap() = refuse;
        let fleet = fleet::start(works, Box::new(store))?;
        Ok(Place {
            dir,
            fleet,
            hub,
            refused,
            said,
        })
    }

    #[tokio::test]
    async fn computer_job_ownership_survives_concurrent_registration_and_failed_writes() {
        use clarp_kernel::jobs::{JobCommand, Worker};
        let place = place("computer-job-write");
        let register = |id: &str| JobCommand::Register {
            id: id.into(),
            queued: true,
            kind: "portrait".into(),
            title: "Portrait".into(),
            detail: String::new(),
            worker: Worker::none(),
            restart_cancelled: true,
            started_trace_id: String::new(),
            heartbeat_timeout_ms: None,
            metadata: serde_json::json!({}),
        };
        let fleet = &place.fleet;
        let mut events = place.hub.subscribe(None).1;
        *place.refused.lock().unwrap() = Some(fleet::shelf("computer-jobs"));
        assert!(
            fleet
                .change_computer_job(register("picture"))
                .await
                .is_err()
        );
        assert!(fleet.computer_jobs().await.unwrap().is_empty());
        assert!(events.try_recv().is_err());
        *place.refused.lock().unwrap() = None;
        let registered = fleet
            .change_computer_job(register("picture"))
            .await
            .unwrap()
            .unwrap()
            .job
            .unwrap();
        let event: serde_json::Value =
            serde_json::from_str(&events.try_recv().unwrap().json).unwrap();
        assert_eq!(event["type"], "background-job-updated");
        assert_eq!(event["job_id"], "picture");
        assert_eq!(event["job"]["computer_id"], "fixture-computer");
        assert_eq!(event["change_revision"], registered.revision);
        let claim = |id: &str| fleet.claim_job(id.into(), "agent-fixture".into());
        assert_eq!(
            claim("picture").await.unwrap(),
            Err(fleet::JobOwner::Computer)
        );
        assert!(claim("agent-work").await.unwrap().is_ok());
        assert_eq!(
            fleet
                .change_computer_job(register("agent-work"))
                .await
                .unwrap(),
            Err(fleet::ComputerJobRefusal::AgentOwned {
                id: "agent-work".into(),
                agent_id: "agent-fixture".into()
            })
        );
        assert_eq!(
            fleet
                .claim_job("agent-work".into(), "other".into())
                .await
                .unwrap(),
            Err(fleet::JobOwner::Agent("agent-fixture".into()))
        );
        assert_eq!(
            fleet
                .change_computer_job(register("claude-task:missing"))
                .await
                .unwrap(),
            Err(fleet::ComputerJobRefusal::Kernel(
                clarp_kernel::jobs::JobRefusal::ReservedId
            ))
        );
        assert_eq!(
            fleet
                .change_computer_job(JobCommand::SetLog {
                    id: "picture".into(),
                    generation: 1,
                    path: "relative.log".into(),
                    worker_cwd: String::new()
                })
                .await
                .unwrap(),
            Err(fleet::ComputerJobRefusal::Kernel(
                clarp_kernel::jobs::JobRefusal::LogPathNotAbsolute("relative.log".into())
            ))
        );
        let (agent, computer) = tokio::join!(
            claim("simultaneous"),
            fleet.change_computer_job(register("simultaneous"))
        );
        assert_ne!(agent.unwrap().is_ok(), computer.unwrap().is_ok());
        *place.refused.lock().unwrap() = Some(fleet::shelf("computer-jobs"));
        let cancel = JobCommand::Cancel {
            id: "picture".into(),
            expected_generation: Some(1),
            reason: "user_cancelled".into(),
        };
        assert!(fleet.change_computer_job(cancel.clone()).await.is_err());
        let unchanged = fleet.computer_job("picture".into()).await.unwrap().unwrap();
        assert_eq!(unchanged.job, registered);
        assert_eq!(unchanged.timeline.len(), 1);
        *place.refused.lock().unwrap() = None;
        let ended = fleet
            .change_computer_job(cancel)
            .await
            .unwrap()
            .unwrap()
            .job
            .unwrap();
        assert!(ended.status.is_terminal());
        let restarted = fleet
            .change_computer_job(register("picture"))
            .await
            .unwrap()
            .unwrap()
            .job
            .unwrap();
        assert_eq!(restarted.generation, 2);
        assert_eq!(
            fleet
                .computer_job("picture".into())
                .await
                .unwrap()
                .unwrap()
                .timeline
                .len(),
            3
        );
    }

    #[tokio::test]
    async fn computer_job_corruption_preserves_originals_until_registration_succeeds() {
        use clarp_kernel::jobs::{JobCommand, Worker};
        let table = fleet::shelf("computer-jobs");
        assert!(seeded_place("no-list", |_| {}, Some(format!("list:{table}"))).is_err());
        let place = seeded_place(
            "bad-jobs",
            |store| {
                for key in ["private-one", "private-two"] {
                    store.put(&table, key, b"private-corrupt-bytes").unwrap();
                }
            },
            None,
        )
        .unwrap();
        let fleet = &place.fleet;
        assert!(fleet.computer_jobs().await.unwrap().is_empty());
        assert_eq!(
            place.said.lock().unwrap().as_slice(),
            ["clarp-host: skipped 2 unreadable computer job records; original bytes retained"]
        );
        assert!(
            fleet
                .claim_job("private-one".into(), "agent".into())
                .await
                .unwrap()
                .is_err()
        );
        let register = JobCommand::Register {
            id: "private-one".into(),
            kind: "picture".into(),
            title: "Picture".into(),
            queued: true,
            worker: Worker::none(),
            metadata: serde_json::json!({}),
            detail: String::new(),
            started_trace_id: String::new(),
            restart_cancelled: true,
            heartbeat_timeout_ms: None,
        };
        *place.refused.lock().unwrap() = Some(table);
        assert!(fleet.change_computer_job(register.clone()).await.is_err());
        assert_eq!(place.said.lock().unwrap().len(), 1);
        assert_eq!(
            fleet
                .kept("computer-jobs", "private-one".into())
                .await
                .unwrap()
                .unwrap(),
            b"private-corrupt-bytes"
        );
        *place.refused.lock().unwrap() = None;
        fleet.change_computer_job(register).await.unwrap().unwrap();
        assert_eq!(
            place.said.lock().unwrap()[1],
            "clarp-host: registration replaced an unreadable computer job record"
        );
        assert_eq!(fleet.computer_jobs().await.unwrap().len(), 1);
        assert_eq!(
            fleet
                .kept("computer-jobs", "private-two".into())
                .await
                .unwrap()
                .unwrap(),
            b"private-corrupt-bytes"
        );
    }

    #[tokio::test]
    async fn computer_job_producers_wake_only_after_their_generation_ends_on_disk() {
        use clarp_kernel::jobs::{JobCommand, Worker};
        use futures_util::FutureExt;

        let place = place("watch-computer-job");
        let fleet = &place.fleet;
        let id = "watched";
        let register = |token: &str| JobCommand::Register {
            id: id.into(),
            kind: "portrait".into(),
            title: "Picture".into(),
            queued: false,
            worker: Worker {
                pid: 17,
                start_token: token.into(),
            },
            metadata: serde_json::json!({}),
            detail: String::new(),
            started_trace_id: String::new(),
            restart_cancelled: true,
            heartbeat_timeout_ms: None,
        };
        let wait = |generation| fleet.wait_computer_job_end(id.into(), generation);
        let limit = std::time::Duration::from_secs(2);
        tokio::time::timeout(limit, wait(1)).await.unwrap().unwrap(); // no such id
        fleet
            .change_computer_job(register("first"))
            .await
            .unwrap()
            .unwrap();
        let mut old = Box::pin(wait(1));
        let mut another = Box::pin(wait(1));
        assert!(old.as_mut().now_or_never().is_none());
        assert!(another.as_mut().now_or_never().is_none());
        fleet
            .change_computer_job(JobCommand::Progress {
                id: id.into(),
                generation: 1,
                text: "working".into(),
            })
            .await
            .unwrap()
            .unwrap();
        let cancel = |generation| JobCommand::Cancel {
            id: id.into(),
            expected_generation: Some(generation),
            reason: "user_cancelled".into(),
        };
        fleet.change_computer_job(cancel(2)).await.unwrap().unwrap();
        *place.refused.lock().unwrap() = Some(fleet::shelf("computer-jobs"));
        assert!(fleet.change_computer_job(cancel(1)).await.is_err());
        assert!(old.as_mut().now_or_never().is_none());
        *place.refused.lock().unwrap() = None;
        fleet
            .change_computer_job(register("second"))
            .await
            .unwrap()
            .unwrap();
        tokio::time::timeout(limit, old).await.unwrap().unwrap();
        tokio::time::timeout(limit, another).await.unwrap().unwrap();
        tokio::time::timeout(limit, wait(1)).await.unwrap().unwrap(); // already superseded
        let mut current = Box::pin(wait(2));
        assert!(current.as_mut().now_or_never().is_none());
        fleet.change_computer_job(cancel(2)).await.unwrap().unwrap();
        tokio::time::timeout(limit, current).await.unwrap().unwrap();
        tokio::time::timeout(limit, wait(2)).await.unwrap().unwrap(); // already terminal
        fleet
            .change_computer_job(register("third"))
            .await
            .unwrap()
            .unwrap();
        let mut finished = Box::pin(wait(3));
        assert!(finished.as_mut().now_or_never().is_none());
        fleet
            .change_computer_job(JobCommand::Finish {
                id: id.into(),
                generation: 3,
                worker: Worker {
                    pid: 17,
                    start_token: "third".into(),
                },
                failed: None,
            })
            .await
            .unwrap()
            .unwrap();
        tokio::time::timeout(limit, finished)
            .await
            .unwrap()
            .unwrap();
    }

    #[tokio::test]
    async fn unreadable_inventory_requires_a_working_store_and_a_durable_backup() {
        let place = place("transcription-backup");
        let at = place.dir.to_str().unwrap();
        let config = Config::from_args(
            ["--data", at, "--run", at, "--cwd", at]
                .map(String::from)
                .into_iter(),
        )
        .unwrap();
        let key = "transcription-models";
        let original = vec![0xff, b'!', 0, b'}'];
        place
            .fleet
            .keep(key, "records".into(), Some(original.clone()))
            .await
            .unwrap();
        for refusal in [
            "read:kept.transcription-models",
            "kept.transcription-models",
        ] {
            *place.refused.lock().unwrap() = Some(refusal.to_string());
            assert!(
                crate::heard::Ears::new(&config, &place.fleet)
                    .await
                    .is_err()
            );
        }
        *place.refused.lock().unwrap() = None;
        assert_eq!(
            place.fleet.kept(key, "records".into()).await.unwrap(),
            Some(original.clone())
        );
        assert_eq!(
            place
                .fleet
                .kept(key, "records.unreadable".into())
                .await
                .unwrap(),
            None
        );
        assert!(crate::heard::Ears::new(&config, &place.fleet).await.is_ok());
        assert_eq!(
            place
                .fleet
                .kept(key, "records.unreadable".into())
                .await
                .unwrap(),
            Some(original)
        );
    }

    #[tokio::test]
    async fn a_model_is_registered_only_with_its_successful_job_generation() {
        use clarp_kernel::jobs::{JobCommand, Worker};
        let place = place("install-commit");
        let fleet = &place.fleet;
        let key = "install-model";
        let register = JobCommand::Register {
            id: key.into(),
            kind: "transcription-model-install".into(),
            queued: false,
            title: "model".into(),
            detail: String::new(),
            metadata: serde_json::json!({}),
            worker: Worker::none(),
            restart_cancelled: true,
            heartbeat_timeout_ms: None,
            started_trace_id: String::new(),
        };
        let writes = || {
            vec![
                (
                    "transcription-models",
                    "records".into(),
                    Some(b"new".to_vec()),
                ),
                ("model-publication", "model".into(), None),
            ]
        };
        fleet
            .keep(
                "transcription-models",
                "records".into(),
                Some(b"prior".to_vec()),
            )
            .await
            .unwrap();
        fleet
            .keep(
                "model-publication",
                "model".into(),
                Some(b"pending".to_vec()),
            )
            .await
            .unwrap();
        fleet
            .change_computer_job(register.clone())
            .await
            .unwrap()
            .unwrap();
        *place.refused.lock().unwrap() = Some(fleet::shelf("computer-jobs"));
        assert!(
            fleet
                .finish_computer_job_with(key.into(), 1, writes())
                .await
                .is_err()
        );
        assert!(
            fleet
                .computer_job(key.into())
                .await
                .unwrap()
                .unwrap()
                .job
                .status
                .is_active()
        );
        *place.refused.lock().unwrap() = None;
        fleet
            .change_computer_job(JobCommand::Cancel {
                id: key.into(),
                expected_generation: Some(1),
                reason: "user_cancelled".into(),
            })
            .await
            .unwrap()
            .unwrap();
        assert!(
            fleet
                .finish_computer_job_with(key.into(), 1, writes())
                .await
                .unwrap()
                .unwrap()
                .record
                .is_none()
        );
        fleet.change_computer_job(register).await.unwrap().unwrap();
        assert!(
            fleet
                .finish_computer_job_with(key.into(), 1, writes())
                .await
                .unwrap()
                .unwrap()
                .record
                .is_none()
        );
        assert_eq!(
            fleet
                .kept("transcription-models", "records".into())
                .await
                .unwrap()
                .as_deref(),
            Some(b"prior".as_slice())
        );
        assert_eq!(
            fleet
                .kept("model-publication", "model".into())
                .await
                .unwrap()
                .as_deref(),
            Some(b"pending".as_slice())
        );
        assert!(
            fleet
                .finish_computer_job_with(key.into(), 2, writes())
                .await
                .unwrap()
                .unwrap()
                .record
                .is_some()
        );
        assert_eq!(
            fleet
                .kept("transcription-models", "records".into())
                .await
                .unwrap()
                .as_deref(),
            Some(b"new".as_slice())
        );
        assert!(
            fleet
                .kept("model-publication", "model".into())
                .await
                .unwrap()
                .is_none()
        );
    }

    #[tokio::test]
    async fn an_install_in_the_job_book_never_starts_the_download_port_at_host_start() {
        use clarp_kernel::jobs::{JobCommand, Worker};
        use clarp_ports::model_download::{DownloadRequest, DownloadRun, ModelDownloader};
        struct NoDownload;
        impl ModelDownloader for NoDownload {
            fn start(&self, _: DownloadRequest) -> std::io::Result<Box<dyn DownloadRun>> {
                panic!("recovery reached the download port");
            }
        }
        let place = place("recover-offline");
        let at = place.dir.to_str().unwrap();
        let config = Config::from_args(
            ["--data", at, "--run", at, "--cwd", at]
                .map(String::from)
                .into_iter(),
        )
        .unwrap();
        let id = "transcription-model-install-344af2724f588da1";
        place.fleet.change_computer_job(JobCommand::Register{id:id.into(),kind:"transcription-model-install".into(),
            queued:true,title:"Download".into(),detail:"Preparing model download".into(),worker:Worker::none(),
            restart_cancelled:true,started_trace_id:String::new(),heartbeat_timeout_ms:Some(120000),
            metadata:serde_json::json!({"model_id":"faster-whisper:base.en","expire_queued":true})}).await.unwrap().unwrap();
        assert!(
            crate::heard::Ears::with_downloader(&config, &place.fleet, Box::new(NoDownload))
                .await
                .is_ok()
        );
        assert_eq!(
            place
                .fleet
                .computer_job(id.into())
                .await
                .unwrap()
                .unwrap()
                .job
                .terminal_reason,
            "worker_never_started"
        );
    }

    fn here(fleet: &FleetHandle, session: &str) -> bool {
        let directory = fleet.directory();
        directory
            .members
            .iter()
            .any(|member| member.record.session == session)
    }

    /// Of kind (a): the edge's agent that is not to stay, whose removal its caller depends on.
    #[tokio::test]
    async fn an_agent_that_is_not_to_stay_and_cannot_be_let_go_is_what_the_caller_is_told() {
        let place = place("not-to-stay");
        let record = AgentRecord {
            agent_id: "agent-moved".into(),
            session: "moved-1a2b".into(),
            persona: "Moved".into(),
            backend: "claude".into(),
            cwd: place.dir.to_str().unwrap().into(),
            ..AgentRecord::default()
        };
        place.fleet.adopt(record).await.unwrap().unwrap();
        let first = || ApiError::new(StatusCode::CONFLICT, "the move failed");

        *place.refused.lock().unwrap() = Some("agent".into());
        let told = not_to_stay(&place.fleet, "moved-1a2b".into(), first(), ApiError::from).await;
        assert_eq!(
            (told.status(), told.message()),
            (
                StatusCode::SERVICE_UNAVAILABLE,
                "storage failed: refused here"
            )
        );
        assert!(here(&place.fleet, "moved-1a2b"));

        *place.refused.lock().unwrap() = None;
        let told = not_to_stay(&place.fleet, "moved-1a2b".into(), first(), ApiError::from).await;
        assert_eq!(
            (told.status(), told.message()),
            (StatusCode::CONFLICT, "the move failed")
        );
        assert!(!here(&place.fleet, "moved-1a2b"));
        // A caller is told it: nothing of it is said in the log as well.
        assert!(place.said.lock().unwrap().is_empty());
    }

    /// Of kind (b): a turn's use, which nothing waits on: said once, by its kind.
    #[tokio::test]
    async fn a_turns_use_the_store_will_not_keep_is_said_once_by_its_kind_and_never_its_value() {
        let place = place("use");
        *place.refused.lock().unwrap() = Some(fleet::shelf("used"));
        let (tell, told) = tokio::sync::mpsc::unbounded_channel();
        let used = clarp_kernel::usage::Used {
            tool: "claude".into(),
            at_ms: 1_795_000_000_000,
            tokens_in: 4321,
            tokens_out: 8765,
            cost_usd: Some(0.125),
        };
        let name = "1795000000000-agent-quiet-turn-7".to_string();
        tell.send(crate::usage::Told::Used(name, used)).unwrap();
        drop(tell);
        let works = crate::usage::Works {
            clock: Arc::new(SystemClock),
            hub: Arc::new(Hub::new(0)),
            limits: Arc::default(),
            server_id: "host".into(),
        };
        crate::usage::keep(place.fleet.clone(), told, works).await;
        let said = place.said.lock().unwrap().clone();
        assert_eq!(
            said,
            ["clarp-host: a record of the kind used was not kept: storage failed: refused here"]
        );
        for value in ["4321", "8765", "0.125", "quiet", "claude"] {
            assert!(!said[0].contains(value), "{value}");
        }
        assert!(place.fleet.kept_all("used").await.unwrap().is_empty());
    }
}
