//! What is left of a tool's account, as the tool itself said it: kept, and shown to an app.
//!
//! A tool that says how much of its account's windows is used (Codex, with every count of
//! tokens) says it to whoever reads its records, and nobody has to be asked. What was last
//! said is kept for the tool, and an app's usage screen is shown it: each window with how
//! much of it is used, when it begins anew, how old the word of it is, and where the
//! five-hour window stands (a warning at four fifths, a hard limit when it is used up, and
//! what follows when the window begins anew or the account recovers). The events of that are
//! what a phone is told as they come.
//!
//! This is the Python Host's keeping of such records (`backend_usage_store.py`:
//! `_record_codex_five_hour`, `_record_codex_weekly`, `_open_episode`, `_insert_limit_event`,
//! `_reconcile_expired_limits`) and its showing of them (`backend_usage.py`,
//! `_structured_provider`), as a thing that is told and asked and does nothing by itself.
//! Held to that code's own answers over made-up stories
//! (`tests/fixtures/codex-usage-vectors.json`, written by
//! `server-rs/tools/codex-usage-vectors.py`). A record is taken as an observation that says
//! nothing of what it leaves out, which is what a record in a rollout is.
//!
//! Whose account a record is of is known by the login it was seen on: which login it is
//! (another after every renewal of it) and whose account. What stood open under one login is
//! no longer known under the next, since nothing says that it carried over; what an app is
//! shown is the newest word of the account the Host is on, under whichever login it was said.
//!
//! Not everything is kept for ever, as the Python Host keeps it. A window is known by when it
//! begins anew, and Codex does not say the same moment for that throughout: in 213 of 359
//! conversations of the machine this was written on that held more than one record, the
//! first window's reset was said differently along the way, and of the 6,662 steps from one
//! reset to the next that was said, 4,516 were of a minute or less. Each is another window.
//! Here a window is forgotten when there is a newer word of the same kind under the same
//! login and no limit is of it, and a limit that is over is forgotten a week after its
//! window began anew. Nothing that an app is shown changes by it.

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};

/// For how long what was said of a window counts as fresh.
pub const WINDOW_FRESH_MS: i64 = 15 * 60 * 1_000;
/// For how long the tool's row counts as fresh.
pub const ROW_FRESH_MS: i64 = 60 * 60 * 1_000;
/// From how much of the five-hour window on a warning is told, and what it is called.
pub const WARNING_AT: f64 = 80.0;
pub const WARNING_ID: &str = "five_hour_80_percent";
/// What an app is told where nothing was seen of a tool's account.
pub const NOTHING_SEEN: &str = "No structured usage evidence for this provider";
/// The login a limit is kept under where no login could be read.
pub const NO_LOGIN: &str = "auth-unknown";
/// Where a limit is from of which nothing is known but that a turn failed for it.
pub const REFUSED: &str = "classified_terminal";
const REFUSED_DETAIL: &str = "classified quota terminal";
/// For how long after its window began anew a limit that is over is still kept.
pub const KEPT_AFTER_MS: i64 = 7 * 24 * 60 * 60 * 1_000;

/// The names a tool gives the windows of its row.
const ROW_WINDOWS: [&str; 5] = [
    "five_hour",
    "seven_day",
    "primary",
    "secondary",
    "individual_limit",
];

/// An id that follows from what it names (`_canonical_id`): so much of the SHA-256 of the
/// thing written with its fields in the order of their names and nothing between them.
pub fn canonical_id(prefix: &str, named: &Value, size: usize) -> String {
    let digest = Sha256::digest(crate::python::sorted_ascii(named).as_bytes());
    let hex: String = digest.iter().map(|byte| format!("{byte:02x}")).collect();
    format!("{prefix}{}", &hex[..size.min(hex.len())])
}

/// A tool as this Host runs it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Tool<'a> {
    /// The tool on this Host: `<the Host's id>:<the tool>`.
    pub instance: &'a str,
    /// The tool's name.
    pub provider: &'a str,
}

/// A login of a tool.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Login<'a> {
    /// Which login: another after every renewal of it, of the same account or not.
    pub generation: &'a str,
    /// The account, where it is known apart from the login.
    pub account: Option<&'a str>,
}

/// Whose account a record is of: the tool, and the login it ran on.
#[derive(Clone, Copy)]
struct Whose<'a> {
    tool: Tool<'a>,
    login: Login<'a>,
}

/// A window of an account, as it was last said to stand.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
struct Window {
    window_id: String,
    generation: String,
    account: Option<String>,
    /// `five_hour` or `seven_day`; `unknown` for the window of a limit of which nothing is
    /// known but that a turn failed for it.
    kind: String,
    used: Option<f64>,
    resets_at: Option<String>,
    observed_at_ms: i64,
    /// Who says so: the tool itself (`provider_reported`), or a turn that failed.
    #[serde(default = "by_the_tool")]
    source_kind: String,
    source_detail: String,
}

fn by_the_tool() -> String {
    "provider_reported".to_string()
}

/// What is said of a window: its kind and length, how it stands, and who says so.
struct Said<'a> {
    kind: &'a str,
    minutes: Option<i64>,
    used: Option<f64>,
    resets_at: Option<String>,
    source_kind: &'a str,
    detail: &'a str,
}

