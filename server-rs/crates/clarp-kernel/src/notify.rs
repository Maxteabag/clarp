//! Whether a turn that ended tells the user.
//!
//! The rule is small on purpose: a turn tells the user when it ended with something meant
//! for the user. Telling is a badge and an unread mark; a push follows unless the agent is
//! muted or the user sat at the desk when asking. What agents say to each other, and what
//! the Host does on a schedule, stays in the conversation and tells nobody.
//!
//! These are the Python Host's rules (`server/lib/policies/notifications.py`), decision for
//! decision and reason for reason: the reasons are what the apps and the logs name them by.

/// The origins of turns that are talk between agents or routine: never told to the user.
const AGENT_CHATTER: [&str; 4] = ["agent", "schedule", "heartbeat", "dreaming"];
/// The origins that may tell the user when automation is treated specially.
const USER_FACING: [&str; 3] = ["user", "leader_tick", "watcher"];
/// What a turn can end with that is meant for the user: something said aloud, or a written
/// reply.
const NOTIFYING: [&str; 2] = ["speak", "text-reply"];
/// The app a message names itself as when it was sent from the desk.
const DESKTOP: &str = "desktop";

/// The turn that ended, as far as the rule looks at it.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct Completed<'a> {
    pub agent_id: &'a str,
    /// The message that began the turn is known.
    pub has_cause: bool,
    /// What the turn ended with: `speak`, `text-reply`, or nothing that addressed the user.
    pub content: &'a str,
    /// The turn was cut off before it could say anything.
    pub interrupted: bool,
    /// The app that sent the message that began it, when it said.
    pub client: &'a str,
}

/// The agent whose turn it was.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Capabilities {
    /// The Host has the agent.
    pub present: bool,
    pub can_chat: bool,
    /// The user muted it, or it is a helper, which reports to the agent that made it.
    pub muted: bool,
    pub is_team_leader: bool,
    /// An agent that does maintenance and never speaks to the user.
    pub quiet: bool,
}

impl Default for Capabilities {
    fn default() -> Self {
        Self {
            present: true,
            can_chat: true,
            muted: false,
            is_team_leader: false,
            quiet: false,
        }
    }
}

/// What the rule decides.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Decision {
    /// The user is told: a badge and an unread mark, and a push when `push` is set.
    Notify {
        reason: String,
        kind: String,
        push: bool,
        muted: bool,
    },
    /// Nobody is told. The reason is kept.
    Suppress { reason: String },
}

fn suppress(reason: impl Into<String>) -> Decision {
    Decision::Suppress {
        reason: reason.into(),
    }
}

/// Decides whether a turn that ended tells the user. `special_automation` is the owner's
/// setting that only what a person or a team's leader began may tell.
pub fn decide(
    origin: &str,
    turn: &Completed<'_>,
    agent: &Capabilities,
    special_automation: bool,
) -> Decision {
    let origin = origin.trim();
    if origin == "janitor" || agent.quiet || (agent.present && !agent.can_chat) {
        return suppress("janitor-maintenance");
    }
    if turn.agent_id.is_empty() {
        return suppress("missing-agent");
    }
    if !turn.has_cause {
        return suppress("missing-causing-row");
    }
    if AGENT_CHATTER.contains(&origin) {
        return suppress(format!("agent-chatter:{origin}"));
    }
    if special_automation && !USER_FACING.contains(&origin) {
        let named = if origin.is_empty() { "unknown" } else { origin };
        return suppress(format!("not-user-facing-origin:{named}"));
    }
    if special_automation && origin == "leader_tick" && !agent.is_team_leader {
        return suppress("leader-tick-non-leader");
    }
    let content = turn.content;
    if NOTIFYING.contains(&content) {
        let (suffix, push) = if agent.muted {
            ("-muted", false)
        } else if turn.client == DESKTOP {
            ("-desktop", false)
        } else {
            ("", true)
        };
        return Decision::Notify {
            reason: format!("{content}{suffix}"),
            kind: content.to_string(),
            push,
            muted: agent.muted,
        };
    }
    if !content.is_empty() {
        // A kind of content nobody knows is kept and tells no one.
        return suppress(content);
    }
    if turn.interrupted {
        // Cut off before it could say anything: not the same as having nothing to say.
        return suppress("turn-interrupted");
    }
    suppress("no-user-facing-content")
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    fn turn<'a>(content: &'a str, client: &'a str) -> Completed<'a> {
        Completed {
            agent_id: "a1",
            has_cause: true,
            content,
            interrupted: false,
            client,
        }
    }

    #[test]
    fn a_reply_to_the_user_is_told_and_talk_between_agents_is_not() {
        let agent = Capabilities::default();
        assert_eq!(
            decide("user", &turn("text-reply", ""), &agent, false),
            Decision::Notify {
                reason: "text-reply".to_string(),
                kind: "text-reply".to_string(),
                push: true,
                muted: false,
            }
        );
        // What the Host delivers by itself for the user (an answer, a goal's wake) is told.
        assert!(matches!(
            decide("automation", &turn("speak", ""), &agent, false),
            Decision::Notify { push: true, .. }
        ));
        assert_eq!(
            decide("agent", &turn("text-reply", ""), &agent, false),
            suppress("agent-chatter:agent")
        );
        assert_eq!(
            decide("user", &turn("", ""), &agent, false),
            suppress("no-user-facing-content")
        );
    }

    #[test]
    fn a_muted_agent_and_a_user_at_the_desk_get_no_push() {
        let muted = Capabilities {
            muted: true,
            ..Capabilities::default()
        };
        assert_eq!(
            decide("user", &turn("speak", "desktop"), &muted, false),
            Decision::Notify {
                reason: "speak-muted".to_string(),
                kind: "speak".to_string(),
                push: false,
                muted: true,
            }
        );
        assert_eq!(
            decide(
                "user",
                &turn("text-reply", "desktop"),
                &Capabilities::default(),
                false
            ),
            Decision::Notify {
                reason: "text-reply-desktop".to_string(),
                kind: "text-reply".to_string(),
                push: false,
                muted: false,
            }
        );
    }
}
