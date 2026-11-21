//! The seats of a machine's accounts, as an app is told them: the strict, redacted answer of
//! `GET /hotseat/status`.
//!
//! The owner's own tool (Hotseat) knows every saved account of Claude and of Codex and how
//! much of each is left. Whoever reads the tool hands what it answered here
//! ([`Seats::read`]); a read that failed and what the switcher did with a read are handed
//! here too ([`Seats::failed`], [`Seats::acted`]). What is kept of a reading is only what
//! passes an allowlist: a ratio, a moment, a kind of window, a state out of four. An alias
//! that looks like an address is redacted, and anything that looks like a token, a path or a
//! link is shown as `account`. Each account is bound to a keyed digest of who it is, which
//! is kept and never served, so that an old reading is never shown under an account it does
//! not belong to. Serving ([`Seats::served`]) reads what is kept and nothing else: it never
//! asks the tool, so opening the view costs no read.
//!
//! This is the Python Host's `hotseat_status.py` (wire schema `clarp.hotseat-status/1`),
//! held to that contract's own shared fixtures (`tests/fixtures/hotseat_status` of the
//! repository: what the tool answered and what the switcher did, and the exact body that is
//! served for it).

use std::collections::BTreeMap;
use std::sync::LazyLock;

use regex::Regex;
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};

pub const SCHEMA: &str = "clarp.hotseat-status/1";
/// The tools whose accounts are told, in the order they are told in; and those of them that
/// are read at all (of the third nothing is read: it is unavailable, never empty).
pub const PROVIDERS: [&str; 3] = ["claude", "codex", "muse"];
pub const READ_PROVIDERS: [&str; 2] = ["claude", "codex"];
/// How old a reading may be before it is stale, at the least, in seconds.
const STALE_FLOOR_S: i64 = 600;
const MAX_ACCOUNTS: usize = 32;
const MAX_WINDOWS: usize = 8;
const MAX_ALIAS: usize = 32;
const MAX_LABEL: usize = 40;
const ERROR_CODES: [&str; 7] = [
    "auth_error",
    "exhausted",
    "timeout",
    "cli_failed",
    "parse_failed",
    "provider_error",
    "disabled",
];
const ACTION_KINDS: [&str; 6] = [
    "switched",
    "would_switch",
    "noop",
    "exhausted",
    "skipped",
    "failed",
];
const REASON_CODES: [&str; 6] = [
    "no_active_account",
    "quota_unknown",
    "already_ok",
    "below_floor",
    "switch_failed",
    "no_better_account",
];
const MODEL_FAMILIES: [&str; 6] = ["opus", "sonnet", "haiku", "fable", "gpt-5", "spark"];
const PLANS: [(&str, &str); 8] = [
    ("free", "Free"),
    ("pro", "Pro"),
    ("max", "Max"),
    ("team", "Team"),
    ("plus", "Plus"),
    ("business", "Business"),
    ("enterprise", "Enterprise"),
    ("edu", "Edu"),
];
const COST_NOTE: &str = "Numbers come from the Hotseat switcher's own scheduled reads, which may probe or \
                         refresh provider quota; opening this view adds no read.";

static EMAIL: LazyLock<Regex> = LazyLock::new(|| pattern(r"^[^\s@]+@[^\s@]+$"));
static TOKENISH: LazyLock<Regex> = LazyLock::new(|| pattern(r"[A-Za-z0-9+=_]{20,}"));
static SAFE_ALIAS: LazyLock<Regex> = LazyLock::new(|| pattern(r"^[A-Za-z0-9][A-Za-z0-9 ._\-]*$"));
static PLAN_LABEL: LazyLock<Regex> =
    LazyLock::new(|| pattern(r"^(Free|Pro|Max|Team|Plus|Business|Enterprise|Edu)( [0-9]{1,2}×)?$"));
static NUMBERED: LazyLock<Regex> = LazyLock::new(|| pattern(r"^(.+) \(([0-9]{1,2})\)$"));

fn pattern(source: &str) -> Regex {
    // The patterns are written here, not read from anywhere: one that does not compile is a
    // mistake in this file, which the tests of this module would not get past.
    #[allow(clippy::expect_used)]
    Regex::new(source).expect("a pattern of this module compiles")
}

/// A number that is one, and no yes or no.
fn number(value: &Value) -> Option<f64> {
    value.as_f64().filter(|_| value.is_number())
}

/// A whole number above nothing, as a moment in milliseconds is kept (`_int_ms`).
fn int_ms(value: &Value) -> Option<i64> {
    value.as_i64().filter(|at| *at > 0 && !value.is_f64())
}

