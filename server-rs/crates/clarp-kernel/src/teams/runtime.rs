//! The per-turn team brief and the rules for autonomous leader work.

use std::collections::BTreeMap;
use std::sync::LazyLock;

pub use clarp_speech::markup::strip_team_blocks;
use regex::Regex;
use sha1::{Digest, Sha1};

use super::{Delivery, Message, Team, Teams};
use crate::python;

pub const LEADER_TICK: &str = "leader_tick";
pub const NUDGE_DISABLED: &str = "team leader nudging is disabled";
pub const INTERVAL_SEC: u64 = 600;
pub const TICK_PROMPT: &str = "[Automated team check] Review your team's status (members and their current state are in your brief). Follow Leader Standing Orders v2: decide what matters, delegate execution to workers, track delegated runs, and capture any genuine user judgment calls. If a teammate is stalled, blocked, or waiting, prompt them with the next bounded task using the self-prompt skill with --from your own session so it comes from you. If there is no valuable action, keep it quiet: reply only with LEADER_NOOP. Do not implement the work yourself and do not duplicate work teammates are already doing.";
const NOOP: &str = "LEADER_NOOP";
pub const NOTHING_TO_DO: &str = "Leader check: no action needed.";
const OPEN: &str = "--- Clarp team context ---";
const CLOSE: &str = "--- End Clarp team context ---";
const DIGEST_LIMIT: usize = 5;

pub type Deliveries = BTreeMap<String, BTreeMap<String, Delivery>>;

pub struct Person {
    pub name: String,
    pub session: String,
    pub state: String,
}

#[derive(Default)]
pub struct Brief {
    pub protocol: String,
    pub digest: String,
    /// Every unread id in the snapshot, including those too old to quote in the brief.
    pub ids: Vec<String>,
}

impl Brief {
    pub fn prompt(&self, text: &str) -> String {
        if self.protocol.is_empty() && self.digest.is_empty() {
            return text.to_owned();
        }
        let context = match (self.protocol.is_empty(), self.digest.is_empty()) {
            (false, false) => format!("{}\n\n{}", self.protocol, self.digest),
            (false, true) => self.protocol.clone(),
            _ => self.digest.clone(),
        };
        format!("{OPEN}\n{context}\n{CLOSE}\n\n{text}")
    }
}

/// Provider-only context may appear in a tool's native history. It never belongs in a chat.
pub fn strip_prompt_context(text: &str) -> Option<String> {
    let (prefix, mut tail) = text.split_once(OPEN)?;
    let (_, after) = tail.split_once(CLOSE)?;
    let mut visible = prefix.to_owned();
    tail = after;
    while let Some((before, rest)) = tail.split_once(OPEN) {
        let Some((_, after)) = rest.split_once(CLOSE) else {
            break;
        };
        visible.push_str(before);
        tail = after;
    }
    visible.push_str(tail);
    Some(python::strip(&visible).into())
}

pub fn routine(origin: &str) -> bool {
    matches!(origin, "heartbeat" | LEADER_TICK | "dreaming" | "janitor")
}

pub fn nudge_allowed(teams: &Teams, agent: &str) -> bool {
    teams.values().any(|team| may_nudge(team, agent))
}

pub fn may_nudge(team: &Team, agent: &str) -> bool {
    !agent.is_empty()
        && team.archived_at.is_none()
        && team.leader_enabled
        && team.nudge_enabled
        && team.leader_agent_id == agent
}

/// The reason an otherwise idle and authorized leader has something to act on.
pub fn wake_reason(stalled: usize, unread: bool) -> Option<String> {
    match (stalled, unread) {
        (0, false) => None,
        (0, true) => Some("new team activity".into()),
        (n, false) => Some(format!("{n} stalled")),
        (n, true) => Some(format!("{n} stalled, new team activity")),
    }
}

pub fn has_unread(teams: &Teams, deliveries: &Deliveries, agent: &str) -> bool {
    active_for(teams, agent)
        .filter(|team| team.communication_enabled)
        .flat_map(|team| {
            deliveries
                .get(&team.team_id)
                .into_iter()
                .flat_map(BTreeMap::values)
        })
        .any(|row| row.inbox.get(agent) == Some(&None))
}

fn active_for<'a>(teams: &'a Teams, agent: &'a str) -> impl Iterator<Item = &'a Team> {
    teams
        .values()
        .filter(|team| team.archived_at.is_none() && team.has(agent))
}

