//! Unasked movement in a chat (the rule every Clarp app shares): rows the
//! reader sees move while nobody scrolls. From two drawn frames of the
//! dashcam (scroll_journal.rs) it measures how far the visible rows moved,
//! and over a pane's frames it finds three things:
//!
//! - **shift**: rows moved 2 px or more in one frame nobody asked for;
//! - **shake**: two or more reversals of such movement within 500 ms, each
//!   step 2 px or more, as one event;
//! - **layout_shift**: a score summed over those frames of (the share of
//!   the viewport the moved rows cover) × (distance / viewport height), per
//!   chat opened and per minute, sent alone once it reaches 0.02.
//!
//! A frame is asked for while the reader scrolls (wheel, touchpad, bar,
//! keys, a fling settling) or a move they asked for runs (jump to the
//! latest, a seek, a search hit or link revealed, an older page), and
//! for 0.15 s after a resize that came within 0.4 s of the user's own input
//! (the window or a divider dragged, the text size changed, typing in the
//! composer); a resize the app made by itself is movement like any other. One
//! movement is expected: while the chat follows its end, new content at
//! the end slides the rows up, once, to the end. It does not count as a
//! shift or toward the score, but its steps count toward a shake, so a
//! slide that turns back is one.

use std::collections::hash_map::Entry;
use std::collections::{HashMap, VecDeque};
use std::time::{Duration, Instant};

use crate::scroll_journal::{Frame, Shot};

/// Movement under this (px) is not a shift and not a shake's step.
pub const STEP: f32 = 2.0;
/// The reversals of a shake fall within this time.
pub const SHAKE_WITHIN: Duration = Duration::from_millis(500);
/// A shake: at least this many reversals.
pub const REVERSALS: u32 = 2;
/// The layout score sent on its own at this.
pub const SCORE: f32 = 0.02;
/// The score's window while a chat stays open.
pub const MINUTE: Duration = Duration::from_secs(60);
/// At most one report per rule in this time, across windows.
pub const REPORT_EVERY: Duration = Duration::from_secs(60);

/// What moved between two frames nobody asked for.
#[derive(Debug, Clone, PartialEq)]
pub struct Step {
    /// The largest move of a visible row (px; up is negative).
    pub dy: f32,
    /// The share of the viewport the moved rows cover, before and after.
    pub fraction: f32,
    /// False for the follow-the-end slide: no shift, no score.
    pub counted: bool,
}

/// A move someone asked for: the reader's own scroll and keys, a fling
/// settling, a seek, a search hit or link revealed, an older page, the
/// jump to the latest.
pub fn asked(event: &str) -> bool {
    ["reader", "key:", "vim:", "bar:", "seek", "reveal", "load-older", "jump-latest"].iter().any(|p| event.starts_with(p))
}

/// The user's own input that may resize a chat: the window's size
/// changing under their drag, a pane divider dragged, the text size
/// changed, a key or edit in the composer (which grows with it).
pub const INPUT: &str = "input";

/// A resize is the user's when their input came this long before it (or
/// in the same frame); any other resize that moves the rows counts.
pub const AFTER_INPUT: Duration = Duration::from_millis(400);

/// After the user's resize, the rows settle for a moment: frames within
/// this of it are asked for too.
pub const AFTER_RESIZE: Duration = Duration::from_millis(150);

/// The chat's height changed between two frames.
fn resized(a: &Shot, b: &Shot) -> bool {
    (a.viewport - b.viewport).abs() >= 0.5
}

/// The chat was resized in `after`: its height changed, or its width or
/// text size did (the list lays its rows out again).
pub fn resize(before: &Frame, after: &Frame) -> bool {
    resized(&before.shot, &after.shot) || after.events.iter().any(|e| e == "resize")
}

fn at_end(s: &Shot) -> bool {
    s.content <= s.viewport || (s.offset - (s.viewport - s.content)).abs() <= 1.0
}

/// The span a row covered before and after, within the viewport, as a
/// share of it.
fn cover(viewport: f32, before: (f32, f32), after: (f32, f32)) -> f32 {
    let lo = before.0.min(after.0).max(0.0);
    let hi = (before.0 + before.1).max(after.0 + after.1).min(viewport);
    ((hi - lo) / viewport).clamp(0.0, 1.0)
}

