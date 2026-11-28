//! Chats from all paired Hosts in one list (Maxteabag/clarp#651), the pure
//! half: how a chat on another Host is named, which rows a merged list
//! keeps, in what order, and what each row says about its Host.
//!
//! The desktop stays connected to one primary Host. With the option on it
//! also reads every other saved Host it holds a device token for (a "peer"),
//! and the chat list shows their agents too. A peer's chat is addressed by
//! its Host's `server_id` and its session together (`@<server_id>/<session>`),
//! so drafts, panes and caches of two Hosts never meet; the primary's chats
//! keep their plain sessions, so with the option off nothing changes.

use crate::protocol::Agent;
use crate::tr;

// i18n-screen: chat list

/// The device preference (never a Host's): off unless set.
pub const SETTING: &str = "connection/allHosts";

/// Set once the one-time default for SETTING was decided on this device.
pub const DEFAULTED_SETTING: &str = "connection/allHostsDefaulted";

/// "Show Host badges", a device preference: on unless set off. With more
/// than one Host listed the badges always show; this hides them only while
/// the list holds one Host's chats.
pub const BADGES_SETTING: &str = "appearance/hostBadges";

/// The Host badge a row shows: where the agent lives. That is its Host
/// (`own_label`), unless the row moved to a Host not in the list (`listed`,
/// server ids): then the Host it moved to (`moved.to.host`). Empty when the
/// list holds one Host and badges are hidden.
pub fn badge(agent: &Agent, own_label: &str, listed: &[&str], several: bool, show: bool) -> String {
    if !several && !show {
        return String::new();
    }
    if !agent.moved_to.is_empty() && !agent.moved_to_host.is_empty() && !listed.contains(&agent.moved_to.as_str()) {
        return agent.moved_to_host.clone();
    }
    own_label.to_owned()
}

/// The address of `session` on the Host whose `server_id` is `server_id`.
pub fn qualify(server_id: &str, session: &str) -> String {
    format!("@{server_id}/{session}")
}

/// A peer chat's (server id, session); None for a primary chat.
pub fn split(qualified: &str) -> Option<(&str, &str)> {
    let rest = qualified.strip_prefix('@')?;
    let (server_id, session) = rest.split_once('/')?;
    (!server_id.is_empty() && !session.is_empty()).then_some((server_id, session))
}

/// Whether `session` names a chat on another Host than the primary.
pub fn is_peer_session(session: &str) -> bool {
    split(session).is_some()
}

/// One Host's agents as the merge reads them.
#[derive(Debug, Clone)]
pub struct HostRoster<'a> {
    /// Its `server_id`; empty for a primary that has not answered yet.
    pub server_id: &'a str,
    /// What its rows say (the name the desktop shows for it).
    pub label: &'a str,
    pub agents: &'a [Agent],
    /// False while it cannot be reached: its rows stay, shown offline.
    pub reachable: bool,
}

