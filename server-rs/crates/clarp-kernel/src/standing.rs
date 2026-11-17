//! How an account stands by the one word last said of it: kept, and shown to an app.
//!
//! Of a Claude account the Python Host keeps one word: how much of its two windows (five
//! hours, a week) is used and when each begins anew, as the provider last answered. A newer
//! word takes the place of the older, whole. An app's usage screen is shown it for an hour as
//! fresh and as stale after, and an agent's row says that the account is used up while a
//! window of a fresh word is all used and no turn of the tool completed since the word was
//! said. There are no warnings and no stretches: that is all that Host makes of it
//! (`backend_usage._structured_provider` for Claude, and `exhausted_backends`).
//!
//! Held to that code's own answers over made-up stories
//! (`tests/fixtures/claude-usage-vectors.json`, written by
//! `server-rs/tools/claude-usage-vectors.py`). Where the word comes from is another matter:
//! the Python Host asks the provider with the account's login, and this Host has it from
//! Claude Code, which says it as it runs.

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};

use crate::quota::{NOTHING_SEEN, Tool, canonical_id, from_text, moment};

/// For how long a word of the account counts as fresh.
pub const FRESH_MS: i64 = 60 * 60 * 1_000;
/// The windows a word can have, in the order they are shown.
const KINDS: [&str; 2] = ["five_hour", "seven_day"];
/// The name the ids of such windows are made with in the login's place: the Python Host's,
/// from the days it had the word from Claude Code's status line.
const LOGIN: &str = "claude-statusline";

/// What is kept of an account: the last word of its windows.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct Standing {
    /// The windows by their kind, each with `used_percentage`, `resets_at` (empty where it
    /// was not said) and `window_minutes`. None before any word.
    windows: Map<String, Value>,
    /// Whose account the word is of, where that is known.
    account: Option<String>,
    /// Where the word is from, as an app is told.
    source: String,
    seen_at_ms: i64,
    /// When a turn of the tool last completed.
    done_ms: Option<i64>,
}

impl Standing {
    /// A word of the account's windows, of this account where it is known: it takes the
    /// place of the one before. A window counts where it says how much of it is used; a word
    /// with no such window is none, and leaves what was kept. Answers whether it was kept.
    pub fn seen(
        &mut self,
        windows: &Value,
        account: Option<&str>,
        source: &str,
        now_ms: i64,
    ) -> bool {
        let said: Map<String, Value> = KINDS
            .iter()
            .filter_map(|kind| {
                let window = windows.get(kind)?;
                window["used_percentage"].as_f64()?;
                Some(((*kind).to_string(), window.clone()))
            })
            .collect();
        if said.is_empty() {
            return false;
        }
        self.windows = said;
        self.account = account.map(str::to_string);
        self.source = source.to_string();
        self.seen_at_ms = now_ms;
        true
    }

    /// When the word that is kept was said: none before any was.
    pub fn seen_at_ms(&self) -> Option<i64> {
        (!self.windows.is_empty()).then_some(self.seen_at_ms)
    }

    /// A turn of the tool completed, whatever agent's: an account that served a turn was not
    /// used up when it did.
    pub fn completed(&mut self, at_ms: i64) {
        self.done_ms = Some(self.done_ms.map_or(at_ms, |done| done.max(at_ms)));
    }

    /// Whether the word is fresh, where there is one to show to a Host on this account. A
    /// word of another account than the one the Host is on is not shown; one of no known
    /// account is, and so is any word to a Host on no account it can name.
    fn fresh(&self, active: Option<&str>, now_ms: i64) -> Option<bool> {
        let of_another = match (&self.account, active) {
            (Some(account), Some(active)) => account != active,
            _ => false,
        };
        if self.windows.is_empty() || of_another {
            return None;
        }
        Some(now_ms - self.seen_at_ms <= FRESH_MS)
    }

