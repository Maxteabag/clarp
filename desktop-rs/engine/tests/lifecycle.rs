//! Starting and changing agents against the fake Host: the model catalog,
//! quick starts, creation (with and without the created row), rename,
//! release, the empty contact pool, assignment, paths, the turn queue and
//! the remembered launch defaults.

mod common;

use std::time::Duration;

use clarp_core::settings::Settings;
use clarp_engine::Change;
use common::{Driver, Host};
use serde_json::json;

#[test]
fn the_catalog_loads_once_live_and_lists_backends_models_and_efforts() {
    let host = Host::start("catalog");
    host.control("/__control/catalog", json!({"providers": {
        "codex": {"label": "Codex", "sort_index": 2, "supports_fork": true, "supported_efforts": ["medium"],
                  "models": [{"id": "gpt-5", "label": "GPT-5"}]},
        "claude": {"label": "Claude", "sort_index": 1, "supports_resume": true,
                   "models": [{"id": "opus", "label": "Opus", "supported_efforts": ["low", "high"], "default_effort": "high"}]},
        "secret": {"label": "Secret", "hidden": true},
        "missing": {"label": "Missing", "installed": false},
    }}));
    let mut d = Driver::new(&host.base);
    assert!(!d.engine.model_catalog_loaded());
    assert_eq!(d.engine.backend_options().len(), 3, "the built-in backends before a catalog");
    d.connect();
    d.until("catalog", |e| e.model_catalog_loaded());
    assert!(d.changes.contains(&Change::Launch));
    assert_eq!(host.requests("GET", "/agent-model-options").len(), 1);
    assert_eq!(d.engine.backend_options(), vec![json!({"id": "claude", "label": "Claude"}), json!({"id": "codex", "label": "Codex"})]);
    assert_eq!(d.engine.models_for_backend("claude"), vec![json!({"id": "", "label": "Provider default"}), json!({"id": "opus", "label": "Opus"})]);
    let efforts: Vec<_> = d.engine.efforts_for_model("claude", "opus").iter().map(|e| e["label"].as_str().unwrap().to_owned()).collect();
    assert_eq!(efforts, ["Provider default", "Low", "High"]);
    assert_eq!(d.engine.efforts_for_model("codex", "gpt-5")[1]["id"], "medium", "a model without efforts uses its provider's");
    assert_eq!(d.engine.default_effort_for_model("claude", "opus"), "high");
    assert!(d.engine.backend_supports_resume("claude") && !d.engine.backend_supports_resume("codex"));
    assert!(d.engine.backend_supports_fork("codex"));
}

#[test]
fn an_idle_contact_quick_starts_with_the_launch_values() {
    let host = Host::start("quick-start");
    let mut d = Driver::new(&host.base);
    assert!(!d.engine.quick_start_contact("Paula", "", "", ""));
    assert_eq!(d.engine.error(), "Connect to the Host before starting a contact");
    d.connect();
    d.until("the Host's default folder", |e| e.last_working_directory() == "/tmp");
    assert_eq!(d.engine.launch_directory(), "/tmp", "the Host default replaces the ~ placeholder");
    assert_eq!(d.engine.quick_start_backend(), "claude", "the selected agent's backend");
    let paula = d.engine.matching_contacts("pau");
    assert_eq!(paula.len(), 1);
    assert_eq!(paula[0]["name"], "Paula");
    assert!(d.engine.matching_contacts("rach").is_empty(), "an active persona is not an idle contact");
    assert!(!d.engine.quick_start_contact("Rachel", "", "", ""));
    assert_eq!(d.engine.error(), "This contact is no longer idle; choose its existing chat");

    assert!(d.engine.quick_start_contact("paula", "grok", "grok-4", "high"));
    assert!(d.engine.error().is_empty(), "a start clears the error");
    assert_eq!(d.engine.starting_contact(), "Paula");
    assert!(!d.engine.quick_start_contact("Paula", "", "", ""), "a second start waits for the first");
    d.until_change(&Change::AgentMutated("paula-new".into()));
    let posts = host.requests("POST", "/agents");
    assert_eq!(posts.len(), 1);
    let body = &posts[0]["body"];
    assert_eq!(
        (&body["name"], &body["cwd"], &body["backend"], &body["model"], &body["effort"], &body["synthesize_audio"]),
        (&json!("Paula"), &json!("/tmp"), &json!("grok"), &json!("grok-4"), &json!("high"), &json!(true))
    );
    assert!(body.get("session").is_none() && body.get("replace_sid").is_none() && body.get("resume_session_id").is_none());
    assert_eq!(d.engine.selected_session(), "paula-new", "the new agent opens");
    assert!(d.engine.roster().find("paula-new").is_some(), "the created row is in the roster at once");
    assert_eq!(d.engine.starting_contact(), "");
    assert_eq!((d.engine.last_backend().as_str(), d.engine.quick_start_backend().as_str()), ("grok", "grok"));
    assert_eq!(d.engine.settings().string("launch/backend", ""), "grok");
}