/// How the visible rows moved from `before` to `after`, if the frame was
/// not asked for and anything moved half a px or more. The rows measured
/// are the one across the top and the one across the bottom, where each
/// was drawn in both frames; without either (the frame shows other rows),
/// the offset's change, but only while the rows and the content height
/// hold: the offset is in the list's estimate of its rows' heights, which
/// changes as rows come and are drawn (the offset moves, the view does not).
pub fn movement(before: &Frame, after: &Frame) -> Option<Step> {
    let (a, b) = (&before.shot, &after.shot);
    if a.viewport < 1.0 || b.viewport < 1.0 || after.events.iter().any(|e| asked(e) || e.starts_with("reset:")) {
        return None;
    }
    let viewport = b.viewport;
    let mut moved: Vec<(f32, f32)> = Vec::new();
    let mut anchors = 0;
    // The end stayed put under a message row: rows moving above it are not
    // the follow slide (a message growing above another message counts).
    let mut held_by_message = false;
    if !a.top.is_empty() && a.top == b.top {
        anchors += 1;
        let dy = b.top_y - a.top_y;
        if dy.abs() >= 0.5 {
            moved.push((dy, cover(viewport, (a.top_y, a.top_h), (b.top_y, b.top_h))));
        }
    }
    if !a.bottom.is_empty() && a.bottom == b.bottom && b.bottom != b.top {
        anchors += 1;
        let dy = b.bottom_y - a.bottom_y;
        if dy.abs() >= 0.5 {
            moved.push((dy, cover(viewport, (a.bottom_y, a.bottom_h), (b.bottom_y, b.bottom_h))));
        } else if !b.bottom_trailing {
            held_by_message = true;
        }
    }
    // No row on screen in both frames: the offset's change is the movement,
    // but only while the rows and the content height held. On this list the
    // offset is in estimated row heights: when a row lands or a height
    // changes, its move is the estimate's, not the reader's view (CI showed
    // false shifts and a false shake in a settled, following chat).
    let same_rows = a.rows == b.rows && (a.content - b.content).abs() < 0.5;
    if anchors == 0 && same_rows && !after.events.iter().any(|e| e == "estimate") {
        let dy = b.offset - a.offset;
        if dy.abs() >= 0.5 {
            moved.push((dy, 1.0));
        }
    }
    let &(dy, _) = moved.iter().max_by(|x, y| x.0.abs().total_cmp(&y.0.abs()))?;
    // Both ends moved the same way: every row between them did too.
    let fraction = if moved.len() == 2 && moved[0].0.signum() == moved[1].0.signum() { 1.0 } else { moved.iter().map(|m| m.1).sum::<f32>().min(1.0) };
    // The follow slide: following, every moved row went up, and the view is
    // at the end it was not at before, or something arrived there (a row,
    // a row's new height, a live update; the content height is the list's
    // estimate and may even shrink as a row lands).
    let arrived = b.rows > a.rows
        || b.content > a.content + 0.5
        || after.events.iter().any(|e| e == "height" || e == "live" || (e.starts_with("rows ") && !e.contains(" +0 ~0")));
    let slide = b.follow
        && !held_by_message
        && moved.iter().all(|m| m.0 < 0.0)
        && at_end(b)
        && (!at_end(a) || arrived || (anchors == 0 && a.follow));
    Some(Step { dy, fraction, counted: !slide })
}

/// The layout score of one step: share covered × distance / viewport.
pub fn score(step: &Step, viewport: f32) -> f32 {
    if !step.counted || viewport < 1.0 { 0.0 } else { step.fraction * step.dy.abs() / viewport }
}

/// One finding: `rule` shift, shake or layout_shift, at the frame drawn at
/// `at`.
#[derive(Debug, Clone, PartialEq)]
pub struct Found {
    pub rule: &'static str,
    pub at: Duration,
    /// shift: the step (px, up negative).
    pub step: f32,
    /// shake: reversals so far and the largest step (px).
    pub reversals: u32,
    pub max_step: f32,
    /// layout_shift: the score, and over what (`open` or `minute`).
    pub score: f32,
    pub scope: &'static str,
}

impl Found {
    fn new(rule: &'static str, at: Duration) -> Self {
        Found { rule, at, step: 0.0, reversals: 0, max_step: 0.0, score: 0.0, scope: "" }
    }
}

/// A shake under way: its reversals and largest step so far.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Tally {
    pub reversals: u32,
    pub max_step: f32,
}

