//! The Linux Host's curated transcription models, in the order shown to an app.

use serde_json::{Value, json};

pub fn catalog(installed: &[String]) -> Vec<Value> {
    let mut rows = Vec::new();
    for (provider, model, name, weight, bytes, language, platform) in [
        (
            "faster-whisper",
            "base.en",
            "Base English",
            "light",
            148_000_000,
            "en",
            "linux",
        ),
        (
            "faster-whisper",
            "small.en",
            "Small English",
            "medium",
            488_000_000,
            "en",
            "linux",
        ),
        (
            "faster-whisper",
            "small",
            "Small Multilingual",
            "medium",
            488_000_000,
            "multilingual",
            "linux",
        ),
        (
            "faster-whisper",
            "medium",
            "Medium",
            "heavy",
            1_530_000_000,
            "multilingual",
            "linux",
        ),
        (
            "faster-whisper",
            "large-v3-turbo",
            "Large v3 Turbo",
            "very-heavy",
            1_620_000_000,
            "multilingual",
            "linux",
        ),
        (
            "whisper.cpp",
            "base.en",
            "whisper.cpp Base English",
            "light",
            148_000_000,
            "en",
            "macos",
        ),
        (
            "whisper.cpp",
            "small.en",
            "whisper.cpp Small English",
            "medium",
            488_000_000,
            "en",
            "macos",
        ),
    ] {
        let id = format!("{provider}:{model}");
        let present = installed.contains(&id);
        let supported = platform == "linux";
        let status = if present {
            "installed"
        } else if supported {
            "available"
        } else {
            "unsupported"
        };
        rows.push(
            json!({"id": id, "model": model, "name": name, "weight": weight,
            "provider": provider, "download_bytes": bytes, "languages": [language],
            "platforms": [platform], "recommended": id == "faster-whisper:small.en",
            "supported": supported, "installed": present, "status": status}),
        );
    }
    rows
}

/// Registry metadata which may be returned in the capability list.
pub fn public_record(record: &Value) -> Option<Value> {
    let mut fields = record.as_object()?.clone();
    fields.retain(|name, _| !name.starts_with('_'));
    Some(Value::Object(fields))
}

/// A Linux faster-whisper record requires all three files at its recorded path.
pub fn installed(record: &Value, file_exists: impl Fn(&str) -> bool) -> bool {
    record["provider"] == "faster-whisper"
        && ["config.json", "model.bin", "tokenizer.json"]
            .into_iter()
            .all(file_exists)
}
