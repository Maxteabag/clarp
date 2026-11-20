//! Observe the actual writer at the storage port and interrupt individual
//! protocol steps without SQL or a second writer into the principal's log.
use super::*;
use clarp_ports::{
    clock::SystemClock,
    codec::CodecSet,
    kv::{KeyValueEngine, StoreError},
    log::{AgentLog, Item, Kinds, LogEngine, LogError, NewItem, Seq},
};
use clarp_store_mem::{MemEngine, MemKeyValue};
use std::sync::{
    Mutex,
    atomic::{AtomicBool, Ordering},
};

#[derive(Default)]
struct Faults {
    fail_list: AtomicBool,
    refuse_row_read: AtomicBool,
    hidden_current: AtomicBool,
    cut_note: AtomicBool,
    history_mode: std::sync::atomic::AtomicU8,
    cut_append: AtomicBool,
    fail_next_open: AtomicBool,
    reject_repairs: AtomicBool,
    parked: Mutex<
        Option<(
            tokio::sync::oneshot::Sender<()>,
            std::sync::mpsc::Receiver<()>,
        )>,
    >,
    cut_fleet: AtomicBool,
    writers: Mutex<Vec<String>>,
}
struct CheckedLog(Box<dyn AgentLog>, Arc<Faults>);
impl AgentLog for CheckedLog {
    fn append(&mut self, items: &[NewItem]) -> Result<Seq, LogError> {
        if self.1.cut_note.swap(false, Ordering::SeqCst) {
            return Err(LogError::Storage("reopen fixture".into()));
        }
        if items
            .iter()
            .any(|i| i.key.as_ref().is_some_and(|k| k.starts_with("ledger/")))
        {
            self.1
                .writers
                .lock()
                .unwrap()
                .push(std::thread::current().name().unwrap_or("").to_owned());
            if self.1.cut_append.swap(false, Ordering::SeqCst) {
                return Err(LogError::Storage("cut principal append".into()));
            }
        }
        self.0.append(items)
    }
    fn find(&self, k: &str) -> Result<Option<Item>, LogError> {
        if k.starts_with("ledger/delegation/by-id/") {
            let park = self.1.parked.lock().unwrap().take();
            if let Some((started, release)) = park {
                let _ = started.send(());
                release
                    .recv_timeout(std::time::Duration::from_secs(10))
                    .map_err(|e| LogError::Storage(e.to_string()))?;
            }
        }
        if k == "ledger/delegation/current" && self.1.hidden_current.load(Ordering::SeqCst) {
            return Ok(None);
        }
        let mut found = self.0.find(k)?;
        if k.starts_with("ledger/delegation/by-id/") {
            match self.1.history_mode.load(Ordering::SeqCst) {
                1 => return Ok(None),
                2 => {
                    if let Some(row) = &mut found
                        && let Transition::Start { binding } =
                            serde_json::from_slice(&row.body).unwrap()
                    {
                        row.body = serde_json::to_vec(&Transition::Stop {
                            binding,
                            reason: "ended".into(),
                        })
                        .unwrap();
                    }
                }
                3 => return Err(LogError::Storage("cannot read delegation history".into())),
                _ => {}
            }
        }
        Ok(found)
    }
    fn last_seq(&self) -> Result<Seq, LogError> {
        self.0.last_seq()
    }
    fn read_after(&self, n: Seq, k: Kinds, limit: usize) -> Result<Vec<Item>, LogError> {
        self.0.read_after(n, k, limit)
    }
    fn read_before(&self, n: Seq, k: Kinds, limit: usize) -> Result<Vec<Item>, LogError> {
        self.0.read_before(n, k, limit)
    }
}
struct CheckedEngine(MemEngine, Arc<Faults>);
impl LogEngine for CheckedEngine {
    fn name(&self) -> &'static str {
        self.0.name()
    }
    fn open(&self, dir: &Path, codecs: CodecSet) -> Result<Box<dyn AgentLog>, LogError> {
        if self.1.fail_next_open.swap(false, Ordering::SeqCst) {
            return Err(LogError::Storage("cannot open this principal's log".into()));
        }
        Ok(Box::new(CheckedLog(
            self.0.open(dir, codecs)?,
            self.1.clone(),
        )))
    }
}
struct CheckedKv(Box<dyn KeyValue>, Arc<Faults>);
impl KeyValue for CheckedKv {
    fn put_together(&mut self, w: &[Write]) -> Result<(), StoreError> {
        if w.iter().any(|(t, _, _)| t == ROWS)
            && (self.1.reject_repairs.load(Ordering::SeqCst)
                || self.1.cut_fleet.swap(false, Ordering::SeqCst))
        {
            return Err(StoreError("cut fleet transaction".into()));
        }
        self.0.put_together(w)
    }
    fn get(&self, t: &str, k: &str) -> Result<Option<Vec<u8>>, StoreError> {
        if t == ROWS && self.1.refuse_row_read.load(Ordering::SeqCst) {
            return Err(StoreError("reservation read failed".into()));
        }
        self.0.get(t, k)
    }
    fn put(&mut self, t: &str, k: &str, v: &[u8]) -> Result<(), StoreError> {
        self.0.put(t, k, v)
    }
    fn delete(&mut self, t: &str, k: &str) -> Result<bool, StoreError> {
        self.0.delete(t, k)
    }
    fn list(&self, t: &str) -> Result<Vec<(String, Vec<u8>)>, StoreError> {
        if t == PRINCIPALS && self.1.fail_list.load(Ordering::SeqCst) {
            return Err(StoreError("reservation store unavailable".into()));
        }
        self.0.list(t)
    }
}

