//! The books of what is said aloud, at the Host's edge.
//!
//! The rules are the kernel's ([`clarp_kernel::janitors::audio`]): a clip seen in a stage
//! is written down once, and a pass writes each such fact as a completed run of the janitor
//! that keeps the books for the clip's agent, when one is on. No model is asked. Here is
//! what the rules need of a Host.
//!
//! **What is seen.** A line an agent says aloud carries what became of its clip: that it
//! has one, how its making ended, that it was announced, and what an app last said of it.
//! The lines of an agent are looked at when they changed, on the janitors' tick; and what
//! an app says it did with a clip is seen when it says so (`POST /clips/ack`), so that
//! each thing it says is seen in its turn and not only the last. The Python Host's
//! database sees every change of a clip's row; this is the same from what this Host keeps
//! of a clip.
//!
//! **What is kept**, with the fleet: each fact under its clip and stage, for the week a
//! clip is kept to be played again; then it is forgotten, written down or not.

use std::collections::HashMap;
use std::sync::Arc;

use clarp_kernel::janitors::audio::{self as rules, Fact, Seen};
use clarp_kernel::spoken::{self, Line, LineState, SpeechBook};

use crate::agent::Unavailable;
use crate::edge::App;

/// The kind of thing a fact is, among what is left with the fleet.
const FACTS: &str = "audio-facts";
/// What a run of the bookkeeper is named before its fact's number.
const RUN: &str = "audio-bookkeeping-";
/// How long ago a line may have been said for its clip to be looked at on the tick: what
/// an app says of an older clip is seen when it says it.
const LOOKED_AT_FOR_MS: i64 = 24 * 3_600_000;
/// How often what was seen long ago is forgotten.
const FORGETS_EVERY_MS: i64 = 3_600_000;

/// The audio bookkeeper of this Host. `None` until what it kept was read.
#[derive(Default)]
pub struct Desk {
    held: tokio::sync::Mutex<Option<Held>>,
}

struct Held {
    seen: Seen,
    /// Each agent's lines as they were when they were last looked at.
    looked: HashMap<String, Arc<SpeechBook>>,
    forgot_at: i64,
}

fn place(fact: &Fact) -> String {
    format!("{}:{}", fact.clip_id, fact.stage)
}

/// The stages a line's clip has been in, as far as the line says: made by its producer and
/// for its listener at once, then whole or failed, then announced, then whatever an app
/// last said of it.
fn stages(line: &Line) -> Vec<String> {
    let mut been = vec![rules::producer("streaming"), rules::client("synthesized")];
    match line.state {
        LineState::Spoken => been.push(rules::producer("complete")),
        LineState::Failed => been.push(rules::producer("failed")),
        LineState::Queued | LineState::Speaking | LineState::Dropped => {}
    }
    if line.event_id.is_some() {
        been.push(rules::client("broadcast"));
    }
    if let Some(said) = &line.played {
        been.push(rules::client(said));
    }
    been
}

impl Desk {
    /// What was seen, read where it is kept the first time it is asked for. The numbers
    /// given so far stay given: no smaller than any fact that is kept, nor than any run
    /// the janitors' book still names by one.
    async fn held(
        &self,
        app: &App,
    ) -> Result<tokio::sync::MutexGuard<'_, Option<Held>>, Unavailable> {
        let mut held = self.held.lock().await;
        if held.is_none() {
            let kept = app.fleet.kept_all(FACTS).await?;
            let facts = kept.iter();
            let facts =
                facts.filter_map(|(_, written)| serde_json::from_slice::<Fact>(written).ok());
            let named = app.janitors.read(app, |book, _, _| {
                let numbers = book.runs.keys().filter_map(|run_id| {
                    let number = run_id.strip_prefix(RUN)?;
                    number.parse::<u64>().ok()
                });
                numbers.max().unwrap_or(0)
            });
            *held = Some(Held {
                seen: Seen::new(facts.collect::<Vec<Fact>>(), named.await?),
                looked: HashMap::new(),
                forgot_at: 0,
            });
        }
        Ok(held)
    }
}

async fn keep_fact(app: &App, fact: &Fact) {
    if let Ok(written) = serde_json::to_vec(fact) {
        app.fleet.keep_said(FACTS, place(fact), Some(written)).await;
    }
}

