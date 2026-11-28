//! A Host that does not answer, per Host (the shared "Host not answering"
//! spec, Peter's go of 28 Nov 2026). The primary engine watches every Host it
//! reads, itself and each peer: once one is out of reach it probes that Host
//! alone (`GET /server-info`, 8 s at most, off the UI thread) on a 2, 5, 10,
//! 30 s, then once a minute backoff, names the most likely reason
//! (`clarp_core::host_down::classify`), and when the probe answers has that
//! Host's own engine connect again. Back, it says `Back online` for two
//! seconds and goes. Nothing about one Host goes through another: each probe
//! is its own request to its own address with its own token.
//!
//! "Since" survives a restart: the last time each Host answered is kept in
//! the settings (`hostDown/lastOk/<base URL>`).

use std::time::{Duration, Instant};

use clarp_core::host_down::{self, Action, AddressKind, Attempt, Context, DeviceNetwork, Reason};
use clarp_net::probe::Probe;

use crate::{Change, Engine, Message};

/// The setting holding when a Host last answered (ms since the epoch).
const LAST_OK: &str = "hostDown/lastOk/";

/// While a Host answers, when it did is saved at most this often.
const LAST_OK_SAVE: Duration = Duration::from_secs(300);

struct Down {
    since_ms: i64,
    attempts: u32,
    backoff_step: u32,
    last: Option<Attempt>,
    reason: Reason,
    next_at: Option<Instant>,
    in_flight: bool,
    /// The last probe answered: its engine is connecting again.
    answered: bool,
}

struct Watch {
    url: String,
    /// It answered in this run of the app.
    seen_live: bool,
    last_ok_ms: Option<i64>,
    saved_at: Option<Instant>,
    down: Option<Down>,
    back_until: Option<Instant>,
    /// Bumped by every change of state: older probes and timers do nothing.
    generation: u64,
    host_version: String,
    contract: Option<u32>,
}

#[derive(Default)]
pub(crate) struct HostDown {
    pub(crate) started: bool,
    watches: Vec<Watch>,
    network: DeviceNetwork,
    fingerprint: Option<String>,
}

/// The panel's quiet line (`host_down.status.*`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HostDownStatus {
    /// A probe is out, or the Host answered and is connecting again.
    Reconnecting,
    /// The next probe is this far off.
    RetryIn(Duration),
    BackOnline,
}

/// What a Host-down panel shows: codes, times and the Host's label; the
/// words are the app's (`host_down.*`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HostDownView {
    pub url: String,
    pub label: String,
    pub reason: Reason,
    /// First failure after the last answer (ms since the epoch).
    pub since_ms: i64,
    /// When it last answered, if ever known (kept across restarts).
    pub last_ok_ms: Option<i64>,
    pub status: HostDownStatus,
    pub back_online: bool,
    pub action: Option<Action>,
    /// Another Host is configured: Switch Host is offered.
    pub switch: bool,
    /// How the last attempt ended ("connection refused"), for the closed
    /// Details disclosure; never the message itself.
    pub detail: String,
}

fn now_ms() -> i64 {
    chrono::Utc::now().timestamp_millis()
}

impl Engine {
    // ---- the Hosts read --------------------------------------------------

    fn watched_engine(&self, url: &str) -> Option<&Engine> {
        if self.base_url == url {
            return Some(self);
        }
        self.peers.list.iter().find(|p| p.url == url).map(|p| &*p.engine)
    }

    fn watched_engine_mut(&mut self, url: &str) -> Option<&mut Engine> {
        if self.base_url == url {
            return Some(self);
        }
        self.peers.list.iter_mut().find(|p| p.url == url).map(|p| &mut *p.engine)
    }

    fn watched_label(&self, url: &str) -> String {
        if self.base_url == url {
            return self.primary_label();
        }
        self.peers.list.iter().find(|p| p.url == url).map(|p| p.label.clone()).unwrap_or_else(|| url.to_owned())
    }

