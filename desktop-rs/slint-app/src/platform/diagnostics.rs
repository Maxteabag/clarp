//! What the desktop logs about itself (the Qt app's `Diagnostics`): moments
//! the UI thread was blocked (`stall_monitor`), and once a minute a
//! `memory {...}` line with what the process holds, so an out-of-memory
//! kill leaves a trend in the journal, and how the window drew since the
//! last line: the UI thread's CPU, frames per second and frame time (mean,
//! 95th percentile, longest; ms).
//!
//! `CLARP_STALL_THRESHOLD_MS` (150; 0 disables), `CLARP_STALL_MEMORY_MB`
//! (1536), `CLARP_STALL_LOG` and `CLARP_MEMORY_LOG_SECONDS` (60; 0
//! disables). Headless checks keep the watchdog off unless asked: it must
//! not signal the UI thread mid-check. Slint has no event-dispatcher wake
//! hook: the engine's wakes stand in for it (`awake`).

use std::cell::RefCell;
use std::time::Duration;

use super::stall_monitor::StallMonitor;

thread_local! {
    static MONITOR: RefCell<Option<StallMonitor>> = const { RefCell::new(None) };
    static CPU: RefCell<clarp_core::diagnostics::CpuRate> = RefCell::new(clarp_core::diagnostics::CpuRate::default());
    static TIMER: RefCell<Option<slint::Timer>> = const { RefCell::new(None) };
    /// When the last line's frames were counted, and the UI thread's CPU
    /// time then.
    static FRAMES_SINCE: std::cell::Cell<Option<(std::time::Instant, Duration)>> = const { std::cell::Cell::new(None) };
}

fn env_ms(name: &str, default: i64) -> i64 {
    match std::env::var(name) {
        Ok(value) => value.trim().parse().unwrap_or_else(|_| {
            eprintln!("Diagnostics: {name}={value} is not a number; using {default}");
            default
        }),
        Err(_) => default,
    }
}

/// Starts a second after launch, like the other deferred services.
pub fn start(headless: bool) {
    super::runtime::after(Duration::from_secs(1), move || {
        if let Err(error) = slint::invoke_from_event_loop(move || start_now(headless)) {
            eprintln!("Diagnostics: dropped the start: {error}");
        }
    });
}

fn start_now(headless: bool) {
    let threshold = env_ms("CLARP_STALL_THRESHOLD_MS", if headless { 0 } else { 150 });
    let memory_mb = env_ms("CLARP_STALL_MEMORY_MB", if headless { 0 } else { 1536 });
    let log = std::env::var_os("CLARP_STALL_LOG")
        .filter(|path| !path.is_empty())
        .map(std::path::PathBuf::from)
        .or_else(clarp_core::diagnostics::default_stall_log);
    if let Some(log) = log.filter(|_| threshold > 0) {
        let monitor = StallMonitor::start(threshold, memory_mb, log, |shared| {
            slint::invoke_from_event_loop(move || shared.beat()).is_ok()
        });
        MONITOR.with(|slot| *slot.borrow_mut() = Some(monitor));
    } else if threshold > 0 {
        eprintln!("Diagnostics: no stall log path (set CLARP_STALL_LOG or HOME)");
    }
    let seconds = env_ms("CLARP_MEMORY_LOG_SECONDS", 60);
    if seconds > 0 {
        FRAMES_SINCE.with(|f| f.set(Some((std::time::Instant::now(), crate::perf::thread_cpu()))));
        let timer = slint::Timer::default();
        timer.start(slint::TimerMode::Repeated, Duration::from_secs(seconds as u64), log_memory);
        TIMER.with(|slot| *slot.borrow_mut() = Some(timer));
    }
}

/// The UI thread woke to work (an engine wake): a block is measured from
/// here rather than from the last heartbeat.
pub fn awake() {
    MONITOR.with(|slot| {
        if let Some(monitor) = slot.borrow().as_ref() {
            monitor.shared().awake();
        }
    });
}

/// The minute line: the kernel's RSS split, what the window holds, CPU and
/// stalls.
pub fn log_memory() {
    use serde_json::{Map, Value, json};
    let mut report = Map::new();
    if let Ok(status) = std::fs::read_to_string("/proc/self/status") {
        for (key, kb) in clarp_core::diagnostics::memory_kb(&status) {
            report.insert(key.into(), json!(kb));
        }
    }
    if let Some(app) = crate::app() {
        let (panes, rows) = app.memory_counters();
        report.insert("panes".into(), json!(panes));
        report.insert("transcriptRows".into(), json!(rows));
        let engine = app.engine.borrow();
        report.insert("agents".into(), json!(engine.roster().agents().len()));
    }
    let cpu = std::fs::read_to_string("/proc/self/stat").ok().and_then(|stat| clarp_core::diagnostics::cpu_ticks(&stat));
    // SAFETY: sysconf has no preconditions.
    let hz = unsafe { libc::sysconf(libc::_SC_CLK_TCK) };
    let now = chrono::Utc::now().timestamp_millis();
    let rate = cpu.map_or(0, |ticks| CPU.with(|c| c.borrow_mut().sample(ticks, now, hz)));
    report.insert("cpuMsPerSec".into(), json!(rate));
    let frames = crate::perf::take_frames();
    let ui_cpu = crate::perf::thread_cpu();
    let since = FRAMES_SINCE.with(|f| f.replace(Some((std::time::Instant::now(), ui_cpu))));
    let seconds = since.map_or(0.0, |(at, _)| at.elapsed().as_secs_f64().max(0.001));
    let round = |value: f64| (value * 10.0).round() / 10.0;
    // The UI thread alone (the process's figure includes the network).
    report.insert("uiCpuMsPerSec".into(), json!(since.map_or(0.0, |(_, cpu)| round(ui_cpu.saturating_sub(cpu).as_secs_f64() * 1000.0 / seconds))));
    report.insert("fps".into(), json!(if seconds > 0.0 { round(frames.count as f64 / seconds) } else { 0.0 }));
    report.insert("frameMs".into(), json!(round(f64::from(frames.mean))));
    report.insert("frameP95Ms".into(), json!(round(f64::from(frames.p95))));
    report.insert("frameMaxMs".into(), json!(round(f64::from(frames.max))));
    let (stalls, longest) = MONITOR.with(|slot| slot.borrow().as_ref().map_or((0, 0), |m| m.shared().take_counters()));
    report.insert("stalls".into(), json!(stalls));
    report.insert("longestStallMs".into(), json!(longest));
    eprintln!("memory {}", Value::Object(report));
}