/// A stretch in which the five-hour window of an account was near its end or at it.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
struct Episode {
    episode_id: String,
    generation: String,
    window_id: String,
    /// `open`, `resolved` (the account recovered) or `invalidated` (another login came).
    status: String,
    /// What was last told of it: `unknown`, `warning`, `hard_limit` or `recovered`.
    current_kind: String,
    threshold_id: Option<String>,
    opened_at_ms: i64,
    /// An authoritative read can omit the window before this episode ages out.
    #[serde(default)]
    omitted_window_end_ms: Option<i64>,
    current_event_id: Option<String>,
}

/// The tool's row: its windows under the names the tool gives them.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
struct Row {
    generation: String,
    account: Option<String>,
    used: Value,
    resets_at: String,
    windows: Map<String, Value>,
    seen_at_ms: i64,
}

/// What is kept of one tool's account.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct Kept {
    windows: Vec<Window>,
    episodes: Vec<Episode>,
    row: Option<Row>,
    authoritative_ms: Option<i64>,
    block: Option<Value>,
    error: String,
    /// When a turn of the tool last completed.
    done_ms: Option<i64>,
}

/// A window as an app is shown it.
struct ShownWindow<'a> {
    window: &'a Window,
    kind: &'static str,
    fresh: bool,
    /// Where its limit stands: `normal`, `unknown`, or what was last told of its stretch.
    state: &'a str,
    /// The stretch its limit is of, for the five hours.
    episode: Option<&'a Episode>,
}

/// What an app's usage screen is shown of a tool, and what happened at the asking.
#[derive(Debug, Clone, PartialEq)]
pub struct Shown {
    /// Limits whose window began anew since they were told: told now as no longer known.
    pub events: Vec<Value>,
    /// The provider, with its windows (`providers.<tool>` of `GET /backend-usage`).
    pub provider: Value,
    /// The tool's row (`backends[]` of the same answer), without what only this Host's own
    /// counting adds to it.
    pub row: Value,
}

impl Kept {
    /// When the newest record that was kept was seen: none before any was.
    pub fn seen_at_ms(&self) -> Option<i64> {
        self.row.as_ref().map(|row| row.seen_at_ms)
    }

    pub fn refresh_due(&self, login: Option<Login<'_>>, now: i64) -> bool {
        let Some(row) = &self.row else { return true };
        let other = login.is_some_and(|on| match &row.account {
            Some(account) => on.account != Some(account.as_str()),
            None => on.generation != row.generation,
        });
        let latest = self
            .shown(login, now)
            .into_iter()
            .filter(|w| w.kind != "unknown")
            .map(|w| w.window.observed_at_ms)
            .max();
        other
            || crate::usage_ask::due(latest, now)
            || crate::usage_ask::due(self.authoritative_ms, now)
            || ((self.block.is_some() || row.used.as_f64().is_some_and(|n| n >= 100.0))
                && self
                    .done_ms
                    .is_some_and(|done| done > self.authoritative_ms.unwrap_or(0) && done <= now))
    }

    pub fn refresh_failed(&mut self, now: i64) {
        self.error = "Codex usage unavailable".into();
        if self.row.as_ref().is_none_or(|r| r.used.is_null()) {
            self.authoritative_ms = Some(now);
            self.block = None;
            self.row = None;
        }
    }

