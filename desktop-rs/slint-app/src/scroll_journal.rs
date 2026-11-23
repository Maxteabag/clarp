//! The scroll journal: one line on stderr (so in /var/tmp/clarp-slint.log)
//! for every move of a transcript's offset that was not the reader's own
//! wheel or touchpad, with its cause, and for every jump of over a viewport
//! nobody asked for. "The chat jumped far up" can then be traced to what
//! moved it. Following the latest is silent unless it moves the reader up;
//! each pane and cause logs at most a few lines per ten seconds.
//!
//! The dashcam: each pane keeps what its last three seconds of drawn
//! frames showed (offset, heights, place, the row across the top, follow,
//! row count) and what happened before each (rows in or out, a height,
//! a live tick, a reload, a re-estimate, a pin, the reader's own scroll).
//! A flash (the chat drawn a viewport or more away, or with another row
//! across its top, and back within three frames, nobody scrolling) writes
//! those frames here and posts them to the primary Host's /clog as one
//! `scroll_flash` event, at most once per pane per ten seconds.

use std::collections::{HashMap, VecDeque};
use std::time::{Duration, Instant};

use serde_json::{Value, json};

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
        // The reader's own scroll is told to the dashcam only.
        self.cause != "reader" && (self.cause != "follow" || self.viewports_up() > 1.0)
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