/// The merged list: `hosts[0]` is the primary, whose sessions stay as they
/// are; every other Host's sessions and agent ids are qualified by its server
/// id. With `badges` (`host_badges`) every row names its Host (`badge`). A row that moved to
/// a Host in the list (`moved.to.id` equal to that Host's `server_id`) is
/// left out: that Host lists the agent now. Ordered by last activity, ties
/// to the primary (then to each Host's own order).
pub fn merge(hosts: &[HostRoster], badges: bool) -> Vec<Agent> {
    let listed: Vec<&str> = hosts.iter().map(|h| h.server_id).filter(|id| !id.is_empty()).collect();
    let labelled = badges;
    let mut rows: Vec<(i64, usize, usize, Agent)> = Vec::new();
    for (index, host) in hosts.iter().enumerate() {
        for (position, agent) in host.agents.iter().enumerate() {
            if !agent.moved_to.is_empty() && agent.moved_to != host.server_id && listed.contains(&agent.moved_to.as_str()) {
                continue;
            }
            let mut agent = agent.clone();
            if index > 0 {
                agent.session = qualify(host.server_id, &agent.session);
                if !agent.agent_id.is_empty() {
                    agent.agent_id = qualify(host.server_id, &agent.agent_id);
                }
                if !agent.parent_agent_id.is_empty() {
                    agent.parent_agent_id = qualify(host.server_id, &agent.parent_agent_id);
                }
            }
            if labelled {
                agent.host = badge(&agent, host.label, &listed, true, true);
            }
            if !host.reachable {
                agent.latest_state = "offline".into();
                agent.status_text.clear();
                agent.alive = false;
                agent.busy = false;
            }
            rows.push((agent.last_activity, index, position, agent));
        }
    }
    rows.sort_by(|a, b| b.0.cmp(&a.0).then(a.1.cmp(&b.1)).then(a.2.cmp(&b.2)));
    rows.into_iter().map(|(_, _, _, agent)| agent).collect()
}

/// Whether rows and headers name their Host (#651): "Show the Host on each
/// chat" is on and this client holds pairings for more than one Host.
pub fn host_badges(setting: bool, paired_hosts: usize) -> bool {
    setting && paired_hosts > 1
}

/// A moved agent's quiet line (Host contract 125): "Moved to elitebook Rust",
/// shown with any number of Hosts and either option; empty when not moved.
pub fn moved_line(agent: &Agent) -> String {
    match (agent.moved_to_host.as_str(), agent.moved_to.as_str()) {
        ("", "") => String::new(),
        // i18n: chat list, quiet line of an agent moved to another Host; {} is that Host's id
        ("", id) => tr!("Moved to {}", id),
        // i18n: chat list, quiet line of an agent moved to another Host; {} is that Host's name
        (host, _) => tr!("Moved to {}", host),
    }
}

/// The quiet line under the list for Hosts that cannot be reached:
/// "laptopstudio unreachable", or several names joined; empty when none.
pub fn unreachable_note(labels: &[&str]) -> String {
    match labels {
        [] => String::new(),
        [one] => format!("{one} unreachable"),
        many => format!("{} unreachable", many.join(", ")),
    }
}

