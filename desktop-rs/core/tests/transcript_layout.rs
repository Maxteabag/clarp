//! The C++ tst_transcript_layout scenarios over the Qt-free geometry. A small
//! driver plays the Qt item: it runs the relayout passes, "creates" rows the
//! plan asks for and reports their delegate heights, and holds the viewport.

use clarp_core::transcript_layout::{Geometry, MAX_LAYOUT_PASSES};

const VIEW_WIDTH: f64 = 320.0;
const VIEW_HEIGHT: f64 = 240.0;

struct Driver {
    geometry: Geometry,
    /// messageId and delegate height per model row.
    rows: Vec<(String, f64)>,
    viewport_y: f64,
}

impl Driver {
    fn new() -> Self {
        let mut geometry = Geometry::default();
        geometry.cache_extent = 120.0;
        geometry.set_width(VIEW_WIDTH);
        let mut driver = Self { geometry, rows: Vec::new(), viewport_y: 0.0 };
        driver.reset();
        driver
    }

    fn id(&self, row: usize) -> String {
        self.rows[row].0.clone()
    }

    fn reset(&mut self) {
        let ids: Vec<String> = self.rows.iter().map(|r| r.0.clone()).collect();
        self.geometry.reset(ids.len(), |i| ids[i].clone(), |_| String::new());
    }

    fn insert(&mut self, at: usize, rows: Vec<(String, f64)>) {
        let last = at + rows.len() - 1;
        self.rows.splice(at..at, rows);
        let ids: Vec<String> = self.rows.iter().map(|r| r.0.clone()).collect();
        self.geometry.inserted(at, last, |i| ids[i].clone(), |_| String::new(), |i| ids[i].clone());
        self.settle();
    }

    fn append(&mut self, count: usize, prefix: &str, height: f64) {
        let at = self.rows.len();
        self.insert(at, (0..count).map(|i| (format!("{prefix}{i}"), height)).collect());
    }

    fn remove(&mut self, at: usize, count: usize) {
        let ids: Vec<String> = self.rows.iter().map(|r| r.0.clone()).collect();
        self.geometry.about_to_remove(at, at + count - 1, |i| ids[i].clone());
        self.rows.drain(at..at + count);
        self.geometry.removed(at, at + count - 1);
        self.settle();
    }

    fn set_height(&mut self, row: usize, height: f64) {
        self.rows[row].1 = height;
        self.settle();
    }

    /// C++ `relayout`: correct the viewport, place rows, repeat until stable.
    fn relayout(&mut self) {
        for _ in 0..MAX_LAYOUT_PASSES {
            self.correct();
            if !self.place() {
                break;
            }
        }
        self.correct();
    }

    fn correct(&mut self) {
        let target = self.geometry.correct_target(self.viewport_y, VIEW_HEIGHT);
        if (target - self.viewport_y).abs() > 0.5 {
            self.viewport_y = target;
        }
    }

    fn place(&mut self) -> bool {
        let Some(plan) = self.geometry.plan(self.viewport_y, VIEW_HEIGHT) else { return false };
        for row in plan.release {
            self.geometry.set_created(row, false, false);
        }
        let mut moved = false;
        for row in plan.order {
            self.geometry.set_created(row, true, false);
            moved |= self.geometry.measured(row, self.rows[row].1);
        }
        moved
    }

    fn settle(&mut self) {
        for _ in 0..4 {
            self.relayout();
        }
    }

    fn position_at_row(&mut self, row: i64) {
        let (target, _) = self.geometry.position_at_row(row, false, VIEW_HEIGHT).unwrap();
        self.viewport_y = target;
        self.geometry.anchor_to_viewport(self.viewport_y);
        self.settle();
    }

    fn index_of(&self, id: &str) -> usize {
        self.rows.iter().position(|r| r.0 == id).unwrap()
    }

    fn screen_offset(&mut self, row: usize) -> f64 {
        self.geometry.position_of(row as i64) - self.viewport_y
    }

    fn end_y(&mut self) -> f64 {
        self.geometry.end_y(VIEW_HEIGHT)
    }
}

fn near(actual: f64, expected: f64, what: &str) {
    assert!((actual - expected).abs() <= 1.0, "{what}: actual={actual} expected={expected}");
}

#[test]
fn following_tracks_end_after_model_and_height_changes() {
    let mut f = Driver::new();
    f.append(20, "m", 30.0);
    let end = f.end_y();
    near(f.viewport_y, end, "initial following end");
    f.append(5, "a", 32.0);
    let end = f.end_y();
    near(f.viewport_y, end, "following after append");
    f.set_height(22, 90.0);
    let end = f.end_y();
    near(f.viewport_y, end, "following after row growth");
    f.remove(4, 3);
    let end = f.end_y();
    near(f.viewport_y, end, "following after removals");
}

