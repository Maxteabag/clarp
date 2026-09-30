//! The end-to-end driver (`--e2e-out DIR`): uses the window like a person
//! would, against a real Host, and saves a screenshot per stage. Each stage
//! waits (up to a limit) for what it expects, then acts.

use std::cell::{Cell, RefCell};
use std::rc::Rc;
use std::time::{Duration, Instant};

use slint::{ComponentHandle, Model};

use crate::headless;

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
            let draft = app.window.upgrade().map(|w| w.get_draft().to_string()).unwrap_or_default();
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
            let draft = app.window.upgrade().map(|w| w.get_draft().to_string()).unwrap_or_default();
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
            check(engine.error().is_empty(), &format!("no error: {:?}", engine.error()));
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
            check(false, &format!("timed out: {name}"));
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

fn rows(window: &crate::AppWindow) -> Vec<crate::MessageRow> {
    window.get_messages().iter().collect()
}

/// `--check transcript --out DIR`: tool activity folds and opens (fetching
/// details the Host left out), a reader scrolling up stops following while
/// rows arrive, and End resumes it. Needs the fake Host.
pub fn start_check(name: &str, out: String) {
    match name {
        "transcript" => transcript_check(out),
        "composer" => composer_check(out),
        _ => {
            check(false, &format!("no check named {name}"));
            finish();
        }
    }
}

