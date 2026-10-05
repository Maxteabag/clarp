//! The chat's row heights, one book per pane, so its scrollbar keeps its
//! size. Slint's ListView only lays out the rows on screen and takes the
//! chat's height as their average times the row count, so the thumb grew
//! and shrank as the reader scrolled past short and tall rows. Here every
//! row is measured off screen (the transcript lays out a batch at a time
//! with the very component it draws rows with) and kept by its id while the
//! row is unchanged; the transcript sizes and places its own thumb from the
//! sum.

use std::collections::{HashMap, HashSet};
use std::rc::Rc;
use std::time::{Duration, Instant};

use slint::{ComponentHandle, Model, SharedString, VecModel};

use crate::{MeasureRow, MessageRow};

/// Rows laid out per batch, and the pause before the next look: measuring
/// stays a small share of each frame, even for a chat of heavy cards. Rows
/// that only changed (or whose layout did: the pane was resized) keep their
/// old height meanwhile and are measured a couple at a time.
const BATCH: usize = 8;
const CHANGED_BATCH: usize = 2;
const PAUSE: Duration = Duration::from_millis(33);
/// A row that changed is measured off screen only after this long: one on
/// screen has reported its drawn height by then.
const GRACE: Duration = Duration::from_millis(150);
/// A row's height before it is measured, while nothing is.
const GUESS: f32 = 120.0;
/// A batch whose reports have not all come by then is asked for again.
const LATE: Duration = Duration::from_secs(1);

thread_local! {
    /// The next look at the books, and the look that goes on while a batch
    /// is out (in case its reports never come).
    static SOON: slint::Timer = slint::Timer::default();
    static WATCH: slint::Timer = slint::Timer::default();
}

/// Has every pane's book brought up to its rows shortly (rows changed, or a
/// batch came back). The transcript lays out a batch when the window next
/// draws, so a redraw is asked for while rows remain.
pub fn measure_soon() {
    SOON.with(|timer| {
        if !timer.running() {
            timer.start(slint::TimerMode::SingleShot, PAUSE, look);
        }
    });
}

fn look() {
    let Some(app) = crate::app() else { return };
    if !app.measure_rows() {
        WATCH.with(slint::Timer::stop);
        return;
    }
    if let Some(window) = app.window.upgrade() {
        window.window().request_redraw();
    }
    WATCH.with(|timer| timer.start(slint::TimerMode::SingleShot, LATE / 4, look));
}

#[derive(Default)]
pub struct HeightBook {
    /// Row id → the row as it was measured, its height, and the layout
    /// (`epoch`) it was measured in. One measured for another row or layout
    /// stands in for its row until the row is measured again.
    heights: HashMap<SharedString, (MessageRow, f32, u32)>,
    /// The chat's width, and its layout: the width or text size changed
    /// this many times.
    width: f32,
    epoch: u32,
    /// Rows measured as they are now, and rows waiting, at the last update;
    /// when the rows waiting began to, and how long the last such pass took.
    known: usize,
    missing: usize,
    pass: Option<Instant>,
    last_pass: Option<Duration>,
    /// The batch out for measuring: its number, when it went, its rows,
    /// and those not reported yet.
    batch: i32,
    sent: Option<Instant>,
    asked: HashMap<SharedString, MessageRow>,
    reported: HashSet<SharedString>,
    /// Row id → its index, as of the last update.
    index: HashMap<SharedString, usize>,
    /// Rows measured before they or the layout changed: since when, and
    /// how long such a row waits before it is measured off screen.
    stale: HashMap<SharedString, Instant>,
    grace: Duration,
    /// The rows the transcript measures (the batch out).
    pub measure: Rc<VecModel<MeasureRow>>,
    /// How far down the chat each row starts, and the chat's height last.
    pub before: Rc<VecModel<f32>>,
}

impl HeightBook {
    pub fn new() -> Self {
        Self { grace: GRACE, ..Self::default() }
    }

    /// The chat's width or text size changed: every row is measured again,
    /// its old height standing in meanwhile.
    pub fn relayout(&mut self) {
        self.epoch += 1;
        self.asked.clear();
        self.reported.clear();
        self.sent = None;
    }

