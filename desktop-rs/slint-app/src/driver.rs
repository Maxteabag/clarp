//! The end-to-end driver (`--e2e-out DIR`): uses the window like a person
//! would, against a real Host, and saves a screenshot per stage. Each stage
//! waits (up to a limit) for what it expects, then acts.

use std::cell::{Cell, RefCell};
use std::rc::Rc;
use std::time::{Duration, Instant};

use slint::{ComponentHandle, Model};

use crate::headless;

// ---- launch dialogs
#[path = "launch_checks.rs"]
mod launch_checks;
// ---- profile and overview
#[path = "profile_checks.rs"]
mod profile_checks;

pub const PROMPT: &str = "Hello from the Slint desktop end-to-end run, please answer";

thread_local! {
    static FAILURES: Cell<i32> = const { Cell::new(0) };
    static STEP: Cell<usize> = const { Cell::new(0) };
    static STREAMED: Cell<bool> = const { Cell::new(false) };
    static SINCE: RefCell<Option<Instant>> = const { RefCell::new(None) };
    static TIMER: RefCell<Option<slint::Timer>> = const { RefCell::new(None) };
}

pub fn exit_code() -> i32 {
    FAILURES.with(|f| if f.get() == 0 { 0 } else { 1 })
}

fn check(ok: bool, what: &str) {
    println!("{} {what}", if ok { "ok  " } else { "FAIL" });
    if !ok {
        FAILURES.with(|f| f.set(f.get() + 1));
    }
}

fn finish() {
    println!("{}", if exit_code() == 0 { "E2E_PASS" } else { "E2E_FAIL" });
    if let Err(error) = slint::quit_event_loop() {
        eprintln!("clarp-slint: {error}");
    }
}

fn shot(out: &str, name: &str) {
    let path = format!("{out}/{name}.png");
    let saved = headless::save_frame(&path);
    check(saved.is_ok(), &format!("captured {name} {}", saved.err().unwrap_or_default()));
}

fn texts() -> Vec<String> {
    let Some(app) = crate::app() else { return Vec::new() };
    let engine = app.engine.borrow();
    engine
        .conversation(engine.selected_session())
        .map(|c| c.rows().iter().map(|m| if m.display_text.is_empty() { m.text.clone() } else { m.display_text.clone() }).collect())
        .unwrap_or_default()
}

pub fn start(out: String) {
    let timer = slint::Timer::default();
    timer.start(slint::TimerMode::Repeated, Duration::from_millis(100), move || step(&out));
    TIMER.with(|t| *t.borrow_mut() = Some(timer));
}

fn advance() {
    STEP.with(|s| s.set(s.get() + 1));
    SINCE.with(|s| *s.borrow_mut() = Some(Instant::now()));
}

fn step(out: &str) {
    let Some(app) = crate::app() else { return };
    let since = SINCE.with(|s| *s.borrow_mut().get_or_insert_with(Instant::now));
    let current = STEP.with(Cell::get);
    if since.elapsed() > Duration::from_secs(30) {
        let engine = app.engine.borrow();
        check(false, &format!("timed out at step {current} (state {}, error {:?})", engine.connection_state(), engine.error()));
        drop(engine);
        finish();
        return;
    }
    // Watch every tick for the reply mid-stream.
    if current >= 3 && !STREAMED.with(Cell::get) {
        let texts = texts();
        if let Some(partial) = texts.iter().find(|t| t.contains("QA reply") && !t.contains(PROMPT)) {
            STREAMED.with(|s| s.set(true));
            // Draw the new rows before capturing them.
            slint::Timer::single_shot(Duration::from_millis(40), {
                let out = out.to_owned();
                let partial = partial.clone();
                move || {
                    println!("streaming frame shows: {partial}");
                    shot(&out, "04-streaming");
                }
            });
        }
    }
    let engine = app.engine.borrow();
    match current {
        0 => {
            if engine.connection_state() != "live" || engine.roster().agents().iter().filter(|a| !a.janitor).count() < 2 {
                return;
            }
            check(true, "live connection to the real Host");
            check(
                engine.roster().find("rachel").is_some() && engine.roster().find("mike").is_some(),
                "the roster lists the Host's agents",
            );
            drop(engine);
            app.engine.borrow_mut().select("rachel");
            crate::pump();
            advance();
        }
        1 => {
            let loaded = engine.selected_session() == "rachel" && engine.conversation("rachel").is_some_and(|c| !c.loading());
            if !loaded || since.elapsed() < Duration::from_millis(800) {
                return;
            }
            drop(engine);
            shot(out, "01-roster");
            headless::type_text(PROMPT);
            advance();
        }
        2 => {
            let draft = app.active_draft();
            if draft != PROMPT {
                return;
            }
            drop(engine);
            shot(out, "02-composed");
            headless::press(slint::platform::Key::Return);
            advance();
        }
        3 => {
            drop(engine);
            if !texts().iter().any(|t| t.contains(PROMPT)) {
                return;
            }
            let draft = app.active_draft();
            check(draft.is_empty(), "sending clears the composer");
            shot(out, "03-sent");
            advance();
        }
        4 => {
            drop(engine);
            if !texts().iter().any(|t| t.contains(&format!("QA reply: {PROMPT}"))) {
                return;
            }
            check(STREAMED.with(Cell::get), "the reply was seen streaming before it completed");
            advance();
        }
        5 => {
            if engine.sending() || since.elapsed() < Duration::from_millis(1200) {
                return;
            }
            // The QA Host refuses routes outside its turn lane (the model
            // catalog among them); that failure is the lane's, not the chat's.
            let refused_by_lane = engine.error_source() == "model-catalog";
            check(engine.error().is_empty() || refused_by_lane, &format!("no error: {:?} (from {:?})", engine.error(), engine.error_source()));
            drop(engine);
            shot(out, "05-replied");
            app.engine.borrow_mut().select("mike");
            crate::pump();
            advance();
        }
        _ => {
            let open = engine.selected_session() == "mike" && engine.conversation("mike").is_some_and(|c| !c.loading());
            if !open || since.elapsed() < Duration::from_millis(800) {
                return;
            }
            drop(engine);
            check(true, "the second chat opens");
            shot(out, "06-second-chat");
            TIMER.with(|t| t.borrow_mut().take());
            finish();
        }
    }
    let _ = app.window.upgrade().map(|w| w.window().request_redraw());
}

/// `--shot PATH --select SESSION [--theme ID]`: once live, open SESSION,
/// let it load and settle, save the window as PNG and quit.
pub fn start_shot(path: String, session: String) {
    let timer = slint::Timer::default();
    let started = Instant::now();
    let chosen = Cell::new(false);
    let settled: Cell<Option<Instant>> = Cell::new(None);
    timer.start(slint::TimerMode::Repeated, Duration::from_millis(100), move || {
        let Some(app) = crate::app() else { return };
        if started.elapsed() > Duration::from_secs(20) {
            check(false, "timed out waiting to capture");
            finish();
            return;
        }
        let live = app.engine.borrow().connection_state() == "live";
        if !live {
            return;
        }
        if !chosen.get() {
            chosen.set(true);
            app.engine.borrow_mut().select(&session);
            crate::pump();
            return;
        }
        let loaded = app.engine.borrow().conversation(&session).is_some_and(|c| !c.loading() && !c.rows().is_empty());
        if !loaded {
            return;
        }
        let since = *settled.get().get_or_insert_with(Instant::now);
        settled.set(Some(since));
        if since.elapsed() < Duration::from_millis(600) {
            return;
        }
        shot_to(&path);
        TIMER.with(|t| t.borrow_mut().take());
        finish();
    });
    TIMER.with(|t| *t.borrow_mut() = Some(timer));
}

fn shot_to(path: &str) {
    let saved = headless::save_frame(path);
    check(saved.is_ok(), &format!("captured {path} {}", saved.err().unwrap_or_default()));
}

/// Posts a test control to the fake Host (`/__control/...`), which takes
/// no token. The check never runs against a real Host.
fn control(path: &str, body: &serde_json::Value) -> Result<(), String> {
    use std::io::{Read, Write};
    let base = std::env::var("CLARP_BASE_URL").map_err(|e| e.to_string())?;
    let url = url_host(&base).ok_or_else(|| format!("not a loopback Host: {base}"))?;
    let body = body.to_string();
    let mut stream = std::net::TcpStream::connect(&url).map_err(|e| e.to_string())?;
    let request = format!(
        "POST {path} HTTP/1.1\r\nHost: {url}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        body.len()
    );
    stream.write_all(request.as_bytes()).map_err(|e| e.to_string())?;
    let mut reply = String::new();
    stream.read_to_string(&mut reply).map_err(|e| e.to_string())?;
    if reply.starts_with("HTTP/1.1 200") { Ok(()) } else { Err(reply.lines().next().unwrap_or_default().to_owned()) }
}

/// "127.0.0.1:PORT" from a loopback base URL, else None.
fn url_host(base: &str) -> Option<String> {
    let rest = base.strip_prefix("http://127.0.0.1:")?;
    let port: u16 = rest.trim_end_matches('/').parse().ok()?;
    Some(format!("127.0.0.1:{port}"))
}

