//! `--check frame-budget`: what animating costs the UI thread, on a laptop's
//! panel (CLARP_HEADLESS_SIZE, set by check.sh) with frames paced at 60 Hz
//! and repainted partially, as the real window's softbuffer does. The fake
//! Host serves 180 agents; two panes show a 250-row chat. Each scenario
//! runs for a few seconds with nothing else asking for frames and reports
//! the UI thread's CPU (ms per second), frames per second, frame time
//! (mean, 95th percentile) and the share of the window each frame
//! repainted: an idle fleet, 16 agents working and 30 background jobs with
//! the running row shimmering (Reduce Motion off and on), scrolling and
//! typing over it, and the same fleet drawn whole every frame. Then one
//! full frame's cost. The table goes to `budget.md` beside the shots.
//!
//! The budget: the busy fleet under 150 ms/s, and scrolling and typing
//! over it under 16 ms a frame (95th percentile).

use std::cell::RefCell;
use std::rc::Rc;
use std::time::{Duration, Instant};

use serde_json::json;
use slint::ComponentHandle;
use slint::platform::Key;

use super::{Stage, check, control, quiet, report, run_stages};
use crate::headless::{self, Pacing};

/// Busy CPU budget (ms of UI thread per second) and frame budget (ms, p95).
const CPU_BUDGET: f64 = 150.0;
const FRAME_BUDGET: f64 = 16.0;
/// How long a scenario settles, then how long it is measured.
const SETTLE: Duration = Duration::from_millis(1500);
const MEASURE: Duration = Duration::from_secs(5);

const PARTIAL_60HZ: Pacing = Pacing { partial: true, hz: 60 };
const WHOLE_60HZ: Pacing = Pacing { partial: false, hz: 60 };

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
}

fn mark() -> Mark {
    quiet(true);
    Mark { cpu: crate::perf::thread_cpu(), frames: crate::perf::stats().frames.len(), at: Instant::now() }
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
        cpu: (crate::perf::thread_cpu() - mark.cpu).as_secs_f64() * 1000.0 / seconds,
        fps: summary.count as f64 / seconds,
        mean: f64::from(summary.mean),
        p95: f64::from(summary.p95),
        dirty: if dirty.is_empty() { 0.0 } else { dirty.iter().map(|d| f64::from(*d)).sum::<f64>() / dirty.len() as f64 },
        frames: summary.count,
    };
    println!(
        "perf budget {}: UI thread {:.0} ms/s, {:.1} fps, frame mean {:.2} ms, p95 {:.2} ms, repaints {:.0}% of the window ({} frames)",
        row.name, row.cpu, row.fps, row.mean, row.p95, row.dirty * 100.0, row.frames
    );
    row
}

thread_local! {
    /// Input sent while a scenario is measured (wheel turns, keys).
    static INPUT: RefCell<Option<slint::Timer>> = const { RefCell::new(None) };
}

fn input_every(interval: Duration, mut send: impl FnMut() + 'static) {
    let timer = slint::Timer::default();
    timer.start(slint::TimerMode::Repeated, interval, move || send());
    INPUT.with(|i| *i.borrow_mut() = Some(timer));
}

fn stop_input() {
    INPUT.with(|i| i.borrow_mut().take());
}

/// The active pane's chat: its box in the window.
fn chat_box() -> Option<(f32, f32, f32, f32)> {
    use i_slint_backend_testing::ElementQuery;
    let window = crate::window()?;
    ElementQuery::from_root(&window)
        .match_predicate(|e| e.accessible_id().is_some_and(|id| id.starts_with("chat:")) && e.size().width > 0.0)
        .find_all()
        .into_iter()
        .map(|e| (e.absolute_position().x, e.absolute_position().y, e.size().width, e.size().height))
        .next()
}

fn any_shimmering() -> bool {
    use i_slint_backend_testing::ElementQuery;
    let Some(window) = crate::window() else { return false };
    ElementQuery::from_root(&window)
        .match_predicate(|e| e.accessible_id().is_some_and(|a| a.starts_with("shimmer:")) && e.size().width > 0.0)
        .find_first()
        .is_some()
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

/// A measured scenario as a stage: `setup` once, settle, measure, then
/// `done` (after the input stops).
fn scenario(
    name: &'static str,
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
        true
    }))
}

