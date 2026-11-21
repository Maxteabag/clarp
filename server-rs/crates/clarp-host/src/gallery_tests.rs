use super::*;
use clarp_kernel::gallery::Deletion;
use clarp_kernel::media::Asset;
use clarp_ports::blobs::BlobEngine;
use clarp_ports::kv::KeyValueEngine;
use clarp_store_mem::{MemBlobs, MemKeyValue};

fn image(id: u64, at: i64, digest: &str) -> Image {
    Image::new(
        Asset {
            asset_id: format!("asset_{id:05}"),
            session: "oak".into(),
            source_name: "leaf.png".into(),
            sha256: digest.into(),
            extension: ".png".into(),
            mime_type: "image/png".into(),
            bytes: 5,
            width: Some(1),
            height: Some(1),
            caption: String::new(),
            created_by: "agent".into(),
            created_at: at,
            named_only: false,
        },
        "oak-id".into(),
    )
}

#[test]
fn restart_and_both_republish_orders_leave_the_old_id_gone() {
    for publish_before_release in [false, true] {
        let path = std::path::Path::new("gallery");
        let database = MemKeyValue::new();
        let blobs = MemBlobs::new().open(path).unwrap();
        let open = || Store::open(database.open(path).unwrap(), blobs.clone()).unwrap();
        let mut store = open();
        let old = image(1, 11, "digest");
        let new = image(2, 22, "digest");
        let name = old.asset.blob();
        store.publish(old.clone(), b"image").unwrap();
        store.favorite(&old.asset.asset_id, true, 15).unwrap();
        assert!(store.delete(&old.asset.asset_id, "owner", 18).unwrap().1);
        assert_eq!(blobs.len(&name).unwrap(), Some(5));
        // The store is reopened before the durable removal can be delivered.
        drop(store);
        let mut store = open();
        let check_removed = |store: &Store| {
            assert!(matches!(
                store.read(&old.asset.asset_id, 0, 5),
                Err(Failure::Refused(Refusal::Removed(18)))
            ));
        };
        check_removed(&store);
        if publish_before_release {
            store.publish(new.clone(), b"image").unwrap();
        }
        store.recover(true);
        store.release_due();
        if !publish_before_release {
            assert_eq!(blobs.len(&name).unwrap(), None);
            store.publish(new.clone(), b"image").unwrap();
        }
        assert_eq!(store.read(&new.asset.asset_id, 0, 5).unwrap(), b"image");
        check_removed(&store);
        let (kept, changed) = store.delete(&old.asset.asset_id, "later", 99).unwrap();
        assert!(!changed);
        assert_eq!(kept.deletion.unwrap(), Deletion { at: 18, by: "owner".into() });
        store.release_due();
        assert_eq!(blobs.len(&name).unwrap(), Some(5));
        store.delete(&new.asset.asset_id, "owner", 100).unwrap();
        store.release_due();
        assert_eq!(blobs.len(&name).unwrap(), None);
    }
}

#[test]
#[ignore = "Measure using tools/build on the build machine"]
fn media_gallery_storage_cost_at_300_and_30000_assets() {
    use std::time::Instant;

    fn measure(count: u64) -> [(u128, u128); 3] {
        let path = std::env::temp_dir().join(format!("clarp-gallery-cost-{}-{count}", std::process::id()));
        std::fs::create_dir_all(&path).unwrap();
        let db = clarp_store_sqlite::SqliteKeyValue.open(&path.join("gallery.db")).unwrap();
        let blobs = clarp_store_files::FileBlobs.open(&path.join("blobs")).unwrap();
        let mut store = Store::open(db, blobs).unwrap();
        store.recover(true);
        let halfway = count / 2;
        store.import((0..count).map(|id| {
            let at = i64::try_from(id).unwrap();
            let mut row = image(id, at, "shared");
            if id < halfway {
                row.deletion = Some(Deletion { at, by: "owner".into() });
            }
            row
        }).collect()).unwrap();
        let mut samples = [Vec::new(), Vec::new(), Vec::new()];
        for id in 0..100 {
            let stamp = (halfway + (halfway / 2)) as i64;
            let cursor = format!("c{stamp}.asset_{stamp:05}");
            let t = Instant::now();
            let page = store.index.page("oak", false, &cursor, 40).unwrap();
            assert_eq!(std::hint::black_box(page.rows).len(), 40);
            samples[0].push(t.elapsed().as_micros());

            let t = Instant::now();
            let page = store.index.deleted("oak", (halfway / 2) as i64, 40, count as i64);
            assert_eq!(std::hint::black_box(page.deleted).len(), 40);
            samples[1].push(t.elapsed().as_micros());

            let row = image(count + id, count as i64, &format!("test-{id}"));
            let key = row.asset.asset_id.clone();
            store.publish(row, b"image").unwrap();
            let t = Instant::now();
            assert!(store.delete(&key, "owner", count as i64).unwrap().1);
            store.release_due();
            samples[2].push(t.elapsed().as_micros());
        }
        drop(store);
        std::fs::remove_dir_all(path).unwrap();
        samples.map(|mut times| {
            times.sort_unstable();
            (times[49], times[89])
        })
    }

    let small = measure(300);
    let large = measure(30_000);
    for (name, from, to) in ["gallery page", "tombstones", "delete"].into_iter().zip(small).zip(large)
        .map(|((name, from), to)| (name, from, to))
    {
        eprintln!("media {name}, 300 -> 30000, median and p90 (us): {from:?} -> {to:?}");
        assert!(to.0 <= (from.0 * 2).max(from.0 + 500));
        assert!(to.1 <= (from.1 * 2).max(from.1 + 1000));
    }
}
