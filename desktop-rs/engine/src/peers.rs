//! "Chats from all paired Hosts" (Maxteabag/clarp#651): with the device
//! preference on, the engine also reads every other saved Host it holds a
//! device token for. Each such peer is a whole engine of its own (its token,
//! snapshot, event stream, chats, sends and stops), owned here and pumped
//! with the primary; nothing is relayed between Hosts.
//!
//! The primary stays what it was: new chats, the Oracle, Teams, janitors and
//! Host settings act on it. A peer's chats are addressed as
//! `@<server_id>/<session>` (`clarp_core::all_hosts`), and every per-chat
//! method hands such a chat to its peer (`on_peer!`), so opening, reading,
//! sending, live updates, stopping and per-chat settings all go to the chat's
//! own Host. The chat list (`roster()`) and the attention items merge every
//! Host's; a decision or artifact is answered on the Host that holds it.
//!
//! Off (the default), no peer exists and the engine behaves exactly as
//! before. A Host without a device token is never read.

use std::collections::{HashMap, HashSet};
use std::time::Duration;

use clarp_core::all_hosts::{self, HostRoster};
use clarp_core::roster::Roster;
use clarp_core::settings::{Settings, normalized_base_url};
use serde_json::{Value, json};

use crate::{Change, Config, Engine, Message};

/// A peer that could not be reached is tried again this often.
const PEER_RETRY: Duration = Duration::from_secs(20);

pub(crate) struct Peer {
    pub(crate) url: String,
    /// The name the desktop shows for it: its `/server-info` name, else the
    /// saved one, else its address.
    pub(crate) label: String,
    /// Its `server_id`: saved from an earlier run, else once it answered.
    /// Its chats are listed only once this is known.
    pub(crate) server_id: String,
    pub(crate) engine: Box<Engine>,
    /// The primary's open panes and selection on this Host were handed to it.
    handed_over: bool,
    /// Its first connect ended (live, or failed): until then the
    /// "<Host> unreachable" note stays quiet for it.
    settled: bool,
}

#[derive(Default)]
pub(crate) struct Peers {
    pub(crate) enabled: bool,
    pub(crate) list: Vec<Peer>,
    /// Saved Hosts whose device token was looked up (or is being looked up).
    looked_up: HashSet<String>,
    /// Saved Hosts the keyring holds a device token for: paired.
    tokens: HashMap<String, String>,
    /// Whether rows show their Host badge, as last applied.
    badges: bool,
    /// Bumped whenever the peers are dropped: older lookups are ignored.
    generation: u64,
    retry_scheduled: bool,
    /// Every Host's agents, while at least one peer is listed.
    pub(crate) merged: Option<Roster>,
    attention: Vec<Value>,
    artifacts: Vec<Value>,
    /// Decision and artifact id -> the server id of the peer that holds it.
    owners: HashMap<String, String>,
    /// The peer (server id) whose turn queue is loaded, and its chat.
    queue_peer: String,
    queue_session: String,
}

impl Engine {
    // ---- routing ---------------------------------------------------------

