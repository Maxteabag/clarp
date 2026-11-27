//! Local OpenCode costs and the optional metadata of its configured inference key.
use std::collections::BTreeMap;
use std::net::SocketAddr;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use clarp_kernel::usage::{ProviderUse, Used, WINDOWS};
use serde::Serialize;
use sha2::{Digest, Sha256};

use crate::logins::opencode::{openrouter_key, signed_provider_ids};
use crate::usage_provider::{self, OpenRouterKeyUsage};

const OPENROUTER: &str = "openrouter";

#[derive(Default)]
pub(crate) struct Desk {
    key_usage: Arc<tokio::sync::Mutex<Option<Read>>>,
}

struct Read {
    credential: [u8; 32],
    at: i64,
    report: Remote,
}

#[derive(Clone, Copy, Serialize)]
#[serde(rename_all = "snake_case")]
enum Status {
    Available,
    Unavailable,
    Unsupported,
    Disabled,
}

#[derive(Clone, Serialize)]
struct Remote {
    status: Status,
    source: Option<&'static str>,
    observed_at: Option<String>,
    #[serde(flatten)]
    key_usage: Option<OpenRouterKeyUsage>,
}

impl Remote {
    fn status(status: Status) -> Self {
        Self {
            status,
            source: None,
            observed_at: None,
            key_usage: None,
        }
    }

    fn from_key(at: i64, key_usage: Option<OpenRouterKeyUsage>) -> Self {
        Self {
            status: if key_usage.is_some() {
                Status::Available
            } else {
                Status::Unavailable
            },
            source: Some("openrouter_key"),
            observed_at: key_usage.as_ref().map(|_| crate::wire::iso(at)),
            key_usage,
        }
    }
}

impl Desk {
    async fn openrouter(&self, home: PathBuf, local: Option<SocketAddr>, now: i64) -> Remote {
        let mut cached = self.key_usage.clone().lock_owned().await;
        // The worker owns the guard until the read finishes, even if the app disconnects.
        tokio::task::spawn_blocking(move || {
            let Some(key) = openrouter_key(&home) else {
                *cached = None;
                return Remote::status(Status::Unavailable);
            };
            let stamp = |key: &str| <[u8; 32]>::from(Sha256::digest(key.as_bytes()));
            let credential = stamp(&key);
            if let Some(last) = cached.as_ref().filter(|last| {
                last.credential == credential && !clarp_kernel::usage_ask::due(Some(last.at), now)
            }) {
                return last.report.clone();
            }
            let reading = usage_provider::openrouter_key(&key, local);
            if !openrouter_key(&home).is_some_and(|current| stamp(&current) == credential) {
                *cached = None;
                return Remote::status(Status::Unavailable);
            }
            let report = Remote::from_key(now, reading);
            *cached = Some(Read {
                at: now,
                credential,
                report: report.clone(),
            });
            report
        })
        .await
        .unwrap_or_else(|_| Remote::status(Status::Unavailable))
    }

    pub(super) async fn view(
        &self,
        home: &Path,
        kept: &[Used],
        ask: bool,
        local: Option<SocketAddr>,
        now: i64,
    ) -> impl Serialize {
        let home = home.to_path_buf();
        let lookup = home.clone();
        let mut configured = tokio::task::spawn_blocking(move || {
            let mut ids = signed_provider_ids(&lookup);
            if openrouter_key(&lookup).is_some() {
                ids.insert(OPENROUTER.to_owned());
            }
            ids
        })
        .await
        .unwrap_or_default();
        let mut totals = local_accounting(kept, now);
        let unattributed = totals.remove(&None);
        configured.extend(totals.keys().flatten().cloned());

        let openrouter = if ask && configured.contains(OPENROUTER) {
            self.openrouter(home, local, now).await
        } else {
            Remote::status(Status::Disabled)
        };
        let providers = configured
            .into_iter()
            .map(|id| {
                let remote = match id.as_str() {
                    OPENROUTER => openrouter.clone(),
                    "fireworks-ai" => Remote::status(Status::Unavailable),
                    _ => Remote::status(Status::Unsupported),
                };
                let local = totals.remove(&Some(id.clone()));
                (id, ProviderRow { local, remote })
            })
            .collect();
        Projection {
            source: clarp_kernel::usage::SOURCE,
            providers,
            unattributed,
        }
    }
}

type Windows = BTreeMap<&'static str, Window>;

#[derive(Serialize)]
struct Projection {
    source: &'static str,
    providers: BTreeMap<String, ProviderRow>,
    unattributed: Option<Windows>,
}

#[derive(Serialize)]
struct ProviderRow {
    local: Option<Windows>,
    remote: Remote,
}

#[derive(Serialize)]
struct Window {
    turns: u64,
    window_minutes: i64,
    tokens_in: Option<u64>,
    tokens_out: Option<u64>,
    cost_usd: Option<f64>,
}

impl Window {
    fn new(span: i64) -> Self {
        Self {
            turns: 0,
            window_minutes: span / 60_000,
            tokens_in: None,
            tokens_out: None,
            cost_usd: None,
        }
    }

    fn add(&mut self, tokens_in: Option<u64>, tokens_out: Option<u64>, cost_usd: Option<f64>) {
        self.turns += 1;
        if let Some(tokens) = tokens_in {
            self.tokens_in = Some(self.tokens_in.unwrap_or(0).saturating_add(tokens));
        }
        if let Some(tokens) = tokens_out {
            self.tokens_out = Some(self.tokens_out.unwrap_or(0).saturating_add(tokens));
        }
        if let Some(cost) = cost_usd.filter(|cost| cost.is_finite() && *cost >= 0.0) {
            self.cost_usd = Some(self.cost_usd.unwrap_or(0.0) + cost);
        }
    }
}

fn local_accounting(kept: &[Used], now: i64) -> BTreeMap<Option<String>, Windows> {
    let mut providers: BTreeMap<Option<String>, Windows> = BTreeMap::new();
    for used in kept.iter().filter(|used| {
        matches!(used.tool.as_str(), "opencode" | "deepseek")
            && used.at_ms > now - clarp_kernel::usage::KEPT_FOR_MS
            && used.at_ms <= now
    }) {
        let mut count = |provider, tokens_in, tokens_out, cost_usd| {
            let windows = providers.entry(provider).or_insert_with(|| {
                WINDOWS
                    .into_iter()
                    .map(|(name, span)| (name, Window::new(span)))
                    .collect()
            });
            for (name, span) in WINDOWS {
                if used.at_ms > now - span
                    && let Some(window) = windows.get_mut(name)
                {
                    window.add(tokens_in, tokens_out, cost_usd);
                }
            }
        };
        if used.providers.is_empty() {
            // Legacy records replace missing tokens with zero. Their zero cannot
            // establish a measured zero, and their turn model can be a launch hint.
            count(
                None,
                (used.tokens_in != 0).then_some(used.tokens_in),
                (used.tokens_out != 0).then_some(used.tokens_out),
                used.cost_usd,
            );
        } else {
            for ProviderUse {
                provider_id,
                tokens_in,
                tokens_out,
                cost_micros,
            } in &used.providers
            {
                let provider = provider_id
                    .as_ref()
                    .filter(|id| clarp_kernel::opencode::valid_provider_id(id))
                    .cloned();
                count(
                    provider,
                    *tokens_in,
                    *tokens_out,
                    cost_micros.map(|cost| cost as f64 / 1_000_000.0),
                );
            }
        }
    }
    providers
}