pub fn frame_budget_check(out: String) {
    let rows: Rc<RefCell<Vec<Row>>> = Rc::default();
    let full: Rc<RefCell<Vec<f64>>> = Rc::default();
    let (rows1, rows2, rows3, rows4, rows5, rows6, rows7) =
        (rows.clone(), rows.clone(), rows.clone(), rows.clone(), rows.clone(), rows.clone(), rows.clone());
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
            let sent = control("/__control/rich", &json!({"session": "rachel", "count": 250}))
                .and_then(|()| control("/__control/activity", &json!({"working": 0, "jobs": 0})))
                .and_then(|()| control("/__control/live", &json!({"on": true})));
            check(sent.is_ok(), &format!("the Host serves 180 agents and a 250-row chat, all idle: {sent:?}"));
            app.engine.borrow_mut().reconnect();
            true
        })),
        ("two panes", Box::new(|app, _, elapsed| {
            let ready = app.engine.borrow().panes().pane_count() == 2
                && app.engine.borrow().conversation("rachel").is_some_and(|c| c.len() >= 250)
                && app.engine.borrow().live_items()
                && busy_agents(app) == 0;
            if !ready || elapsed < Duration::from_secs(2) {
                return false;
            }
            let (width, height, scale) = headless::size_from_env();
            check(true, &format!("two panes over the 250-row chat, nothing working, {width}x{height} at {scale}"));
            headless::pace(PARTIAL_60HZ);
            true
        })),
        scenario("idle, 0 working", rows1, |_, _| {}),
        ("busy fleet", Box::new({
            let mut sent = false;
            move |app: &crate::App, _: &crate::AppWindow, elapsed: Duration| {
                if !sent {
                    sent = true;
                    let started = control("/__control/activity", &json!({"working": 16, "jobs": 30}))
                        .and_then(|()| control("/__control/live-replay", &json!({"session": "rachel", "fixture": "turn-full", "through": 14})));
                    check(started.is_ok(), &format!("16 agents start working, 30 run a background job, Rachel runs a command: {started:?}"));
                    return false;
                }
                if busy_agents(app) < 16 || !any_shimmering() {
                    if elapsed.as_millis() / 100 == 120 {
                        println!("perf budget waiting: {} agents busy, shimmering {}", busy_agents(app), any_shimmering());
                    }
                    return false;
                }
                let agents = app.engine.borrow().roster().agents().len();
                check(true, &format!("{} of {agents} agents busy; the running row shimmers", busy_agents(app)));
                true
            }
        })),
        scenario("16 working + 30 jobs, shimmer", rows2, |_, _| {}),
        scenario("same, Reduce Motion", rows3, |_, _| super::live_checks::set_reduced_motion(true)),
        scenario("same, scrolling", rows4, |_, _| {
            super::live_checks::set_reduced_motion(false);
            let mut up = true;
            let mut turns = 0;
            input_every(Duration::from_millis(33), move || {
                if let Some((x, y, width, height)) = chat_box() {
                    headless::wheel(x + width / 2.0, y + height / 2.0, if up { 60.0 } else { -60.0 });
                }
                turns += 1;
                if turns % 15 == 0 {
                    up = !up;
                }
            });
        }),
        scenario("same, typing", rows5, |_, _| {
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
        }),
        scenario("same, every frame whole", rows6, |_, _| headless::pace(WHOLE_60HZ)),
        ("full frame", Box::new({
            // Ten frames drawn whole, one at a time: each the first frame
            // after its request.
            let mut waiting: Option<usize> = None;
            move |_: &crate::App, _: &crate::AppWindow, _: Duration| {
                let frames = crate::perf::stats().frames;
                match waiting {
                    None => headless::pace(PARTIAL_60HZ),
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
                "Frame budget: {width}x{height} logical at {scale} ({}x{} px), 60 Hz pacing, partial repaint; one full frame {one:.2} ms\n\n\
                 | scenario | UI thread ms/s | fps | frame mean ms | frame p95 ms | repainted |\n|---|---|---|---|---|---|\n",
                size.width, size.height
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
            let find = |name: &str| rows.iter().find(|r| r.name == name).cloned().unwrap_or_default();
            let busy = find("16 working + 30 jobs, shimmer");
            check(busy.cpu < CPU_BUDGET, &format!("the busy fleet costs the UI thread {:.0} ms/s (budget {CPU_BUDGET:.0})", busy.cpu));
            for name in ["same, scrolling", "same, typing"] {
                let row = find(name);
                check(row.frames > 0 && row.p95 < FRAME_BUDGET, &format!("{name}: frame p95 {:.2} ms over {} frames (budget {FRAME_BUDGET:.0})", row.p95, row.frames));
            }
            true
        })),
    ];
    run_stages(stages);
}