    /// For a peer chat: Some(its peer engine and its own session), or
    /// Some(None) when that Host is not read. None for the primary's chats.
    pub(crate) fn peer_ref<'s>(&self, session: &'s str) -> Option<Option<(&Engine, &'s str)>> {
        let (server_id, local) = all_hosts::split(session)?;
        Some(self.peers.list.iter().find(|p| p.server_id == server_id).map(|p| (&*p.engine, local)))
    }

    pub(crate) fn peer_mut<'s>(&mut self, session: &'s str) -> Option<Option<(&mut Engine, &'s str)>> {
        let (server_id, local) = all_hosts::split(session)?;
        Some(self.peers.list.iter_mut().find(|p| p.server_id == server_id).map(|p| (&mut *p.engine, local)))
    }

    /// The peer holding a decision or artifact (by its id): one of its
    /// Updates, or an older one its agent's publications listed.
    pub(crate) fn owner_mut(&mut self, id: &str) -> Option<Option<(&mut Engine, &str)>> {
        let server_id = self.owner_server(id)?;
        Some(self.peers.list.iter_mut().find(|p| p.server_id == server_id).map(|p| (&mut *p.engine, "")))
    }

    pub(crate) fn owner_ref(&self, id: &str) -> Option<Option<(&Engine, &str)>> {
        let server_id = self.owner_server(id)?;
        Some(self.peers.list.iter().find(|p| p.server_id == server_id).map(|p| (&*p.engine, "")))
    }

    fn owner_server(&self, id: &str) -> Option<String> {
        if let Some(server_id) = self.peers.owners.get(id) {
            return Some(server_id.clone());
        }
        if self.holds_publication_artifact(id) {
            return None;
        }
        self.peers.list.iter().find(|p| !p.server_id.is_empty() && p.engine.holds_publication_artifact(id)).map(|p| p.server_id.clone())
    }

    /// A peer's publication artifact by its id, its session qualified as
    /// the merged rows' are.
    pub(crate) fn peers_publication_artifact(&self, artifact_id: &str) -> Option<Value> {
        self.peers.list.iter().filter(|p| !p.server_id.is_empty()).find_map(|peer| {
            let mut row = peer.engine.held_publication_artifact(artifact_id)?;
            if let Some(object) = row.as_object_mut()
                && let Some(session) = object.get("session").and_then(Value::as_str).filter(|s| !s.is_empty())
            {
                let qualified = all_hosts::qualify(&peer.server_id, session);
                object.insert("session".into(), json!(qualified));
            }
            Some(row)
        })
    }

    /// The peer whose turn queue is loaded.
    pub(crate) fn queue_peer_mut(&mut self) -> Option<Option<(&mut Engine, &str)>> {
        if self.peers.queue_peer.is_empty() {
            return None;
        }
        let server_id = self.peers.queue_peer.clone();
        Some(self.peers.list.iter_mut().find(|p| p.server_id == server_id).map(|p| (&mut *p.engine, "")))
    }

    pub(crate) fn queue_peer_ref(&self) -> Option<Option<(&Engine, &str)>> {
        if self.peers.queue_peer.is_empty() {
            return None;
        }
        Some(self.peers.list.iter().find(|p| p.server_id == self.peers.queue_peer).map(|p| (&*p.engine, "")))
    }

    /// A turn queue is loaded for `session`: on its peer, or here.
    pub(crate) fn note_queue_host(&mut self, session: &str) {
        match all_hosts::split(session) {
            Some((server_id, _)) => {
                self.peers.queue_peer = server_id.to_owned();
                self.peers.queue_session = session.to_owned();
            }
            None => {
                self.peers.queue_peer.clear();
                self.peers.queue_session.clear();
            }
        }
    }

    pub(crate) fn queue_peer_session(&self) -> Option<&str> {
        (!self.peers.queue_peer.is_empty()).then_some(self.peers.queue_session.as_str())
    }

    /// The chat is on a peer this engine reads (or may read once it is
    /// found): the primary keeps it selected or open instead of replacing it.
    pub(crate) fn reads_peer_session(&self, session: &str) -> bool {
        all_hosts::is_peer_session(session) && !self.peer && self.all_hosts()
    }

    /// Opens a peer's chat: this engine selects it (panes, Ctrl+R), its own
    /// Host opens it. False for the primary's chats.
    pub(crate) fn select_on_peer(&mut self, session: &str) -> bool {
        let Some((server_id, local)) = all_hosts::split(session) else {
            // A primary chat: no peer has a chat open any more.
            for peer in &mut self.peers.list {
                peer.engine.selected.clear();
            }
            return false;
        };
        self.waiting_for_session_choice = false;
        if self.selected != session {
            self.selected = session.to_owned();
            self.changes.push(Change::Selection);
            if self.panes.tree.active_session() != session {
                self.with_panes(|panes| panes.set_active_session(session));
            }
        }
        for peer in &mut self.peers.list {
            if peer.server_id == server_id {
                peer.engine.select(local);
            } else {
                peer.engine.selected.clear();
            }
        }
        true
    }

    // ---- the switch --------------------------------------------------------

    /// Whether "Show chats from all Hosts" is on.
    pub fn all_hosts(&self) -> bool {
        self.settings.boolean(all_hosts::SETTING, false)
    }

    /// Turns "Show chats from all Hosts" on or off (a device preference).
    pub fn set_all_hosts(&mut self, on: bool) {
        if self.all_hosts() != on {
            self.settings.set(all_hosts::SETTING, on);
            self.changes.push(Change::Preferences);
        }
        self.apply_all_hosts();
    }

    /// Starts or stops reading the peers as the preference says (also after
    /// the settings file changed), and looks for newly saved Hosts.
    pub(crate) fn apply_all_hosts(&mut self) {
        let on = !self.peer && self.all_hosts();
        if on != self.peers.enabled {
            self.peers.enabled = on;
            if !on {
                self.drop_peers();
            }
            self.changes.extend([Change::Roster, Change::Updates, Change::RosterFreshness]);
        }
        self.look_up_pairings();
        self.create_peers();
        self.apply_badges();
    }

    /// The primary changed: every peer is found again.
    pub(crate) fn restart_peers(&mut self) {
        if self.peer {
            return;
        }
        if self.peers.enabled {
            self.drop_peers();
        }
        self.look_up_pairings();
        self.create_peers();
        self.apply_badges();
    }

    fn drop_peers(&mut self) {
        self.peers.generation += 1;
        self.peers.retry_scheduled = false;
        for mut peer in std::mem::take(&mut self.peers.list) {
            peer.engine.shutdown();
        }
        self.peers.merged = None;
        self.peers.attention.clear();
        self.peers.artifacts.clear();
        self.peers.owners.clear();
        self.peers.queue_peer.clear();
        self.peers.queue_session.clear();
        self.changes.extend([Change::Roster, Change::Updates]);
        // A peer's chat cannot stay open without its Host.
        if all_hosts::is_peer_session(&self.selected) {
            self.selected.clear();
            self.changes.push(Change::Selection);
            if let Some(first) = self.roster.first_session().map(str::to_owned) {
                self.select(&first);
            }
        }
    }

    /// Looks up the device token of every saved Host not looked up yet,
    /// whether or not the option is on: a Host counts as paired exactly when
    /// the keyring holds one for it (the Host badges count pairings).
    pub(crate) fn look_up_pairings(&mut self) {
        if self.peer {
            return;
        }
        let generation = self.peers.generation;
        for host in clarp_core::hosts::list(&self.settings) {
            if host.url == self.base_url || !self.peers.looked_up.insert(host.url.clone()) {
                continue;
            }
            if !self.keyring {
                // Checks and tests: no keyring, so CLARP_PEER_TOKEN (when
                // set) stands for every saved Host's device token.
                let token = std::env::var("CLARP_PEER_TOKEN").unwrap_or_default();
                self.peer_credential(&host.url, token, generation);
                continue;
            }
            let (sender, wake, base) = (self.sender.clone(), self.wake.clone(), host.url.clone());
            self.runtime.spawn(async move {
                let token = clarp_net::credentials::lookup(&base).await;
                if sender.send(Message::PeerCredential { base, token, generation }).is_ok() {
                    wake();
                }
            });
        }
    }

    pub(crate) fn peer_credential(&mut self, base: &str, token: String, _generation: u64) {
        let base = normalized_base_url(base);
        let token = token.trim().to_owned();
        if token.is_empty() {
            self.peers.tokens.remove(&base);
        } else {
            self.peers.tokens.insert(base, token);
        }
        self.create_peers();
        self.apply_badges();
    }

    /// "Show the Host on each chat" (on unless set off).
    pub fn host_badges_setting(&self) -> bool {
        self.settings.boolean(all_hosts::BADGES_SETTING, true)
    }

    pub fn set_host_badges_setting(&mut self, on: bool) {
        if self.host_badges_setting() != on {
            self.settings.set(all_hosts::BADGES_SETTING, on);
            self.changes.push(Change::Preferences);
        }
        self.apply_badges();
    }

    /// How many Hosts this desktop has saved: the primary and every other
    /// saved Host, one per server id (paired or not, as on iOS).
    pub fn saved_host_count(&self) -> usize {
        let mut seen: Vec<String> = vec![self.base_url.clone()];
        if !self.server_id.is_empty() {
            seen.push(self.server_id.clone());
        }
        let mut count = 1;
        for host in clarp_core::hosts::list(&self.settings) {
            if host.url == self.base_url {
                continue;
            }
            let identity = if host.server_id.is_empty() { host.url.clone() } else { host.server_id.clone() };
            if !seen.contains(&identity) {
                seen.push(identity);
                count += 1;
            }
        }
        count
    }

    /// Whether rows and chat headers name their Host: the setting is on and
    /// this desktop has saved more than one Host (#651; iOS counts saved
    /// Hosts, AllHostsReading.swift).
    pub fn host_badges(&self) -> bool {
        !self.peer && self.host_badges_setting() && self.saved_host_count() > 1
    }

    /// The first launch of a build with the option switches "Chats from all
    /// paired Hosts" on once when more than one Host is saved, never again.
    /// Not while the saved Hosts cannot be read (none listed): it waits for
    /// a launch that can read them.
    pub fn default_all_hosts_once(&mut self) {
        if self.peer || self.settings.boolean(all_hosts::DEFAULTED_SETTING, false) {
            return;
        }
        if clarp_core::hosts::list(&self.settings).is_empty() {
            return;
        }
        if self.saved_host_count() > 1 && !self.all_hosts() {
            self.settings.set(all_hosts::SETTING, true);
            self.changes.push(Change::Preferences);
        }
        self.settings.set(all_hosts::DEFAULTED_SETTING, true);
    }

    /// The rows are rebuilt when whether they show badges changed.
    pub(crate) fn apply_badges(&mut self) {
        let shown = self.host_badges();
        if shown != self.peers.badges {
            self.peers.badges = shown;
            self.rebuild_merged();
            self.changes.push(Change::Roster);
        }
    }

    /// The badge a chat's row and header show ("" for none): its Host's
    /// name as this desktop shows it, or where a moved agent lives now.
    pub fn host_badge(&self, session: &str) -> String {
        if self.peers.merged.is_some() {
            return self.roster().find(session).map(|a| a.host.clone()).unwrap_or_default();
        }
        if !self.host_badges() {
            return String::new();
        }
        let Some(agent) = self.roster.find(session) else { return String::new() };
        let listed = [self.server_id.as_str()];
        all_hosts::badge(agent, &self.primary_label(), &listed, false, true)
    }

    /// Starts a peer for every paired saved Host not read yet, while the
    /// option is on.
    fn create_peers(&mut self) {
        if !self.peers.enabled || self.peer {
            return;
        }
        let paired: Vec<(String, String)> = clarp_core::hosts::list(&self.settings)
            .into_iter()
            .filter_map(|h| self.peers.tokens.get(&h.url).map(|t| (h.url.clone(), t.clone())))
            .collect();
        for (base, token) in paired {
            self.start_peer(&base, &token);
        }
    }

    fn start_peer(&mut self, base: &str, token: &str) {
        let base = base.to_owned();
        if base == self.base_url || self.peers.list.iter().any(|p| p.url == base) {
            return;
        }
        let Some(saved) = clarp_core::hosts::list(&self.settings).into_iter().find(|h| h.url == base) else { return };
        if !saved.server_id.is_empty() && (saved.server_id == self.server_id || self.peers.list.iter().any(|p| p.server_id == saved.server_id)) {
            return;
        }
        // The peer's preferences are the primary's (activity, narration,
        // themes), held in memory: it never writes the settings file.
        let mut settings = Settings::in_memory();
        for (key, value) in self.settings.values() {
            if !key.starts_with("composerDrafts/") {
                settings.set(key, value.clone());
            }
        }
        let config = Config {
            base_url: base.clone(),
            token: token.to_owned(),
            settings,
            workspace_store: None,
            keyring: false,
            transcript_cache: self.transcript_cache.as_ref().map(|c| c.root().to_path_buf()),
            form_events: None,
        };
        let mut engine = Engine::build(config, self.runtime.clone(), self.wake.clone());
        engine.peer = true;
        if !saved.server_id.is_empty() {
            engine.cache_key = format!("clarp-server:{}", saved.server_id);
        }
        engine.start();
        eprintln!("Engine: reading {} as a peer Host ({})", saved.label(), base);
        self.peers.list.push(Peer { url: base, label: saved.label(), server_id: saved.server_id.clone(), engine: Box::new(engine), handed_over: false, settled: false });
        let index = self.peers.list.len() - 1;
        if !saved.server_id.is_empty() {
            self.hand_over(index);
        }
        self.schedule_peer_retry();
        self.rebuild_merged();
        self.changes.extend([Change::Roster, Change::RosterFreshness]);
    }

    /// The primary's selection and open panes on this peer's Host are its.
    fn hand_over(&mut self, index: usize) {
        let server_id = self.peers.list[index].server_id.clone();
        if server_id.is_empty() || std::mem::replace(&mut self.peers.list[index].handed_over, true) {
            return;
        }
        let mut shown: Vec<String> = self
            .panes
            .tree
            .view_layout()
            .iter()
            .filter(|pane| pane.get("shown").and_then(Value::as_bool) == Some(true))
            .filter_map(|pane| pane.get("session").and_then(Value::as_str).map(str::to_owned))
            .collect();
        shown.retain(|s| all_hosts::split(s).is_some_and(|(id, _)| id == server_id));
        let selected = self.selected.clone();
        let engine = &mut self.peers.list[index].engine;
        engine.live.shown = shown.iter().filter_map(|s| all_hosts::split(s)).map(|(_, local)| local.to_owned()).collect();
        for session in &shown {
            if *session != selected {
                engine.open_log(all_hosts::split(session).map_or("", |(_, l)| l));
            }
        }
        if let Some((id, local)) = all_hosts::split(&selected) {
            if id == server_id {
                engine.select(local);
            }
        }
    }

    fn schedule_peer_retry(&mut self) {
        if !std::mem::replace(&mut self.peers.retry_scheduled, true) {
            self.after(PEER_RETRY, Message::PeersDue(self.peers.generation));
        }
    }

    /// Peers that could not be reached connect again, without slowing the
    /// others (each has its own requests and stream).
    pub(crate) fn peers_due(&mut self, generation: u64) {
        if generation != self.peers.generation {
            return;
        }
        self.peers.retry_scheduled = false;
        if !self.peers.enabled {
            return;
        }
        for peer in &mut self.peers.list {
            if matches!(peer.engine.connection_state(), "offline") {
                peer.engine.reconnect();
            }
        }
        self.create_peers();
        self.schedule_peer_retry();
    }

    /// A preference changed: the peers read it too.
    pub(crate) fn share_preferences_with_peers(&mut self) {
        let values: Vec<(String, Value)> =
            self.settings.values().iter().filter(|(k, _)| !k.starts_with("composerDrafts/")).map(|(k, v)| (k.clone(), v.clone())).collect();
        for peer in &mut self.peers.list {
            for (key, value) in &values {
                if peer.engine.settings.get(key) != Some(value) {
                    peer.engine.settings.set(key, value.clone());
                }
            }
            peer.engine.reload_preferences();
        }
    }

    // ---- pumping -------------------------------------------------------------

    /// Applies what the peers received, as changes of the merged view.
    pub(crate) fn pump_peers(&mut self) {
        let roster_change = |c: &Change| matches!(c, Change::Roster | Change::Archive | Change::RosterFreshness | Change::Connection | Change::ServerInfo);
        let mut roster_changed = self.changes.iter().any(roster_change);
        let mut updates_changed = self.changes.contains(&Change::Updates);
        if !self.peer && self.changes.iter().any(|c| matches!(c, Change::Hosts | Change::ServerInfo)) {
            // A Host saved, forgotten or identified: the pairings may differ.
            self.look_up_pairings();
            self.create_peers();
            self.apply_badges();
        }
        if self.peers.list.is_empty() {
            if roster_changed && self.peers.merged.is_some() {
                self.rebuild_merged();
            }
            return;
        }
        let mut out = Vec::new();
        let mut identified = Vec::new();
        let mut chat_views = Vec::new();
        let badges = self.peers.badges;
        for (index, peer) in self.peers.list.iter_mut().enumerate() {
            let changes = peer.engine.pump();
            if peer.engine.connection_state() != "connecting" {
                peer.settled = true;
            }
            // What the peer's Host said of a chat's view (#702): the
            // primary keeps every chat's own choice and its error line.
            chat_views.extend(peer.engine.qualified_chat_view_outcomes(&peer.server_id));
            for change in changes {
                if change == Change::ServerInfo {
                    let (id, name) = (peer.engine.server_id().to_owned(), peer.engine.server_name().to_owned());
                    if peer.label.is_empty() || peer.label == peer.url {
                        peer.label = name;
                    }
                    if !id.is_empty() && id != peer.server_id {
                        peer.server_id = id;
                    }
                    identified.push(index);
                }
                let server_id = peer.server_id.as_str();
                let q = |session: String| if session.is_empty() || server_id.is_empty() { session } else { all_hosts::qualify(server_id, &session) };
                match change {
                    Change::Conversation(s) => out.push(Change::Conversation(q(s))),
                    Change::Live(s) => out.push(Change::Live(q(s))),
                    Change::Composer(s) => out.push(Change::Composer(q(s))),
                    Change::Queue(s) => out.push(Change::Queue(q(s))),
                    Change::Stopped(s) => out.push(Change::Stopped(q(s))),
                    Change::AgentMutated(s) => out.push(Change::AgentMutated(q(s))),
                    Change::AssignmentSucceeded(s) => out.push(Change::AssignmentSucceeded(q(s))),
                    Change::AssignmentRequested { session, automatic } => out.push(Change::AssignmentRequested { session: q(session), automatic }),
                    Change::Notification { title, body } => {
                        let title = if badges { all_hosts::titled(&title, &peer.label) } else { title };
                        out.push(Change::Notification { title, body })
                    }
                    Change::Roster | Change::Archive | Change::RosterFreshness | Change::Connection | Change::ServerInfo | Change::Error | Change::Rooms => {
                        roster_changed = true
                    }
                    Change::Updates => updates_changed = true,
                    Change::Processes | Change::Avatars | Change::Media | Change::Publications | Change::Search | Change::Narrator => out.push(change),
                    // The primary's alone: its selection, panes, saved Hosts,
                    // audio endpoint, voice, launch, Teams and settings.
                    _ => {}
                }
            }
        }
        self.changes.extend(out);
        self.settle_chat_views(chat_views);
        for index in identified {
            self.peer_identified(index);
        }
        self.drop_duplicate_peers();
        if roster_changed || updates_changed {
            self.rebuild_merged();
            self.changes.extend([Change::Roster, Change::RosterFreshness]);
        }
        if updates_changed || roster_changed {
            self.changes.push(Change::Updates);
        }
    }

    /// A peer answered `/server-info`: its name and server id are saved
    /// (in place: reading it does not make it the most recent Host).
    fn peer_identified(&mut self, index: usize) {
        let Some(peer) = self.peers.list.get(index) else { return };
        let (url, name, id) = (peer.url.clone(), peer.engine.server_name().to_owned(), peer.server_id.clone());
        let before = clarp_core::hosts::list(&self.settings);
        clarp_core::hosts::set_identity(&mut self.settings, &url, &name, &id);
        if clarp_core::hosts::list(&self.settings) != before {
            self.changes.push(Change::Hosts);
        }
        let label = self.label_of(&url, &name);
        self.peers.list[index].label = label;
        self.hand_over(index);
    }

    /// The same Host saved under two addresses is read once: by server id.
    fn drop_duplicate_peers(&mut self) {
        let mut seen: Vec<String> = vec![self.server_id.clone()];
        let mut index = 0;
        while index < self.peers.list.len() {
            let id = self.peers.list[index].server_id.clone();
            if !id.is_empty() && seen.contains(&id) {
                let mut peer = self.peers.list.remove(index);
                eprintln!("Engine: {} is the same Host as one already read; not reading it twice", peer.url);
                peer.engine.shutdown();
                continue;
            }
            seen.push(id);
            index += 1;
        }
    }

    /// Whether a peer can be reached now (its rows show offline otherwise).
    fn reachable(engine: &Engine) -> bool {
        engine.roster.transport_available() && !matches!(engine.connection_state(), "offline" | "reconnecting" | "unauthorized")
    }

    /// The name this desktop shows for a pairing (`SavedHost::label`): the
    /// saved name, else `fallback` (its `/server-info` name), else its address.
    fn label_of(&self, url: &str, fallback: &str) -> String {
        let saved = clarp_core::hosts::list(&self.settings).into_iter().find(|h| h.url == url);
        match saved {
            Some(host) if !host.name.is_empty() => host.label(),
            _ if !fallback.is_empty() => fallback.to_owned(),
            _ => clarp_core::hosts::SavedHost { url: url.to_owned(), name: String::new(), server_id: String::new() }.label(),
        }
    }

    /// What the primary's rows say about their Host.
    pub(crate) fn primary_label(&self) -> String {
        self.label_of(&self.base_url, &self.server_name)
    }

    /// Rebuilds the merged chat list and attention items from every Host.
    pub(crate) fn rebuild_merged(&mut self) {
        let listed: Vec<&Peer> = self.peers.list.iter().filter(|p| !p.server_id.is_empty()).collect();
        if !self.peers.enabled || listed.is_empty() {
            self.peers.merged = None;
            self.peers.attention.clear();
            self.peers.artifacts.clear();
            self.peers.owners.clear();
            return;
        }
        let label = self.primary_label();
        let mut hosts = vec![HostRoster { server_id: &self.server_id, label: &label, agents: self.roster.agents(), reachable: self.roster.transport_available() }];
        hosts.extend(listed.iter().map(|p| HostRoster {
            server_id: &p.server_id,
            label: &p.label,
            agents: p.engine.roster.agents(),
            reachable: Self::reachable(&p.engine),
        }));
        let merged = Roster::from_agents(all_hosts::merge(&hosts, self.peers.badges));
        // Attention and artifacts: each names its Host; a peer's sessions
        // are qualified like its rows, and its ids remember their Host.
        let mut owners = HashMap::new();
        let badges = self.peers.badges;
        let tag = |items: &[Value], server_id: &str, label: &str, peer: bool, owners: &mut HashMap<String, String>| -> Vec<Value> {
            items
                .iter()
                .map(|item| {
                    let mut item = item.clone();
                    if let Some(object) = item.as_object_mut() {
                        if badges {
                            object.insert("host".into(), json!(label));
                        }
                        if peer {
                            for key in ["decision_id", "artifact_id", "id"] {
                                if let Some(id) = object.get(key).and_then(Value::as_str).filter(|id| !id.is_empty()) {
                                    owners.insert(id.to_owned(), server_id.to_owned());
                                }
                            }
                            if let Some(id) = object.get("decision").and_then(|d| d.get("decision_id")).and_then(Value::as_str).filter(|id| !id.is_empty()) {
                                owners.insert(id.to_owned(), server_id.to_owned());
                            }
                            if let Some(session) = object.get("session").and_then(Value::as_str).filter(|s| !s.is_empty()) {
                                let qualified = all_hosts::qualify(server_id, session);
                                object.insert("session".into(), json!(qualified));
                            }
                        }
                    }
                    item
                })
                .collect()
        };
        let mut attention = tag(self.updates.attention(), &self.server_id, &label, false, &mut owners);
        let mut artifacts = tag(self.updates.artifacts(), &self.server_id, &label, false, &mut owners);
        for peer in &listed {
            attention.extend(tag(peer.engine.updates.attention(), &peer.server_id, &peer.label, true, &mut owners));
            artifacts.extend(tag(peer.engine.updates.artifacts(), &peer.server_id, &peer.label, true, &mut owners));
        }
        self.peers.merged = Some(merged);
        self.peers.attention = attention;
        self.peers.artifacts = artifacts;
        self.peers.owners = owners;
    }

    /// The attention items shown: every Host's while peers are listed.
    pub(crate) fn merged_attention(&self) -> Option<&[Value]> {
        self.peers.merged.as_ref().map(|_| self.peers.attention.as_slice())
    }

    pub(crate) fn merged_artifacts(&self) -> Option<&[Value]> {
        self.peers.merged.as_ref().map(|_| self.peers.artifacts.as_slice())
    }

    /// The quiet line for peers that cannot be reached ("laptopstudio
    /// unreachable"); empty when every Host answers or the option is off.
    /// Only while more than one Host is listed (a single Host keeps the
    /// app's own connection banner), and a Host still connecting is quiet.
    pub fn unreachable_hosts_note(&self) -> String {
        if self.peers.merged.is_none() {
            return String::new();
        }
        let labels: Vec<&str> = self
            .peers
            .list
            .iter()
            .filter(|p| p.settled && !Self::reachable(&p.engine))
            .map(|p| p.label.as_str())
            .collect();
        all_hosts::unreachable_note(&labels)
    }

    /// The peers' Updates lists reload with the primary's.
    pub(crate) fn peers_load_updates(&mut self, forced: bool) {
        for peer in &mut self.peers.list {
            if forced {
                peer.engine.load_updates();
            } else {
                peer.engine.poll_updates();
            }
        }
    }

    pub(crate) fn peers_release_explanation_view(&mut self, owner: u64) {
        for peer in &mut self.peers.list {
            peer.engine.release_explanation_view(owner);
        }
    }

    /// Server ids of the Hosts read as peers, with their names (checks).
    pub fn peer_hosts(&self) -> Vec<(String, String)> {
        self.peers.list.iter().map(|p| (p.server_id.clone(), p.label.clone())).collect()
    }
}