/// A used part of the whole, 0 to 1, to four places; nothing for a number that is none, is
/// below nothing or far above the whole (`_ratio`). A missing number is never 0.
fn ratio(value: &Value) -> Option<f64> {
    let said = number(value).filter(|said| said.is_finite() && (0.0..=1.5).contains(said))?;
    format!("{:.4}", said.min(1.0)).parse().ok()
}

/// Seconds since 1970 as milliseconds (`_epoch_ms`).
fn epoch_ms(seconds: &Value) -> Option<i64> {
    let said = number(seconds).filter(|said| said.is_finite() && *said > 0.0)?;
    Some((said * 1000.0).round_ties_even() as i64)
}

/// A text as Python's `str()` writes a value: what an identity is made of.
fn written(value: &Value) -> String {
    match value {
        Value::Null => String::new(),
        Value::String(text) => text.clone(),
        other => crate::python::repr(other),
    }
}

/// HMAC-SHA-256 (RFC 2104) of a message under a key.
pub fn keyed(key: &[u8], message: &[u8]) -> [u8; 32] {
    let mut block = [0u8; 64];
    if key.len() > block.len() {
        block[..32].copy_from_slice(&Sha256::digest(key));
    } else {
        block[..key.len()].copy_from_slice(key);
    }
    let pad = |with: u8| block.map(|byte| byte ^ with);
    let inner = Sha256::new()
        .chain_update(pad(0x36))
        .chain_update(message)
        .finalize();
    let outer = Sha256::new().chain_update(pad(0x5c)).chain_update(inner);
    outer.finalize().into()
}

/// What may be shown for an alias: itself where it is plainly safe, else a redaction
/// (`display_alias`).
pub fn display_alias(alias: &Value) -> String {
    let text = match alias {
        Value::Null => String::new(),
        other if !crate::python::truthy(other) => String::new(),
        other => written(other),
    };
    let text = crate::python::strip(&text);
    if EMAIL.is_match(text) {
        let (local, domain) = text.split_once('@').unwrap_or((text, ""));
        let first = |part: &str| part.chars().take(1).collect::<String>();
        return format!("{}…@{}…", first(local), first(domain));
    }
    let unsafe_marker = ["://", "/", "\\", "~"]
        .iter()
        .any(|mark| text.contains(mark));
    let plain = !text.is_empty()
        && text.chars().count() <= MAX_ALIAS
        && !TOKENISH.is_match(text)
        && !text.contains('@')
        && !unsafe_marker
        && SAFE_ALIAS.is_match(text);
    if plain {
        text.to_string()
    } else {
        "account".to_string()
    }
}

/// A kept alias, looked at once more at the door: this file's own redaction, with its
/// number where two redactions were the same (`_served_alias`).
fn served_alias(value: &Value) -> Option<String> {
    if value.is_null() {
        return None;
    }
    let text = written(value);
    Some(match NUMBERED.captures(&text) {
        Some(found) => format!("{} ({})", display_alias(&json!(&found[1])), &found[2]),
        None => display_alias(&json!(text)),
    })
}

fn plan(value: &Value) -> Option<&'static str> {
    let said = crate::python::strip(value.as_str()?).to_lowercase();
    PLANS
        .iter()
        .find(|(name, _)| *name == said)
        .map(|(_, shown)| *shown)
}

fn plan_label(value: &Value) -> Option<String> {
    let said = crate::python::strip(value.as_str()?);
    PLAN_LABEL
        .is_match(said)
        .then(|| said.chars().take(MAX_LABEL).collect())
}

fn family(model: &Value) -> Option<&'static str> {
    let text = if crate::python::truthy(model) {
        written(model).to_lowercase()
    } else {
        String::new()
    };
    MODEL_FAMILIES
        .into_iter()
        .find(|family| text.contains(family))
}

/// A family's name with its first letter large, as `str.capitalize` writes it.
fn capitalized(family: &str) -> String {
    let mut letters = family.chars();
    match letters.next() {
        Some(first) => first
            .to_uppercase()
            .chain(letters.flat_map(char::to_lowercase))
            .collect(),
        None => String::new(),
    }
}

fn window(kind: &str, label: &str, used: &Value, reset_ms: Option<i64>, observed_at: i64) -> Value {
    json!({
        "kind": kind, "label": label, "used": ratio(used), "resets_at": reset_ms,
        "observed_at": observed_at, "source": "hotseat-cli",
    })
}

