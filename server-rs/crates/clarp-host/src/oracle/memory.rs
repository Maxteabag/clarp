//! The memory of the Oracle's threads, as this Host keeps it.
//!
//! The memory is the kernel's ([`clarp_kernel::oracle::threads`]): which thread a call goes
//! on in and which connection has it, what was kept of the talk, the actions that were begun,
//! the work of a thread and what its voice was told of. Here it is kept with the Host, row by
//! row under the kinds the kernel names: read from what was kept the first time it is asked
//! for, asked by one question at a time, and what a question changed is written before
//! anybody is told.

use clarp_kernel::oracle::threads::{BEGUN, LINKED, Memory, THREADS, TOLD};
use serde_json::Value;

use crate::agent::Unavailable;
use crate::edge::App;

/// The memory, read from what was kept the first time it is asked for.
async fn opened<'a>(
    app: &App,
    held: &'a mut Option<Memory>,
) -> Result<&'a mut Memory, Unavailable> {
    if held.is_none() {
        let mut rows: Vec<(&'static str, Value)> = Vec::new();
        for kind in [THREADS, BEGUN, LINKED, TOLD] {
            for (_, row) in app.fleet.kept_all(kind).await? {
                rows.extend(
                    serde_json::from_slice::<Value>(&row)
                        .ok()
                        .map(|row| (kind, row)),
                );
            }
        }
        *held = Some(Memory::kept(rows.iter().map(|(kind, row)| (*kind, row))));
    }
    Ok(held.get_or_insert_with(Memory::default))
}

/// The memory for one question. What the question changed in it is written when it is
/// answered, whatever it was answered.
pub(super) async fn with_memory<T>(
    app: &App,
    ask: impl FnOnce(&mut Memory) -> T,
) -> Result<T, Unavailable> {
    let mut held = app.oracle.threads.lock().await;
    let memory = opened(app, &mut held).await?;
    let answered = ask(memory);
    for kept in memory.written() {
        let row = match &kept.row {
            Some(row) => {
                let written = serde_json::to_vec(row);
                Some(written.map_err(|error| Unavailable(error.to_string()))?)
            }
            None => None,
        };
        app.fleet.keep(kept.kind, kept.id, row).await?;
    }
    Ok(answered)
}
