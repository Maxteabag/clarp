//! The launch dialogs' checks (`--check launch`, `--check agent-dialogs`),
//! driven through keys as a person would, against the fake Host.

use std::time::Duration;

use serde_json::{Value, json};
use slint::{ComponentHandle, Model};

use super::{Stage, app_now, check, control, report, run_stages, shot, view};
use crate::{AgentDialogs, LaunchHub, StartAgent, headless, launch_view};

/// The fake Host's request log: every `method` request to `path`.
fn requests(method: &str, path: &str) -> Vec<Value> {
    let Some(log) = std::env::var_os("CLARP_TEST_HOST_LOG") else { return Vec::new() };
    std::fs::read_to_string(log)
        .unwrap_or_default()
        .lines()
        .filter_map(|line| serde_json::from_str::<Value>(line).ok())
        .filter(|entry| entry["method"] == method && entry["path"] == path)
        .collect()
}

fn last_create() -> Value {
    requests("POST", "/agents").pop().map(|r| r["body"].clone()).unwrap_or_default()
}

fn names(window: &crate::AppWindow) -> Vec<String> {
    window.global::<LaunchHub>().get_rows().iter().map(|r| r.name.to_string()).collect()
}

fn selected(app: &crate::App) -> String {
    app.engine.borrow().selected_session().to_owned()
}

fn waited(elapsed: Duration, millis: u64) -> bool {
    elapsed >= Duration::from_millis(millis)
}

/// True once the composer has the keyboard (asking for it until then), so a
/// shortcut pressed next is not followed by the composer taking focus back.
fn typing_ready() -> bool {
    if report().composer_focused {
        return true;
    }
    app_now().focus_composer();
    false
}

