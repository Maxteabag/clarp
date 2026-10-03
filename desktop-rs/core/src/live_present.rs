//! How live items read in a transcript (docs/live-items.md §7.3–7.6).

use std::collections::{HashMap, HashSet};

use crate::live::LiveView;
use crate::protocol::Message;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum Kind {
    #[default]
    Message,
    Reasoning,
    Tool,
    Explore,
    Plan,
    Diff,
    Compaction,
    Fold,
}

#[derive(Debug, Clone, PartialEq, Default)]
pub struct Entry {
    pub key: String,
    pub kind: Kind,
    pub items: Vec<String>,
    pub status: String,
    pub title: String,
    pub meta: String,
    pub secondary: String,
    pub reserve_secondary: bool,
    pub lines: Vec<String>,
    pub more: String,
    pub text: String,
    pub phase: String,
    pub expandable: bool,
    pub expanded: bool,
    pub rev: i64,
}

#[derive(Debug, Clone, Default)]
pub struct Options {
    pub explanations: bool,
    pub expanded: HashSet<String>,
    pub host_now_ms: i64,
}

#[derive(Debug, Clone, Default)]
pub struct Presented {
    pub entries: Vec<Entry>,
    pub hidden_rows: Vec<String>,
    pub taken_over: HashMap<String, String>,
}

#[derive(Debug, Clone, PartialEq, Default)]
pub struct StatusLine {
    pub text: String,
    pub busy: bool,
    pub state: String,
}

pub fn present(_view: &LiveView, _rows: &[Message], _options: &Options) -> Presented {
    Presented::default()
}

pub fn status_line(_view: &LiveView, _host_now_ms: i64) -> Option<StatusLine> {
    None
}