pub fn pending(
    teams: &Teams,
    deliveries: &Deliveries,
    agent: &str,
    person: &impl Fn(&str) -> Option<Person>,
) -> (String, Vec<String>) {
    let mut unread: Vec<_> = active_for(teams, agent)
        .filter(|team| team.communication_enabled)
        .flat_map(|team| {
            deliveries
                .get(&team.team_id)
                .into_iter()
                .flat_map(BTreeMap::values)
                .filter(|row| row.inbox.get(agent) == Some(&None))
                .map(move |row| (team, row))
        })
        .collect();
    if unread.is_empty() {
        return (String::new(), Vec::new());
    }
    unread.sort_by(|(_, left), (_, right)| {
        right
            .message
            .created_at
            .cmp(&left.message.created_at)
            .then_with(|| right.order.cmp(&left.order))
            .then_with(|| {
                right
                    .message
                    .team_message_id
                    .cmp(&left.message.team_message_id)
            })
    });
    let ids = unread
        .iter()
        .map(|(_, row)| row.message.team_message_id.clone())
        .collect();
    let mut lines = vec!["Team updates since your last turn:".to_owned()];
    let skipped = unread.len().saturating_sub(DIGEST_LIMIT);
    if skipped != 0 {
        let plural = if skipped == 1 { "" } else { "s" };
        lines.push(format!(
            "(+{skipped} earlier update{plural} skipped — showing the {DIGEST_LIMIT} most recent)"
        ));
    }
    for (team, row) in unread.into_iter().take(DIGEST_LIMIT).rev() {
        let msg = &row.message;
        let source = person(&msg.source_agent_id)
            .map(|p| p.name)
            .filter(|name| !name.is_empty())
            .unwrap_or_else(|| "A teammate".into());
        lines.push(format!("- [{}] {source}: {}", team.name, msg.text));
    }
    lines.push(String::new());
    lines.push("This is background awareness. Keep broadcasting your OWN progress with <team> as you work — that's expected. Just don't get pulled into back-and-forth replies to teammates unless the user asks or it directly affects your current task.".into());
    (lines.join("\n"), ids)
}

fn member_lines(
    team: &Team,
    agent: &str,
    indent: &str,
    person: &impl Fn(&str) -> Option<Person>,
) -> Vec<String> {
    team.members
        .iter()
        .filter(|member| member.agent_id != agent)
        .map(|member| {
            let p = person(&member.agent_id);
            let name = p
                .as_ref()
                .map(|p| p.name.as_str())
                .filter(|s| !s.is_empty())
                .unwrap_or(&member.agent_id);
            let session = p
                .as_ref()
                .map(|p| p.session.as_str())
                .filter(|s| !s.is_empty())
                .unwrap_or("?");
            let state = p
                .as_ref()
                .map(|p| p.state.as_str())
                .filter(|s| !s.is_empty())
                .unwrap_or("unknown");
            format!("{indent}- {name} ({session}): {state}")
        })
        .collect()
}

pub fn protocol(
    teams: &Teams,
    agent: &str,
    session: &str,
    origin: &str,
    person: &impl Fn(&str) -> Option<Person>,
) -> String {
    let mut active: Vec<_> = active_for(teams, agent).collect();
    active.sort_by(|a, b| a.name.cmp(&b.name));
    let led: Vec<_> = active
        .iter()
        .copied()
        .filter(|t| t.leader_enabled && t.leader_agent_id == agent)
        .collect();
    if !led.is_empty() && matches!(origin, "heartbeat" | LEADER_TICK) {
        let noop = if origin == "heartbeat" {
            "HEARTBEAT_OK"
        } else {
            NOOP
        };
        let mut lines = vec![format!(
            "You lead this team; decide/delegate/track. Reply {noop} if nothing needs action."
        )];
        for team in led {
            lines.push(format!("Team '{}' live member states:", team.name));
            lines.extend(member_lines(team, agent, "", person));
        }
        return lines.join("\n");
    }

    let mut parts = Vec::new();
    let mut speaking = active
        .into_iter()
        .filter(|t| t.communication_enabled)
        .peekable();
    if speaking.peek().is_some() {
        let speaking: Vec<_> = speaking.collect();
        let names: Vec<_> = speaking.iter().map(|t| t.name.as_str()).collect();
        parts.push(format!("Team feed: you are on {}. Share useful coordination as <team>short first-person update</team>; it is not spoken. Broadcast starts, finishes, blockers, handoffs, and decisions that affect others. Keep it short; skip routine noise. Recent team updates are injected below when present.", names.join(", ")));
        if let Some(leader) = speaking
            .into_iter()
            .find(|t| {
                t.leader_enabled && !t.leader_agent_id.is_empty() && t.leader_agent_id != agent
            })
            .and_then(|t| person(&t.leader_agent_id))
            .filter(|p| !p.session.is_empty())
        {
            let me = if session.is_empty() {
                "<your_session>"
            } else {
                session
            };
            let name = if leader.name.is_empty() {
                &leader.session
            } else {
                &leader.name
            };
            parts.push(format!("- Report TERMINAL results directly to leader {name} via self-prompt --from {me} --to {} (done+proof, blocked+need, or failed). Use <team> for progress, handoffs, file touches, and awareness; if a finish affects teammates, do both. Rule: leader needs to act -> direct; awareness -> <team>.", leader.session));
        }
    }
    if !led.is_empty() {
        let mut lines = vec![
            "Leader role: decide, delegate, track, and learn; workers execute.".into(),
            format!(
                "Delegate as yourself with self-prompt --from {session}. Track owner, objective, proof, risk, time budget, and terminal result."
            ),
            include_str!("leader-standing-orders.txt").trim_end().into(),
        ];
        for team in led {
            let states = member_lines(team, agent, "  ", person);
            if !states.is_empty() {
                lines.push(format!(
                    "Team '{}' members and their current state:",
                    team.name
                ));
                lines.extend(states);
            }
        }
        parts.push(lines.join("\n"));
    }
    parts.join("\n\n")
}

