//! `--check frame-budget`: what animating costs the UI thread, on a laptop's
//! panel (CLARP_HEADLESS_SIZE, set by check.sh) with frames paced at 60 Hz
//! and repainted partially, as the real window's softbuffer does. The fake
//! Host serves 180 agents; two panes show two 250-row chats. Each scenario
//! runs for a few seconds with nothing else asking for frames and reports
//! the UI thread's CPU (ms per second), frames per second, frame time
//! (mean, 95th percentile) and the share of the window each frame
//! repainted: an idle fleet, 16 agents working and 30 background jobs with
//! the running row shimmering (Reduce Motion off and on), scrolling and
//! typing over it, and the same fleet drawn whole every frame. Then one
//! full frame's cost. The table goes to `budget.md` beside the shots.
//!
//! The budget, in ms and also in the run's own full frames (a runner twice
//! as fast draws a full frame in half the time, so the ratio holds where
//! the ms do not): the busy fleet under 120 ms/s and 9 full frames a
//! second; typing, in one pane or mirrored into both, under 12 full frames
//! a second with its frames under 8 ms (mirrored: 0.8 of a full frame; 95th
//! percentile); scrolling, which
//! repaints its pane every frame of the wheel's animation, under 32 full
//! frames a second and its frames under one full frame (95th percentile).

use std::cell::RefCell;
use std::rc::Rc;
use std::time::{Duration, Instant};

use serde_json::json;
use slint::ComponentHandle;
use slint::platform::Key;

use super::{Stage, check, control, quiet, report, run_stages};
use crate::headless::{self, Pacing};

/// Busy CPU budget (ms of UI thread per second), typing's frame budget
/// (ms, p95), and the budgets in full frames (ms/s over one full frame).
const CPU_BUDGET: f64 = 120.0;
const FRAME_BUDGET: f64 = 8.0;
const BUSY_FULL_FRAMES: f64 = 9.0;
const TYPING_FULL_FRAMES: f64 = 12.0;
const SCROLLING_FULL_FRAMES: f64 = 32.0;
/// How long a scenario settles, then how long it is measured.
const SETTLE: Duration = Duration::from_millis(1500);
const MEASURE: Duration = Duration::from_secs(5);

/// The display's rate: 60 Hz, or CLARP_BUDGET_HZ (120 for a laptop panel
/// like Peter's).
fn hz() -> u32 {
    std::env::var("CLARP_BUDGET_HZ").ok().and_then(|v| v.parse().ok()).filter(|hz| *hz > 0).unwrap_or(60)
}

fn partial() -> Pacing {
    Pacing { partial: true, hz: hz() }
}

fn whole() -> Pacing {
    Pacing { partial: false, hz: hz() }
}

/// One scenario's numbers.
#[derive(Debug, Clone, Default)]
struct Row {
    name: String,
    cpu: f64,
    fps: f64,
    mean: f64,
    p95: f64,
    dirty: f64,
    frames: usize,
}

impl Row {
    fn line(&self) -> String {
        format!(
            "| {} | {:.0} | {:.1} | {:.2} | {:.2} | {:.0}% |",
            self.name, self.cpu, self.fps, self.mean, self.p95, self.dirty * 100.0
        )
    }
}

/// Where a measurement started: UI thread CPU, frames so far, when.
#[derive(Debug, Clone, Copy)]
struct Mark {
    cpu: Duration,
    frames: usize,
    at: Instant,
    clock: f64,
}

/// CLOCK_MONOTONIC in seconds: perf record -k CLOCK_MONOTONIC stamps its
/// samples with it, so a profile can be cut to one scenario's window.
fn monotonic() -> f64 {
    // SAFETY: clock_gettime fills the zeroed timespec it is given.
    let mut time: libc::timespec = unsafe { std::mem::zeroed() };
    if unsafe { libc::clock_gettime(libc::CLOCK_MONOTONIC, &mut time) } != 0 {
        eprintln!("perf: clock_gettime: {}", std::io::Error::last_os_error());
    }
    time.tv_sec as f64 + time.tv_nsec as f64 / 1e9
}

fn mark() -> Mark {
    quiet(true);
    Mark { cpu: crate::perf::thread_cpu(), frames: crate::perf::stats().frames.len(), at: Instant::now(), clock: monotonic() }
}