/// An app said what it did with a clip: seen now, in its turn.
pub(crate) async fn said(app: &App, agent_id: &str, clip: u64, turn: u64, status: &str) {
    let Ok(mut held) = app.audio.held(app).await else {
        return;
    };
    let Some(held) = held.as_mut() else {
        return;
    };
    let now = app.clock.now_ms();
    let stage = rules::client(status);
    let fresh = held
        .seen
        .observe(clip, agent_id, Some(turn), &stage, now)
        .cloned();
    if let Some(fact) = fresh {
        keep_fact(app, &fact).await;
    }
}

/// The bookkeeper's part of the janitors' tick: the lines that changed are looked at, what
/// waits is written down where somebody keeps the books for its agent, and what was seen
/// more than a week ago is forgotten.
pub(crate) async fn keep(app: &App) {
    let mut held = match app.audio.held(app).await {
        Ok(held) => held,
        Err(why) => return app.fleet.unread(FACTS, &why.0),
    };
    let Some(held) = held.as_mut() else {
        return;
    };
    let now = app.clock.now_ms();
    let directory = app.fleet.directory();
    let mut changed: Vec<Fact> = Vec::new();
    for member in &directory.members {
        let agent_id = &member.record.agent_id;
        let lines = member.agent.view().speech.clone();
        let as_before = held.looked.get(agent_id);
        if as_before.is_some_and(|before| Arc::ptr_eq(before, &lines)) {
            continue;
        }
        for line in lines.lines() {
            let Some(clip) = line
                .clip_id
                .filter(|_| now - line.queued_at_ms <= LOOKED_AT_FOR_MS)
            else {
                continue;
            };
            for stage in stages(line) {
                let fresh = held
                    .seen
                    .observe(clip, agent_id, Some(line.turn), &stage, now);
                changed.extend(fresh.cloned());
            }
        }
        held.looked.insert(agent_id.clone(), lines);
    }
    held.looked.retain(|agent_id, _| {
        let mut members = directory.members.iter();
        members.any(|member| member.record.agent_id == *agent_id)
    });
    // The pass is made where somebody keeps the books for an agent something waits of:
    // asked first without touching the book, since most ticks find nothing.
    let seen = &held.seen;
    let waits = app.janitors.read(app, |book, world, _| {
        let waiting = seen.all().into_iter();
        let mut waiting = waiting.filter(|fact| fact.completed_at.is_none());
        waiting.any(|fact| book.audio_kept_for(&fact.agent_id, world))
    });
    let completed = match waits.await {
        Ok(true) => {
            let seen = &mut held.seen;
            app.janitors
                .with(app, |book, world, now_ms| {
                    book.keep_audio(seen, world, now_ms, rules::AT_A_TIME)
                })
                .await
        }
        Ok(false) => Ok(Vec::new()),
        Err(why) => {
            app.fleet.unread(FACTS, &why.0);
            Ok(Vec::new())
        }
    };
    match completed {
        Ok(facts) => changed.extend(facts),
        Err(why) => app.fleet.unkept(FACTS, &why.0),
    }
    // Each fact is kept as it now stands: once, also where it was seen and written down
    // on the same tick.
    let mut last: HashMap<String, &Fact> = HashMap::new();
    for fact in &changed {
        last.insert(place(fact), fact);
    }
    for fact in last.into_values() {
        keep_fact(app, fact).await;
    }
    if now - held.forgot_at >= FORGETS_EVERY_MS {
        held.forgot_at = now;
        for gone in held.seen.forget_before(now - spoken::KEPT_FOR_MS) {
            app.fleet.keep_said(FACTS, place(&gone), None).await;
        }
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    #[test]
    fn a_line_says_which_stages_its_clip_has_been_in() {
        let line = |state: LineState, event_id: Option<u64>, played: Option<&str>| Line {
            clip_id: Some(4),
            state,
            event_id,
            played: played.map(str::to_string),
            ..Line::default()
        };
        // Being made: made by its producer, and for its listener.
        assert_eq!(
            stages(&line(LineState::Speaking, None, None)),
            ["producer:streaming", "client:synthesized"]
        );
        // Whole, announced, and played to its end.
        assert_eq!(
            stages(&line(LineState::Spoken, Some(9), Some("play-ok"))),
            [
                "producer:streaming",
                "client:synthesized",
                "producer:complete",
                "client:broadcast",
                "client:play-ok"
            ]
        );
        // Nothing came of it.
        assert_eq!(
            stages(&line(LineState::Failed, None, None)),
            [
                "producer:streaming",
                "client:synthesized",
                "producer:failed"
            ]
        );
    }
}
