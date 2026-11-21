//! Refuse one publication at each storage port and reopen the accepted pair.
use super::*;
use crate::fleet::{retirement_tests::test_works, start};
use clarp_ports::{
    blobs::Blobs,
    clock::SystemClock,
    kv::{KeyValueEngine, StoreError, Write},
};
use clarp_store_mem::{MemEngine, MemKeyValue};
use std::sync::atomic::{AtomicBool, Ordering};

struct RejectPair {
    inner: Box<dyn KeyValue>,
    reject: Arc<AtomicBool>,
}
impl KeyValue for RejectPair {
    fn get(&self, table: &str, key: &str) -> Result<Option<Vec<u8>>, StoreError> {
        self.inner.get(table, key)
    }
    fn list(&self, table: &str) -> Result<Vec<(String, Vec<u8>)>, StoreError> {
        self.inner.list(table)
    }
    fn put(&mut self, table: &str, key: &str, value: &[u8]) -> Result<(), StoreError> {
        self.inner.put(table, key, value)
    }
    fn delete(&mut self, table: &str, key: &str) -> Result<bool, StoreError> {
        self.inner.delete(table, key)
    }
    fn put_together(&mut self, writes: &[Write]) -> Result<(), StoreError> {
        if self.reject.load(Ordering::SeqCst)
            && writes.iter().any(|(table, _, _)| table == &shelf(PENDING))
        {
            return Err(StoreError("fixture transaction refusal".into()));
        }
        self.inner.put_together(writes)
    }
}

struct RejectSecond {
    inner: Arc<dyn Blobs>,
    reject: AtomicBool,
}
impl Blobs for RejectSecond {
    fn put(&self, key: &str, bytes: &[u8]) -> Result<(), StoreError> {
        if bytes.ends_with(b"second") && self.reject.load(Ordering::SeqCst) {
            return Err(StoreError("fixture blob refusal".into()));
        }
        self.inner.put(key, bytes)
    }
    fn names_with_prefix(&self, prefix: &str) -> Result<Vec<String>, StoreError> {
        self.inner.names_with_prefix(prefix)
    }
    fn remove(&self, key: &str) -> Result<(), StoreError> {
        self.inner.remove(key)
    }
    fn len(&self, key: &str) -> Result<Option<u64>, StoreError> {
        self.inner.len(key)
    }
    fn read(&self, key: &str, offset: u64, most: usize) -> Result<Vec<u8>, StoreError> {
        self.inner.read(key, offset, most)
    }
}