/// Logs `entry` (rate-limited); the dashcam hears of every move.
pub fn record(entry: Move) {
    event(&entry.pane, &entry.cause);
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
    event(pane, &format!("reset:{why}"));
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

// ---- The dashcam ----

/// What a transcript showed: its offset, content and viewport heights, the
/// row across its top (id and index), whether it followed the latest and
/// its row count.
#[derive(Debug, Clone, PartialEq, Default)]
pub struct Shot {
    pub offset: f32,
    pub content: f32,
    pub viewport: f32,
    pub top: String,
    pub top_index: i32,
    pub follow: bool,
    pub rows: i32,
}

impl Shot {
    fn range(&self) -> f32 {
        (self.content - self.viewport).max(0.0)
    }

    /// Where the offset is in the chat: 0 the top, 1 the end (past 1: blank
    /// below the last row).
    pub fn place(&self) -> f32 {
        if self.content > self.viewport { -self.offset / self.range() } else { 1.0 }
    }

    /// How far apart two pictures of the chat are, in px of the chat: the
    /// places' distance over the larger range, so a re-estimate of the rows'
    /// heights (the offset moves, the place holds) is not a move.
    fn distance(&self, other: &Shot) -> f32 {
        (self.place() - other.place()).abs() * self.range().max(other.range())
    }
}

/// One drawn frame: when (since launch), what it showed, and what happened
/// since the frame before.
#[derive(Debug, Clone, PartialEq)]
pub struct Frame {
    pub at: Duration,
    pub shot: Shot,
    pub events: Vec<String>,
}

/// How long each pane's frames are kept, and at most how many.
const KEEP: Duration = Duration::from_secs(3);
const MOST_FRAMES: usize = 400;
/// A jump that comes back within this many frames is a flash.
const BACK_WITHIN: usize = 3;
/// At most one flash per pane is recorded and posted in this time.
const FLASH_EVERY: Duration = Duration::from_secs(10);
/// What happened before one frame: at most this many things.
const EVENTS_PER_FRAME: usize = 12;

/// A flash: the frame drawn away (`jumped`) and the one back (`back`), by
/// when they were drawn, how it showed (`offset`: a viewport or more away;
/// `top-row`: another row across the top) and how many viewports away.
#[derive(Debug, Clone, PartialEq)]
pub struct Flash {
    pub kind: &'static str,
    pub jumped: Duration,
    pub back: Duration,
    pub frames_away: usize,
    pub viewports: f32,
}

/// A move someone asked for (the reader's scroll and keys, End, a search,
/// a seek, an older page, another chat): never a flash.
fn asked(event: &str) -> bool {
    ["reader", "key:", "vim:", "jump-latest", "seek", "reveal", "load-older", "reset:"].iter().any(|p| event.starts_with(p))
}

/// The flash that `frames`' newest frame ends, if any: the chat was drawn a
/// viewport or more from where it was (or with a row two or more away
/// across its top) and the newest frame is the first back, within
/// `BACK_WITHIN` frames, with no move anyone asked for in between.
pub fn detect(frames: &[Frame]) -> Option<Flash> {
    let back = frames.len().checked_sub(1)?;
    let now = &frames[back].shot;
    for jumped in back.saturating_sub(BACK_WITHIN).max(1)..back {
        let before = &frames[jumped - 1].shot;
        if frames[jumped - 1..=back].iter().any(|f| f.events.iter().any(|e| asked(e))) {
            continue;
        }
        let away = &frames[jumped..back];
        let viewport = before.viewport.max(1.0);
        let offset_flash = before.distance(&frames[jumped].shot) > viewport
            && away.iter().all(|f| before.distance(&f.shot) > viewport / 2.0)
            && before.distance(now) <= viewport / 2.0;
        let top_flash = !before.top.is_empty()
            && now.top == before.top
            && away.iter().all(|f| !f.shot.top.is_empty() && f.shot.top != before.top && (f.shot.top_index - before.top_index).abs() >= 2);
        if offset_flash || top_flash {
            let farthest = away.iter().map(|f| before.distance(&f.shot)).fold(0.0, f32::max);
            return Some(Flash {
                kind: if offset_flash { "offset" } else { "top-row" },
                jumped: frames[jumped].at,
                back: frames[back].at,
                frames_away: back - jumped,
                viewports: farthest / viewport,
            });
        }
    }
    None
}

#[derive(Default)]
struct Ring {
    /// What the transcript would draw now, and what happened since the
    /// last frame.
    latest: Option<Shot>,
    events: Vec<String>,
    frames: VecDeque<Frame>,
    recorded: Option<Instant>,
    /// Flashes since the last one recorded that were not.
    skipped: u32,
}

/// The panes' rings.
#[derive(Default)]
pub struct Dashcam {
    rings: HashMap<String, Ring>,
}

impl Dashcam {
    pub fn shot(&mut self, pane: &str, shot: Shot) {
        self.rings.entry(pane.to_owned()).or_default().latest = Some(shot);
    }

    pub fn event(&mut self, pane: &str, what: &str) {
        let ring = self.rings.entry(pane.to_owned()).or_default();
        if ring.events.len() < EVENTS_PER_FRAME && ring.events.last().is_none_or(|last| last != what) {
            ring.events.push(what.to_owned());
        }
    }

    /// A frame was drawn at `at`: each pane whose picture changed (or that
    /// something happened to) keeps it. The flashes it ended, by pane, that
    /// are due to be recorded at `now`.
    pub fn drawn(&mut self, at: Duration, now: Instant) -> Vec<(String, Flash, u32)> {
        let mut found = Vec::new();
        for (pane, ring) in &mut self.rings {
            let Some(shot) = ring.latest.clone() else { continue };
            let last = ring.frames.back();
            if last.is_some_and(|f| f.shot == shot) && ring.events.is_empty() {
                continue;
            }
            let mut events = std::mem::take(&mut ring.events);
            // The list re-estimated its rows' heights: the content height
            // changed with nothing drawn or added that would change it.
            if let Some(last) = last
                && (last.shot.content - shot.content).abs() >= 0.5
                && last.shot.rows == shot.rows
                && !events.iter().any(|e| e.starts_with("rows") || e.starts_with("height") || e.starts_with("live"))
            {
                events.push("estimate".into());
            }
            ring.frames.push_back(Frame { at, shot, events });
            while ring.frames.len() > MOST_FRAMES || ring.frames.front().is_some_and(|f| at.saturating_sub(f.at) > KEEP) {
                ring.frames.pop_front();
            }
            let frames = ring.frames.make_contiguous();
            let Some(flash) = detect(&frames[frames.len().saturating_sub(BACK_WITHIN + 2)..]) else { continue };
            if ring.recorded.is_some_and(|at| now.duration_since(at) < FLASH_EVERY) {
                ring.skipped += 1;
                continue;
            }
            ring.recorded = Some(now);
            found.push((pane.clone(), flash, std::mem::take(&mut ring.skipped)));
        }
        found
    }

    /// A pane's frames now (the flash's, and those drawn since).
    pub fn frames(&self, pane: &str) -> Vec<Frame> {
        self.rings.get(pane).map(|r| r.frames.iter().cloned().collect()).unwrap_or_default()
    }

}

/// The local journal's lines for a flash: the flash, then each frame.
pub fn flash_lines(pane: &str, flash: &Flash, frames: &[Frame], skipped: u32, clock: &str) -> Vec<String> {
    let more = if skipped > 0 { format!(" ({skipped} more not recorded)") } else { String::new() };
    let mut lines = vec![format!(
        "scroll-flash {clock} pane={pane} kind={} {:.1} viewports away for {} frame(s), no one scrolling; {} frames follow{more}",
        flash.kind,
        flash.viewports,
        flash.frames_away,
        frames.len()
    )];
    for frame in frames {
        let ms = frame.at.as_secs_f64() * 1000.0 - flash.jumped.as_secs_f64() * 1000.0;
        let mark = if frame.at == flash.jumped { " <- away" } else if frame.at == flash.back { " <- back" } else { "" };
        let s = &frame.shot;
        lines.push(format!(
            "scroll-frame {ms:+.0}ms offset {:.0} content {:.0} viewport {:.0} place {:.3} top #{} {} follow {} rows {} [{}]{mark}",
            s.offset,
            s.content,
            s.viewport,
            s.place(),
            s.top_index,
            s.top,
            s.follow,
            s.rows,
            frame.events.join(", ")
        ));
    }
    lines
}

/// The /clog event's detail: the flash, and as many of its frames (the
/// newest; always those from the one before the jump) as fit the Host's
/// 4 KB detail. Each frame is [ms from the jump, offset, content, viewport,
/// place × 1000, top row index, follow 0/1, rows, top row (an index into
/// `tops`), "what happened"].
pub fn flash_detail(pane: &str, flash: &Flash, frames: &[Frame], skipped: u32) -> Value {
    const MOST: usize = 3800;
    let first_needed = frames.iter().position(|f| f.at >= flash.jumped).unwrap_or(0).saturating_sub(1);
    let build = |from: usize, with_events: bool| {
        let shown = &frames[from..];
        let mut tops: Vec<String> = Vec::new();
        let rows: Vec<Value> = shown
            .iter()
            .map(|f| {
                let s = &f.shot;
                let id: String = s.top.chars().take(24).collect();
                let top = tops.iter().position(|t| *t == id).unwrap_or_else(|| {
                    tops.push(id);
                    tops.len() - 1
                });
                let ms = (f.at.as_secs_f64() - flash.jumped.as_secs_f64()) * 1000.0;
                let what: String = if with_events { f.events.join(",").chars().take(60).collect() } else { String::new() };
                json!([ms.round() as i64, s.offset.round() as i64, s.content.round() as i64, s.viewport.round() as i64, (s.place() * 1000.0).round() as i64, s.top_index, u8::from(s.follow), s.rows, top, what])
            })
            .collect();
        json!({
            "v": 1,
            "pane": pane,
            "kind": flash.kind,
            "viewports": (flash.viewports * 10.0).round() / 10.0,
            "frames_away": flash.frames_away,
            "skipped": skipped,
            "dropped": from,
            "fields": "ms,offset,content,viewport,place_milli,top_index,follow,rows,top,events",
            "frames": rows,
            "tops": tops,
        })
    };
    for from in 0..=first_needed {
        let detail = build(from, true);
        if detail.to_string().len() <= MOST {
            return detail;
        }
    }
    build(first_needed, false)
}

thread_local! {
    static DASHCAM: std::cell::RefCell<Dashcam> = std::cell::RefCell::default();
    /// The flash events posted (or that would have been, with no Host), for
    /// the checks.
    static FLASHES: std::cell::RefCell<Vec<Value>> = const { std::cell::RefCell::new(Vec::new()) };
}

/// What a pane's transcript would draw now.
pub fn shot(pane: &str, shot: Shot) {
    DASHCAM.with(|d| d.borrow_mut().shot(pane, shot));
}

/// Something happened to a pane's transcript before its next frame.
pub fn event(pane: &str, what: &str) {
    DASHCAM.with(|d| d.borrow_mut().event(pane, what));
}

/// A frame is drawn (headless: was drawn; the real window: is about to be,
/// with everything that will be drawn settled). Cheap: a flash found is
/// written and posted a moment later, off the frame.
pub fn frame_drawn() {
    let found = DASHCAM.with(|d| d.borrow_mut().drawn(crate::perf::now(), Instant::now()));
    for (pane, flash, skipped) in found {
        // A moment later: the frames after the flash come along.
        slint::Timer::single_shot(Duration::from_millis(300), move || {
            let frames = DASHCAM.with(|d| d.borrow().frames(&pane));
            let clock = chrono::Local::now().format("%Y-%m-%dT%H:%M:%S%.3f").to_string();
            for line in flash_lines(&pane, &flash, &frames, skipped, &clock) {
                eprintln!("{line}");
            }
            let detail = flash_detail(&pane, &flash, &frames, skipped);
            if let Some(app) = crate::app() {
                app.engine.borrow().report_client_event("scroll_flash", "fault", detail.clone());
            }
            FLASHES.with(|f| f.borrow_mut().push(detail));
        });
    }
}

/// The flash events recorded so far (for the checks).
pub fn flashes() -> Vec<Value> {
    FLASHES.with(|f| f.borrow().clone())
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

    fn at_end(rows: i32, content: f32) -> Shot {
        Shot { offset: 480.0 - content, content, viewport: 480.0, top: format!("row-{}", rows - 4), top_index: rows - 4, follow: true, rows }
    }

    fn frames(shots: &[(Shot, &[&str])]) -> Vec<Frame> {
        shots
            .iter()
            .enumerate()
            .map(|(i, (shot, events))| Frame { at: Duration::from_millis(16 * i as u64), shot: shot.clone(), events: events.iter().map(|e| (*e).to_owned()).collect() })
            .collect()
    }

    #[test]
    fn a_jump_up_that_comes_back_is_a_flash() {
        let end = at_end(78, 8421.0);
        let up = Shot { offset: -3887.0, top: "row-39".into(), top_index: 39, ..end.clone() };
        let shots = [(end.clone(), &[][..]), (up.clone(), &["rows -1 +1"][..]), (up, &[][..]), (end, &["follow"][..])];
        let found = frames(&shots);
        assert_eq!(detect(&found[..3]), None, "still away is not a flash yet");
        let flash = detect(&found).expect("back within three frames");
        assert_eq!(flash.kind, "offset");
        assert_eq!(flash.frames_away, 2);
        assert_eq!(flash.jumped, Duration::from_millis(16));
        assert!((flash.viewports - 4054.0 / 480.0).abs() < 0.01, "{flash:?}");
    }

    #[test]
    fn blank_space_past_the_end_that_comes_back_is_a_flash() {
        let end = at_end(93, 6095.0);
        let past = Shot { offset: -6115.0 - 700.0, ..end.clone() };
        let flash = detect(&frames(&[(end.clone(), &[][..]), (past, &[][..]), (end, &[][..])])).expect("overshoot and back");
        assert_eq!(flash.kind, "offset");
    }

    #[test]
    fn the_readers_own_moves_and_slow_returns_are_not_flashes() {
        let end = at_end(78, 8421.0);
        let up = Shot { offset: -3887.0, ..end.clone() };
        assert_eq!(detect(&frames(&[(end.clone(), &[][..]), (up.clone(), &["reader"][..]), (end.clone(), &[][..])])), None);
        assert_eq!(detect(&frames(&[(end.clone(), &[][..]), (up.clone(), &["key:Home"][..]), (end.clone(), &["jump-latest"][..])])), None);
        let slow = [(end.clone(), &[][..]), (up.clone(), &[][..]), (up.clone(), &[][..]), (up.clone(), &[][..]), (up, &[][..]), (end.clone(), &[][..])];
        assert_eq!(detect(&frames(&slow)), None, "four frames away is a move, not a flash");
        // A re-estimate moves the offset a long way, the place not at all.
        let estimate = Shot { offset: 480.0 - 20_000.0, content: 20_000.0, ..end.clone() };
        assert_eq!(detect(&frames(&[(end.clone(), &[][..]), (estimate, &["estimate"][..]), (end, &["estimate"][..])])), None);
    }

    #[test]
    fn another_row_across_the_top_and_back_is_a_flash() {
        let end = at_end(78, 8421.0);
        let other = Shot { top: "row-60".into(), top_index: 60, offset: end.offset + 100.0, ..end.clone() };
        let flash = detect(&frames(&[(end.clone(), &[][..]), (other, &[][..]), (end, &[][..])])).expect("top row flash");
        assert_eq!(flash.kind, "top-row");
    }

    #[test]
    fn a_pane_records_frames_only_when_its_picture_changes_and_tags_reestimates() {
        let mut cam = Dashcam::default();
        let start = Instant::now();
        let end = at_end(78, 8421.0);
        cam.shot("pane-1", end.clone());
        assert!(cam.drawn(Duration::ZERO, start).is_empty());
        assert!(cam.drawn(Duration::from_millis(16), start).is_empty());
        assert_eq!(cam.frames("pane-1").len(), 1, "an unchanged picture is not another frame");
        cam.shot("pane-1", Shot { content: 9000.0, offset: 480.0 - 9000.0, ..end });
        cam.drawn(Duration::from_millis(32), start);
        assert_eq!(cam.frames("pane-1")[1].events, ["estimate"]);
        cam.drawn(Duration::from_secs(5), start);
        cam.event("pane-1", "live");
        cam.drawn(Duration::from_secs(5), start);
        assert_eq!(cam.frames("pane-1").len(), 1, "frames older than three seconds are let go");
    }

    #[test]
    fn a_pane_records_one_flash_per_ten_seconds() {
        let mut cam = Dashcam::default();
        let start = Instant::now();
        let end = at_end(78, 8421.0);
        let up = Shot { offset: -3887.0, ..end.clone() };
        let mut t = 0;
        let mut flash = |cam: &mut Dashcam, now: Instant| {
            let mut found = Vec::new();
            for shot in [&end, &up, &end] {
                cam.shot("pane-1", shot.clone());
                cam.event("pane-1", "live");
                t += 16;
                found.extend(cam.drawn(Duration::from_millis(t), now));
            }
            found
        };
        assert_eq!(flash(&mut cam, start).len(), 1);
        assert!(flash(&mut cam, start + Duration::from_secs(2)).is_empty());
        let next = flash(&mut cam, start + FLASH_EVERY);
        assert_eq!(next.len(), 1);
        assert_eq!(next[0].2, 1, "the one not recorded is counted");
    }

    #[test]
    fn the_hosts_event_fits_its_detail_and_keeps_the_flash() {
        let end = at_end(78, 8421.0);
        let up = Shot { offset: -3887.0, ..end.clone() };
        let mut shots: Vec<(Shot, &[&str])> = (0..180).map(|i| (Shot { top: format!("a-long-message-id-{i:04}-0123456789"), ..end.clone() }, &["live", "height", "rows -1 +2"][..])).collect();
        shots.push((up, &["rows -3 +1"][..]));
        shots.push((end.clone(), &["follow"][..]));
        let frames = frames(&shots);
        let flash = detect(&frames).expect("flash");
        let detail = flash_detail("pane-3", &flash, &frames, 2);
        assert!(detail.to_string().len() <= 3800, "{}", detail.to_string().len());
        let rows = detail["frames"].as_array().unwrap();
        assert_eq!(rows.last().unwrap()[0], 16, "the frame back, 16 ms after the jump");
        assert_eq!(rows[rows.len() - 2][4], 489, "the jump's place, x1000");
        assert_eq!(detail["kind"], "offset");
        assert_eq!(detail["skipped"], 2);
        assert_eq!(detail["dropped"].as_u64().unwrap() as usize + rows.len(), frames.len());
        let lines = flash_lines("pane-3", &flash, &frames, 0, "t");
        assert_eq!(lines.len(), frames.len() + 1);
        assert!(lines.last().unwrap().ends_with("<- back"), "{}", lines.last().unwrap());
    }
}
