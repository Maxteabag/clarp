//! A committed removal still succeeds when the following log operation fails.
use super::*;
use clarp_ports::{
    blobs::BlobEngine,
    clock::SystemClock,
    kv::KeyValueEngine,
    log::{AgentLog, LogError},
};
use clarp_store_mem::{MemBlobs, MemEngine, MemKeyValue};
use std::sync::{
    Mutex,
    atomic::{AtomicBool, Ordering},
};

struct CutOpen {
    logs: MemEngine,
    cut: AtomicBool,
}
impl LogEngine for CutOpen {
    fn name(&self) -> &'static str {
        self.logs.name()
    }
    fn open(&self, path: &Path, codecs: CodecSet) -> Result<Box<dyn AgentLog>, LogError> {
        if self.cut.swap(false, Ordering::SeqCst) {
            return Err(LogError::Storage("cannot open retired log".into()));
        }
        self.logs.open(path, codecs)
    }
}

#[tokio::test]
async fn a_durable_removal_reports_success_and_retries_failed_retirement_on_restart() {
    let dir = std::env::temp_dir().join(format!("clarp-retirement-{}", crate::ids::hex(8)));
    std::fs::create_dir(&dir).unwrap();
    let engine = Arc::new(CutOpen {
        logs: MemEngine::new(),
        cut: AtomicBool::new(false),
    });
    let roster = MemKeyValue::new();
    let messages = Arc::new(Mutex::new(Vec::new()));
    let boot = || {
        let path = dir.to_str().unwrap();
        let config = Config::from_args(
            ["--data", path, "--run", path, "--cwd", path]
                .into_iter()
                .map(String::from),
        )
        .unwrap();
        let log = messages.clone();
        let works = Works {
            runtime: tokio::runtime::Handle::current(),
            opening_gate: crate::calls::opening_launch::channel().0,
            config: Arc::new(config),
            engine: engine.clone(),
            codecs: clarp_codec::standard_set("none").unwrap(),
            launcher: Arc::new(Launcher::by_name("direct", dir.join("no-shim")).unwrap()),
            clock: Arc::new(SystemClock),
            hub: Arc::new(Hub::new(0)),
            revisions: Arc::default(),
            tool_env: vec![],
            pusher: crate::pusher::pusher().0,
            blobs: MemBlobs::new().open(&dir).unwrap(),
            speaks: crate::voice::mouth().0,
            telling: crate::telling::Telling::new(crate::owner::OwnerFile::new(Some(
                dir.join("absent"),
            ))),
            tally: crate::usage::tally().0,
            turns: Arc::default(),
            pools: vec![],
            log: crate::unkept::Log::to(
                move |s| log.lock().unwrap().push(s.to_string()),
                Arc::new(SystemClock),
            ),
        };
        start(works, roster.open(&dir).unwrap()).unwrap()
    };
    let fleet = boot();
    let made = fleet
        .create(
            serde_json::from_value(
                serde_json::json!({"name":"Test","cwd":dir,"backend":"scripted"}),
            )
            .unwrap(),
        )
        .await
        .unwrap()
        .unwrap();
    let plan = format!("{}:example", made.agent_id);
    fleet
        .directory()
        .by_session(&made.session)
        .unwrap()
        .agent
        .goal_events(plan.clone(), 0, 1)
        .await
        .unwrap();
    engine.cut.store(true, Ordering::SeqCst);
    assert!(fleet.remove(made.session.clone()).await.unwrap());
    assert!(fleet.directory().by_session(&made.session).is_none());
    assert_eq!(
        fleet.past_ledger(plan.clone(), 0, 1).await.unwrap_err().0,
        "goal ledger retirement is incomplete"
    );
    assert_eq!(
        messages
            .lock()
            .unwrap()
            .iter()
            .filter(|s| s.contains("cannot open retired log"))
            .count(),
        1
    );
    drop(fleet);
    let fleet = boot();
    assert!(
        fleet
            .stored_agent(made.agent_id)
            .await
            .unwrap()
            .unwrap()
            .ledger_retired
    );
    assert_eq!(
        fleet.past_ledger(plan, 0, 1).await.unwrap(),
        (vec![], false)
    );
    drop(fleet);
    std::fs::remove_dir_all(&dir).unwrap();
}
