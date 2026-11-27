//! A client's receipts belong to this Host's playback desk. Only the agent writes
//! its conversation log. Playback and the next line can therefore make progress
//! while the receipt's own durable write waits.
use std::borrow::Cow;
use std::collections::HashMap;
use std::path::Path;
use std::sync::{Arc, PoisonError, RwLock};

use clarp_kernel::spoken::Line;
use clarp_ports::kv::{KeyValue, KeyValueEngine, Write};
use clarp_store_mem::MemKeyValue;
use clarp_store_sqlite::SqliteKeyValue;
use serde::{Deserialize, Serialize};
use tokio::sync::Mutex;

use crate::agent::Unavailable;

const TABLE: &str = "receipt";

#[derive(Clone, Deserialize, Serialize)]
struct Receipt {
    line: String,
    #[serde(default)]
    base_played: Option<String>,
    #[serde(default)]
    base_error: Option<String>,
    played: Option<String>,
    error: Option<String>,
    at_ms: i64,
}

/// One SQLite writer and a view of the state that has reached disk. Readers
/// of recoverable audio do not take the writer lock.
pub(crate) struct Receipts {
    writer: Arc<Mutex<Box<dyn KeyValue>>>,
    kept: RwLock<HashMap<String, Receipt>>,
}

fn refused(error: impl std::fmt::Display) -> Unavailable {
    Unavailable::new(format!("playback receipt store failed: {error}"))
}

fn place(agent: &str, clip: u64) -> String {
    format!("{agent}/{clip}")
}

impl Receipts {
    pub(crate) fn open(path: &Path, persistent: bool) -> Result<Arc<Self>, Unavailable> {
        let writer = if persistent {
            SqliteKeyValue::new().open(path)
        } else {
            MemKeyValue::new().open(path)
        }
        .map_err(refused)?;
        let kept = writer
            .list(TABLE)
            .map_err(refused)?
            .into_iter()
            .map(|(key, body)| {
                let decoded = serde_json::from_slice(&body).map_err(refused)?;
                Ok((key, decoded))
            })
            .collect::<Result<HashMap<_, _>, Unavailable>>()?;
        Ok(Arc::new(Self {
            writer: Arc::new(Mutex::new(writer)),
            kept: RwLock::new(kept),
        }))
    }

    /// A receipt applies to the exact line and clip it was recorded over.
    /// A changed status in the log can come from a Host that wrote there again.
    pub(crate) fn shown<'a>(&self, agent: &str, line: &'a Line) -> Cow<'a, Line> {
        let Some(clip) = line.clip_id else {
            return Cow::Borrowed(line);
        };
        let kept = self.kept.read().unwrap_or_else(PoisonError::into_inner);
        let Some(receipt) = kept.get(&place(agent, clip)).filter(|receipt| {
            receipt.line == line.id
                && receipt.base_played == line.played
                && receipt.base_error == line.error
        }) else {
            return Cow::Borrowed(line);
        };
        let Some(status) = receipt.played.as_deref() else {
            return Cow::Borrowed(line);
        };
        Cow::Owned(line.played(status, receipt.error.as_deref()))
    }

    /// Return only after the new status is durable. The only serialization is
    /// with other playback receipts. Do not hold the view lock over I/O.
    pub(crate) async fn record(
        self: &Arc<Self>,
        agent: String,
        line: Line,
        status: String,
        error: Option<String>,
        now_ms: i64,
    ) -> Result<(), Unavailable> {
        let Some(clip) = line.clip_id else {
            return Ok(());
        };
        let mut writer = self.writer.clone().lock_owned().await;
        let book = self.clone();
        tokio::task::spawn_blocking(move || {
            let shown = book.shown(&agent, &line);
            let played = shown.played(&status, error.as_deref());
            if *shown == played {
                return Ok(());
            }
            let key = place(&agent, clip);
            let receipt = Receipt {
                line: played.id,
                base_played: line.played,
                base_error: line.error,
                played: played.played,
                error: played.error,
                at_ms: now_ms,
            };
            let encoded = serde_json::to_vec(&receipt).map_err(refused)?;
            writer.put(TABLE, &key, &encoded).map_err(refused)?;
            book.kept
                .write()
                .unwrap_or_else(PoisonError::into_inner)
                .insert(key, receipt);
            Ok(())
        })
        .await
        .map_err(refused)?
    }

    pub(crate) async fn forget_before(self: &Arc<Self>, cutoff: i64) -> Result<(), Unavailable> {
        let mut writer = self.writer.clone().lock_owned().await;
        let book = self.clone();
        tokio::task::spawn_blocking(move || {
            let deletions: Vec<Write> = book
                .kept
                .read()
                .unwrap_or_else(PoisonError::into_inner)
                .iter()
                .filter(|(_, receipt)| receipt.at_ms < cutoff)
                .map(|(key, _)| (TABLE.to_string(), key.clone(), None))
                .collect();
            if !deletions.is_empty() {
                writer.put_together(&deletions).map_err(refused)?;
                book.kept
                    .write()
                    .unwrap_or_else(PoisonError::into_inner)
                    .retain(|_, receipt| receipt.at_ms >= cutoff);
            }
            Ok(())
        })
        .await
        .map_err(refused)?
    }
}