    /// Every Host this engine reads, and whether each answers now (None:
    /// still connecting for the first time, or not set up).
    fn host_states(&self) -> Vec<(String, Option<bool>)> {
        let mut states = Vec::new();
        if !self.base_url.is_empty() {
            let configured = !self.token.is_empty() || self.has_stored_credential;
            let state = match self.connection_state.as_str() {
                "live" => Some(true),
                "offline" | "unauthorized" if configured => Some(false),
                "reconnecting" => Some(false),
                _ => None,
            };
            states.push((self.base_url.clone(), state));
        }
        for peer in &self.peers.list {
            let state = match peer.engine.connection_state() {
                "live" => Some(true),
                "offline" | "reconnecting" | "unauthorized" if peer.settled => Some(false),
                _ => None,
            };
            states.push((peer.url.clone(), state));
        }
        states
    }

    /// After each pump: notices Hosts that stopped or started answering.
    pub(crate) fn watch_hosts(&mut self) {
        if self.peer || !self.host_down.started {
            return;
        }
        let states = self.host_states();
        let now = Instant::now();
        let wall = now_ms();
        self.host_down.watches.retain(|w| states.iter().any(|(url, _)| *url == w.url));
        let mut probe = Vec::new();
        let mut timers = Vec::new();
        let mut changed = false;
        for (url, state) in &states {
            if !self.host_down.watches.iter().any(|w| w.url == *url) {
                let last_ok_ms = self.settings.get(&format!("{LAST_OK}{url}")).and_then(serde_json::Value::as_i64);
                self.host_down.watches.push(Watch {
                    url: url.clone(),
                    seen_live: false,
                    last_ok_ms,
                    saved_at: None,
                    down: None,
                    back_until: None,
                    generation: 0,
                    host_version: String::new(),
                    contract: None,
                });
            }
            let index = self.host_down.watches.iter().position(|w| w.url == *url).expect("watched");
            let watch = &mut self.host_down.watches[index];
            match state {
                Some(true) => {
                    watch.last_ok_ms = Some(wall);
                    let save = watch.saved_at.is_none_or(|at| now.duration_since(at) >= LAST_OK_SAVE);
                    watch.seen_live = true;
                    if watch.down.take().is_some() {
                        watch.generation += 1;
                        watch.back_until = Some(now + host_down::BACK_ONLINE_FOR);
                        timers.push(Message::HostBackOnlineDue { url: url.clone(), generation: watch.generation });
                        changed = true;
                    }
                    if save {
                        watch.saved_at = Some(now);
                        self.settings.set(&format!("{LAST_OK}{url}"), wall);
                    }
                }
                Some(false) if watch.down.is_none() => {
                    let since_ms = if watch.seen_live { wall } else { watch.last_ok_ms.unwrap_or(wall) };
                    watch.generation += 1;
                    watch.back_until = None;
                    watch.down = Some(Down { since_ms, attempts: 0, backoff_step: 0, last: None, reason: Reason::Unknown, next_at: None, in_flight: false, answered: false });
                    if let Some(at) = watch.last_ok_ms {
                        self.settings.set(&format!("{LAST_OK}{url}"), at);
                    }
                    probe.push(url.clone());
                    changed = true;
                }
                _ => {}
            }
        }
        for timer in timers {
            self.after(host_down::BACK_ONLINE_FOR, timer);
        }
        if changed {
            self.changes.push(Change::HostDown);
        }
        if !probe.is_empty() {
            self.host_down.network = clarp_net::device::network();
            for url in probe {
                self.probe_host(&url);
            }
        }
    }

    /// One probe of `url`, off the UI thread; `host_probed` takes its answer.
    fn probe_host(&mut self, url: &str) {
        let Some(engine) = self.watched_engine(url) else { return };
        let (token, expected) = (engine.token.clone(), engine.server_id.clone());
        let Some(watch) = self.host_down.watches.iter_mut().find(|w| w.url == url) else { return };
        let Some(down) = watch.down.as_mut() else { return };
        if down.in_flight {
            return;
        }
        down.in_flight = true;
        down.next_at = None;
        watch.generation += 1;
        let generation = watch.generation;
        let (sender, wake, base) = (self.sender.clone(), self.wake.clone(), url.to_owned());
        self.runtime.spawn(async move {
            let probe = clarp_net::probe::probe(&base, &token, &expected, host_down::PROBE_TIMEOUT).await;
            if sender.send(Message::HostProbed { url: base, generation, probe }).is_ok() {
                wake();
            }
        });
        self.changes.push(Change::HostDown);
    }

