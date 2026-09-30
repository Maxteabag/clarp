//! The engine against the fake Host (`app/tests/fake_host.py`), with no UI:
//! connect, roster, selection, transcript, send and its confirmation, stop.

use std::process::{Child, Command};
use std::sync::{Arc, Condvar, Mutex};
use std::time::{Duration, Instant};

use clarp_core::settings::Settings;
use clarp_engine::{Change, Config, Engine};

struct Host {
    child: Child,
    base: String,
    log: std::path::PathBuf,
    dir: std::path::PathBuf,
}

impl Host {
    fn start(name: &str) -> Self {
        let dir = std::env::temp_dir().join(format!("clarp-engine-{name}-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let (port, log) = (dir.join("port"), dir.join("host.log"));
        let script = concat!(env!("CARGO_MANIFEST_DIR"), "/../app/tests/fake_host.py");
        let child = Command::new("/usr/bin/python3")
            .args([script, "--port-file", port.to_str().unwrap(), "--log", log.to_str().unwrap()])
            .spawn()
            .expect("fake host starts");
        let deadline = Instant::now() + Duration::from_secs(10);
        while std::fs::read_to_string(&port).map(|p| p.trim().is_empty()).unwrap_or(true) {
            assert!(Instant::now() < deadline, "fake host did not start");
            std::thread::sleep(Duration::from_millis(50));
        }
        let base = format!("http://127.0.0.1:{}", std::fs::read_to_string(&port).unwrap().trim());
        Host { child, base, log, dir }
    }

    fn requests(&self, method: &str, path: &str) -> Vec<serde_json::Value> {
        std::fs::read_to_string(&self.log)
            .unwrap_or_default()
            .lines()
            .filter_map(|l| serde_json::from_str::<serde_json::Value>(l).ok())
            .filter(|r| r["method"] == method && r["path"] == path)
            .collect()
    }
}

impl Drop for Host {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
        let _ = std::fs::remove_dir_all(&self.dir);
    }
}

/// An engine whose wake just signals this thread, which pumps it.
struct Driver {
    engine: Engine,
    woken: Arc<(Mutex<bool>, Condvar)>,
    changes: Vec<Change>,
}

impl Driver {
    fn new(base: &str) -> Self {
        let woken = Arc::new((Mutex::new(false), Condvar::new()));
        let signal = woken.clone();
        let config = Config { base_url: base.into(), token: "probe-token".into(), settings: Settings::in_memory() };
        let engine = Engine::new(config, move || {
            let (flag, condvar) = &*signal;
            *flag.lock().unwrap() = true;
            condvar.notify_all();
        })
        .unwrap();
        Driver { engine, woken, changes: Vec::new() }
    }

    /// Pumps until `done` holds or the deadline passes.
    fn until(&mut self, what: &str, done: impl Fn(&Engine) -> bool) {
        let deadline = Instant::now() + Duration::from_secs(10);
        loop {
            self.changes.extend(self.engine.pump());
            if done(&self.engine) {
                return;
            }
            assert!(Instant::now() < deadline, "timed out waiting for: {what} (error: {:?})", self.engine.error());
            let (flag, condvar) = &*self.woken;
            let guard = flag.lock().unwrap();
            let (mut guard, _) = condvar.wait_timeout_while(guard, Duration::from_millis(100), |w| !*w).unwrap();
            *guard = false;
        }
    }
}

#[test]
fn connects_loads_the_roster_and_opens_the_first_chat() {
    let host = Host::start("connect");
    let mut d = Driver::new(&host.base);
    d.engine.start();
    d.until("live", |e| e.connection_state() == "live" && e.roster().agents().len() == 2);
    assert!(d.changes.contains(&Change::Connection) && d.changes.contains(&Change::Roster));
    d.until("rachel's transcript", |e| e.selected_session() == "rachel" && e.conversation("rachel").is_some_and(|c| c.rows().len() == 2));
    assert!(d.changes.contains(&Change::Selection));
    assert_eq!(host.requests("POST", "/select").last().unwrap()["body"]["session"], "rachel");
    assert!(d.engine.error().is_empty());
}

#[test]
fn a_send_shows_at_once_and_is_confirmed_by_the_log() {
    let host = Host::start("send");
    let mut d = Driver::new(&host.base);
    d.engine.start();
    d.until("rachel open", |e| e.conversation("rachel").is_some_and(|c| c.rows().len() == 2));
    d.engine.send("Hello from the engine");
    let optimistic = d.engine.conversation("rachel").unwrap().rows().last().unwrap().clone();
    assert!(optimistic.id.starts_with("u-") && optimistic.pending, "the message shows at once, pending");
    assert!(d.engine.sending());
    d.until("confirmed and answered", |e| {
        let rows = e.conversation("rachel").unwrap().rows();
        !e.sending() && rows.iter().any(|r| r.text == "Echo: Hello from the engine")
    });
    let sent = d.engine.conversation("rachel").unwrap().rows().iter().find(|r| r.id == optimistic.id).cloned().unwrap();
    assert!(!sent.pending && !sent.delivery_failed);
    let body = &host.requests("POST", "/send")[0]["body"];
    assert_eq!((body["session"].as_str(), body["text"].as_str()), (Some("rachel"), Some("Hello from the engine")));
    d.engine.stop();
    d.until("stop posted", |_| !host.requests("POST", "/stop").is_empty());
    assert_eq!(host.requests("POST", "/stop")[0]["body"]["session"], "rachel");
}

#[test]
fn an_unreachable_host_is_offline_with_an_error() {
    let mut d = Driver::new("http://127.0.0.1:9");
    d.engine.start();
    d.until("offline", |e| e.connection_state() == "offline" && !e.error().is_empty());
    assert!(!d.engine.connected());
}

#[test]
fn pair_rooms_load_unread_and_are_read_once_opened() {
    let host = Host::start("rooms");
    let mut d = Driver::new(&host.base);
    d.engine.start();
    d.until("rooms", |e| !e.rooms().is_empty());
    assert_eq!(d.engine.rooms().len(), 1, "only pair: conversations are rooms");
    assert_eq!(d.engine.unread_rooms(), 1);
    assert_eq!(d.engine.chat_name("pair:a1:a2"), "Rachel & Mike");
    d.engine.select("pair:a1:a2");
    d.until("room opened and read", |e| e.unread_rooms() == 0 && e.conversation("pair:a1:a2").is_some_and(|c| !c.rows().is_empty()));
    assert!(host.requests("POST", "/select").iter().all(|r| r["body"]["session"] != "pair:a1:a2"), "a room takes no Host focus");
}
