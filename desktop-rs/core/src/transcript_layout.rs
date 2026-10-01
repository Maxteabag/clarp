//! The geometry of the transcript view (C++ `TranscriptLayout`): rows laid
//! out from heights the view knows instead of from an average. Every row has
//! its own height, measured once its delegate exists at this width, otherwise
//! estimated from its own text. A row's top is the sum of the heights above
//! it, so a height that changes above the viewport never moves what is on
//! screen: the viewport is kept on an anchor (the row at its top and the
//! offset into it), or on the end while following.
//!
//! This is the Qt-free state. The Qt item creates and releases delegates,
//! measures them, and asks this where things go; row data (estimate text,
//! section labels, identities) is read through closures.

/// Rough monospace metrics for estimates; only the scrollbar ever sees them.
pub const ESTIMATED_CHAR_WIDTH: f64 = 7.8;
pub const ESTIMATED_LINE_HEIGHT: f64 = 19.0;
pub const ESTIMATED_ROW_CHROME: f64 = 30.0;
pub const ESTIMATED_SECTION_HEIGHT: f64 = 26.0;
pub const MAX_LAYOUT_PASSES: usize = 8;
/// Spare time per frame for rows in the margin around the viewport.
pub const MARGIN_CREATION_BUDGET_MS: i64 = 4;

/// Qt's `qFuzzyCompare` for doubles.
fn fuzzy_equal(a: f64, b: f64) -> bool {
    (a - b).abs() * 1_000_000_000_000.0 <= a.abs().min(b.abs())
}

#[derive(Debug, Clone, Default)]
pub struct Row {
    /// The row's block: section heading, then the delegate.
    pub height: f64,
    pub measured: bool,
    pub created: bool,
    pub section_created: bool,
    pub section_label: String,
    pub section_shown: bool,
}

/// What one layout pass should create and release (C++ `placeVisibleRows`).
#[derive(Debug, Clone, PartialEq, Default)]
pub struct Plan {
    pub first: usize,
    pub last: usize,
    pub visible_first: usize,
    pub visible_last: usize,
    /// Created rows outside `first..=last`.
    pub release: Vec<usize>,
    /// Rows on screen first, top to bottom, then the margin nearest first.
    pub order: Vec<usize>,
}

#[derive(Debug, Clone)]
pub struct Geometry {
    rows: Vec<Row>,
    /// `prefix[i]` = sum of block heights (plus spacing) before row i.
    prefix: Vec<f64>,
    prefix_dirty: bool,
    pub spacing: f64,
    pub left_margin: f64,
    pub right_margin: f64,
    pub top_margin: f64,
    pub bottom_margin: f64,
    pub cache_extent: f64,
    following: bool,
    pub has_section_delegate: bool,
    /// The measured section heading height, once one exists.
    section_height: Option<f64>,
    pub header_height: f64,
    pub footer_height: f64,
    width: f64,
    last_width: Option<f64>,
    anchor_row: Option<usize>,
    anchor_offset: f64,
    anchor_identity: String,
    anchor_identity_offset: f64,
}

impl Default for Geometry {
    fn default() -> Self {
        Self {
            rows: Vec::new(),
            prefix: vec![0.0],
            prefix_dirty: false,
            spacing: 0.0,
            left_margin: 0.0,
            right_margin: 0.0,
            top_margin: 0.0,
            bottom_margin: 0.0,
            cache_extent: 400.0,
            following: true,
            has_section_delegate: false,
            section_height: None,
            header_height: 0.0,
            footer_height: 0.0,
            width: 0.0,
            last_width: None,
            anchor_row: None,
            anchor_offset: 0.0,
            anchor_identity: String::new(),
            anchor_identity_offset: 0.0,
        }
    }
}

impl Geometry {
    pub fn count(&self) -> usize {
        self.rows.len()
    }
    pub fn rows(&self) -> &[Row] {
        &self.rows
    }
    pub fn following(&self) -> bool {
        self.following
    }
    pub fn anchor(&self) -> Option<(usize, f64)> {
        self.anchor_row.map(|row| (row, self.anchor_offset))
    }
    pub fn width(&self) -> f64 {
        self.width
    }
    pub fn row_width(&self) -> f64 {
        (self.width - self.left_margin - self.right_margin).max(0.0)
    }
    fn section_height_or_estimate(&self) -> f64 {
        self.section_height.unwrap_or(ESTIMATED_SECTION_HEIGHT)
    }

