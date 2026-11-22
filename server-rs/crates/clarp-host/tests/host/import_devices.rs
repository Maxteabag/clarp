//! One cutover against a frozen, synthetic Python-format database with uncheckpointed WAL.
use super::*;
use rusqlite::{Connection, params};

const ADMIN: &str = "device-import-scratch-admin";
const PYTHON_ID: &str = "11111111-2222-4333-a444-555555555555";
const FULL: &str = "cld_fixture_python_full_known_device_token";
const LIMITED_PEER: &str = "cld_fixture_python_limited_peer_token";
const REVOKED: &str = "cld_fixture_revoked_python_token";
const FULL_ID: &str = "device_00000000000000000000000000000001";
const LIMITED_ID: &str = "device_00000000000000000000000000000002";

#[test]
fn python_devices_authenticate_with_their_original_scope_and_host_identity_after_cutover() {
    let place = Place::new("import-devices");
    let old = place.start_with_token(ADMIN);
    let own_id = old.get("/server-info")["server_id"].clone();
    assert_ne!(own_id, PYTHON_ID);
    assert_eq!(old.get("/server-info")["previous_server_ids"], json!([]));
    assert_eq!(
        old.get("/pairing/identity")["previous_server_ids"],
        json!([])
    );
    let (existing, existing_id) = pair(&old, "Rust pairing", "limited");
    let (revoked_here, revoked_here_id) = pair(&old, "Revoked in Rust", "limited");
    assert_eq!(
        old.post(
            "/paired-devices/revoke",
            json!({"device_id": revoked_here_id})
        )["ok"],
        true
    );
    let session = old.new_agent("Greeter");
    drop(old);

    let seed = place.root.join("seed.sqlite");
    let writer = Connection::open(&seed).unwrap();
    writer.execute_batch(
        "PRAGMA journal_mode=WAL;
         PRAGMA wal_autocheckpoint=0;
         CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at INTEGER NOT NULL);
         CREATE TABLE paired_devices(
            device_id TEXT PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
            scope TEXT NOT NULL CHECK(scope IN ('full','limited')), created_at INTEGER NOT NULL,
            last_seen_at INTEGER, revoked_at INTEGER);",
    ).unwrap();
    writer
        .execute(
            "INSERT INTO settings VALUES ('server_instance_id', ?1, 10)",
            [PYTHON_ID],
        )
        .unwrap();
    let existing_hash = clarp_host::pairing::digest(&existing);
    let revoked_here_hash = clarp_host::pairing::digest(&revoked_here);
    for (id, name, hash, scope, seen, revoked) in [
        (
            FULL_ID,
            "Python phone",
            "1c3e4968d364c2291406cd1ace51b2245ce956bd8e09cbdb74abd4f6532e725f",
            "full",
            Some(456),
            None,
        ),
        (
            LIMITED_ID,
            "Python peer",
            "55c89e34699574fab5169dead4326e247c9cb2edeacf93aee72d4eac0f7d3ba2",
            "limited",
            None,
            None,
        ),
        (
            "device_00000000000000000000000000000003",
            "Revoked in Python",
            "36e75fbd1a73852ed8c842e05cb173ae4478916d30492749ed3718d8d88b2745",
            "full",
            Some(456),
            Some(500),
        ),
        (
            "device_00000000000000000000000000000004",
            "Old name",
            &existing_hash,
            "full",
            Some(456),
            None,
        ),
        (
            "device_00000000000000000000000000000005",
            "Old grant",
            &revoked_here_hash,
            "full",
            None,
            None,
        ),
    ] {
        writer
            .execute(
                "INSERT INTO paired_devices VALUES (?1, ?2, ?3, ?4, 123, ?5, ?6)",
                params![id, name, hash, scope, seen, revoked],
            )
            .unwrap();
    }
    // A stable snapshot with committed pages still in the WAL, and no shared-memory file.
    // Include punctuation so the CLI must treat it as part of the actual filename.
    let source = place.root.join("python state?.sqlite");
    let wal = place.root.join("python state?.sqlite-wal");
    let shm = place.root.join("python state?.sqlite-shm");
    std::fs::copy(&seed, &source).unwrap();
    std::fs::copy(place.root.join("seed.sqlite-wal"), &wal).unwrap();
    drop(writer);
    let before = std::fs::read(&source).unwrap();
    let before_wal = std::fs::read(&wal).unwrap();
    let data = place.root.join("data");
    for (imported, already_present, previous_ids_added) in [(2, 2, 1), (0, 4, 0)] {
        let outcome = place
            .host_command()
            .args(["import", "--devices", "--from-db"])
            .arg(&source)
            .arg("--data")
            .arg(&data)
            .ended();
        assert!(
            outcome.status.success(),
            "{}",
            String::from_utf8_lossy(&outcome.stderr)
        );
        let actual: Value = serde_json::from_slice(&outcome.stdout).unwrap();
        assert_eq!(
            actual,
            json!({ "imported": imported, "already_present": already_present,
            "revoked": 1, "previous_ids_added": previous_ids_added })
        );
        assert!(outcome.stderr.is_empty());
    }
    assert_eq!(std::fs::read(source).unwrap(), before);
    assert_eq!(std::fs::read(wal).unwrap(), before_wal);
    assert!(!shm.exists());

    let host = place.start_with_token(ADMIN);
    for path in ["/server-info", "/pairing/identity"] {
        let (status, identity) = host.ask_as(FULL, "GET", path, None);
        assert_eq!(status, 200);
        assert_eq!(identity["server_id"], own_id);
        assert_eq!(identity["previous_server_ids"], json!([PYTHON_ID]));
    }
    let (status, info) = host.ask_as(LIMITED_PEER, "GET", "/server-info", None);
    assert_eq!(status, 200);
    assert_eq!(info["previous_server_ids"], json!([PYTHON_ID]));
    assert_eq!(
        host.ask_as(LIMITED_PEER, "GET", "/paired-devices", None).0,
        403
    );
    assert_eq!(
        host.ask_as(existing.as_str(), "GET", "/paired-devices", None)
            .0,
        403
    );
    for token in [REVOKED, revoked_here.as_str()] {
        assert_eq!(host.ask_as(token, "GET", "/server-info", None).0, 401);
    }
    let (status, listed) = host.ask_as(FULL, "GET", "/paired-devices", None);
    assert_eq!(status, 200);
    let devices = listed["devices"].as_array().unwrap();
    let find = |id: &str| devices.iter().find(|row| row["device_id"] == id).unwrap();
    assert_eq!(devices.len(), 3);
    let full = find(FULL_ID);
    assert_eq!(full["name"], "Python phone");
    assert_eq!(full["scope"], "full");
    assert_eq!(full["created_at"], 123);
    assert_eq!(full["last_seen_at"], 456);
    assert_eq!(find(LIMITED_ID)["last_seen_at"], 0);
    assert_eq!(find(&existing_id)["name"], "Rust pairing");
    assert!(devices.iter().all(|row| row.get("token_hash").is_none()));

    let body =
        json!({"session": session, "text": "hello from the old peer", "synthesize_audio": false});
    let sent = fetch(
        &host,
        LIMITED_PEER,
        "POST",
        "/send",
        &[
            ("Content-Type", "application/json"),
            ("X-Clarp-Peer", "fixture-peer"),
        ],
        body.to_string().as_bytes(),
    );
    assert_eq!(sent.status, 200, "{}", String::from_utf8_lossy(&sent.body));
}