#[tokio::test]
async fn authority_at_the_port_obeys_mailbox_order_and_survives_each_refused_write() {
    let root = std::env::temp_dir().join(format!("clarp-delegation-{}", crate::ids::hex(6)));
    fs::create_dir(&root).unwrap();
    let faults = Arc::new(Faults::default());
    let engine = Arc::new(CheckedEngine(MemEngine::new(), faults.clone()));
    let roster = MemKeyValue::new();
    let works = super::super::retirement_tests::test_works(
        &root,
        engine.clone(),
        crate::unkept::Log::stderr(Arc::new(SystemClock)),
    );
    let fleet = super::super::start(
        works,
        Box::new(CheckedKv(roster.open(&root).unwrap(), faults.clone())),
    )
    .unwrap();
    let make = |name: &str, parent: Option<&str>| {
        let mut req = json!({"name":name,"session":name,"cwd":root,"backend":"claude"});
        if let Some(p) = parent {
            req["parent"] = json!(p);
            req["role"] = json!("helper");
        }
        fleet.create(serde_json::from_value(req).unwrap())
    };
    let p = make("owner", None).await.unwrap().unwrap();
    let h = make("helper", Some("owner")).await.unwrap().unwrap();
    let agent = fleet
        .directory()
        .by_session(&p.session)
        .unwrap()
        .agent
        .clone();
    let enable = || {
        fleet.goal_delegation(Request::Enable {
            principal: p.session.clone(),
            delegate: h.session.clone(),
            reason: "fixture".into(),
        })
    };
    let list = || fleet.goal_delegation(Request::List(p.session.clone()));
    faults.cut_fleet.store(true, Ordering::SeqCst);
    assert!(enable().await.is_err());
    assert_eq!(list().await.unwrap()["delegations"], json!([]));
    faults.cut_append.store(true, Ordering::SeqCst);
    assert!(enable().await.is_err());
    // A failed append may have triggered a reopen. Only a reconciled worker
    // may answer an ordinary command again.
    for _ in 0..30 {
        if agent
            .goal_events(format!("{}:fixture", p.agent_id), 0, 1)
            .await
            .is_ok()
        {
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(100)).await;
    }
    assert_eq!(list().await.unwrap()["delegations"], json!([]));
    for receipt_first in [true, false] {
        let active = enable().await.unwrap();
        let id = active["delegation_id"].as_str().unwrap().to_owned();
        let receipt = DelegationCommand::Receipt {
            id: id.clone(),
            caller: h.agent_id.clone(),
        };
        let stop = DelegationCommand::Change(Transition::Stop {
            binding: Binding {
                delegation_id: id.clone(),
                delegate_agent_id: h.agent_id.clone(),
            },
            reason: "ordered stop".into(),
        });
        let (first, second) = if receipt_first {
            (receipt, stop)
        } else {
            (stop, receipt)
        };
        // Both messages have been submitted before either answer is received.
        let one = agent.enqueue_delegation(first).unwrap();
        let two = agent.enqueue_delegation(second).unwrap();
        assert!(one.recv().unwrap().unwrap().accepted);
        assert_eq!(two.recv().unwrap().unwrap().accepted, receipt_first);
        let log = engine
            .open(
                &root.join("agents").join(&p.agent_id),
                clarp_codec::standard_set("none").unwrap(),
            )
            .unwrap();
        assert_eq!(
            log.find(&format!("ledger/applied/{id}")).unwrap().is_some(),
            receipt_first
        );
        faults.cut_fleet.store(true, Ordering::SeqCst);
        assert!(
            fleet
                .goal_delegation(Request::Stop {
                    id: id.clone(),
                    reason: "retry".into()
                })
                .await
                .is_err()
        );
        assert!(
            !agent
                .delegation(DelegationCommand::Receipt {
                    id: id.clone(),
                    caller: h.agent_id.clone()
                })
                .unwrap()
                .accepted
        );
        let rows = list().await.unwrap();
        let stopped = rows["delegations"]
            .as_array()
            .unwrap()
            .iter()
            .find(|r| r["delegation_id"] == id)
            .unwrap();
        assert_eq!(stopped["status"], "stopped");
        assert_eq!(stopped["stopped_reason"], "ordered stop");
    }
    let writers = faults.writers.lock().unwrap();
    assert!(!writers.is_empty());
    assert!(writers.iter().all(|w| w == "agent-owner"), "{writers:?}");
    drop(writers);
    drop(fleet);
    fs::remove_dir_all(&root).unwrap();
}