/// `--check launch --out DIR`: Ctrl+N opens the hub; Ctrl+Right switches
/// provider, the model editor sets model and effort and Escape closes it;
/// typing searches the contacts and Enter quick-starts one; Ctrl+Alt+D
/// chooses a Host folder; a new named contact is created there; an empty
/// contact pool falls back to New contact; Escape steps back and closes;
/// offline the hub asks for a Host; Ctrl+Alt+N lists only idle contacts;
/// and the Start dialog creates a named agent.
pub(super) fn launch_check(out: String) {
    use slint::platform::Key;
    let catalog = json!({"providers": {
        "claude": {"label": "Claude", "sort_index": 1, "models": [{"id": "opus", "label": "Opus", "supported_efforts": ["low", "high"]}]},
        "codex": {"label": "Codex", "sort_index": 2, "supported_efforts": ["medium"], "models": [{"id": "gpt-5", "label": "GPT-5"}]},
    }});
    let personas = json!({"personas": [{"id": "p", "name": "Paula"}, {"id": "q", "name": "Quinn", "description": "Research"}, {"id": "r", "name": "Rosa"}]});
    let (o1, o2, o3, o4, o5, o6, o7, o8) = (out.clone(), out.clone(), out.clone(), out.clone(), out.clone(), out.clone(), out.clone(), out.clone());
    let stages: Vec<Stage> = vec![
        ("ready", Box::new(move |app, _window, _| {
            let open = app.engine.borrow().conversation("rachel").is_some_and(|c| !c.rows().is_empty());
            if !open || !report().composer_focused {
                return false;
            }
            check(control("/__control/catalog", &catalog).is_ok(), "the Host offers Claude and Codex models");
            check(control("/__control/personas", &personas).is_ok(), "the Host has three idle contacts");
            // The catalog is read once live: connect again to read the new one.
            app.engine.borrow_mut().reconnect();
            crate::pump();
            true
        })),
        ("catalog", Box::new(|app, _window, _| {
            let ready = {
                let engine = app.engine.borrow();
                engine.connected() && engine.backend_options().len() == 2 && engine.matching_contacts("").len() == 3
            };
            if !ready {
                return false;
            }
            if !typing_ready() { return false; }
            headless::press_with(&[Key::Control], "n");
            true
        })),
        ("hub open", Box::new(move |_, window, elapsed| {
            if window.get_overlay() != "new-session" || !waited(elapsed, 300) {
                return false;
            }
            let hub = window.global::<LaunchHub>();
            check(window.get_keyboard_mode() == "LAUNCH", &format!("Ctrl+N opens the hub in its own keyboard state: {}", window.get_keyboard_mode()));
            check(hub.get_providers().row_count() == 2 && hub.get_provider_index() == 0, "the provider marks show the catalog, Claude first");
            check(names(window) == ["Paula", "Quinn", "Rosa"], &format!("every idle contact is a card: {:?}", names(window)));
            check(hub.get_current() == 0 && hub.get_confirm_label() == "Create session · Enter", "the first card is selected, ready to start");
            check(hub.get_directory() == "/tmp", &format!("the directory is the Host's default: {}", hub.get_directory()));
            shot(&o1, "launch-01-hub");
            headless::press_with(&[Key::Control], Key::RightArrow);
            true
        })),
        ("provider", Box::new(|_, window, _| {
            let hub = window.global::<LaunchHub>();
            if hub.get_provider_index() != 1 {
                return false;
            }
            check(hub.get_model_summary() == "Server default", "Ctrl+Right switches to Codex with the server's model");
            hub.invoke_toggle_edit_model();
            true
        })),
        ("model editor", Box::new(|_, window, _| {
            let hub = window.global::<LaunchHub>();
            if !hub.get_editing_model() {
                return false;
            }
            let labels: Vec<String> = hub.get_model_labels().iter().map(|l| l.to_string()).collect();
            check(labels == ["Server default", "GPT-5"], &format!("Edit shows Codex's models: {labels:?}"));
            hub.invoke_model_chosen(1);
            hub.invoke_effort_chosen(1);
            true
        })),
        ("model chosen", Box::new(|_, window, elapsed| {
            let hub = window.global::<LaunchHub>();
            if hub.get_model_summary() != "GPT-5 · Medium" || !waited(elapsed, 200) {
                return false;
            }
            check(true, "choosing a model and effort updates the summary");
            headless::press(Key::Escape);
            true
        })),
        ("editor closed", Box::new(|_, window, _| {
            if window.global::<LaunchHub>().get_editing_model() {
                return false;
            }
            check(window.get_overlay() == "new-session", "Escape closes the model editor first, not the hub");
            headless::type_text("qu");
            true
        })),
        ("searched", Box::new(move |_, window, elapsed| {
            if window.global::<LaunchHub>().get_query() != "qu" || !waited(elapsed, 200) {
                return false;
            }
            check(names(window) == ["Quinn"], &format!("typing searches the contacts: {:?}", names(window)));
            shot(&o2, "launch-02-search");
            headless::press(Key::Return);
            true
        })),
        ("quick started", Box::new(|app, window, _| {
            if !window.get_overlay().is_empty() || selected(app) != "quinn-new" || !typing_ready() {
                return false;
            }
            let body = last_create();
            check(
                body["name"] == "Quinn" && body["backend"] == "codex" && body["model"] == "gpt-5" && body["effort"] == "medium" && body["cwd"] == "/tmp",
                &format!("Enter quick-starts the contact with the chosen provider, model and folder: {body}"),
            );
            check(window.get_surface() == "chats", "the new chat opens");
            headless::press_with(&[Key::Control], "n");
            true
        })),
        ("reopened", Box::new(|_, window, elapsed| {
            if window.get_overlay() != "new-session" || !waited(elapsed, 200) {
                return false;
            }
            check(names(window) == ["Paula", "Rosa"], &format!("a started contact is no longer idle: {:?}", names(window)));
            check(window.global::<LaunchHub>().get_provider_index() == 1, "the hub remembers the last provider");
            headless::press_with(&[Key::Control, Key::Alt], "d");
            true
        })),
        ("directory mode", Box::new(|app, window, elapsed| {
            let hub = window.global::<LaunchHub>();
            if !hub.get_choosing_directory() || app.engine.borrow().launch_directories_loading() || !waited(elapsed, 300) {
                return false;
            }
            check(true, "Ctrl+Alt+D in the hub opens the directory picker");
            headless::type_text("proj");
            true
        })),
        ("directory typed", Box::new(move |app, window, elapsed| {
            let hub = window.global::<LaunchHub>();
            let rows: Vec<String> = hub.get_directory_rows().iter().map(|r| r.path.to_string()).collect();
            if rows != ["/home/fake/proj"] || app.engine.borrow().launch_directories_loading() || !waited(elapsed, 300) {
                return false;
            }
            check(hub.get_directory_current() == 0, "the Host's launch directories match the typed path");
            shot(&o3, "launch-03-directory");
            headless::press(Key::Return);
            true
        })),
        ("directory chosen", Box::new(|app, window, _| {
            let hub = window.global::<LaunchHub>();
            if hub.get_choosing_directory() {
                return false;
            }
            check(hub.get_directory() == "/home/fake/proj" && app.engine.borrow().launch_directory() == "/home/fake/proj", "Enter chooses the folder");
            // "New contact" is a mouse button.
            hub.invoke_toggle_new_contact();
            true
        })),
        ("new contact", Box::new(|_, window, elapsed| {
            if !window.global::<LaunchHub>().get_new_contact() || !waited(elapsed, 200) {
                return false;
            }
            headless::type_text("Nova");
            true
        })),
        ("named", Box::new(move |_, window, _| {
            let hub = window.global::<LaunchHub>();
            if hub.get_name() != "Nova" {
                return false;
            }
            check(hub.get_can_confirm(), "a typed name can be created");
            shot(&o4, "launch-04-new-contact");
            headless::press(Key::Return);
            true
        })),
        ("created", Box::new(|app, window, _| {
            if !window.get_overlay().is_empty() || selected(app) != "nova-new" {
                return false;
            }
            let body = last_create();
            check(
                body["name"] == "Nova" && body["session"] == "nova" && body["cwd"] == "/home/fake/proj" && body["backend"] == "codex",
                &format!("a new contact is created in the chosen folder: {body}"),
            );
            check(control("/__control/create", &json!({"respond": {"status": 409, "body": {"error": "contact_pool_empty"}}})).is_ok(), "the Host's contact pool is empty");
            // A command-line launch (`--backend codex`) starts whichever contact is free.
            launch_view::open_launch(&app_now(), window, "codex", "", "", false, "");
            true
        })),
        ("pool empty", Box::new(move |app, window, elapsed| {
            let hub = window.global::<LaunchHub>();
            if window.get_overlay() != "new-session" || !hub.get_new_contact() || !waited(elapsed, 300) {
                return false;
            }
            check(last_create()["auto_contact"] == true, "the launch asked the pool for a contact");
            check(app.engine.borrow().error().is_empty() && app.engine.borrow().starting_contact().is_empty(), "an empty pool is not an error");
            shot(&o5, "launch-05-pool-empty");
            headless::type_text("Olive");
            true
        })),
        ("olive", Box::new(|_, window, _| {
            if window.global::<LaunchHub>().get_name() != "Olive" {
                return false;
            }
            check(true, "an empty pool falls back to New contact with the name field focused");
            headless::press(Key::Return);
            true
        })),
        ("olive created", Box::new(|app, window, _| {
            if !window.get_overlay().is_empty() || selected(app) != "olive-new" || !typing_ready() {
                return false;
            }
            check(true, "the named contact is created instead");
            headless::press_with(&[Key::Control], "n");
            true
        })),
        ("step back", Box::new(|_, window, elapsed| {
            if window.get_overlay() != "new-session" || !waited(elapsed, 200) {
                return false;
            }
            window.global::<LaunchHub>().invoke_toggle_new_contact();
            true
        })),
        ("new contact again", Box::new(|_, window, elapsed| {
            if !window.global::<LaunchHub>().get_new_contact() || !waited(elapsed, 100) {
                return false;
            }
            headless::press(Key::Escape);
            true
        })),
        ("stepped back", Box::new(|_, window, _| {
            if window.global::<LaunchHub>().get_new_contact() {
                return false;
            }
            check(window.get_overlay() == "new-session", "Escape leaves New contact before it closes the hub");
            headless::press(Key::Escape);
            true
        })),
        ("escape closed", Box::new(|_, window, _| {
            if !window.get_overlay().is_empty() {
                return false;
            }
            check(true, "Escape then closes the hub");
            // The sidebar's "+" runs "new".
            window.invoke_run_command("new".into());
            true
        })),
        ("sidebar plus", Box::new(|_, window, _| {
            if window.get_overlay() != "new-session" {
                return false;
            }
            check(true, "the sidebar's + opens the hub");
            // A click on the backdrop.
            window.global::<LaunchHub>().invoke_cancel();
            true
        })),
        ("clicked outside", Box::new(|_, window, _| {
            if !window.get_overlay().is_empty() {
                return false;
            }
            check(true, "a click outside closes the hub");
            check(control("/__control/outage", &json!({"seconds": 3})).is_ok(), "the Host goes away for a moment");
            true
        })),
        ("offline", Box::new(|app, _window, _| {
            if app.engine.borrow().connected() || !typing_ready() {
                return false;
            }
            headless::press_with(&[Key::Control], "n");
            true
        })),
        ("offline hub", Box::new(move |_, window, elapsed| {
            let hub = window.global::<LaunchHub>();
            if window.get_overlay() != "new-session" || hub.get_connected() || !waited(elapsed, 300) {
                return false;
            }
            check(!hub.get_can_confirm(), "offline, the hub asks for a Host and starts nothing");
            shot(&o6, "launch-06-offline");
            headless::press(Key::Escape);
            true
        })),
        ("back online", Box::new(|app, window, _| {
            if !window.get_overlay().is_empty() || !app.engine.borrow().connected() {
                return false;
            }
            if !typing_ready() { return false; }
            headless::press_with(&[Key::Control, Key::Alt], "n");
            true
        })),
        ("contacts only", Box::new(move |_, window, elapsed| {
            if !window.get_switcher_open() || !waited(elapsed, 300) {
                return false;
            }
            let rows: Vec<String> = window.get_switcher_rows().iter().map(|r| format!("{}:{}", r.kind, r.label)).collect();
            check(rows == ["contact:Start Paula", "contact:Start Rosa"], &format!("Ctrl+Alt+N lists only the idle contacts: {rows:?}"));
            check(window.get_switcher_placeholder() == "Start an idle contact", "and says so");
            shot(&o7, "launch-07-contacts");
            headless::type_text("ro");
            true
        })),
        ("contact typed", Box::new(|_, window, elapsed| {
            if window.get_switcher_query() != "ro" || !waited(elapsed, 200) {
                return false;
            }
            let first = window.get_switcher_rows().row_data(0).map(|r| r.label.to_string()).unwrap_or_default();
            check(first == "Start Rosa", &format!("typing narrows the contacts: {first}"));
            headless::press(Key::Return);
            true
        })),
        ("contact started", Box::new(|app, window, _| {
            if window.get_switcher_open() || selected(app) != "rosa-new" {
                return false;
            }
            check(last_create()["name"] == "Rosa", "Enter on a contact row starts that contact");
            // AgentOverview's "Start" asks for the named start dialog.
            launch_view::open_start_agent(&app_now(), window, "", "Zed");
            true
        })),
        ("start dialog", Box::new(move |_, window, elapsed| {
            let start = window.global::<StartAgent>();
            if window.get_overlay() != "start-agent" || start.get_favorites().row_count() != 2 || !waited(elapsed, 300) {
                return false;
            }
            let favorites: Vec<String> = start.get_favorites().iter().map(|f| f.to_string()).collect();
            check(favorites == ["src", "notes"], &format!("the Start dialog offers the favourite folders: {favorites:?}"));
            check(start.get_suggestions().row_count() == 2, "and the typed path's subfolders");
            check(start.get_name() == "Zed" && start.get_can_start(), "with the name filled in");
            shot(&o8, "launch-08-start");
            headless::press(Key::Return);
            true
        })),
        ("started", Box::new(|app, window, _| {
            if !window.get_overlay().is_empty() || selected(app) != "zed-new" {
                return false;
            }
            let body = last_create();
            check(body["name"] == "Zed" && body["cwd"] == "~", &format!("Enter starts the named agent: {body}"));
            true
        })),
    ];
    run_stages(stages);
}