type Stage = (&'static str, Box<dyn FnMut(&crate::App, &crate::AppWindow, Duration) -> bool>);

/// Runs `stages` in order: each is polled every 100 ms until it returns
/// true, and fails the run if it takes over 15 s.
fn run_stages(mut stages: Vec<Stage>) {
    let timer = slint::Timer::default();
    let since = Cell::new(Instant::now());
    let index = Cell::new(0usize);
    timer.start(slint::TimerMode::Repeated, Duration::from_millis(100), move || {
        let (Some(app), Some(window)) = (crate::app(), crate::window()) else { return };
        let app = &*app;
        let Some((name, stage)) = stages.get_mut(index.get()) else {
            TIMER.with(|t| t.borrow_mut().take());
            finish();
            return;
        };
        let elapsed = since.get().elapsed();
        if elapsed > Duration::from_secs(15) {
            check(false, &format!("timed out: {name} (active pane {:?}, report {:?}, panes {:?})", app.active_id(), app.active_report(), app.pane_drafts()));
            TIMER.with(|t| t.borrow_mut().take());
            finish();
            return;
        }
        if stage(app, &window, elapsed) {
            index.set(index.get() + 1);
            since.set(Instant::now());
        }
        window.window().request_redraw();
    });
    TIMER.with(|t| *t.borrow_mut() = Some(timer));
}

fn rows(_window: &crate::AppWindow) -> Vec<crate::MessageRow> {
    crate::app().and_then(|app| app.active_messages()).map(|m| m.iter().collect()).unwrap_or_default()
}

fn app_now() -> std::rc::Rc<crate::App> {
    crate::app().expect("the app runs")
}

fn report() -> crate::panes::Report {
    app_now().active_report()
}

fn view() -> crate::PaneView {
    app_now().active_view().unwrap_or_default()
}

/// `--check transcript --out DIR`: tool activity folds and opens (fetching
/// details the Host left out), a reader scrolling up stops following while
/// rows arrive, and End resumes it. Needs the fake Host.
pub fn start_check(name: &str, out: String) {
    match name {
        "transcript" => transcript_check(out),
        "composer" => composer_check(out),
        "panes" => panes_check(out),
        "switcher" => switcher_check(out),
        "settings" => settings_check(out),
        "keymap" => keymap_check(out),
        "voice" => voice_check(out),
        "desktop" => desktop_check(out),
        "instance" => instance_check(out),
        "diagnostics" => diagnostics_check(out),
        "preview" => preview_check(out),
        "connection" => connection_check(out),
        "lifecycle" => lifecycle_check(out),
        "narration" => narration_check(out),
        "sidebar" => sidebar_check(out),
        // ---- updates and teams
        "updates" => updates_check(out),
        "teams" => teams_check(out),
        // ---- launch dialogs
        "launch" => launch_checks::launch_check(out),
        "agent-dialogs" => launch_checks::agent_dialogs_check(out),
        // ---- profile and overview
        "profile" => profile_checks::profile_check(out),
        "overview" => profile_checks::overview_check(out),
        "extras" => profile_checks::extras_check(out),
        _ => {
            check(false, &format!("no check named {name}"));
            finish();
        }
    }
}

/// The fake Host's request log (`CLARP_TEST_HOST_LOG`): its `/send`s.
fn sends() -> Vec<serde_json::Value> {
    posts("/send")
}

fn posts(path: &str) -> Vec<serde_json::Value> {
    let Some(log) = std::env::var_os("CLARP_TEST_HOST_LOG") else { return Vec::new() };
    std::fs::read_to_string(log)
        .unwrap_or_default()
        .lines()
        .filter_map(|line| serde_json::from_str::<serde_json::Value>(line).ok())
        .filter(|entry| entry["method"] == "POST" && entry["path"] == path)
        .collect()
}

/// `--check composer --out DIR`: drafts belong to their chat, a file is
/// attached (Ctrl+Shift+O, `CLARP_TEST_ATTACH_FILE`), uploaded and removed,
/// the queue and quota notices show, Ctrl+Enter queues the text with the
/// attachment's path, and Escape hands the keyboard to the transcript.
fn composer_check(out: String) {
    use slint::platform::Key;
    let out2 = out.clone();
    let stages: Vec<Stage> = vec![
        ("rachel open", Box::new(|app, _window, _| {
            let open = app.engine.borrow().conversation("rachel").is_some_and(|c| !c.loading() && !c.rows().is_empty());
            if !open {
                return false;
            }
            app_now().focus_composer();
            headless::type_text("draft one");
            true
        })),
        ("typed", Box::new(|app, window, _| {
            if app_now().active_draft() != "draft one" {
                return false;
            }
            check(report().composer_focused, "the composer has the keyboard");
            check(app.engine.borrow().draft("rachel") == "draft one", "typing keeps the chat's draft");
            window.invoke_chat_chosen("mike".into());
            true
        })),
        ("other chat", Box::new(|app, window, _| {
            if app.engine.borrow().selected_session() != "mike" {
                return false;
            }
            check(app_now().active_draft().is_empty(), "another chat has its own (empty) draft");
            window.invoke_chat_chosen("rachel".into());
            true
        })),
        ("back", Box::new(|_, _window, _| {
            if app_now().active_draft() != "draft one" {
                return false;
            }
            check(true, "returning restores the chat's draft");
            app_now().focus_composer();
            headless::press_with(&[Key::Control, Key::Shift], "O");
            true
        })),
        ("uploading", Box::new(|_, _window, _| {
            if view().attachments.row_count() == 0 {
                return false;
            }
            let chip = view().attachments.row_data(0).expect("chip");
            check(chip.name == "photo.png", &format!("Ctrl+Shift+O attaches the file: {} ({})", chip.name, chip.status));
            check(chip.thumbnail.size().width > 0, "an image shows its thumbnail");
            true
        })),
        ("uploaded", Box::new(|_, window, _| {
            let ready = view().attachments.row_data(0).is_some_and(|c| c.status == "ready");
            if !ready || !view().can_send {
                return false;
            }
            check(true, "the upload finishes and the message can be sent");
            let id = view().attachments.row_data(0).expect("chip").id;
            window.invoke_remove_attachment(app_now().active_id(), id);
            true
        })),
        ("removed", Box::new(|_, window, _| {
            if view().attachments.row_count() != 0 {
                return false;
            }
            check(true, "a chip's remove button drops the attachment");
            window.invoke_attach(app_now().active_id());
            let quota = serde_json::json!({"session": "rachel", "set": {"queued_turn_count": 2,
                "backend_quota": {"state": "exhausted", "provider_id": "claude", "reason": "rate_limited"}}});
            check(control("/__control/agent", &quota).is_ok(), "the Host reports a queue and an exhausted quota");
            true
        })),
        ("notices", Box::new(move |_, _window, elapsed| {
            let ready = view().attachments.row_data(0).is_some_and(|c| c.status == "ready");
            if !ready || view().queued != 2 || elapsed < Duration::from_millis(300) {
                return false;
            }
            check(view().quota_notice.starts_with("Claude is out of quota"), &format!("the quota notice shows: {:?}", view().quota_notice));
            shot(&out2, "composer-01-notices");
            app_now().focus_composer();
            headless::press_with(&[Key::Control], Key::Return);
            true
        })),
        ("queued", Box::new(|app, _window, _| {
            let Some(send) = sends().pop() else { return false };
            check(
                send["body"]["text"] == "draft one /srv/uploads/photo.png" && send["body"]["queue_if_busy"] == true,
                &format!("Ctrl+Enter queues the text with the attachment's path: {}", send["body"]),
            );
            check(app_now().active_draft().is_empty() && view().attachments.row_count() == 0, "sending clears the draft and the chips");
            check(app.engine.borrow().draft("rachel").is_empty(), "and the saved draft");
            true
        })),
        ("working", Box::new(|app, _window, _| {
            let state = app.engine.borrow().roster().find("rachel").map(|a| a.latest_state.clone()).unwrap_or_default();
            if state != "thinking" {
                return false;
            }
            headless::press(Key::Escape);
            true
        })),
        ("stopped", Box::new(|_, _window, _| {
            if posts("/stop").is_empty() {
                return false;
            }
            check(report().composer_focused, "Escape stops a working agent and keeps the keyboard in the composer");
            let now = chrono::Utc::now().timestamp_millis();
            let idle = serde_json::json!({"type": "agent-state", "session": "rachel", "kind": "idle", "ts": now});
            check(control("/__control/event", &idle).is_ok(), "the agent goes idle");
            true
        })),
        ("idle", Box::new(|app, _window, _| {
            let state = app.engine.borrow().roster().find("rachel").map(|a| a.latest_state.clone()).unwrap_or_default();
            if state != "idle" {
                return false;
            }
            headless::press(Key::Escape);
            true
        })),
        ("escape", Box::new(|_, _window, elapsed| {
            if elapsed < Duration::from_millis(200) {
                return false;
            }
            check(report().transcript_focused && !report().composer_focused, "Escape hands the keyboard to the transcript");
            // The "clipboard" holds an image: Ctrl+V in the composer attaches it.
            let clipboard = std::env::var_os("CLARP_TEST_CLIPBOARD").expect("check.sh sets the clipboard file");
            let image = std::fs::read(std::env::var_os("CLARP_TEST_ATTACH_FILE").expect("check.sh sets the photo")).expect("the photo");
            std::fs::write(clipboard, image).expect("the clipboard file");
            app_now().focus_composer();
            true
        })),
        ("paste", Box::new(|_, _window, _| {
            if !report().composer_focused {
                return false;
            }
            headless::press_with(&[Key::Control], "v");
            true
        })),
        ("pasted", Box::new(|_, _window, _| {
            let chip = view().attachments.row_data(0);
            let Some(chip) = chip.filter(|c| c.status == "ready") else { return false };
            check(chip.name.ends_with(".png") && app_now().active_draft().is_empty(), &format!("Ctrl+V with an image copied attaches it without typing: {}", chip.name));
            true
        })),
    ];
    run_stages(stages);
}

/// `--check panes --out DIR`: splitting, a draft shared by panes on one
/// chat, moving between panes, the sidebar's J/K/Enter, zoom, close, and
/// the shortcut bar following the keyboard. Keys go through the keyboard
/// map exactly as typed.
fn panes_check(out: String) {
    use slint::platform::Key;
    let out2 = out.clone();
    fn drafts() -> Vec<(String, String, String)> {
        app_now().pane_drafts()
    }
    let stages: Vec<Stage> = vec![
        ("rachel open", Box::new(|app, window, _| {
            let open = app.engine.borrow().conversation("rachel").is_some_and(|c| !c.loading() && !c.rows().is_empty());
            if !open || !report().composer_focused {
                return false;
            }
            check(window.get_keyboard_mode() == "INSERT", &format!("the shortcut bar says INSERT in the composer: {}", window.get_keyboard_mode()));
            headless::press_with(&[Key::Control, Key::Alt], "v");
            true
        })),
        ("split", Box::new(move |app, _window, _| {
            if app.engine.borrow().panes().pane_count() != 2 || !report().composer_focused {
                return false;
            }
            let panes = drafts();
            check(panes.iter().all(|(_, session, _)| session == "rachel"), "Ctrl+Alt+V splits the chat into a second pane");
            check(app.engine.borrow().panes().active_pane_id() == panes[1].0, &format!("the new pane is active, its composer focused: {} of {panes:?}", app.engine.borrow().panes().active_pane_id()));
            headless::type_text("shared");
            true
        })),
        ("shared draft", Box::new(|_, window, _| {
            let panes = drafts();
            if panes.iter().any(|(_, _, draft)| draft != "shared") {
                return false;
            }
            check(true, "typing in one pane shows the draft in the other pane on that chat");
            window.invoke_chat_chosen("mike".into());
            true
        })),
        ("two chats", Box::new(move |app, _window, elapsed| {
            let panes = drafts();
            let loaded = app.engine.borrow().conversation("mike").is_some_and(|c| !c.rows().is_empty());
            if panes.get(1).is_none_or(|p| p.1 != "mike") || !loaded || elapsed < Duration::from_millis(600) {
                return false;
            }
            check(panes[0].1 == "rachel", "choosing a chat opens it in the active pane only");
            check(panes[1].2.is_empty(), "the other chat has its own draft");
            shot(&out2, "panes-01-split");
            headless::press_with(&[Key::Control, Key::Alt], Key::LeftArrow);
            true
        })),
        ("moved left", Box::new(|app, _window, _| {
            if app.engine.borrow().selected_session() != "rachel" {
                return false;
            }
            check(app.engine.borrow().panes().active_pane_id() == drafts()[0].0, "Ctrl+Alt+Left moves to the left pane and selects its chat");
            headless::press(Key::Escape);
            true
        })),
        ("navigating", Box::new(|_, window, _| {
            if !report().transcript_focused {
                return false;
            }
            check(window.get_keyboard_mode() == "CONVERSATION", &format!("Escape leaves the composer for the conversation: {}", window.get_keyboard_mode()));
            headless::press("e");
            true
        })),
        ("sidebar", Box::new(|_, window, _| {
            if !window.get_sidebar_focused() {
                return false;
            }
            check(window.get_keyboard_mode() == "AGENTS" && window.get_sidebar_cursor() == "rachel", "E moves to the agents list, on the open chat");
            headless::press("j");
            true
        })),
        ("cursor", Box::new(|_, window, _| {
            if window.get_sidebar_cursor() == "rachel" {
                return false;
            }
            check(window.get_sidebar_cursor() == "mike", &format!("J moves the cursor down: {}", window.get_sidebar_cursor()));
            headless::press(Key::Return);
            true
        })),
        ("opened", Box::new(|app, _window, _| {
            if app.engine.borrow().selected_session() != "mike" || !report().composer_focused {
                return false;
            }
            check(drafts()[0].1 == "mike", "Enter opens the chat in the active pane, ready to type");
            headless::press_with(&[Key::Control, Key::Alt], "z");
            true
        })),
        ("zoomed", Box::new(|app, _window, _| {
            if app.engine.borrow().panes().zoomed_pane_id().is_empty() {
                return false;
            }
            let shown = view();
            check(shown.shown && shown.width == 1.0, "Ctrl+Alt+Z zooms the active pane to the whole workspace");
            headless::press_with(&[Key::Control, Key::Alt], "z");
            true
        })),
        ("unzoomed", Box::new(|app, _window, _| {
            if !app.engine.borrow().panes().zoomed_pane_id().is_empty() {
                return false;
            }
            headless::press_with(&[Key::Control, Key::Alt], "x");
            true
        })),
        ("closed", Box::new(|app, window, _| {
            if app.engine.borrow().panes().pane_count() != 1 {
                return false;
            }
            check(true, "Ctrl+Alt+X closes the active pane");
            check(window.get_shortcuts_visible(), "the shortcut bar shows by default");
            headless::press_with(&[Key::Control, Key::Shift], "K");
            true
        })),
        ("bar hidden", Box::new(|_, window, _| {
            if window.get_shortcuts_visible() {
                return false;
            }
            check(true, "Ctrl+Shift+K hides the shortcut bar");
            headless::press_with(&[Key::Control, Key::Alt], "s");
            true
        })),
        ("split down", Box::new(|app, window, _| {
            if app.engine.borrow().panes().pane_count() != 2 || window.get_splits().row_count() != 1 {
                return false;
            }
            let split = window.get_splits().row_data(0).expect("split");
            check(!split.vertical && (split.ratio - 0.5).abs() < 0.01, "Ctrl+Alt+S splits down, half and half");
            window.invoke_resize_split(split.id, 0.3);
            true
        })),
        ("resized", Box::new(|_, window, _| {
            let ratio = window.get_splits().row_data(0).map(|s| s.ratio).unwrap_or(0.0);
            if (ratio - 0.3).abs() > 0.01 {
                return false;
            }
            check((view().height - 0.7).abs() < 0.01 || (view().height - 0.3).abs() < 0.01, "dragging the split resizes the panes");
            check(window.get_workspace_bar(), "the workspace bar shows by default, as in the Qt app");
            window.invoke_create_workspace();
            true
        })),
        ("second workspace", Box::new(|app, window, _| {
            if window.get_workspaces().row_count() != 2 {
                return false;
            }
            let active = window.get_workspaces().iter().find(|w| w.active).map(|w| w.name.to_string()).unwrap_or_default();
            check(active == "Workspace 2", &format!("a new workspace gets a tab and opens: {active}"));
            check(app.engine.borrow().panes().pane_count() == 1, "with a single pane");
            headless::press_with(&[Key::Control, Key::Alt], "w");
            true
        })),
        ("back", Box::new(|app, window, _| {
            let active = window.get_workspaces().iter().find(|w| w.active).map(|w| w.name.to_string()).unwrap_or_default();
            if active != "Main" {
                return false;
            }
            check(app.engine.borrow().panes().pane_count() == 2, "Ctrl+Alt+W returns to the first workspace and its two panes");
            true
        })),
    ];
    run_stages(stages);
}

/// `--check switcher --out DIR`: Ctrl+K opens it from the composer, typing
/// ranks agents first, Up/Down/Enter choose, commands and settings run, and
/// Escape closes it with the composer's keyboard back.
fn switcher_check(out: String) {
    use slint::platform::Key;
    let out2 = out.clone();
    fn first(window: &crate::AppWindow) -> String {
        window.get_switcher_rows().row_data(0).map(|r| format!("{}:{}", r.kind, r.label)).unwrap_or_default()
    }
    let stages: Vec<Stage> = vec![
        ("ready", Box::new(|app, _window, _| {
            let open = app.engine.borrow().conversation("rachel").is_some_and(|c| !c.rows().is_empty());
            if !open || !report().composer_focused {
                return false;
            }
            headless::press_with(&[Key::Control], "k");
            true
        })),
        ("open", Box::new(|_, window, elapsed| {
            if !window.get_switcher_open() || elapsed < Duration::from_millis(200) {
                return false;
            }
            check(window.get_keyboard_mode() == "MODAL", "Ctrl+K opens the switcher over everything");
            check(first(window).starts_with("command:"), &format!("with no query, commands come first: {}", first(window)));
            headless::press(Key::DownArrow);
            true
        })),
        ("down", Box::new(|_, window, _| {
            if window.get_switcher_current() != 1 {
                return false;
            }
            headless::press(Key::UpArrow);
            true
        })),
        ("up", Box::new(|_, window, _| {
            if window.get_switcher_current() != 0 {
                return false;
            }
            check(true, "Up and Down move the selection");
            headless::type_text("mik");
            true
        })),
        ("typed", Box::new(move |_, window, elapsed| {
            if window.get_switcher_query() != "mik" || elapsed < Duration::from_millis(200) {
                return false;
            }
            check(first(window) == "agent:Mike", &format!("typing puts the matching agent first: {}", first(window)));
            shot(&out2, "switcher-01-search");
            headless::press(Key::Return);
            true
        })),
        ("agent chosen", Box::new(|app, window, _| {
            if window.get_switcher_open() || app.engine.borrow().selected_session() != "mike" || !report().composer_focused {
                return false;
            }
            check(true, "Enter opens the agent, ready to type");
            headless::press_with(&[Key::Control], "k");
            true
        })),
        ("reopened", Box::new(|_, window, elapsed| {
            if !window.get_switcher_open() || elapsed < Duration::from_millis(200) {
                return false;
            }
            check(window.get_switcher_query().is_empty(), "the switcher reopens empty");
            headless::type_text("split right");
            true
        })),
        ("command", Box::new(|_, window, elapsed| {
            if window.get_switcher_query() != "split right" || elapsed < Duration::from_millis(200) {
                return false;
            }
            check(first(window) == "command:Split right", &format!("every term must match: {}", first(window)));
            headless::press(Key::Return);
            true
        })),
        ("split", Box::new(|app, window, _| {
            if window.get_switcher_open() || app.engine.borrow().panes().pane_count() != 2 {
                return false;
            }
            check(true, "choosing a command runs it");
            check(rows(window).iter().all(|r| r.stamp.is_empty()), "timestamps are off by default");
            headless::press_with(&[Key::Control], "k");
            true
        })),
        ("settings", Box::new(|_, window, elapsed| {
            if !window.get_switcher_open() || elapsed < Duration::from_millis(200) {
                return false;
            }
            headless::type_text("timestamps");
            true
        })),
        ("toggle", Box::new(|_, window, elapsed| {
            if window.get_switcher_query() != "timestamps" || elapsed < Duration::from_millis(200) {
                return false;
            }
            check(first(window) == "command:Off → On · Timestamps", &format!("a setting says what it will do: {}", first(window)));
            headless::press(Key::Return);
            true
        })),
        ("stamps", Box::new(|_, window, _| {
            if window.get_switcher_open() || rows(window).iter().all(|r| r.stamp.is_empty()) {
                return false;
            }
            check(true, "the setting applies at once");
            headless::press_with(&[Key::Control], "k");
            true
        })),
        ("escape", Box::new(|_, window, elapsed| {
            if !window.get_switcher_open() || elapsed < Duration::from_millis(200) {
                return false;
            }
            headless::press(Key::Escape);
            true
        })),
        ("closed", Box::new(|_, window, _| {
            if window.get_switcher_open() || !report().composer_focused {
                return false;
            }
            check(true, "Escape closes it and gives the composer the keyboard back");
            window.invoke_open_switcher();
            true
        })),
        ("outside", Box::new(|_, window, elapsed| {
            if !window.get_switcher_open() || elapsed < Duration::from_millis(200) {
                return false;
            }
            window.invoke_switcher_dismissed();
            check(!window.get_switcher_open(), "a click outside closes it");
            true
        })),
    ];
    run_stages(stages);
}

/// `--check settings --out DIR`: Ctrl+, opens the surface with the Host's
/// status, the keyboard moves over the rows that do something, Space and
/// Left/Right change them, and Escape goes back to the chat.
fn settings_check(out: String) {
    use slint::platform::Key;
    let out2 = out.clone();
    fn row(window: &crate::AppWindow, label: &str) -> Option<crate::SettingRow> {
        window.get_setting_rows().iter().find(|r| r.label == label)
    }
    fn current(window: &crate::AppWindow) -> String {
        window.get_setting_rows().row_data(window.get_setting_current() as usize).map(|r| r.label.to_string()).unwrap_or_default()
    }
    let stages: Vec<Stage> = vec![
        ("ready", Box::new(|app, _window, _| {
            let open = app.engine.borrow().conversation("rachel").is_some_and(|c| !c.rows().is_empty());
            if !open || !report().composer_focused {
                return false;
            }
            headless::press_with(&[Key::Control], ",");
            true
        })),
        ("open", Box::new(move |app, window, _| {
            if window.get_surface() != "settings" || !window.get_settings_focused() || app.engine.borrow().host_status_loading() {
                return false;
            }
            check(window.get_keyboard_mode() == "SETTINGS", "Ctrl+, opens the settings with the keyboard in them");
            // The fake Host's health has no `ready`, which QML also shows as needing attention.
            check(row(window, "Diagnostics").is_some_and(|r| r.detail == "Needs attention"), &format!("the Host's diagnostics show: {:?}", row(window, "Diagnostics").map(|r| r.detail)));
            check(row(window, "Voice provider").is_some_and(|r| r.detail == "elevenlabs"), "and its voice provider");
            check(row(window, "TTS queue").is_some_and(|r| r.detail.contains("pending")), "and its speech queue");
            check(current(window) == "Timestamps", &format!("the first row that does something is current: {}", current(window)));
            shot(&out2, "settings-01");
            headless::press(" ");
            true
        })),
        ("toggled", Box::new(|app, window, _| {
            if !row(window, "Timestamps").is_some_and(|r| r.on) {
                return false;
            }
            check(app.prefs.borrow().timestamps, "Space switches the current row");
            headless::press(Key::DownArrow);
            headless::press(Key::DownArrow);
            headless::press(Key::DownArrow);
            true
        })),
        ("moved", Box::new(|_, window, _| {
            if current(window) != "Tool activity" {
                return false;
            }
            check(true, "Down skips section titles");
            headless::press(Key::RightArrow);
            true
        })),
        ("stepped", Box::new(|app, window, _| {
            if app.engine.borrow().activity_mode() != 1 {
                return false;
            }
            check(row(window, "Tool activity").is_some_and(|r| r.detail == "Always visible"), "Right steps a choice");
            headless::press(Key::Escape);
            true
        })),
        ("back", Box::new(|_, window, _| {
            if window.get_surface() != "chats" || !report().composer_focused {
                return false;
            }
            check(true, "Escape goes back to the chat, ready to type");
            true
        })),
    ];
    run_stages(stages);
}

/// `--check keymap --out DIR`: Ctrl+Alt+, opens the editor, a rebinding
/// is validated and saved, Escape closes it, and the new key works.
fn keymap_check(out: String) {
    use slint::platform::Key;
    let out2 = out.clone();
    let stages: Vec<Stage> = vec![
        ("ready", Box::new(|app, _window, _| {
            let open = app.engine.borrow().conversation("rachel").is_some_and(|c| !c.rows().is_empty());
            if !open || !report().composer_focused {
                return false;
            }
            headless::press_with(&[Key::Control, Key::Alt], ",");
            true
        })),
        ("open", Box::new(move |_, window, elapsed| {
            if window.get_overlay() != "keymap" || elapsed < Duration::from_millis(200) {
                return false;
            }
            check(window.get_keymap_actions().row_count() == 7 && window.get_keymap_profile().contains("\"version\": 1"), "Ctrl+Alt+, opens the key bindings with the profile");
            window.invoke_keymap_apply("zoom".into(), "Ctrl+V".into());
            check(window.get_keymap_error().contains("Reserved"), &format!("a text editing key is refused: {}", window.get_keymap_error()));
            window.invoke_keymap_apply("switcher".into(), "Ctrl+P".into());
            check(window.get_keymap_error().is_empty() && window.get_keymap_profile().contains("Ctrl+P"), "a valid chord is saved into the profile");
            true
        })),
        ("drawn", Box::new(move |_, _window, elapsed| {
            if elapsed < Duration::from_millis(200) {
                return false;
            }
            shot(&out2, "keymap-01");
            headless::press(Key::Escape);
            true
        })),
        ("closed", Box::new(|app, window, _| {
            if !window.get_overlay().is_empty() || !report().composer_focused {
                return false;
            }
            check(app.engine.borrow().settings().get("keymap/bindings").is_some_and(|b| b["switcher"] == "Ctrl+P"), "the binding is kept in settings");
            headless::press_with(&[Key::Control], "k");
            true
        })),
        ("old key", Box::new(|_, window, elapsed| {
            if elapsed < Duration::from_millis(300) {
                return false;
            }
            check(!window.get_switcher_open(), "the old key no longer opens the switcher");
            headless::press_with(&[Key::Control], "p");
            true
        })),
        ("new key", Box::new(|_, window, _| {
            if !window.get_switcher_open() {
                return false;
            }
            check(true, "the new key does");
            headless::press(Key::Escape);
            true
        })),
        ("larger", Box::new(|_, window, elapsed| {
            if window.get_switcher_open() || elapsed < Duration::from_millis(200) {
                return false;
            }
            headless::press_with(&[Key::Control], "=");
            true
        })),
        ("scaled", Box::new(|app, window, _| {
            let scale = window.window().scale_factor();
            if (scale - 1.2).abs() > 0.01 {
                return false;
            }
            check(app.engine.borrow().settings().get("appearance/uiScale").and_then(|v| v.as_f64()) == Some(1.2), "Ctrl+= scales the interface up a step, and keeps it");
            headless::press_with(&[Key::Control], "0");
            true
        })),
        ("reset", Box::new(|_, window, _| {
            if (window.window().scale_factor() - 1.15).abs() > 0.01 {
                return false;
            }
            check(true, "Ctrl+0 resets it to the Qt app's 1.15");
            true
        })),
    ];
    run_stages(stages);
}

/// `--check voice --out DIR`: a clip waiting for the opened chat plays
/// (silently: `CLARP_AUDIO_OUTPUT=null`) and is acknowledged, Talk records
/// the fixture "microphone", and the dictation is sent with its ids.
fn voice_check(out: String) {
    use slint::platform::Key;
    let out2 = out.clone();
    fn acks() -> Vec<String> {
        posts("/clips/ack").iter().map(|a| a["body"]["status"].as_str().unwrap_or_default().to_owned()).collect()
    }
    let stages: Vec<Stage> = vec![
        ("ready", Box::new(|app, window, _| {
            let open = app.engine.borrow().conversation("rachel").is_some_and(|c| !c.rows().is_empty());
            if !open || !report().composer_focused {
                return false;
            }
            window.invoke_chat_chosen("mike".into());
            true
        })),
        ("clip played", Box::new(|_, _window, _| {
            let acks = acks();
            if !acks.iter().any(|s| s == "play-ok") {
                return false;
            }
            check(acks == ["queued", "play-start", "play-ok"], &format!("a clip waiting for the chat is queued, played and acknowledged in order: {acks:?}"));
            headless::press_with(&[Key::Control, Key::Shift], " ");
            true
        })),
        ("recording", Box::new(move |_, _window, _| {
            let recording = crate::platform::audio::with(|audio| audio.recording()).unwrap_or(false);
            if !recording || !view().recording {
                return false;
            }
            check(true, "Ctrl+Shift+Space records for the open chat");
            shot(&out2, "voice-01-recording");
            true
        })),
        ("recorded", Box::new(|_, _window, elapsed| {
            if elapsed < Duration::from_millis(400) {
                return false;
            }
            headless::press_with(&[Key::Control, Key::Shift], " ");
            true
        })),
        ("sent", Box::new(|_, _window, _| {
            let Some(send) = sends().into_iter().find(|s| s["body"]["text"] == "dictated words") else { return false };
            let transcribe = posts("/transcribe").pop().unwrap_or_default();
            check(transcribe["body"]["riff"] == "RIFF" && transcribe["body"]["size"].as_u64().unwrap_or(0) > 1000, "the recording goes to /transcribe as WAV");
            check(
                send["body"]["session"] == "mike" && send["body"]["trace_id"] == "trace-dictation" && send["body"]["transcription_id"] == transcribe["body"]["transcription_id"],
                &format!("the dictation is sent to its chat with its trace and transcription ids: {}", send["body"]),
            );
            check(!view().recording && view().transcribing == 0, "recording and transcribing end");
            true
        })),
        ("mpris", Box::new(|_, _window, _| {
            // The check's own private session bus (dbus-run-session), never the user's.
            let property = |interface: &str, name: &str| {
                std::process::Command::new("dbus-send")
                    .args(["--session", "--print-reply", &format!("--dest=org.mpris.MediaPlayer2.Clarp.instance{}", std::process::id()),
                           "/org/mpris/MediaPlayer2", "org.freedesktop.DBus.Properties.Get", &format!("string:{interface}"), &format!("string:{name}")])
                    .output()
                    .map(|o| String::from_utf8_lossy(&o.stdout).into_owned())
                    .unwrap_or_default()
            };
            let identity = property("org.mpris.MediaPlayer2", "Identity");
            if identity.is_empty() {
                return false;
            }
            check(identity.contains("\"Clarp\""), "the voice is a media player on the session bus");
            check(property("org.mpris.MediaPlayer2.Player", "PlaybackStatus").contains("\"Stopped\""), "stopped once the clip ended");
            true
        })),
    ];
    run_stages(stages);
}

/// `--check desktop --out DIR`: a reply in a chat that is not open raises
/// a notification (recorded: `CLARP_TEST_NOTIFY_LOG`), and someone typing
/// at the window is reported as present (`CLARP_TEST_FOREGROUND`).
fn desktop_check(_out: String) {
    fn notifications() -> Vec<serde_json::Value> {
        let Some(path) = std::env::var_os("CLARP_TEST_NOTIFY_LOG") else { return Vec::new() };
        std::fs::read_to_string(path).unwrap_or_default().lines().filter_map(|l| serde_json::from_str(l).ok()).collect()
    }
    let stages: Vec<Stage> = vec![
        ("ready", Box::new(|app, window, elapsed| {
            let open = app.engine.borrow().conversation("rachel").is_some_and(|c| !c.rows().is_empty());
            // The desktop services start a second after launch.
            if !open || elapsed < Duration::from_millis(1500) {
                return false;
            }
            window.invoke_chat_chosen("mike".into());
            let event = serde_json::json!({"type": "user-notification", "session": "rachel", "persona": "Rachel", "preview": "Echo from Rachel"});
            check(control("/__control/event", &event).is_ok(), "the Host says Rachel replied");
            true
        })),
        ("notified", Box::new(|_, _window, _| {
            let shown = notifications();
            if shown.is_empty() {
                return false;
            }
            check(shown[0]["title"].as_str().is_some_and(|t| t.contains("Rachel")) && shown[0]["body"] == "Echo from Rachel", &format!("a reply in a chat that is not open is notified: {shown:?}"));
            headless::type_text("x");
            true
        })),
        ("present", Box::new(|_, _window, _| {
            let reports = posts("/desktop-presence");
            let Some(active) = reports.iter().find(|r| r["body"]["active"] == true) else { return false };
            check(active["body"]["instance_id"].as_str().is_some_and(|i| !i.is_empty()), "someone typing at the window is reported present");
            check(!posts("/application-activity").is_empty(), "and the window's activity with it");
            true
        })),
    ];
    run_stages(stages);
}

/// `--check instance` (with `CLARP_TEST_INSTANCE=1`): a second launch of
/// this desktop hands its arguments to this window and exits at once.
fn instance_check(_out: String) {
    let stages: Vec<Stage> = vec![
        ("ready", Box::new(|app, _window, _| {
            if app.engine.borrow().connection_state() != "live" {
                return false;
            }
            let started = Instant::now();
            let second = std::env::current_exe()
                .and_then(|exe| std::process::Command::new(exe).args(["--new-agent", "--backend", "codex"]).output());
            match second {
                Ok(output) => check(
                    output.status.success() && started.elapsed() < Duration::from_secs(3),
                    &format!("a second launch hands over and exits in {} ms (status {})", started.elapsed().as_millis(), output.status),
                ),
                Err(error) => check(false, &format!("the second launch did not run: {error}")),
            }
            true
        })),
        ("forwarded", Box::new(|app, _window, _| {
            let Some(request) = app.launch.borrow().clone() else { return false };
            check(request.backend == "codex", &format!("this window takes the launch it was handed: {request:?}"));
            true
        })),
    ];
    run_stages(stages);
}

/// `--check diagnostics` (with `CLARP_STALL_THRESHOLD_MS` and
/// `CLARP_STALL_LOG`): blocking the UI thread is caught with its stack.
fn diagnostics_check(_out: String) {
    let stages: Vec<Stage> = vec![
        ("started", Box::new(|_, _window, elapsed| {
            if elapsed < Duration::from_millis(1600) {
                return false;
            }
            // Block the UI thread past the threshold, as a slow handler would.
            std::thread::sleep(Duration::from_millis(600));
            true
        })),
        ("caught", Box::new(|_, _window, elapsed| {
            let log = std::env::var_os("CLARP_STALL_LOG").and_then(|p| std::fs::read_to_string(p).ok()).unwrap_or_default();
            if !log.contains("-- stall ended") {
                return elapsed < Duration::from_secs(5) || {
                    check(false, &format!("no stall logged: {log:?}"));
                    true
                };
            }
            check(log.contains("== stall at") && log.contains("diagnostics_check"), "a blocked UI thread is logged with the stack that blocked it");
            crate::platform::diagnostics::log_memory();
            true
        })),
    ];
    run_stages(stages);
}

/// `--check preview` (with `CLARP_TEST_PREVIEW_FIXTURE=1`): a newer preview
/// build shows a banner, Ctrl+Alt+U asks to reopen on it, and the versions
/// panel lists the builds and pins one with Enter.
fn preview_check(out: String) {
    use slint::platform::Key;
    let out2 = out.clone();
    let stages: Vec<Stage> = vec![
        ("banner", Box::new(move |_, window, elapsed| {
            if !window.get_preview_enabled() || elapsed < Duration::from_millis(500) {
                return false;
            }
            check(window.get_preview_update_label() == "v1.1.3 · abcd1234", &format!("a newer installed build is offered: {:?}", window.get_preview_update_label()));
            shot(&out2, "preview-01-banner");
            headless::press_with(&[Key::Control, Key::Alt], "u");
            true
        })),
        ("update asked", Box::new(|_, window, _| {
            if window.get_preview_notice().is_empty() {
                return false;
            }
            check(window.get_preview_notice() == "Fixture captured restart request", "Ctrl+Alt+U asks to reopen on the newer build");
            window.invoke_run_command("preview-versions".into());
            true
        })),
        ("panel", Box::new(|_, window, elapsed| {
            if window.get_overlay() != "preview" || elapsed < Duration::from_millis(200) {
                return false;
            }
            let labels: Vec<String> = window.get_preview_versions().iter().map(|v| v.label.to_string()).collect();
            check(labels.len() == 2 && labels[0].contains("installed") && labels[1].contains("running"), &format!("the panel lists the builds: {labels:?}"));
            headless::press(Key::DownArrow);
            headless::press(Key::Return);
            true
        })),
        ("pinned", Box::new(|_, window, _| {
            if window.get_preview_current() != 1 {
                return false;
            }
            check(true, "Down and Enter choose a build");
            headless::press(Key::Escape);
            true
        })),
        ("closed", Box::new(|_, window, _| {
            if !window.get_overlay().is_empty() {
                return false;
            }
            check(true, "Escape closes the panel");
            true
        })),
    ];
    run_stages(stages);
}

/// `--check connection --out DIR`: the Host button opens the connection
/// page, a wrong token is refused, the right one connects, a one-time code
/// pairs (the keyring stays off), and Ctrl+B hides and shows the sidebar.
fn connection_check(out: String) {
    use slint::platform::Key;
    let out2 = out.clone();
    fn base() -> String {
        std::env::var("CLARP_BASE_URL").unwrap_or_default()
    }
    let stages: Vec<Stage> = vec![
        ("live", Box::new(|app, window, _| {
            if app.engine.borrow().connection_state() != "live" {
                return false;
            }
            window.invoke_run_command("connection".into());
            true
        })),
        ("open", Box::new(move |_, window, elapsed| {
            if window.get_overlay() != "connection" || elapsed < Duration::from_millis(200) {
                return false;
            }
            check(window.get_base_url() == clarp_core::settings::normalized_base_url(&base()), "the page shows this Host");
            shot(&out2, "connection-01");
            window.invoke_connect_host(base().into(), "wrong-token".into());
            true
        })),
        ("refused", Box::new(|app, _window, _| {
            if app.engine.borrow().connection_state() != "unauthorized" {
                return false;
            }
            check(true, "a wrong token is refused");
            crate::window().expect("window").invoke_connect_host(base().into(), "probe-token".into());
            true
        })),
        ("connected", Box::new(|app, _window, _| {
            if app.engine.borrow().connection_state() != "live" {
                return false;
            }
            check(true, "the right token connects");
            crate::window().expect("window").invoke_pair_host(base().into(), "123456".into());
            true
        })),
        ("paired", Box::new(|app, window, elapsed| {
            let authorized = requests("GET", "/server-info").iter().any(|r| r["authorization"] == "Bearer cld_probe_paired_device");
            if !authorized || app.engine.borrow().connection_state() != "live" || elapsed < Duration::from_millis(300) {
                return false;
            }
            check(true, "a one-time code pairs this desktop and connects with its device token");
            check(!window.get_stored_credential(), "with the keyring off, nothing is stored");
            headless::press(Key::Escape);
            true
        })),
        ("closed", Box::new(|_, window, _| {
            if !window.get_overlay().is_empty() {
                return false;
            }
            check(window.get_sidebar_visible(), "the sidebar shows");
            headless::press_with(&[Key::Control], "b");
            true
        })),
        ("hidden", Box::new(|_, window, _| {
            if window.get_sidebar_visible() {
                return false;
            }
            check(true, "Ctrl+B hides the sidebar");
            headless::press_with(&[Key::Control], "b");
            true
        })),
        ("shown", Box::new(|_, window, _| {
            if !window.get_sidebar_visible() {
                return false;
            }
            check(true, "and shows it again");
            true
        })),
    ];
    run_stages(stages);
}

/// `--check lifecycle --out DIR`: Relaunch (profile or overview) opens the
/// Start dialog replacing the agent; it relaunches fresh, and forks a past
/// conversation of the same folder.
fn lifecycle_check(out: String) {
    use crate::StartAgent;
    let out2 = out.clone();
    let catalog = serde_json::json!({"providers": {
        "codex": {"label": "Codex", "sort_index": 1, "supports_resume": true, "supports_fork": true, "models": [{"id": "gpt-5", "label": "GPT-5"}]},
    }});
    fn created() -> Vec<serde_json::Value> {
        posts("/agents").into_iter().map(|r| r["body"].clone()).collect()
    }
    let stages: Vec<Stage> = vec![
        ("ready", Box::new(move |app, _window, _| {
            if app.engine.borrow().connection_state() != "live" {
                return false;
            }
            check(control("/__control/catalog", &catalog).is_ok(), "the Host's Codex can resume and fork");
            app.engine.borrow_mut().reconnect();
            crate::pump();
            true
        })),
        ("relaunch", Box::new(|app, window, _| {
            if app.engine.borrow().connection_state() != "live" || !app.engine.borrow().backend_supports_fork("codex") {
                return false;
            }
            app.engine.borrow_mut().select("mike");
            crate::pump();
            check(crate::commands::run(&app_now(), window, "relaunch-agent"), "Relaunch runs");
            true
        })),
        ("start dialog", Box::new(move |_, window, elapsed| {
            let start = window.global::<StartAgent>();
            if window.get_overlay() != "start-agent" || elapsed < Duration::from_millis(200) {
                return false;
            }
            check(start.get_relaunch() && start.get_name() == "Mike", &format!("Relaunch opens the Start dialog for Mike: {}", start.get_name()));
            check(start.get_can_fork(), "Codex offers fork");
            start.set_workspace("/tmp".into());
            start.invoke_workspace_edited("/tmp".into());
            shot(&out2, "lifecycle-01-relaunch");
            start.invoke_start();
            true
        })),
        ("fresh", Box::new(|_, _window, _| {
            let Some(body) = created().pop() else { return false };
            check(body["replace_sid"] == "mike" && body.get("fork_session_id").is_none(), &format!("it relaunches Mike fresh, replacing the old session: {body}"));
            check(crate::commands::run(&app_now(), &crate::window().expect("window"), "relaunch-agent"), "Relaunch again");
            true
        })),
        ("fork mode", Box::new(|_, window, elapsed| {
            let start = window.global::<StartAgent>();
            if window.get_overlay() != "start-agent" || elapsed < Duration::from_millis(200) {
                return false;
            }
            start.set_workspace("/tmp".into());
            start.invoke_workspace_edited("/tmp".into());
            start.invoke_mode_chosen("fork".into());
            true
        })),
        ("past sessions", Box::new(|_, window, _| {
            let start = window.global::<StartAgent>();
            if start.get_past_sessions().row_count() == 0 {
                return false;
            }
            start.invoke_past_chosen(0);
            start.invoke_start();
            true
        })),
        ("forked", Box::new(|_, _window, _| {
            let bodies = created();
            let Some(body) = bodies.iter().find(|b| b.get("fork_session_id").is_some()) else { return false };
            check(body["fork_session_id"] == "old-1" && body["replace_sid"].as_str().is_some_and(|s| s.starts_with("mike")), &format!("fork starts from the chosen past conversation: {body}"));
            true
        })),
    ];
    run_stages(stages);
}

/// `--check narration --out DIR`: with plain-English tools on, an open tool
/// call shows the Host's explanation in place of the raw call.
fn narration_check(out: String) {
    let out2 = out.clone();
    let turns = serde_json::json!({"session": "rachel", "turns": [
        {"id": "n1", "role": "user", "text": "List the files"},
        {"id": "n2", "role": "assistant", "text": "Here they are.",
         "tools": [{"id": "call-1", "name": "Bash", "summary": "ls the project", "command": "ls", "status": "completed"}]},
    ]});
    let stages: Vec<Stage> = vec![
        ("live", Box::new(move |app, window, _| {
            if app.engine.borrow().connection_state() != "live" {
                return false;
            }
            check(control("/__control/turns", &turns).is_ok(), "the Host has a reply with a tool call");
            check(crate::commands::run(&app_now(), window, "tool-narration"), "plain-English tools switch on");
            check(crate::commands::run(&app_now(), window, "setting:detail:3"), "at Plain English");
            true
        })),
        ("folded", Box::new(|_, window, _| {
            let Some(row) = rows(window).into_iter().find(|r| r.id == "n2") else { return false };
            window.invoke_toggle_activity(app_now().active_id(), row.id, row.group_id);
            true
        })),
        ("explained", Box::new(move |app, window, _| {
            let Some(row) = rows(window).into_iter().find(|r| r.id == "n2" && r.expanded) else { return false };
            let Some(tool) = row.tools.row_data(0) else { return false };
            if tool.explanation.is_empty() {
                return false;
            }
            check(tool.narrated && tool.explanation == "Explained: ls the project", &format!("the call is explained in plain English: {:?}", tool.explanation));
            check(app.engine.borrow().settings().integer("experiments/toolDetailLevel", 0) == 3, "the level is kept");
            shot(&out2, "narration-01");
            // A message's copy button puts its text on the clipboard.
            window.invoke_copy_message(app_now().active_id(), "n2".into());
            let copied = std::env::var_os("CLARP_TEST_CLIPBOARD").and_then(|p| std::fs::read_to_string(p).ok()).unwrap_or_default();
            check(copied == "Here they are.", &format!("the copy button copies the message: {copied:?}"));
            true
        })),
    ];
    run_stages(stages);
}

/// `--check sidebar --out DIR`: search and the Unread scope filter the
/// chats, finished helpers fold under their parent, an archived agent is
/// restored, and the pane header says what the agent runs.
fn sidebar_check(out: String) {
    use slint::platform::Key;
    let out2 = out.clone();
    fn names(window: &crate::AppWindow) -> Vec<String> {
        window.get_chats().iter().map(|r| r.name.to_string()).collect()
    }
    let stages: Vec<Stage> = vec![
        ("live", Box::new(|app, window, _| {
            if app.engine.borrow().conversation("rachel").is_none_or(|c| c.rows().is_empty()) || names(window).len() != 2 {
                return false;
            }
            window.invoke_focus_search();
            true
        })),
        ("search", Box::new(|_, window, _| {
            if !window.get_search_focused() {
                return false;
            }
            headless::type_text("mik");
            true
        })),
        ("filtered", Box::new(|_, window, _| {
            if names(window) != ["Mike"] {
                return false;
            }
            check(window.get_keyboard_mode() == "SEARCH", "typing in the search filters the chats to Mike");
            for _ in 0..3 {
                headless::press(Key::Backspace);
            }
            true
        })),
        ("cleared", Box::new(|_, window, _| {
            if names(window).len() != 2 {
                return false;
            }
            let event = serde_json::json!({"type": "user-notification", "session": "mike", "persona": "Mike", "preview": "news"});
            check(control("/__control/event", &event).is_ok(), "Mike has news");
            true
        })),
        ("unread", Box::new(|_, window, _| {
            if !window.get_chats().iter().any(|r| r.session == "mike" && r.unread) {
                return false;
            }
            window.invoke_choose_scope("unread".into());
            true
        })),
        ("unread only", Box::new(|_, window, _| {
            if names(window) != ["Mike"] {
                return false;
            }
            check(true, "Unread shows only the chats with news");
            window.invoke_choose_scope("all".into());
            let helper = serde_json::json!({"session": "mike", "set": {"role": "helper", "parent_agent_id": "a1", "helper_state": "done"}});
            check(control("/__control/agent", &helper).is_ok(), "Mike becomes Rachel's finished helper");
            true
        })),
        ("folded", Box::new(move |_, window, _| {
            let Some(rachel) = window.get_chats().iter().find(|r| r.session == "rachel") else { return false };
            if rachel.done_count != 1 || names(window) != ["Rachel"] {
                return false;
            }
            check(!rachel.done_expanded, "a finished helper folds under its parent as \"1 helper done\"");
            shot(&out2, "sidebar-01-helpers");
            window.invoke_done_helpers_toggled(rachel.done_parent);
            true
        })),
        ("unfolded", Box::new(|_, window, _| {
            if names(window).len() != 2 {
                return false;
            }
            check(true, "the line unfolds the helper");
            let back = serde_json::json!({"session": "mike", "set": {"role": "agent", "parent_agent_id": "", "helper_state": "", "archived_at": 1790000000}});
            check(control("/__control/agent", &back).is_ok(), "Mike is archived");
            true
        })),
        ("archived", Box::new(|_, window, _| {
            if window.get_archived().row_count() != 1 || names(window) != ["Rachel"] {
                return false;
            }
            window.invoke_show_archive(true);
            let row = window.get_archived().row_data(0).expect("archived row");
            check(row.archived && row.name == "Mike", "an archived agent leaves the chats for the archive");
            window.invoke_restore_agent(row.session);
            true
        })),
        ("restored", Box::new(|_, window, _| {
            if window.get_archived().row_count() != 0 || names(window).len() != 2 {
                return false;
            }
            check(posts("/agent-archive").last().is_some_and(|r| r["body"]["archived"] == false), "Restore brings it back");
            window.invoke_show_archive(false);
            let runtime = serde_json::json!({"session": "rachel", "set": {"model": "", "effort": "", "status_text": "Reading the docs"}});
            check(control("/__control/agent", &runtime).is_ok(), "Rachel reports a status and no model");
            true
        })),
        ("header", Box::new(|_, _window, _| {
            let pane = view();
            if pane.status != "Reading the docs" {
                return false;
            }
            check(pane.model.is_empty() && pane.effort.is_empty(), "the header then says the model is not reported and the effort is the default");
            true
        })),
    ];
    run_stages(stages);
}

fn transcript_check(out: String) {
    {
    let tools = serde_json::json!({"session": "rachel", "turns": [
        {"id": "t1", "role": "user", "text": "Run the build"},
        {"id": "t2", "role": "assistant", "text": "", "activity_count": 2, "tool_details_available": true},
        {"id": "t3", "role": "assistant", "text": "", "activity_count": 1, "tool_details_available": true},
        {"id": "t4", "role": "assistant", "text": "I read **main.rs**.",
         "tools": [{"name": "Read", "file_path": "src/main.rs", "status": "completed", "result": "fn main() {}"}]},
        {"id": "t5", "role": "assistant", "text": "Done."},
    ]});
    let fill = |count: usize| serde_json::json!({"session": "mike", "count": count, "prefix": "Line"});
    let out2 = out.clone();
    let out3 = out.clone();
    let offset = Rc::new(Cell::new(0.0f32));
    let offset2 = offset.clone();
    let stages: Vec<Stage> = vec![
        ("live", Box::new(move |app, _, _| {
            if app.engine.borrow().connection_state() != "live" {
                return false;
            }
            check(control("/__control/turns", &tools).is_ok(), "the Host takes a transcript with tool calls");
            app.engine.borrow_mut().select("rachel");
            crate::pump();
            true
        })),
        ("tool rows fold", Box::new(|_, window, _| {
            let rows = rows(window);
            let Some(group) = rows.iter().find(|r| !r.group_id.is_empty()) else { return false };
            check(group.activity_label.starts_with("3 tool calls"), &format!("old activity folds into one row: {:?}", group.activity_label));
            check(!group.expanded && group.tools.row_count() == 0, "a folded group shows no tool cards");
            let read = rows.iter().find(|r| r.id == "t4");
            check(read.is_some_and(|r| r.activity_label == "1 tool call" && !r.expanded), "a reply's own tool call is folded behind its toggle");
            window.invoke_toggle_activity(app_now().active_id(), group.id.clone(), group.group_id.clone());
            window.invoke_toggle_activity(app_now().active_id(), "t4".into(), "".into());
            true
        })),
        ("tools open with details", Box::new(move |_, window, _| {
            let rows = rows(window);
            let Some(group) = rows.iter().find(|r| !r.group_id.is_empty()) else { return false };
            if !group.expanded || group.tools.row_count() < 2 {
                return false;
            }
            let names: Vec<String> = group.tools.iter().map(|t| t.name.to_string()).collect();
            check(names.iter().all(|n| n == "Bash"), &format!("opening a group fetches the tool calls the Host left out: {names:?}"));
            let read = rows.iter().find(|r| r.id == "t4").expect("t4");
            let tool = read.tools.row_data(0);
            check(
                read.expanded && tool.as_ref().is_some_and(|t| t.name == "Read" && t.summary == "src/main.rs" && t.detail.contains("fn main")),
                "a reply's tool card shows name, file and result",
            );
            shot(&out2, "transcript-01-tools");
            true
        })),
        ("folds again", Box::new(move |app, window, elapsed| {
            if elapsed < Duration::from_millis(300) {
                return false;
            }
            let rows_now = rows(window);
            let group = rows_now.iter().find(|r| !r.group_id.is_empty()).expect("group");
            window.invoke_toggle_activity(app_now().active_id(), group.id.clone(), group.group_id.clone());
            let folded = rows(window).iter().find(|r| !r.group_id.is_empty()).is_some_and(|r| !r.expanded);
            check(folded, "a second toggle folds the group");
            check(control("/__control/fill", &fill(80)).is_ok(), "the Host takes a long chat");
            app.engine.borrow_mut().select("mike");
            crate::pump();
            true
        })),
        ("long chat follows", Box::new(|_, window, elapsed| {
            if rows(window).len() < 80 || elapsed < Duration::from_millis(600) {
                return false;
            }
            check(report().follows && report().at_end, &format!("a chat opens at its latest message (follows {}, at end {}, offset {})", report().follows, report().at_end, report().offset));
            app_now().focus_transcript();
            true
        })),
        ("transcript keyboard", Box::new(|_, _window, _| {
            if !report().transcript_focused {
                return false;
            }
            headless::press(slint::platform::Key::PageUp);
            headless::press(slint::platform::Key::PageUp);
            true
        })),
        ("paused", Box::new(move |_, _window, elapsed| {
            if elapsed < Duration::from_millis(300) {
                return false;
            }
            check(report().transcript_focused, "Escape-style focus gives the transcript the keyboard");
            check(!report().follows && !report().at_end, "Page Up scrolls back and stops following");
            offset.set(report().offset);
            check(control("/__control/fill", &fill(90)).is_ok(), "the Host adds rows");
            true
        })),
        ("stays put", Box::new(move |_, window, elapsed| {
            if rows(window).len() < 90 || elapsed < Duration::from_millis(500) {
                return false;
            }
            check(!report().follows, "new rows do not pull a reader who scrolled up");
            let moved = (report().offset - offset2.get()).abs();
            check(moved < 1.0, &format!("the reader's place holds ({moved}px)"));
            headless::press(slint::platform::Key::End);
            true
        })),
        ("resumes", Box::new(move |_, _window, elapsed| {
            if elapsed < Duration::from_millis(300) {
                return false;
            }
            check(report().follows && report().at_end, "End returns to the latest and follows again");
            check(control("/__control/fill", &fill(100)).is_ok(), "the Host adds more rows");
            true
        })),
        ("follows new rows", Box::new(move |_, window, elapsed| {
            if rows(window).len() < 100 || elapsed < Duration::from_millis(500) {
                return false;
            }
            check(report().at_end, "following keeps the latest in view as rows arrive");
            shot(&out3, "transcript-02-following");
            let long = serde_json::json!({"session": "long", "count": 250, "prefix": "Line"});
            check(
                control("/__control/add-agent", &serde_json::json!({"session": "long"})).is_ok() && control("/__control/fill", &long).is_ok(),
                "the Host holds a chat longer than one page",
            );
            true
        })),
        ("open the long chat", Box::new(|app, _, _| {
            if app.engine.borrow().roster().find("long").is_none() {
                return false;
            }
            app.engine.borrow_mut().select("long");
            crate::pump();
            true
        })),
        ("one page", Box::new(|_, window, elapsed| {
            let rows = rows(window);
            if rows.len() != 100 || rows.last().is_none_or(|r| r.id != "long-249") || elapsed < Duration::from_millis(500) {
                return false;
            }
            check(rows[0].id == "long-150", &format!("the chat loads its latest page: first {}", rows[0].id));
            headless::press(slint::platform::Key::Home);
            true
        })),
        ("older page", Box::new(|_, window, elapsed| {
            let rows = rows(window);
            if rows.len() < 200 || elapsed < Duration::from_millis(500) {
                return false;
            }
            check(rows[0].id == "long-50" && rows.len() == 200, &format!("reaching the top loads the page before: first {}", rows[0].id));
            check(
                !report().follows && report().offset < -1000.0,
                &format!("the reader stays on the row they were reading ({}px)", report().offset),
            );
            headless::press(slint::platform::Key::Home);
            true
        })),
        ("oldest page", Box::new(|app, window, elapsed| {
            let rows = rows(window);
            if rows.len() < 250 || elapsed < Duration::from_millis(500) {
                return false;
            }
            check(rows[0].id == "long-0", "the next page reaches the first message");
            check(app.engine.borrow().conversation("long").is_some_and(|c| !c.has_more()), "and there is nothing older to load");
            true
        })),
    ];
    let _ = out;
    run_stages(stages);
    }
}

// ---- updates and teams

/// The fake Host's requests for `method` and `path`.
fn requests(method: &str, path: &str) -> Vec<serde_json::Value> {
    let Some(log) = std::env::var_os("CLARP_TEST_HOST_LOG") else { return Vec::new() };
    std::fs::read_to_string(log)
        .unwrap_or_default()
        .lines()
        .filter_map(|line| serde_json::from_str::<serde_json::Value>(line).ok())
        .filter(|entry| entry["method"] == method && entry["path"] == path)
        .collect()
}

/// A left click at (x, y) in the window, as a mouse sends it.
fn click_at(window: &crate::AppWindow, x: f32, y: f32) {
    use slint::platform::{PointerEventButton, WindowEvent};
    let position = slint::LogicalPosition::new(x, y);
    let window = window.window();
    window.dispatch_event(WindowEvent::PointerMoved { position });
    window.dispatch_event(WindowEvent::PointerPressed { position, button: PointerEventButton::Left });
    window.dispatch_event(WindowEvent::PointerReleased { position, button: PointerEventButton::Left });
    window.dispatch_event(WindowEvent::PointerExited);
}

/// `--check updates --out DIR`: a running job shows in its chat's row and
/// process popover (Escape / click outside close it), Ctrl+2 opens the
/// Updates, a decision resolves, a job cancels, Ctrl+R reloads, a report
/// opens and closes, Escape and the rail switch surfaces, and Ctrl+J opens
/// the next chat that wants the user.
fn updates_check(out: String) {
    use slint::platform::Key;
    let (out1, out2, out3, out4) = (out.clone(), out.clone(), out.clone(), out);
    let attention_gets = Rc::new(Cell::new(0usize));
    let gets = attention_gets.clone();
    let stages: Vec<Stage> = vec![
        ("ready", Box::new(|app, _window, _| {
            let open = app.engine.borrow().conversation("rachel").is_some_and(|c| !c.rows().is_empty());
            if !open || !report().composer_focused {
                return false;
            }
            let decision = |id: &str, title: &str, question: &str, context: &str, yes: &str, no: &str, revision: i64| serde_json::json!({
                "id": id, "decision_id": id, "session": "mike", "agent_name": "Mike", "kind": "decision", "title": title,
                "question": question, "context": context, "yes_label": yes, "no_label": no, "revision": revision});
            let items = serde_json::json!({"items": [
                decision("d1", "Deploy to production?", "The release branch is green. Ship it to production now?",
                         "All 214 tests pass; the canary has run for 30 minutes.", "Deploy", "Hold", 3),
                decision("d2", "Rotate the API key?", "The staging key expires on Friday.", "", "Yes", "No", 1)]});
            let now = chrono::Utc::now().timestamp_millis();
            let job = serde_json::json!({"job_id": "j1", "agent_id": "a1", "session": "rachel", "status": "running", "kind": "watch",
                "title": "Index the docs", "detail": "12 of 48 files", "metadata": {"completed": 12, "total": 48},
                "started_at": now - 95_000, "heartbeat_at": now - 4_000, "updated_at": now});
            let jobs = serde_json::json!({"jobs": [job], "event": {"type": "background-job-updated", "job": job}});
            let sent = control("/__control/attention", &items).and_then(|()| control("/__control/jobs", &jobs));
            check(sent.is_ok(), &format!("the fake Host takes the attention items and the job: {sent:?}"));
            true
        })),
        ("job listed", Box::new(|app, window, _| {
            let row = window.get_chats().iter().find(|r| r.session == "rachel");
            if app.engine.borrow().background_jobs().len() != 1 || !row.as_ref().is_some_and(|r| r.jobs == 1) || window.get_attention() != 2 {
                return false;
            }
            check(true, "the job shows in Rachel's row and the rail counts two decisions");
            // The hourglass on Rachel's row.
            click_at(window, 311.0, 150.0);
            let jobs: Vec<crate::ProcessJob> = window.get_process_jobs().iter().collect();
            check(window.get_overlay() == "processes", "clicking the row's indicator opens the process popover");
            check(
                jobs.len() == 1 && jobs[0].title == "Index the docs" && jobs[0].detail == "watch · 12 of 48 files" && jobs[0].elapsed != "",
                &format!("it lists the job with its kind, detail and running time: {:?}", jobs.first().map(|j| (j.detail.clone(), j.elapsed.clone()))),
            );
            check(window.get_process_count() == "1 running", &format!("and counts it: {}", window.get_process_count()));
            true
        })),
        ("popover drawn", Box::new(move |_, _window, elapsed| {
            if elapsed < Duration::from_millis(300) {
                return false;
            }
            shot(&out1, "updates-01-processes");
            headless::press(Key::Escape);
            true
        })),
        ("popover closed", Box::new(|_, window, _| {
            if !window.get_overlay().is_empty() {
                return false;
            }
            check(report().composer_focused, "Escape closes the popover; the composer has the keyboard back");
            window.invoke_show_processes("rachel".into(), 330.0, 60.0);
            click_at(window, 1100.0, 700.0);
            check(window.get_overlay().is_empty(), "a click outside closes it too");
            true
        })),
        ("chat again", Box::new(|_, _window, elapsed| {
            if !report().composer_focused || elapsed < Duration::from_millis(200) {
                return false;
            }
            headless::press_with(&[Key::Control], "2");
            true
        })),
        ("open", Box::new(|app, window, elapsed| {
            let engine = app.engine.borrow();
            let loaded = !engine.updates_loading() && engine.update_artifacts().len() == 4 && window.get_update_attention().row_count() == 2;
            if window.get_surface() != "updates" || !loaded || elapsed < Duration::from_millis(300) {
                return false;
            }
            check(window.get_updates_focused() && window.get_keyboard_mode() == "UPDATES", &format!("Ctrl+2 opens the Updates with the keyboard in them ({}, {})", window.get_updates_focused(), window.get_keyboard_mode()));
            let jobs: Vec<crate::JobView> = window.get_update_jobs().iter().collect();
            check(jobs.len() == 1 && jobs[0].active && (jobs[0].progress - 0.25).abs() < 0.01 && jobs[0].status == "RUNNING", "the job shows running with its progress");
            let artifacts: Vec<crate::ArtifactView> = window.get_update_artifacts().iter().collect();
            check(
                artifacts.iter().filter(|a| a.readable).count() == 2 && artifacts[1].title == "Findings" && artifacts[1].kind == "DOCUMENT",
                "the artifacts list, the document and the page readable",
            );
            true
        })),
        ("drawn", Box::new(move |_, window, _| {
            shot(&out2, "updates-02-panel");
            window.invoke_resolve_decision("d1".into(), "yes".into(), 3);
            check(window.get_update_attention().row_data(0).is_some_and(|d| d.pending), "a decision being resolved shows it");
            true
        })),
        ("resolved", Box::new(|_, window, _| {
            if window.get_update_attention().row_count() != 1 || window.get_attention() != 1 {
                return false;
            }
            let posted = posts("/decisions/d1/resolve");
            check(
                posted.len() == 1 && posted[0]["body"] == serde_json::json!({"choice": "accepted", "expected_revision": 3}),
                &format!("Deploy accepts the decision at the revision shown: {posted:?}"),
            );
            window.invoke_cancel_job("j1".into());
            check(window.get_update_jobs().row_data(0).is_some_and(|j| j.pending), "the job's cancel is in flight");
            true
        })),
        ("cancelled", Box::new(move |_, window, _| {
            if requests("DELETE", "/background-jobs/j1").len() != 1 || window.get_update_jobs().row_data(0).is_some_and(|j| j.pending) {
                return false;
            }
            check(true, "the cancel reaches the Host");
            gets.set(requests("GET", "/attention").len());
            headless::press_with(&[Key::Control], "r");
            true
        })),
        ("refreshed", Box::new(move |app, window, _| {
            if requests("GET", "/attention").len() <= attention_gets.get() || app.engine.borrow().updates_loading() {
                return false;
            }
            check(true, "Ctrl+R reloads the updates");
            window.invoke_open_report("doc1".into());
            true
        })),
        ("report", Box::new(move |_, window, elapsed| {
            if window.get_overlay() != "report" || elapsed < Duration::from_millis(300) {
                return false;
            }
            let blocks: Vec<crate::MessageBlock> = window.get_report_blocks().iter().collect();
            check(window.get_report_title() == "Findings" && window.get_report_summary() == "What we found" && !window.get_report_html(), "Read opens the report with its title and summary");
            check(blocks.len() == 2 && blocks[0].kind == "heading" && blocks[1].kind == "prose", &format!("its Markdown body: {:?}", blocks.iter().map(|b| b.kind.clone()).collect::<Vec<_>>()));
            shot(&out3, "updates-03-report");
            headless::press(Key::Escape);
            true
        })),
        ("report closed", Box::new(|_, window, _| {
            if !window.get_overlay().is_empty() {
                return false;
            }
            check(window.get_surface() == "updates" && window.get_updates_focused(), "Escape closes the report, back on the Updates");
            window.invoke_open_report("html1".into());
            let heading = window.get_report_blocks().row_data(0);
            check(window.get_report_html() && heading.is_some_and(|b| b.kind == "heading"), "an HTML report reads as its headings and text");
            click_at(window, 60.0, 400.0);
            check(window.get_overlay().is_empty(), "a click outside closes the report");
            headless::press(Key::Escape);
            true
        })),
        ("chats", Box::new(|_, window, _| {
            if window.get_surface() != "chats" || !report().composer_focused {
                return false;
            }
            check(true, "Escape goes back to the chats, ready to type");
            window.invoke_surface_chosen("updates".into());
            check(window.get_surface() == "updates", "the rail's Updates button opens them again");
            headless::press_with(&[Key::Control], "j");
            true
        })),
        ("next attention", Box::new(move |app, window, elapsed| {
            if app.engine.borrow().selected_session() != "mike" || window.get_surface() != "chats" || elapsed < Duration::from_millis(400) {
                return false;
            }
            check(report().composer_focused, "Ctrl+J opens Mike's chat, whose decision is still pending, ready to type");
            shot(&out4, "updates-04-next-attention");
            true
        })),
    ];
    run_stages(stages);
}

/// `--check teams --out DIR`: Ctrl+3 opens the Teams on the first team;
/// a team is created, selected and edited, a member added and removed,
/// nudging switched, the delete dialog closed by Escape and a click outside
/// and then confirmed, and Escape goes back to the chats.
fn teams_check(out: String) {
    use slint::platform::Key;
    let (out1, out2, out3, out4) = (out.clone(), out.clone(), out.clone(), out);
    let stages: Vec<Stage> = vec![
        ("ready", Box::new(|app, _window, _| {
            let open = app.engine.borrow().conversation("rachel").is_some_and(|c| !c.rows().is_empty());
            if !open || !report().composer_focused {
                return false;
            }
            headless::press_with(&[Key::Control], "3");
            true
        })),
        ("open", Box::new(move |_, window, elapsed| {
            if window.get_surface() != "teams" || window.get_team_messages().row_count() != 1 || window.get_teams_loading() || elapsed < Duration::from_millis(300) {
                return false;
            }
            check(window.get_teams_focused() && window.get_keyboard_mode() == "TEAMS", "Ctrl+3 opens the Teams with the keyboard in them");
            let members: Vec<crate::TeamMemberView> = window.get_team_members().iter().collect();
            check(
                window.get_team_selected() == "t1" && window.get_team_title() == "Core" && members.len() == 1 && members[0].name == "Rachel",
                "the first team shows with its member",
            );
            check(window.get_team_messages().row_data(0).is_some_and(|m| m.text == "Standup at 9"), "and its messages");
            shot(&out1, "teams-01-panel");
            window.invoke_team_action("grouping".into(), "".into());
            check(!window.get_teams_grouped(), "the list can go flat");
            window.invoke_team_action("grouping".into(), "".into());
            window.invoke_team_action("create".into(), "".into());
            check(window.get_overlay() == "team-create", "New team opens the create dialog");
            headless::type_text("Ops");
            headless::press(Key::Return);
            true
        })),
        ("created", Box::new(|_, window, _| {
            if window.get_team_list().row_count() != 2 || window.get_teams_loading() {
                return false;
            }
            let posted = posts("/teams");
            check(window.get_overlay().is_empty() && posted.len() == 1 && posted[0]["body"]["name"] == "Ops", &format!("Enter creates the team: {posted:?}"));
            window.invoke_team_action("select".into(), "t2".into());
            true
        })),
        ("selected", Box::new(|_, window, _| {
            if window.get_team_selected() != "t2" || window.get_teams_loading() {
                return false;
            }
            check(window.get_team_title() == "Ops" && window.get_team_messages().row_count() == 0, "choosing it shows it, with no messages yet");
            window.invoke_team_action("edit".into(), "".into());
            check(window.get_overlay() == "team-edit" && window.get_team_leaders().row_count() == 1, "Edit opens the dialog; with no members only No leader");
            headless::press(Key::End);
            headless::type_text(" crew");
            headless::press(Key::Tab);
            headless::type_text("#2a7");
            true
        })),
        ("edit drawn", Box::new(move |_, _window, elapsed| {
            if elapsed < Duration::from_millis(300) {
                return false;
            }
            shot(&out2, "teams-02-edit");
            headless::press(Key::Return);
            true
        })),
        ("edited", Box::new(|_, window, _| {
            let posted = posts("/teams/t2");
            if posted.is_empty() || !window.get_overlay().is_empty() {
                return false;
            }
            check(posted[0]["body"] == serde_json::json!({"name": "Ops crew", "color": "#2a7", "leader": ""}), &format!("Enter saves the name and colour: {:?}", posted[0]["body"]));
            window.invoke_team_action("add-member".into(), "".into());
            check(window.get_overlay() == "team-member" && window.get_team_agents().row_count() == 2, "+ member offers the roster's agents");
            window.set_team_agent_index(1);
            true
        })),
        ("member drawn", Box::new(move |_, window, elapsed| {
            if elapsed < Duration::from_millis(300) {
                return false;
            }
            shot(&out3, "teams-03-member");
            window.invoke_team_member_added(window.get_team_agent_index());
            true
        })),
        ("member added", Box::new(|_, window, _| {
            let members: Vec<crate::TeamMemberView> = window.get_team_members().iter().collect();
            if members.len() != 1 || window.get_team_title() != "Ops crew" {
                return false;
            }
            check(members[0].name == "Mike" && posts("/teams/t2/members")[0]["body"]["agent_id"] == "a2", "Mike joins the renamed team");
            window.invoke_team_action("remove-member".into(), "a2".into());
            true
        })),
        ("member removed", Box::new(|_, window, _| {
            if window.get_team_members().row_count() != 0 || requests("DELETE", "/teams/t2/members/a2").is_empty() {
                return false;
            }
            check(true, "removing him reaches the Host");
            window.invoke_team_action("nudging".into(), "".into());
            window.invoke_team_action("delete".into(), "".into());
            check(window.get_overlay() == "team-delete", "Delete asks first");
            true
        })),
        ("delete drawn", Box::new(move |_, window, elapsed| {
            if elapsed < Duration::from_millis(300) {
                return false;
            }
            let nudged = posts("/team-nudging");
            check(nudged.len() == 1 && nudged[0]["body"] == serde_json::json!({"team_id": "t2", "nudge_enabled": true}), "nudging switches on");
            shot(&out4, "teams-04-delete");
            headless::press(Key::Escape);
            check(window.get_overlay().is_empty() && window.get_teams_focused(), "Escape closes the dialog, back on the Teams");
            window.invoke_team_action("delete".into(), "".into());
            click_at(window, 60.0, 400.0);
            check(window.get_overlay().is_empty(), "so does a click outside");
            window.invoke_team_action("delete".into(), "".into());
            window.invoke_team_deleted();
            true
        })),
        ("deleted", Box::new(|_, window, _| {
            if window.get_team_list().row_count() != 1 || window.get_teams_loading() || window.get_team_selected() != "t1" {
                return false;
            }
            check(requests("DELETE", "/teams/t2").len() == 1, "the team is deleted and the first one shows again");
            headless::press(Key::Escape);
            true
        })),
        ("back", Box::new(|_, window, _| {
            if window.get_surface() != "chats" || !report().composer_focused {
                return false;
            }
            check(true, "Escape goes back to the chats, ready to type");
            true
        })),
    ];
    run_stages(stages);
}