    /// A record of the account's limits was seen on a login: what the tool's reader made of
    /// it (`clarp_backend::limits::codex`), where it was seen (`detail`), and now. `new_id`
    /// makes an id nobody has, after the prefix it is given. Answers the events to tell.
    pub fn seen(
        &mut self,
        tool: Tool<'_>,
        login: Login<'_>,
        snapshot: &Value,
        detail: &str,
        now_ms: i64,
        new_id: &mut dyn FnMut(&str) -> String,
    ) -> Vec<Value> {
        let whose = Whose { tool, login };
        let mut events = self.another_login(tool, login, now_ms, new_id);
        let none = Map::new();
        let seen = snapshot["windows"].as_object().unwrap_or(&none);
        // The tool's row: what was seen before of this login's windows stays beside what is
        // seen now, since a record says nothing of what it leaves out.
        let same = |row: &&Row| {
            row.generation == login.generation && row.account.as_deref() == login.account
        };
        if self.row.as_ref().is_some_and(|row| !same(&row)) {
            self.block = None;
        }
        let whole = matches!(detail, "/wham/usage" | "account/rateLimits/read");
        let mut merged: Map<String, Value> = match self.row.as_ref().filter(same).filter(|_| !whole)
        {
            Some(row) => {
                let named = |(name, window): &(&String, &Value)| {
                    ROW_WINDOWS.contains(&name.as_str()) && window.is_object()
                };
                let kept = row.windows.iter().filter(named);
                kept.map(|(name, window)| (name.clone(), window.clone()))
                    .collect()
            }
            None => Map::new(),
        };
        for (name, window) in seen {
            merged.insert(name.clone(), window.clone());
        }
        let first = ["primary", "secondary", "individual_limit"].into_iter();
        let mut there = first.filter_map(|name| merged.get(name));
        let preferred = there.find(|window| crate::python::truthy(window));
        let said = |field: &str| preferred.and_then(|window| window.get(field)).cloned();
        let resets_at = said("resets_at").and_then(|at| at.as_str().map(str::to_string));
        // The windows of the lengths that have a name, as this record has them.
        let of_length = |minutes: f64| {
            let long = |window: &&Value| window["window_minutes"].as_f64() == Some(minutes);
            seen.values().find(long).cloned()
        };
        let (five_hours, week) = (of_length(300.0), of_length(10_080.0));
        if whole {
            self.authoritative_ms = Some(now_ms);
            self.block = snapshot.get("blocked").cloned();
            let omitted = |w: &Window| {
                w.generation == login.generation
                    && match w.kind.as_str() {
                        "five_hour" => five_hours.is_none(),
                        "seven_day" => week.is_none(),
                        _ => false,
                    }
            };
            for at in 0..self.episodes.len() {
                let ep = &self.episodes[at];
                if ep.status == "open"
                    && self
                        .windows
                        .iter()
                        .any(|w| omitted(w) && w.window_id == ep.window_id)
                {
                    let tell = Told {
                        kind: "unknown",
                        threshold_id: None,
                        used: None,
                        resets_at: None,
                        freshness: "unknown",
                        source_kind: "authoritative_window_omitted",
                    };
                    events.extend(self.tell(tool, at, &tell, now_ms, new_id));
                    self.episodes[at].status = "invalidated".into();
                }
            }
            for episode in &mut self.episodes {
                if let Some(window) = self.windows.iter().find(|w| {
                    omitted(w)
                        && w.generation == episode.generation
                        && w.window_id == episode.window_id
                }) {
                    episode.omitted_window_end_ms = Some(
                        window
                            .resets_at
                            .as_deref()
                            .and_then(from_text)
                            .unwrap_or(window.observed_at_ms),
                    );
                }
            }
            self.windows.retain(|w| !omitted(w));
            if self.block.is_none()
                && !seen.is_empty()
                && seen
                    .values()
                    .all(|w| w["used_percentage"].as_f64().unwrap_or(0.0) < 100.0)
            {
                events.extend(self.clear_unknown(tool, Some(login), now_ms, new_id));
            }
        }
        self.error.clear();
        self.row = Some(Row {
            generation: login.generation.to_string(),
            account: login.account.map(str::to_string),
            used: said("used_percentage").unwrap_or(Value::Null),
            resets_at: resets_at.unwrap_or_default(),
            windows: merged,
            seen_at_ms: now_ms,
        });
        if let Some(window) = five_hours {
            let used = window["used_percentage"].as_f64();
            let resets_at = text(&window["resets_at"]);
            let said = Said {
                kind: "five_hour",
                minutes: Some(300),
                used,
                resets_at: resets_at.clone(),
                source_kind: "provider_reported",
                detail,
            };
            let id = self.keep(whose, said, now_ms);
            events.extend(self.five_hours(whose, &id, used, resets_at.as_deref(), now_ms, new_id));
        }
        if let Some(window) = week {
            let said = Said {
                kind: "seven_day",
                minutes: Some(10_080),
                used: window["used_percentage"].as_f64(),
                resets_at: text(&window["resets_at"]),
                source_kind: "provider_reported",
                detail,
            };
            self.keep(whose, said, now_ms);
        }
        self.forget(now_ms);
        events
    }

    /// Forgets what can no longer be shown: a limit that is over and whose window began
    /// anew more than a week ago, and a window that no limit is of and of which there is a
    /// newer word of the same kind under the same login.
    fn forget(&mut self, now_ms: i64) {
        let windows = &self.windows;
        let long_over = |episode: &Episode| {
            let of_it = |window: &&Window| {
                window.window_id == episode.window_id && window.generation == episode.generation
            };
            let ended = windows.iter().find(of_it).map(|window| {
                let reset = window.resets_at.as_deref().and_then(from_text);
                reset.unwrap_or(window.observed_at_ms)
            });
            ended
                .or(episode.omitted_window_end_ms)
                .is_none_or(|ended_ms| now_ms - ended_ms > KEPT_AFTER_MS)
        };
        self.episodes
            .retain(|episode| episode.status == "open" || !long_over(episode));
        let episodes = &self.episodes;
        let stays = |window: &Window| {
            let of_a_limit = episodes.iter().any(|episode| {
                episode.window_id == window.window_id && episode.generation == window.generation
            });
            let newer = |other: &Window| {
                other.generation == window.generation
                    && other.account == window.account
                    && other.kind == window.kind
                    && other.observed_at_ms > window.observed_at_ms
            };
            of_a_limit || !self.windows.iter().any(newer)
        };
        let mut staying: Vec<bool> = self.windows.iter().map(stays).collect();
        staying.reverse();
        self.windows.retain(|_| staying.pop().unwrap_or(true));
    }

    /// Another login than the one limits stand open under: what stood open under the one
    /// before is no longer known (`_invalidate_rotated_auth`).
    fn another_login(
        &mut self,
        tool: Tool<'_>,
        login: Login<'_>,
        now_ms: i64,
        new_id: &mut dyn FnMut(&str) -> String,
    ) -> Vec<Value> {
        let mut events = Vec::new();
        for at in 0..self.episodes.len() {
            let episode = &self.episodes[at];
            if episode.generation == login.generation || episode.status != "open" {
                continue;
            }
            let told = Told {
                kind: "unknown",
                threshold_id: None,
                used: None,
                resets_at: None,
                freshness: "unknown",
                source_kind: "auth_generation_rotated",
            };
            events.extend(self.tell(tool, at, &told, now_ms, new_id));
            self.episodes[at].status = "invalidated".to_string();
        }
        events
    }

