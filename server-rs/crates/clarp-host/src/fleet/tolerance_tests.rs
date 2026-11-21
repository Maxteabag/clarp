//! A record the fleet cannot read stops nothing: it is left out, kept as it was, counted and
//! said by its kind and key; its names stay its own; a store that does not answer stops the
//! start (`kept.rs`).
use super::*;
use clarp_ports::{
    blobs::BlobEngine,
    clock::SystemClock,
    kv::{KeyValueEngine, StoreError, Write},
    log::{AgentLog, LogError},
};
use clarp_store_mem::{MemBlobs, MemEngine, MemKeyValue};
use std::sync::{
    Mutex,
    atomic::{AtomicBool, Ordering},
};

/// The agents' logs, of which one does not open while `cut` is set.
struct CutOpen {
    logs: MemEngine,
    cut: AtomicBool,
}

impl LogEngine for CutOpen {
    fn name(&self) -> &'static str {
        self.logs.name()
    }
    fn open(&self, path: &Path, codecs: CodecSet) -> Result<Box<dyn AgentLog>, LogError> {
        if self.cut.load(Ordering::SeqCst) {
            return Err(LogError::Storage("the log does not open".into()));
        }
        self.logs.open(path, codecs)
    }
}

/// A store that does not answer when its tables are listed.
struct Silent;

impl KeyValue for Silent {
    fn get(&self, _: &str, _: &str) -> Result<Option<Vec<u8>>, StoreError> {
        Err(StoreError("no answer".into()))
    }
    fn put(&mut self, _: &str, _: &str, _: &[u8]) -> Result<(), StoreError> {
        Err(StoreError("no answer".into()))
    }
    fn put_together(&mut self, _: &[Write]) -> Result<(), StoreError> {
        Err(StoreError("no answer".into()))
    }
    fn delete(&mut self, _: &str, _: &str) -> Result<bool, StoreError> {
        Err(StoreError("no answer".into()))
    }
    fn list(&self, _: &str) -> Result<Vec<(String, Vec<u8>)>, StoreError> {
        Err(StoreError("no answer".into()))
    }
}

struct Place {
    dir: PathBuf,
    engine: Arc<CutOpen>,
    roster: MemKeyValue,
    said: Arc<Mutex<Vec<String>>>,
}

impl Drop for Place {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

impl Place {
    fn new(name: &str) -> Self {
        let dir = std::env::temp_dir().join(format!("clarp-{name}-{}", crate::ids::hex(8)));
        std::fs::create_dir(&dir).unwrap();
        Self {
            dir,
            engine: Arc::new(CutOpen {
                logs: MemEngine::new(),
                cut: AtomicBool::new(false),
            }),
            roster: MemKeyValue::new(),
            said: Arc::default(),
        }
    }

    /// The store as its own crate writes it, before a start.
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
        let said = self.said.clone();
        Works {
            teams: None,
            runtime: tokio::runtime::Handle::current(),
            opening_gate: crate::calls::opening_launch::channel().0,
            config: Arc::new(config),
            engine: self.engine.clone(),
            codecs: clarp_codec::standard_set("none").unwrap(),
            launcher: Arc::new(Launcher::by_name("direct", self.dir.join("no-shim")).unwrap()),
            clock: Arc::new(SystemClock),
            hub: Arc::new(Hub::new(0)),
            revisions: Arc::default(),
            tool_env: vec![],
            pusher: crate::pusher::pusher().0,
            blobs: MemBlobs::new().open(&self.dir).unwrap(),
            portrait_hints: None,
            speaks: crate::voice::mouth().0,
            telling: crate::telling::Telling::new(crate::owner::OwnerFile::new(Some(
                self.dir.join("absent"),
            ))),
            tally: crate::usage::tally().0,
            turns: Arc::default(),
            pools: vec![],
            log: crate::unkept::Log::to(
                move |line| said.lock().unwrap().push(line.to_string()),
                Arc::new(SystemClock),
            ),
            left_out: Arc::default(),
        }
    }

