//! The rows the transcript view shows (C++ `TranscriptRows`). Most messages
//! are one row. A large finished assistant message is split into several rows
//! at markdown block boundaries, and a large table into chunks of rows that
//! repeat its header, so the view only lays out the parts on screen. Every
//! part carries its message's roles; the model adds the part's own body,
//! `fullBody`, `partIndex`/`partCount`, and a `rowKey` for anchoring.

use crate::text::markdown_display_blocks;

/// Split only what is expensive to lay out as one text document.
pub const SPLIT_ABOVE_CHARACTERS: usize = 6_000;
pub const SPLIT_TABLES_ABOVE_ROWS: usize = 24;
pub const PART_CHARACTERS: usize = 2_500;
pub const TABLE_ROWS_PER_PART: usize = 16;

/// Lengths in UTF-16 code units, as the C++ `QString::size` counts them.
fn qlen(text: &str) -> usize {
    text.encode_utf16().count()
}

fn is_table_block(lines: &[&str]) -> bool {
    lines.len() >= 3 && lines[0].trim().starts_with('|') && lines[1].contains("---")
}

fn is_big_table(lines: &[&str]) -> bool {
    is_table_block(lines) && lines.len() - 2 > SPLIT_TABLES_ABOVE_ROWS
}

/// How a message is split. Joining the parts with blank lines gives back the
/// markdown (tables aside, whose chunks repeat the header).
pub fn split_markdown(markdown: &str) -> Vec<String> {
    let blocks = markdown_display_blocks(markdown);
    let big_table = blocks.iter().any(|block| is_big_table(&block.split('\n').collect::<Vec<_>>()));
    if qlen(markdown) <= SPLIT_ABOVE_CHARACTERS && !big_table {
        return vec![markdown.to_owned()];
    }
    let mut parts = Vec::new();
    let mut pending = String::new();
    for block in &blocks {
        let lines: Vec<&str> = block.split('\n').collect();
        if is_big_table(&lines) {
            if !pending.is_empty() {
                parts.push(std::mem::take(&mut pending));
            }
            let header = format!("{}\n{}", lines[0], lines[1]);
            for chunk in lines[2..].chunks(TABLE_ROWS_PER_PART) {
                parts.push(format!("{header}\n{}", chunk.join("\n")));
            }
            continue;
        }
        if !pending.is_empty() && qlen(&pending) + qlen(block) > PART_CHARACTERS {
            parts.push(std::mem::take(&mut pending));
        }
        if !pending.is_empty() {
            pending.push_str("\n\n");
        }
        pending.push_str(block);
    }
    if !pending.is_empty() {
        parts.push(pending);
    }
    if parts.is_empty() { vec![markdown.to_owned()] } else { parts }
}

/// What splitting needs to know about one source row.
#[derive(Debug, Clone, Default)]
pub struct Source {
    pub id: String,
    pub body: String,
    pub kind: String,
    pub author: String,
    pub activity: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Part {
    pub source: usize,
    pub part: usize,
    pub parts: usize,
    /// The part's markdown when split, else empty (the message body is used).
    pub body: String,
}

impl Part {
    fn whole(source: usize) -> Self {
        Self { source, part: 0, parts: 1, body: String::new() }
    }

    /// Identifies the part across refreshes: the message id, with `#n` for
    /// the parts of a split message.
    pub fn row_key(&self, message_id: &str) -> String {
        if self.parts > 1 { format!("{message_id}#{}", self.part) } else { message_id.to_owned() }
    }
}

pub fn parts_for(source_row: usize, source: &Source) -> Vec<Part> {
    let splittable = !source.activity && source.kind != "live" && source.author == "assistant";
    if !splittable
        || (qlen(&source.body) <= SPLIT_ABOVE_CHARACTERS && !source.body.contains("|---") && !source.body.contains("| ---"))
    {
        return vec![Part::whole(source_row)];
    }
    let parts = split_markdown(&source.body);
    if parts.len() <= 1 {
        return vec![Part::whole(source_row)];
    }
    let count = parts.len();
    parts.into_iter().enumerate().map(|(part, body)| Part { source: source_row, part, parts: count, body }).collect()
}

/// A change the Qt adapter replays between the matching notifications.
#[derive(Debug, Clone, PartialEq)]
pub enum Change {
    Reset,
    Insert { at: usize, count: usize },
    Remove { at: usize, count: usize },
    /// Rows `first..=last` changed; `all_roles` when the source said so or
    /// the part changed, else only the source's roles.
    Update { first: usize, last: usize, all_roles: bool },
}

#[derive(Debug, Default)]
pub struct TranscriptRows {
    rows: Vec<Part>,
}

impl TranscriptRows {
    pub fn rows(&self) -> &[Part] {
        &self.rows
    }
    pub fn count(&self) -> usize {
        self.rows.len()
    }