fn measured(name: &str, mark: Mark) -> Row {
    quiet(false);
    let seconds = mark.at.elapsed().as_secs_f64().max(0.001);
    let stats = crate::perf::stats();
    let times: Vec<f32> = stats.frames[mark.frames..].iter().map(|f| crate::perf::ms(f.took) as f32).collect();
    let dirty = &stats.frame_dirty[mark.frames..];
    let summary = crate::perf::summarize(times);
    let row = Row {
        name: name.to_owned(),
        cpu: crate::perf::thread_cpu().saturating_sub(mark.cpu).as_secs_f64() * 1000.0 / seconds,
        fps: summary.count as f64 / seconds,
        mean: f64::from(summary.mean),
        p95: f64::from(summary.p95),
        dirty: if dirty.is_empty() { 0.0 } else { dirty.iter().map(|d| f64::from(*d)).sum::<f64>() / dirty.len() as f64 },
        frames: summary.count,
    };
    println!("perf budget window {} {:.6} {:.6} {}", file_name(name), mark.clock, monotonic(), std::process::id());
    // What three frames through it repainted (px), to see what moves.
    let rects = &stats.frame_rects[mark.frames..];
    if std::env::var_os("CLARP_BUDGET_RECTS").is_some() {
        for (at, frame) in rects.iter().enumerate() {
            println!("perf budget {name} rects {at} {frame:?}");
        }
    }
    for at in [rects.len() / 4, rects.len() / 2, rects.len() * 3 / 4] {
        if let Some(frame) = rects.get(at) {
            println!("perf budget {name} frame {at} repainted {frame:?}");
        }
    }
    println!(
        "perf budget {}: UI thread {:.0} ms/s, {:.1} fps, frame mean {:.2} ms, p95 {:.2} ms, repaints {:.0}% of the window ({} frames)",
        row.name, row.cpu, row.fps, row.mean, row.p95, row.dirty * 100.0, row.frames
    );
    row
}

thread_local! {
    /// Input sent while a scenario is measured (wheel turns, keys).
    static INPUT: RefCell<Option<slint::Timer>> = const { RefCell::new(None) };
    /// The active chat's offset at each wheel turn of the scrolling scenario.
    static SCROLLED: Rc<RefCell<Vec<f32>>> = Rc::default();
}

fn input_every(interval: Duration, mut send: impl FnMut() + 'static) {
    let timer = slint::Timer::default();
    timer.start(slint::TimerMode::Repeated, interval, move || send());
    INPUT.with(|i| *i.borrow_mut() = Some(timer));
}

/// Types a key every 50 ms (twenty a second); every fortieth deletes the
/// line typed.
fn type_keys() {
    let mut typed = 0;
    input_every(Duration::from_millis(50), move || {
        typed += 1;
        if typed % 40 == 0 {
            for _ in 0..39 {
                headless::press(Key::Backspace);
            }
        } else {
            headless::press("a");
        }
    });
}

fn stop_input() {
    INPUT.with(|i| i.borrow_mut().take());
}

/// A row of the active chat shimmers (its live label, while it runs).
fn any_shimmering() -> bool {
    use slint::Model;
    crate::app().and_then(|app| app.active_messages()).is_some_and(|rows| rows.iter().any(|r| !r.live.shimmer.is_empty()))
}

/// The process's resident and peak memory (VmRSS, VmHWM), in MB.
fn memory_mb() -> (f64, f64) {
    let status = std::fs::read_to_string("/proc/self/status").unwrap_or_default();
    let field = |name: &str| {
        status.lines().find_map(|l| l.strip_prefix(name)).and_then(|v| v.split_whitespace().next()?.parse::<f64>().ok()).map_or(0.0, |kb| kb / 1024.0)
    };
    (field("VmRSS:"), field("VmHWM:"))
}

/// Starts the peak (VmHWM) again from what is resident now.
fn reset_peak() -> Result<(), String> {
    std::fs::write("/proc/self/clear_refs", "5").map_err(|e| format!("clear_refs: {e}"))
}

/// The UI thread's longest single piece of work since `from` (a frame or
/// an engine wake), in ms.
fn longest_since(from: Duration) -> f64 {
    let stats = crate::perf::stats();
    stats.frames.iter().chain(stats.wakes.iter()).filter(|e| e.at >= from).map(|e| crate::perf::ms(e.took)).fold(0.0, f64::max)
}

