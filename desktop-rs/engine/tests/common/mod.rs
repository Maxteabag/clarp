//! The fake Host (`tests/fake_host.py`) and an engine driver, shared by
//! the integration tests that are not `host_flow.rs`.
#![allow(dead_code)]

use std::io::{Read, Write};
use std::process::{Child, Command};
use std::sync::{Arc, Condvar, Mutex};
use std::time::{Duration, Instant};

use clarp_core::settings::Settings;
use clarp_engine::{Change, Config, Engine};

pub struct Host {
    child: Child,
    pub base: String,
    log: std::path::PathBuf,
    pub dir: std::path::PathBuf,
}

impl Host {
    pub fn start(name: &str) -> Self {
        let dir = std::env::temp_dir().join(format!("clarp-engine-{name}-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let (port, log) = (dir.join("port"), dir.join("host.log"));
        let script = concat!(env!("CARGO_MANIFEST_DIR"), "/../tests/fake_host.py");
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

    pub fn requests(&self, method: &str, path: &str) -> Vec<serde_json::Value> {
        std::fs::read_to_string(&self.log)
            .unwrap_or_default()
            .lines()
            .filter_map(|l| serde_json::from_str::<serde_json::Value>(l).ok())
            .filter(|r| r["method"] == method && r["path"] == path)
            .collect()
    }

    /// Posts to one of the fake Host's `/__control/…` test endpoints.
    pub fn control(&self, path: &str, body: serde_json::Value) {
        let address = self.base.trim_start_matches("http://");
        let mut stream = std::net::TcpStream::connect(address).expect("fake host reachable");
        let body = body.to_string();
        write!(
            stream,
            "POST {path} HTTP/1.1\r\nHost: {address}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        )
        .unwrap();
        let mut response = String::new();
        stream.read_to_string(&mut response).unwrap();
        assert!(response.starts_with("HTTP/1.1 200"), "control {path} failed: {response}");
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
pub struct Driver {
    pub engine: Engine,
    woken: Arc<(Mutex<bool>, Condvar)>,
    pub changes: Vec<Change>,
}

impl Driver {
    pub fn new(base: &str) -> Self {
        Self::with_settings(base, Settings::in_memory())
    }

    pub fn with_settings(base: &str, settings: Settings) -> Self {
        let woken = Arc::new((Mutex::new(false), Condvar::new()));
        let signal = woken.clone();
        let config = Config { base_url: base.into(), token: "probe-token".into(), settings, workspace_store: None, keyring: false };
        let engine = Engine::new(config, move || {
            let (flag, condvar) = &*signal;
            *flag.lock().unwrap() = true;
            condvar.notify_all();
        })
        .unwrap();
        Driver { engine, woken, changes: Vec::new() }
    }

    /// Pumps until `done` holds or the deadline passes.
    pub fn until(&mut self, what: &str, done: impl Fn(&Engine) -> bool) {
        self.until_with(what, |engine, _| done(engine));
    }

    /// Pumps until `change` has been reported.
    pub fn until_change(&mut self, change: &Change) {
        self.until_with(&format!("{change:?}"), |_, changes| changes.contains(change));
    }

    fn until_with(&mut self, what: &str, done: impl Fn(&Engine, &[Change]) -> bool) {
        let deadline = Instant::now() + Duration::from_secs(10);
        loop {
            self.changes.extend(self.engine.pump());
            if done(&self.engine, &self.changes) {
                return;
            }
            assert!(Instant::now() < deadline, "timed out waiting for: {what} (error: {:?})", self.engine.error());
            let (flag, condvar) = &*self.woken;
            let guard = flag.lock().unwrap();
            let (mut guard, _) = condvar.wait_timeout_while(guard, Duration::from_millis(100), |w| !*w).unwrap();
            *guard = false;
        }
    }

    /// Pumps for `duration`, whatever arrives.
    pub fn settle(&mut self, duration: Duration) {
        let end = Instant::now() + duration;
        while Instant::now() < end {
            self.changes.extend(self.engine.pump());
            std::thread::sleep(Duration::from_millis(20));
        }
        self.changes.extend(self.engine.pump());
    }

    /// Starts and waits for the live stream and at least the two seeded agents.
    pub fn connect(&mut self) {
        self.engine.start();
        self.until("live", |e| e.connection_state() == "live" && e.connected() && e.roster().agents().len() >= 2);
    }
}