/// `--check agent-dialogs --out DIR`: F2 renames the chat's contact,
/// Ctrl+A assigns one (choosing, a refused name, automatic with
/// Ctrl+Shift+A), the queue dialog edits, sends and deletes queued turns,
/// Ctrl+Alt+R retries a failed message, Ctrl+Alt+T opens the agent's CLI
/// (recorded by `CLARP_TEST_TERMINAL_LOG`) and Ctrl+Shift+R releases an agent.
pub(super) fn agent_dialogs_check(out: String) {
    use slint::platform::Key;
    let (o1, o2, o3, o4) = (out.clone(), out.clone(), out.clone(), out.clone());
    let terminal_log = std::env::var_os("CLARP_TEST_TERMINAL_LOG").map(std::path::PathBuf::from).unwrap_or_default();
    let project = terminal_log.parent().map(|p| p.join("project")).unwrap_or_default();
    let project2 = project.clone();
    let log2 = terminal_log.clone();
    let stages: Vec<Stage> = vec![
        ("ready", Box::new(|app, _window, _| {
            let open = app.engine.borrow().conversation("rachel").is_some_and(|c| !c.rows().is_empty());
            if !open || !report().composer_focused {
                return false;
            }
            headless::press(Key::F2);
            true
        })),
        ("rename open", Box::new(move |_, window, elapsed| {
            if window.get_overlay() != "rename-agent" || !waited(elapsed, 300) {
                return false;
            }
            let dialogs = window.global::<AgentDialogs>();
            check(dialogs.get_rename_name() == "Rachel" && dialogs.get_rename_session() == "rachel", "F2 opens the rename with the contact's name");
            shot(&o1, "agent-dialogs-01-rename");
            // The name is selected: typing replaces it.
            headless::type_text("Rae");
            true
        })),
        ("renamed", Box::new(|_, window, _| {
            if window.global::<AgentDialogs>().get_rename_name() != "Rae" {
                return false;
            }
            headless::press(Key::Return);
            true
        })),
        ("rename sent", Box::new(|_, window, _| {
            let renames = requests("POST", "/agent-rename");
            if renames.is_empty() || !window.get_overlay().is_empty() || !typing_ready() {
                return false;
            }
            check(renames[0]["body"] == json!({"session": "rachel", "name": "Rae"}), &format!("Enter renames and the dialog closes: {}", renames[0]["body"]));
            headless::press(Key::F2);
            true
        })),
        ("rename escape", Box::new(|_, window, elapsed| {
            if window.get_overlay() != "rename-agent" || !waited(elapsed, 200) {
                return false;
            }
            headless::press(Key::Escape);
            true
        })),
        ("rename closed", Box::new(|_, window, _| {
            if !window.get_overlay().is_empty() || !typing_ready() {
                return false;
            }
            check(requests("POST", "/agent-rename").len() == 1, "Escape closes the rename without renaming");
            headless::press(Key::F2);
            true
        })),
        ("rename outside", Box::new(|_, window, elapsed| {
            if window.get_overlay() != "rename-agent" || !waited(elapsed, 200) {
                return false;
            }
            window.global::<AgentDialogs>().invoke_cancel();
            check(window.get_overlay().is_empty(), "a click outside closes the rename");
            true
        })),
        ("composer back", Box::new(|_, _window, _| {
            if !typing_ready() {
                return false;
            }
            headless::press_with(&[Key::Control], "a");
            true
        })),
        ("assign open", Box::new(|_, window, elapsed| {
            let dialogs = window.global::<AgentDialogs>();
            if window.get_overlay() != "assign-agent" || dialogs.get_assign_contacts().row_count() != 1 || !waited(elapsed, 300) {
                return false;
            }
            check(dialogs.get_assign_mode() == "auto", "Ctrl+A opens the assignment on Automatic");
            headless::press(Key::DownArrow);
            true
        })),
        ("assign choose", Box::new(move |_, window, elapsed| {
            let dialogs = window.global::<AgentDialogs>();
            if dialogs.get_assign_mode() != "choose" || !waited(elapsed, 200) {
                return false;
            }
            check(dialogs.get_assign_contact() == 0, "Down picks Choose contact, with the Host's idle contact");
            shot(&o2, "agent-dialogs-02-assign");
            headless::press(Key::Return);
            true
        })),
        ("assigned", Box::new(|_, window, _| {
            let assigned = requests("POST", "/agent-assign").into_iter().any(|r| r["body"]["mode"] == "choose");
            if !assigned || !window.get_overlay().is_empty() || !typing_ready() {
                return false;
            }
            check(true, "Enter assigns the chosen contact and the dialog closes");
            headless::press_with(&[Key::Control], "a");
            true
        })),
        ("assign again", Box::new(|_, window, elapsed| {
            if window.get_overlay() != "assign-agent" || !waited(elapsed, 200) {
                return false;
            }
            headless::press(Key::UpArrow);
            true
        })),
        ("assign create", Box::new(|_, window, elapsed| {
            if window.global::<AgentDialogs>().get_assign_mode() != "create" || !waited(elapsed, 200) {
                return false;
            }
            check(true, "Up wraps round to New contact");
            headless::type_text("Nobody");
            true
        })),
        ("assign typed", Box::new(|_, window, _| {
            if window.global::<AgentDialogs>().get_assign_name() != "Nobody" {
                return false;
            }
            headless::press(Key::Return);
            true
        })),
        ("assign refused", Box::new(move |_, window, elapsed| {
            let dialogs = window.global::<AgentDialogs>();
            if dialogs.get_error().is_empty() || !waited(elapsed, 200) {
                return false;
            }
            check(window.get_overlay() == "assign-agent" && !dialogs.get_submitting(), &format!("a refused name stays open with the Host's reason: {}", dialogs.get_error()));
            shot(&o3, "agent-dialogs-03-assign-refused");
            headless::press(Key::Escape);
            true
        })),
        ("assign closed", Box::new(|app, window, _| {
            if !window.get_overlay().is_empty() || !typing_ready() {
                return false;
            }
            app.engine.borrow_mut().clear_error();
            headless::press_with(&[Key::Control, Key::Shift], "A");
            true
        })),
        ("auto assigned", Box::new(|_, window, _| {
            let auto = requests("POST", "/agent-assign").into_iter().any(|r| r["body"]["mode"] == "auto");
            if !auto || !window.get_overlay().is_empty() {
                return false;
            }
            check(true, "Ctrl+Shift+A assigns automatically at once");
            let queued = json!({"session": "rachel", "set": {"queued_turn_count": 2}});
            check(control("/__control/agent", &queued).is_ok(), "two turns wait in rachel's queue");
            true
        })),
        ("queue line", Box::new(|_, window, _| {
            if view().queued != 2 {
                return false;
            }
            // A click on the composer's queue line.
            window.global::<AgentDialogs>().invoke_queue_open(view().session);
            true
        })),
        ("queue open", Box::new(move |_, window, elapsed| {
            let dialogs = window.global::<AgentDialogs>();
            if window.get_overlay() != "queue" || dialogs.get_queue_items().row_count() != 2 || !waited(elapsed, 300) {
                return false;
            }
            check(dialogs.get_queue_title() == "Rachel", &format!("the queue dialog names the chat: {}", dialogs.get_queue_title()));
            check(!report().composer_focused, "and takes the keyboard from the composer behind it");
            shot(&o4, "agent-dialogs-04-queue");
            dialogs.invoke_queue_save("q1".into(), "sooner".into());
            true
        })),
        ("queue edited", Box::new(|_, window, _| {
            let first = window.global::<AgentDialogs>().get_queue_items().row_data(0).map(|i| i.text.to_string()).unwrap_or_default();
            if first != "sooner" {
                return false;
            }
            check(requests("PUT", "/turn-queue/q1").first().is_some_and(|r| r["body"] == json!({"text": "sooner"})), "Save edits the queued turn");
            window.global::<AgentDialogs>().invoke_queue_send("q1".into());
            true
        })),
        ("queue sent", Box::new(|_, window, _| {
            if window.global::<AgentDialogs>().get_queue_items().row_count() != 1 {
                return false;
            }
            check(!requests("POST", "/turn-queue/q1/send").is_empty(), "Send now sends it ahead of the queue");
            window.global::<AgentDialogs>().invoke_queue_delete("q2".into());
            true
        })),
        ("queue deleted", Box::new(|_, window, _| {
            if window.global::<AgentDialogs>().get_queue_items().row_count() != 0 {
                return false;
            }
            check(requests("DELETE", "/turn-queue/q2").len() == 1, "Delete removes it");
            headless::press(Key::Escape);
            true
        })),
        ("queue closed", Box::new(|_, window, _| {
            if !window.get_overlay().is_empty() {
                return false;
            }
            check(true, "Escape closes the queue");
            window.global::<AgentDialogs>().invoke_queue_open("rachel".into());
            window.global::<AgentDialogs>().invoke_cancel();
            check(window.get_overlay().is_empty(), "so does a click outside");
            true
        })),
        ("compose", Box::new(|_, _window, _| {
            if !typing_ready() {
                return false;
            }
            check(control("/__control/outage", &json!({"seconds": 1})).is_ok(), "the Host goes away for a second");
            headless::type_text("Try again");
            true
        })),
        ("typed", Box::new(|_, _window, _| {
            if app_now().active_draft() != "Try again" {
                return false;
            }
            headless::press(Key::Return);
            true
        })),
        ("failed", Box::new(|app, _window, _| {
            let failed = app.engine.borrow().conversation("rachel").is_some_and(|c| c.rows().iter().any(|r| r.delivery_failed));
            if !failed || app.engine.borrow().sending() {
                return false;
            }
            check(true, "the message fails to deliver");
            true
        })),
        ("back", Box::new(|app, _window, elapsed| {
            if !app.engine.borrow().connected() || !waited(elapsed, 1200) {
                return false;
            }
            if !typing_ready() { return false; }
            headless::press_with(&[Key::Control, Key::Alt], "r");
            true
        })),
        ("retried", Box::new(move |app, _window, _| {
            let rows: Vec<(String, bool)> =
                app.engine.borrow().conversation("rachel").map(|c| c.rows().iter().map(|r| (r.text.clone(), r.delivery_failed)).collect()).unwrap_or_default();
            if !rows.iter().any(|(text, _)| text == "Echo: Try again") {
                return false;
            }
            check(!rows.iter().any(|(_, failed)| *failed), "Ctrl+Alt+R sends the failed message again");
            check(std::fs::create_dir_all(&project).is_ok(), "the agent's folder exists on this desktop");
            let native = json!({"session": "rachel", "set": {"cwd": project, "conversation_id": "native-1"}});
            check(control("/__control/agent", &native).is_ok(), "rachel works there");
            true
        })),
        ("folder", Box::new(move |app, _window, _| {
            let folder = app.engine.borrow().roster().find("rachel").map(|a| a.working_directory.clone()).unwrap_or_default();
            if folder != project2.to_string_lossy() {
                return false;
            }
            if !typing_ready() { return false; }
            headless::press_with(&[Key::Control], "k");
            true
        })),
        ("switcher", Box::new(|_, window, elapsed| {
            if !window.get_switcher_open() || !waited(elapsed, 200) {
                return false;
            }
            headless::type_text("shared filesystem");
            true
        })),
        ("setting", Box::new(|_, window, elapsed| {
            if window.get_switcher_query() != "shared filesystem" || !waited(elapsed, 200) {
                return false;
            }
            headless::press(Key::Return);
            true
        })),
        ("shared", Box::new(|app, window, _| {
            if window.get_switcher_open() || !app.engine.borrow().shared_filesystem() {
                return false;
            }
            if !typing_ready() { return false; }
            headless::press_with(&[Key::Control, Key::Alt], "t");
            true
        })),
        ("terminal", Box::new(move |app, _window, _| {
            let Some(line) = std::fs::read_to_string(&log2).ok().and_then(|t| t.lines().last().map(str::to_owned)) else { return false };
            let recorded: Value = serde_json::from_str(&line).unwrap_or_default();
            let directory = std::fs::canonicalize(&terminal_log).ok().and_then(|p| p.parent().map(|d| d.join("project"))).unwrap_or_default();
            check(
                recorded["program"] == "xdg-terminal-exec" && recorded["directory"] == directory.to_string_lossy().as_ref(),
                &format!("Ctrl+Alt+T opens the agent's CLI in its folder: {recorded}"),
            );
            check(recorded["arguments"].as_array().is_some_and(|a| a.iter().any(|v| v == "native-1")), "resuming its conversation");
            check(app.engine.borrow().error().is_empty(), &format!("without an error: {:?}", app.engine.borrow().error()));
            app.engine.borrow_mut().select("mike");
            crate::pump();
            true
        })),
        ("mike", Box::new(|app, _window, elapsed| {
            if selected(app) != "mike" || !waited(elapsed, 300) {
                return false;
            }
            if !typing_ready() { return false; }
            headless::press_with(&[Key::Control, Key::Shift], "R");
            true
        })),
        ("released", Box::new(|app, _window, _| {
            if requests("DELETE", "/agents/mike").is_empty() || app.engine.borrow().roster().find("mike").is_some() {
                return false;
            }
            check(true, "Ctrl+Shift+R releases the agent and it leaves the roster");
            true
        })),
    ];
    run_stages(stages);
}
