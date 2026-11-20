//! The worker reads its roster projections before it puts a recovered log at rest.
use super::*;
use clarp_ports::clock::SystemClock;
use clarp_ports::kv::KeyValueEngine;
use clarp_ports::log::{AgentLog, LogError};
use clarp_ports::testkit::{LogAccess, ObservedLog, scratch_dir};
use clarp_store_mem::{MemEngine, MemKeyValue};
use std::sync::Mutex;

#[derive(Default)]
struct ObservedOpening {
    inner: MemEngine,
    accesses: Arc<Mutex<Vec<LogAccess>>>,
}
impl LogEngine for ObservedOpening {
    fn name(&self) -> &'static str {
        self.inner.name()
    }
    fn open(&self, path: &Path, codecs: CodecSet) -> Result<Box<dyn AgentLog>, LogError> {
        let accesses = self.accesses.clone();
        Ok(Box::new(ObservedLog::new(
            self.inner.open(path, codecs)?,
            move |rest| accesses.lock().unwrap().push(rest),
        )))
    }
}

#[tokio::test]
async fn log_at_rest_after_the_workers_last_opening_read() {
    let dir = scratch_dir("host-log-at-rest");
    let engine = Arc::new(ObservedOpening::default());
    let works = super::retirement_tests::test_works(
        &dir,
        engine.clone(),
        crate::unkept::Log::to(|_| {}, Arc::new(SystemClock)),
    );
    let fleet = start(works, MemKeyValue::new().open(&dir).unwrap()).unwrap();
    let made = fleet
        .create(
            serde_json::from_value(
                serde_json::json!({"name":"Example","cwd":dir,"backend":"scripted"}),
            )
            .unwrap(),
        )
        .await
        .unwrap()
        .unwrap();
    let directory = fleet.directory();
    let agent = directory.by_session(&made.session).unwrap().agent.clone();
    // The close is acknowledged by this worker after opening has finished. It never reads the log.
    tokio::task::spawn_blocking(move || agent.close())
        .await
        .unwrap();
    let accesses = engine.accesses.lock().unwrap().clone();
    assert!(accesses.contains(&LogAccess::ReadOrWrite));
    assert_eq!(
        accesses
            .iter()
            .filter(|&&access| access == LogAccess::Rest)
            .count(),
        1
    );
    assert_eq!(accesses.last(), Some(&LogAccess::Rest));
    drop(fleet);
    std::fs::remove_dir_all(dir).unwrap();
}
