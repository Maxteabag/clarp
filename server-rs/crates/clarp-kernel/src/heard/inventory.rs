//! The Linux Host's curated transcription models, in the order shown to an app.

use serde_json::{Value, json};

/// Sources are fixed by the Host release. Clients name a catalog id only.
pub struct InstallSource {
    pub repository: &'static str,
    pub revision: &'static str,
}

pub struct CatalogModel {
    pub model: &'static str,
    provider: &'static str,
    name: &'static str,
    weight: &'static str,
    pub bytes: u64,
    language: &'static str,
    platform: &'static str,
    pub source: Option<InstallSource>,
}

pub const CATALOG: &[CatalogModel] = &[
    CatalogModel {
        model: "base.en",
        provider: "faster-whisper",
        name: "Base English",
        weight: "light",
        bytes: 148_000_000,
        language: "en",
        platform: "linux",
        source: Some(InstallSource {
            repository: "Systran/faster-whisper-base.en",
            revision: "3d3d5dee26484f91867d81cb899cfcf72b96be6c",
        }),
    },
    CatalogModel {
        model: "small.en",
        provider: "faster-whisper",
        name: "Small English",
        weight: "medium",
        bytes: 488_000_000,
        language: "en",
        platform: "linux",
        source: Some(InstallSource {
            repository: "Systran/faster-whisper-small.en",
            revision: "d1d751a5f8271d482d14ca55d9e2deeebbae577f",
        }),
    },
    CatalogModel {
        model: "small",
        provider: "faster-whisper",
        name: "Small Multilingual",
        weight: "medium",
        bytes: 488_000_000,
        language: "multilingual",
        platform: "linux",
        source: Some(InstallSource {
            repository: "Systran/faster-whisper-small",
            revision: "536b0662742c02347bc0e980a01041f333bce120",
        }),
    },
    CatalogModel {
        model: "medium",
        provider: "faster-whisper",
        name: "Medium",
        weight: "heavy",
        bytes: 1_530_000_000,
        language: "multilingual",
        platform: "linux",
        source: Some(InstallSource {
            repository: "Systran/faster-whisper-medium",
            revision: "08e178d48790749d25932bbc082711ddcfdfbc4f",
        }),
    },
    CatalogModel {
        model: "large-v3-turbo",
        provider: "faster-whisper",
        name: "Large v3 Turbo",
        weight: "very-heavy",
        bytes: 1_620_000_000,
        language: "multilingual",
        platform: "linux",
        source: Some(InstallSource {
            repository: "deepdml/faster-whisper-large-v3-turbo-ct2",
            revision: "4df90f75321148c3a29a9e2351b7ddf8f5b115a8",
        }),
    },
    CatalogModel {
        model: "base.en",
        provider: "whisper.cpp",
        name: "whisper.cpp Base English",
        weight: "light",
        bytes: 148_000_000,
        language: "en",
        platform: "macos",
        source: None,
    },
    CatalogModel {
        model: "small.en",
        provider: "whisper.cpp",
        name: "whisper.cpp Small English",
        weight: "medium",
        bytes: 488_000_000,
        language: "en",
        platform: "macos",
        source: None,
    },
];

pub fn install_model(id: &str) -> Result<&'static CatalogModel, String> {
    let model = CATALOG
        .iter()
        .find(|m| id.split_once(':') == Some((m.provider, m.model)))
        .ok_or_else(|| format!("unsupported transcription model: {id}"))?;
    if model.source.is_none() {
        return Err(format!("{id} is not supported on linux"));
    }
    Ok(model)
}

impl CatalogModel {
    pub fn id(&self) -> String {
        format!("{}:{}", self.provider, self.model)
    }

    pub fn installed_record(&self) -> Value {
        let mut row = json!({"id":self.id(), "provider":self.provider,"model":self.model,"name":self.name,
               "weight":self.weight,"download_bytes":self.bytes,"languages":[self.language],
               "platforms":[self.platform]});
        if self.id() == "faster-whisper:small.en" {
            row["recommended"] = json!(true);
        }
        row
    }

    pub fn maximum_bytes(&self) -> u64 {
        self.bytes + self.bytes / 4
    }
}

/// Space for downloads already authorized must be kept available when admitting another.
pub fn space_available(free: u64, reserved: u64, remaining: u64) -> bool {
    const MARGIN: u64 = 256 * 1024 * 1024;
    free.checked_sub(MARGIN)
        .and_then(|free| free.checked_sub(reserved))
        .is_some_and(|free| free >= remaining)
}

/// Staging from an interrupted job is retained for another explicit installation.
pub fn discard_staging(status: Option<crate::jobs::JobStatus>, age: std::time::Duration) -> bool {
    use crate::jobs::JobStatus;
    match status {
        Some(JobStatus::Cancelled) => true,
        Some(_) => false,
        None => age > std::time::Duration::from_secs(7 * 24 * 3600),
    }
}

pub fn catalog(installed: &[String]) -> Vec<Value> {
    let mut rows = Vec::new();
    for m in CATALOG {
        let (provider, model, name, weight, bytes, language, platform) = (
            m.provider, m.model, m.name, m.weight, m.bytes, m.language, m.platform,
        );
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