    fn first_row_of(&self, source_row: usize) -> usize {
        self.rows.partition_point(|row| row.source < source_row)
    }

    fn rows_of(&self, source_row: usize) -> usize {
        self.rows[self.first_row_of(source_row)..].iter().take_while(|row| row.source == source_row).count()
    }

    /// The first view row of a source row, for jumping to a message.
    pub fn row_for_source(&self, source_row: usize) -> Option<usize> {
        let row = self.first_row_of(source_row);
        (self.rows.get(row)?.source == source_row).then_some(row)
    }

    pub fn source_row(&self, row: usize) -> Option<usize> {
        self.rows.get(row).map(|r| r.source)
    }

    /// `source(row)` reads one source row; only the rows a change touches
    /// are read.
    pub fn rebuild(&mut self, count: usize, source: impl Fn(usize) -> Source) -> Change {
        self.rows = (0..count).flat_map(|row| parts_for(row, &source(row))).collect();
        Change::Reset
    }

    fn shift_sources(&mut self, from: usize, delta: isize) {
        for row in &mut self.rows {
            if row.source >= from {
                row.source = row.source.saturating_add_signed(delta);
            }
        }
    }

    /// Source rows `first..=last` were inserted; `source` reads the source
    /// after the insert.
    pub fn inserted(&mut self, first: usize, last: usize, source: impl Fn(usize) -> Source) -> Vec<Change> {
        let at = self.first_row_of(first);
        self.shift_sources(first, (last - first + 1) as isize);
        let parts: Vec<Part> = (first..=last).flat_map(|row| parts_for(row, &source(row))).collect();
        let count = parts.len();
        self.rows.splice(at..at, parts);
        vec![Change::Insert { at, count }]
    }

    pub fn removed(&mut self, first: usize, last: usize) -> Vec<Change> {
        let from = self.first_row_of(first);
        let to = self.first_row_of(last + 1);
        self.rows.drain(from..to);
        self.shift_sources(last + 1, -((last - first + 1) as isize));
        if to > from { vec![Change::Remove { at: from, count: to - from }] } else { Vec::new() }
    }

    /// Source rows `first..=last` changed. `split_may_change` when the body,
    /// kind or activity changed (or the roles are unknown).
    pub fn data_changed(&mut self, first: usize, last: usize, source: impl Fn(usize) -> Source, split_may_change: bool, all_roles: bool) -> Vec<Change> {
        let mut changes = Vec::new();
        for source_row in first..=last {
            let start = self.first_row_of(source_row);
            let existing = self.rows_of(source_row);
            if !split_may_change {
                if existing > 0 {
                    changes.push(Change::Update { first: start, last: start + existing - 1, all_roles });
                }
                continue;
            }
            let parts = parts_for(source_row, &source(source_row));
            let wanted = parts.len();
            if wanted == existing {
                self.rows.splice(start..start + existing, parts);
                if existing > 0 {
                    changes.push(Change::Update { first: start, last: start + existing - 1, all_roles });
                }
                continue;
            }
            // The message now splits differently (it finished streaming, or
            // grew past the threshold): keep the rows that stay and add or
            // drop the rest, so the view keeps the first part's delegate.
            let kept = existing.min(wanted);
            let mut parts = parts.into_iter();
            for (offset, part) in parts.by_ref().take(kept).enumerate() {
                self.rows[start + offset] = part;
            }
            if kept > 0 {
                changes.push(Change::Update { first: start, last: start + kept - 1, all_roles: true });
            }
            if wanted > existing {
                let extra: Vec<Part> = parts.collect();
                let count = extra.len();
                self.rows.splice(start + existing..start + existing, extra);
                changes.push(Change::Insert { at: start + existing, count });
            } else {
                self.rows.drain(start + wanted..start + existing);
                changes.push(Change::Remove { at: start + wanted, count: existing - wanted });
            }
        }
        changes
    }
}