fn busy_agents(app: &crate::App) -> usize {
    app.engine.borrow().roster().agents().iter().filter(|a| a.busy).count()
}

/// A scenario's name as a file name.
fn file_name(name: &str) -> String {
    name.replace([' ', ',', '+'], "-").replace("--", "-")
}

/// A measured scenario as a stage: `setup` once, settle, measure, then
/// `done` (after the input stops).
fn scenario(
    name: &'static str,
    out: String,
    rows: Rc<RefCell<Vec<Row>>>,
    mut setup: impl FnMut(&crate::App, &crate::AppWindow) + 'static,
) -> Stage {
    let started: Rc<RefCell<Option<Mark>>> = Rc::default();
    let mut set = false;
    (name, Box::new(move |app: &crate::App, window: &crate::AppWindow, elapsed: Duration| {
        if !set {
            set = true;
            setup(app, window);
            return false;
        }
        if elapsed < SETTLE {
            return false;
        }
        let mut started = started.borrow_mut();
        let Some(mark) = *started else {
            *started = Some(mark());
            return false;
        };
        if mark.at.elapsed() < MEASURE {
            return false;
        }
        stop_input();
        rows.borrow_mut().push(measured(name, mark));
        let file = file_name(name);
        let saved = headless::save_frame(&format!("{}/{file}.png", out));
        check(saved.is_ok(), &format!("captured {name} {}", saved.err().unwrap_or_default()));
        true
    }))
}