/// The fake Host's request log (`CLARP_TEST_HOST_LOG`): its `/send`s.
fn sends() -> Vec<serde_json::Value> {
    let Some(log) = std::env::var_os("CLARP_TEST_HOST_LOG") else { return Vec::new() };
    std::fs::read_to_string(log)
        .unwrap_or_default()
        .lines()
        .filter_map(|line| serde_json::from_str::<serde_json::Value>(line).ok())
        .filter(|entry| entry["method"] == "POST" && entry["path"] == "/send")
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
        ("rachel open", Box::new(|app, window, _| {
            let open = app.engine.borrow().conversation("rachel").is_some_and(|c| !c.loading() && !c.rows().is_empty());
            if !open {
                return false;
            }
            window.invoke_focus_composer();
            headless::type_text("draft one");
            true
        })),
        ("typed", Box::new(|app, window, _| {
            if window.get_draft() != "draft one" {
                return false;
            }
            check(window.get_composer_focused(), "the composer has the keyboard");
            check(app.engine.borrow().draft("rachel") == "draft one", "typing keeps the chat's draft");
            window.invoke_chat_chosen("mike".into());
            true
        })),
        ("other chat", Box::new(|app, window, _| {
            if app.engine.borrow().selected_session() != "mike" {
                return false;
            }
            check(window.get_draft().is_empty(), "another chat has its own (empty) draft");
            window.invoke_chat_chosen("rachel".into());
            true
        })),
        ("back", Box::new(|_, window, _| {
            if window.get_draft() != "draft one" {
                return false;
            }
            check(true, "returning restores the chat's draft");
            window.invoke_focus_composer();
            headless::press_with(&[Key::Control, Key::Shift], "O");
            true
        })),
        ("uploading", Box::new(|_, window, _| {
            if window.get_attachments().row_count() == 0 {
                return false;
            }
            let chip = window.get_attachments().row_data(0).expect("chip");
            check(chip.name == "photo.png", &format!("Ctrl+Shift+O attaches the file: {} ({})", chip.name, chip.status));
            check(chip.thumbnail.size().width > 0, "an image shows its thumbnail");
            true
        })),
        ("uploaded", Box::new(|_, window, _| {
            let ready = window.get_attachments().row_data(0).is_some_and(|c| c.status == "ready");
            if !ready || !window.get_can_send() {
                return false;
            }
            check(true, "the upload finishes and the message can be sent");
            let id = window.get_attachments().row_data(0).expect("chip").id;
            window.invoke_remove_attachment(id);
            true
        })),
        ("removed", Box::new(|_, window, _| {
            if window.get_attachments().row_count() != 0 {
                return false;
            }
            check(true, "a chip's remove button drops the attachment");
            window.invoke_attach();
            let quota = serde_json::json!({"session": "rachel", "set": {"queued_turn_count": 2,
                "backend_quota": {"state": "exhausted", "provider_id": "claude", "reason": "rate_limited"}}});
            check(control("/__control/agent", &quota).is_ok(), "the Host reports a queue and an exhausted quota");
            true
        })),
        ("notices", Box::new(move |_, window, elapsed| {
            let ready = window.get_attachments().row_data(0).is_some_and(|c| c.status == "ready");
            if !ready || window.get_queued() != 2 || elapsed < Duration::from_millis(300) {
                return false;
            }
            check(window.get_quota_notice().starts_with("Claude is out of quota"), &format!("the quota notice shows: {:?}", window.get_quota_notice()));
            shot(&out2, "composer-01-notices");
            window.invoke_focus_composer();
            headless::press_with(&[Key::Control], Key::Return);
            true
        })),
        ("queued", Box::new(|app, window, _| {
            let Some(send) = sends().pop() else { return false };
            check(
                send["body"]["text"] == "draft one /srv/uploads/photo.png" && send["body"]["queue_if_busy"] == true,
                &format!("Ctrl+Enter queues the text with the attachment's path: {}", send["body"]),
            );
            check(window.get_draft().is_empty() && window.get_attachments().row_count() == 0, "sending clears the draft and the chips");
            check(app.engine.borrow().draft("rachel").is_empty(), "and the saved draft");
            headless::press(Key::Escape);
            true
        })),
        ("escape", Box::new(|_, window, elapsed| {
            if elapsed < Duration::from_millis(200) {
                return false;
            }
            check(window.get_transcript_focused() && !window.get_composer_focused(), "Escape hands the keyboard to the transcript");
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
            window.invoke_toggle_activity(group.id.clone(), group.group_id.clone());
            window.invoke_toggle_activity("t4".into(), "".into());
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
            window.invoke_toggle_activity(group.id.clone(), group.group_id.clone());
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
            check(window.get_transcript_follows() && window.get_transcript_at_end(), &format!("a chat opens at its latest message (follows {}, at end {}, offset {})", window.get_transcript_follows(), window.get_transcript_at_end(), window.get_transcript_offset()));
            window.invoke_focus_transcript();
            headless::press(slint::platform::Key::PageUp);
            headless::press(slint::platform::Key::PageUp);
            true
        })),
        ("paused", Box::new(move |_, window, elapsed| {
            if elapsed < Duration::from_millis(300) {
                return false;
            }
            check(window.get_transcript_focused(), "Escape-style focus gives the transcript the keyboard");
            check(!window.get_transcript_follows() && !window.get_transcript_at_end(), "Page Up scrolls back and stops following");
            offset.set(window.get_transcript_offset());
            check(control("/__control/fill", &fill(90)).is_ok(), "the Host adds rows");
            true
        })),
        ("stays put", Box::new(move |_, window, elapsed| {
            if rows(window).len() < 90 || elapsed < Duration::from_millis(500) {
                return false;
            }
            check(!window.get_transcript_follows(), "new rows do not pull a reader who scrolled up");
            let moved = (window.get_transcript_offset() - offset2.get()).abs();
            check(moved < 1.0, &format!("the reader's place holds ({moved}px)"));
            headless::press(slint::platform::Key::End);
            true
        })),
        ("resumes", Box::new(move |_, window, elapsed| {
            if elapsed < Duration::from_millis(300) {
                return false;
            }
            check(window.get_transcript_follows() && window.get_transcript_at_end(), "End returns to the latest and follows again");
            check(control("/__control/fill", &fill(100)).is_ok(), "the Host adds more rows");
            true
        })),
        ("follows new rows", Box::new(move |_, window, elapsed| {
            if rows(window).len() < 100 || elapsed < Duration::from_millis(500) {
                return false;
            }
            check(window.get_transcript_at_end(), "following keeps the latest in view as rows arrive");
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
                !window.get_transcript_follows() && window.get_transcript_offset() < -1000.0,
                &format!("the reader stays on the row they were reading ({}px)", window.get_transcript_offset()),
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