#[test]
fn create_agent_opens_the_created_row_or_waits_for_the_roster() {
    let host = Host::start("create");
    let mut d = Driver::new(&host.base);
    d.connect();
    d.engine.create_agent("Nova", "  ", "codex", "", "", "", "", "", vec![]);
    assert_eq!(d.engine.error(), "Name, workspace, and backend are required");
    d.engine.clear_error();

    // A modern Host answers with the agent's row.
    d.engine.create_agent(" Nova ", "/work", "codex", "gpt-5", "", "", "", "", vec![json!("github"), json!("")]);
    d.until_change(&Change::AgentMutated("nova-new".into()));
    let body = host.requests("POST", "/agents")[0]["body"].clone();
    assert_eq!((&body["name"], &body["session"], &body["cwd"], &body["backend"]), (&json!("Nova"), &json!("nova"), &json!("/work"), &json!("codex")));
    assert_eq!((&body["model"], &body["mcp_servers"]), (&json!("gpt-5"), &json!(["github"])));
    assert!(body.get("effort").is_none() && body.get("replace_sid").is_none());
    assert_eq!(d.engine.selected_session(), "nova-new");
    assert_eq!((d.engine.last_working_directory().as_str(), d.engine.last_backend().as_str()), ("/work", "codex"));

    // A session-only Host: the agent opens once the roster shows it.
    host.control("/__control/create", json!({"mode": "session-only"}));
    let snapshots = host.requests("GET", "/agents/snapshot").len();
    d.engine.create_agent("Orla", "/work", "claude", "", "low", "mike", "resume", "old-1", vec![]);
    d.until("the create reply and a snapshot after it", |_| host.requests("GET", "/agents/snapshot").len() > snapshots + 1);
    let body = host.requests("POST", "/agents")[1]["body"].clone();
    assert_eq!((&body["replace_sid"], &body["resume_session_id"], &body["effort"]), (&json!("mike"), &json!("old-1"), &json!("low")));
    assert!(!d.changes.contains(&Change::AgentMutated("orla-new".into())), "no success before the roster shows it");
    d.engine.create_agent("Orla", "/work", "claude", "", "", "", "", "", vec![]);
    assert!(d.engine.start_anonymous_agent("codex", "", ""), "a start while one is awaited retries instead");
    d.settle(Duration::from_millis(300));
    assert_eq!(host.requests("POST", "/agents").len(), 2, "still exactly one session-only creation");
    host.control("/__control/publish-pending", json!({}));
    d.engine.refresh_agents();
    d.until_change(&Change::AgentMutated("orla-new".into()));
    assert_eq!(d.engine.selected_session(), "orla-new");
    assert!(d.engine.error().is_empty());
}

#[test]
fn rename_release_and_settings_report_the_mutation() {
    let host = Host::start("mutate");
    let mut d = Driver::new(&host.base);
    d.connect();
    d.engine.rename_agent("rachel", "   ");
    d.engine.rename_agent("rachel", "  Rae  ");
    d.until_change(&Change::AgentMutated("rachel".into()));
    let renames = host.requests("POST", "/agent-rename");
    assert_eq!(renames.len(), 1, "a blank name is not sent");
    assert_eq!(renames[0]["body"], json!({"session": "rachel", "name": "Rae"}));

    d.engine.set_agent_heartbeat("rachel", true);
    d.engine.set_agent_dreaming("rachel", false);
    d.engine.set_agent_push_muted("rachel", true);
    d.until("settings posted", |_| !host.requests("POST", "/agent-mute").is_empty());
    assert_eq!(host.requests("POST", "/agent-heartbeat")[0]["body"], json!({"session": "rachel", "heartbeat_enabled": true}));
    assert_eq!(host.requests("POST", "/agent-dreaming")[0]["body"], json!({"session": "rachel", "dreaming_enabled": false}));
    assert_eq!(host.requests("POST", "/agent-mute")[0]["body"], json!({"session": "rachel", "muted": true}));

    d.engine.release_agent("mike");
    d.until_change(&Change::AgentMutated("mike".into()));
    assert_eq!(host.requests("DELETE", "/agents/mike").len(), 1, "release deletes the agent");
    d.until("mike leaves the roster", |e| e.roster().find("mike").is_none());
}

