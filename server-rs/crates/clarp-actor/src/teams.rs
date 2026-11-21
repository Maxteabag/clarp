//! The agent journal is the source of truth for work owed to the shared Teams store.
use clarp_kernel::conversation::Reply;
use clarp_kernel::journal::{Batch, kind, team_transit::Effect};
use clarp_kernel::teams::runtime::{self, Brief};
use clarp_ports::log::Kinds;

use crate::{Actor, ActorError, NewItem, WINDOW, record_item};

const CURSOR: &str = "team-effect-through";

/// The Host's team store is shared by all actors and by the management routes.
pub trait Runtime: Send + Sync {
    fn brief(&self, agent: &str, session: &str, origin: &str) -> Brief;
    fn apply(&self, agent: &str, effect: &Effect, at: i64) -> Result<(), String>;
}

pub(crate) fn capture(batch: &mut Batch<'_>, reply: &Reply, at: i64) -> Result<(), ActorError> {
    if !reply.text.contains('<') {
        return Ok(());
    }
    let blocks = runtime::blocks(&reply.text);
    if blocks.is_empty() {
        return Ok(());
    }
    let effect = Effect::Reply {
        id: reply.id.clone(),
        trace: reply.trace_id.clone().unwrap_or_default(),
        blocks,
    };
    batch.item(record_item(
        kind::TEAM_EFFECT,
        format!("team-reply-{}", reply.id),
        &effect,
        at,
    )?);
    Ok(())
}

pub(crate) fn record_noop(batch: &mut Batch<'_>, reply: &str, now: i64) -> Result<(), ActorError> {
    let key = format!("team-noop-{reply}");
    if batch.log().find(&key)?.is_none() {
        batch.item(record_item(
            kind::TEAM_EFFECT,
            key,
            &Effect::LeaderNoop,
            now,
        )?);
    }
    Ok(())
}

impl Actor {
    pub fn is_leader_tick(&self) -> bool {
        self.live
            .as_ref()
            .is_some_and(|live| live.origin.name() == runtime::LEADER_TICK)
    }

    pub fn refuse_leader_launch(
        &mut self,
        turn: clarp_kernel::turn::TurnId,
        now: i64,
    ) -> Result<crate::Outputs, ActorError> {
        use clarp_kernel::{agent::Command, turn::RunOutcome};
        self.ended_outside_the_spool(
            turn,
            Command::TurnEnded {
                turn,
                outcome: RunOutcome::Interrupted {
                    reason: runtime::NUDGE_DISABLED.into(),
                },
            },
            now,
        )
    }

    pub(crate) fn flush_teams(&mut self) -> Result<(), ActorError> {
        let Some(teams) = self.teams.as_ref() else {
            return Ok(());
        };
        let mut through = self.team_effect_through;
        if through >= self.recorder.team_effect_seq() {
            return Ok(());
        }
        loop {
            let page =
                self.recorder
                    .log()
                    .read_after(through, Kinds::only(kind::TEAM_EFFECT), WINDOW)?;
            let Some(last) = page.last() else {
                break;
            };
            through = last.seq;
            for row in page {
                let effect = serde_json::from_slice(&row.body).map_err(|why| {
                    ActorError::Corrupt(format!("team effect at {}: {why}", row.seq))
                })?;
                teams
                    .apply(&self.profile.agent_id, &effect, row.ts_ms)
                    .map_err(ActorError::Io)?;
            }
        }
        if through != self.team_effect_through {
            let mut batch = self.recorder.batch();
            let body = through.to_le_bytes().to_vec();
            batch.item(NewItem::new(kind::TEAM_CURSOR, 0, body).keyed(CURSOR));
            batch.commit()?;
            self.team_effect_through = through;
        }
        Ok(())
    }

    pub(crate) fn open_team_effects(&mut self) -> Result<(), ActorError> {
        if self.teams.is_none() {
            return Ok(());
        }
        self.team_effect_through = self.recorder.log().find(CURSOR)?.map_or(Ok(0), |item| {
            item.body
                .as_slice()
                .try_into()
                .map(u64::from_le_bytes)
                .map_err(|_| ActorError::Corrupt("invalid team effect cursor".to_owned()))
        })?;
        self.flush_teams()
    }
}