    /// A turn of the tool failed for a usage limit, on this login (or on none that could be
    /// read): a hard limit of which nothing more is known, neither which window nor until
    /// when (`record_classified_usage_limit`). Told once for as long as it stands; a turn
    /// that fails the same way meanwhile says only that it still does. Answers the events to
    /// tell.
    pub fn refused(
        &mut self,
        tool: Tool<'_>,
        login: Option<Login<'_>>,
        now_ms: i64,
        new_id: &mut dyn FnMut(&str) -> String,
    ) -> Vec<Value> {
        let mut events = match login {
            Some(login) => self.another_login(tool, login, now_ms, new_id),
            None => Vec::new(),
        };
        let login = login.unwrap_or(Login {
            generation: NO_LOGIN,
            account: None,
        });
        let said = Said {
            kind: "unknown",
            minutes: None,
            used: None,
            resets_at: None,
            source_kind: REFUSED,
            detail: REFUSED_DETAIL,
        };
        let window_id = self.keep(Whose { tool, login }, said, now_ms);
        // The stretch of this window that stands open, the newest where there are several;
        // a new one where there is none.
        let stands = |episode: &Episode| {
            episode.generation == login.generation
                && episode.status == "open"
                && episode.window_id == window_id
        };
        let open = (0..self.episodes.len())
            .filter(|at| stands(&self.episodes[*at]))
            .max_by_key(|at| (self.episodes[*at].opened_at_ms, std::cmp::Reverse(*at)));
        let at = match open {
            Some(at) => at,
            None => {
                self.episodes.push(Episode {
                    episode_id: new_id("plp-"),
                    generation: login.generation.to_string(),
                    window_id,
                    status: "open".to_string(),
                    current_kind: "unknown".to_string(),
                    threshold_id: None,
                    opened_at_ms: now_ms,
                    omitted_window_end_ms: None,
                    current_event_id: None,
                });
                self.episodes.len() - 1
            }
        };
        let told = Told {
            kind: "hard_limit",
            threshold_id: None,
            used: None,
            resets_at: None,
            freshness: "fresh",
            source_kind: REFUSED,
        };
        events.extend(self.tell(tool, at, &told, now_ms, new_id));
        events
    }

    /// Keeps how a window stands; answers its id. A window is known by whose it is, what
    /// kind and how long it is, and when it begins anew: one that began anew is another.
    fn keep(&mut self, whose: Whose<'_>, said: Said<'_>, now_ms: i64) -> String {
        let Whose { tool, login } = whose;
        let Said {
            kind,
            minutes,
            used,
            resets_at,
            source_kind,
            detail,
        } = said;
        let window_id = canonical_id(
            "puw-",
            &json!({
                "schema_version": 1,
                "provider_instance_id": tool.instance,
                "auth_generation_id": login.generation,
                "account_scope_ref": login.account,
                "scope_kind": "account",
                "window_kind": kind,
                "unit": "percent",
                "window_minutes": minutes,
                "reset_semantics": resets_at.as_deref().unwrap_or("provider_reset_unknown"),
            }),
            32,
        );
        let window = Window {
            window_id: window_id.clone(),
            generation: login.generation.to_string(),
            account: login.account.map(str::to_string),
            kind: kind.to_string(),
            used,
            resets_at,
            observed_at_ms: now_ms,
            source_kind: source_kind.to_string(),
            source_detail: detail.to_string(),
        };
        let known = |kept: &&mut Window| {
            kept.window_id == window_id && kept.generation == window.generation
        };
        match self.windows.iter_mut().find(known) {
            Some(kept) => *kept = window,
            None => self.windows.push(window),
        }
        window_id
    }

    /// Whether an episode stands open under this login and is of a five-hour window of its
    /// account.
    fn of_the_account(&self, episode: &Episode, login: Login<'_>) -> bool {
        let its_window = self.windows.iter().find(|window| {
            window.window_id == episode.window_id && window.generation == episode.generation
        });
        episode.generation == login.generation
            && episode.status == "open"
            && its_window.is_some_and(|window| {
                window.kind == "five_hour" && window.account.as_deref() == login.account
            })
    }

    /// What follows from how the five-hour window stands now.
    fn five_hours(
        &mut self,
        whose: Whose<'_>,
        window_id: &str,
        used: Option<f64>,
        resets_at: Option<&str>,
        now_ms: i64,
        new_id: &mut dyn FnMut(&str) -> String,
    ) -> Vec<Value> {
        let Some(used) = used else {
            return Vec::new();
        };
        let Whose { tool, login } = whose;
        let mut events = Vec::new();
        if used >= WARNING_AT {
            // The account's open episode, the newest where there are several; a new one
            // where there is none. It is of the window as it is now.
            let open = (0..self.episodes.len())
                .filter(|at| self.of_the_account(&self.episodes[*at], login))
                .max_by_key(|at| (self.episodes[*at].opened_at_ms, std::cmp::Reverse(*at)));
            let at = match open {
                Some(at) => at,
                None => {
                    self.episodes.push(Episode {
                        episode_id: new_id("plp-"),
                        generation: login.generation.to_string(),
                        window_id: window_id.to_string(),
                        status: "open".to_string(),
                        current_kind: "unknown".to_string(),
                        threshold_id: None,
                        opened_at_ms: now_ms,
                        omitted_window_end_ms: None,
                        current_event_id: None,
                    });
                    self.episodes.len() - 1
                }
            };
            self.episodes[at].window_id = window_id.to_string();
            let told = if used >= 100.0 {
                Told::of("hard_limit", None, used, resets_at)
            } else {
                Told::of("warning", Some(WARNING_ID), used, resets_at)
            };
            events.extend(self.tell(tool, at, &told, now_ms, new_id));
        } else {
            // Under the warning again: what stood open of the account is over.
            for at in 0..self.episodes.len() {
                if !self.of_the_account(&self.episodes[at], login) {
                    continue;
                }
                self.episodes[at].window_id = window_id.to_string();
                let told = Told::of("recovered", None, used, resets_at);
                events.extend(self.tell(tool, at, &told, now_ms, new_id));
            }
        }
        events
    }

