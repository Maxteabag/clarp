//! The final authorization runs on the Host's launch thread before a shim
//! starts. The shim never waits on a Host and carries no opening token.
use crate::edge::App;
use clarp_kernel::rings::opening as rules;
use serde_json::{Value, json};
use tokio::sync::{mpsc, oneshot};

pub type Request = (String, String, oneshot::Sender<bool>);
#[derive(Clone)]
pub struct Gate(mpsc::UnboundedSender<Request>);
pub fn channel() -> (Gate, mpsc::UnboundedReceiver<Request>) {
    let (tx, rx) = mpsc::unbounded_channel();
    (Gate(tx), rx)
}
impl Gate {
    pub fn allowed(&self, id: &str, trace: &str) -> bool {
        let (tx, rx) = oneshot::channel();
        self.0.send((id.into(), trace.into(), tx)).is_ok() && rx.blocking_recv().unwrap_or(false)
    }
}

pub async fn run(app: App, mut rx: mpsc::UnboundedReceiver<Request>) {
    while let Some((id, trace, reply)) = rx.recv().await {
        let _guard = app.calls.opening_turn.lock().await;
        let result = app
            .calls
            .rings(&app, |rings, calls, with, (out, _)| {
                let Some((ring_id, _)) = id
                    .strip_prefix("call-open:")
                    .and_then(|i| i.rsplit_once(':'))
                else {
                    return false;
                };
                let generation = super::opening::proven(with);
                let now = with.at.now_ms;
                rings
                    .change_opening(ring_id, now, out, |ring, fx| {
                        let fence = with
                            .world
                            .opening
                            .and_then(|p| rules::fence(ring, calls, with.at.host_id, p));
                        let op = &mut ring.opening;
                        if op["intent"]["admission_id"] != id || op["intent"]["trace_id"] != trace {
                            return false;
                        }
                        if op["intent"]["state"] != "admitted" || !op["retired"].is_null() {
                            return false;
                        }
                        if let Some(reason) =
                            rules::breach(&op["fence"]["host_gen"], &generation).or(fence)
                        {
                            op["retired"] = json!({"reason":reason,"at":now});
                            op["state"] = json!("unknown");
                            op["intent"]["state"] = json!("unknown");
                            fx.dirty = true;
                            fx.announce = true;
                            return false;
                        }
                        op["state"] = json!("initiative_started");
                        op["intent"]["state"] = json!("started");
                        fx.dirty = true;
                        fx.announce = true;
                        true
                    })
                    .is_some_and(|(allowed, _)| allowed)
            })
            .await
            .unwrap_or(false);
        let _ = reply.send(result);
    }
}
