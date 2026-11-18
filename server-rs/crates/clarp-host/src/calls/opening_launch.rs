//! Check the opening immediately before handing a turn to the shim. This is
//! a Host launch-thread request; the shim never calls back into the Host.
use crate::edge::App;
use clarp_kernel::rings::opening::{self as rules, Gate as Phase};
use serde_json::json;
use tokio::sync::{mpsc, oneshot};

pub struct Request {
    phase: Phase,
    id: String,
    trace: String,
    attempt: u32,
    previous_unsent: bool,
    reply: oneshot::Sender<bool>,
}
#[derive(Clone)]
pub struct Gate(mpsc::UnboundedSender<Request>);

pub fn channel() -> (Gate, mpsc::UnboundedReceiver<Request>) {
    let (tx, rx) = mpsc::unbounded_channel();
    (Gate(tx), rx)
}

impl Gate {
    pub fn ask(&self, phase: Phase, opening: &clarp_actor::OpeningOrder, attempt: u32) -> bool {
        let (reply, rx) = oneshot::channel();
        self.0
            .send(Request {
                phase,
                id: opening.id.clone(),
                trace: opening.trace.clone(),
                attempt,
                previous_unsent: opening.previous_unsent,
                reply,
            })
            .is_ok()
            && rx.blocking_recv().unwrap_or(false)
    }

    /// A terminal failure observed by the actor may race another receipt.
    /// Report it to the desk without blocking the actor's own mailbox.
    pub fn failed(&self, id: &str, trace: &str) {
        let (reply, _) = oneshot::channel();
        let _ = self.0.send(Request {
            phase: Phase::FailedAfterLaunch,
            id: id.into(),
            trace: trace.into(),
            attempt: 0,
            previous_unsent: false,
            reply,
        });
    }
}

pub async fn run(app: App, mut rx: mpsc::UnboundedReceiver<Request>) {
    if let Err(why) = app.calls.recover_openings(&app).await {
        eprintln!("clarp-host: opening recovery: {}", why.0);
    }
    while let Some(Request {
        phase,
        id,
        trace,
        attempt,
        previous_unsent,
        reply,
    }) = rx.recv().await
    {
        let _guard = app.calls.opening_turn.lock().await;
        let Some((ring_id, _)) = id
            .strip_prefix("call-open:")
            .and_then(|id| id.rsplit_once(':'))
        else {
            let _ = reply.send(false);
            continue;
        };
        let answer = app.calls.with_opening(&app,ring_id, |rings, calls, with, (out, _)| {
            let present = rings.rings().iter().any(|r| r.ring_id == ring_id);
            let now = with.at.now_ms;
            let generation = super::opening::proven(with);
            rings.change_opening(ring_id, now, out, |ring, fx| {
                let reason = with.world.opening.and_then(|proof| rules::fence(ring, present, calls, with.at.host_id, proof).map(str::to_owned));
                let op = &mut ring.opening;
                let dispatch = |phase, op: &mut serde_json::Value, fx: &mut rules::Effects| {
                    let status = if phase == Phase::Claim && op["intent"]["state"] == "admitted" { "queued" } else { "claimed" };
                    let row = json!({"status":status,"trace_id":trace,"claimed_at":if status=="claimed"{json!(now)}else{serde_json::Value::Null}});
                    let world = rules::World{now,host:with.at.host_id,generation:&generation,fence:reason.as_deref(),queue:&row,nonce:"",trace:""};
                    let mut dispatched = rules::Dispatch::default();
                    let result = rules::dispatch(phase, op, &id, &trace, &world, &mut dispatched);
                    *fx = dispatched.opening;
                    result == true || result == "launch"
                };
                // An older attempt can release only its own committed claim.
                // A replay of this attempt after a crash must never reuse that proof.
                if phase == Phase::Claim && previous_unsent && attempt > 1
                    && op["launch_attempt"] == attempt - 1 && op["intent"]["state"] == "started"
                    && (!dispatch(Phase::ReleaseUnsent, op, fx) || op["intent"]["state"] != "admitted")
                {
                    return false;
                }
                let allowed = dispatch(phase, op, fx);
                if allowed && phase == Phase::Claim {
                    op["launch_attempt"] = json!(attempt);
                }
                allowed
            }).is_some_and(|(allowed, _)| allowed)
        }).await.unwrap_or(false);
        let _ = reply.send(answer);
    }
}