    /// Forgets every height (another chat).
    pub fn clear(&mut self) {
        self.heights.clear();
        self.asked.clear();
        self.reported.clear();
        self.sent = None;
        self.measure.set_vec(Vec::new());
    }

    /// A row measured off screen. A row reports as it is created, before
    /// the blocks inside it are, and again once they are laid out, all
    /// before the window draws: the last report of the batch's frame is
    /// the height, so the batch counts as done only on the next update.
    pub fn measured(&mut self, batch: i32, id: &str, height: f32, width: f32) {
        if batch != self.batch {
            return;
        }
        self.at_width(width);
        let Some(row) = self.asked.get(id) else { return };
        self.heights.insert(row.id.clone(), (row.clone(), height, self.epoch));
        self.reported.insert(row.id.clone());
    }

    /// A row drawn in the chat at `height`: true when that is news (it
    /// differs from what the book holds for the row as it is now).
    pub fn shown(&mut self, rows: &VecModel<MessageRow>, id: &str, height: f32, width: f32) -> bool {
        if height <= 0.0 {
            return false;
        }
        self.at_width(width);
        let Some(row) = self.index.get(id).and_then(|&i| rows.row_data(i)).filter(|r| r.id == id) else { return false };
        if self.heights.get(id).is_some_and(|(seen, h, epoch)| *seen == row && *epoch == self.epoch && (h - height).abs() < 0.5) {
            return false;
        }
        self.heights.insert(row.id.clone(), (row, height, self.epoch));
        true
    }

    fn at_width(&mut self, width: f32) {
        if (width - self.width).abs() >= 0.5 {
            self.width = width;
            self.epoch += 1;
        }
    }

    /// Brings the book up to `rows`: publishes where each row starts (a row
    /// not measured as it is now counts as its old height, or else as the
    /// average of those measured) and, when no batch is out, sends the next
    /// rows to measure, the latest first. True while rows remain to be
    /// measured.
    pub fn update(&mut self, rows: &VecModel<MessageRow>) -> bool {
        let count = rows.row_count();
        let mut heights: Vec<Height> = Vec::with_capacity(count);
        let mut missing = Vec::new();
        let mut unknown = 0;
        let mut waiting = 0;
        let now = Instant::now();
        let mut stale = HashMap::new();
        self.index.clear();
        for i in 0..count {
            let Some(row) = rows.row_data(i) else { continue };
            match self.heights.get(&row.id) {
                Some((seen, height, epoch)) if *seen == row && *epoch == self.epoch => heights.push(Height::Measured(*height)),
                Some((_, height, _)) => {
                    heights.push(Height::Was(*height));
                    let since = *self.stale.get(&row.id).unwrap_or(&now);
                    stale.insert(row.id.clone(), since);
                    if now.duration_since(since) >= self.grace {
                        missing.push(i);
                    } else {
                        waiting += 1;
                    }
                }
                None => {
                    heights.push(Height::Unknown);
                    missing.push(i);
                    unknown += 1;
                }
            }
            self.index.insert(row.id.clone(), i);
        }
        self.stale = stale;
        (self.known, self.missing) = (heights.len() - missing.len() - waiting, missing.len() + waiting);
        match (self.missing, self.pass) {
            (0, Some(started)) => (self.pass, self.last_pass) = (None, Some(started.elapsed())),
            (n, None) if n > 0 => self.pass = Some(now),
            _ => {}
        }
        let index = &self.index;
        self.heights.retain(|id, _| index.contains_key(id));
        let before = starts(&heights);
        if self.before.iter().ne(before.iter().copied()) {
            self.before.set_vec(before);
        }
        if self.reported.len() < self.asked.len() && self.sent.is_some_and(|sent| sent.elapsed() < LATE) {
            return true;
        }
        if missing.is_empty() {
            self.asked.clear();
            self.reported.clear();
            self.sent = None;
            if self.measure.row_count() > 0 {
                self.measure.set_vec(Vec::new());
            }
            // Changed rows wait out their grace (or their drawn report).
            return waiting > 0;
        }
        self.batch += 1;
        let size = if unknown > 0 { BATCH } else { CHANGED_BATCH };
        let batch: Vec<MessageRow> = missing.iter().rev().take(size).filter_map(|&i| rows.row_data(i)).collect();
        self.asked = batch.iter().map(|row| (row.id.clone(), row.clone())).collect();
        self.reported.clear();
        self.sent = Some(Instant::now());
        self.measure.set_vec(batch.into_iter().map(|row| MeasureRow { row, batch: self.batch }).collect::<Vec<_>>());
        true
    }