    /// Tells something of an episode, unless that is what was told of it last
    /// (`_insert_limit_event`).
    fn tell(
        &mut self,
        tool: Tool<'_>,
        at: usize,
        told: &Told<'_>,
        now_ms: i64,
        new_id: &mut dyn FnMut(&str) -> String,
    ) -> Option<Value> {
        let episode = &mut self.episodes[at];
        if episode.current_kind == told.kind && episode.threshold_id.as_deref() == told.threshold_id
        {
            return None;
        }
        let dedupe_key = canonical_id(
            "pld-",
            &json!({
                "schema_version": 1,
                "episode_id": episode.episode_id,
                "provider_instance_id": tool.instance,
                "auth_generation_id": episode.generation,
                "window_id": episode.window_id,
                "kind": told.kind,
                "threshold_id": told.threshold_id,
                "predecessor_event_id": episode.current_event_id,
            }),
            48,
        );
        let event_id = new_id("ple-");
        episode.current_kind = told.kind.to_string();
        episode.threshold_id = told.threshold_id.map(str::to_string);
        episode.current_event_id = Some(event_id.clone());
        if told.kind == "recovered" {
            episode.status = "resolved".to_string();
        }
        // In the order the Python Host writes an event of this kind in.
        Some(json!({
            "schema_version": 1,
            "type": "provider-limit",
            "provider_limit_event_id": event_id,
            "episode_id": episode.episode_id,
            "provider_instance_id": tool.instance,
            "provider_id": tool.provider,
            "window_id": episode.window_id,
            "kind": told.kind,
            "threshold_id": told.threshold_id,
            "used_percentage": told.used,
            "resets_at": told.resets_at,
            "observed_at": moment(now_ms),
            "freshness": told.freshness,
            "source": { "kind": told.source_kind },
            "dedupe_key": dedupe_key,
        }))
    }

    /// An app asks, while the Host is on a login of the tool or on none. Limits whose
    /// window has begun anew since they were told are told as no longer known first
    /// (`_reconcile_expired_limits`); then what is shown.
    pub fn asked(
        &mut self,
        tool: Tool<'_>,
        login: Option<Login<'_>>,
        now_ms: i64,
        new_id: &mut dyn FnMut(&str) -> String,
    ) -> Shown {
        let mut events = Vec::new();
        for at in 0..self.episodes.len() {
            let episode = &self.episodes[at];
            let limited = matches!(episode.current_kind.as_str(), "warning" | "hard_limit");
            if episode.status != "open" || !limited {
                continue;
            }
            let its_window = self.windows.iter().find(|window| {
                window.window_id == episode.window_id && window.generation == episode.generation
            });
            let Some(resets_at) = its_window.and_then(|window| window.resets_at.clone()) else {
                continue;
            };
            if !from_text(&resets_at).is_some_and(|reset_ms| now_ms >= reset_ms) {
                continue;
            }
            let told = Told {
                kind: "unknown",
                threshold_id: None,
                used: None,
                resets_at: Some(&resets_at),
                freshness: "stale",
                source_kind: "reset_passed",
            };
            events.extend(self.tell(tool, at, &told, now_ms, new_id));
        }
        Shown {
            events,
            provider: self.provider(tool, login, now_ms),
            row: self.row(tool.provider, now_ms),
        }
    }