    fn start(&self) -> FleetHandle {
        start(self.works(), self.store()).unwrap()
    }

    fn said(&self) -> Vec<String> {
        self.said.lock().unwrap().clone()
    }
}

/// An agent's record as a Host writes it, for the stores written before a start.
fn agent(id: &str, session: &str, persona: &str) -> Vec<u8> {
    let record = serde_json::json!({
        "agent_id": id, "session": session, "persona": persona, "backend": "scripted",
        "cwd": "/", "conversation_id": "c-1", "created_at_ms": 1,
    });
    serde_json::to_vec(&record).unwrap()
}

/// What no kind can read: synthetic bytes, none of them anyone's.
const NO_JSON: &[u8] = b"not json at all";
/// JSON that names an agent, in a shape no Host wrote.
const ANOTHER_SHAPE: &[u8] = br#"{"session":"kept-name","persona":"Kept Name","zebra":2}"#;
/// A push token, which is a secret: not one sign of it is ever said.
const TOKEN: &str = "0123456789abcdefghijklmnopqrstuv";

fn counts(fleet: &FleetHandle) -> BTreeMap<String, u64> {
    fleet.left_out().counts()
}

#[tokio::test]
async fn a_record_that_cannot_be_read_is_left_out_kept_as_it_was_and_counted() {
    let place = Place::new("left-out");
    let mut store = place.store();
    store
        .put(AGENTS, "agent-good", &agent("agent-good", "good", "Good"))
        .unwrap();
    store.put(AGENTS, "agent-bytes", NO_JSON).unwrap();
    store.put(AGENTS, "agent-shape", ANOTHER_SHAPE).unwrap();
    // An agent this Host cannot run (no tool of that name here): read, and not set up.
    let mut elsewhere: serde_json::Value =
        serde_json::from_slice(&agent("agent-elsewhere", "elsewhere", "Elsewhere")).unwrap();
    elsewhere["backend"] = serde_json::json!("no-such-tool");
    let elsewhere = serde_json::to_vec(&elsewhere).unwrap();
    store.put(AGENTS, "agent-elsewhere", &elsewhere).unwrap();
    for table in [DEVICES, PERSONAS, LOOKS, PEERS] {
        store.put(table, &format!("{table}-bad"), NO_JSON).unwrap();
    }
    store.put(PUSH, TOKEN, b"[]").unwrap();
    // A book's record (the chip's, which its desk lists): set aside, not left in place for a desk.
    store.put("kept.chip", "chip-bad", NO_JSON).unwrap();
    drop(store);

    // The Host starts, and runs what it can read.
    let fleet = place.start();
    assert!(fleet.directory().by_session("good").is_some());
    let expected: BTreeMap<String, u64> = [
        ("agent", 2),
        ("chip", 1),
        ("device", 1),
        ("peer", 1),
        ("persona", 1),
        ("persona-look", 1),
        ("push", 1),
    ]
    .into_iter()
    .map(|(kind, count)| (kind.to_string(), count))
    .collect();
    assert_eq!(counts(&fleet), expected);
    assert!(
        fleet
            .left_out()
            .line()
            .contains("8 records read at the start could not be read")
    );
    // The book's record is moved aside, its key free, and no desk that lists the kind sees the
    // copy.
    let copy = place
        .store()
        .get("kept.chip", "chip-bad.unreadable")
        .unwrap();
    assert_eq!(copy.as_deref(), Some(NO_JSON));
    assert_eq!(place.store().get("kept.chip", "chip-bad").unwrap(), None);
    let listed = fleet.kept_all("chip").await.unwrap();
    assert!(
        listed.iter().all(|(key, _)| !key.ends_with(".unreadable")),
        "{listed:?}"
    );
    // Each is said once, by its kind and key; the token, a secret, by its place alone, and nothing
    // of what any record holds.
    let said = place.said();
    for (kind, key) in [
        ("agent", "agent-bytes"),
        ("agent", "agent-shape"),
        ("device", "device-bad"),
    ] {
        let line = format!("a record of the kind {kind} could not be read and is left out: {key}");
        assert_eq!(
            said.iter().filter(|s| s.ends_with(&line)).count(),
            1,
            "{said:?}"
        );
    }
    assert!(
        said.iter()
            .any(|s| s
                .ends_with("kind push could not be read and is left out: 1 of this kind so far")),
        "{said:?}"
    );
    for line in &said {
        let leaked = [TOKEN, "not json", "zebra", "Kept Name", "kept-name"];
        assert!(
            leaked.iter().all(|content| !line.contains(content)),
            "{line}"
        );
    }

    // A left-out agent's names are its own: neither its session nor the name it goes by is
    // given to another.
    let asked = |name: &str, session: Option<&str>| {
        let request =
            serde_json::json!({ "name": name, "backend": "scripted", "session": session });
        serde_json::from_value::<CreateAgentRequest>(request).unwrap()
    };
    let refused = fleet.create(asked("Kept Name", None)).await.unwrap();
    assert!(
        matches!(&refused, Err(NotMade::Occupied { session, .. }) if session == "kept-name"),
        "{refused:?}"
    );
    let refused = fleet.create(asked("Elsewhere", None)).await.unwrap();
    assert!(
        matches!(&refused, Err(NotMade::Occupied { session, .. }) if session == "elsewhere"),
        "{refused:?}"
    );
    let made = fleet
        .create(asked("Someone", Some("kept-name")))
        .await
        .unwrap()
        .unwrap();
    assert_ne!(made.session, "kept-name");

    // The Host wrote since, and what it could not read is there byte for byte.
    let store = place.store();
    assert_eq!(
        store.get(AGENTS, "agent-bytes").unwrap().as_deref(),
        Some(NO_JSON)
    );
    assert_eq!(
        store.get(AGENTS, "agent-shape").unwrap().as_deref(),
        Some(ANOTHER_SHAPE)
    );
    assert_eq!(store.get(PUSH, TOKEN).unwrap().as_deref(), Some(&b"[]"[..]));
    drop(fleet);

    // Mended where it is kept, it is read again at the next start, and no longer counted.
    let mut store = place.store();
    store
        .put(
            AGENTS,
            "agent-bytes",
            &agent("agent-bytes", "mended", "Mended"),
        )
        .unwrap();
    store
        .put(
            PEERS,
            "peer-bad",
            br#"{"url":"http://peer.invalid","token":"t"}"#,
        )
        .unwrap();
    drop(store);
    let fleet = place.start();
    assert!(fleet.directory().by_session("mended").is_some());
    let counted = counts(&fleet);
    assert_eq!(
        (counted.get("agent"), counted.get("peer")),
        (Some(&1), None),
        "{counted:?}"
    );
}