/// One pane's movement: the steps of the last half second, the shake under
/// way and the scores.
#[derive(Debug, Default)]
pub struct Motion {
    /// (when, direction, size) of each step of 2 px or more.
    steps: VecDeque<(Duration, f32, f32)>,
    /// The shake under way (reported once), until it is still for 500 ms.
    pub shake: Option<Tally>,
    open_score: f32,
    open_sent: bool,
    minute: Option<(Duration, f32, bool)>,
    /// When the user's input last came (a frame with an `input` event).
    input: Option<Duration>,
    /// The user's resize asks for movement until then.
    resized_until: Option<Duration>,
}

impl Motion {
    /// A frame was drawn: what it ends or starts.
    pub fn frame(&mut self, before: &Frame, after: &Frame) -> Vec<Found> {
        let mut found = Vec::new();
        if after.events.iter().any(|e| e.starts_with("reset:")) {
            // Another chat (or every row again): a new open.
            *self = Motion { minute: self.minute.take(), input: self.input, resized_until: self.resized_until, ..Motion::default() };
        }
        if after.events.iter().any(|e| e == INPUT) {
            self.input = Some(after.at);
        }
        // A resize right after the user's input is theirs; any other (a
        // side pane, an overlay or a banner opening, the composer growing
        // with a restored draft) is movement like any other.
        if resize(before, after) && self.input.is_some_and(|input| after.at.saturating_sub(input) <= AFTER_INPUT) {
            self.resized_until = Some(after.at + AFTER_RESIZE);
        }
        if after.events.iter().any(|e| asked(e)) || self.resized_until.is_some_and(|until| after.at <= until) {
            self.steps.clear();
            self.shake = None;
            if self.resized_until.is_some_and(|until| after.at <= until) {
                return found;
            }
        }
        while self.steps.front().is_some_and(|s| after.at.saturating_sub(s.0) > SHAKE_WITHIN) {
            self.steps.pop_front();
        }
        if self.steps.is_empty() {
            self.shake = None;
        }
        let Some(step) = movement(before, after) else { return found };
        if step.counted && step.dy.abs() >= STEP {
            found.push(Found { step: step.dy, ..Found::new("shift", after.at) });
        }
        if step.dy.abs() >= STEP {
            let direction = step.dy.signum();
            let turned = self.steps.back().is_some_and(|s| s.1 != direction);
            self.steps.push_back((after.at, direction, step.dy.abs()));
            let reversals = self.steps.iter().zip(self.steps.iter().skip(1)).filter(|(x, y)| x.1 != y.1).count() as u32;
            let max_step = self.steps.iter().map(|s| s.2).fold(0.0, f32::max);
            if let Some(tally) = &mut self.shake {
                if turned {
                    tally.reversals += 1;
                }
                tally.max_step = tally.max_step.max(step.dy.abs());
            } else if reversals >= REVERSALS {
                self.shake = Some(Tally { reversals, max_step });
                found.push(Found { reversals, max_step, ..Found::new("shake", after.at) });
            }
        }
        let gained = score(&step, after.shot.viewport);
        if gained > 0.0 {
            self.open_score += gained;
            let minute = self.minute.get_or_insert((after.at, 0.0, false));
            if after.at.saturating_sub(minute.0) >= MINUTE {
                *minute = (after.at, 0.0, false);
            }
            minute.1 += gained;
            if !self.open_sent && self.open_score >= SCORE {
                self.open_sent = true;
                found.push(Found { score: self.open_score, scope: "open", ..Found::new("layout_shift", after.at) });
            } else if !minute.2 && minute.1 >= SCORE {
                minute.2 = true;
                found.push(Found { score: minute.1, scope: "minute", ..Found::new("layout_shift", after.at) });
            }
            if minute.1 >= SCORE {
                minute.2 = true;
            }
        }
        found
    }

    /// The score of the chat since it was opened.
    pub fn open_score(&self) -> f32 {
        self.open_score
    }
}

/// At most one report per rule per `REPORT_EVERY`, across every window;
/// those held back are counted for the next.
#[derive(Debug, Default)]
pub struct Limit {
    last: HashMap<&'static str, (Instant, u32)>,
}

