//! The Updates, Teams and agent profile panels against the fake Host
//! (`tests/fake_host.py`), with no UI.

use std::io::{Read, Write};
use std::process::{Child, Command};
use std::sync::{Arc, Condvar, Mutex};
use std::time::{Duration, Instant};

use clarp_core::settings::Settings;
use clarp_engine::{Change, Config, Engine};
use serde_json::{Value, json};

struct Host {
    child: Child,
    base: String,
    port: u16,
    log: std::path::PathBuf,
    dir: std::path::PathBuf,
}

impl Host {
    fn start(name: &str) -> Self {
        let dir = std::env::temp_dir().join(format!("clarp-engine-panels-{name}-{}", std::process::id()));
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
        let port: u16 = std::fs::read_to_string(&port).unwrap().trim().parse().unwrap();
        Host { child, base: format!("http://127.0.0.1:{port}"), port, log, dir }
    }

    fn requests(&self, method: &str, path: &str) -> Vec<Value> {
        std::fs::read_to_string(&self.log)
            .unwrap_or_default()
            .lines()
            .filter_map(|l| serde_json::from_str::<Value>(l).ok())
            .filter(|r| r["method"] == method && r["path"] == path)
            .collect()
    }

    /// Posts to one of the fake Host's `/__control/` endpoints.
    fn control(&self, path: &str, body: Value) {
        let body = body.to_string();
        let mut stream = std::net::TcpStream::connect(("127.0.0.1", self.port)).unwrap();
        write!(
            stream,
            "POST /__control/{path} HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        )
        .unwrap();
        let mut reply = String::new();
        stream.read_to_string(&mut reply).unwrap();
        assert!(reply.starts_with("HTTP/1.1 200") || reply.starts_with("HTTP/1.0 200"), "control {path}: {reply}");
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

    fn live(host: &Host) -> Self {
        let mut d = Driver::new(&host.base);
        d.engine.start();
        d.until("live", |e| e.connection_state() == "live" && e.roster().agents().len() == 2);
        d
    }

    /// Pumps until `change` has been signalled.
    fn until_change(&mut self, what: &str, change: Change) {
        let deadline = Instant::now() + Duration::from_secs(10);
        while !self.changes.contains(&change) {
            assert!(Instant::now() < deadline, "timed out waiting for: {what} (error: {:?})", self.engine.error());
            self.until(what, |_| true);
            std::thread::sleep(Duration::from_millis(20));
        }
    }

    /// Whether `change` was signalled since the last call, which forgets it.
    fn saw(&mut self, change: &Change) -> bool {
        let seen = self.changes.contains(change);
        self.changes.retain(|c| c != change);
        seen
    }
}

#[test]
fn updates_load_after_connecting_and_a_decision_resolves() {
    let host = Host::start("updates");
    let mut d = Driver::live(&host);
    // Not requested at once: the updates follow the sidebar.
    d.until("updates loaded", |e| e.attention_count() == 1 && e.update_artifacts().len() == 4 && !e.updates_loading());
    assert!(d.saw(&Change::Updates));
    assert_eq!(host.requests("GET", "/artifacts")[0]["query"], json!({"limit": "50", "order": "updated"}));
    assert_eq!(d.engine.selected_session(), "rachel");
    assert_eq!(d.engine.next_attention_session(), "mike", "a pending decision makes its chat the next target");
    assert_eq!(d.engine.artifacts_for_session("rachel").len(), 2);
    let report = d.engine.report_for_artifact("doc1").expect("a document is a report");
    assert_eq!((report["isHtml"].as_bool(), report["body"].as_str()), (Some(false), Some("# Findings\n\nAll *good*.")));
    assert!(d.engine.report_for_artifact("art1").is_none(), "an artifact without a body is no report");
    assert!(d.engine.updates_error().is_empty());

    d.engine.resolve_decision("d1", "yes", 3);
    assert!(d.engine.update_action_pending("decision", "d1"));
    d.engine.resolve_decision("d1", "yes", 3);
    d.until("resolved and reloaded", |e| !e.update_action_pending("decision", "d1") && e.attention_count() == 0 && !e.updates_loading());
    let resolves = host.requests("POST", "/decisions/d1/resolve");
    assert_eq!(resolves.len(), 1, "a pending action is not sent twice");
    assert_eq!(resolves[0]["body"], json!({"choice": "accepted", "expected_revision": 3}));
    assert_eq!(d.engine.next_attention_session(), "", "nothing else wants the user");
    assert!(d.saw(&Change::Updates));

    d.engine.resolve_decision("d2", "maybe", 1);
    assert!(!d.engine.update_action_pending("decision", "d2"), "an unknown choice is not sent");
}

#[test]
fn a_job_event_updates_the_tracker_and_a_job_can_be_cancelled() {
    let host = Host::start("jobs");
    let mut d = Driver::live(&host);
    d.until("updates loaded", |e| e.update_artifacts().len() == 4 && !e.updates_loading());
    d.saw(&Change::Processes);
    let job = json!({"job_id": "j1", "agent_id": "a1", "session": "rachel", "status": "running", "title": "Index",
                     "metadata": {"completed": 1, "total": 4}, "updated_at": 5});
    host.control("jobs", json!({"jobs": [job], "event": {"type": "background-job-updated", "job": job}}));
    d.until("job listed", |e| e.background_jobs().len() == 1 && !e.updates_loading());
    assert!(d.saw(&Change::Processes), "the tracker change is signalled");
    let listed = d.engine.background_jobs()[0].as_object().unwrap().clone();
    assert_eq!(d.engine.background_job_progress(&listed), 0.25);
    let processes = d.engine.agent_processes("rachel").expect("rachel's processes");
    assert_eq!((processes["jobs"][0]["jobId"].as_str(), processes["jobCount"].as_i64()), (Some("j1"), Some(1)), "{processes:?}");

    d.engine.cancel_background_job("j1");
    assert!(d.engine.update_action_pending("job", "j1"));
    d.until("cancelled", |e| !e.update_action_pending("job", "j1"));
    assert_eq!(host.requests("DELETE", "/background-jobs/j1").len(), 1);
}

#[test]
fn teams_load_select_the_first_and_reload_after_an_action() {
    let host = Host::start("teams");
    let mut d = Driver::live(&host);
    d.engine.load_teams();
    assert!(d.engine.teams_loading());
    d.until("teams and messages", |e| e.teams().len() == 1 && !e.team_messages().is_empty() && !e.teams_loading());
    assert!(d.saw(&Change::Teams));
    assert_eq!(d.engine.selected_team_id(), "t1", "the first team shows");
    assert_eq!(d.engine.team_messages()[0]["text"], "Standup at 9");
    assert_eq!(host.requests("GET", "/teams/t1/messages")[0]["query"]["limit"], "100");
    assert_eq!(d.engine.team_name_by_id("t1"), "Core");
    assert_eq!(d.engine.team_name_by_id("gone"), "gone");
    assert_eq!(d.engine.agent_name_by_id("a1"), "Rachel");
    assert_eq!(d.engine.team_agent_choices()[1], json!({"id": "a2", "session": "mike", "name": "Mike"}));

    d.engine.create_team("  Ops  ", "#0f0");
    d.until("the new team listed", |e| e.teams().len() == 2 && !e.teams_loading());
    assert_eq!(host.requests("POST", "/teams")[0]["body"], json!({"name": "Ops", "color": "#0f0"}));
    assert_eq!(d.engine.selected_team_id(), "t1", "the selection survives the reload");

    d.engine.update_team("t1", "Core", "#fff", "a2");
    assert_eq!(d.engine.teams_error(), "Team leader must already be a member");
    assert!(d.saw(&Change::Teams));
    d.engine.add_team_member("t1", "a2");
    d.until("member added", |e| e.team("t1").is_some_and(|t| t["member_agent_ids"].as_array().is_some_and(|m| m.len() == 2)));
    assert_eq!(host.requests("POST", "/teams/t1/members")[0]["body"], json!({"agent_id": "a2"}));
    d.engine.set_team_nudging("t1", true);
    d.engine.delete_team("t2");
    d.until("deleted", |e| e.teams().len() == 1 && !e.teams_loading());
    assert_eq!(host.requests("POST", "/team-nudging")[0]["body"], json!({"team_id": "t1", "nudge_enabled": true}));
    assert_eq!(host.requests("DELETE", "/teams/t2").len(), 1);
}

#[test]
fn a_profile_loads_its_plan_prompts_updates_and_teams_and_changes_settings() {
    let host = Host::start("profile");
    let mut d = Driver::live(&host);
    d.until("model catalog and MCP servers", |e| e.model_catalog_loaded() && e.available_mcp_servers().len() == 1);
    assert!(d.saw(&Change::Launch));
    let details = d.engine.agent_details("rachel").expect("rachel is in the roster");
    assert_eq!((details["name"].as_str(), details["backend"].as_str()), (Some("Rachel"), Some("claude")));
    assert!(d.engine.agent_details("nobody").is_none());

    d.engine.load_agent_profile("rachel");
    assert!(d.engine.profile_loading() && d.engine.profile_prompts_loading());
    d.until("profile", |e| {
        !e.profile_loading() && !e.profile_prompts_loading() && !e.profile_heartbeat().is_empty() && e.teams().len() == 1 && !e.updates_loading()
    });
    assert!(d.saw(&Change::Profile) && d.saw(&Change::Updates) && d.saw(&Change::Teams));
    assert_eq!(d.engine.profile_session(), "rachel");
    assert_eq!(d.engine.profile_task_plan()["title"], "Ship it");
    assert_eq!(d.engine.profile_heartbeat()["interval_minutes"], 30);
    assert_eq!((d.engine.profile_prompts().len(), d.engine.profile_prompts_have_more()), (20, true));
    assert_eq!(host.requests("GET", "/task-plan")[0]["query"]["session"], "rachel");
    d.engine.load_prompt_history("rachel", true);
    d.until("the next page", |e| e.profile_prompts().len() == 30 && !e.profile_prompts_loading());
    assert!(!d.engine.profile_prompts_have_more());
    assert_eq!(host.requests("GET", "/identity/prompt-history")[1]["query"]["before"], "p20");
    d.engine.load_prompt_history("rachel", true);
    assert!(!d.engine.profile_prompts_loading(), "no more pages to ask for");

    d.engine.set_agent_heartbeat("rachel", false);
    d.until_change("heartbeat accepted", Change::AgentMutated("rachel".into()));
    assert_eq!(host.requests("POST", "/agent-heartbeat")[0]["body"], json!({"session": "rachel", "heartbeat_enabled": false}));
    d.engine.set_schedule_enabled(" s1 ", true);
    d.engine.set_agent_mcp("rachel", &["github".to_owned()]);
    d.until("schedule and MCP posted", |_| {
        !host.requests("POST", "/agent-schedules/toggle").is_empty() && !host.requests("POST", "/agent-mcp").is_empty()
    });
    assert_eq!(host.requests("POST", "/agent-schedules/toggle")[0]["body"], json!({"schedule_id": "s1", "enabled": true}));
    assert_eq!(host.requests("POST", "/agent-mcp")[0]["body"], json!({"session": "rachel", "mcp_servers": ["github"]}));
    assert!(d.engine.error().is_empty(), "{:?}", d.engine.error());
}

#[test]
fn voices_load_preview_and_choose() {
    let host = Host::start("voices");
    let mut d = Driver::live(&host);
    d.engine.load_voices("rachel");
    assert!(d.engine.voices_loading());
    d.until("voices", |e| !e.voices_loading() && e.voices().len() == 2);
    assert!(d.saw(&Change::Voices));
    assert_eq!(d.engine.voice_bio(), "Warm and clear");
    assert_eq!((d.engine.voices()[0].label.as_str(), d.engine.voices()[0].taken_by.as_str()), ("Warm", "Mike"));
    assert_eq!(host.requests("GET", "/voices")[0]["query"]["for"], "rachel");

    d.engine.preview_voice("rachel", "Rachel", "v2");
    d.engine.choose_voice("rachel", "v2");
    d.until("chosen and reloaded", |e| !e.voices_loading() && host.requests("GET", "/voices").len() == 2);
    assert_eq!(host.requests("POST", "/preview")[0]["body"], json!({"voice_id": "v2", "session": "rachel", "text": "Hi, I'm Rachel."}));
    assert_eq!(host.requests("POST", "/agent-voice")[0]["body"], json!({"session": "rachel", "voice_id": "v2"}));
    assert!(d.saw(&Change::AgentMutated("rachel".into())));
    assert!(d.engine.error().is_empty(), "{:?}", d.engine.error());
}

#[test]
fn the_media_gallery_lists_and_caches_images() {
    let host = Host::start("media");
    let mut d = Driver::live(&host);
    let cache = host.dir.join("media");
    d.engine.set_media_directory(cache.clone());
    d.engine.load_media("rachel");
    d.until("image cached", |e| e.media_for_session("rachel").len() == 2 && e.media_source("m1").is_some());
    assert!(d.saw(&Change::Media));
    assert_eq!(host.requests("GET", "/media")[0]["query"], json!({"session": "rachel", "limit": "100"}));
    let source = d.engine.media_source("m1").unwrap().to_owned();
    let path = url::Url::parse(&source).unwrap().to_file_path().unwrap();
    assert!(path.starts_with(&cache) && std::fs::read(&path).unwrap().starts_with(b"\x89PNG"));
    assert!(d.engine.media_source("doc1").is_none(), "only images are fetched");
    assert!(d.engine.media_revision() >= 2);
    assert_eq!(d.engine.resolve_media_markdown("![x](clarp-media://asset/m1)"), format!("![x]({source})"));
}

#[test]
fn the_orchestrator_loads_and_saves_clamped_settings() {
    let host = Host::start("orchestrator");
    let mut d = Driver::live(&host);
    d.engine.load_orchestrator();
    assert!(d.engine.orchestrator_loading());
    d.until("loaded", |e| !e.orchestrator_loading());
    assert!(d.saw(&Change::Orchestrator));
    assert_eq!(d.engine.orchestrator_last_decision(), "route: rachel (0.87)");
    assert_eq!(d.engine.orchestrator_settings()["enabled"], false);

    d.engine.save_orchestrator(true, false, 0.2, "", " gpt-5 ", " high ", 10);
    assert!(d.engine.orchestrator_loading());
    d.until("saved", |e| !e.orchestrator_loading());
    let body = &host.requests("POST", "/orchestrator/settings")[0]["body"];
    assert_eq!(
        *body,
        json!({"enabled": true, "fallback_only": false, "confidence_threshold": 0.5, "provider": "openai",
               "model": "gpt-5", "effort": "high", "timeout_ms": 250})
    );
    assert_eq!(d.engine.orchestrator_settings()["enabled"], true);
    assert_eq!(d.engine.orchestrator_last_decision(), "No decisions logged yet.");
    assert!(d.saw(&Change::Orchestrator));
}

#[test]
fn portraits_are_fetched_once_rounded_and_cached() {
    let host = Host::start("avatars");
    let mut d = Driver::live(&host);
    let cache = host.dir.join("portraits");
    d.engine.set_portrait_directory(cache.clone());
    assert!(d.engine.avatar_source("rachel").is_none(), "nothing until it arrives");
    assert!(d.engine.avatar_source("rachel").is_none());
    d.until_change("portrait", Change::Avatars);
    let source = d.engine.avatar_source("rachel").expect("cached");
    let path = url::Url::parse(&source).unwrap().to_file_path().unwrap();
    assert!(path.starts_with(&cache), "{path:?}");
    // A PNG's IHDR holds its width and height.
    let png = std::fs::read(&path).unwrap();
    let side = |at: usize| u32::from_be_bytes(png[at..at + 4].try_into().unwrap());
    assert_eq!((side(16), side(20)), (192, 192), "rounded to the sidebar's size");
    assert_eq!(host.requests("GET", "/static/avatars/rachel.png").len(), 1, "asked for once");
    assert_eq!(d.engine.avatar_revision(), 1);
    assert!(d.engine.avatar_source("nobody").is_none());
}