/// How a sample ranks among those of its kind: the one that binds most first (the highest
/// used), one without a number last, the tool's own before any other source, then by label.
fn rank(window: &Value) -> (bool, f64, usize, String) {
    let used = window["used"].as_f64();
    let source = match window["source"].as_str() {
        Some("hotseat-cli") => 0,
        Some("usage-worker") => 1,
        _ => 2,
    };
    (
        used.is_none(),
        -used.unwrap_or(0.0),
        source,
        written(&window["label"]),
    )
}

/// One window of a kind: the sample that binds most. Says whether any was dropped.
fn unique(windows: Vec<Value>) -> (Vec<Value>, bool) {
    let count = windows.len();
    let mut best: Vec<Value> = Vec::new();
    for sample in windows {
        let held = best.iter_mut().find(|held| held["kind"] == sample["kind"]);
        match held {
            None => best.push(sample),
            Some(held) => {
                let (new, old) = (rank(&sample), rank(held));
                if new.partial_cmp(&old) == Some(std::cmp::Ordering::Less) {
                    *held = sample;
                }
            }
        }
    }
    let dropped = best.len() < count;
    (best, dropped)
}

fn numbers(windows: &[Value]) -> bool {
    windows.iter().any(|window| !window["used"].is_null())
}

/// That a login no longer holds is said by a typed field, never by a text.
fn typed_auth(row: &Value) -> bool {
    row["error_code"] == "auth_error"
}

/// What is kept of the accounts of the machine: of each tool the last reading, what the
/// switcher last did, and how the switcher is set; and the key identities are made with.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct Seats {
    /// A secret of this Host's own, so that a kept identity cannot be matched to an address.
    salt: String,
    snapshots: BTreeMap<String, Value>,
    actions: BTreeMap<String, Value>,
    lanes: BTreeMap<String, Value>,
}

impl Seats {
    /// With a secret drawn for it: nothing is read or kept without one.
    pub fn with_salt(salt: &str) -> Self {
        Self {
            salt: salt.to_string(),
            ..Self::default()
        }
    }

    pub fn has_salt(&self) -> bool {
        !self.salt.is_empty()
    }

    /// A keyed digest of parts that say who or what: kept, never served (`_keyed`).
    fn keyed(&self, parts: &[&Value]) -> String {
        let said: Vec<String> = parts.iter().map(|part| written(part)).collect();
        let digest = keyed(self.salt.as_bytes(), said.join("\0").as_bytes());
        let hex: String = digest.iter().map(|byte| format!("{byte:02x}")).collect();
        hex[..32].to_string()
    }

    fn identity(
        &self,
        provider: &str,
        row: &Value,
        email: &Value,
        org: &Value,
        id: &Value,
    ) -> String {
        let (kind, provider) = (json!("identity"), json!(provider));
        self.keyed(&[&kind, &provider, &row["alias"], email, org, id])
    }

    fn alias_tag(&self, provider: &str, alias: &Value) -> String {
        self.keyed(&[&json!("alias"), &json!(provider), alias])
    }

    fn claude_account(&self, provider: &str, row: &Value, observed_at: i64) -> Value {
        let nothing = json!({});
        let usage = Some(&row["usage"])
            .filter(|usage| usage.is_object())
            .unwrap_or(&nothing);
        let truthy = crate::python::truthy;
        let mut windows = Vec::new();
        // A row whose read failed gives no samples: its numbers may be old or partial, and
        // what was last known good of the same account stays.
        if truthy(usage) && !truthy(&row["error"]) && !typed_auth(row) {
            windows.push(window(
                "5h",
                "5-hour",
                &usage["used_5h"],
                epoch_ms(&usage["reset_5h"]),
                observed_at,
            ));
            windows.push(window(
                "7d",
                "7-day",
                &usage["used_7d"],
                epoch_ms(&usage["reset_7d"]),
                observed_at,
            ));
            for scoped in usage["scoped"].as_array().into_iter().flatten() {
                let of = Some(scoped).filter(|scoped| scoped.is_object());
                let family = of.and_then(|scoped| family(&scoped["model"]));
                if let Some(family) = family.filter(|_| ratio(&scoped["used"]).is_some()) {
                    let (kind, label) = (
                        format!("model:{family}"),
                        format!("{} weekly", capitalized(family)),
                    );
                    windows.push(window(
                        &kind,
                        &label,
                        &scoped["used"],
                        epoch_ms(&scoped["reset"]),
                        observed_at,
                    ));
                }
            }
        }
        let (windows, dropped) = unique(windows);
        let (state, code) = if typed_auth(row) {
            ("auth_error", Some("auth_error"))
        } else if truthy(&row["error"]) {
            ("unknown", Some("provider_error"))
        } else if usage["limited"] == true || usage["status"] == "rejected" {
            ("exhausted", Some("exhausted"))
        } else if numbers(&windows) {
            ("ok", None)
        } else {
            ("unknown", None)
        };
        json!({
            "identity": self.identity(provider, row, &row["email"], &row["org"], &Value::Null),
            "tag": self.alias_tag(provider, &row["alias"]), "alias": display_alias(&row["alias"]),
            "plan": plan(&row["plan"]), "label": plan_label(&row["plan_label"]),
            "active": row["is_active"] == true, "state": state, "error_code": code,
            "windows": windows, "dropped": dropped,
        })
    }