struct Hosts {
    root: std::path::PathBuf,
    faults: Arc<Faults>,
    engine: Arc<CheckedEngine>,
    roster: MemKeyValue,
    journal: Arc<Mutex<Vec<String>>>,
}
impl Hosts {
    fn new() -> Self {
        let root =
            std::env::temp_dir().join(format!("clarp-books-recovery-{}", crate::ids::hex(6)));
        fs::create_dir(&root).unwrap();
        let faults = Arc::new(Faults::default());
        Self {
            root,
            faults: faults.clone(),
            engine: Arc::new(CheckedEngine(MemEngine::new(), faults)),
            roster: MemKeyValue::new(),
            journal: Arc::default(),
        }
    }
    fn boot(&self) -> super::super::FleetHandle {
        self.try_boot().unwrap()
    }
    fn try_boot(&self) -> Result<super::super::FleetHandle, String> {
        let notes = self.journal.clone();
        let works = super::super::retirement_tests::test_works(
            &self.root,
            self.engine.clone(),
            crate::unkept::Log::to(
                move |line| notes.lock().unwrap().push(line.to_string()),
                Arc::new(SystemClock),
            ),
        );
        super::super::start(
            works,
            Box::new(CheckedKv(
                self.roster.open(&self.root).unwrap(),
                self.faults.clone(),
            )),
        )
    }
    async fn fixture(&self) -> (super::super::FleetHandle, AgentRecord, AgentRecord) {
        let fleet = self.boot();
        let make = |name: &str, parent: Option<&str>| {
            let mut r = json!({"name":name,"session":name,"cwd":self.root,"backend":"claude"});
            if let Some(parent) = parent {
                r["parent"] = json!(parent);
                r["role"] = json!("helper");
            }
            fleet.create(serde_json::from_value(r).unwrap())
        };
        let owner = make("owner", None).await.unwrap().unwrap();
        let helper = make("helper", Some("owner")).await.unwrap().unwrap();
        (fleet, owner, helper)
    }
}
impl Drop for Hosts {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.root);
    }
}
fn enable_pair() -> Request {
    Request::Enable {
        principal: "owner".into(),
        delegate: "helper".into(),
        reason: "test".into(),
    }
}