    /// The row `y` px down the chat falls on.
    pub fn row_at(&self, y: f32) -> usize {
        let starts: Vec<f32> = self.before.iter().collect();
        let rows = starts.len().saturating_sub(1);
        starts.partition_point(|&start| start <= y).saturating_sub(1).min(rows.saturating_sub(1))
    }

    /// The height the book holds for row `id` (for the checks).
    pub fn height(&self, id: &str) -> Option<f32> {
        self.heights.get(id).map(|(_, h, _)| *h)
    }

    /// Rows measured as they are now, and rows waiting to be, at the last
    /// update (for the checks).
    pub fn progress(&self) -> (usize, usize) {
        (self.known, self.missing)
    }

    /// How long the last pass took to measure every row waiting (for the
    /// checks).
    pub fn last_pass(&self) -> Option<Duration> {
        self.last_pass
    }
}

/// A row's height as the book knows it.
#[derive(Clone, Copy)]
enum Height {
    Measured(f32),
    /// Measured before the row or the layout changed.
    Was(f32),
    Unknown,
}

/// Where each row starts, and the chat's height last: a row never measured
/// counts as the average of those measured.
fn starts(heights: &[Height]) -> Vec<f32> {
    let known: Vec<f32> = heights.iter().filter_map(|h| if let Height::Measured(h) = h { Some(*h) } else { None }).collect();
    let guess = if known.is_empty() { GUESS } else { known.iter().sum::<f32>() / known.len() as f32 };
    let mut y = 0.0;
    let mut starts = Vec::with_capacity(heights.len() + 1);
    starts.push(0.0);
    for height in heights {
        y += match height {
            Height::Measured(h) | Height::Was(h) => *h,
            Height::Unknown => guess,
        };
        starts.push(y);
    }
    starts
}

#[cfg(test)]
mod tests {
    use super::*;

    fn row(id: &str) -> MessageRow {
        MessageRow { id: id.into(), ..MessageRow::default() }
    }

    fn rows(count: usize) -> Rc<VecModel<MessageRow>> {
        Rc::new(VecModel::from((0..count).map(|i| row(&format!("r{i}"))).collect::<Vec<_>>()))
    }

    /// Measures every batch the book sends at `height(id)`, as the
    /// transcript does, until nothing is left.
    fn measure_all(book: &mut HeightBook, rows: &VecModel<MessageRow>, height: impl Fn(&str) -> f32) -> usize {
        let mut batches = 0;
        while book.update(rows) {
            let sent: Vec<MeasureRow> = book.measure.iter().collect();
            assert!(!sent.is_empty() && sent.len() <= BATCH, "a batch of up to {BATCH} rows: {}", sent.len());
            assert!(sent.iter().all(|m| book.height(&m.row.id).is_none()) || sent.len() <= CHANGED_BATCH, "rows that only changed go a couple at a time");
            // A row reports as it is created, then laid out.
            for m in sent {
                book.measured(m.batch, &m.row.id, 14.0, 800.0);
                book.measured(m.batch, &m.row.id, height(&m.row.id), 800.0);
            }
            batches += 1;
            assert!(batches < 100, "the book finishes");
        }
        batches
    }

    /// Every row is measured, the latest first, and the chat's height is
    /// their sum, whatever order the rows were drawn in.
    #[test]
    fn the_chat_is_as_tall_as_its_measured_rows() {
        let model = rows(40);
        let mut book = HeightBook::default();
        book.update(&model);
        let first: Vec<SharedString> = book.measure.iter().map(|m| m.row.id).collect();
        assert_eq!(first.first().map(|s| s.as_str()), Some("r39"), "the latest rows are measured first");
        let height = |id: &str| 50.0 + id[1..].parse::<f32>().unwrap_or(0.0);
        assert_eq!(measure_all(&mut book, &model, height), 5);
        let before: Vec<f32> = book.before.iter().collect();
        assert_eq!(before.len(), 41);
        let total: f32 = (0..40).map(|i| 50.0 + i as f32).sum();
        assert_eq!(before[40], total);
        assert_eq!(before[1], 50.0);
        assert!(!book.update(&model));
        assert_eq!(book.progress(), (40, 0));
    }

