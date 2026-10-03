//! Finished turns in history (docs/live-items.md §7.6): each settled turn
//! of the `/log` rows folds behind `Worked for N · K tools` the way the
//! live fold does, with the same entries and keys, so a turn reads the same
//! while live items hold it, after the next turn takes over, and opened
//! cold. Pure: the window splices the pieces among the chat's rows.

use std::collections::HashSet;

use crate::json::Object;
use crate::live::LiveView;
use crate::live_present::Entry;
use crate::protocol::Message;

#[derive(Default)]
pub struct Options<'a> {
    /// The Host explains tool items (`tool_explanations.enabled`).
    pub explanations: bool,
    /// Entry keys the reader opened.
    pub expanded: HashSet<String>,
    /// The turn live items show and the rows they own: left to them.
    pub open_turn: String,
    pub held_rows: HashSet<String>,
    /// The agent works: the chat's last turn may not be over.
    pub busy: bool,
    /// A cached explanation of a `/log` tool call (never asks for one).
    pub explain: Option<&'a dyn Fn(&Object) -> String>,
}

/// One place in a folded turn: a durable row shown where it is, or an
/// entry (the fold, a tool) shown as a live row.
#[derive(Debug, Clone, PartialEq)]
pub enum Piece {
    Row(String),
    Entry(Entry),
}

#[derive(Debug, Clone, PartialEq, Default)]
pub struct Fold {
    /// `live:fold:<turn id>`, as the live fold's.
    pub key: String,
    /// The prompt that started the turn and the row after the turn.
    pub prompt: Option<String>,
    pub next: Option<String>,
    pub pieces: Vec<Piece>,
}

#[derive(Debug, Clone, Default)]
pub struct Folded {
    pub folds: Vec<Fold>,
    /// Rows the folds stand for: not shown themselves.
    pub hidden_rows: HashSet<String>,
    /// Tool and cell ids entries show: left out of the rows that stay.
    pub stripped_calls: HashSet<String>,
}

/// The settled turns of `rows` as folds.
pub fn present(_rows: &[Message], _live: Option<&LiveView>, _options: &Options) -> Folded {
    Folded::default()
}