#[tokio::test]
async fn a_damaged_reservation_never_stops_the_host_or_grants_the_delegate_authority() {
    for damage in [
        "missing row",
        "row read error",
        "cannot close orphan",
        "misplaced row",
        "bad id bytes",
        "different start",
        "unreadable log",
        "malformed row",
        "wrong secondary key",
        "different current",
    ] {
        let hosts = Hosts::new();
        let (fleet, owner, helper) = hosts.fixture().await;
        let created = fleet.goal_delegation(enable_pair()).await.unwrap();
        let id = created["delegation_id"].as_str().unwrap();
        for m in &fleet.directory().members {
            m.agent.close(crate::agent::CLOSE_WITHIN);
        }
        drop(fleet);
        let mut store = hosts.roster.open(&hosts.root).unwrap();
        let mut row = find(store.as_ref(), id).unwrap().unwrap();
        match damage {
            "row read error" => hosts.faults.refuse_row_read.store(true, Ordering::SeqCst),
            "cannot close orphan" => {
                store.delete(ROWS, id).unwrap();
                hosts.faults.cut_append.store(true, Ordering::SeqCst);
            }
            "missing row" => {
                store.delete(ROWS, id).unwrap();
            }
            "misplaced row" => {
                row.principal_agent_id = "wrong".into();
                store.put_together(&[row.row().unwrap()]).unwrap();
            }
            "bad id bytes" => {
                store.put(PRINCIPALS, &owner.agent_id, &[0xff]).unwrap();
            }
            "different start" => {
                row.delegate_agent_id = "other".into();
                store.put(DELEGATES, "other", id.as_bytes()).unwrap();
                store.put_together(&[row.row().unwrap()]).unwrap();
            }
            "different current" => hosts.faults.hidden_current.store(true, Ordering::SeqCst),
            "unreadable log" => hosts.faults.fail_next_open.store(true, Ordering::SeqCst),
            "malformed row" => {
                store.put(ROWS, id, b"{").unwrap();
            }
            "wrong secondary key" => {
                store.put(DELEGATES, &helper.agent_id, b"wrong").unwrap();
            }
            _ => unreachable!(),
        }
        let snapshot =
            |store: &dyn KeyValue| [ROWS, PRINCIPALS, DELEGATES].map(|t| store.list(t).unwrap());
        let before = snapshot(store.as_ref());
        let fleet = hosts.boot();
        let members = fleet.directory();
        for m in &members.members {
            assert!(
                m.agent
                    .note("test-note".into(), "after recovery".into())
                    .await
                    .unwrap(),
                "{damage}"
            );
        }
        let principal = &members.by_session("owner").unwrap().agent;
        assert!(
            !principal
                .delegation(DelegationCommand::Receipt {
                    id: id.into(),
                    caller: helper.agent_id.clone()
                })
                .unwrap()
                .accepted,
            "{damage}"
        );
        // The orphan notification was queued before this fleet request.
        assert!(
            fleet
                .stored_agent(owner.agent_id.clone())
                .await
                .unwrap()
                .is_some()
        );
        // Quarantined bookkeeping cannot leak into either participant's history
        // or prevent inspection of unrelated relationships.
        for session in ["owner", "helper"] {
            assert_eq!(
                fleet
                    .goal_delegation(Request::List(session.into()))
                    .await
                    .unwrap()["delegations"],
                json!([]),
                "{damage}"
            );
        }
        assert_eq!(
            fleet
                .goal_delegation(Request::Current(owner.agent_id.clone()))
                .await
                .unwrap(),
            Value::Null
        );
        assert_eq!(
            fleet
                .goal_delegation(Request::Descendant(helper.agent_id.clone()))
                .await
                .unwrap(),
            false
        );
        assert_eq!(before, snapshot(store.as_ref()), "{damage}");
        let log = hosts.journal.lock().unwrap();
        assert!(
            log.iter()
                .any(|line| line.contains("1 reservations could not be reconciled")),
            "{damage}: {log:?}"
        );
        let expected = if damage == "bad id bytes" { "ff" } else { id };
        assert!(
            log.iter().any(|line| line.contains(expected)),
            "{damage}: {log:?}"
        );
        drop(log);
        for m in &members.members {
            m.agent.close(crate::agent::CLOSE_WITHIN);
        }
    }
}