    pub fn invalidate(&mut self) {
        self.prefix_dirty = true;
    }

    /// Height estimate for a row with this text at the current width.
    pub fn estimate(&self, text: &str) -> f64 {
        let per_line = (self.row_width() / ESTIMATED_CHAR_WIDTH).max(10.0);
        if text.is_empty() {
            return ESTIMATED_ROW_CHROME;
        }
        let lines: f64 = text.split('\n').map(|line| (line.encode_utf16().count() as f64 / per_line).ceil().max(1.0)).sum();
        ESTIMATED_ROW_CHROME + lines * ESTIMATED_LINE_HEIGHT
    }

    fn rebuild_prefix(&mut self) {
        if !self.prefix_dirty && self.prefix.len() == self.rows.len() + 1 {
            return;
        }
        self.prefix.clear();
        self.prefix.push(0.0);
        let mut sum = 0.0;
        for row in &self.rows {
            sum += row.height + self.spacing;
            self.prefix.push(sum);
        }
        self.prefix_dirty = false;
    }

    pub fn rows_top(&self) -> f64 {
        self.top_margin + self.header_height
    }

    pub fn content_height(&mut self) -> f64 {
        self.rebuild_prefix();
        self.rows_top() + self.prefix[self.rows.len()] + self.footer_height + self.bottom_margin
    }

    pub fn measured_count(&self) -> usize {
        self.rows.iter().filter(|row| row.measured).count()
    }

    /// Top of a row's block (its section heading, if any, then the row).
    pub fn position_of(&mut self, row: i64) -> f64 {
        self.rebuild_prefix();
        if self.rows.is_empty() {
            return self.rows_top();
        }
        let row = row.clamp(0, self.rows.len() as i64) as usize;
        self.rows_top() + self.prefix[row]
    }

    pub fn height_of(&self, row: i64) -> f64 {
        usize::try_from(row).ok().and_then(|r| self.rows.get(r)).map_or(0.0, |r| r.height)
    }

    pub fn is_measured(&self, row: i64) -> bool {
        usize::try_from(row).ok().and_then(|r| self.rows.get(r)).is_some_and(|r| r.measured)
    }

    /// The row whose block contains `y` (content coordinates), clamped.
    pub fn row_at(&mut self, y: f64) -> Option<usize> {
        self.rebuild_prefix();
        if self.rows.is_empty() {
            return None;
        }
        let local = y - self.rows_top();
        // The last prefix entry not above local: the row whose block holds y.
        let after = self.prefix.partition_point(|&top| top <= local);
        Some((after as i64 - 1).clamp(0, self.rows.len() as i64 - 1) as usize)
    }

    /// The end of the content, where a following viewport sits.
    pub fn end_y(&mut self, viewport_height: f64) -> f64 {
        (self.content_height() - viewport_height).max(0.0)
    }

    /// The reader moved the viewport: remember where it is now.
    pub fn anchor_to_viewport(&mut self, viewport_y: f64) {
        match self.row_at(viewport_y) {
            None => self.anchor_row = None,
            Some(row) => {
                self.anchor_row = Some(row);
                self.anchor_offset = viewport_y - self.position_of(row as i64);
            }
        }
    }

    /// Returns whether `following` changed.
    pub fn set_following(&mut self, following: bool, viewport_y: f64) -> bool {
        if self.following == following {
            return false;
        }
        self.following = following;
        if !following {
            self.anchor_to_viewport(viewport_y);
        }
        true
    }

    /// Where the viewport goes to put `row` at its top (or bottom); stops
    /// following. Returns the target and whether `following` changed. The
    /// caller moves the viewport, then calls `anchor_to_viewport`.
    pub fn position_at_row(&mut self, row: i64, at_bottom: bool, viewport_height: f64) -> Option<(f64, bool)> {
        if self.rows.is_empty() {
            return None;
        }
        let row = row.clamp(0, self.rows.len() as i64 - 1);
        let stopped = std::mem::replace(&mut self.following, false);
        let top = self.position_of(row);
        let target = if at_bottom { top + self.height_of(row) - viewport_height } else { top };
        Some((target.clamp(0.0, self.end_y(viewport_height)), stopped))
    }