    fn codex_account(&self, provider: &str, row: &Value, observed_at: i64) -> Value {
        let truthy = crate::python::truthy;
        let usage = Some(&row["usage"]).filter(|usage| usage.is_object());
        let said = |field: &str| usage.map_or(&Value::Null, |usage| &usage[field]);
        let usage_failed = usage
            .is_some_and(|usage| truthy(usage) && (typed_auth(usage) || truthy(&usage["error"])));
        let failed = typed_auth(row) || usage_failed;
        let mut windows = Vec::new();
        // A row that failed gives no samples, also where it still carries numbers.
        for raw in said("windows")
            .as_array()
            .into_iter()
            .flatten()
            .filter(|_| !failed)
        {
            let Some(label) = raw["label"].as_str().filter(|_| raw.is_object()) else {
                continue;
            };
            if let Some((kind, shown)) = codex_kind(provider, label) {
                windows.push(window(
                    &kind,
                    &shown,
                    &raw["used"],
                    epoch_ms(&raw["reset"]),
                    observed_at,
                ));
            }
        }
        let (windows, dropped) = unique(windows);
        let usage_auth = usage.is_some_and(|usage| truthy(usage) && typed_auth(usage));
        let (state, code) = if typed_auth(row) || usage_auth {
            ("auth_error", Some("auth_error"))
        } else if usage.is_none() {
            ("unknown", None)
        } else if truthy(said("error")) {
            ("unknown", Some("provider_error"))
        } else if said("blocked") == &json!(true) {
            ("exhausted", Some("exhausted"))
        } else if said("usable") == &json!(false) {
            // Not usable, and no typed block: why is not known.
            ("unknown", None)
        } else if numbers(&windows) {
            ("ok", None)
        } else {
            ("unknown", None)
        };
        json!({
            "identity": self.identity(provider, row, &row["email"], &Value::Null, &row["account_id"]),
            "tag": self.alias_tag(provider, &row["alias"]), "alias": display_alias(&row["alias"]),
            "plan": plan(&row["plan"]), "label": null, "active": row["is_active"] == true,
            "state": state, "error_code": code, "windows": windows, "dropped": dropped,
            "failed": failed, "no_reset": no_reset(said("no_reset_reason")),
        })
    }

    /// The allowlisted reading of what the tool answered for a provider. `None` for a shape
    /// that cannot be trusted (`parse`).
    fn parse(&self, provider: &str, payload: &Value, observed_at: i64) -> Option<Value> {
        let listed = payload.get("accounts")?.as_array()?;
        let rows: Vec<&Value> = listed
            .iter()
            .filter(|row| row.is_object() && crate::python::truthy(&row["alias"]))
            .collect();
        let mut accounts: Vec<Value> = rows
            .iter()
            .take(MAX_ACCOUNTS)
            .map(|row| match provider {
                "claude" => self.claude_account(provider, row, observed_at),
                _ => self.codex_account(provider, row, observed_at),
            })
            .collect();
        let mut partial = rows.len() > MAX_ACCOUNTS
            || accounts.iter().any(|account| account["state"] == "unknown");
        for account in &mut accounts {
            let dropped = account
                .as_object_mut()
                .and_then(|fields| fields.remove("dropped"));
            partial |= dropped == Some(json!(true));
        }
        // The quota of Codex's accounts was not read this time.
        if provider == "codex" && payload["quota_read"] == false {
            partial = true;
        }
        for account in &mut accounts {
            if let Some(windows) = account["windows"].as_array_mut()
                && windows.len() > MAX_WINDOWS
            {
                windows.truncate(MAX_WINDOWS);
                partial = true;
            }
        }
        // Two redactions may be the same: the rows stay apart by a number.
        let mut seen: BTreeMap<String, u32> = BTreeMap::new();
        for account in &mut accounts {
            let alias = written(&account["alias"]);
            let count = seen.entry(alias.clone()).or_insert(0);
            *count += 1;
            if *count > 1 {
                account["alias"] = json!(format!("{alias} ({count})"));
            }
        }
        let inventory = if accounts.is_empty() { "empty" } else { "ok" };
        Some(json!({ "accounts": accounts, "partial": partial, "inventory": inventory }))
    }

