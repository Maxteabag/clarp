//! Closing an agent never waits without a bound: an agent whose thread does not answer its close
//! within `CLOSE_WITHIN` is not removed, and nothing of its own is touched; the answer says so in
//! words, and the same request asked again finishes the removal once (`Fleet::remove`).
//!
//! The agent's thread is held by its log: the stand-in engine's `open`, which the thread calls
//! first, waits while the agent is held, and keeps how often each agent's log was opened.
use super::*;
use clarp_ports::{blobs::BlobEngine, clock::SystemClock, kv::KeyValueEngine, log::AgentLog};
use clarp_store_mem::{MemBlobs, MemEngine, MemKeyValue};
use std::sync::{Condvar, Mutex};

/// The agents' logs. One of a held agent does not open until it is let go; one of a broken
/// agent ends the thread that opens it the first time.
#[derive(Default)]
struct Gate {
    logs: MemEngine,
    state: Mutex<GateState>,
    moved: Condvar,
}

#[derive(Default)]
struct GateState {
    held: HashSet<String>,
    broken: HashSet<String>,
    opened: BTreeMap<String, usize>,
}

impl Gate {
    fn hold(&self, id: &str) {
        self.state.lock().unwrap().held.insert(id.to_string());
    }
    fn let_go(&self, id: &str) {
        self.state.lock().unwrap().held.remove(id);
        self.moved.notify_all();
    }
    fn opened(&self, id: &str) -> usize {
        self.state
            .lock()
            .unwrap()
            .opened
            .get(id)
            .copied()
            .unwrap_or(0)
    }
}

impl LogEngine for Gate {
    fn name(&self) -> &'static str {
        self.logs.name()
    }
    fn open(
        &self,
        path: &Path,
        codecs: CodecSet,
    ) -> Result<Box<dyn AgentLog>, clarp_ports::log::LogError> {
        let id = path
            .file_name()
            .map(|name| name.to_string_lossy().into_owned())
            .unwrap_or_default();
        let mut state = self.state.lock().unwrap();
        let count = state.opened.entry(id.clone()).or_default();
        *count += 1;
        let first = *count == 1;
        if first && state.broken.contains(&id) {
            drop(state);
            panic!("the thread of {id} ends");
        }
        while state.held.contains(&id) {
            state = self.moved.wait(state).unwrap();
        }
        drop(state);
        self.logs.open(path, codecs)
    }
}

struct Place {
    dir: PathBuf,
    engine: Arc<Gate>,
    roster: MemKeyValue,
    hub: Arc<Hub>,
}

