//! The app's own startup clock: when the roster arrived, when the explorer
//! listed every agent and showed every portrait, when the first chat had
//! rows, and what each chat-list rebuild and each engine wake cost the UI
//! thread. The `startup` check reads it; `CLARP_PERF_LOG=1` prints each
//! rebuild and wake as it happens. Every frame drawn (headless, or the real
//! window's redraws) also lands in the minute's frame clock that the
//! `memory {...}` line reports.

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
    /// Per frame (as `frames`): the share of the window it repainted, 0 to 1.
    pub frame_dirty: Vec<f32>,
    /// Per frame: the rectangles it repainted (x, y, width, height; px).
    pub frame_rects: Vec<Vec<[u32; 4]>>,
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

/// A headless frame drawn since `started`, repainting `rects`, `dirty` of
/// the window.
pub fn drawn(started: Instant, dirty: f32, rects: Vec<[u32; 4]>) {
    let event = Event { at: now(), took: started.elapsed(), what: String::new() };
    frame_took(event.took);
    STATS.with(|s| {
        let mut stats = s.borrow_mut();
        stats.frames.push(event);
        stats.frame_dirty.push(dirty);
        stats.frame_rects.push(rects);
    });
}

/// The minute's frame times (ms), for the `memory {...}` line; bounded.
const MINUTE_FRAMES: usize = 50_000;

thread_local! {
    static MINUTE: RefCell<Vec<f32>> = const { RefCell::new(Vec::new()) };
    /// When the real window's current redraw started.
    static REDRAW: std::cell::Cell<Option<Instant>> = const { std::cell::Cell::new(None) };
}

fn frame_took(took: Duration) {
    MINUTE.with(|m| {
        let mut minute = m.borrow_mut();
        if minute.len() < MINUTE_FRAMES {
            minute.push(ms(took) as f32);
        }
    });
}

/// The window system asked the real window to redraw: the frame starts.
pub fn redraw_started() {
    REDRAW.with(|r| r.set(Some(Instant::now())));
}

/// The real window's redraw is over (the first thing the event loop runs
/// after it).
pub fn redraw_finished() {
    if let Some(started) = REDRAW.with(std::cell::Cell::take) {
        frame_took(started.elapsed());
    }
}

/// Frames drawn since the last call: how many, and their mean, 95th
/// percentile and longest time in ms.
#[derive(Debug, Default, Clone, Copy, PartialEq)]
pub struct FrameSummary {
    pub count: usize,
    pub mean: f32,
    pub p95: f32,
    pub max: f32,
}

pub fn take_frames() -> FrameSummary {
    summarize(MINUTE.with(|m| std::mem::take(&mut *m.borrow_mut())))
}

pub fn summarize(mut times: Vec<f32>) -> FrameSummary {
    if times.is_empty() {
        return FrameSummary::default();
    }
    times.sort_by(f32::total_cmp);
    let count = times.len();
    let p95 = times[((count as f32 * 0.95).ceil() as usize).clamp(1, count) - 1];
    FrameSummary { count, mean: times.iter().sum::<f32>() / count as f32, p95, max: times[count - 1] }
}

/// The calling thread's CPU time so far (user and system): the UI thread's,
/// called from it.
pub fn thread_cpu() -> Duration {
    // SAFETY: clock_gettime fills the zeroed timespec it is given.
    let mut time: libc::timespec = unsafe { std::mem::zeroed() };
    if unsafe { libc::clock_gettime(libc::CLOCK_THREAD_CPUTIME_ID, &mut time) } != 0 {
        eprintln!("perf: clock_gettime: {}", std::io::Error::last_os_error());
        return Duration::ZERO;
    }
    Duration::new(time.tv_sec as u64, time.tv_nsec as u32)
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

#[cfg(test)]
mod tests {
    use super::{FrameSummary, summarize};

    #[test]
    fn frame_summary_reads_mean_p95_and_longest() {
        assert_eq!(summarize(Vec::new()), FrameSummary::default());
        let times: Vec<f32> = (1..=100).map(|n| n as f32).collect();
        let summary = summarize(times);
        assert_eq!((summary.count, summary.mean, summary.p95, summary.max), (100, 50.5, 95.0, 100.0));
        assert_eq!(summarize(vec![4.0]).p95, 4.0);
    }
}