impl Limit {
    /// Whether `rule` may be reported at `now`, and how many were held
    /// back since the last.
    pub fn allow(&mut self, rule: &'static str, now: Instant) -> Option<u32> {
        match self.last.entry(rule) {
            Entry::Vacant(vacant) => {
                vacant.insert((now, 0));
                Some(0)
            }
            Entry::Occupied(mut occupied) => {
                let (at, held) = occupied.get_mut();
                if now.duration_since(*at) < REPORT_EVERY {
                    *held += 1;
                    None
                } else {
                    *at = now;
                    Some(std::mem::take(held))
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const VIEW: f32 = 480.0;

    /// A chat at its end: `content` tall, the top row `top` at `top_y`, the
    /// last row across the bottom, ending at the viewport's bottom.
    fn pinned(content: f32, top_y: f32, last_h: f32) -> Shot {
        Shot {
            offset: VIEW - content,
            content,
            viewport: VIEW,
            top: "row-10".into(),
            top_index: 10,
            follow: true,
            rows: 20,
            top_y,
            top_h: 120.0,
            bottom: "row-19".into(),
            bottom_y: VIEW - last_h,
            bottom_h: last_h,
            bottom_trailing: false,
        }
    }

    fn frame(ms: u64, shot: Shot, events: &[&str]) -> Frame {
        Frame { at: Duration::from_millis(ms), shot, events: events.iter().map(|e| (*e).to_owned()).collect() }
    }

    /// A reply streaming above the Running box (a trailing live row that
    /// stays at the end) is the follow slide; a message growing above
    /// another message row that stays at the end is counted.
    #[test]
    fn a_reply_above_the_running_box_slides_and_one_above_a_message_counts() {
        let mut before = pinned(2000.0, 40.0, 60.0);
        let mut after = pinned(2100.0, -60.0, 60.0);
        before.bottom = "live:fold:t1".into();
        after.bottom = "live:fold:t1".into();
        before.bottom_trailing = true;
        after.bottom_trailing = true;
        let step = movement(&frame(0, before.clone(), &[]), &frame(16, after.clone(), &["live"])).expect("rows moved");
        assert!(!step.counted, "above the Running box: the slide {step:?}");
        before.bottom = "r-2".into();
        after.bottom = "r-2".into();
        before.bottom_trailing = false;
        after.bottom_trailing = false;
        let step = movement(&frame(0, before, &[]), &frame(16, after, &["height"])).expect("rows moved");
        assert!(step.counted, "a message growing above another message counts {step:?}");
    }

    /// No row on screen in both frames, the rows and content held, nobody
    /// following: a big jump is counted from the offset.
    #[test]
    fn a_big_jump_with_no_row_in_both_frames_is_counted_from_the_offset() {
        let mut before = pinned(2000.0, 40.0, 200.0);
        before.follow = false;
        before.offset = -800.0;
        let mut after = before.clone();
        after.offset = -1500.0;
        after.top = "row-15".into();
        after.bottom = "row-18".into();
        let step = movement(&frame(0, before, &[]), &frame(16, after, &[])).expect("the jump is seen");
        assert!(step.counted && (step.dy + 700.0).abs() < 0.5, "{step:?}");
    }

    /// A reply taller than the window lands at the end while the chat
    /// follows it: no row is on screen in both frames, the content grew and
    /// the view moved up to the new end. That is the follow slide, never a
    /// shift (the offset, an estimate once the content changed, says nothing).
    #[test]
    fn a_reply_taller_than_the_window_landing_while_following_is_no_shift() {
        let before = pinned(2000.0, 40.0, 200.0);
        let mut after = pinned(2900.0, 0.0, 900.0);
        after.rows = 21;
        after.top = "row-20".into();
        after.top_index = 20;
        after.top_h = 900.0;
        after.bottom = "row-20".into();
        after.bottom_y = VIEW - 900.0;
        after.bottom_h = 900.0;
        let step = movement(&frame(0, before.clone(), &[]), &frame(16, after.clone(), &["rows -0 +1 ~0"]));
        assert!(step.as_ref().is_none_or(|s| !s.counted), "no shift for the expected slide: {step:?}");
        // The same tall reply growing on screen in both frames slides up as
        // it grows: the follow slide too.
        let mut growing = after.clone();
        growing.content = 3100.0;
        growing.offset = VIEW - 3100.0;
        growing.top_y = -200.0;
        growing.top_h = 1100.0;
        growing.bottom_y = VIEW - 1100.0;
        growing.bottom_h = 1100.0;
        let step = movement(&frame(16, after, &[]), &frame(32, growing, &["live"])).expect("the reply moved");
        assert!(!step.counted, "a tall reply growing at the end while following is the slide: {step:?}");
    }

    #[test]
    fn the_follow_slide_is_no_shift_and_scores_nothing() {
        // The last row grows by 40 px, the chat stays at its end: the rows
        // above slide up 40 px.
        let a = frame(0, pinned(4000.0, -20.0, 100.0), &[]);
        let b = frame(16, pinned(4040.0, -60.0, 140.0), &["live"]);
        let step = movement(&a, &b).expect("rows moved");
        assert_eq!((step.dy, step.counted), (-40.0, false));
        assert!(Motion::default().frame(&a, &b).is_empty());
        // Drawn short of the end, then pinned a frame later: the slide too.
        let short = frame(16, Shot { offset: VIEW - 4000.0, bottom_y: VIEW - 100.0, ..pinned(4040.0, -20.0, 140.0) }, &["live"]);
        let late = frame(32, pinned(4040.0, -60.0, 140.0), &["follow"]);
        assert_eq!(movement(&a, &short), None, "the last row grew below the viewport; nothing visible moved");
        assert_eq!(movement(&short, &late).map(|s| s.counted), Some(false));
    }

    #[test]
    fn a_row_landing_while_the_estimate_shrinks_is_the_slide_and_other_rows_are_no_move() {
        // Seen in CI: a row lands at the end, the list's estimate of the
        // content drops 248 px, the rows on screen slide up 74 px.
        let a = frame(0, pinned(7015.0, -36.0, 157.0), &[]);
        let b = frame(16, Shot { rows: 21, bottom: "row-20".into(), ..pinned(6767.0, -110.0, 74.0) }, &["rows -0 +1 ~0", "follow", "height"]);
        assert_eq!(movement(&a, &b).map(|s| (s.dy, s.counted)), Some((-74.0, false)));
        // Neither row drawn in both, a row more: the offset (an estimate)
        // says nothing.
        let other = Shot { top: "row-11".into(), bottom: "row-20".into(), rows: 21, offset: -9000.0, content: 9480.0, ..pinned(4000.0, -20.0, 100.0) };
        assert_eq!(movement(&a, &frame(16, other, &["rows -0 +1 ~0"])), None);
    }

    #[test]
    fn rows_moving_while_the_end_stays_put_are_a_shift() {
        let a = frame(0, pinned(4000.0, -20.0, 100.0), &[]);
        // Same content, same end, yet the top row is drawn 6 px lower.
        let b = frame(16, Shot { top_y: -14.0, ..pinned(4000.0, -20.0, 100.0) }, &[]);
        let found = Motion::default().frame(&a, &b);
        assert_eq!(found.iter().map(|f| (f.rule, f.step)).collect::<Vec<_>>(), [("shift", 6.0)]);
        // A slide down while following (the chat shrank under the reader) is one too.
        let shrunk = frame(16, pinned(3960.0, 20.0, 60.0), &["live"]);
        assert!(movement(&a, &shrunk).is_some_and(|s| s.counted && s.dy == 40.0));
        // Under 2 px is not.
        let tiny = frame(16, Shot { top_y: -18.5, ..pinned(4000.0, -20.0, 100.0) }, &[]);
        assert!(Motion::default().frame(&a, &tiny).is_empty());
    }

    /// The chat resized (its height and rows' places changed) at `ms`.
    fn resized_at(ms: u64, events: &[&str]) -> Frame {
        frame(ms, Shot { viewport: 420.0, top_y: -80.0, ..pinned(4000.0, -20.0, 100.0) }, events)
    }

    fn shifts(found: &[Found]) -> usize {
        found.iter().filter(|f| f.rule == "shift").count()
    }

    /// A resize right after the user's input (dragging the window or a
    /// divider, zooming, typing in the composer) is theirs: it and the rows
    /// settling in the 0.15 s after it post nothing; a jump later counts.
    #[test]
    fn a_resize_right_after_input_and_its_settling_post_nothing() {
        let a = frame(0, pinned(4000.0, -20.0, 100.0), &[]);
        let typed = frame(100, pinned(4000.0, -20.0, 100.0), &[INPUT]);
        let wider = resized_at(450, &["resize"]);
        let mut motion = Motion::default();
        assert!(motion.frame(&a, &typed).is_empty());
        assert!(motion.frame(&typed, &wider).is_empty(), "the resize 0.35 s after input");
        let settle = frame(550, Shot { viewport: 420.0, top_y: -40.0, ..pinned(4000.0, -20.0, 100.0) }, &[]);
        assert!(motion.frame(&wider, &settle).is_empty(), "rows settling within 0.15 s after it");
        let later = frame(900, Shot { viewport: 420.0, top_y: 60.0, ..pinned(4000.0, -20.0, 100.0) }, &[]);
        assert_eq!(shifts(&motion.frame(&settle, &later)), 1, "a jump well after the resize counts");
        // Input in the resize's own frame (the window's size changed).
        let mut motion = Motion::default();
        assert!(motion.frame(&a, &resized_at(16, &[INPUT, "resize"])).is_empty());
    }

    /// A resize the app made by itself (a side pane, overlay or banner
    /// opening, a restored draft growing the composer) is a shift.
    #[test]
    fn a_resize_with_no_input_before_it_is_a_shift() {
        let a = frame(0, pinned(4000.0, -20.0, 100.0), &[]);
        assert_eq!(shifts(&Motion::default().frame(&a, &resized_at(16, &["resize"]))), 1, "a pane or banner opening");
        assert_eq!(shifts(&Motion::default().frame(&a, &resized_at(16, &[]))), 1, "the height alone changed");
    }

    #[test]
    fn a_resize_half_a_second_after_input_is_a_shift() {
        let typed = frame(0, pinned(4000.0, -20.0, 100.0), &[INPUT]);
        let mut motion = Motion::default();
        assert!(motion.frame(&frame(0, pinned(4000.0, -20.0, 100.0), &[]), &typed).is_empty());
        let still = frame(300, pinned(4000.0, -20.0, 100.0), &["live"]);
        assert!(motion.frame(&typed, &still).is_empty());
        assert_eq!(shifts(&motion.frame(&still, &resized_at(500, &["resize"]))), 1);
    }

    #[test]
    fn what_the_reader_asked_for_is_not_movement() {
        let a = frame(0, pinned(4000.0, -20.0, 100.0), &[]);
        let moved = pinned(4000.0, 200.0, 100.0);
        for asked in ["reader", "key:End", "vim:row", "bar:drag", "seek", "reveal", "load-older", "jump-latest"] {
            assert_eq!(movement(&a, &frame(16, moved.clone(), &[asked])), None, "{asked}");
        }
        // Another chat opened: other rows.
        assert_eq!(movement(&a, &frame(16, moved, &["reset:chat"])), None);
        // A re-estimate moves the offset and no row drawn in both.
        let other = Shot { top: "row-3".into(), bottom: "row-5".into(), offset: -9000.0, ..pinned(4000.0, -20.0, 100.0) };
        assert_eq!(movement(&a, &frame(16, other.clone(), &["estimate"])), None);
        // Without one, the offset is what moved.
        assert_eq!(movement(&a, &frame(16, other, &[])).map(|s| (s.dy, s.fraction)), Some((-9000.0 - (VIEW - 4000.0), 1.0)));
    }

    /// Row heights settling up and down while pinned to the end.
    fn settling(start: u64, every: u64, heights: &[f32]) -> Vec<Frame> {
        heights
            .iter()
            .enumerate()
            .map(|(i, h)| frame(start + every * i as u64, pinned(4000.0 + h, -20.0 - h, 100.0 + h), &["live"]))
            .collect()
    }

    #[test]
    fn heights_settling_up_and_down_at_the_end_are_one_shake() {
        let frames = settling(0, 60, &[0.0, 30.0, 0.0, 30.0, 0.0, 30.0, 0.0]);
        let mut motion = Motion::default();
        let found: Vec<Found> = frames.windows(2).flat_map(|w| motion.frame(&w[0], &w[1])).collect();
        let shakes: Vec<&Found> = found.iter().filter(|f| f.rule == "shake").collect();
        assert_eq!(shakes.len(), 1, "one event, not one per step: {found:?}");
        assert_eq!((shakes[0].reversals, shakes[0].max_step, shakes[0].at), (2, 30.0, Duration::from_millis(180)));
        assert_eq!(motion.shake, Some(Tally { reversals: 5, max_step: 30.0 }), "the shake goes on counting");
        // The slides up are expected; the slides down are shifts.
        assert_eq!(found.iter().filter(|f| f.rule == "shift").count(), 3);
        assert!(found.iter().all(|f| f.rule != "shift" || f.step == 30.0));
        // Still for half a second: the next is another shake.
        let more = settling(1000, 60, &[0.0, 30.0, 0.0, 30.0]);
        let again: Vec<Found> = std::iter::once((&frames[6], &more[0])).chain(more.windows(2).map(|w| (&w[0], &w[1]))).flat_map(|(a, b)| motion.frame(a, b)).collect();
        assert_eq!(again.iter().filter(|f| f.rule == "shake").count(), 1);
    }

    #[test]
    fn slow_turns_and_one_way_slides_are_no_shake() {
        // One way only (a streaming reply growing): no reversal.
        let growing = settling(0, 30, &[0.0, 20.0, 40.0, 60.0, 80.0, 100.0]);
        let mut motion = Motion::default();
        assert!(growing.windows(2).flat_map(|w| motion.frame(&w[0], &w[1])).next().is_none());
        // Turning back each 300 ms: two reversals are never within 500 ms.
        let slow = settling(0, 300, &[0.0, 30.0, 0.0, 30.0, 0.0]);
        let mut motion = Motion::default();
        let found: Vec<Found> = slow.windows(2).flat_map(|w| motion.frame(&w[0], &w[1])).collect();
        assert!(found.iter().all(|f| f.rule != "shake"), "{found:?}");
        // A reversal the reader made is theirs.
        let mut frames = settling(0, 60, &[0.0, 30.0, 0.0, 30.0]);
        frames[2].events.push("reader".into());
        let mut motion = Motion::default();
        assert!(frames.windows(2).flat_map(|w| motion.frame(&w[0], &w[1])).all(|f| f.rule != "shake"));
    }

    #[test]
    fn the_layout_score_is_share_times_distance_and_sent_at_two_hundredths() {
        // The whole viewport moves 6 px down: 1 × 6/480 = 0.0125 a frame.
        let a = frame(0, pinned(4000.0, -20.0, 100.0), &[]);
        let down = |ms: u64, by: f32| frame(ms, Shot { top_y: -20.0 + by, bottom_y: VIEW - 100.0 + by, ..pinned(4000.0, -20.0, 100.0) }, &[]);
        let b = down(16, 6.0);
        let step = movement(&a, &b).unwrap();
        assert_eq!(step.fraction, 1.0, "both ends moved down together");
        assert!((score(&step, VIEW) - 0.0125).abs() < 1e-6);
        let mut motion = Motion::default();
        assert!(motion.frame(&a, &b).iter().all(|f| f.rule != "layout_shift"), "0.0125 is under 0.02");
        let found = motion.frame(&b, &a);
        let sent: Vec<(f32, &str)> = found.iter().filter(|f| f.rule == "layout_shift").map(|f| (f.score, f.scope)).collect();
        assert_eq!(sent.len(), 1);
        assert!((sent[0].0 - 0.025).abs() < 1e-6 && sent[0].1 == "open", "{sent:?}");
        // Only the top row moved: its own span, not the viewport.
        let top_only = frame(16, Shot { top_y: -14.0, ..pinned(4000.0, -20.0, 100.0) }, &[]);
        let step = movement(&a, &top_only).unwrap();
        assert!((step.fraction - 106.0 / VIEW).abs() < 1e-6, "{step:?}");
        // The minute: another open's movement goes on into the same minute;
        // one past it starts the next.
        let mut motion = Motion::default();
        let mut sent = Vec::new();
        for i in 0..4u64 {
            let (x, y) = if i % 2 == 0 { (a.clone(), down(16, 6.0)) } else { (down(16, 6.0), a.clone()) };
            let y = Frame { at: Duration::from_secs(i * 25), events: if i == 2 { vec!["reset:chat".into()] } else { vec![] }, ..y };
            sent.extend(motion.frame(&x, &y).into_iter().filter(|f| f.rule == "layout_shift").map(|f| f.scope));
        }
        assert_eq!(sent, ["open"], "the reset frame itself is no movement; the open is new but under 0.02");
    }

    #[test]
    fn one_report_per_rule_a_minute_across_windows() {
        let mut limit = Limit::default();
        let now = Instant::now();
        assert_eq!(limit.allow("shake", now), Some(0));
        assert_eq!(limit.allow("shake", now + Duration::from_secs(10)), None);
        assert_eq!(limit.allow("shift", now + Duration::from_secs(10)), Some(0), "each rule its own");
        assert_eq!(limit.allow("shake", now + Duration::from_secs(59)), None);
        assert_eq!(limit.allow("shake", now + REPORT_EVERY), Some(2), "the held-back ones are counted");
    }
}
