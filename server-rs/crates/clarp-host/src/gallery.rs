//! One owner of the gallery's mutable records and shared files.

use std::collections::BTreeSet;
use std::sync::Arc;
use std::time::Duration;

use clarp_kernel::gallery::{Image, Index, Refusal};
use clarp_ports::blobs::Blobs;
use clarp_ports::kv::{KeyValue, StoreError, Write};
use tokio::sync::{mpsc, oneshot};

const IMAGES: &str = "media";
const PUBLICATIONS: &str = "media-publication";
const REMOVALS: &str = "remove-media-blob";
const WAIT: Duration = Duration::from_secs(3);
const QUEUE: usize = 32;

#[derive(Debug)]
pub enum Failure {
    Refused(Refusal),
    Store(String),
    Busy,
    Stopped,
    TimedOut,
    Taken,
}

impl Failure {
    pub fn words(&self) -> &str {
        match self {
            Self::Refused(why) => why.words(),
            Self::Store(why) => why,
            Self::Busy => "the media writer is busy",
            Self::Stopped => "the media writer is unavailable",
            Self::TimedOut => "the media writer did not answer in time",
            Self::Taken => "another file has this id",
        }
    }
}

impl From<Refusal> for Failure {
    fn from(why: Refusal) -> Self {
        Self::Refused(why)
    }
}

impl From<StoreError> for Failure {
    fn from(why: StoreError) -> Self {
        Self::Store(why.to_string())
    }
}

pub struct Store {
    pub index: Index,
    db: Box<dyn KeyValue>,
    blobs: Arc<dyn Blobs>,
    pending: BTreeSet<String>,
}

impl Store {
    pub fn open(db: Box<dyn KeyValue>, blobs: Arc<dyn Blobs>) -> Result<Self, Failure> {
        let mut index = Index::default();
        for (_, raw) in db.list(IMAGES)? {
            let image = serde_json::from_slice(&raw)
                .map_err(|why| Failure::Store(format!("bad kept media row: {why}")))?;
            index.apply(image);
        }
        let pending = db.list(REMOVALS)?.into_iter().map(|(name, _)| name).collect();
        Ok(Self { index, db, blobs, pending })
    }

    fn record(image: &Image) -> Result<Write, Failure> {
        let raw = serde_json::to_vec(image).map_err(|why| Failure::Store(why.to_string()))?;
        Ok((IMAGES.to_owned(), image.asset.asset_id.clone(), Some(raw)))
    }

    /// Fold existing publication history into the index before reclaiming any shared file.
    /// Stored mutations win over older journal rows.
    pub fn import(&mut self, images: Vec<Image>) -> Result<(), Failure> {
        let mut pending = Vec::new();
        let mut seen = BTreeSet::new();
        for image in images {
            if self.index.get(&image.asset.asset_id).is_none()
                && seen.insert(image.asset.asset_id.clone())
            {
                pending.push(image);
            }
        }
        let writes = pending.iter().map(Self::record).collect::<Result<Vec<_>, _>>()?;
        if !writes.is_empty() {
            self.db.put_together(&writes)?;
        }
        for image in pending {
            self.index.apply(image);
        }
        Ok(())
    }

    /// Takes bytes and records an ID on the same writer that can unlink the bytes.
    /// The journal delivery is owed in the same atomic decision as the gallery row.
    pub fn publish(&mut self, image: Image, bytes: &[u8]) -> Result<(Image, bool), Failure> {
        let id = &image.asset.asset_id;
        let existing = self.index.get(id);
        if let Some(known) = existing {
            if let Some(deletion) = &known.deletion {
                return Err(Refusal::Removed(deletion.at).into());
            }
            if known.asset.blob() != image.asset.blob() {
                return Err(Failure::Taken);
            }
        }
        let replace = existing.is_none_or(|known| known.asset.named_only && !image.asset.named_only);
        let mut canonical = if replace {
            image.clone()
        } else {
            existing.cloned().ok_or(Refusal::Unknown)?
        };
        if let Some(known) = existing {
            canonical.favorite_at = known.favorite_at;
        }
        self.blobs.put(&canonical.asset.blob(), bytes)?;
        let delivery = serde_json::to_vec(&image).map_err(|why| Failure::Store(why.to_string()))?;
        let key = Self::delivery_key(&image.agent_id, id);
        self.db.put_together(&[
            Self::record(&canonical)?,
            (PUBLICATIONS.to_owned(), key, Some(delivery)),
        ])?;
        self.index.apply(canonical.clone());
        Ok((canonical, replace))
    }