    /// The windows an app is shown, of the account the Host is on: the newest word of the
    /// five hours and of the week, each with whether it is fresh and where its limit stands.
    fn shown(&self, login: Option<Login<'_>>, now_ms: i64) -> Vec<ShownWindow<'_>> {
        // Whose windows: the account's that the Host is on, as far as an account is known
        // (a window of no known account is its login's); any, where the Host is on no login.
        let of_the_login = |window: &&Window| match (login, &window.account) {
            (None, _) => true,
            (Some(login), Some(account)) => login.account == Some(account.as_str()),
            (Some(login), None) => window.generation == login.generation,
        };
        // The newest word of them (of two as new, the one kept last) says under which login
        // they are shown: what an earlier login of the account said is not shown beside it.
        let mut said_last: Option<&Window> = None;
        for window in self.windows.iter().filter(of_the_login) {
            if said_last.is_none_or(|kept| window.observed_at_ms >= kept.observed_at_ms) {
                said_last = Some(window);
            }
        }
        let Some(generation) = said_last.map(|window| window.generation.as_str()) else {
            return Vec::new();
        };
        let mut shown = Vec::new();
        for kind in ["five_hour", "seven_day", "unknown"] {
            let of_kind = |window: &&Window| window.generation == generation && window.kind == kind;
            // The newest word of a window of this kind; of two as new, the one kept first.
            let mut latest: Option<&Window> = None;
            for window in self.windows.iter().filter(of_kind) {
                if latest.is_none_or(|kept| window.observed_at_ms > kept.observed_at_ms) {
                    latest = Some(window);
                }
            }
            let Some(window) = latest else {
                continue;
            };
            let age = (now_ms - window.observed_at_ms).max(0);
            let reset = window.resets_at.as_deref().and_then(from_text);
            let fresh = age <= WINDOW_FRESH_MS && !reset.is_some_and(|reset_ms| now_ms >= reset_ms);
            let of_window = |episode: &&Episode| {
                episode.window_id == window.window_id && episode.generation == window.generation
            };
            // The limit of the five hours: the stretch of this window that began last.
            let mut episode: Option<&Episode> = None;
            for candidate in self.episodes.iter().filter(of_window) {
                if episode.is_none_or(|kept| candidate.opened_at_ms > kept.opened_at_ms) {
                    episode = Some(candidate);
                }
            }
            let episode = episode.filter(|_| kind != "seven_day");
            let state = match episode.map(|episode| episode.current_kind.as_str()) {
                None if fresh && window.used.is_some() => "normal",
                None => "unknown",
                // A limit nobody has heard of lately is not known to stand.
                Some("warning" | "hard_limit") if !fresh => "unknown",
                Some(told) => told,
            };
            shown.push(ShownWindow {
                window,
                kind,
                fresh,
                state,
                episode,
            });
        }
        shown
    }

    /// The provider as an app's usage screen reads it (`_structured_provider`).
    fn provider(&self, tool: Tool<'_>, login: Option<Login<'_>>, now_ms: i64) -> Value {
        let shown = self.shown(login, now_ms);
        // The newest word among them; of two as new, the five hours'.
        let mut newest: Option<&Window> = None;
        for one in &shown {
            if newest.is_none_or(|kept| one.window.observed_at_ms > kept.observed_at_ms) {
                newest = Some(one.window);
            }
        }
        let Some(newest) = newest else {
            return json!({
                "provider_id": tool.provider,
                "provider_instance_id": tool.instance,
                "freshness": "unknown",
                "observed_at": null,
                "source": { "kind": "unavailable", "detail": NOTHING_SEEN },
                "windows": [],
            });
        };
        let all_fresh = shown.iter().all(|one| one.fresh);
        let windows: Vec<Value> = shown
            .iter()
            .map(|one| {
                let window = one.window;
                json!({
                    "window_id": window.window_id,
                    "kind": one.kind,
                    "scope": "account",
                    "unit": "percent",
                    "used_percentage": window.used,
                    "resets_at": window.resets_at,
                    "observed_at": moment(window.observed_at_ms),
                    "freshness": if one.fresh { "fresh" } else { "stale" },
                    "source": { "kind": window.source_kind, "detail": window.source_detail },
                    "limit": {
                        "state": one.state,
                        "episode_id": one.episode.map(|episode| &episode.episode_id),
                        "provider_limit_event_id":
                            one.episode.and_then(|episode| episode.current_event_id.as_ref()),
                    },
                })
            })
            .collect();
        json!({
            "provider_id": tool.provider,
            "provider_instance_id": tool.instance,
            "freshness": if all_fresh { "fresh" } else { "stale" },
            "observed_at": moment(newest.observed_at_ms),
            "source": { "kind": newest.source_kind, "detail": newest.source_detail },
            "windows": windows,
        })
    }

    /// A turn of the tool completed on this login (or on none that could be read), whatever
    /// agent's: an account that served a turn was not used up when it did. A limit of which
    /// nothing was known but that a turn failed for it is over with that: told as recovered,
    /// and its window is no more. (The Python Host ends such a limit when it asks the
    /// provider and is told that all is well, `_resolve_unknown_terminals`: the same ending,
    /// for a Host that asks nobody.) Answers the events to tell.
    pub fn completed(
        &mut self,
        tool: Tool<'_>,
        login: Option<Login<'_>>,
        at_ms: i64,
        new_id: &mut dyn FnMut(&str) -> String,
    ) -> Vec<Value> {
        self.done_ms = Some(self.done_ms.map_or(at_ms, |done| done.max(at_ms)));
        self.clear_unknown(tool, login, at_ms, new_id)
    }

    fn clear_unknown(
        &mut self,
        tool: Tool<'_>,
        login: Option<Login<'_>>,
        at_ms: i64,
        new_id: &mut dyn FnMut(&str) -> String,
    ) -> Vec<Value> {
        // Whose such limits: this login's, those kept under no login, and those of this
        // login's account under whichever login.
        let generation = login.map_or(NO_LOGIN, |login| login.generation);
        let account = login.and_then(|login| login.account);
        let over = |window: &Window| {
            let of_the_login = window.generation == generation || window.generation == NO_LOGIN;
            let of_the_account = account.is_some() && window.account.as_deref() == account;
            window.kind == "unknown" && (of_the_login || of_the_account)
        };
        if !self.windows.iter().any(over) {
            return Vec::new();
        }
        let mut events = Vec::new();
        for at in 0..self.episodes.len() {
            let episode = &self.episodes[at];
            let its_window = self.windows.iter().find(|window| {
                window.window_id == episode.window_id && window.generation == episode.generation
            });
            if episode.status != "open" || !its_window.is_some_and(over) {
                continue;
            }
            let told = Told {
                kind: "recovered",
                threshold_id: None,
                used: None,
                resets_at: None,
                freshness: "fresh",
                source_kind: "provider_reported",
            };
            events.extend(self.tell(tool, at, &told, at_ms, new_id));
        }
        self.windows.retain(|window| !over(window));
        self.forget(at_ms);
        events
    }

    /// Whether the account the Host is on is used up, as far as what was said of it stands,
    /// and what an agent's row says of its tool then (`backend_usage.exhausted_backends`): a
    /// window that is fresh and all used, or at its hard limit, unless a turn of the tool
    /// completed since that was said. What is no longer fresh says nothing: an account
    /// that was filled up or changed for another does not stay marked.
    pub fn used_up(&self, tool: Tool<'_>, login: Option<Login<'_>>, now_ms: i64) -> Option<Value> {
        let all_used = |one: &ShownWindow<'_>| {
            let full = one.window.used.is_some_and(|used| used >= 100.0);
            one.fresh && (one.state == "hard_limit" || full)
        };
        let mut full: Vec<ShownWindow<'_>> = self.shown(login, now_ms);
        full.retain(all_used);
        let block = self.block.as_ref().filter(|b| {
            let at = self.authoritative_ms.unwrap_or(0);
            now_ms - at <= WINDOW_FRESH_MS
                && !self.done_ms.is_some_and(|d| d > at && d <= now_ms)
                && !b["resets_at"]
                    .as_str()
                    .and_then(from_text)
                    .is_some_and(|end| now_ms >= end)
                && login.is_none_or(|l| {
                    self.row
                        .as_ref()
                        .is_some_and(|r| r.account.as_deref() == l.account)
                })
        });
        let first_said = full
            .iter()
            .map(|one| one.window.observed_at_ms)
            .min()
            .unwrap_or(now_ms);
        // A turn that completed after the first of it was said: what still counts is what
        // was said after that turn.
        let since = |done: &i64| *done > first_said && *done <= now_ms;
        if let Some(done) = self.done_ms.filter(since) {
            full.retain(|one| one.window.observed_at_ms >= done);
        }
        // The account serves again when the last of its used-up windows begins anew; one
        // that does not say when makes that unknown.
        let unsaid = full.iter().find(|one| one.window.resets_at.is_none());
        let reset_ms = |one: &ShownWindow<'_>| {
            let said = one.window.resets_at.as_deref();
            said.and_then(from_text).unwrap_or(0)
        };
        let mut last: Option<&ShownWindow<'_>> = None;
        for one in &full {
            if last.is_none_or(|kept| reset_ms(one) > reset_ms(kept)) {
                last = Some(one);
            }
        }
        let Some(named) = unsaid.or(last) else {
            return block.map(|b| json!({"state":"exhausted","provider_id":tool.provider,"reason":b["reason"],"window":"unknown","resets_at":b["resets_at"],"observed_at":null}));
        };
        Some(json!({
            "state": "exhausted",
            "provider_id": tool.provider,
            "reason": block.map_or(json!("usage_limit"),|b| b["reason"].clone()),
            "window": named.kind,
            "resets_at": named.window.resets_at.clone().or_else(|| block.and_then(|b| text(&b["resets_at"]))),
            "observed_at": moment(named.window.observed_at_ms),
        }))
    }

    /// The tool's row (`_response_row`). It says when the record was seen and is fresh for
    /// an hour from then. (The Python Host's row keeps the time of the last whole reading of
    /// the account, and a Host that is given only records that are none says 1970 and stale.)
    fn row(&self, tool: &str, now_ms: i64) -> Value {
        let Some(row) = &self.row else {
            if let Some(at) = self.authoritative_ms {
                return json!({"backend":tool,"used_percentage":null,"resets_at":null,"source":"codex-usage-endpoint","fetched_at":moment(at),"freshness":"unknown","error":self.error,"windows":{}});
            }
            return json!({
                "backend": tool,
                "used_percentage": null,
                "resets_at": null,
                "source": "none",
                "fetched_at": null,
                "freshness": "unknown",
                "error": "no data captured",
                "windows": {},
            });
        };
        let fetched = self.authoritative_ms.unwrap_or(row.seen_at_ms);
        let freshness = if row.used.is_null() {
            "unknown"
        } else if now_ms - fetched <= ROW_FRESH_MS {
            "fresh"
        } else {
            "stale"
        };
        let named = |(name, window): &(&String, &Value)| {
            ROW_WINDOWS.contains(&name.as_str()) && window.is_object()
        };
        let windows: Map<String, Value> = row
            .windows
            .iter()
            .filter(named)
            .map(|(name, window)| (name.clone(), window.clone()))
            .collect();
        json!({
            "backend": tool,
            "used_percentage": row.used,
            "resets_at": if row.resets_at.is_empty() { Value::Null } else { json!(row.resets_at) },
            "source": if self.authoritative_ms.is_some() {"codex-usage-endpoint"} else {SOURCE},
            "fetched_at": moment(fetched),
            "freshness": freshness,
            "error": self.error,
            "windows": windows,
        })
    }
}

