//! The scroll journal: one line on stderr (so in /var/tmp/clarp-slint.log)
//! for every move of a transcript's offset that was not the reader's own
//! wheel or touchpad, with its cause, and for every jump of over a viewport
//! nobody asked for. "The chat jumped far up" can then be traced to what
//! moved it. Following the latest is silent unless it moves the reader up;
//! each pane and cause logs at most a few lines per ten seconds.

use std::collections::HashMap;
use std::time::{Duration, Instant};

/// One move, as the transcript reports it.
#[derive(Debug, Clone, PartialEq)]
pub struct Move {
    pub pane: String,
    /// follow, jump-latest, seek, load-older, key:<key>, unexplained, or a
    /// list reset (reset:<why>, from the app).
    pub cause: String,
    pub old: f32,
    pub new: f32,
    /// Where the offset was and is in the chat: 0 the top, 1 the end.
    pub old_place: f32,
    pub new_place: f32,
    pub content: f32,
    pub viewport: f32,
    pub at_end: bool,
    pub follow: bool,
    pub rows: i32,
}

impl Move {
    /// How many viewports up (positive) or down the reader was carried.
    fn viewports_up(&self) -> f32 {
        let range = (self.content - self.viewport).max(0.0);
        (self.old_place - self.new_place) * range / self.viewport.max(1.0)
    }

    /// Following pins the end on every streamed update: only a pin that
    /// carried the reader up (which it never should) is worth a line.
    fn wanted(&self) -> bool {
        self.cause != "follow" || self.viewports_up() > 1.0
    }

    fn line(&self, clock: &str, suppressed: u32) -> String {
        let row = |place: f32| (place.clamp(0.0, 1.0) * (self.rows - 1).max(0) as f32).round() as i32;
        let up = self.viewports_up();
        let more = if suppressed > 0 { format!(" ({suppressed} more like it not logged)") } else { String::new() };
        format!(
            "scroll {clock} pane={} cause={} offset {:.0} -> {:.0} place {:.3} -> {:.3} (row ~{} -> ~{} of {}; {:.1} viewports {}) content {:.0} viewport {:.0} at-end {} follow {}{more}",
            self.pane,
            self.cause,
            self.old,
            self.new,
            self.old_place,
            self.new_place,
            row(self.old_place),
            row(self.new_place),
            self.rows,
            up.abs(),
            if up >= 0.0 { "up" } else { "down" },
            self.content,
            self.viewport,
            self.at_end,
            self.follow,
        )
    }
}

const WINDOW: Duration = Duration::from_secs(10);
const LINES_PER_WINDOW: u32 = 6;

/// The rate limit: per pane and cause, lines in the current window and
/// those left out.
#[derive(Default)]
pub struct Journal {
    windows: HashMap<(String, String), (Instant, u32, u32)>,
}

impl Journal {
    /// The line to log for `entry` at `now`, if any.
    pub fn record(&mut self, entry: &Move, now: Instant, clock: &str) -> Option<String> {
        if !entry.wanted() {
            return None;
        }
        let window = self.windows.entry((entry.pane.clone(), entry.cause.clone())).or_insert((now, 0, 0));
        if now.duration_since(window.0) >= WINDOW {
            window.0 = now;
            window.1 = 0;
        }
        if window.1 >= LINES_PER_WINDOW {
            window.2 += 1;
            return None;
        }
        window.1 += 1;
        let suppressed = std::mem::take(&mut window.2);
        Some(entry.line(clock, suppressed))
    }
}

thread_local! {
    static JOURNAL: std::cell::RefCell<Journal> = std::cell::RefCell::default();
    /// The causes of the lines logged, oldest first (for the checks).
    static LOGGED: std::cell::RefCell<Vec<String>> = const { std::cell::RefCell::new(Vec::new()) };
}

/// Logs `entry` (rate-limited).
pub fn record(entry: Move) {
    let clock = chrono::Local::now().format("%Y-%m-%dT%H:%M:%S%.3f").to_string();
    if let Some(line) = JOURNAL.with(|j| j.borrow_mut().record(&entry, Instant::now(), &clock)) {
        eprintln!("{line}");
        LOGGED.with(|l| l.borrow_mut().push(entry.cause));
    }
}

/// The causes of every line logged so far.
pub fn logged() -> Vec<String> {
    LOGGED.with(|l| l.borrow().clone())
}

/// The app emptied a pane's transcript to fill it again (`why`: another
/// chat, or a view setting that changes every row): the list starts over
/// at its top, so the lines after this one say where it went.
pub fn reset(pane: &str, why: &str, rows: usize) {
    record(Move {
        pane: pane.to_owned(),
        cause: format!("reset:{why}"),
        old: 0.0,
        new: 0.0,
        old_place: 1.0,
        new_place: 0.0,
        content: 0.0,
        viewport: 0.0,
        at_end: false,
        follow: false,
        rows: rows as i32,
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    fn moved(cause: &str, old_place: f32, new_place: f32) -> Move {
        Move {
            pane: "pane-1".into(),
            cause: cause.into(),
            old: -9000.0,
            new: -1000.0,
            old_place,
            new_place,
            content: 10_480.0,
            viewport: 480.0,
            at_end: false,
            follow: false,
            rows: 321,
        }
    }

    #[test]
    fn following_is_silent_unless_it_moves_the_reader_up() {
        let mut journal = Journal::default();
        let now = Instant::now();
        assert_eq!(journal.record(&moved("follow", 0.98, 1.0), now, "t"), None);
        let up = journal.record(&moved("follow", 1.0, 0.5), now, "t").expect("a pin that carried the reader up is logged");
        assert!(up.contains("cause=follow"), "{up}");
        assert!(up.contains("10.4 viewports up"), "{up}");
    }

    #[test]
    fn a_line_says_the_cause_offsets_place_and_rows() {
        let line = Journal::default().record(&moved("key:Home", 1.0, 0.0), Instant::now(), "12:00:00.000").expect("logged");
        assert_eq!(
            line,
            "scroll 12:00:00.000 pane=pane-1 cause=key:Home offset -9000 -> -1000 place 1.000 -> 0.000 (row ~320 -> ~0 of 321; 20.8 viewports up) content 10480 viewport 480 at-end false follow false"
        );
    }

    #[test]
    fn each_pane_and_cause_logs_a_few_lines_per_window_then_counts_the_rest() {
        let mut journal = Journal::default();
        let start = Instant::now();
        let logged = (0..10).filter(|_| journal.record(&moved("seek", 1.0, 0.9), start, "t").is_some()).count();
        assert_eq!(logged, LINES_PER_WINDOW as usize);
        assert!(journal.record(&moved("unexplained", 1.0, 0.0), start, "t").is_some(), "another cause has its own window");
        let next = journal.record(&moved("seek", 1.0, 0.9), start + WINDOW, "t").expect("a new window logs again");
        assert!(next.ends_with("(4 more like it not logged)"), "{next}");
    }
}