#[tokio::test]
async fn the_owners_write_to_a_key_that_was_left_out_repairs_it_and_nothing_else_writes_it() {
    let place = Place::new("repaired");
    let mut store = place.store();
    store.put(PEERS, "far", NO_JSON).unwrap();
    store.put(PEERS, "near", NO_JSON).unwrap();
    drop(store);
    let fleet = place.start();
    assert_eq!(counts(&fleet).get("peer"), Some(&2));
    // The owner names the peer again: that write is the repair, said, and no longer counted.
    let (url, token) = ("http://far.invalid".to_string(), "t".to_string());
    fleet
        .peer_add("far".to_string(), url, token)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(counts(&fleet).get("peer"), Some(&1));
    let said = place.said();
    assert!(
        said.iter()
            .any(|s| s.ends_with("could not be read was written anew: far")),
        "{said:?}"
    );
    // The other is as it was.
    assert_eq!(
        place.store().get(PEERS, "near").unwrap().as_deref(),
        Some(NO_JSON)
    );
}

#[tokio::test]
async fn a_deleted_agent_whose_goals_cannot_be_retired_is_left_for_the_next_start() {
    let place = Place::new("retire-later");
    let mut deleted: serde_json::Value =
        serde_json::from_slice(&agent("gone", "gone", "Gone")).unwrap();
    deleted["deleted_at_ms"] = serde_json::json!(5);
    let mut store = place.store();
    store
        .put(AGENTS, "gone", &serde_json::to_vec(&deleted).unwrap())
        .unwrap();
    store
        .put(AGENTS, "here", &agent("here", "here", "Here"))
        .unwrap();
    drop(store);
    // Its log does not open: the Host starts all the same, the deleted agent is counted.
    place.engine.cut.store(true, Ordering::SeqCst);
    let fleet = place.start();
    assert_eq!(counts(&fleet).get("agent-ledger"), Some(&1));
    drop(fleet);
    // At the next start it is retired, and no longer counted.
    place.engine.cut.store(false, Ordering::SeqCst);
    let fleet = place.start();
    assert!(counts(&fleet).is_empty(), "{:?}", counts(&fleet));
    assert!(
        fleet
            .stored_agent("gone".to_string())
            .await
            .unwrap()
            .unwrap()
            .ledger_retired
    );
    assert!(fleet.directory().by_session("here").is_some());
}

