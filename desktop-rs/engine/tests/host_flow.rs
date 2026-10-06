//! The engine against the fake Host (`tests/fake_host.py`), with no UI:
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
        let config = Config { base_url: base.into(), token: "probe-token".into(), settings: Settings::in_memory(), workspace_store: None, keyring: false, transcript_cache: None, form_events: None };
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
    assert_eq!(body["client"], "desktop", "the Host must know the desktop sent it, so the reply does not push to the phone");
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

#[test]
fn preferences_are_remembered() {
    let mut d = Driver::new("http://127.0.0.1:9");
    assert_eq!(d.engine.reading_theme(), "terminal");
    d.engine.set_reading_theme("paper");
    d.engine.set_muted(true);
    let changes = d.engine.pump();
    assert!(changes.contains(&Change::Preferences));
    assert_eq!((d.engine.reading_theme().as_str(), d.engine.muted()), ("paper", true));
    assert_eq!(d.engine.settings().string("appearance/readingTheme", ""), "paper");
    d.engine.set_reading_theme("no-such-theme");
    assert_eq!(d.engine.reading_theme(), "terminal", "an unknown theme falls back to the default");
}

#[test]
fn a_font_override_is_the_theme_s_own_and_survives_a_restart() {
    use clarp_core::reading_theme::FontOverride;
    let dir = std::env::temp_dir().join(format!("clarp-font-override-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let file = dir.join("settings.json");
    let _ = std::fs::remove_file(&file);
    let engine = || {
        let config = Config { base_url: "http://127.0.0.1:9".into(), token: "probe-token".into(), settings: Settings::at(file.clone()), workspace_store: None, keyring: false, transcript_cache: None, form_events: None };
        Engine::new(config, || {}).unwrap()
    };
    let mut e = engine();
    let mono = FontOverride { family: "JetBrains Mono".into(), size: Some(16.0) };
    e.set_font_override("hacker", Some(mono.clone()));
    assert!(e.pump().contains(&Change::Preferences), "the theme is applied again");
    assert_eq!(e.font_override("hacker"), Some(mono.clone()));
    assert_eq!(e.font_override("paper"), None, "another theme is unaffected");
    e.set_font_override("paper", Some(FontOverride { family: "Literata".into(), size: None }));
    drop(e);

    let mut e = engine();
    assert_eq!(e.font_override("hacker"), Some(mono), "kept across a restart");
    e.set_font_override("hacker", None);
    assert!(e.pump().contains(&Change::Preferences));
    assert_eq!(e.font_override("hacker"), None, "reset to the theme's default");
    assert_eq!(e.font_override("paper").map(|f| f.family), Some("Literata".into()), "and only that theme's");
    drop(e);
    assert_eq!(engine().font_override("hacker"), None, "the reset is saved too");
    std::fs::remove_dir_all(&dir).unwrap();
}

#[test]
fn drafts_and_attachments_belong_to_the_chat_and_survive_a_restart() {
    let host = Host::start("composer");
    let file = host.dir.join("settings.json");
    let driver = |base: &str| {
        let mut d = Driver::new(base);
        let settings = Settings::at(file.clone());
        let config = Config { base_url: base.into(), token: "probe-token".into(), settings, workspace_store: None, keyring: false, transcript_cache: None, form_events: None };
        let signal = d.woken.clone();
        d.engine = Engine::new(config, move || {
            let (flag, condvar) = &*signal;
            *flag.lock().unwrap() = true;
            condvar.notify_all();
        })
        .unwrap();
        d
    };
    let mut d = driver(&host.base);
    d.engine.start();
    d.until("rachel open", |e| e.conversation("rachel").is_some_and(|c| c.rows().len() == 2));
    d.engine.set_draft("rachel", "half a thought");
    assert_eq!(d.engine.draft("rachel"), "half a thought");
    assert_eq!(d.engine.draft("mike"), "", "a draft is the chat's own");

    let notes = host.dir.join("notes.txt");
    std::fs::write(&notes, "some notes").unwrap();
    d.engine.attach_file("rachel", &notes);
    assert_eq!(d.engine.attachments("rachel")[0]["status"], "uploading");
    assert!(!d.engine.can_send("rachel"), "an upload in flight holds the send");
    assert!(!d.engine.send_composer("rachel", "now", false));
    d.engine.clear_error();
    d.until("uploaded", |e| e.attachments("rachel").first().is_some_and(|a| a["status"] == "ready"));
    assert_eq!(d.engine.attachments("rachel")[0]["path"], "/srv/uploads/notes.txt");
    let upload = host.requests("POST", "/upload").pop().unwrap();
    assert_eq!((upload["body"]["name"].as_str(), upload["body"]["session"].as_str(), upload["body"]["size"].as_u64()), (Some("notes.txt"), Some("rachel"), Some(10)));

    let broken = host.dir.join("fail.txt");
    std::fs::write(&broken, "x").unwrap();
    d.engine.attach_file("mike", &broken);
    d.until("refused", |e| e.attachments("mike").first().is_some_and(|a| a["status"] == "failed"));
    assert!(d.engine.error().contains("upload refused"), "{:?}", d.engine.error());
    let failed = d.engine.attachments("mike")[0]["id"].as_str().unwrap().to_owned();
    d.engine.remove_attachment("mike", &failed);
    assert!(d.engine.attachments("mike").is_empty() && d.engine.can_send("mike"));
    d.engine.clear_error();

    // Restarting keeps the draft (written on close) and the attachment.
    drop(d);
    let mut d = driver(&host.base);
    assert_eq!(d.engine.draft("rachel"), "half a thought");
    assert_eq!(d.engine.attachments("rachel").len(), 1);
    d.engine.start();
    d.until("rachel open", |e| e.conversation("rachel").is_some_and(|c| c.rows().len() == 2));
    assert!(d.engine.send_composer("rachel", "Read this", true));
    assert!(d.engine.draft("rachel").is_empty() && d.engine.attachments("rachel").is_empty(), "sending clears both");
    d.until("sent", |_| host.requests("POST", "/send").iter().any(|r| r["body"]["text"] == "Read this /srv/uploads/notes.txt"));
    let send = host.requests("POST", "/send").pop().unwrap();
    assert_eq!(send["body"]["queue_if_busy"], true, "Ctrl+Enter queues behind the running turn");
}

#[test]
fn panes_follow_the_selection_and_their_layout_is_restored() {
    let host = Host::start("panes");
    let store = host.dir.join("workspaces.json");
    let driver = |base: &str| {
        let mut d = Driver::new(base);
        let config = Config { base_url: base.into(), token: "probe-token".into(), settings: Settings::in_memory(), workspace_store: Some(store.clone()), keyring: false, transcript_cache: None, form_events: None };
        let signal = d.woken.clone();
        d.engine = Engine::new(config, move || {
            let (flag, condvar) = &*signal;
            *flag.lock().unwrap() = true;
            condvar.notify_all();
        })
        .unwrap();
        d
    };
    let mut d = driver(&host.base);
    d.engine.start();
    d.until("rachel open", |e| e.conversation("rachel").is_some_and(|c| c.rows().len() == 2));
    assert_eq!(d.engine.panes().active_session(), "rachel", "the selection is the active pane's chat");
    d.engine.with_panes(|p| p.split_active("vertical", "mike"));
    assert_eq!(d.engine.panes().pane_count(), 2);
    assert_eq!(d.engine.selected_session(), "mike", "the new pane is active and its chat selected");
    d.until("mike loads in its pane", |e| e.conversation("mike").is_some_and(|c| !c.rows().is_empty()));
    let left = d.engine.panes().pane_layout().iter().find(|p| p["session"] == "rachel").unwrap()["id"].as_str().unwrap().to_owned();
    d.engine.with_panes(|p| p.focus_pane(&left));
    assert_eq!(d.engine.selected_session(), "rachel", "focusing a pane selects its chat");
    d.engine.select("mike");
    assert_eq!(d.engine.panes().active_session(), "mike", "choosing a chat puts it in the active pane");
    assert_eq!(d.engine.panes().pane_count(), 2);
    d.until("saved", |_| std::fs::read_to_string(&store).is_ok_and(|s| s.contains("mike") && s.contains("vertical")));

    drop(d);
    let mut d = driver(&host.base);
    assert_eq!(d.engine.panes().pane_count(), 2, "the layout is restored");
    d.engine.start();
    d.until("both panes load", |e| e.conversation("mike").is_some_and(|c| !c.rows().is_empty()) && e.selected_session() == "mike");
    assert!(d.engine.panes().pane_layout().iter().all(|p| p["session"] == "mike"), "{:?}", d.engine.panes().pane_layout());
}

#[test]
fn connecting_pairing_and_forgetting_a_host() {
    let host = Host::start("connection");
    let mut d = Driver::new("http://127.0.0.1:9");
    d.engine.connect_to_server(&host.base, "wrong-token");
    d.until("refused", |e| e.connection_state() == "unauthorized");
    d.engine.connect_to_server(&host.base, "probe-token");
    d.until("live", |e| e.connection_state() == "live" && e.roster().agents().len() == 2);
    assert_eq!(d.engine.settings().string("connection/baseUrl", ""), clarp_core::settings::normalized_base_url(&host.base));

    d.engine.pair_device(&host.base, "999999");
    d.until("pairing refused", |e| e.connection_state() == "offline" && e.error().contains("pairing code expired"));
    d.engine.pair_device(&host.base, "123456");
    d.until("paired and live", |e| e.connection_state() == "live");
    let exchange = host.requests("POST", "/pairing/exchange").pop().unwrap();
    assert_eq!(exchange["body"]["device_name"], "Clarp desktop");
    let info = host.requests("GET", "/server-info");
    assert!(info.last().is_some_and(|r| r["authorization"] == "Bearer cld_probe_paired_device"), "the paired token is used: {info:?}");
    assert!(!d.engine.has_stored_credential(), "with the keyring off nothing is stored");

    d.engine.forget_credential();
    d.changes.extend(d.engine.pump());
    assert_eq!(d.engine.connection_state(), "offline");
}

#[test]
fn tool_activity_is_explained_in_plain_english_once_asked() {
    let host = Host::start("narrator");
    let mut d = Driver::new(&host.base);
    d.engine.start();
    d.until("live", |e| e.connection_state() == "live");
    let activity = serde_json::json!({"id": "act-1", "name": "Bash", "summary": "list the files"}).as_object().cloned().unwrap();
    assert!(d.engine.explanation_for("rachel", &activity).is_empty(), "nothing is asked while the experiment is off");
    d.engine.set_narrator_enabled(true);
    d.engine.set_narrator_detail_level(3);
    assert_eq!(d.engine.settings().integer("experiments/toolDetailLevel", 0), 3, "the level is kept");
    d.engine.request_explanation("rachel", &activity);
    d.until("explained", |e| e.explanation_for("rachel", &activity) == "Explained: list the files");
    let asked = host.requests("POST", "/tool-explanations");
    assert!(asked.len() >= 2, "a pending answer is polled: {asked:?}");
    assert!(d.changes.contains(&Change::Narrator));
}