    /// What the tool answered for a provider is kept, as far as the allowlist lets it
    /// (`record_read`). `received_at`: when it was answered.
    pub fn read(&mut self, provider: &str, payload: &Value, received_at: i64) {
        if !READ_PROVIDERS.contains(&provider) {
            return;
        }
        // A reading that was kept by the tool says how old it is.
        let age = (provider == "codex" && payload.is_object()).then_some(&payload["quota_age_s"]);
        let age = age
            .and_then(number)
            .filter(|age| age.is_finite() && *age > 0.0);
        let stamp = received_at - age.map_or(0, |age| (age * 1000.0) as i64);
        let previous = self.snapshots.get(provider).cloned();
        let whole = |value: &Value| value.as_i64().filter(|_| !value.is_f64());
        let kept_at = previous
            .as_ref()
            .and_then(|kept| whole(&kept["observed_at"]));
        if kept_at.is_some_and(|kept| kept > stamp) {
            // An older read that ends late never takes the place of a newer one.
            return;
        }
        let Some(reading) = self.parse(provider, payload, stamp) else {
            self.store_failure(provider, "parse_failed", received_at);
            return;
        };
        let accounts = merge(previous.as_ref(), reading["accounts"].clone(), stamp);
        let kept = json!({
            "v": 1, "observed_at": stamp, "attempt_at": received_at, "read_state": "ok",
            "error_code": null, "inventory": reading["inventory"], "partial": reading["partial"],
            "accounts": accounts,
        });
        self.snapshots.insert(provider.to_string(), kept);
    }

    fn store_failure(&mut self, provider: &str, code: &str, at: i64) {
        let nothing = json!({ "v": 1, "observed_at": null, "inventory": null, "partial": false, "accounts": [] });
        let mut kept = self.snapshots.get(provider).cloned().unwrap_or(nothing);
        kept["attempt_at"] = json!(at);
        kept["read_state"] = json!("failed");
        kept["error_code"] = json!(code);
        self.snapshots.insert(provider.to_string(), kept);
    }

    /// A read that failed, and how (`timeout`, `parse_failed`, `cli_failed`, or
    /// `provider_error`): only that changes; what was last known good and what the switcher
    /// last did stay (`record_failure`).
    pub fn failed(&mut self, provider: &str, code: &str, now: i64) {
        self.store_failure(provider, code, now);
    }

    /// What the switcher did with a read: its branch, as a kind and a reason out of a closed
    /// list, with the aliases redacted; and how it is set (`record_action`).
    pub fn acted(&mut self, provider: &str, did: &Acted<'_>) {
        let Some((kind, reason)) = branch(did.decision, did.mode, did.switched) else {
            // No branch the switcher has: nothing is written.
            return;
        };
        let snapshot = self.snapshots.get(provider);
        let alias = |alias: &Value| -> Value {
            if !crate::python::truthy(alias) {
                return Value::Null;
            }
            let tag = self.alias_tag(provider, alias);
            let accounts = snapshot.and_then(|kept| kept["accounts"].as_array());
            let known = accounts
                .into_iter()
                .flatten()
                .find(|account| account["tag"] == tag.as_str());
            known.map_or_else(
                || json!(display_alias(alias)),
                |account| account["alias"].clone(),
            )
        };
        let target = (did.decision["action"] == "switch").then_some(&did.decision["target"]);
        let action = json!({
            "kind": kind, "at": did.now, "reason_code": reason,
            "from_alias": alias(&did.decision["active"]),
            "to_alias": alias(target.unwrap_or(&Value::Null)),
        });
        let whole = |value: &Value| value.as_i64().filter(|_| value.is_i64() || value.is_u64());
        let lane = json!({
            "mode": Some(did.mode).filter(|mode| matches!(*mode, "automatic" | "notify")),
            "floor_pct": whole(did.floor_pct),
            "cadence_s": whole(did.cadence_s).filter(|cadence| *cadence > 0),
            "last_run_at": did.now, "next_due_at": int_ms(did.next_due_at),
        });
        self.actions.insert(provider.to_string(), action);
        self.lanes.insert(provider.to_string(), lane);
    }

