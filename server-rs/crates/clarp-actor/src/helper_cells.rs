//! Only rows known to contain a Claude helper are revisited when the
//! association between the parent's call and a child file changes.
use super::*;
use serde_json::Value;
use std::collections::BTreeSet;

#[derive(Default)]
pub struct HelperRows {
    through: Seq,
    floor: Seq,
    ids: BTreeSet<String>,
    pub has_imported: bool,
    unresolved: BTreeSet<String>,
    dirty: BTreeSet<String>,
}

const PREFIX: &str = "helper-cells:";
#[derive(serde::Serialize, serde::Deserialize)]
struct Revision {
    row: String,
    cells: Value,
}
fn revision(item: &Item) -> Result<Revision, ActorError> {
    serde_json::from_slice(&item.body).map_err(|e| ActorError::Corrupt(e.to_string()))
}
pub(super) fn parent(item: &Item) -> Result<Option<String>, ActorError> {
    if item.kind != kind::HELPER_CELLS {
        return Ok(None);
    }
    Ok(Some(revision(item)?.row))
}
pub(super) fn current(
    log: &dyn AgentLog,
    id: &str,
    since: Seq,
) -> Result<Option<(Seq, Value)>, ActorError> {
    let Some(item) = log.find(&format!("{PREFIX}{id}"))? else {
        return Ok(None);
    };
    if item.kind != kind::HELPER_CELLS || item.seq <= since {
        return Ok(None);
    }
    Ok(Some((item.seq, revision(&item)?.cells)))
}

fn is_cell(cell: &Value) -> bool {
    cell["kind"] == "subagents" && cell["ephemeral"] == true
}

impl HelperRows {
    pub fn needs_receipt(&self) -> bool {
        !self.unresolved.is_empty()
    }
}

impl Actor {
    pub fn helper_context(&self) -> (bool, Option<&str>) {
        let agent = self.recorder.agent();
        (agent.live().is_some(), agent.backend_session())
    }

    pub fn track_helpers(&self, rows: &mut HelperRows) -> Result<bool, ActorError> {
        let floor = self.recorder.floor();
        if floor != rows.floor || rows.through > self.recorder.last_seq() {
            *rows = HelperRows {
                floor,
                through: floor,
                ..Default::default()
            };
        }
        if rows.through != self.recorder.last_seq() {
            for kind in [kind::IMPORTED, kind::TOOL_USE, kind::TOOL] {
                let mut cursor = rows.through;
                loop {
                    let batch = self
                        .recorder
                        .log()
                        .read_after(cursor, Kinds::only(kind), 256)?;
                    for item in &batch {
                        cursor = item.seq;
                        if let Some(entry) = Entry::from_item(item)
                            .map_err(|e| ActorError::Corrupt(e.to_string()))?
                        {
                            let matches = match &entry {
                                Entry::Tool(tool) => tool.cell.as_ref().is_some_and(is_cell),
                                Entry::Imported(import) => import.row["display_cells"]
                                    .as_array()
                                    .is_some_and(|cells| cells.iter().any(is_cell)),
                                _ => false,
                            };
                            if let Entry::Tool(tool) = &entry
                                && let Some(cell) = tool.cell.as_ref().filter(|c| is_cell(c))
                            {
                                if cell["status"] == "running" {
                                    rows.unresolved.insert(tool.id.clone());
                                } else {
                                    rows.unresolved.remove(&tool.id);
                                }
                            }
                            if matches {
                                rows.has_imported |= matches!(entry, Entry::Imported(_));
                                rows.dirty.insert(entry.id().into());
                                rows.ids.insert(entry.id().into());
                            }
                        }
                    }
                    if batch.len() < 256 {
                        break;
                    }
                }
            }
            rows.through = self.recorder.last_seq();
        }
        Ok(!rows.ids.is_empty())
    }

    pub fn refresh_helpers(
        &mut self,
        rows: &mut HelperRows,
        source_changed: bool,
        now_ms: i64,
        mut update: impl FnMut(&mut Value, bool, Option<u64>),
    ) -> Result<Outputs, ActorError> {
        let mut writes = Vec::new();
        let changed_ids = if source_changed {
            &rows.ids
        } else {
            &rows.dirty
        };
        for id in changed_ids {
            let Some(item) = self.recorder.log().find(id)? else {
                continue;
            };
            let Some(mut entry) =
                Entry::from_item(&item).map_err(|e| ActorError::Corrupt(e.to_string()))?
            else {
                continue;
            };
            let row = entry.part_of().unwrap_or(id).to_owned();
            // Always project from the provider's original report. A filename
            // inferred on a prior pass is not evidence of a reported agentId.
            let (base, shown) = match &mut entry {
                Entry::Tool(call) => {
                    let Some(cell) = call.cell.as_mut().filter(|c| is_cell(c)) else {
                        continue;
                    };
                    let base = cell.clone();
                    update(cell, false, Some(call.turn));
                    (base, cell.clone())
                }
                Entry::Imported(import) => {
                    let cells = &mut import.row["display_cells"];
                    let base = cells.clone();
                    if let Some(all) = cells.as_array_mut() {
                        for cell in all.iter_mut().filter(|c| is_cell(c)) {
                            update(cell, true, None);
                        }
                    }
                    (base, cells.clone())
                }
                _ => continue,
            };
            let previous =
                current(self.recorder.log(), id, item.seq)?.map_or(base, |(_, cells)| cells);
            if shown != previous {
                let data = serde_json::to_vec(&Revision { row, cells: shown })
                    .map_err(|e| ActorError::Corrupt(e.to_string()))?;
                writes.push(
                    NewItem::new(kind::HELPER_CELLS, now_ms, data).keyed(format!("{PREFIX}{id}")),
                );
            }
        }
        let mut out = Outputs::default();
        if !writes.is_empty() {
            let mut batch = self.recorder.batch();
            for write in writes {
                batch.item(write);
            }
            batch.commit()?;
            out.changed.conversation = true;
        }
        rows.dirty.clear();
        Ok(out)
    }
}
