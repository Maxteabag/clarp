//! A write that failed and that nothing waits on is said once in the Host's own log: the
//! kind of record and why, never what was to be written. A write a caller depends on returns
//! its failure instead; an answer to a caller who has gone is all that is left unsaid.
//!
//! The fleet says it ([`crate::fleet::FleetHandle::unkept`]), through the log it was started
//! with: the standard error, or what a test reads.

use std::sync::Arc;

/// Where the Host says what it could not keep.
#[derive(Clone)]
pub struct Log(Arc<dyn Fn(&str) + Send + Sync>);

impl Log {
    /// The Host's own log: its standard error, beside its other lines.
    pub fn stderr() -> Self {
        Self(Arc::new(|line| eprintln!("{line}")))
    }

    /// A log that hands each line to `said`.
    pub fn to(said: impl Fn(&str) + Send + Sync + 'static) -> Self {
        Self(Arc::new(said))
    }

    /// The one way a failed write is said: the kind and the store's words.
    pub fn unkept(&self, kind: &str, why: &str) {
        (self.0)(&format!(
            "clarp-host: a record of the kind {kind} was not kept: {why}"
        ));
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
        let line = std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
        let said = line.clone();
        let log = super::Log::to(move |text| said.lock().unwrap().push(text.to_string()));
        log.unkept("used", "disk full");
        assert_eq!(
            *line.lock().unwrap(),
            ["clarp-host: a record of the kind used was not kept: disk full"]
        );
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
            self.inner.list(table)
        }
    }

    struct Place {
        dir: PathBuf,
        fleet: FleetHandle,
        refused: Refused,
        said: Arc<Mutex<Vec<String>>>,
    }

    impl Drop for Place {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.dir);
        }
    }

    fn place(name: &str) -> Place {
        let dir = std::env::temp_dir().join(format!("clarp-unkept-{name}-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let at = dir.to_str().unwrap();
        let config = ["--data", at, "--run", at, "--cwd", at].map(String::from);
        let config = Config::from_args(config.into_iter()).unwrap();
        let said: Arc<Mutex<Vec<String>>> = Arc::default();
        let heard = said.clone();
        let works = fleet::Works {
            runtime: tokio::runtime::Handle::current(),
            opening_gate: crate::calls::opening_launch::channel().0,
            config: Arc::new(config),
            engine: Arc::new(MemEngine::new()),
            codecs: clarp_codec::standard_set("zstd").unwrap(),
            launcher: Arc::new(Launcher::by_name("direct", dir.join("unused-shim")).unwrap()),
            clock: Arc::new(SystemClock),
            hub: Arc::new(Hub::new(0)),
            revisions: Arc::default(),
            tool_env: Vec::new(),
            pusher: crate::pusher::pusher().0,
            blobs: MemBlobs::new().open(&dir).unwrap(),
            speaks: crate::voice::mouth().0,
            telling: Telling::new(OwnerFile::new(Some(dir.join("absent.toml")))),
            tally: crate::usage::tally().0,
            pools: Vec::new(),
            log: Log::to(move |line| heard.lock().unwrap().push(line.to_string())),
        };
        let refused: Refused = Arc::default();
        let store = Refusing {
            inner: MemKeyValue::new().open(Path::new(&dir)).unwrap(),
            refused: refused.clone(),
        };
        let fleet = fleet::start(works, Box::new(store)).unwrap();
        Place {
            dir,
            fleet,
            refused,
            said,
        }
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