/// Where the tool's row says its figures are from: the tool's own record of its turns.
pub const SOURCE: &str = "codex-rollout";

/// What is told of an episode.
struct Told<'a> {
    kind: &'a str,
    threshold_id: Option<&'a str>,
    used: Option<f64>,
    resets_at: Option<&'a str>,
    freshness: &'a str,
    source_kind: &'a str,
}

impl<'a> Told<'a> {
    /// Something the tool itself said just now.
    fn of(
        kind: &'a str,
        threshold_id: Option<&'a str>,
        used: f64,
        resets_at: Option<&'a str>,
    ) -> Self {
        Self {
            kind,
            threshold_id,
            used: Some(used),
            resets_at,
            freshness: "fresh",
            source_kind: "provider_reported",
        }
    }
}

/// A text that is there and not empty.
fn text(value: &Value) -> Option<String> {
    value
        .as_str()
        .filter(|said| !said.is_empty())
        .map(str::to_string)
}

const DAY_S: i64 = 86_400;

/// A moment in milliseconds as text: to the second, in UTC.
pub fn moment(ms: i64) -> String {
    let second = ms.div_euclid(1_000);
    let (days, rest) = (second.div_euclid(DAY_S), second.rem_euclid(DAY_S));
    let shifted = days + 719_468;
    let (era, day_of_era) = (shifted.div_euclid(146_097), shifted.rem_euclid(146_097));
    let year_of_era =
        (day_of_era - day_of_era / 1_460 + day_of_era / 36_524 - day_of_era / 146_096) / 365;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let shifted_month = (5 * day_of_year + 2) / 153;
    let day = day_of_year - (153 * shifted_month + 2) / 5 + 1;
    let month = if shifted_month < 10 {
        shifted_month + 3
    } else {
        shifted_month - 9
    };
    let year = year_of_era + era * 400 + i64::from(month <= 2);
    let (hour, minute, of_minute) = (rest / 3_600, rest % 3_600 / 60, rest % 60);
    format!("{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{of_minute:02}Z")
}

