//! The app's own startup clock: when the roster arrived, when the explorer
//! listed every agent and showed every portrait, when the first chat had
//! rows, and what each chat-list rebuild and each engine wake cost the UI
//! thread. The `startup` check reads it; `CLARP_PERF_LOG=1` prints each
//! rebuild and wake as it happens.

use std::cell::RefCell;
use std::sync::OnceLock;
use std::time::{Duration, Instant};

static LAUNCHED: OnceLock<Instant> = OnceLock::new();

/// Starts the clock (first thing in `main`).
pub fn launched() {
    LAUNCHED.get_or_init(Instant::now);
}

/// Time since launch.
pub fn now() -> Duration {
    LAUNCHED.get_or_init(Instant::now).elapsed()
}

#[derive(Debug, Clone)]
pub struct Event {
    pub at: Duration,
    pub took: Duration,
    pub what: String,
}

#[derive(Debug, Default, Clone)]
pub struct Stats {
    pub snapshot: Option<Duration>,
    pub explorer_complete: Option<Duration>,
    pub portraits_complete: Option<Duration>,
    pub first_chat: Option<Duration>,
    /// Chat-list rebuilds: what asked for them and their rows.
    pub rebuilds: Vec<Event>,
    /// Engine wakes and commands: the UI thread's work per `pump`.
    pub wakes: Vec<Event>,
    /// Frames the (headless) software renderer drew.
    pub frames: Vec<Event>,
}

thread_local! {
    static STATS: RefCell<Stats> = RefCell::new(Stats::default());
}

fn logging() -> bool {
    static ON: OnceLock<bool> = OnceLock::new();
    *ON.get_or_init(|| std::env::var("CLARP_PERF_LOG").is_ok_and(|v| !v.is_empty() && v != "0"))
}

pub fn stats() -> Stats {
    STATS.with(|s| s.borrow().clone())
}

pub fn rebuilt(started: Instant, what: String) {
    let event = Event { at: now(), took: started.elapsed(), what };
    if logging() {
        eprintln!("perf: rebuild {:.2} ms at {} ms ({})", ms(event.took), event.at.as_millis(), event.what);
    }
    STATS.with(|s| s.borrow_mut().rebuilds.push(event));
}

pub fn woke(started: Instant, what: String) {
    let event = Event { at: now(), took: started.elapsed(), what };
    if logging() {
        eprintln!("perf: wake {:.2} ms at {} ms ({})", ms(event.took), event.at.as_millis(), event.what);
    }
    STATS.with(|s| s.borrow_mut().wakes.push(event));
}

pub fn drawn(started: Instant) {
    let event = Event { at: now(), took: started.elapsed(), what: String::new() };
    STATS.with(|s| s.borrow_mut().frames.push(event));
}

/// Marks a milestone the first time it is reached.
pub fn reached(milestone: fn(&mut Stats) -> &mut Option<Duration>, name: &str) {
    STATS.with(|s| {
        let mut stats = s.borrow_mut();
        let slot = milestone(&mut stats);
        if slot.is_none() {
            *slot = Some(now());
            if logging() {
                eprintln!("perf: {name} at {} ms", now().as_millis());
            }
        }
    });
}

pub fn ms(duration: Duration) -> f64 {
    duration.as_secs_f64() * 1000.0
}

/// Splits one piece of work into timed laps for `CLARP_PERF_LOG`: `lap`
/// after each part, `done` prints them on one line.
pub struct Laps {
    last: Instant,
    laps: Vec<(&'static str, Duration)>,
}

impl Laps {
    pub fn new() -> Self {
        Self { last: Instant::now(), laps: Vec::new() }
    }

    pub fn lap(&mut self, name: &'static str) {
        if logging() {
            self.laps.push((name, self.last.elapsed()));
            self.last = Instant::now();
        }
    }

    pub fn done(self, what: &str) {
        if logging() {
            let parts: Vec<String> = self.laps.iter().map(|(name, took)| format!("{name} {:.2}", ms(*took))).collect();
            eprintln!("perf: {what}: {}", parts.join(", "));
        }
    }
}