    /// The body an app is given, made of what is kept and of nothing else (`payload`).
    /// `enabled`: whether a switcher that is switched on reads the tool on this Host.
    pub fn served(&self, enabled: bool, served_at: i64) -> Value {
        let lanes: Vec<Value> = READ_PROVIDERS
            .iter()
            .filter_map(|provider| self.served_lane(provider, enabled))
            .collect();
        let actions: Vec<Value> = READ_PROVIDERS
            .iter()
            .filter_map(|provider| self.served_action(provider))
            .collect();
        let providers: Vec<Value> = PROVIDERS
            .iter()
            .map(|provider| self.served_provider(provider, enabled, served_at))
            .collect();
        json!({
            "schema": SCHEMA,
            "served_at": served_at,
            "source": { "cli_version": null, "cost_note": COST_NOTE },
            "switcher": { "kind": "deterministic", "model": null, "enabled": enabled, "lanes": lanes },
            "actions": actions,
            "providers": providers,
        })
    }

    fn served_lane(&self, provider: &str, enabled: bool) -> Option<Value> {
        let lane = self.lanes.get(provider)?;
        let whole = |value: &Value| value.as_i64().filter(|_| value.is_i64() || value.is_u64());
        let mode = lane["mode"]
            .as_str()
            .filter(|mode| matches!(*mode, "automatic" | "notify"));
        Some(json!({
            "provider": provider, "mode": mode,
            "floor_pct": whole(&lane["floor_pct"]), "cadence_s": whole(&lane["cadence_s"]),
            "last_run_at": int_ms(&lane["last_run_at"]),
            "next_due_at": int_ms(&lane["next_due_at"]).filter(|_| enabled),
        }))
    }

    fn served_action(&self, provider: &str) -> Option<Value> {
        let action = self.actions.get(provider)?;
        let kind = action["kind"]
            .as_str()
            .filter(|kind| ACTION_KINDS.contains(kind))?;
        let reason = action["reason_code"]
            .as_str()
            .filter(|code| REASON_CODES.contains(code))?;
        Some(json!({
            "provider": provider, "kind": kind, "at": int_ms(&action["at"]), "reason_code": reason,
            "from_alias": served_alias(&action["from_alias"]),
            "to_alias": served_alias(&action["to_alias"]),
        }))
    }

    fn served_provider(&self, provider: &str, enabled: bool, served_at: i64) -> Value {
        let whole = |value: &Value| value.as_i64().filter(|_| value.is_i64() || value.is_u64());
        let cadence = self
            .lanes
            .get(provider)
            .and_then(|lane| whole(&lane["cadence_s"]));
        let unavailable = |code: Option<&str>, cadence: Option<i64>| {
            json!({
                "provider": provider, "inventory": "unavailable", "observed_at": null, "stale": true,
                "partial": false, "read_state": "never", "error_code": code, "cadence_s": cadence,
                "accounts": [],
            })
        };
        if !READ_PROVIDERS.contains(&provider) {
            return unavailable(None, None);
        }
        // Nothing read yet, which is not the same as no account.
        let Some(snapshot) = self.snapshots.get(provider) else {
            return unavailable((!enabled).then_some("disabled"), cadence);
        };
        let observed_at = int_ms(&snapshot["observed_at"]);
        let read_state = match (snapshot["read_state"] == "failed", observed_at) {
            (true, _) => "failed",
            (false, Some(_)) => "ok",
            (false, None) => "never",
        };
        let stale_after = stale_after_ms(cadence);
        let old = observed_at.is_none_or(|at| served_at - at > stale_after);
        let stale = !enabled || read_state != "ok" || old;
        let code = snapshot["error_code"]
            .as_str()
            .filter(|code| ERROR_CODES.contains(code));
        let code = if enabled { code } else { Some("disabled") };
        let source_failed = !enabled || read_state != "ok";
        let accounts: Vec<Value> = snapshot["accounts"]
            .as_array()
            .into_iter()
            .flatten()
            .take(MAX_ACCOUNTS)
            .filter(|account| account.is_object())
            .map(|account| served_account(account, source_failed, stale_after, served_at))
            .collect();
        let inventory = snapshot["inventory"]
            .as_str()
            .filter(|inventory| matches!(*inventory, "ok" | "empty"))
            .unwrap_or("unavailable");
        let partial = crate::python::truthy(&snapshot["partial"])
            || accounts.iter().any(|account| account["state"] == "unknown");
        json!({
            "provider": provider, "inventory": inventory, "observed_at": observed_at, "stale": stale,
            "partial": partial, "read_state": read_state, "error_code": code, "cadence_s": cadence,
            "accounts": accounts,
        })
    }
}

