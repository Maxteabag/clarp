//! `--check observe`: watches one chat on whatever Host CLARP_BASE_URL names
//! (a real one included) and records what the desktop shows: every
//! `CLARP_OBSERVE_EVERY_MS` (default 3000) a line with the live state and a
//! screenshot, for `CLARP_OBSERVE_SECONDS` (default 60). Read-only apart from
//! opening the chat named by `CLARP_OBSERVE_SESSION`.

use std::cell::Cell;
use std::time::{Duration, Instant};

use slint::ComponentHandle;

use super::{check, finish, shot};

fn env_u64(name: &str, default: u64) -> u64 {
    std::env::var(name).ok().and_then(|v| v.parse().ok()).unwrap_or(default)
}

pub fn observe_check(out: String) {
    let session = std::env::var("CLARP_OBSERVE_SESSION").unwrap_or_default();
    let every = Duration::from_millis(env_u64("CLARP_OBSERVE_EVERY_MS", 3000));
    let total = Duration::from_secs(env_u64("CLARP_OBSERVE_SECONDS", 60));
    let started = Instant::now();
    let opened = Cell::new(false);
    let shots = Cell::new(0u32);
    let last = Cell::new(Instant::now() - every);
    let timer = slint::Timer::default();
    timer.start(slint::TimerMode::Repeated, Duration::from_millis(200), move || {
        let (Some(app), Some(window)) = (crate::app(), crate::window()) else { return };
        if !opened.get() {
            if !app.engine.borrow().connected() || app.engine.borrow().roster().find(&session).is_none() {
                if started.elapsed() > Duration::from_secs(30) {
                    check(false, &format!("connected and {session} in the roster"));
                    finish();
                }
                return;
            }
            app.engine.borrow_mut().select(&session);
            crate::pump();
            opened.set(true);
            return;
        }
        if last.get().elapsed() < every {
            return;
        }
        last.set(Instant::now());
        let engine = app.engine.borrow();
        let view = engine.live_view(&session);
        let (status, busy, key) = crate::live_view::status(&engine, &session);
        let rows = engine.conversation(&session).map_or(0, |c| c.rows().len());
        let activity = view.map(|v| v.activity().get("state").and_then(|s| s.as_str()).unwrap_or("").to_owned()).unwrap_or_default();
        println!(
            "observe t={:>5.1}s live_items={} active={} lseq={:?} items={} activity={:?} status={:?} busy={} key={:?} log_rows={} transcript_rows={} working={}",
            started.elapsed().as_secs_f32(),
            engine.live_items(),
            engine.live_active(&session),
            view.and_then(|v| v.lseq()),
            view.map_or(0, |v| v.items().len()),
            activity,
            status,
            busy,
            key,
            rows,
            app.active_messages().map_or(0, |m| slint::Model::row_count(&*m)),
            crate::cells_view::working(&engine, &session),
        );
        drop(engine);
        let n = shots.get() + 1;
        shots.set(n);
        shot(&out, &format!("observe-{n:03}"));
        window.window().request_redraw();
        if started.elapsed() > total {
            check(true, "observed");
            finish();
        }
    });
    super::TIMER.with(|t| *t.borrow_mut() = Some(timer));
}