impl Drop for Place {
    fn drop(&mut self) {
        // No thread is left waiting on the gate.
        let mut state = self.engine.state.lock().unwrap();
        state.held.clear();
        drop(state);
        self.engine.moved.notify_all();
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

const HELD: &str = "agent-held";
const HELPER: &str = "agent-helper";
const OTHER: &str = "agent-other";

impl Place {
    /// A store as a Host left it: the agent to be deleted, open; its helper, running; another.
    fn new(name: &str) -> Self {
        let dir = std::env::temp_dir().join(format!("clarp-{name}-{}", crate::ids::hex(8)));
        std::fs::create_dir(&dir).unwrap();
        let place = Self {
            dir,
            engine: Arc::default(),
            roster: MemKeyValue::new(),
            hub: Arc::new(Hub::new(0)),
        };
        let mut store = place.store();
        let plain = serde_json::json!({});
        let helper = serde_json::json!({
            "parent_agent_id": HELD, "role": "helper", "helper_state": "running",
        });
        for (id, session, extra) in [
            (HELD, "held", &plain),
            (HELPER, "helper", &helper),
            (OTHER, "other", &plain),
        ] {
            let mut record = serde_json::json!({
                "agent_id": id, "session": session, "persona": session, "backend": "scripted",
                "cwd": "/", "conversation_id": "c-1", "created_at_ms": 1,
            });
            for (key, value) in extra.as_object().unwrap() {
                record[key] = value.clone();
            }
            store
                .put(AGENTS, id, &serde_json::to_vec(&record).unwrap())
                .unwrap();
        }
        store.put(SETTINGS, FOCUS, HELD.as_bytes()).unwrap();
        drop(store);
        place
    }

    fn store(&self) -> Box<dyn KeyValue> {
        self.roster.open(&self.dir).unwrap()
    }

    fn works(&self) -> Works {
        let path = self.dir.to_str().unwrap();
        let config = Config::from_args(
            ["--data", path, "--run", path, "--cwd", path]
                .into_iter()
                .map(String::from),
        )
        .unwrap();
        Works {
            runtime: tokio::runtime::Handle::current(),
            opening_gate: crate::calls::opening_launch::channel().0,
            config: Arc::new(config),
            engine: self.engine.clone(),
            codecs: clarp_codec::standard_set("none").unwrap(),
            launcher: Arc::new(Launcher::by_name("direct", self.dir.join("no-shim")).unwrap()),
            clock: Arc::new(SystemClock),
            hub: self.hub.clone(),
            revisions: Arc::default(),
            tool_env: vec![],
            pusher: crate::pusher::pusher().0,
            blobs: MemBlobs::new().open(&self.dir).unwrap(),
            speaks: crate::voice::mouth().0,
            telling: crate::telling::Telling::new(crate::owner::OwnerFile::new(Some(
                self.dir.join("absent"),
            ))),
            tally: crate::usage::tally().0,
            turns: Arc::default(),
            pools: vec![],
            log: crate::unkept::Log::to(|_| {}, Arc::new(SystemClock)),
            left_out: Arc::default(),
        }
    }

    fn start(&self) -> FleetHandle {
        start(self.works(), self.store()).unwrap()
    }

    /// The events the apps were told that name `session` and say it was deleted.
    fn told_deleted(&self, session: &str) -> usize {
        let (kept, _) = self.hub.subscribe(None);
        kept.iter()
            .map(|event| serde_json::from_str::<serde_json::Value>(&event.json).unwrap())
            .filter(|event| {
                event["session"] == session
                    && (event["kind"] == "deleted" || event["detail"]["reason"] == "deleted")
            })
            .count()
    }
}

fn helper_state(fleet: &FleetHandle) -> Option<String> {
    fleet
        .directory()
        .members
        .iter()
        .find(|member| member.record.agent_id == HELPER)
        .and_then(|member| member.record.helper_state.clone())
}

async fn stored(fleet: &FleetHandle, id: &str) -> AgentRecord {
    fleet.stored_agent(id.to_string()).await.unwrap().unwrap()
}

/// What a removal that did not come leaves: the agent in the roster as it was, its helper and the
/// focus as they were, nothing said, nothing of its own opened by anyone but its thread; its
/// tombstone kept.
async fn left_as_it_was(place: &Place, fleet: &FleetHandle) -> i64 {
    let directory = fleet.directory();
    let member = directory.by_session("held").expect("the agent is not removed");
    assert_eq!(member.record.deleted_at_ms, None);
    assert_eq!(directory.focus.as_deref(), Some(HELD));
    assert!(!directory.gone.contains_key(HELD));
    assert_eq!(helper_state(fleet).as_deref(), Some("running"));
    assert_eq!(place.told_deleted("held"), 0);
    assert_eq!(place.engine.opened(HELD), 1, "only its own thread opened its log");
    let kept = stored(fleet, HELD).await;
    assert!(!kept.ledger_retired);
    kept.deleted_at_ms.expect("its tombstone is kept")
}

/// What a removal that came leaves, once: the agent gone from the roster and kept as gone, its
/// helper abandoned, the focus let go, the apps told, its goals retired.
async fn removed_whole(place: &Place, fleet: &FleetHandle) {
    let directory = fleet.directory();
    assert!(directory.by_session("held").is_none());
    assert!(directory.gone.contains_key(HELD));
    assert_eq!(directory.focus, None);
    assert_eq!(helper_state(fleet).as_deref(), Some("abandoned"));
    assert_eq!(
        stored(fleet, HELPER).await.helper_state.as_deref(),
        Some("abandoned")
    );
    assert_eq!(place.told_deleted("held"), 3, "one roster event, two of its state");
    let kept = stored(fleet, HELD).await;
    assert!(kept.deleted_at_ms.is_some());
    assert!(kept.ledger_retired);
    assert_eq!(place.engine.opened(HELD), 2, "its thread, and its goals retired");
}

fn not_closed(answer: Result<bool, Unavailable>) {
    let Err(Unavailable(why)) = answer else {
        panic!("an agent that did not close was removed: {answer:?}");
    };
    assert_eq!(
        why,
        "The agent's thread did not answer within 3 seconds, so it is not removed yet: ask again to finish removing it."
    );
}

#[tokio::test]
async fn an_agent_that_answers_at_once_is_removed_whole() {
    let place = Place::new("close-at-once");
    let fleet = place.start();
    let asked = Instant::now();
    assert!(fleet.remove("held".into()).await.unwrap());
    assert!(asked.elapsed() < agent::CLOSE_WITHIN);
    removed_whole(&place, &fleet).await;
    // Asked again, there is no such agent.
    assert!(!fleet.remove("held".into()).await.unwrap());
    assert_eq!(place.told_deleted("held"), 3);
}

#[tokio::test]
async fn an_agent_that_answers_after_the_bound_is_removed_when_asked_again_and_nothing_twice() {
    let place = Place::new("close-late");
    place.engine.hold(HELD);
    let fleet = place.start();
    let asked = Instant::now();
    not_closed(fleet.remove("held".into()).await);
    let waited = asked.elapsed();
    assert!(waited >= agent::CLOSE_WITHIN, "{waited:?}");
    assert!(waited < agent::CLOSE_WITHIN * 2, "{waited:?}");
    let deleted_at = left_as_it_was(&place, &fleet).await;
    // Its thread answers late: it has answered, and the next ask finds it closed.
    place.engine.let_go(HELD);
    let again = Instant::now();
    assert!(fleet.remove("held".into()).await.unwrap());
    assert!(again.elapsed() < agent::CLOSE_WITHIN);
    removed_whole(&place, &fleet).await;
    // Deleted once: when it was first asked.
    assert_eq!(stored(&fleet, HELD).await.deleted_at_ms, Some(deleted_at));
}

#[tokio::test]
async fn an_agent_that_never_answers_holds_no_other_request_and_is_never_half_removed() {
    let place = Place::new("close-never");
    place.engine.hold(HELD);
    let fleet = place.start();
    not_closed(fleet.remove("held".into()).await);
    let deleted_at = left_as_it_was(&place, &fleet).await;
    // The fleet serves every other request at once.
    let served = Instant::now();
    let other = fleet.select("other".into()).await.unwrap().unwrap();
    assert_eq!(other.agent_id, OTHER);
    assert!(served.elapsed() < Duration::from_secs(1));
    assert!(fleet.select("held".into()).await.unwrap().is_some());
    // Asked again it is still not closed, and still left as it was; not stopped twice.
    not_closed(fleet.remove("held".into()).await);
    let directory = fleet.directory();
    assert!(directory.by_session("held").is_some());
    assert_eq!(helper_state(&fleet).as_deref(), Some("running"));
    assert_eq!(place.told_deleted("held"), 0);
    assert_eq!(place.engine.opened(HELD), 1);
    assert_eq!(stored(&fleet, HELD).await.deleted_at_ms, Some(deleted_at));
}

#[tokio::test]
async fn an_agent_whose_thread_has_ended_is_closed_at_once() {
    let place = Place::new("close-ended");
    place
        .engine
        .state
        .lock()
        .unwrap()
        .broken
        .insert(HELD.to_string());
    let fleet = place.start();
    let asked = Instant::now();
    assert!(fleet.remove("held".into()).await.unwrap());
    assert!(asked.elapsed() < Duration::from_secs(1));
    removed_whole(&place, &fleet).await;
}

#[tokio::test]
async fn a_second_delete_while_the_first_waits_waits_on_the_same_close() {
    let place = Place::new("close-twice");
    place.engine.hold(HELD);
    let fleet = place.start();
    let asked = Instant::now();
    let (first, second) = tokio::join!(fleet.remove("held".into()), fleet.remove("held".into()));
    not_closed(first);
    not_closed(second);
    // One after the other: the fleet is one worker, and each waits no longer than the bound.
    let waited = asked.elapsed();
    assert!(waited >= agent::CLOSE_WITHIN * 2, "{waited:?}");
    assert!(waited < agent::CLOSE_WITHIN * 3, "{waited:?}");
    let deleted_at = left_as_it_was(&place, &fleet).await;
    place.engine.let_go(HELD);
    assert!(fleet.remove("held".into()).await.unwrap());
    removed_whole(&place, &fleet).await;
    assert_eq!(stored(&fleet, HELD).await.deleted_at_ms, Some(deleted_at));
}

#[tokio::test]
async fn a_delete_while_the_host_stops_is_answered_and_the_next_start_removes_the_agent_whole() {
    let place = Place::new("close-stopping");
    place.engine.hold(HELD);
    let fleet = place.start();
    let thread = fleet.directory().by_session("held").unwrap().agent.clone();
    // The Host stops with the delete under way: nobody but the request holds the fleet.
    let asked = Instant::now();
    let request = tokio::spawn({
        let fleet = fleet.clone();
        async move { fleet.remove("held".into()).await }
    });
    drop(fleet);
    not_closed(request.await.unwrap());
    assert!(asked.elapsed() < agent::CLOSE_WITHIN * 2);
    assert_eq!(place.told_deleted("held"), 0);
    assert_eq!(place.engine.opened(HELD), 1);
    // The process ends; its thread with it (here: it answers the close it was asked).
    place.engine.let_go(HELD);
    assert!(thread.close(agent::CLOSE_WITHIN));
    // The next start finds its tombstone: it is not run, its goals are retired, the focus and
    // its helper let go of it.
    let fleet = place.start();
    let directory = fleet.directory();
    assert!(directory.by_session("held").is_none());
    assert!(directory.gone.contains_key(HELD));
    assert_eq!(directory.focus, None);
    assert_eq!(helper_state(&fleet).as_deref(), Some("abandoned"));
    assert_eq!(
        stored(&fleet, HELPER).await.helper_state.as_deref(),
        Some("abandoned")
    );
    assert!(stored(&fleet, HELD).await.ledger_retired);
    assert_eq!(place.engine.opened(HELD), 2, "its thread, and its goals retired");
    // A third start moves nothing again.
    drop(fleet);
    let fleet = place.start();
    assert_eq!(helper_state(&fleet).as_deref(), Some("abandoned"));
    assert_eq!(place.engine.opened(HELD), 2);
    // And the other agent is as it was.
    assert!(fleet.directory().by_session("other").is_some());
}
