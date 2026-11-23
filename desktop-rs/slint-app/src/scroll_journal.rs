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
    /// From past the end (blank below the last row) back to it is not up.
    fn viewports_up(&self) -> f32 {
        let range = (self.content - self.viewport).max(0.0);
        (self.old_place.min(1.0) - self.new_place) * range / self.viewport.max(1.0)
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
/// when they were drawn, how it showed (`jump_back`: a viewport or more
/// away; `top_flip`: another row across the top) and how many viewports away.
#[derive(Debug, Clone, PartialEq)]
pub struct Flash {
    pub kind: &'static str,
    pub jumped: Duration,
    pub back: Duration,
    pub frames_away: usize,
    pub viewports: f32,
}

/// A move someone asked for (the reader's scroll and keys, a search, a
/// seek, an older page): never a flash. Opening another chat is not one:
/// it starts at the end, so the old chat's end and the new one's are both
/// place 1, and a frame of the new chat anywhere else between them is the
/// flash this looks for.
fn asked(event: &str) -> bool {
    ["reader", "key:", "vim:", "bar:", "seek", "reveal", "load-older"].iter().any(|p| event.starts_with(p))
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
                kind: if offset_flash { "jump_back" } else { "top_flip" },
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
    /// Flashes found so far, recorded or not.
    pub seen: usize,
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
            self.seen += 1;
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

/// What happened before a frame, as the Host's `scroll_flash` names it:
/// (kind, note), kind reload, insert, remove, height or live. Pins, the
/// list's re-estimates and moves are the local journal's only.
fn host_events(events: &[String]) -> Vec<(&'static str, String)> {
    let mut out = Vec::new();
    for event in events {
        if let Some(why) = event.strip_prefix("reset:") {
            out.push(("reload", why.to_owned()));
        } else if let Some(counts) = event.strip_prefix("rows ") {
            let count = |sign: char| counts.split(' ').find_map(|c| c.strip_prefix(sign)).and_then(|n| n.parse::<usize>().ok()).unwrap_or(0);
            let (removed, inserted, updated) = (count('-'), count('+'), count('~'));
            if removed > 0 {
                out.push(("remove", format!("{removed} rows")));
            }
            if inserted > 0 {
                out.push(("insert", format!("{inserted} rows")));
            }
            if updated > 0 {
                out.push(("live", format!("{updated} rows updated")));
            }
        } else if event == "height" {
            out.push(("height", String::new()));
        } else if event == "live" {
            out.push(("live", "tick".to_owned()));
        }
    }
    out
}

/// The shared `scroll_flash` event's detail (every app sends this shape):
/// `chat` the chat's session on the Host it is posted to, `window` the
/// pane, `screen` the viewport's height, `rule` jump_back or top_flip,
/// `frames` [{t ms from the jump, y offset, h content height, top message
/// id, g a gesture}] (only those where y, h or top changed; at most 60),
/// `events` [{t, kind, note}] (at most 30). It stays under the Host's 4 KB
/// detail: the oldest frames go first, then the oldest events, never the
/// flash's own frames.
pub fn flash_detail(chat: &str, pane: &str, flash: &Flash, frames: &[Frame]) -> Value {
    const MOST: usize = 3900;
    const MOST_FRAMES: usize = 60;
    const MOST_EVENTS: usize = 30;
    let ms = |at: Duration| ((at.as_secs_f64() - flash.jumped.as_secs_f64()) * 1000.0).round() as i64;
    let mut kept: Vec<Value> = Vec::new();
    let mut needed = 0;
    let mut last: Option<&Shot> = None;
    let mut events: Vec<Value> = Vec::new();
    for (i, frame) in frames.iter().enumerate() {
        let s = &frame.shot;
        for (kind, note) in host_events(&frame.events) {
            events.push(json!({"t": ms(frame.at), "kind": kind, "note": note}));
        }
        let changed = last.is_none_or(|l| (l.offset - s.offset).abs() >= 0.5 || (l.content - s.content).abs() >= 0.5 || l.top != s.top);
        // The frame before the jump is the flash's too.
        let flash_frame = frames.get(i + 1).is_some_and(|next| next.at >= flash.jumped) || frame.at >= flash.jumped;
        if changed || (flash_frame && frame.at <= flash.back) {
            if flash_frame && needed == 0 {
                needed = kept.len();
            }
            kept.push(json!({"t": ms(frame.at), "y": s.offset.round() as i64, "h": s.content.round() as i64, "top": s.top,
                             "g": frame.events.iter().any(|e| asked(e))}));
            last = Some(s);
        }
    }
    let screen = frames.iter().find(|f| f.at >= flash.jumped).map_or(0.0, |f| f.shot.viewport);
    let mut from_frame = kept.len().saturating_sub(MOST_FRAMES).min(needed);
    let mut from_event = events.len().saturating_sub(MOST_EVENTS);
    loop {
        let detail = json!({
            "chat": chat,
            "window": pane,
            "screen": screen.round() as i64,
            "rule": flash.kind,
            "frames": &kept[from_frame..],
            "events": &events[from_event..],
        });
        if detail.to_string().len() <= MOST || (from_frame >= needed && from_event >= events.len()) {
            return detail;
        }
        if from_frame < needed {
            from_frame += 1;
        } else {
            from_event += 1;
        }
    }
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
            // A peer's chat goes to its own Host, under its session there.
            let session = crate::app().map(|app| app.session_of(&pane)).unwrap_or_default();
            let mut posted = None;
            if let Some(app) = crate::app() {
                app.engine.borrow().report_chat_event(&session, "scroll_flash", "warning", |chat| {
                    let detail = flash_detail(chat, &pane, &flash, &frames);
                    posted = Some(detail.clone());
                    detail
                });
            }
            let detail = posted.unwrap_or_else(|| flash_detail(&session, &pane, &flash, &frames));
            FLASHES.with(|f| f.borrow_mut().push(detail));
        });
    }
}