/// What a chat's header says with several Hosts in the list: its name and
/// its Host ("Rachel · laptopstudio"); the name alone otherwise.
pub fn titled(name: &str, host: &str) -> String {
    if host.is_empty() { name.to_owned() } else { format!("{name} · {host}") }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn agent(session: &str, activity: i64) -> Agent {
        Agent { agent_id: format!("{session}-id"), session: session.into(), persona: session.into(), last_activity: activity, ..Agent::default() }
    }

    fn sessions(agents: &[Agent]) -> Vec<&str> {
        agents.iter().map(|a| a.session.as_str()).collect()
    }

    #[test]
    fn a_peer_chat_is_addressed_by_server_id_and_session() {
        assert_eq!(qualify("srv-2", "rachel"), "@srv-2/rachel");
        assert_eq!(split("@srv-2/rachel"), Some(("srv-2", "rachel")));
        assert_eq!(split("@srv-2/pair:a:b"), Some(("srv-2", "pair:a:b")), "a session may hold anything after the first slash");
        assert_eq!(split("rachel"), None);
        assert_eq!(split("pair:a1:a2"), None);
        assert_eq!(split("@/rachel"), None);
        assert_eq!(split("@srv/"), None);
        assert!(!is_peer_session("rachel"));
    }

    #[test]
    fn rows_order_by_last_activity_and_ties_go_to_the_primary() {
        let primary = [agent("rachel", 2000), agent("mike", 1000)];
        let peer = [agent("nova", 1500), agent("tia", 1000), agent("old", 10)];
        let merged = merge(&[
            HostRoster { server_id: "one", label: "elitebook", agents: &primary, reachable: true },
            HostRoster { server_id: "two", label: "laptopstudio", agents: &peer, reachable: true },
        ], true);
        assert_eq!(sessions(&merged), ["rachel", "@two/nova", "mike", "@two/tia", "@two/old"]);
    }

    #[test]
    fn rows_name_their_host_only_with_badges_on_and_several_paired_hosts() {
        assert!(host_badges(true, 2));
        assert!(!host_badges(true, 1), "one paired Host: never");
        assert!(!host_badges(false, 3), "the setting hides it, also with several Hosts listed");
        let primary = [agent("rachel", 2)];
        let peer = [agent("nova", 1)];
        let hosts = [
            HostRoster { server_id: "one", label: "elitebook", agents: &primary, reachable: true },
            HostRoster { server_id: "two", label: "elitebook Rust", agents: &peer, reachable: true },
        ];
        let merged = merge(&hosts, true);
        let labels: Vec<&str> = merged.iter().map(|a| a.host.as_str()).collect();
        assert_eq!(labels, ["elitebook", "elitebook Rust"]);
        let hidden = merge(&hosts, false);
        assert!(hidden.iter().all(|a| a.host.is_empty()), "badges off: no row names its Host");
        assert_eq!(hidden[1].session, "@two/nova", "but the chats are still each Host's");
        assert_eq!(titled("Nova", &merged[1].host), "Nova · elitebook Rust");
        assert_eq!(titled("Rachel", ""), "Rachel");
    }

    #[test]
    fn a_moved_agent_says_where_it_lives_now() {
        let mut mochi = agent("mochi", 1);
        assert_eq!(moved_line(&mochi), "", "not moved: no line");
        mochi.moved_to = "rust-1".into();
        assert_eq!(moved_line(&mochi), "Moved to rust-1", "a destination without a name: its id");
        mochi.moved_to_host = "elitebook Rust".into();
        assert_eq!(moved_line(&mochi), "Moved to elitebook Rust");
        let parsed = Agent::from_json(
            serde_json::json!({"session": "mochi", "moved": {"to": {"host": "elitebook Rust", "id": "rust-1"}, "at": "2026-11-22T09:00:00Z"}})
                .as_object()
                .unwrap(),
        );
        assert_eq!((parsed.moved_to.as_str(), parsed.moved_to_host.as_str()), ("rust-1", "elitebook Rust"));
        assert_eq!(moved_line(&parsed), "Moved to elitebook Rust");
        assert_eq!(moved_line(&Agent::from_json(serde_json::json!({"session": "x", "moved": null}).as_object().unwrap())), "");
    }

    #[test]
    fn a_peers_agent_ids_are_qualified_so_helpers_nest_under_their_own_host() {
        let primary = [agent("lead", 5)];
        let mut helper = agent("helper", 4);
        helper.role = "helper".into();
        helper.parent_agent_id = "lead-id".into();
        let peer = [agent("lead", 3), helper];
        let merged = merge(&[
            HostRoster { server_id: "one", label: "a", agents: &primary, reachable: true },
            HostRoster { server_id: "two", label: "b", agents: &peer, reachable: true },
        ], true);
        let helper = merged.iter().find(|a| a.session == "@two/helper").unwrap();
        assert_eq!(helper.parent_agent_id, "@two/lead-id");
        assert_eq!(merged.iter().find(|a| a.session == "@two/lead").unwrap().agent_id, "@two/lead-id");
        assert_eq!(merged.iter().find(|a| a.session == "lead").unwrap().agent_id, "lead-id", "the primary's ids stay");
    }

    #[test]
    fn a_moved_row_is_left_out_only_when_its_destination_is_listed_by_server_id() {
        let mut moved_here = agent("rachel", 9);
        moved_here.moved_to = "one".into();
        let mut moved_away = agent("wanderer", 8);
        moved_away.moved_to = "gone".into();
        let mut named_only = agent("named", 7);
        // A Host's name is never a match: only its server id.
        named_only.moved_to = "elitebook".into();
        let primary = [agent("rachel", 1)];
        let peer = [moved_here, moved_away, named_only];
        let merged = merge(&[
            HostRoster { server_id: "one", label: "elitebook", agents: &primary, reachable: true },
            HostRoster { server_id: "two", label: "laptopstudio", agents: &peer, reachable: true },
        ], true);
        assert_eq!(sessions(&merged), ["@two/wanderer", "@two/named", "rachel"]);
        // The destination not in the list: the moved row shows, on one Host.
        let alone = merge(&[HostRoster { server_id: "two", label: "laptopstudio", agents: &peer, reachable: true }], true);
        assert_eq!(alone.len(), 3);
    }

    #[test]
    fn a_primary_row_that_moved_to_a_listed_peer_is_left_out_too() {
        let mut moved = agent("theo", 9);
        moved.moved_to = "two".into();
        let primary = [moved, agent("rachel", 1)];
        let peer = [agent("theo", 5)];
        let merged = merge(&[
            HostRoster { server_id: "one", label: "a", agents: &primary, reachable: true },
            HostRoster { server_id: "two", label: "b", agents: &peer, reachable: true },
        ], true);
        assert_eq!(sessions(&merged), ["@two/theo", "rachel"]);
    }

    #[test]
    fn a_badge_names_where_the_agent_lives() {
        let rachel = agent("rachel", 1);
        let mut mochi = agent("mochi", 1);
        mochi.moved_to = "rust-1".into();
        mochi.moved_to_host = "elitebook Rust".into();
        // One Host listed: its name, or none with badges hidden.
        assert_eq!(badge(&rachel, "elitebook", &["py-1"], false, true), "elitebook");
        assert_eq!(badge(&rachel, "elitebook", &["py-1"], false, false), "");
        // Moved to a Host not in the list: that Host is where it lives.
        assert_eq!(badge(&mochi, "elitebook", &["py-1"], false, true), "elitebook Rust");
        // Several Hosts: always shown, whatever the setting says.
        assert_eq!(badge(&rachel, "elitebook", &["py-1", "two"], true, false), "elitebook");
        let mut no_name = mochi.clone();
        no_name.moved_to_host.clear();
        assert_eq!(badge(&no_name, "elitebook", &["py-1"], false, true), "elitebook", "a destination without a name: its own Host");
        // In a merged list, a row moved to an unlisted Host names that Host.
        let primary = [rachel.clone()];
        let peer = [mochi];
        let merged = merge(&[
            HostRoster { server_id: "py-1", label: "elitebook", agents: &primary, reachable: true },
            HostRoster { server_id: "two", label: "laptopstudio", agents: &peer, reachable: true },
        ], true);
        assert_eq!(merged.iter().find(|a| a.session == "@two/mochi").unwrap().host, "elitebook Rust");
    }

    #[test]
    fn an_unreachable_hosts_rows_stay_but_show_offline() {
        let mut busy = agent("nova", 3);
        busy.busy = true;
        busy.alive = true;
        busy.latest_state = "thinking".into();
        busy.status_text = "reading".into();
        let primary = [agent("rachel", 1)];
        let peer = [busy];
        let merged = merge(&[
            HostRoster { server_id: "one", label: "a", agents: &primary, reachable: true },
            HostRoster { server_id: "two", label: "b", agents: &peer, reachable: false },
        ], true);
        let nova = &merged[0];
        assert_eq!((nova.session.as_str(), nova.latest_state.as_str(), nova.busy, nova.alive), ("@two/nova", "offline", false, false));
        assert!(nova.status_text.is_empty());
        assert_eq!(unreachable_note(&["b"]), "b unreachable");
        assert_eq!(unreachable_note(&["b", "c"]), "b, c unreachable");
        assert_eq!(unreachable_note(&[]), "");
    }
}