#[allow(clippy::expect_used)]
fn pattern(text: &str) -> Regex {
    Regex::new(text).expect("a fixed pattern of the Teams runtime compiles")
}

static BLOCK: LazyLock<Regex> = LazyLock::new(|| pattern(r"(?is)<team\b[^>]*>(.*?)</team>"));
static TAG: LazyLock<Regex> = LazyLock::new(|| pattern(r"<[^>]+>"));
static NOOP_EDGE: LazyLock<Regex> =
    LazyLock::new(|| pattern(r"(?:^LEADER_NOOP\W*|\W*LEADER_NOOP\W*$)"));

pub fn blocks(text: &str) -> Vec<String> {
    BLOCK
        .captures_iter(text)
        .filter_map(|block| {
            let clean = clarp_speech::markup::clean_for_display(&block[1], true);
            let clean = TAG.replace_all(&clean, " ");
            let words = clean
                .split(python::blank)
                .filter(|word| !word.is_empty())
                .collect::<Vec<_>>()
                .join(" ");
            (!words.is_empty()).then_some(words)
        })
        .collect()
}

/// A sole no-op or a short edge acknowledgement stays out of the conversation.
pub fn leader_reply(text: &str) -> Option<String> {
    let trimmed = python::strip(text);
    if !trimmed.starts_with(NOOP) && !trimmed.ends_with(NOOP) {
        return Some(text.to_owned());
    }
    let stripped = NOOP_EDGE.replace_all(trimmed, "");
    let remaining = stripped
        .split(python::blank)
        .filter(|word| !word.is_empty())
        .collect::<Vec<_>>()
        .join(" ");
    (remaining.chars().count() > 300).then_some(remaining)
}

pub fn fanout(
    teams: &Teams,
    agent: &str,
    reply: &str,
    trace: &str,
    blocks: &[String],
    at: i64,
) -> Vec<Delivery> {
    active_for(teams, agent)
        .filter(|team| team.communication_enabled)
        .flat_map(|team| {
            blocks.iter().map(move |text| {
                let hash = format!(
                    "{:x}",
                    Sha1::digest(format!("{}\0{reply}\0{text}", team.team_id))
                );
                let message = Message {
                    team_message_id: format!("tm-{}", &hash[..20]),
                    team_id: team.team_id.clone(),
                    source_agent_id: agent.into(),
                    source_message_id: reply.into(),
                    trace_id: trace.into(),
                    text: text.clone(),
                    created_at: at,
                };
                let inbox = team
                    .members
                    .iter()
                    .filter(|member| member.agent_id != agent)
                    .map(|member| (member.agent_id.clone(), None))
                    .collect();
                Delivery {
                    order: 0,
                    message,
                    inbox,
                }
            })
        })
        .collect()
}

pub fn latest_activity(
    teams: &Teams,
    deliveries: &Deliveries,
    agent: &str,
    state: impl Fn(&str) -> i64,
) -> i64 {
    active_for(teams, agent)
        .filter(|team| team.communication_enabled)
        .map(|team| {
            let feed = deliveries
                .get(&team.team_id)
                .into_iter()
                .flat_map(BTreeMap::values)
                .map(|row| {
                    row.inbox
                        .get(agent)
                        .copied()
                        .flatten()
                        .unwrap_or(0)
                        .max(row.message.created_at)
                })
                .max()
                .unwrap_or(0);
            let peers = team
                .members
                .iter()
                .filter(|peer| peer.agent_id != agent)
                .map(|peer| state(&peer.agent_id))
                .max()
                .unwrap_or(0);
            feed.max(peers)
        })
        .max()
        .unwrap_or(0)
}
