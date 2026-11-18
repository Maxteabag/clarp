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
    reply: oneshot::Sender<bool>,
}
#[derive(Clone)]
pub struct Gate(mpsc::UnboundedSender<Request>);

pub fn channel() -> (Gate, mpsc::UnboundedReceiver<Request>) {
    let (tx, rx) = mpsc::unbounded_channel();
    (Gate(tx), rx)
}

impl Gate {
    pub fn ask(&self, phase: Phase, id: &str, trace: &str) -> bool {
        let (reply, rx) = oneshot::channel();
        self.0
            .send(Request {
                phase,
                id: id.into(),
                trace: trace.into(),
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
        reply,
    }) = rx.recv().await
    {
        let _guard = app.calls.opening_turn.lock().await;
        let answer=app.calls.rings(&app,|rings,calls,with,(out,_)|{
            let Some((ring_id,_))=id.strip_prefix("call-open:").and_then(|id|id.rsplit_once(':')) else{return false;};
            let now=with.at.now_ms;let generation=super::opening::proven(with);
            rings.change_opening(ring_id,now,out,|ring,fx|{
                let reason=with.world.opening.and_then(|proof|rules::fence(ring,calls,with.at.host_id,proof));
                let intent=&ring.opening["intent"];
                // The actor has picked the row but the launch thread still
                // holds it. On recovery a previously committed launch is a
                // claim of unknown outcome, never a fresh queued row.
                let status=if phase==Phase::Claim && intent["state"]=="admitted" {"queued"}else{"claimed"};
                let row=json!({"status":status,"trace_id":trace,"claimed_at":if status=="claimed"{json!(now)}else{serde_json::Value::Null}});
                let world=rules::World{now,host:with.at.host_id,generation:&generation,fence:reason,queue:&row,nonce:"",trace:""};
                let mut dispatched=rules::Dispatch::default();
                let result=rules::dispatch(phase,&mut ring.opening,&id,&trace,&world,&mut dispatched);
                *fx=dispatched.opening;
                result==true || result=="launch"
            }).is_some_and(|(allowed,_)|allowed)
        }).await.unwrap_or(false);
        let _ = reply.send(answer);
    }
}