#[tokio::test]
async fn a_portrait_pair_is_atomic_on_refusal_and_recovered_without_another_request() {
    let dir = std::env::temp_dir().join(format!("clarp-portrait-store-{}", crate::ids::hex(8)));
    std::fs::create_dir(&dir).unwrap();
    let roster = MemKeyValue::new();
    let engine = Arc::new(MemEngine::new());
    let make = || {
        test_works(
            &dir,
            engine.clone(),
            crate::unkept::Log::to(|_| {}, Arc::new(SystemClock)),
        )
    };
    let blobs = Arc::new(RejectSecond {
        inner: make().blobs,
        reject: AtomicBool::new(false),
    });
    let store_reject = Arc::new(AtomicBool::new(false));
    let boot = || {
        let mut works = make();
        works.blobs = blobs.clone();
        start(
            works,
            Box::new(RejectPair {
                inner: roster.open(&dir).unwrap(),
                reject: store_reject.clone(),
            }),
        )
        .unwrap()
    };
    let fleet = boot();
    let image = |tag: &[u8]| [b"\x89PNG\r\n\x1a\n".as_slice(), tag].concat();
    let request = serde_json::json!({"name":"Subject","backend":"scripted","cwd":dir,"avatar_base64":clarp_kernel::python::b64encode(&image(b"source"))});
    let made = fleet
        .create(serde_json::from_value(request).unwrap())
        .await
        .unwrap()
        .unwrap();
    let member = fleet.directory().named(&made.agent_id).unwrap().clone();
    let Admission::Begun(job) = fleet.begin_portraits(made.agent_id.clone()).await.unwrap() else {
        panic!("a new job")
    };
    fleet
        .change_computer_job(JobCommand::Start {
            id: job.id.clone(),
            generation: Some(job.generation),
            worker: Worker::none(),
        })
        .await
        .unwrap()
        .unwrap();
    let raw = [image(b"first"), image(b"second")];
    let assets = [
        crate::portrait_generation::asset(&member, &raw[0], 1, 100).unwrap(),
        crate::portrait_generation::asset(&member, &raw[1], 2, 100).unwrap(),
    ];
    let publication = Publication {
        agent_id: made.agent_id.clone(),
        assets,
    };
    let keep = || {
        fleet.accept_portraits(
            job.id.clone(),
            job.generation,
            publication.clone(),
            raw.clone(),
        )
    };
    // A refused transaction keeps the old gallery, the running job and no staged assets.
    store_reject.store(true, Ordering::SeqCst);
    assert!(
        keep()
            .await
            .unwrap_err()
            .0
            .contains("fixture transaction refusal")
    );
    assert_eq!(
        fleet
            .directory()
            .named(&made.agent_id)
            .unwrap()
            .record
            .portraits,
        made.portraits
    );
    assert_eq!(
        fleet
            .computer_job(job.id.clone())
            .await
            .unwrap()
            .unwrap()
            .job
            .status,
        JobStatus::Running
    );
    assert!(fleet.kept_all(PENDING).await.unwrap().is_empty());
    assert!(fleet.kept_all(BYTES).await.unwrap().is_empty());
    for a in &publication.assets {
        assert!(blobs.len(&a.blob()).unwrap().is_none());
    }
    // Fail on the second local image after a successful decision: both originals remain recoverable.
    store_reject.store(false, Ordering::SeqCst);
    blobs.reject.store(true, Ordering::SeqCst);
    assert!(keep().await.unwrap());
    assert_eq!(
        fleet
            .computer_job(job.id.clone())
            .await
            .unwrap()
            .unwrap()
            .job
            .status,
        JobStatus::Succeeded
    );
    assert_eq!(fleet.kept_all(PENDING).await.unwrap().len(), 1);
    assert_eq!(fleet.kept_all(BYTES).await.unwrap().len(), 2);
    let local: Arc<dyn Blobs> = blobs.clone();
    let gallery =
        crate::gallery::Store::open(roster.open(&dir.join("gallery")).unwrap(), local.clone())
            .unwrap();
    let gallery = crate::gallery::Writer::start(gallery).unwrap();
    let record = fleet
        .directory()
        .named(&made.agent_id)
        .unwrap()
        .record
        .clone();
    assert!(
        crate::personas::gallery_ready(&fleet, local.as_ref(), &record)
            .await
            .is_err()
    );
    assert!(
        crate::portrait_generation::deliver(&fleet, &local, &gallery, &publication)
            .await
            .is_err()
    );
    for a in &publication.assets {
        assert!(member.agent.view().books.media.get(&a.asset_id).is_none());
    }
    drop(member);
    drop(fleet);
    // Opening the durable decision again needs no provider. Ids, pixels and job generation survive.
    blobs.reject.store(false, Ordering::SeqCst);
    let fleet = boot();
    let receipts = fleet.kept_all(PENDING).await.unwrap();
    let restored: Publication = serde_json::from_slice(&receipts[0].1).unwrap();
    crate::portrait_generation::deliver(&fleet, &local, &gallery, &restored)
        .await
        .unwrap();
    let member = fleet.directory().named(&made.agent_id).unwrap().clone();
    crate::personas::gallery_ready(&fleet, local.as_ref(), &member.record)
        .await
        .unwrap();
    assert_eq!(member.record.portraits.len(), 3);
    let primary = member
        .record
        .portraits
        .iter()
        .find(|p| p.media_asset_id.is_none())
        .unwrap();
    assert_eq!(Some(&primary.sha256), made.avatar.as_ref());
    assert_eq!(
        primary.portrait_id,
        kept::worn_id(&made.agent_id, &primary.sha256)
    );
    for (a, raw) in restored.assets.iter().zip(raw) {
        assert_eq!(member.agent.view().books.media.get(&a.asset_id), Some(a));
        assert_eq!(blobs.read(&picture_name(&a.sha256), 0, 1000).unwrap(), raw);
    }
    assert!(fleet.kept_all(PENDING).await.unwrap().is_empty());
    assert!(fleet.kept_all(BYTES).await.unwrap().is_empty());
    drop(member);
    drop(fleet);
    std::fs::remove_dir_all(dir).unwrap();
}