/// What the switcher did with a read, as it is handed to [`Seats::acted`].
#[derive(Debug, Clone, Copy)]
pub struct Acted<'a> {
    /// The switcher's decision: `action`, `active`, `target`, `reason`.
    pub decision: &'a Value,
    /// `automatic` or `notify`.
    pub mode: &'a str,
    /// Whether the switch it decided on was made.
    pub switched: bool,
    pub now: i64,
    pub floor_pct: &'a Value,
    pub cadence_s: &'a Value,
    pub next_due_at: &'a Value,
}

/// The switcher's branch as a kind and a reason out of the closed list; `None` for a branch
/// it does not have (`_kind`).
fn branch(decision: &Value, mode: &str, switched: bool) -> Option<(&'static str, &'static str)> {
    match decision["action"].as_str() {
        Some("switch") if mode != "automatic" => Some(("would_switch", "below_floor")),
        Some("switch") if switched => Some(("switched", "below_floor")),
        Some("switch") => Some(("failed", "switch_failed")),
        Some("noop") => Some(("noop", "already_ok")),
        Some("exhausted") => Some(("exhausted", "no_better_account")),
        _ => {
            // Of a skip the switcher writes one of two fixed sentences.
            let reason = if crate::python::truthy(&decision["reason"]) {
                written(&decision["reason"])
            } else {
                String::new()
            };
            if reason == "Hotseat reports no account in use" {
                Some(("skipped", "no_active_account"))
            } else if reason.ends_with(": quota unknown") {
                Some(("skipped", "quota_unknown"))
            } else {
                None
            }
        }
    }
}

/// The kind and the label of a window of Codex from the tool's name for it: `<limit> [tier]
/// <span>`, where the account's own limit carries the provider's name (`_codex_kind`).
pub(crate) fn codex_kind(provider: &str, label: &str) -> Option<(String, String)> {
    let lowered = label.to_lowercase();
    let words: Vec<&str> = lowered
        .split(crate::python::blank)
        .filter(|word| !word.is_empty())
        .collect();
    let (first, last) = (*words.first()?, *words.last()?);
    let span = match last {
        "5-hour" => Some("5h"),
        "weekly" => Some("7d"),
        _ => None,
    };
    if first == provider {
        let span = span?;
        let shown = if span == "5h" { "5-hour" } else { "weekly" };
        return Some((span.to_string(), shown.to_string()));
    }
    let family = family(&json!(first))?;
    let of = match span {
        Some("5h") => "5-hour",
        Some(_) => "weekly",
        None => "window",
    };
    Some((
        format!("model:{family}"),
        format!("{} {of}", capitalized(family)),
    ))
}

/// Why a window's reset will not free the account, out of the tool's closed reasons; none
/// where it says none (`_no_reset`).
fn no_reset(value: &Value) -> Option<&'static str> {
    let said = crate::python::strip(value.as_str()?);
    match said {
        "" => None,
        "no credits" => Some("no_credits"),
        "spend cap" => Some("spend_cap"),
        _ => Some("other"),
    }
}

/// What was last known good is carried to the very same account, never by its alias
/// (`_merge`).
fn merge(previous: Option<&Value>, accounts: Value, _observed_at: i64) -> Vec<Value> {
    let before: Vec<&Value> = previous
        .and_then(|kept| kept["accounts"].as_array())
        .into_iter()
        .flatten()
        .filter(|account| account.is_object())
        .collect();
    let mut merged = Vec::new();
    for mut account in accounts.as_array().cloned().unwrap_or_default() {
        // Of two kept under one identity the later counts, as a table of them would have it.
        let prior = before
            .iter()
            .rev()
            .find(|kept| kept["identity"] == account["identity"]);
        let failed = account
            .as_object_mut()
            .and_then(|fields| fields.remove("failed"));
        if failed == Some(json!(true)) && account.get("no_reset").is_some() {
            // What a failed row says of the reset is no sample either.
            account["no_reset"] = prior.map_or(Value::Null, |prior| prior["no_reset"].clone());
        }
        let mut kept: Vec<&Value> = Vec::new();
        for window in prior
            .and_then(|prior| prior["windows"].as_array())
            .into_iter()
            .flatten()
        {
            let good = window.is_object()
                && !window["used"].is_null()
                && int_ms(&window["observed_at"]).is_some();
            if !good {
                continue;
            }
            match kept.iter_mut().find(|held| held["kind"] == window["kind"]) {
                Some(held) => *held = window,
                None => kept.push(window),
            }
        }
        let own = account["windows"].as_array().cloned().unwrap_or_default();
        let windows: Vec<Value> = if numbers(&own) {
            // Kind by kind: a new number wins, none keeps what was last known good.
            let carried = |window: Value| {
                let held = kept.iter().find(|held| held["kind"] == window["kind"]);
                match held {
                    Some(held) if window["used"].is_null() => (*held).clone(),
                    _ => window,
                }
            };
            own.into_iter().map(carried).collect()
        } else if kept.is_empty() {
            own
        } else {
            // The row had no number at all: every window that was last known good stays.
            kept.into_iter().cloned().collect()
        };
        let good = windows
            .iter()
            .filter(|window| !window["used"].is_null())
            .filter_map(|window| window["observed_at"].as_i64());
        account["last_good_observed_at"] = json!(good.max());
        account["windows"] = json!(windows);
        merged.push(account);
    }
    merged
}

