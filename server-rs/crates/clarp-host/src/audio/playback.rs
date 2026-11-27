//! A client's receipts belong to this Host's playback desk. The agent copies their
//! playback status to its own log when its speech has drained, for older Hosts.
use std::borrow::Cow;
use std::collections::{HashMap, HashSet};
use std::path::Path;
use std::sync::{Arc, PoisonError, RwLock};

use clarp_kernel::spoken::{Line, LineState};
use clarp_ports::kv::{KeyValue, KeyValueEngine, Write};
use clarp_store_mem::MemKeyValue;
use clarp_store_sqlite::SqliteKeyValue;
use serde::{Deserialize, Serialize};
use tokio::sync::Mutex;

use crate::agent::{AgentHandle, Unavailable};

const TABLE: &str = "receipt";

#[derive(Clone, Deserialize, Serialize)]
struct Receipt {
    line: String,
    #[serde(default)]
    revision: u64,
    #[serde(default)]
    base_played: Option<String>,
    #[serde(default)]
    base_error: Option<String>,
    #[serde(default)]
    while_speaking: bool,
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

    /// A mirror from this book carries its revision. A different legacy status
    /// without that revision can come from an older Host writing the log again.
    pub(crate) fn shown<'a>(&self, agent: &str, line: &'a Line) -> Cow<'a, Line> {
        let Some(clip) = line.clip_id else {
            return Cow::Borrowed(line);
        };
        let kept = self.kept.read().unwrap_or_else(PoisonError::into_inner);
        let Some(receipt) = kept.get(&place(agent, clip)) else {
            return Cow::Borrowed(line);
        };
        let failed_later = receipt.while_speaking && line.state == LineState::Failed;
        let applies = line.playback_revision.map_or_else(
            || {
                receipt.base_played == line.played
                    && (line.played.as_deref() != Some("play-fail")
                        || failed_later
                        || receipt.base_error == line.error)
            },
            |revision| revision < receipt.revision,
        );
        if receipt.line != line.id || !applies {
            return Cow::Borrowed(line);
        }
        let Some(status) = receipt.played.as_deref() else {
            return Cow::Borrowed(line);
        };
        let mut copied = line.played(status, receipt.error.as_deref());
        if failed_later {
            // A producer error that came after the client spoke survives as
            // it did when the actor recorded playback before finishing the line.
            copied.error.clone_from(&line.error);
        }
        copied.playback_revision = Some(receipt.revision);
        Cow::Owned(copied)
    }

    /// Who may still have a receipt to copy after the Host has restarted.
    pub(crate) fn agents(&self) -> HashSet<String> {
        self.kept
            .read()
            .unwrap_or_else(PoisonError::into_inner)
            .keys()
            .filter_map(|key| key.rsplit_once('/').map(|(agent, _)| agent.to_string()))
            .collect()
    }

    /// Return only after the new status is durable. The only serialization is
    /// with other playback receipts. Do not hold the view lock over I/O.
    pub(crate) async fn record(
        self: &Arc<Self>,
        agent: String,
        handle: AgentHandle,
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
                if matches!(shown, Cow::Owned(_)) {
                    handle.mirror_playback(book);
                }
                return Ok(());
            }
            let key = place(&agent, clip);
            let previous = book
                .kept
                .read()
                .unwrap_or_else(PoisonError::into_inner)
                .get(&key)
                .filter(|receipt| receipt.line == line.id)
                .map_or(0, |receipt| receipt.revision);
            let receipt = Receipt {
                line: played.id,
                revision: previous
                    .max(line.playback_revision.unwrap_or_default())
                    .saturating_add(1),
                base_played: line.played,
                base_error: line.error,
                while_speaking: line.state == LineState::Speaking,
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
            // Do this in the committing task so an HTTP disconnect after the
            // write still leaves a copy requested. The agent decides when to write.
            handle.mirror_playback(book);
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