#[tokio::test]
async fn a_store_that_does_not_answer_stops_the_start() {
    let place = Place::new("silent");
    let Err(why) = start(place.works(), Box::new(Silent)) else {
        panic!("a store that does not answer stopped nothing");
    };
    assert!(why.contains("no answer"), "{why}");
}

#[tokio::test]
async fn a_kind_judged_when_read_is_judged_at_its_listing_and_not_at_the_start() {
    let place = Place::new("judged-when-read");
    let mut store = place.store();
    store
        .put("kept.oracle-delegation", "d-bad", NO_JSON)
        .unwrap();
    store
        .put("kept.keeper-progress", "attachment-1", br#"{"seen":1}"#)
        .unwrap();
    // Not read at the start: nothing of it is counted there.
    let fleet = place.start();
    assert!(counts(&fleet).is_empty());
    // Listed by its desk: the one judge at that read, the same as the walk's.
    let listed = fleet.kept_listed("oracle-delegation").await.unwrap();
    assert!(listed.is_empty());
    assert_eq!(counts(&fleet).get("oracle-delegation"), Some(&1));
    let aside = place
        .store()
        .get("kept.oracle-delegation", "d-bad.unreadable");
    assert_eq!(aside.unwrap().as_deref(), Some(NO_JSON));
    // What is its desk's is answered.
    let progress = fleet.kept_listed("keeper-progress").await.unwrap();
    assert_eq!(progress.len(), 1);
}

#[tokio::test]
async fn a_row_left_out_is_seen_by_no_desk_until_the_owners_write_repairs_it() {
    let place = Place::new("rows-hidden");
    let mut store = place.store();
    store.put("kept.opening_history", "r-bad", NO_JSON).unwrap();
    let fleet = place.start();
    // Left out at the start: no read of a desk meets it, by key or whole; it is where it was.
    assert!(fleet.kept_all("opening_history").await.unwrap().is_empty());
    let asked = fleet.kept("opening_history", "r-bad".into()).await.unwrap();
    assert_eq!(asked, None);
    let kept = place.store().get("kept.opening_history", "r-bad").unwrap();
    assert_eq!(kept.as_deref(), Some(NO_JSON));
    // The owner's write repairs it: seen again.
    fleet
        .keep("opening_history", "r-bad".into(), Some(b"{}".to_vec()))
        .await
        .unwrap();
    let asked = fleet.kept("opening_history", "r-bad".into()).await.unwrap();
    assert_eq!(asked.as_deref(), Some(&b"{}"[..]));
}