    pub(crate) fn host_probed(&mut self, url: &str, generation: u64, probe: Probe) {
        let Some(index) = self.host_down.watches.iter().position(|w| w.url == url && w.generation == generation) else { return };
        let context = self.reason_context(url);
        let watch = &mut self.host_down.watches[index];
        let Some(down) = watch.down.as_mut() else { return };
        down.in_flight = false;
        down.attempts += 1;
        down.backoff_step += 1;
        let delay = host_down::backoff(down.backoff_step);
        down.next_at = Some(Instant::now() + delay);
        let answered = matches!(probe, Probe::Answered { .. });
        match probe {
            Probe::Answered { version, contract, .. } => {
                down.answered = true;
                watch.host_version = version;
                watch.contract = contract;
            }
            Probe::Failed(attempt) => {
                down.answered = false;
                down.reason = host_down::classify(&attempt, &context);
                down.last = Some(attempt);
            }
        }
        watch.generation += 1;
        let generation = watch.generation;
        // The Host answers: its own engine connects again (unless it is on
        // the way already).
        if answered
            && let Some(engine) = self.watched_engine_mut(url)
            && matches!(engine.connection_state(), "offline" | "unauthorized")
        {
            engine.reconnect();
        }
        self.after(delay, Message::HostProbeDue { url: url.to_owned(), generation });
        self.changes.push(Change::HostDown);
    }

    pub(crate) fn host_probe_due(&mut self, url: &str, generation: u64) {
        if self.host_down.watches.iter().any(|w| w.url == url && w.generation == generation && w.down.is_some()) {
            self.probe_host(url);
        }
    }

    pub(crate) fn host_back_online_due(&mut self, url: &str, generation: u64) {
        if let Some(watch) = self.host_down.watches.iter_mut().find(|w| w.url == url && w.generation == generation) {
            watch.back_until = None;
            self.changes.push(Change::HostDown);
        }
    }

    /// What else the classifier knows about `url`'s failure.
    fn reason_context(&self, url: &str) -> Context {
        let others: Vec<&Watch> = self.host_down.watches.iter().filter(|w| w.url != url && w.seen_live || w.url != url && w.down.is_some()).collect();
        let tailnet = |w: &Watch| host_down::address_kind(&w.url) == AddressKind::Tailnet;
        let answering = |w: &Watch| w.down.is_none();
        Context {
            device_online: self.host_down.network.online,
            tailscale_on_device: self.host_down.network.tailscale,
            address: host_down::address_kind(url),
            other_hosts: others.len(),
            other_hosts_answering: others.iter().filter(|w| answering(w)).count(),
            other_tailnet_hosts: others.iter().filter(|w| tailnet(w)).count(),
            other_tailnet_hosts_answering: others.iter().filter(|w| tailnet(w) && answering(w)).count(),
        }
    }

    // ---- what the window asks ---------------------------------------------

    /// The panel for `url`'s Host: while it does not answer, and for the two
    /// seconds of `Back online` after.
    pub fn host_down(&self, url: &str) -> Option<HostDownView> {
        let watch = self.host_down.watches.iter().find(|w| w.url == url)?;
        let now = Instant::now();
        let back = watch.back_until.is_some_and(|until| until > now);
        let (reason, since_ms, status, detail) = match (&watch.down, back) {
            (_, true) => (Reason::Unknown, watch.last_ok_ms.unwrap_or_default(), HostDownStatus::BackOnline, String::new()),
            (Some(down), false) => {
                let status = match down.next_at {
                    _ if down.in_flight || down.answered || down.attempts == 0 => HostDownStatus::Reconnecting,
                    Some(at) => HostDownStatus::RetryIn(at.saturating_duration_since(now)),
                    None => HostDownStatus::Reconnecting,
                };
                (down.reason, down.since_ms, status, down.last.as_ref().map(Attempt::describe).unwrap_or_default())
            }
            (None, false) => return None,
        };
        Some(HostDownView {
            url: url.to_owned(),
            label: self.watched_label(url),
            reason,
            since_ms,
            last_ok_ms: watch.last_ok_ms,
            status,
            back_online: back,
            action: reason.action(),
            switch: self.saved_host_count() > 1,
            detail,
        })
    }

