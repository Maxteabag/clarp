#![allow(clippy::unwrap_used)]
use clarp_kernel::heard::inventory::{CATALOG, install_model, space_available};
use serde_json::json;

#[test]
fn downloads_are_pinned_and_charge_the_cap_and_disk_margin() {
    let pins = [
        (
            "base.en",
            "Systran/faster-whisper-base.en",
            "3d3d5dee26484f91867d81cb899cfcf72b96be6c",
            148_000_000,
        ),
        (
            "small.en",
            "Systran/faster-whisper-small.en",
            "d1d751a5f8271d482d14ca55d9e2deeebbae577f",
            488_000_000,
        ),
        (
            "small",
            "Systran/faster-whisper-small",
            "536b0662742c02347bc0e980a01041f333bce120",
            488_000_000,
        ),
        (
            "medium",
            "Systran/faster-whisper-medium",
            "08e178d48790749d25932bbc082711ddcfdfbc4f",
            1_530_000_000,
        ),
        (
            "large-v3-turbo",
            "deepdml/faster-whisper-large-v3-turbo-ct2",
            "4df90f75321148c3a29a9e2351b7ddf8f5b115a8",
            1_620_000_000,
        ),
    ];
    assert_eq!(
        CATALOG.iter().filter(|m| m.source.is_some()).count(),
        pins.len()
    );
    let margin = 256 * 1024 * 1024;
    for (name, repo, rev, bytes) in pins {
        let id = format!("faster-whisper:{name}");
        let model = install_model(&id).unwrap();
        let source = model.source.as_ref().unwrap();
        assert_eq!((source.repository, source.revision), (repo, rev));
        assert_eq!(model.bytes, bytes);
        let cap = bytes / 4 * 5;
        assert_eq!(model.maximum_bytes(), cap);
        let row = model.installed_record();
        assert_eq!(row["id"], id);
        assert_eq!(row["model"], name);
        assert_eq!(row["provider"], "faster-whisper");
        assert_eq!(row["download_bytes"], bytes);
        assert_eq!(
            row["recommended"],
            if name == "small.en" {
                json!(true)
            } else {
                json!(null)
            }
        );
        for reserved in [0, 10000] {
            let needed = cap - 17;
            assert!(space_available(
                margin + reserved + needed,
                reserved,
                needed
            ));
            assert!(!space_available(
                margin + reserved + needed - 1,
                reserved,
                needed
            ));
        }
    }
    assert!(!space_available(0, 0, 0));
    assert!(!space_available(margin - 1, 0, 0));
    assert!(!space_available(u64::MAX, u64::MAX, 1));
    assert!(space_available(margin, 0, 0));
    assert!(install_model("faster-whisper:deep/new").is_err());
}

#[test]
fn staging_retention_keeps_the_seven_day_boundary_and_files_owned_by_jobs() {
    use clarp_kernel::{heard::inventory::discard_staging, jobs::JobStatus};
    use std::time::Duration;
    let week = Duration::from_secs(604800);
    assert!(!discard_staging(None, week - Duration::from_nanos(1)));
    assert!(!discard_staging(None, week));
    assert!(discard_staging(None, week + Duration::from_nanos(1)));
    for status in [
        JobStatus::Queued,
        JobStatus::Running,
        JobStatus::Succeeded,
        JobStatus::Failed,
    ] {
        assert!(!discard_staging(Some(status), week * 2));
    }
    assert!(discard_staging(Some(JobStatus::Cancelled), Duration::ZERO));
}
