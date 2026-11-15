//! Who a Host is and what it can do: `GET /server-info` (`contract/schemas/server-info.json`).
//!
//! This is how an app decides whether it can talk to a Host, and how a Host that does not have a
//! feature yet says so: the feature is absent from `capabilities.features`.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ServerInfo {
    pub server_id: String,
    pub name: String,
    pub deployment_mode: String,
    pub version: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub image_version: Option<String>,
    pub default_cwd: String,
    pub clarp_version: String,
    pub min_app_version: String,
    pub capabilities: Capabilities,
    pub contract: Contract,
    /// Present when the client said who it is: whether this Host can serve it.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub client: Option<ClientCompatibility>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub local_connection: Option<LocalConnection>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Capabilities {
    pub version: u64,
    pub features: Vec<String>,
}

impl Capabilities {
    pub fn has(&self, feature: &str) -> bool {
        self.features.iter().any(|f| f == feature)
    }
}

/// The contract numbers: what this Host speaks, the oldest app it still serves, and the contract
/// number at which each feature arrived.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Contract {
    pub host: u64,
    pub min_ios: u64,
    pub features: BTreeMap<String, u64>,
}

#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Compatibility {
    Compatible,
    ClientOutdated,
    Unknown,
    #[serde(untagged)]
    Other(String),
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ClientCompatibility {
    pub status: Compatibility,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub platform: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub build: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub contract: Option<u64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub reason: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct LocalConnection {
    pub enabled: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub port: Option<u16>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub certificate_sha256: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub urls: Option<Vec<String>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub service_type: Option<String>,
}
