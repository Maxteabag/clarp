//! The books of what is said aloud.
//!
//! One of a Host's own janitors keeps them, and asks no model for it. The first time a clip
//! is seen in a stage (what its producer says of it: `producer:streaming`,
//! `producer:complete`, `producer:failed`; what was done with it for its listener:
//! `client:synthesized`, `client:broadcast`, `client:queued`, `client:play-ok` and the like)
//! that is written down, once for a clip and a stage ([`Seen::observe`]): a clip played a
//! second time is no new fact. A pass ([`Book::keep_audio`]) writes each such fact as a
//! completed run of the janitor that keeps the books for the clip's agent, when one is on
//! and has the agent in its scope. What nobody keeps the books for waits, and is written
//! down when somebody does.
//!
//! The rule is the Python Host's (`server/lib/audio_bookkeeper.py`, and the triggers of its
//! database that see a clip change), held step by step by
//! `tests/fixtures/audio-bookkeeper-vectors.json`
//! (`server-rs/tools/audio-bookkeeper-vectors.py`).

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};
use serde_json::json;

use super::book::{Book, World};
use super::demand::in_scope;
use super::runs::Run;

/// The job of the janitor that keeps these books.
pub const ROLE: &str = "audio-bookkeeper";
/// The trigger its runs are filed under.
const TRIGGER: &str = "audio-lifecycle-observed";
/// How many facts a pass writes down where it is not told, and at most.
pub const AT_A_TIME: usize = 32;
const AT_MOST: usize = 128;
const WRITTEN: &str = "Audio lifecycle fact recorded";

/// The stage a clip is in by what its producer says of it.
pub fn producer(says: &str) -> String {
    format!("producer:{says}")
}

/// The stage a clip is in by what was done with it for its listener.
pub fn client(status: &str) -> String {
    format!("client:{status}")
}

/// One thing seen of a clip: that it was in a stage, for the first time.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Fact {
    /// Its number in the order things were seen. Never given again.
    pub event_id: u64,
    pub clip_id: u64,
    /// The agent the clip is of.
    pub agent_id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub turn_id: Option<u64>,
    pub stage: String,
    pub created_at: i64,
    /// The run it was written down as, and when.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub run_id: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub completed_at: Option<i64>,
}

/// Everything that was seen of clips and is still kept in mind.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Seen {
    /// By the clip and the stage: each once.
    facts: BTreeMap<(u64, String), Fact>,
    /// The number the last fact was given.
    last: u64,
}

impl Seen {
    /// What was kept, and the last number that was given: no smaller than the largest kept.
    pub fn new(kept: impl IntoIterator<Item = Fact>, last: u64) -> Self {
        let facts = kept.into_iter();
        let facts: BTreeMap<(u64, String), Fact> = facts
            .map(|fact| ((fact.clip_id, fact.stage.clone()), fact))
            .collect();
        let largest = facts.values().map(|fact| fact.event_id).max().unwrap_or(0);
        Self {
            facts,
            last: last.max(largest),
        }
    }

    /// The number the last fact was given.
    pub fn last(&self) -> u64 {
        self.last
    }

    /// A clip is in a stage. The first time, that is written down, and answered. A stage
    /// that is seen again leaves nothing and takes no number (the Python Host's database
    /// gives one to a stage a clip comes back to, and so has gaps in its numbers).
    pub fn observe(
        &mut self,
        clip_id: u64,
        agent_id: &str,
        turn_id: Option<u64>,
        stage: &str,
        now_ms: i64,
    ) -> Option<&Fact> {
        let key = (clip_id, stage.to_string());
        if self.facts.contains_key(&key) {
            return None;
        }
        self.last += 1;
        let fact = Fact {
            event_id: self.last,
            clip_id,
            agent_id: agent_id.to_string(),
            turn_id,
            stage: stage.to_string(),
            created_at: now_ms,
            run_id: None,
            completed_at: None,
        };
        Some(self.facts.entry(key).or_insert(fact))
    }

    /// Everything that is kept in mind, in the order it was seen.
    pub fn all(&self) -> Vec<&Fact> {
        let mut all: Vec<&Fact> = self.facts.values().collect();
        all.sort_by_key(|fact| fact.event_id);
        all
    }

    /// Forgets what was seen before a time, written down or not. Answers what was
    /// forgotten. The numbers given so far stay given.
    pub fn forget_before(&mut self, cutoff_ms: i64) -> Vec<Fact> {
        let old = |fact: &Fact| fact.created_at < cutoff_ms;
        let gone: Vec<(u64, String)> = self
            .facts
            .iter()
            .filter(|(_, fact)| old(fact))
            .map(|(key, _)| key.clone())
            .collect();
        let forgotten = gone.iter().filter_map(|key| self.facts.remove(key));
        forgotten.collect()
    }
}