    /// The chat's Host's panel (a peer's chat: its peer's).
    pub fn host_down_for_session(&self, session: &str) -> Option<HostDownView> {
        let url = match clarp_core::all_hosts::split(session) {
            Some((server_id, _)) => self.peers.list.iter().find(|p| p.server_id == server_id)?.url.clone(),
            None => self.base_url.clone(),
        };
        self.host_down(&url)
    }

    /// Every Host with a panel now, the primary first.
    pub fn hosts_down(&self) -> Vec<HostDownView> {
        self.host_down.watches.iter().filter_map(|w| self.host_down(&w.url)).collect()
    }

    /// Whether a Host's chat may send: not while its Host does not answer.
    pub fn host_answers_for_session(&self, session: &str) -> bool {
        self.host_down_for_session(session).is_none_or(|view| view.back_online)
    }

    /// This device has no network at all (and some Host is out of reach):
    /// one app-wide banner instead of a panel per Host.
    pub fn device_offline(&self) -> bool {
        self.host_down.network.online == Some(false) && self.host_down.watches.iter().any(|w| w.down.is_some())
    }

    /// Try again: `url` is probed at once, its backoff from the start.
    pub fn host_try_again(&mut self, url: &str) {
        self.host_down.network = clarp_net::device::network();
        if let Some(watch) = self.host_down.watches.iter_mut().find(|w| w.url == url)
            && let Some(down) = watch.down.as_mut()
        {
            down.backoff_step = 0;
            if down.in_flight {
                return;
            }
            watch.generation += 1;
            self.probe_host(url);
        }
    }

    /// The window came to the front, or the network changed: every Host out
    /// of reach is probed at once.
    pub fn hosts_probe_now(&mut self) {
        let urls: Vec<String> = self.host_down.watches.iter().filter(|w| w.down.is_some()).map(|w| w.url.clone()).collect();
        for url in urls {
            self.host_try_again(&url);
        }
    }

    /// Each second while a Host is out of reach (the status line counts
    /// down): a change in this device's network probes every such Host.
    pub fn host_down_tick(&mut self) {
        if !self.host_down.watches.iter().any(|w| w.down.is_some()) {
            self.host_down.fingerprint = None;
            return;
        }
        let interfaces = clarp_net::device::interfaces();
        let fingerprint = interfaces.as_deref().map(clarp_net::device::fingerprint);
        let changed = self.host_down.fingerprint.is_some() && fingerprint != self.host_down.fingerprint;
        self.host_down.fingerprint = fingerprint;
        let network = interfaces.as_deref().map_or_else(DeviceNetwork::default, clarp_net::device::network_of);
        if network != self.host_down.network {
            self.host_down.network = network;
            self.changes.push(Change::HostDown);
        }
        if changed {
            eprintln!("Engine: the network changed; asking the Hosts out of reach again");
            self.hosts_probe_now();
        }
    }

    /// The private-data-free diagnostics for `url`'s Host.
    pub fn host_diagnostics(&self, url: &str, platform: &str, version: &str, build: &str) -> String {
        let Some(watch) = self.host_down.watches.iter().find(|w| w.url == url) else { return String::new() };
        let engine = self.watched_engine(url);
        let server_id = engine.map(|e| e.server_id.clone()).unwrap_or_default();
        let host_version = if watch.host_version.is_empty() { engine.map(|e| e.server_version.clone()).unwrap_or_default() } else { watch.host_version.clone() };
        let others: Vec<&Watch> = self.host_down.watches.iter().filter(|w| w.url != url).collect();
        let down = watch.down.as_ref();
        let now = Instant::now();
        host_down::diagnostics(&host_down::Diagnostics {
            platform,
            version,
            build,
            host_id_or_url: if server_id.is_empty() { url } else { &server_id },
            base_url: url,
            host_version: &host_version,
            contract: watch.contract.or_else(|| engine.and_then(|e| e.server_contract)),
            down_since_ms: down.map_or(now_ms(), |d| d.since_ms),
            now_ms: now_ms(),
            reason: down.map_or(Reason::Unknown, |d| d.reason),
            last_attempt: down.and_then(|d| d.last.as_ref()),
            attempts: down.map_or(0, |d| d.attempts),
            next_in: down.and_then(|d| d.next_at).map(|at| at.saturating_duration_since(now)),
            others_answering: others.iter().filter(|w| w.down.is_none()).count(),
            others: others.len(),
            network: self.host_down.network.clone(),
        })
    }
}
