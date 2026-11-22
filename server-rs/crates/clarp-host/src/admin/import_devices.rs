//! Offline migration from the Python Host's paired-device book.

use std::collections::HashSet;
use std::path::Path;

use clarp_ports::kv::StoreError;
use clarp_store_sqlite::SqliteKeyValue;
use serde_json::json;

use crate::fleet::DEVICES;
use crate::pairing::{self, Device, Scope};

use super::offline_import::{self, Files};

pub(super) fn run(args: impl Iterator<Item = String>) -> Result<String, String> {
    let files = Files::parse(args, "device import", false)?;
    migrate(&files.source, &files.target)
}

fn migrate(source: &Path, target: &Path) -> Result<String, String> {
    let (server_id, devices, revoked) = read_source(source)?;
    let (imported, previous_id_added) = SqliteKeyValue::update_existing(target, |store| {
        let mut ids = HashSet::new();
        let mut hashes = HashSet::new();
        for (id, body) in store.list(DEVICES)? {
            let existing: Device = serde_json::from_slice(&body)
                .map_err(|_| StoreError("the Rust device book has an unreadable row".into()))?;
            if existing.device_id != id || !is_hex(&existing.token_hash, 64) {
                return Err(StoreError(
                    "the Rust device book has an unreadable row".into(),
                ));
            }
            ids.insert(id);
            // A locally revoked token stays revoked even if it was still active in Python.
            hashes.insert(existing.token_hash);
        }
        let mut writes = Vec::new();
        for device in &devices {
            if !hashes.insert(device.token_hash.clone()) {
                continue;
            }
            if !ids.insert(device.device_id.clone()) {
                return Err(StoreError(
                    "a device ID has a different pairing in the Rust book".into(),
                ));
            }
            let body = serde_json::to_vec(device)
                .map_err(|_| StoreError("a device could not be encoded".into()))?;
            writes.push((DEVICES.into(), device.device_id.clone(), Some(body)));
        }
        let imported = writes.len();
        let own_id = store.get("setting", pairing::SERVER_ID)?;
        let mut previous_ids = pairing::previous_server_ids(store)?;
        let previous_id_added =
            own_id.as_deref() != Some(server_id.as_bytes()) && !previous_ids.contains(&server_id);
        if previous_id_added {
            previous_ids.push(server_id);
            let body = serde_json::to_vec(&previous_ids)
                .map_err(|_| StoreError("the previous Host IDs could not be encoded".into()))?;
            writes.push((
                "setting".into(),
                pairing::PREVIOUS_SERVER_IDS.into(),
                Some(body),
            ));
        }
        Ok((writes, (imported, previous_id_added)))
    })
    .map_err(|error| match error.0.as_str() {
        "the Rust device book has an unreadable row" => {
            "the Rust device book has an unreadable row"
        }
        "a device ID has a different pairing in the Rust book" => {
            "a device ID has a different pairing in the Rust book"
        }
        _ => "the Rust device book could not commit the import",
    })?;
    Ok(json!({
        "imported": imported,
        "already_present": devices.len() - imported,
        "revoked": revoked,
        "previous_ids_added": u8::from(previous_id_added),
    })
    .to_string())
}

fn read_source(path: &Path) -> Result<(String, Vec<Device>, u64), String> {
    let conn = offline_import::source(path)?;
    let server_id: String = conn
        .query_row(
            "SELECT value FROM settings WHERE key = 'server_instance_id'",
            [],
            |row| row.get(0),
        )
        .map_err(|_| "the Python state database has no readable server ID")?;
    let server_id = server_id.trim();
    if server_id.is_empty() {
        return Err("the Python state database has no server ID".into());
    }
    let revoked = conn
        .query_row(
            "SELECT COUNT(*) FROM paired_devices WHERE revoked_at IS NOT NULL",
            [],
            |row| row.get(0),
        )
        .map_err(|_| "the Python paired-device table could not be read")?;
    let mut select = conn
        .prepare(
            "SELECT device_id, name, token_hash, scope, created_at, COALESCE(last_seen_at, 0)
           FROM paired_devices WHERE revoked_at IS NULL ORDER BY device_id",
        )
        .map_err(|_| "the Python paired-device table could not be read")?;
    let mut rows = select
        .query([])
        .map_err(|_| "the Python paired-device table could not be read")?;
    let mut devices = Vec::new();
    while let Some(row) = rows
        .next()
        .map_err(|_| "a Python device row could not be read")?
    {
        devices.push(source_device(row).map_err(|_| "a Python device row is invalid")?);
    }
    Ok((server_id.to_string(), devices, revoked))
}

fn source_device(row: &rusqlite::Row<'_>) -> Result<Device, rusqlite::Error> {
    let invalid = rusqlite::Error::InvalidQuery;
    let scope: String = row.get(3)?;
    let scope = match scope.as_str() {
        "full" => Scope::Full,
        "limited" => Scope::Limited,
        _ => return Err(invalid),
    };
    let device_id: String = row.get(0)?;
    let token_hash: String = row.get(2)?;
    if !device_id
        .strip_prefix("device_")
        .is_some_and(|id| is_hex(id, 32))
        || !is_hex(&token_hash, 64)
    {
        return Err(invalid);
    }
    Ok(Device {
        device_id,
        name: row.get(1)?,
        token_hash,
        scope,
        created_at: row.get(4)?,
        last_seen_at: row.get(5)?,
        revoked_at: None,
    })
}

fn is_hex(value: &str, bytes: usize) -> bool {
    value.len() == bytes
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}