    /// A row that changes is measured again; the others keep their heights.
    #[test]
    fn a_changed_row_is_measured_again() {
        let model = rows(20);
        let mut book = HeightBook::default();
        measure_all(&mut book, &model, |_| 100.0);
        model.set_row_data(5, MessageRow { stamp: "12:00".into(), ..row("r5") });
        assert!(book.update(&model), "the changed row goes out");
        let sent: Vec<SharedString> = book.measure.iter().map(|m| m.row.id).collect();
        assert_eq!(sent, vec![SharedString::from("r5")]);
        measure_all(&mut book, &model, |id| if id == "r5" { 300.0 } else { 100.0 });
        assert_eq!(book.before.row_data(20), Some(19.0 * 100.0 + 300.0));
    }

    /// A changed row waits a moment before it is laid out off screen: on
    /// screen, it reports its drawn height first.
    #[test]
    fn a_changed_row_on_screen_reports_itself() {
        let model = rows(20);
        let mut book = HeightBook::new();
        measure_all(&mut book, &model, |_| 100.0);
        model.set_row_data(5, MessageRow { stamp: "12:00".into(), ..row("r5") });
        assert!(book.update(&model), "the changed row waits");
        assert_eq!(book.measure.row_count(), 0, "nothing is laid out off screen yet");
        assert_eq!(book.progress(), (19, 1));
        assert!(book.shown(&model, "r5", 250.0, 800.0));
        assert!(!book.update(&model));
        assert_eq!(book.before.row_data(20), Some(19.0 * 100.0 + 250.0));
    }

    /// Rows not measured yet count as the average of those that are, and a
    /// late report from an old batch is ignored.
    #[test]
    fn unmeasured_rows_count_as_the_average() {
        let model = rows(32);
        let mut book = HeightBook::default();
        book.update(&model);
        let sent: Vec<MeasureRow> = book.measure.iter().collect();
        for m in &sent {
            book.measured(m.batch, &m.row.id, 200.0, 800.0);
        }
        book.measured(sent[0].batch - 1, "r0", 5.0, 800.0);
        book.update(&model);
        assert_eq!(book.before.row_data(32), Some(32.0 * 200.0), "an old batch's report is ignored");
    }

    /// Another width measures everything again; meanwhile each row keeps
    /// its old height, so the chat's height holds.
    #[test]
    fn another_width_measures_again() {
        let model = rows(10);
        let mut book = HeightBook::default();
        measure_all(&mut book, &model, |_| 100.0);
        assert!(book.shown(&model, "r3", 100.0, 900.0), "a row drawn at another width is news");
        assert!(book.update(&model), "the rest are measured again");
        assert_eq!(book.progress(), (1, 9));
        assert_eq!(book.before.row_data(10), Some(1000.0), "old heights stand in");
        book.relayout();
        assert!(book.update(&model));
        assert_eq!(book.progress(), (0, 10), "a new text size measures every row again");
        assert_eq!(book.before.row_data(10), Some(1000.0));
    }

    #[test]
    fn row_at_finds_the_row_a_distance_falls_on() {
        let model = rows(4);
        let mut book = HeightBook::default();
        measure_all(&mut book, &model, |id| if id == "r1" { 300.0 } else { 100.0 });
        assert_eq!(book.row_at(0.0), 0);
        assert_eq!(book.row_at(99.0), 0);
        assert_eq!(book.row_at(100.0), 1);
        assert_eq!(book.row_at(399.0), 1);
        assert_eq!(book.row_at(400.0), 2);
        assert_eq!(book.row_at(10_000.0), 3);
        assert_eq!(book.row_at(-5.0), 0);
    }
}
