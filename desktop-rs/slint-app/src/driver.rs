//! The end-to-end driver (`--e2e-out DIR`): uses the window like a person
//! would, against a real Host, and saves a screenshot per stage. Each stage
//! waits (up to a limit) for what it expects, then acts.

use std::cell::{Cell, RefCell};
use std::time::{Duration, Instant};

use slint::ComponentHandle;

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