pub fn frame_budget_check(out: String) {
    let rows: Rc<RefCell<Vec<Row>>> = Rc::default();
    let full: Rc<RefCell<Vec<f64>>> = Rc::default();
    let (rows1, rows2, rows3, rows4, rows5, rows6, rows7, rows8) =
        (rows.clone(), rows.clone(), rows.clone(), rows.clone(), rows.clone(), rows.clone(), rows.clone(), rows.clone());
    let full1 = full.clone();
    // The pictures stage: RSS before and after, the peak, the longest block.
    let memory: Rc<RefCell<(f64, f64, f64, f64)>> = Rc::default();
    let memory1 = memory.clone();
    let stages: Vec<Stage> = vec![
        ("ready", Box::new(|app, _, _| {
            let loaded = app.engine.borrow().conversation("rachel").is_some_and(|c| !c.loading() && !c.rows().is_empty());
            if !loaded || !report().composer_focused || app.engine.borrow().roster().agents().len() < 180 {
                return false;
            }
            headless::press_with(&[Key::Control, Key::Alt], "v");
            true
        })),
        ("split", Box::new(|app, _, _| {
            if app.engine.borrow().panes().pane_count() != 2 {
                return false;
            }
            // The new pane shows Mike's chat, the first Rachel's: two
            // chats of 250 rows.
            let sent = control("/__control/rich", &json!({"session": "rachel", "count": 250}))
                .and_then(|()| control("/__control/rich", &json!({"session": "mike", "count": 250})))
                .and_then(|()| control("/__control/activity", &json!({"working": 0, "jobs": 0})))
                .and_then(|()| control("/__control/live", &json!({"on": true})));
            check(sent.is_ok(), &format!("the Host serves 180 agents and two 250-row chats, all idle: {sent:?}"));
            app.engine.borrow_mut().select("mike");
            app.engine.borrow_mut().reconnect();
            crate::pump();
            true
        })),
        ("two panes", Box::new(|app, _, elapsed| {
            let ready = app.engine.borrow().panes().pane_count() == 2
                && app.active_session() == "mike"
                && ["rachel", "mike"].iter().all(|s| app.engine.borrow().conversation(s).is_some_and(|c| c.len() >= 250))
                && app.engine.borrow().live_items()
                && busy_agents(app) == 0;
            if !ready || elapsed < Duration::from_secs(2) {
                return false;
            }
            let (width, height, scale) = headless::size_from_env();
            check(true, &format!("two panes, Rachel's chat and Mike's, nothing working, {width}x{height} at {scale}"));
            headless::pace(partial());
            true
        })),
        scenario("idle, 0 working", out.clone(), rows1, |_, _| {}),
        ("busy fleet", Box::new({
            let mut sent = false;
            move |app: &crate::App, _: &crate::AppWindow, elapsed: Duration| {
                if !sent {
                    sent = true;
                    let started = control("/__control/activity", &json!({"working": 16, "jobs": 30}))
                        .and_then(|()| control("/__control/live-replay", &json!({"session": "mike", "fixture": "turn-full", "through": 14})));
                    check(started.is_ok(), &format!("16 agents start working, 30 run a background job, Mike runs a command: {started:?}"));
                    return false;
                }
                if busy_agents(app) < 16 || !any_shimmering() {
                    if elapsed.as_millis() / 100 == 120 {
                        let lseq = app.engine.borrow().live_view("mike").and_then(|v| v.lseq());
                        println!("perf budget waiting: {} agents busy, shimmering {}, live lseq {lseq:?}", busy_agents(app), any_shimmering());
                    }
                    return false;
                }
                let agents = app.engine.borrow().roster().agents().len();
                check(true, &format!("{} of {agents} agents busy; the running row shimmers", busy_agents(app)));
                true
            }
        })),
        scenario("16 working + 30 jobs, shimmer", out.clone(), rows2, |_, _| {}),
        scenario("same, Reduce Motion", out.clone(), rows3, |_, _| super::live_checks::set_reduced_motion(true)),
        scenario("same, scrolling", out.clone(), rows4, |_, _| {
            super::live_checks::set_reduced_motion(false);
            let mut up = true;
            let mut turns = 0;
            // A point in the active (right) pane's chat. Not found by a
            // query: a release build has no element debug info, so a query
            // finds nothing (and walks the whole window, logging a line per
            // element, which then was all this scenario measured).
            let (width, height, _) = headless::size_from_env();
            let (x, y) = (width as f32 * 0.8, height as f32 * 0.45);
            let offsets = SCROLLED.with(|s| s.clone());
            offsets.borrow_mut().clear();
            input_every(Duration::from_millis(33), move || {
                headless::wheel(x, y, if up { 60.0 } else { -60.0 });
                offsets.borrow_mut().push(report().offset);
                turns += 1;
                if turns % 15 == 0 {
                    up = !up;
                }
            });
        }),
        scenario("same, typing", out.clone(), rows5, |_, _| type_keys()),
        scenario("same, every frame whole", out.clone(), rows6, |_, _| headless::pace(whole())),
        // Both panes on Rachel's chat: each key shows in the other pane too.
        scenario("typing, both panes on one chat", out.clone(), rows8, |app, _| {
            headless::pace(partial());
            app.engine.borrow_mut().select("rachel");
            crate::pump();
            app.focus_composer();
            type_keys();
        }),
        ("full frame", Box::new({
            // Ten frames drawn whole, one at a time: each the first frame
            // after its request.
            let mut waiting: Option<usize> = None;
            move |_: &crate::App, _: &crate::AppWindow, _: Duration| {
                let frames = crate::perf::stats().frames;
                match waiting {
                    None => headless::pace(partial()),
                    Some(before) if frames.len() > before => full1.borrow_mut().push(crate::perf::ms(frames[before].took)),
                    Some(_) => return false,
                }
                if full1.borrow().len() >= 10 {
                    return true;
                }
                waiting = Some(frames.len());
                headless::full_frame();
                false
            }
        })),
        ("pictures", Box::new({
            // Six 4K screenshots land in the chat: the peak memory they
            // take and the UI thread's longest block while they land.
            let mut started: Option<(u64, f64, Duration)> = None;
            let memory = memory1.clone();
            move |_: &crate::App, _: &crate::AppWindow, _: Duration| {
                let landed = crate::artifacts_view::pictures_landed();
                let Some((before, rss, from)) = started else {
                    let (rss, _) = memory_mb();
                    let reset = reset_peak();
                    check(reset.is_ok(), &format!("the peak starts again from {rss:.0} MB {:?}", reset.err()));
                    let gallery: String = (1..=6).map(|n| format!("![Screenshot {n}](clarp-media://asset/screenshot-{n})\n")).collect();
                    let sent = control("/__control/upsert", &json!({"session": "rachel", "turns": [
                        {"id": "budget-shots", "role": "assistant", "text": format!("Six screenshots:\n\n```clarp-gallery\n{gallery}```")}]}));
                    check(sent.is_ok(), &format!("Rachel posts six 4K screenshots: {sent:?}"));
                    started = Some((landed, rss, crate::perf::now()));
                    return false;
                };
                if landed < before + 6 {
                    return false;
                }
                let (after, peak) = memory_mb();
                let longest = longest_since(from);
                println!("perf budget pictures: RSS {rss:.0} → {after:.0} MB, peak {peak:.0} MB, longest UI block {longest:.0} ms");
                *memory.borrow_mut() = (rss, after, peak, longest);
                true
            }
        })),
        ("verdict", Box::new(move |_, window, _| {
            let rows = rows7.borrow();
            let full = full.borrow();
            let one = full.iter().sum::<f64>() / full.len().max(1) as f64;
            let (width, height, scale) = headless::size_from_env();
            let size = window.window().size();
            let mut table = format!(
                "Frame budget: {width}x{height} logical at {scale} ({}x{} px), {} Hz pacing, partial repaint; one full frame {one:.2} ms\n\n\
                 | scenario | UI thread ms/s | fps | frame mean ms | frame p95 ms | repainted |\n|---|---|---|---|---|---|\n",
                size.width, size.height, hz()
            );
            for row in rows.iter() {
                table.push_str(&row.line());
                table.push('\n');
            }
            let (rss, after, peak, longest) = *memory.borrow();
            table.push_str(&format!(
                "\nSix 4K screenshots landing in the chat: RSS {rss:.0} → {after:.0} MB, peak {peak:.0} MB, longest UI-thread block {longest:.0} ms\n"
            ));
            println!("perf budget full frame: {one:.2} ms ({}x{} px)", size.width, size.height);
            let saved = std::fs::write(format!("{out}/budget.md"), &table);
            check(saved.is_ok(), &format!("wrote {out}/budget.md {:?}", saved.err()));
            let sessions: Vec<String> = crate::app().map(|app| app.pane_drafts().into_iter().map(|(_, session, _)| session).collect()).unwrap_or_default();
            check(sessions.iter().all(|s| s == "rachel") && sessions.len() == 2, &format!("both panes showed Rachel's chat for the last typing: {sessions:?}"));
            let find = |name: &str| rows.iter().find(|r| r.name == name).cloned().unwrap_or_default();
            let busy = find("16 working + 30 jobs, shimmer");
            check(busy.cpu < CPU_BUDGET, &format!("the busy fleet costs the UI thread {:.0} ms/s (budget {CPU_BUDGET:.0})", busy.cpu));
            let offsets = SCROLLED.with(|s| s.borrow().clone());
            let (low, high) = offsets.iter().fold((f32::MAX, f32::MIN), |(l, h), o| (l.min(*o), h.max(*o)));
            check(offsets.len() > 50 && high - low > 300.0, &format!("the scrolling scenario scrolled the chat: offsets {low:.0} to {high:.0} over {} wheel turns", offsets.len()));
            let full_frames = |row: &Row| row.cpu / one.max(0.1);
            check(full_frames(&busy) < BUSY_FULL_FRAMES, &format!("the busy fleet: {:.1} full frames a second (budget {BUSY_FULL_FRAMES:.0})", full_frames(&busy)));
            // Mirrored, a key repaints both composers: its frames may take
            // up to 0.8 of a full frame.
            for (name, p95) in [("same, typing", FRAME_BUDGET), ("typing, both panes on one chat", one * 0.8)] {
                let row = find(name);
                check(row.frames > 0 && row.p95 < p95, &format!("{name}: frame p95 {:.2} ms over {} frames (budget {p95:.1})", row.p95, row.frames));
                check(full_frames(&row) < TYPING_FULL_FRAMES, &format!("{name}: {:.0} ms/s, {:.1} full frames a second (budget {TYPING_FULL_FRAMES:.0})", row.cpu, full_frames(&row)));
            }
            let scrolling = find("same, scrolling");
            check(
                scrolling.frames > 0 && scrolling.p95 < one,
                &format!("scrolling: frame p95 {:.2} ms over {} frames (budget one full frame, {one:.2} ms)", scrolling.p95, scrolling.frames),
            );
            check(full_frames(&scrolling) < SCROLLING_FULL_FRAMES, &format!("scrolling: {:.0} ms/s, {:.1} full frames a second (budget {SCROLLING_FULL_FRAMES:.0})", scrolling.cpu, full_frames(&scrolling)));
            true
        })),
    ];
    run_stages(stages);
}
