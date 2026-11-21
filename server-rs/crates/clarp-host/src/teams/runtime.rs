//! The Teams desk at the turn boundary, and the Host's idle leader checks.
use std::collections::HashSet;
use std::sync::Arc;
use std::time::Duration;

use clarp_kernel::{
    journal::team_transit::Effect,
    teams::runtime::{self, Brief, INTERVAL_SEC, LEADER_TICK, Person, TICK_PROMPT},
};
use clarp_protocol::requests::SendRequest;

use super::Desk;
use crate::{
    edge::{self, App},
    fleet::{Directory, Member},
    hub::Hub,
    wire,
};

pub struct TurnTeams {
    pub desk: Arc<Desk>,
    pub hub: Arc<Hub>,
}

fn person(directory: Option<&Directory>, agent: &str) -> Option<Person> {
    let member = directory?.named(agent)?;
    Some(Person {
        name: member.record.persona.clone(),
        session: member.record.session.clone(),
        state: wire::state_name(&wire::state_kind(&member.agent.view().status)),
    })
}

impl TurnTeams {
    pub(crate) fn nudge_allowed(&self, agent: &str) -> bool {
        self.desk
            .read(|teams, _| runtime::nudge_allowed(teams, agent))
    }
}

impl clarp_actor::teams::Runtime for TurnTeams {
    fn brief(&self, agent: &str, session: &str, origin: &str) -> Brief {
        let directory = self.hub.queue_directory();
        let person = |agent: &str| person(directory.as_deref(), agent);
        self.desk.read(|teams, deliveries| {
            let (digest, ids) = runtime::pending(teams, deliveries, agent, &person);
            Brief {
                protocol: runtime::protocol(teams, agent, session, origin, &person),
                digest,
                ids,
            }
        })
    }

    fn apply(&self, agent: &str, effect: &Effect, at: i64) -> Result<(), String> {
        match effect {
            Effect::LeaderNoop => {
                eprintln!("clarp-host: teamLeaderNoop agent={agent}");
                Ok(())
            }
            Effect::Reply { id, trace, blocks } => self
                .desk
                .deliver(|teams| runtime::fanout(teams, agent, id, trace, blocks, at))
                .map(drop),
            Effect::Injected { ids } => self.desk.mark_injected(agent, ids, at),
        }
        .map_err(|why| why.to_string())
    }
}

pub fn latest_activity(app: &App, target: &str) -> i64 {
    let directory = app.fleet.directory();
    app.teams.read(|teams, feed| {
        runtime::latest_activity(teams, feed, target, |id| {
            directory
                .named(id)
                .map_or(0, |member| member.agent.view().team_activity_ms)
        })
    })
}

fn idle(leader: &Member) -> bool {
    let view = leader.agent.view();
    view.status.moved.is_none()
        && !view.status.paused
        && view.status.live.is_none()
        && view.status.waiting.is_empty()
        && view.broken.is_none()
        && leader.record.archived_at_ms.is_none()
        && !leader.record.is_janitor
}

async fn tick(app: &App, quiet_ms: i64) {
    let directory = app.fleet.directory();
    let now = app.clock.now_ms();
    let who = |id: &str| person(Some(&directory), id);
    let leaders = app.teams.read(|teams, feed| {
        let mut seen = HashSet::new();
        teams
            .values()
            .filter_map(|team| {
                let id = &team.leader_agent_id;
                let leader = directory.named(id)?;
                if !runtime::may_nudge(team, id) || !idle(leader) || seen.contains(id) {
                    return None;
                }
                let last = leader.agent.view().last_real_message_ms;
                let age = now.saturating_sub(last);
                if quiet_ms > 0 && last != 0 && age < quiet_ms {
                    let age_sec = age.max(0) / 1000;
                    eprintln!(
                        "clarp-host: teamLeaderSkip agent={id} reason=recent-activity:{age_sec}s"
                    );
                    return None;
                }
                let peers: Vec<_> = team
                    .members
                    .iter()
                    .filter(|member| &member.agent_id != id)
                    .collect();
                if peers.is_empty() {
                    return None;
                }
                let stalled = peers
                    .iter()
                    .filter(|peer| {
                        who(&peer.agent_id)
                            .is_some_and(|p| matches!(p.state.as_str(), "interrupted" | "waiting"))
                    })
                    .count();
                let reason = runtime::wake_reason(stalled, runtime::has_unread(teams, feed, id))?;
                seen.insert(id.clone());
                Some((leader.record.session.clone(), reason))
            })
            .collect::<Vec<_>>()
    });
    for (session, reason) in leaders {
        let request = SendRequest {
            session,
            text: TICK_PROMPT.into(),
            origin: Some(LEADER_TICK.into()),
            synthesize_audio: Some(false),
            force_session: Some(true),
            queue_if_busy: Some(true),
            ..SendRequest::default()
        };
        let session = request.session.clone();
        match edge::send_own(app, request).await {
            Ok(_) => eprintln!("clarp-host: teamLeaderTick session={session} ({reason})"),
            Err(why) => eprintln!(
                "clarp-host: teamLeaderTickFail session={session}: {}",
                why.message()
            ),
        }
    }
}