#[test]
fn an_empty_pool_and_a_refused_folder_end_the_launch() {
    let host = Host::start("pool");
    let mut d = Driver::new(&host.base);
    d.connect();
    host.control("/__control/create", json!({"respond": {"status": 409, "body": {"error": "contact_pool_empty"}}}));
    assert!(d.engine.start_available_contact("codex", "", ""));
    assert_eq!(d.engine.starting_contact(), "pool");
    assert!(!d.engine.start_anonymous_agent("codex", "", ""), "one launch at a time");
    d.until_change(&Change::LaunchPoolEmpty);
    assert_eq!(d.engine.starting_contact(), "", "an empty pool clears the launch");
    assert!(d.engine.error().is_empty(), "an empty pool is not an error");
    let pool = host.requests("POST", "/agents")[0]["body"].clone();
    assert_eq!((&pool["auto_contact"], &pool["backend"]), (&json!(true), &json!("codex")));
    assert!(pool.get("name").is_none());

    host.control("/__control/create", json!({"respond": {"status": 403, "body": {
        "error": "workspace_path_forbidden", "message": "path /home/clarp is outside the Clarp workspace root /data/workspace"}}}));
    d.engine.set_launch_directory(" /home/clarp ");
    assert_eq!(d.engine.launch_directory(), "/home/clarp");
    assert!(d.engine.start_anonymous_agent("codex", "", ""));
    d.until("the Host's message", |e| !e.error().is_empty());
    assert_eq!(d.engine.error(), "path /home/clarp is outside the Clarp workspace root /data/workspace", "no HTTP suffix");
    assert_eq!(d.engine.starting_contact(), "");
    assert_eq!(host.requests("POST", "/agents")[1]["body"]["cwd"], "/home/clarp");
    assert!(!d.changes.iter().any(|c| matches!(c, Change::AgentMutated(_))));
}

#[test]
fn contacts_are_assigned_and_paths_suggested() {
    let host = Host::start("assign");
    let mut d = Driver::new(&host.base);
    d.connect();
    d.engine.request_contact_assignment("rachel", true);
    d.until_change(&Change::AssignmentRequested { session: "rachel".into(), automatic: true });
    d.engine.load_assignment_contacts("rachel");
    d.until("assignment contacts", |e| e.assignment_contacts().len() == 1);
    assert_eq!(d.engine.assignment_contacts()[0]["name"], "Paula");
    d.engine.assign_contact("", "choose", "Paula");
    assert_eq!(d.engine.error(), "Connect and select an agent before assigning a contact");
    d.engine.assign_contact("rachel", "choose", "Paula");
    assert!(d.engine.error().is_empty());
    d.until_change(&Change::AssignmentSucceeded("rachel".into()));
    let body = host.requests("POST", "/agent-assign").pop().unwrap()["body"].clone();
    assert_eq!(body, json!({"session": "rachel", "mode": "choose", "name": "Paula"}));
    d.engine.assign_contact("rachel", "choose", "Nobody");
    d.until("a refused assignment", |e| !e.error().is_empty());
    assert_eq!(d.engine.error(), "contact is busy (HTTP 409)");
    d.engine.clear_error();

    d.engine.request_directory_suggestions(" /home/fake ");
    d.engine.load_favorite_paths();
    d.until("paths", |e| e.directory_suggestions().len() == 2 && e.favorite_paths().len() == 2);
    assert_eq!(d.engine.directory_suggestions(), ["/home/fake/one", "/home/fake/two"]);
    assert_eq!(d.engine.favorite_paths(), ["/home/fake/src", "/home/fake/notes"]);
    assert_eq!(host.requests("GET", "/favorite-paths")[0]["query"]["limit"], "5");
    d.engine.request_directory_suggestions("");
    assert!(d.engine.directory_suggestions().is_empty(), "an empty path clears the suggestions");

    d.engine.load_past_sessions("/work", "claude", true);
    assert!(d.engine.past_sessions_loading());
    d.until("past sessions", |e| !e.past_sessions_loading());
    assert_eq!(d.engine.past_sessions().len(), 2);
    d.engine.load_launch_directories("proj");
    d.until("launch directories", |e| !e.launch_directories_loading());
    assert_eq!(d.engine.launch_directories()[0]["path"], "/home/fake/proj");
    assert_eq!(d.engine.last_working_directory(), "/tmp", "a chosen folder is not replaced by the Host's home");
}

