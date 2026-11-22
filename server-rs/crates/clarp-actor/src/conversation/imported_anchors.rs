//! Conversation anchors for immutable history brought over from another Host.

use std::cell::OnceCell;
use std::collections::HashMap;

use clarp_kernel::artifacts::forms::is_answer_row;
use clarp_kernel::chat_view::imported_is_conversation;
use clarp_kernel::conversation::{Entry, Imported};
use clarp_kernel::coordination::NOTE_KIND;
use clarp_kernel::journal::kind;
use clarp_ports::log::AgentLog;

use super::{CHUNK, corrupt};
use crate::ActorError;

/// Derive once per actor, on the first read that reaches an imported row. The
/// imported log prefix is immutable, including after the agent starts new turns.
#[derive(Default)]
pub(crate) struct ImportedAnchors(OnceCell<HashMap<String, usize>>);

impl ImportedAnchors {
    pub(super) fn count(
        &self,
        log: &dyn AgentLog,
        row: &Imported,
    ) -> Result<Option<usize>, ActorError> {
        if trace_of(row).is_none() || !row.row["session_rows"].is_null() {
            return Ok(None);
        }
        let anchors = match self.0.get() {
            Some(anchors) => anchors,
            None => {
                let anchors = collect(log)?;
                self.0.get_or_init(|| anchors)
            }
        };
        Ok(anchors.get(&row.id).copied())
    }
}

#[derive(Default)]
struct Turn {
    settled: bool,
    stamped: bool,
    reply: Option<String>,
    fallback: Option<String>,
    session_rows: usize,
}

impl Turn {
    fn add(&mut self, imported: Imported) {
        let Imported { id, row } = imported;
        if let Some(status) = row["turn"]["status"]
            .as_str()
            .filter(|value| !value.is_empty())
        {
            self.settled = status != "replaced";
        }
        self.stamped |= !row["session_rows"].is_null();
        if is_answer_row(&id) {
            return;
        }
        if imported_is_conversation(&row) {
            if row["role"] == "user" || row["kind"] == NOTE_KIND {
                self.fallback = Some(id);
            }
        } else {
            self.session_rows += 1;
            if row["role"] == "assistant"
                && row["kind"] != NOTE_KIND
                && row["origin"] != "system"
                && row["tool_name"].as_str().unwrap_or_default().is_empty()
                && row["text"]
                    .as_str()
                    .is_some_and(|text| !text.trim().is_empty())
            {
                self.reply = Some(id);
            }
        }
    }

    fn anchor(self) -> Option<(String, usize)> {
        if !self.settled || self.stamped {
            return None;
        }
        if let Some(reply) = self.reply {
            Some((reply, self.session_rows - 1))
        } else {
            self.fallback.map(|id| (id, self.session_rows))
        }
    }
}

fn trace_of(row: &Imported) -> Option<&str> {
    row.row["trace_id"]
        .as_str()
        .filter(|value| !value.is_empty())
}

fn collect(log: &dyn AgentLog) -> Result<HashMap<String, usize>, ActorError> {
    let mut turns: HashMap<String, Turn> = HashMap::new();
    let mut after = 0;
    'prefix: loop {
        let items = log.read_after(after, kind::ROWS, CHUNK)?;
        let whole = items.len() == CHUNK;
        for item in items {
            if item.kind != kind::IMPORTED {
                break 'prefix;
            }
            after = item.seq;
            if let Some(Entry::Imported(row)) =
                Entry::from_item(&item).map_err(|why| corrupt(&item, why))?
                && let Some(trace) = trace_of(&row)
            {
                turns.entry(trace.to_owned()).or_default().add(row);
            }
        }
        if !whole {
            break;
        }
    }
    Ok(turns.into_values().filter_map(Turn::anchor).collect())
}
