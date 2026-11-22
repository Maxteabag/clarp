//! The owner's settings that both Hosts can represent. The migration never opens a
//! runtime record for its value, even to decide whether there is a Rust reader.

use std::fmt::Write as _;
use std::path::Path;

use clarp_ports::kv::{KeyValue, StoreError, Write};
use clarp_store_sqlite::SqliteKeyValue;
use serde_json::{Map, Value, json};

use super::offline_import::{self, Files};

const SETTING: &str = "setting";

fn supported(key: &str) -> bool {
    // Exact names, checked against the Rust read and write paths. Several of these
    // families also have counters, receipts or identity secrets, so prefixes are
    // not authority to copy a whole family.
    matches!(
        key,
        // personas, coordination, capture and account-independent presentation
        "avatars.model_variants"
            | "coordination.visibility"
            | "diagnostics.capture.v1"
            | "personalities.enabled"
            | "tool_explanations.detail_level"
            | "tool_explanations.enabled"
            | "voice.agent_humanness"
            | "voice.humanness"
            // herald and speech settings
            | "herald.disabled"
            | "herald.short_reply_bypass_enabled"
            | "herald.speak_if_short_chars"
            | "speech.fallback"
            | "speech.local_voice"
            | "speech.provider"
            // the sites and controls of the optional judgment service
            | "judgments.enabled"
            | "judgments.errors"
            | "judgments.explanations"
            | "judgments.junk"
            | "judgments.labels"
            | "judgments.naming"
            | "judgments.router"
            | "judgments.timeout_ms"
            | "judgments.watch"
            // the router's addressing mode and the Oracle's two kept selections
            | "orchestrator.addressing_mode"
            | "oracle.contact_session"
            | "oracle.voice_provider"
            // how the Host hears and the separate call policy
            | "transcription.agent_engines"
            | "transcription.agent_languages"
            | "transcription.call_engines"
            | "transcription.call_languages"
            | "transcription.engine"
            | "transcription.guidance.delegation_names"
            | "transcription.guidance.glossary"
            | "transcription.language"
            | "transcription.long_form_model"
            | "transcription.long_form_threshold_sec"
            | "transcription.retain_audio"
            | "transcription.turn_taking"
    ) || ["chat_view.", "coordination.chat."].iter().any(|prefix| {
        // These are per-agent preferences. A client-chat focus or a compound
        // state key is not an agent's preference.
        key.strip_prefix(prefix).is_some_and(|agent| {
            !agent.is_empty()
                && agent
                    .bytes()
                    .all(|byte| byte.is_ascii_alphanumeric() || b"-_".contains(&byte))
        })
    })
}

fn heartbeat_field(key: &str) -> Option<&'static str> {
    match key {
        "heartbeat.interval_sec" => Some("heartbeat_interval_sec"),
        "heartbeat.backoff_strategy" => Some("heartbeat_backoff_strategy"),
        "heartbeat.backoff_cap_sec" => Some("heartbeat_backoff_cap_sec"),
        "heartbeat.dormant_after_noops" => Some("heartbeat_dormant_after_noops"),
        _ => None,
    }
}

fn heartbeat_value(field: &str, value: &str) -> Result<Value, StoreError> {
    let value = value.trim();
    if field == "heartbeat_backoff_strategy" {
        Ok(json!(value))
    } else {
        let number: i64 = value
            .parse()
            .map_err(|_| StoreError("invalid Python heartbeat integer".into()))?;
        Ok(json!(number))
    }
}

type Rows = Vec<(String, Option<String>)>;

fn read(path: &Path) -> Result<Rows, String> {
    let conn = offline_import::source(path)?;
    let mut keys = conn
        .prepare("SELECT key FROM settings ORDER BY key")
        .map_err(|_| "the Python settings table could not be read")?;
    let mut value = conn
        .prepare("SELECT value FROM settings WHERE key = ?1")
        .map_err(|_| "the Python settings table could not be read")?;
    let keys = keys
        .query_map([], |row| row.get::<_, String>(0))
        .map_err(|_| "the Python setting keys could not be read")?;
    let mut rows = Vec::new();
    for key in keys {
        let key = key.map_err(|_| "a Python setting key could not be read")?;
        let setting = if supported(&key) || heartbeat_field(&key).is_some() {
            Some(
                value
                    .query_row([&key], |row| row.get(0))
                    .map_err(|_| "a supported Python setting could not be read")?,
            )
        } else {
            None
        };
        rows.push((key, setting));
    }
    Ok(rows)
}

fn compare(store: &dyn KeyValue, rows: Rows) -> Result<(Vec<Write>, String), StoreError> {
    let heartbeat: Map<String, Value> = rows
        .iter()
        .filter_map(|(key, value)| Some((heartbeat_field(key)?, value.as_deref()?)))
        .map(|(field, value)| Ok((field.to_owned(), heartbeat_value(field, value)?)))
        .collect::<Result<_, StoreError>>()?;
    let mut writes = Vec::new();
    let before = if heartbeat.is_empty() {
        Value::Null
    } else {
        let (previous, write) =
            crate::heartbeat::plan_import_settings(store, Value::Object(heartbeat.clone()))?;
        writes.extend(write);
        previous.shown()
    };
    let mut receipt = String::new();
    for (key, value) in rows {
        let status = if let Some(field) = heartbeat_field(&key) {
            if before.get(field) == heartbeat.get(field) {
                "equal"
            } else {
                "copied"
            }
        } else {
            match value {
                Some(value) if store.get(SETTING, &key)?.as_deref() == Some(value.as_bytes()) => {
                    "equal"
                }
                Some(value) => {
                    writes.push((SETTING.into(), key.clone(), Some(value.into_bytes())));
                    "copied"
                }
                None if key.starts_with("orchestrator.") => "carried by janitor configs",
                None => "no reader",
            }
        };
        // One output line per key, with no value. A stored key cannot inject a line.
        let _ = writeln!(receipt, "{}\t{status}", key.escape_debug());
    }
    let _ = receipt.pop();
    Ok((writes, receipt))
}

pub(super) fn run(args: impl Iterator<Item = String>) -> Result<String, String> {
    let files = Files::parse(args, "settings import", true)?;
    let rows = read(&files.source)?;
    let answer = if files.dry_run {
        SqliteKeyValue::read_existing(&files.target, |store| {
            compare(store, rows).map(|(_, receipt)| receipt)
        })
    } else {
        SqliteKeyValue::update_existing(&files.target, |store| compare(store, rows))
    };
    answer.map_err(|error| {
        if error.0.starts_with("heartbeat_")
            || error.0 == "invalid heartbeat_backoff_strategy"
            || error.0 == "invalid Python heartbeat integer"
        {
            error.0
        } else {
            "the Rust settings store could not complete the import".into()
        }
    })
}