/// A pane's frames of the last three seconds (for the checks).
pub fn frames(pane: &str) -> Vec<Frame> {
    DASHCAM.with(|d| d.borrow().frames(pane))
}

/// Flashes found so far, recorded or not (for the checks).
pub fn flashes_seen() -> usize {
    DASHCAM.with(|d| d.borrow().seen)
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
        assert_eq!(flash.kind, "jump_back");
        assert_eq!(flash.frames_away, 2);
        assert_eq!(flash.jumped, Duration::from_millis(16));
        assert!((flash.viewports - 4054.0 / 480.0).abs() < 0.01, "{flash:?}");
    }

    #[test]
    fn blank_space_past_the_end_that_comes_back_is_a_flash() {
        let end = at_end(93, 6095.0);
        let past = Shot { offset: -6115.0 - 700.0, ..end.clone() };
        let flash = detect(&frames(&[(end.clone(), &[][..]), (past, &[][..]), (end, &[][..])])).expect("overshoot and back");
        assert_eq!(flash.kind, "jump_back");
    }

    #[test]
    fn the_readers_own_moves_and_slow_returns_are_not_flashes() {
        let end = at_end(78, 8421.0);
        let up = Shot { offset: -3887.0, ..end.clone() };
        assert_eq!(detect(&frames(&[(end.clone(), &[][..]), (up.clone(), &["reader"][..]), (end.clone(), &[][..])])), None);
        assert_eq!(detect(&frames(&[(end.clone(), &[][..]), (up.clone(), &["key:Home"][..]), (end.clone(), &["jump-latest"][..])])), None);
        // Another chat opened at its middle, then pinned to its end, is one.
        assert!(detect(&frames(&[(end.clone(), &[][..]), (up.clone(), &["reset:chat", "rows -0 +78 ~0"][..]), (end.clone(), &["jump-latest"][..])])).is_some());
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
        assert_eq!(flash.kind, "top_flip");
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
            // Away and back, then still at the end (or the way back up
            // from the end would look like a flash too).
            for shot in [&end, &up, &end, &end, &end, &end, &end] {
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
    fn the_scroll_flash_event_has_the_shared_shape_and_fits_four_kb() {
        let end = at_end(78, 8421.0);
        let up = Shot { offset: -3887.0, ..end.clone() };
        // 180 frames before the flash, each another top row (so each is kept).
        let mut shots: Vec<(Shot, &[&str])> = (0..180)
            .map(|i| (Shot { top: format!("a-long-message-id-{i:04}-0123456789"), ..end.clone() }, &["live", "height", "rows -1 +2 ~1"][..]))
            .collect();
        shots.push((end.clone(), &["reset:chat"][..]));
        shots.push((up, &["rows -3 +1 ~0"][..]));
        shots.push((end.clone(), &["follow"][..]));
        shots.push((end.clone(), &["live"][..]));
        let frames = frames(&shots);
        let flash = detect(&frames[..frames.len() - 1]).expect("flash");
        let detail = flash_detail("rachel", "pane-3", &flash, &frames);
        assert!(detail.to_string().len() <= 3900, "{}", detail.to_string().len());
        let mut keys: Vec<&String> = detail.as_object().unwrap().keys().collect();
        keys.sort();
        assert_eq!(keys, ["chat", "events", "frames", "rule", "screen", "window"]);
        assert_eq!((detail["chat"].as_str(), detail["window"].as_str(), detail["screen"].as_i64(), detail["rule"].as_str()), (Some("rachel"), Some("pane-3"), Some(480), Some("jump_back")));
        let shown = detail["frames"].as_array().unwrap();
        assert!(shown.len() <= 60);
        for frame in shown {
            let mut fields: Vec<&String> = frame.as_object().unwrap().keys().collect();
            fields.sort();
            assert_eq!(fields, ["g", "h", "t", "top", "y"]);
        }
        // The flash: before it, away and back (the last frame did not move).
        let tail: Vec<(i64, i64)> = shown[shown.len() - 3..].iter().map(|f| (f["t"].as_i64().unwrap(), f["y"].as_i64().unwrap())).collect();
        assert_eq!(tail, [(-16, -7941), (0, -3887), (16, -7941)]);
        let events = detail["events"].as_array().unwrap();
        assert!(events.len() <= 30);
        for event in events {
            assert!(["reload", "insert", "remove", "height", "live"].contains(&event["kind"].as_str().unwrap()), "{event}");
            let mut fields: Vec<&String> = event.as_object().unwrap().keys().collect();
            fields.sort();
            assert_eq!(fields, ["kind", "note", "t"]);
        }
        let last: Vec<(&str, &str)> = events[events.len() - 4..].iter().map(|e| (e["kind"].as_str().unwrap(), e["note"].as_str().unwrap())).collect();
        assert_eq!(last, [("reload", "chat"), ("remove", "3 rows"), ("insert", "1 rows"), ("live", "tick")]);
        let lines = flash_lines("pane-3", &flash, &frames, 0, "t");
        assert_eq!(lines.len(), frames.len() + 1);
    }
}