#[test]
fn the_turn_queue_loads_and_follows_its_actions() {
    let host = Host::start("queue");
    let mut d = Driver::new(&host.base);
    d.connect();
    assert!(d.engine.turn_queue("rachel").is_empty());
    d.engine.load_turn_queue("rachel");
    assert!(d.engine.turn_queue_loading());
    d.until("queue loaded", |e| !e.turn_queue_loading() && e.turn_queue("rachel").len() == 2);
    assert!(d.changes.contains(&Change::Queue("rachel".into())));
    assert!(d.engine.turn_queue("mike").is_empty(), "the queue is the loaded chat's");
    assert!(!d.engine.turn_queue_paused());

    d.engine.update_queued_turn("q1", "  sooner  ");
    d.until("edited", |e| e.turn_queue("rachel").first().is_some_and(|i| i["text"] == "sooner"));
    assert_eq!(host.requests("PUT", "/turn-queue/q1")[0]["body"], json!({"text": "sooner"}));
    d.engine.send_queued_turn("q1");
    d.until("sent", |e| e.turn_queue("rachel").len() == 1);
    assert_eq!(d.engine.turn_queue("rachel")[0]["queue_id"], "q2");
    d.engine.delete_queued_turn("q2");
    d.until("deleted", |e| e.turn_queue("rachel").is_empty() && !e.turn_queue_loading());
    assert_eq!(host.requests("DELETE", "/turn-queue/q2").len(), 1);
    assert!(d.engine.turn_queue_error().is_empty());

    let loads = host.requests("GET", "/turn-queue").len();
    host.control("/__control/event", json!({"type": "queue-updated", "session": "rachel", "count": 0}));
    d.until("reloaded on queue-updated", |_| host.requests("GET", "/turn-queue").len() > loads);
    host.control("/__control/event", json!({"type": "queue-updated", "session": "mike", "count": 0}));
    d.settle(Duration::from_millis(300));
    assert_eq!(host.requests("GET", "/turn-queue").len(), loads + 1, "another chat's queue is not loaded");
}

#[test]
fn launch_defaults_survive_a_restart() {
    let host = Host::start("defaults");
    let file = host.dir.join("settings.json");
    let mut d = Driver::with_settings(&host.base, Settings::at(file.clone()));
    assert_eq!((d.engine.last_working_directory().as_str(), d.engine.last_backend().as_str()), ("~", ""));
    assert_eq!(d.engine.launch_directory(), "~");
    d.connect();
    d.engine.create_agent("Nova", "/work", "codex", "", "", "", "", "", vec![]);
    d.until_change(&Change::AgentMutated("nova-new".into()));
    d.engine.set_launch_directory("/elsewhere");
    drop(d);

    let mut d = Driver::with_settings(&host.base, Settings::at(file));
    assert_eq!((d.engine.last_working_directory().as_str(), d.engine.last_backend().as_str()), ("/work", "codex"));
    assert_eq!(d.engine.quick_start_backend(), "codex");
    assert_eq!(d.engine.launch_directory(), "~", "the dialog's folder is not saved, only the last used one");
    d.connect();
    d.settle(Duration::from_millis(200));
    assert_eq!(d.engine.last_working_directory(), "/work", "the Host's default never replaces a chosen folder");
}

#[test]
fn a_failed_send_is_retried() {
    let host = Host::start("retry");
    let mut d = Driver::new(&host.base);
    d.connect();
    d.until("rachel open", |e| e.conversation("rachel").is_some_and(|c| c.rows().len() == 2));
    host.control("/__control/outage", json!({"seconds": 1}));
    d.engine.send("Try again");
    d.until("failed", |e| !e.sending() && e.conversation("rachel").unwrap().rows().iter().any(|r| r.delivery_failed));
    d.settle(Duration::from_millis(1200));
    d.engine.retry_latest_failed_message();
    d.until("retried and answered", |e| e.conversation("rachel").unwrap().rows().iter().any(|r| r.text == "Echo: Try again"));
    let rows = d.engine.conversation("rachel").unwrap().rows();
    assert!(!rows.iter().any(|r| r.delivery_failed), "the failed copy is gone");
    assert_eq!(rows.iter().filter(|r| r.text == "Try again").count(), 1);
}