impl Book {
    /// Whether a bookkeeper that is on has this agent in its scope: whether what was seen
    /// of the agent's clips is somebody's to write down.
    pub fn audio_kept_for(&self, agent_id: &str, world: &dyn World) -> bool {
        let found = world.agent(agent_id);
        let Some(target) = found.filter(|agent| agent.agent_id == agent_id) else {
            return false;
        };
        let mut keepers = self.configs.values();
        keepers.any(|config| {
            config.template_id == ROLE && config.enabled && in_scope(&config.scope, &target)
        })
    }

    /// One pass over what was seen and not yet written down, the oldest first, `limit` at
    /// most (one at least, a hundred and twenty-eight at most): each fact whose agent a
    /// bookkeeper that is on has in its scope becomes a completed run of the janitor that
    /// keeps the books for that agent. What nobody keeps is passed over before the measure
    /// is taken, so that it does not hold up what somebody keeps; a fact two janitors would
    /// both keep waits. Answers the facts that were written down now, as they then stand.
    pub fn keep_audio(
        &mut self,
        seen: &mut Seen,
        world: &dyn World,
        now_ms: i64,
        limit: usize,
    ) -> Vec<Fact> {
        let kept_by_somebody = |agent_id: &str| self.audio_kept_for(agent_id, world);
        let mut taken: Vec<(u64, (u64, String))> = seen
            .facts
            .iter()
            .filter(|(_, fact)| fact.completed_at.is_none() && kept_by_somebody(&fact.agent_id))
            .map(|(key, fact)| (fact.event_id, key.clone()))
            .collect();
        taken.sort();
        taken.truncate(limit.clamp(1, AT_MOST));

        let mut keepers: BTreeMap<String, Option<(String, String)>> = BTreeMap::new();
        let mut written = Vec::new();
        for (_, key) in taken {
            let Some(fact) = seen.facts.get_mut(&key) else {
                continue;
            };
            // Who keeps the books for this agent, and under which of its triggers.
            let keeper = keepers.entry(fact.agent_id.clone()).or_insert_with(|| {
                let target = Some(fact.agent_id.as_str());
                let janitor = self.resolve(ROLE, target, world).ok().flatten()?;
                let mut own = self.triggers_of(&janitor).into_iter();
                let filed = own.find(|(_, kept)| kept.enabled && kept.trigger_id == TRIGGER)?;
                Some((janitor.clone(), filed.0.clone()))
            });
            let Some((janitor, attachment_id)) = keeper.clone() else {
                continue;
            };
            let (Some(config), Some(agent)) =
                (self.configs.get_mut(&janitor), world.agent(&janitor))
            else {
                continue;
            };
            let run_id = format!("audio-bookkeeping-{}", fact.event_id);
            let frozen = json!({
                "template_id": ROLE, "executor": "deterministic", "generation": config.generation,
                "context": {
                    "event_id": fact.event_id, "clip_id": fact.clip_id, "stage": fact.stage,
                    "target_agent_id": fact.agent_id, "turn_id": fact.turn_id, "runtime_id": null,
                },
            });
            let result = json!({
                "summary": WRITTEN, "status": fact.stage, "item_count": 1,
                "target_agent_id": fact.agent_id,
            });
            let run = Run {
                run_id: run_id.clone(),
                agent_id: janitor.clone(),
                session: agent.session,
                attachment_id,
                generation: config.generation,
                trace_id: run_id.clone(),
                status: "completed".to_string(),
                outcome: "completed".to_string(),
                configuration: frozen.as_object().cloned().unwrap_or_default(),
                created_at: now_ms,
                started_at: Some(now_ms),
                finished_at: Some(now_ms),
                demand_result: Some(result),
                ..Run::default()
            };
            // The janitor's last run is this one; its book is not said to have changed.
            config.last_run_at = Some(now_ms);
            config.last_error = String::new();
            self.runs.entry(run_id.clone()).or_insert(run);
            fact.run_id = Some(run_id);
            fact.completed_at = Some(now_ms);
            written.push(fact.clone());
        }
        let touched: Vec<String> = keepers
            .into_values()
            .flatten()
            .map(|(janitor, _)| janitor)
            .collect();
        for janitor in touched {
            self.forget_old(&janitor);
        }
        written
    }
}