#[test]
fn anchor_survives_changes_above_viewport() {
    let mut f = Driver::new();
    f.append(80, "m", 30.0);
    f.position_at_row(40);
    let offset = f.screen_offset(f.index_of("m40"));
    f.insert(10, vec![("inserted".into(), 44.0)]);
    let row = f.index_of("m40");
    near(f.screen_offset(row), offset, "insert above anchor");
    f.set_height(5, 140.0);
    let row = f.index_of("m40");
    near(f.screen_offset(row), offset, "grow above anchor");
    f.set_height(5, 18.0);
    let row = f.index_of("m40");
    near(f.screen_offset(row), offset, "shrink above anchor");
    f.insert(0, (0..300).map(|i| (format!("p{i}"), 21.0)).collect());
    let row = f.index_of("m40");
    near(f.screen_offset(row), offset, "prepend many rows");
    f.remove(20, 30);
    let row = f.index_of("m40");
    near(f.screen_offset(row), offset, "remove rows above anchor");
}

#[test]
fn model_reset_keeps_same_message_id() {
    let mut f = Driver::new();
    f.append(60, "m", 30.0);
    f.position_at_row(30);
    let offset = f.screen_offset(f.index_of("m30"));
    let ids: Vec<String> = (0..60).map(|i| f.id(i)).collect();
    f.geometry.remember_anchor_identity(|i| ids[i].clone());
    f.rows = (0..70).map(|i| (if i == 44 { "m30".into() } else { format!("r{i}") }, 34.0)).collect();
    f.reset();
    let ids: Vec<String> = (0..70).map(|i| f.id(i)).collect();
    f.geometry.restore_anchor_identity(|i| ids[i].clone());
    f.settle();
    assert_eq!(f.index_of("m30"), 44);
    near(f.screen_offset(44), offset, "model reset anchor identity");
}

#[test]
fn width_change_keeps_anchor_on_screen() {
    let mut f = Driver::new();
    f.append(100, "m", 30.0);
    f.position_at_row(50);
    f.geometry.set_width(520.0);
    f.settle();
    let row = f.index_of("m50");
    let offset = f.screen_offset(row);
    assert!((-1.0..=VIEW_HEIGHT - 1.0).contains(&offset), "anchored row should remain on screen, offset={offset}");
}

#[test]
fn position_at_row_stays_at_top_after_neighbour_measurement() {
    let mut f = Driver::new();
    f.append(120, "m", 30.0);
    f.position_at_row(70);
    let top = f.geometry.position_of(70);
    near(f.viewport_y, top, "row positioned at top");
    f.rows[69].1 = 105.0;
    f.set_height(71, 64.0);
    let top = f.geometry.position_of(70);
    near(f.viewport_y, top, "row remains at top");
}

#[test]
fn rows_far_from_viewport_are_not_created() {
    let mut f = Driver::new();
    f.append(120, "m", 30.0);
    f.position_at_row(40);
    let (top, bottom) = (f.viewport_y, f.viewport_y + VIEW_HEIGHT);
    for i in 0..f.geometry.count() {
        if !f.geometry.rows()[i].created {
            continue;
        }
        let row_top = f.geometry.position_of(i as i64);
        let row_bottom = row_top + f.geometry.height_of(i as i64);
        assert!(row_bottom >= top - 2.0 * VIEW_HEIGHT && row_top <= bottom + 2.0 * VIEW_HEIGHT, "row {i} created far away");
    }
    assert!(!f.geometry.rows()[0].created);
    assert!(!f.geometry.rows()[119].created);
}

#[test]
fn created_rows_match_delegate_heights_and_are_contiguous() {
    let mut f = Driver::new();
    f.append(50, "m", 30.0);
    f.position_at_row(20);
    let mut previous: Option<usize> = None;
    for i in 0..f.geometry.count() {
        if !f.geometry.rows()[i].created {
            continue;
        }
        near(f.geometry.height_of(i as i64), f.rows[i].1, "height equals delegate height");
        if let Some(p) = previous {
            let expected = f.geometry.position_of(p as i64) + f.geometry.height_of(p as i64);
            near(f.geometry.position_of(i as i64), expected, "created rows are contiguous");
        }
        previous = Some(i);
    }
    assert!(previous.is_some());
}

#[test]
fn sections_add_a_heading_to_the_first_row_of_each_label() {
    let mut geometry = Geometry::default();
    geometry.has_section_delegate = true;
    geometry.set_width(VIEW_WIDTH);
    let labels = ["Mon", "Mon", "Tue"];
    geometry.reset(3, |_| String::new(), |i| labels[i].into());
    let shown: Vec<bool> = geometry.rows().iter().map(|r| r.section_shown).collect();
    assert_eq!(shown, [true, false, true]);
    assert!(geometry.height_of(0) > geometry.height_of(1));
}