    /// Where the viewport belongs now (C++ `correctViewport`): on the end
    /// while following, else on the anchor, clamped to the content.
    pub fn correct_target(&mut self, viewport_y: f64, viewport_height: f64) -> f64 {
        let mut target = viewport_y;
        if self.following {
            target = self.end_y(viewport_height);
        } else if let Some(row) = self.anchor_row.filter(|&row| row < self.rows.len()) {
            target = self.position_of(row as i64) + self.anchor_offset;
        }
        target.clamp(0.0, self.end_y(viewport_height))
    }

    /// The layout width changed. Rows that are not on screen keep their
    /// height at the old width as an estimate, scaled to the new one;
    /// created rows re-measure.
    pub fn set_width(&mut self, width: f64) {
        if let Some(last) = self.last_width.filter(|&last| !fuzzy_equal(last, width) && last > 0.0) {
            let old_row_width = (last - self.left_margin - self.right_margin).max(1.0);
            self.width = width;
            let ratio = old_row_width / self.row_width().max(1.0);
            for row in &mut self.rows {
                if row.created {
                    continue;
                }
                if row.measured {
                    row.height *= ratio;
                }
                row.measured = false;
            }
            self.prefix_dirty = true;
        }
        self.width = width;
        self.last_width = Some(width);
    }

    fn update_section_flags(&mut self, from: usize) {
        let section_height = self.section_height_or_estimate();
        for i in from..self.rows.len() {
            let shown = self.has_section_delegate
                && !self.rows[i].section_label.is_empty()
                && (i == 0 || self.rows[i].section_label != self.rows[i - 1].section_label);
            let row = &mut self.rows[i];
            if shown == row.section_shown {
                continue;
            }
            row.section_shown = shown;
            if !row.created {
                row.height += if shown { section_height } else { -section_height };
            }
            self.prefix_dirty = true;
        }
    }

    /// Rebuild every row (a model reset or a new model). The caller has
    /// released every delegate first.
    pub fn reset(&mut self, count: usize, text: impl Fn(usize) -> String, section: impl Fn(usize) -> String) {
        self.rows = (0..count)
            .map(|i| Row { height: self.estimate(&text(i)), section_label: section(i), ..Row::default() })
            .collect();
        self.update_section_flags(0);
        self.prefix_dirty = true;
        if self.anchor_row.is_some_and(|row| row >= count) {
            self.anchor_row = count.checked_sub(1);
        }
    }

    pub fn clear_anchor(&mut self) {
        self.anchor_row = None;
    }

    /// Around a refresh that rebuilds the rows: remember the reader's message.
    pub fn remember_anchor_identity(&mut self, identity: impl Fn(usize) -> String) {
        self.anchor_identity.clear();
        let Some(row) = self.anchor_row.filter(|&row| !self.following && row < self.rows.len()) else { return };
        self.anchor_identity = identity(row);
        self.anchor_identity_offset = self.anchor_offset;
    }

    pub fn restore_anchor_identity(&mut self, identity: impl Fn(usize) -> String) {
        if self.anchor_identity.is_empty() {
            return;
        }
        if let Some(row) = (0..self.rows.len()).find(|&i| identity(i) == self.anchor_identity) {
            self.anchor_row = Some(row);
            self.anchor_offset = self.anchor_identity_offset;
        }
        self.anchor_identity.clear();
    }

    pub fn inserted(&mut self, first: usize, last: usize, text: impl Fn(usize) -> String, section: impl Fn(usize) -> String, identity: impl Fn(usize) -> String) {
        let added = last - first + 1;
        let rows: Vec<Row> = (first..=last)
            .map(|i| Row { height: self.estimate(&text(i)), section_label: section(i), ..Row::default() })
            .collect();
        self.rows.splice(first..first, rows);
        // Rows inserted above the reader push the anchor down by the same
        // rows, so what is on screen stays where it is.
        if let Some(anchor) = self.anchor_row.filter(|&row| row >= first) {
            self.anchor_row = Some(anchor + added);
        }
        if !self.anchor_identity.is_empty()
            && let Some(row) = (first..=last).find(|&i| identity(i) == self.anchor_identity)
        {
            self.anchor_row = Some(row);
            self.anchor_offset = self.anchor_identity_offset;
            self.anchor_identity.clear();
        }
        self.update_section_flags(first);
        self.prefix_dirty = true;
    }