    fn delivery_key(agent_id: &str, asset_id: &str) -> String {
        format!("{agent_id}/{asset_id}")
    }

    pub fn delivered(&mut self, agent_id: &str, asset_id: &str) -> Result<(), Failure> {
        self.db.delete(PUBLICATIONS, &Self::delivery_key(agent_id, asset_id))?;
        Ok(())
    }

    pub fn undelivered(&self) -> Result<Vec<Image>, Failure> {
        self.db
            .list(PUBLICATIONS)?
            .into_iter()
            .map(|(_, raw)| serde_json::from_slice(&raw).map_err(|why| Failure::Store(why.to_string())))
            .collect()
    }

    pub fn favorite(&mut self, id: &str, favorite: bool, now: i64) -> Result<(Image, bool), Failure> {
        let current = self.index.get(id).ok_or(Refusal::Unknown)?;
        let Some(updated) = current.favorite(favorite, now)? else {
            return Ok((current.clone(), false));
        };
        self.db.put_together(&[Self::record(&updated)?])?;
        self.index.apply(updated.clone());
        Ok((updated, true))
    }

    pub fn delete(&mut self, id: &str, by: &str, now: i64) -> Result<(Image, bool), Failure> {
        let current = self.index.get(id).ok_or(Refusal::Unknown)?;
        let Some(updated) = current.delete(by, now)? else {
            return Ok((current.clone(), false));
        };
        let blob = updated.asset.blob();
        self.db.put_together(&[
            Self::record(&updated)?,
            (REMOVALS.to_owned(), blob.clone(), Some(Vec::new())),
        ])?;
        self.index.apply(updated.clone());
        self.pending.insert(blob);
        Ok((updated, true))
    }

    /// A concurrent publish is ordered against this check and unlink by the writer. Disk
    /// errors leave the durable work behind, for a later call or a restart to finish.
    pub fn release(&mut self, name: &str) -> Result<(), Failure> {
        if !self.pending.contains(name) {
            return Ok(());
        }
        if !self.index.holds_blob(name) {
            self.blobs.remove(name)?;
        }
        self.db.delete(REMOVALS, name)?;
        self.pending.remove(name);
        Ok(())
    }

    pub fn recover(&mut self) -> Result<(), Failure> {
        for name in self.pending.clone() {
            self.release(&name)?;
        }
        Ok(())
    }

    /// Gate each streamed chunk on the same record as a content request. A tombstone never
    /// sends another chunk even if another image has the same bytes.
    pub fn read(&self, id: &str, offset: u64, most: usize) -> Result<Vec<u8>, Failure> {
        let image = self.index.get(id).ok_or(Refusal::Unknown)?;
        if let Some(deletion) = &image.deletion {
            return Err(Refusal::Removed(deletion.at).into());
        }
        Ok(self.blobs.read(&image.asset.blob(), offset, most)?)
    }
}

type Work = Box<dyn FnOnce(&mut Store) + Send>;

#[derive(Clone)]
pub struct Writer {
    send: mpsc::Sender<Work>,
}

impl Writer {
    pub fn start(mut store: Store) -> Result<Self, std::io::Error> {
        let (send, mut read) = mpsc::channel::<Work>(QUEUE);
        std::thread::Builder::new().name("clarp-gallery".to_owned()).spawn(move || {
            while let Some(work) = read.blocking_recv() {
                work(&mut store);
            }
        })?;
        Ok(Self { send })
    }

    /// The fleet and agent actors never await this writer. A caller waiting for its own
    /// answer has one deadline. A command whose caller timed out before it began is skipped.
    pub async fn ask<T: Send + 'static>(
        &self,
        work: impl FnOnce(&mut Store) -> Result<T, Failure> + Send + 'static,
    ) -> Result<T, Failure> {
        let (answer, read) = oneshot::channel();
        self.send
            .try_send(Box::new(move |store| {
                if !answer.is_closed() {
                    let _ = answer.send(work(store));
                }
            }))
            .map_err(|error| match error {
                mpsc::error::TrySendError::Full(_) => Failure::Busy,
                mpsc::error::TrySendError::Closed(_) => Failure::Stopped,
            })?;
        tokio::time::timeout(WAIT, read)
            .await
            .map_err(|_| Failure::TimedOut)?
            .map_err(|_| Failure::Stopped)?
    }
}