fn stale_after_ms(cadence_s: Option<i64>) -> i64 {
    (2 * cadence_s.filter(|cadence| *cadence > 0).unwrap_or(0)).max(STALE_FLOOR_S) * 1000
}

fn served_window(window: &Value, stale: bool, served_at: i64, no_reset: bool) -> Value {
    let mut used = ratio(&window["used"]);
    let mut stale = stale;
    // A reset that will not free the account is never told: none means not known.
    let resets_at = int_ms(&window["resets_at"]).filter(|_| !no_reset);
    let observed = int_ms(&window["observed_at"]);
    if let Some(reset) = resets_at
        && reset <= served_at
        && observed.is_none_or(|observed| observed < reset)
    {
        // Not known since the reset: no window that is all used.
        (used, stale) = (None, true);
    }
    let remaining = used.and_then(|used| format!("{:.4}", 1.0 - used).parse::<f64>().ok());
    json!({
        "kind": window["kind"], "label": window["label"], "used_ratio": used,
        "remaining_ratio": remaining, "resets_at": resets_at, "observed_at": observed,
        "stale": stale, "source": "hotseat-cli",
    })
}

fn served_account(account: &Value, source_failed: bool, stale_after: i64, served_at: i64) -> Value {
    let states = ["ok", "exhausted", "auth_error", "unknown"];
    let mut state = account["state"]
        .as_str()
        .filter(|state| states.contains(state))
        .unwrap_or("unknown");
    let mut code = account["error_code"]
        .as_str()
        .filter(|code| ERROR_CODES.contains(code));
    // Each window by its own age; a source that failed, or this row's own error, is stale.
    let failed = source_failed || matches!(code, Some("provider_error" | "auth_error"));
    let no_reset = crate::python::truthy(&account["no_reset"]);
    let mut windows = Vec::new();
    for window in account["windows"]
        .as_array()
        .into_iter()
        .flatten()
        .take(MAX_WINDOWS)
    {
        if !window.is_object() {
            continue;
        }
        let observed = int_ms(&window["observed_at"]);
        let stale = failed || observed.is_none_or(|observed| served_at - observed > stale_after);
        windows.push(served_window(window, stale, served_at, no_reset));
    }
    let reset_passed = |window: &Value| {
        let reset = window["resets_at"].as_i64();
        window["used_ratio"].is_null() && reset.is_some_and(|reset| reset <= served_at)
    };
    if state == "exhausted" && !no_reset && windows.iter().any(reset_passed) {
        // A reset passed since it was seen used up, and that reset frees it.
        (state, code) = ("unknown", None);
    }
    if state == "ok"
        && !windows.is_empty()
        && windows.iter().all(|window| window["used_ratio"].is_null())
    {
        state = "unknown";
    }
    json!({
        "alias": served_alias(&account["alias"]).filter(|alias| !alias.is_empty()).unwrap_or_else(|| "account".to_string()),
        "label": plan_label(&account["label"]), "plan": plan(&account["plan"]),
        "active_default": account["active"] == true, "state": state, "error_code": code,
        "last_good_observed_at": int_ms(&account["last_good_observed_at"]), "windows": windows,
    })
}

/// The fields of a kept object, for a test that looks at what is kept.
pub fn kept_fields(seats: &Seats) -> Map<String, Value> {
    serde_json::to_value(seats)
        .ok()
        .and_then(|kept| kept.as_object().cloned())
        .unwrap_or_default()
}