    /// Before rows go: if the reader's row is among them, remember which
    /// message it was, so the reader comes back with it (a clear and
    /// re-append). The caller releases the rows' delegates.
    pub fn about_to_remove(&mut self, first: usize, last: usize, identity: impl Fn(usize) -> String) {
        let anchor_in_range = self.anchor_row.is_some_and(|row| row >= first && row <= last);
        if !self.following && anchor_in_range && self.anchor_identity.is_empty() {
            self.remember_anchor_identity(identity);
        }
    }

    pub fn removed(&mut self, first: usize, last: usize) {
        if self.rows.is_empty() {
            return;
        }
        let last = last.min(self.rows.len() - 1);
        if first > last {
            return;
        }
        let removed = last - first + 1;
        self.rows.drain(first..=last);
        match self.anchor_row {
            Some(anchor) if anchor > last => self.anchor_row = Some(anchor - removed),
            Some(anchor) if anchor >= first => {
                // The row the reader was on is gone: keep the one that took its place.
                self.anchor_row = self.rows.len().checked_sub(1).map(|end| first.min(end));
                self.anchor_offset = 0.0;
            }
            _ => {}
        }
        self.update_section_flags(first);
        self.prefix_dirty = true;
    }

    /// Rows `first..=last` changed. Uncreated, unmeasured rows re-estimate
    /// when their text changed; returns the rows whose section label changed
    /// and that have a heading item (the caller updates it).
    pub fn data_changed(
        &mut self,
        first: usize,
        last: usize,
        text_changed: bool,
        section_changed: bool,
        text: impl Fn(usize) -> String,
        section: impl Fn(usize) -> String,
    ) -> Vec<usize> {
        let mut relabelled = Vec::new();
        for i in first..(last + 1).min(self.rows.len()) {
            if !self.rows[i].created && !self.rows[i].measured && text_changed {
                let heading = if self.rows[i].section_shown { self.section_height_or_estimate() } else { 0.0 };
                self.rows[i].height = self.estimate(&text(i)) + heading;
                self.prefix_dirty = true;
            }
            if section_changed {
                let label = section(i);
                if label != self.rows[i].section_label {
                    self.rows[i].section_label = label;
                    if self.rows[i].section_created {
                        relabelled.push(i);
                    }
                    self.update_section_flags(i);
                }
            }
        }
        relabelled
    }

    /// What to release and create for the viewport at `viewport_y`.
    pub fn plan(&mut self, viewport_y: f64, viewport_height: f64) -> Option<Plan> {
        let bottom = viewport_y + viewport_height;
        let first = self.row_at(viewport_y - self.cache_extent)?;
        let last = self.row_at(bottom + self.cache_extent)?;
        let visible_first = self.row_at(viewport_y)?;
        let visible_last = self.row_at(bottom)?;
        let release = (0..self.rows.len())
            .filter(|&i| (i < first || i > last) && (self.rows[i].created || self.rows[i].section_created))
            .collect();
        let mut order: Vec<usize> = (visible_first..=visible_last).collect();
        let mut step = 1;
        while visible_first >= first + step || visible_last + step <= last {
            if visible_last + step <= last {
                order.push(visible_last + step);
            }
            if visible_first >= first + step {
                order.push(visible_first - step);
            }
            step += 1;
        }
        Some(Plan { first, last, visible_first, visible_last, release, order })
    }

    pub fn set_created(&mut self, row: usize, item: bool, section: bool) {
        if let Some(row) = self.rows.get_mut(row) {
            row.created = item;
            row.section_created = section;
        }
    }

    /// The first section heading created sets the heading height used for
    /// estimates from then on.
    pub fn section_measured(&mut self, height: f64) {
        self.section_height = Some(height);
    }

    /// A created row's measured block height. Returns whether it moved rows.
    pub fn measured(&mut self, row: usize, height: f64) -> bool {
        let Some(row) = self.rows.get_mut(row) else { return false };
        let moved = (height - row.height).abs() > 0.5;
        if !row.measured || moved {
            row.height = height;
            row.measured = true;
            self.prefix_dirty = true;
        }
        moved
    }

    /// Where the footer goes: after the last row.
    pub fn footer_y(&mut self) -> f64 {
        self.rebuild_prefix();
        self.rows_top() + self.prefix[self.rows.len()]
    }
}