#[tokio::test]
async fn a_silent_principal_cannot_hold_the_fleet_mailbox() {
    let hosts = Hosts::new();
    let (fleet, owner, _) = hosts.fixture().await;
    let (started, entered) = tokio::sync::oneshot::channel();
    let (release, held) = std::sync::mpsc::channel();
    *hosts.faults.parked.lock().unwrap() = Some((started, held));
    let ask = fleet.clone();
    let waiting = tokio::spawn(async move { ask.goal_delegation(enable_pair()).await });
    entered.await.unwrap();
    let next = tokio::time::timeout(
        std::time::Duration::from_secs(6),
        fleet.stored_agent(owner.agent_id.clone()),
    )
    .await;
    let answered_while_held = next.is_ok();
    let refusal = if answered_while_held {
        Some(waiting.await.unwrap().unwrap_err())
    } else {
        None
    };
    release.send(()).unwrap();
    assert!(
        answered_while_held,
        "unrelated fleet ask waited for the actor to be released"
    );
    assert_eq!(refusal.unwrap().status(), StatusCode::SERVICE_UNAVAILABLE);
    let store = hosts.roster.open(&hosts.root).unwrap();
    assert_eq!(
        store.list(PRINCIPALS).unwrap().len(),
        1,
        "a timed-out START may still commit"
    );
    for m in &fleet.directory().members {
        m.agent.close(crate::agent::CLOSE_WITHIN);
    }
}

#[tokio::test]
async fn the_owner_rejoins_while_its_delegation_index_cannot_be_repaired() {
    for (mode, wrong_identity, error) in [
        (1, false, "cut fleet transaction"),
        (2, false, "cut fleet transaction"),
        (0, false, "history disagrees"),
        (3, false, "cannot read delegation history"),
        (1, true, "identity mismatch"),
    ] {
        let hosts = Hosts::new();
        let (fleet, p, h) = hosts.fixture().await;
        let value = fleet.goal_delegation(enable_pair()).await.unwrap();
        let id = value["delegation_id"].as_str().unwrap();
        let mut store = hosts.roster.open(&hosts.root).unwrap();
        if wrong_identity {
            let mut row = find(store.as_ref(), id).unwrap().unwrap();
            row.principal_agent_id = "wrong".into();
            store.put_together(&[row.row().unwrap()]).unwrap();
        }
        let agent = fleet.directory().by_session("owner").unwrap().agent.clone();
        hosts.faults.reject_repairs.store(true, Ordering::SeqCst);
        hosts.faults.hidden_current.store(true, Ordering::SeqCst);
        hosts.faults.history_mode.store(mode, Ordering::SeqCst);
        hosts.faults.cut_note.store(true, Ordering::SeqCst);
        assert!(
            agent
                .note("first".into(), "forces reopen".into())
                .await
                .is_err()
        );
        assert!(
            agent
                .note("second".into(), "ordinary owner work".into())
                .await
                .unwrap()
        );
        assert!(agent.row("second".into()).await.unwrap().is_some());
        assert!(
            !agent
                .delegation(DelegationCommand::Receipt {
                    id: id.into(),
                    caller: h.agent_id
                })
                .unwrap()
                .accepted
        );
        assert!(
            fleet
                .stored_agent(p.agent_id.clone())
                .await
                .unwrap()
                .is_some()
        );
        let notes = hosts.journal.lock().unwrap();
        assert!(
            notes
                .iter()
                .any(|line| line.contains(id) && line.contains(error)),
            "{mode}: {notes:?}"
        );
        drop(notes);
        for m in &fleet.directory().members {
            m.agent.close(crate::agent::CLOSE_WITHIN);
        }
    }
}

#[tokio::test]
async fn a_store_that_cannot_list_the_reservations_refuses_startup() {
    let hosts = Hosts::new();
    hosts.faults.fail_list.store(true, Ordering::SeqCst);
    assert!(matches!(hosts.try_boot(),Err(why) if why.contains("reservation store unavailable")));
}