/// A moment written as a date and a time of day (`2030-03-17T21:46:40Z`, with or without
/// parts of a second, in UTC or with how far from it), in milliseconds. `None` for a text
/// that is no such moment: when a window begins anew is then not known.
pub fn from_text(written: &str) -> Option<i64> {
    let written = written.trim();
    let (date, rest) = written.split_once(['T', ' '])?;
    let mut parts = date.splitn(3, '-').map(str::parse::<i64>);
    let (year, month, day) = (
        parts.next()?.ok()?,
        parts.next()?.ok()?,
        parts.next()?.ok()?,
    );
    // The time of day, and how far the clock it is read off is from UTC.
    let (clock, ahead_s) = match rest.strip_suffix('Z') {
        Some(clock) => (clock, 0),
        None => match rest.rfind(['+', '-']) {
            Some(at) => {
                let (hours, minutes) = rest[at + 1..].split_once(':')?;
                let far = hours.parse::<i64>().ok()? * 3_600 + minutes.parse::<i64>().ok()? * 60;
                (
                    &rest[..at],
                    if rest[at..].starts_with('-') {
                        -far
                    } else {
                        far
                    },
                )
            }
            None => (rest, 0),
        },
    };
    let (whole, part) = clock.split_once('.').unwrap_or((clock, ""));
    let mut of_day = whole.splitn(3, ':').map(str::parse::<i64>);
    let (hour, minute) = (of_day.next()?.ok()?, of_day.next()?.ok()?);
    let second = of_day.next().and_then(Result::ok).unwrap_or(0);
    if !(1..=12).contains(&month)
        || !(1..=31).contains(&day)
        || hour > 23
        || minute > 59
        || second > 60
    {
        return None;
    }
    let ms = part.chars().take(3).collect::<String>();
    let ms = format!("{ms:0<3}").parse::<i64>().ok()?;
    // Days since 1970 of that date (the rule of dates into days).
    let shifted_year = if month <= 2 { year - 1 } else { year };
    let era = shifted_year.div_euclid(400);
    let year_of_era = shifted_year.rem_euclid(400);
    let shifted_month = if month > 2 { month - 3 } else { month + 9 };
    let day_of_year = (153 * shifted_month + 2) / 5 + day - 1;
    let day_of_era = year_of_era * 365 + year_of_era / 4 - year_of_era / 100 + day_of_year;
    let days = era * 146_097 + day_of_era - 719_468;
    Some((days * DAY_S + hour * 3_600 + minute * 60 + second - ahead_s) * 1_000 + ms)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_moment_is_written_and_read_back() {
        for (ms, written) in [
            (0, "1970-01-01T00:00:00Z"),
            (1_900_000_000_000, "2030-03-17T17:46:40Z"),
            (951_782_400_000, "2000-02-29T00:00:00Z"),
            (253_402_300_799_000, "9999-12-31T23:59:59Z"),
        ] {
            assert_eq!(moment(ms), written);
            assert_eq!(from_text(written), Some(ms), "{written}");
        }
        // Parts of a second are dropped when a moment is written.
        assert_eq!(moment(1_900_000_000_999), "2030-03-17T17:46:40Z");
        // Read as Python's `fromisoformat` reads it: with how far from UTC, with parts of a
        // second, with a space, and without a word of where (UTC).
        let at = Some(1_900_000_000_000);
        assert_eq!(from_text("2030-03-17T18:46:40+01:00"), at);
        assert_eq!(from_text("2030-03-17T12:46:40-05:00"), at);
        assert_eq!(from_text("2030-03-17 17:46:40"), at);
        assert_eq!(
            from_text("2030-03-17T17:46:40.250Z"),
            Some(1_900_000_000_250)
        );
        for odd in [
            "",
            "soon",
            "1900000000",
            "2030-03-17",
            "2030-13-17T00:00:00Z",
            "nan",
        ] {
            assert_eq!(from_text(odd), None, "{odd:?}");
        }
    }

    #[test]
    fn an_id_follows_from_what_it_names() {
        let named = json!({ "b": 1, "a": "x" });
        let id = canonical_id("puw-", &named, 32);
        assert_eq!(id.len(), 36);
        assert_eq!(id, canonical_id("puw-", &json!({ "a": "x", "b": 1 }), 32));
        assert_ne!(id, canonical_id("puw-", &json!({ "a": "y", "b": 1 }), 32));
    }
}