pub fn run(app: &App) {
    let app = app.clone();
    let quiet = std::env::var("CLAUDE_PWA_TEAM_LEADER_QUIET_PERIOD_SEC")
        .ok()
        .and_then(|raw| raw.trim().parse::<i64>().ok())
        .unwrap_or(INTERVAL_SEC as i64)
        .max(0);
    tokio::spawn(async move {
        tokio::time::sleep(Duration::from_secs(60)).await;
        let mut interval = tokio::time::interval(Duration::from_secs(INTERVAL_SEC));
        interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        loop {
            interval.tick().await;
            tick(&app, quiet.saturating_mul(1_000)).await;
        }
    });
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;
    use clarp_actor::conversation::{Versioned, Which};
    use clarp_protocol::requests::CreateAgentRequest;
    use serde_json::{Value, json};

    async fn add(app: &App, name: &str) -> crate::fleet::AgentRecord {
        let request: CreateAgentRequest = serde_json::from_value(json!({
            "name": name, "backend": "scripted"
        }))
        .unwrap();
        app.fleet.create(request).await.unwrap().ok().unwrap()
    }

    async fn rows(app: &App, session: &str) -> Vec<Versioned> {
        app.member(session)
            .unwrap()
            .agent
            .tail(10, Which::All)
            .await
            .unwrap()
            .entries
    }

    #[tokio::test]
    async fn teams_runtime_dispatches_an_authorized_idle_leader_for_new_work_once() {
        let place = crate::unkept::at_the_port::place("team-leader-dispatch");
        let app = place.app().await;
        let leader = add(&app, "Lead").await;
        let member = add(&app, "Worker").await;
        let outsider = add(&app, "Solo").await;
        let at = app.clock.now_ms();
        let create = json!({"name":"Ops"});
        let team = app
            .teams
            .create("team-ops".into(), create.as_object().unwrap(), at)
            .unwrap();
        let id = team["team_id"].as_str().unwrap();
        for agent in [&leader, &member] {
            app.teams.add(id, agent.agent_id.clone(), at).unwrap();
        }
        let change = json!({"leader":leader.agent_id,"communication_enabled":true});
        app.teams
            .change(id, change.as_object().unwrap(), at)
            .unwrap();

        let requested = |session: &str| SendRequest {
            session: session.into(),
            text: "Untrusted leader check".into(),
            origin: Some(LEADER_TICK.into()),
            synthesize_audio: Some(false),
            force_session: Some(true),
            ..SendRequest::default()
        };
        let outside = edge::send_message(&app, requested(&outsider.session))
            .await
            .unwrap();
        assert_eq!(outside.session, outsider.session);
        assert_eq!(
            rows(&app, &outsider.session).await[0].entry.origin_name(),
            "user"
        );
        let refused = edge::send_own(&app, requested(&leader.session))
            .await
            .unwrap_err();
        assert_eq!(refused.status(), axum::http::StatusCode::CONFLICT);
        assert_eq!(refused.message(), runtime::NUDGE_DISABLED);

        app.teams.nudge(id, true, at).unwrap();
        tick(&app, 0).await;
        assert!(rows(&app, &leader.session).await.is_empty());
        app.teams
            .deliver(|teams| {
                runtime::fanout(
                    teams,
                    &member.agent_id,
                    "reply",
                    "trace",
                    &["A handoff".to_owned()],
                    at,
                )
            })
            .unwrap();
        app.teams.nudge(id, false, at).unwrap();
        tick(&app, 0).await;
        assert!(rows(&app, &leader.session).await.is_empty());
        app.teams.nudge(id, true, at).unwrap();
        tick(&app, 0).await;
        tick(&app, 0).await;
        let turns = rows(&app, &leader.session).await;
        assert_eq!(turns.len(), 1);
        assert_eq!(turns[0].entry.origin_name(), LEADER_TICK);
        assert!(
            matches!(&turns[0].entry, clarp_kernel::conversation::Entry::Message(message)
            if message.text == TICK_PROMPT && !message.speak)
        );
        assert!(
            app.teams
                .read(|teams, rows| runtime::has_unread(teams, rows, &leader.agent_id))
        );
        assert_ne!(
            app.teams.read(|teams, rows| runtime::latest_activity(
                teams,
                rows,
                &leader.agent_id,
                |_| 0
            )),
            0
        );
        assert_eq!(team["archived_at"], Value::Null);
    }
}