    /// The provider as an app's usage screen reads it (`_structured_provider`).
    pub fn provider(&self, tool: Tool<'_>, active: Option<&str>, now_ms: i64) -> Value {
        let Some(fresh) = self.fresh(active, now_ms) else {
            return json!({
                "provider_id": tool.provider,
                "provider_instance_id": tool.instance,
                "freshness": "unknown",
                "observed_at": null,
                "source": { "kind": "unavailable", "detail": NOTHING_SEEN },
                "windows": [],
            });
        };
        let freshness = if fresh { "fresh" } else { "stale" };
        let source = json!({ "kind": "provider_reported", "detail": self.source });
        let observed_at = moment(self.seen_at_ms);
        let windows: Vec<Value> = KINDS
            .iter()
            .filter_map(|kind| {
                let window = self.windows.get(*kind)?;
                let resets_at = window["resets_at"].as_str().unwrap_or("");
                let reset = if resets_at.is_empty() {
                    "provider_reset_unknown"
                } else {
                    resets_at
                };
                let window_id = canonical_id(
                    "puw-",
                    &json!({
                        "schema_version": 1,
                        "provider_instance_id": tool.instance,
                        "auth_generation_id": LOGIN,
                        "account_scope_ref": null,
                        "scope_kind": "account",
                        "window_kind": kind,
                        "unit": "percent",
                        "window_minutes": window["window_minutes"],
                        "reset_semantics": reset,
                    }),
                    32,
                );
                let said = !window["used_percentage"].is_null();
                Some(json!({
                    "window_id": window_id,
                    "kind": kind,
                    "scope": "account",
                    "unit": "percent",
                    "used_percentage": window["used_percentage"],
                    "resets_at": window["resets_at"],
                    "observed_at": observed_at,
                    "freshness": freshness,
                    "source": source,
                    "limit": {
                        "state": if fresh && said { "normal" } else { "unknown" },
                        "episode_id": null,
                        "provider_limit_event_id": null,
                    },
                }))
            })
            .collect();
        json!({
            "provider_id": tool.provider,
            "provider_instance_id": tool.instance,
            "freshness": freshness,
            "observed_at": observed_at,
            "source": source,
            "windows": windows,
        })
    }

    /// Whether the account the Host is on is used up, as far as the word of it stands, and
    /// what an agent's row says of its tool then (`backend_usage.exhausted_backends`): a
    /// window of a fresh word that is all used, unless a turn of the tool completed after
    /// the word was said.
    pub fn used_up(&self, tool: Tool<'_>, active: Option<&str>, now_ms: i64) -> Option<Value> {
        if !self.fresh(active, now_ms)? {
            return None;
        }
        let since = |done: &i64| *done > self.seen_at_ms && *done <= now_ms;
        if self.done_ms.filter(since).is_some() {
            return None;
        }
        let all_used = |kind: &&&str| {
            let used = self.windows.get(**kind)?["used_percentage"].as_f64()?;
            (used >= 100.0).then_some(())
        };
        let full: Vec<&str> = KINDS
            .iter()
            .filter(|kind| all_used(kind).is_some())
            .copied()
            .collect();
        let first = *full.first()?;
        let resets_at = |kind: &str| self.windows[kind]["resets_at"].as_str().unwrap_or("");
        // The account serves again when the last of its used-up windows begins anew. Where
        // one of them does not say when, that is unknown, and the first of them is named.
        let all_say = full.iter().all(|kind| !resets_at(kind).is_empty());
        let mut last = first;
        for kind in &full {
            let later =
                from_text(resets_at(kind)).unwrap_or(0) > from_text(resets_at(last)).unwrap_or(0);
            if all_say && later {
                last = kind;
            }
        }
        Some(json!({
            "state": "exhausted",
            "provider_id": tool.provider,
            "reason": "usage_limit",
            "window": last,
            "resets_at": all_say.then(|| resets_at(last)),
            "observed_at": moment(self.seen_at_ms),
        }))
    }
}
