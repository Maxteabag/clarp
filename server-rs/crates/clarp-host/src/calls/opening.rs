//! Claims and greetings are serialized with changes to the owning call. Raw
//! tokens are accepted only in the request and never published on the stream.
use super::{Desk, With, caller};
use crate::{
    agent::Unavailable,
    edge::{App, Principal},
};
use axum::{
    Extension, Json,
    body::Bytes,
    extract::State,
    http::StatusCode,
    response::{IntoResponse, Response},
};
use clarp_kernel::{
    calls::{Asker, Refused},
    rings::opening as rules,
};
use serde_json::{Value, json};

#[derive(Clone, Copy)]
pub(crate) enum Change<'a> {
    Claim(&'a Value),
    Greeting {
        body: &'a Value,
        message: &'a str,
        trace: &'a str,
        session: &'a str,
        call: &'a str,
    },
}

struct Applied {
    status: u16,
    answer: Value,
    admit: bool,
    withdraw: bool,
    state: Value,
}

impl Desk {
    async fn apply_opening(
        &self,
        app: &App,
        owner: &str,
        change: Change<'_>,
        id: &str,
        queue: &Value,
    ) -> Result<Applied, Unavailable> {
        self.with_opening(app, id, |rings, calls, with, (out, call_effects)| {
            let now = with.at.now_ms;
            let body = match &change {
                Change::Claim(b) | Change::Greeting { body: b, .. } => *b,
            };
            let token = &body["token"];
            let found = rings.by_opening_token(token);
            let ring = found.map(|ring| ring.ring_id.clone()).unwrap_or_default();
            let fence = found.and_then(|ring| {
                with.world.opening.and_then(|proof| {
                    rules::fence(
                        ring,
                        rings.rings().iter().any(|r| r.ring_id == ring.ring_id),
                        calls,
                        with.at.host_id,
                        proof,
                    )
                    .map(str::to_owned)
                })
            });
            let generation = proven(with);
            let nonce = format!("own_{}", crate::ids::hex(16));
            let trace = format!("callopen-{}", crate::ids::hex(6));
            let w = rules::World {
                generation: &generation,
                now,
                host: with.at.host_id,
                fence: fence.as_deref(),
                queue,
                nonce: &nonce,
                trace: &trace,
            };
            let perform = |op: &mut Value, fx: &mut rules::Effects| match &change {
                Change::Claim(body) => rules::claim(op, owner, body, &w, fx),
                Change::Greeting {
                    body,
                    message,
                    trace,
                    session,
                    call,
                } => rules::greet(
                    op,
                    rules::Greeting {
                        principal: owner,
                        token: &body["token"],
                        epoch: &body["epoch"],
                        message,
                        trace,
                        session,
                        call,
                    },
                    &w,
                    fx,
                ),
            };
            if let Change::Greeting { call, session, .. } = &change
                && let Err(failed) = calls.exact_recipient(owner, call, session)
            {
                return Applied {
                    status: failed.status,
                    answer: failed.body,
                    admit: false,
                    withdraw: false,
                    state: Value::Null,
                };
            }
            let ((status, answer, state), fx) = rings
                .change_opening(&ring, now, out, |ring, fx| {
                    let (status, answer) = perform(&mut ring.opening, fx);
                    (status, answer, ring.opening.clone())
                })
                .unwrap_or_else(|| {
                    let mut fx = rules::Effects::default();
                    let (status, answer) = perform(&mut Value::Null, &mut fx);
                    ((status, answer, Value::Null), fx)
                });
            if fx.announce {
                let claim = body["claim"].as_str().unwrap_or("greeting");
                rings.opening_audit(&ring, claim, owner, answer["route"].as_str(), now, out);
            }
            if status == 200
                && let Change::Greeting { call, session, .. } = change
            {
                let asker = Asker {
                    caller: owner,
                    call_id: call,
                    ..Default::default()
                };
                if let Err(Refused { status, body }) =
                    calls.route(asker, session, with.at, call_effects)
                {
                    return Applied {
                        status,
                        answer: body,
                        admit: false,
                        withdraw: false,
                        state,
                    };
                }
            }
            Applied {
                status,
                answer,
                admit: fx.admit,
                withdraw: fx.takes_waiter(queue, &state["intent"]["trace_id"]),
                state,
            }
        })
        .await
    }

    pub(crate) async fn greeting(
        &self,
        app: &App,
        owner: &str,
        change: Change<'_>,
    ) -> Result<Result<Value, Refused>, Unavailable> {
        let applied = self.resolve_opening(app, owner, change).await?;
        if applied.status != 200 {
            return Ok(Err(Refused {
                status: applied.status,
                body: applied.answer,
            }));
        }
        Ok(Ok(applied.answer))
    }

    pub(crate) async fn bind_clip(&self,app:&App,id:&str,agent:&str,binding:clarp_kernel::rings::binding::Binding)->Result<(),Unavailable>{
        self.with_opening(app,id,|rings,_,with,(out,_)|{
            rings.change_opening(id,with.at.now_ms,out,|r,fx|{
                if r.agent_id == agent {
                    fx.dirty = binding.record(&mut r.opening);
                }
            });
        }).await
    }

    pub(crate) async fn greeting_admitted(
        &self,
        app: &App,
        id: &str,
        message: &str,
        ok: bool,
    ) -> Result<(), Unavailable> {
        self.with_opening(app, id, |rings, _, with, (out, _)| {
            let g = proven(with);
            let world = rules::World {
                generation: &g,
                now: with.at.now_ms,
                host: with.at.host_id,
                fence: None,
                queue: &Value::Null,
                nonce: "",
                trace: "",
            };
            rings.change_opening(id, world.now, out, |ring, fx| {
                rules::greeting_admitted(&mut ring.opening, message, ok, &world, fx)
            });
        })
        .await
    }
}

pub(super) fn proven(with: &With) -> Value {
    let Some(proof) = with.world.opening else {
        return Value::Null;
    };
    let first = proof.generation();
    if first == proof.generation() {
        first
    } else {
        Value::Null
    }
}

pub(super) async fn claim(
    State(app): State<App>,
    Extension(principal): Extension<Principal>,
    body: Bytes,
) -> Response {
    let owner = match caller(&principal) {
        Ok(who) => who,
        Err(err) => return err.into_response(),
    };
    let body: Value = serde_json::from_slice(&body).unwrap_or(Value::Null);
    match app
        .calls
        .resolve_opening(&app, &owner, Change::Claim(&body))
        .await
    {
        Ok(applied) => {
            let status = StatusCode::from_u16(applied.status).unwrap_or(StatusCode::CONFLICT);
            (status, Json(applied.answer)).into_response()
        }
        Err(failed) => crate::edge::ApiError::from(failed).into_response(),
    }
}

impl Desk {
    async fn saved_opening(&self, app: &App, token: &Value) -> Result<Option<Value>, Unavailable> {
        let mut held = self.books.lock().await;
        let books = Self::read(&mut held, app).await?;
        if let Some(r) = books.rings.by_opening_token(token) {
            return Ok(Some(r.opening.clone()));
        }
        let Some(token) = token.as_str().filter(|t| t.starts_with("opn_")) else {
            return Ok(None);
        };
        let Some(id) = app.fleet.kept("opening_by_token", token.into()).await? else {
            return Ok(None);
        };
        let id = String::from_utf8(id).map_err(|e| Unavailable(e.to_string()))?;
        let Some(raw) = app.fleet.kept("opening_history", id).await? else {
            return Ok(None);
        };
        let ring: clarp_kernel::rings::Ring =
            serde_json::from_slice(&raw).map_err(|e| Unavailable(e.to_string()))?;
        Ok(Some(ring.opening))
    }

    pub(super) async fn queue_row(&self, app: &App, op: &Value) -> Result<Value, Unavailable> {
        let Some(id) = op["intent"]["admission_id"].as_str() else {
            return Ok(Value::Null);
        };
        let Some(member) = op["session"].as_str().and_then(|s| app.member(s)) else {
            return Ok(Value::Null);
        };
        member.agent.opening_row(id).await
    }

    async fn resolve_opening(
        &self,
        app: &App,
        owner: &str,
        change: Change<'_>,
    ) -> Result<Applied, Unavailable> {
        let _serial = self.opening_turn.lock().await;
        let body = match &change {
            Change::Claim(b) | Change::Greeting { body: b, .. } => *b,
        };
        let original = self
            .saved_opening(app, &body["token"])
            .await?
            .unwrap_or(Value::Null);
        let row = self.queue_row(app, &original).await?;
        let id = original["ring_id"].as_str().unwrap_or("");
        let mut applied = self.apply_opening(app, owner, change, id, &row).await?;
        if applied.withdraw
            && let Some(turn) = row["turn"].as_u64()
            && let Some(member) = original["session"].as_str().and_then(|s| app.member(s))
        {
            let _ = member
                .agent
                .withdraw(
                    clarp_kernel::turn::TurnId(turn),
                    "opening superseded".into(),
                )
                .await?;
        }
        if applied.admit {
            self.materialize(app, &applied.state).await?;
            if let Some(op) = self.saved_opening(app, &body["token"]).await? {
                applied.answer["opening"] = rules::public(&op, true);
            }
        }
        Ok(applied)
    }

    /// Called under opening_turn. An admitted-but-not-yet-written agent row
    /// can be recovered using its reserved id; a foreign row is never adopted.
    pub(super) async fn materialize(&self, app: &App, op: &Value) -> Result<(), Unavailable> {
        use clarp_kernel::conversation::Message;
        use clarp_kernel::turn::{Admission, Origin};
        let id = op["intent"]["admission_id"].as_str().unwrap_or("");
        let trace = op["intent"]["trace_id"].as_str().unwrap_or("");
        let ring_id = op["ring_id"].as_str().unwrap_or("");
        let member = app
            .member(op["session"].as_str().unwrap_or(""))
            .ok_or_else(|| Unavailable("opening agent gone".into()))?;
        let row = member.agent.opening_row(id).await?;
        let (authorized,withdraw) = self
            .with_opening(app,ring_id, |rings, calls, with, (out, _)| {
                let present = rings.rings().iter().any(|r| r.ring_id == ring_id);
                let generation = proven(with);
                rings
                    .change_opening(ring_id, with.at.now_ms, out, |ring, fx| {
                        let fence = with.world.opening.and_then(|p| {
                            rules::fence(ring, present, calls, with.at.host_id, p).map(str::to_owned)
                        });
                        let op = &mut ring.opening;
                        if op["intent"]["admission_id"] != id
                            || op["intent"]["trace_id"] != trace
                        {
                            return false;
                        }
                        let world = rules::World {
                            generation: &generation,
                            now: with.at.now_ms,
                            host: with.at.host_id,
                            queue: &row,
                            nonce: "",
                            trace: "",
                            fence: fence.as_deref(),
                        };
                        let admission = if row.is_null() { Value::Null } else {
                            json!({"channel": if row["foreign"] == true {"message"} else {"call"}, "trace_id":row["trace_id"]})
                        };
                        let decision = rules::admit(op,&admission,true,&world,fx);
                        fx.announce = fx.dirty && decision == "fenced";
                        decision == "admitted"
                    })
                    .map(|(send, fx)| (send,fx.takes_waiter(&row,&op["intent"]["trace_id"]))).unwrap_or_default()
            })
            .await?;
        if withdraw && let Some(turn) = row["turn"].as_u64() {
            member
                .agent
                .withdraw(clarp_kernel::turn::TurnId(turn), "opening retired".into())
                .await?;
        }
        if !authorized {
            return Ok(());
        }
        let (filed, marked) = Message::filed(id);
        let msg = Message {
            id: filed,
            marked,
            unminted: true,
            call: None,
            sender: None,
            client: None,
            speak: true,
            unheard: false,
            trace_id: Some(trace.into()),
            origin: Origin::Trigger {
                name: "automation".into(),
            },
            text: rules::header(op),
        };
        let _guard = app.mouth.binding_admission.write().await;
        let sent = member.agent.send(msg, Admission::Later).await?;
        if let Some(turn) = sent.turn {
            self.bind_clip(app,ring_id,&member.record.agent_id,clarp_kernel::rings::binding::Binding {
                turn_id: turn.0, kind: clarp_kernel::rings::binding::Kind::Initiative,
                admission_id:id.into(),epoch:op["epoch"].as_u64().unwrap_or(1)
            }).await?;
        }
        if sent.duplicate {
            // Another send may have won the id after our initial read. Only
            // the actor can establish the provenance of the winning message.
            let winner = member.agent.opening_row(id).await?;
            if winner["foreign"] == true || winner["trace_id"] != trace {
                self.with_opening(app, ring_id, |rings, _, with, (out, _)| {
                    rings.change_opening(ring_id, with.at.now_ms, out, |r, fx| {
                        if r.opening["intent"]["admission_id"] == id
                            && r.opening["intent"]["trace_id"] == trace
                            && r.opening["intent"]["state"] == "admitted"
                        {
                            r.opening["intent"]["state"] = json!("unknown");
                            if r.opening["retired"].is_null() {
                                r.opening["state"] = json!("unknown");
                            }
                            fx.dirty = true;
                            fx.announce = true;
                        }
                    });
                })
                .await?;
            }
        }
        Ok(())
    }
}

impl Desk {
    async fn archived_openings(app: &App) -> Result<Vec<clarp_kernel::rings::Ring>, Unavailable> {
        app.fleet
            .kept_all("opening_history")
            .await?
            .into_iter()
            .map(|(_, raw)| serde_json::from_slice(&raw).map_err(|e| Unavailable(e.to_string())))
            .collect()
    }

    /// A process may stop between the intent commit and the agent's idempotent
    /// admission. Recover only the reservation that is still current.
    pub async fn recover_openings(&self, app: &App) -> Result<(), Unavailable> {
        let _guard = self.opening_turn.lock().await;
        let pending = {
            let mut held = self.books.lock().await;
            let books = Self::read(&mut held, app).await?;
            let archived = Self::archived_openings(app).await?;
            books
                .rings
                .rings()
                .iter()
                .chain(archived.iter())
                .filter(|r| {
                    r.opening["retired"].is_null()
                        && matches!(
                            r.opening["intent"]["state"].as_str(),
                            Some("admitted" | "intended")
                        )
                })
                .map(|r| r.opening.clone())
                .collect::<Vec<_>>()
        };
        for op in pending {
            self.materialize(app, &op).await?;
        }
        Ok(())
    }

    /// Prune from the call's durable end time even after its public history
    /// has rolled over. Update the ring list and delete lookup keys together.
    pub async fn prune_openings(&self, app:&App)->Result<(),Unavailable> {
        let _serial=self.opening_turn.lock().await;
        let mut held=self.books.lock().await;
        let books=Self::read(&mut held,app).await?;
        let now=app.clock.now_ms();
        let archived=Self::archived_openings(app).await?;
        let mut rings=books.rings.clone();
        let mut deletes=Vec::new();
        let mut hot_changed=false;
        for (ring,hot) in books.rings.opening_records().map(|r|(r,true)).chain(archived.iter().map(|r|(r,false))) {
            let op=&ring.opening;
            let known=books.calls.of(op["principal"].as_str().unwrap_or("")).iter().find(|c| c.call_id==ring.call_id);
            let ended = match known {
                Some(call) if call.state=="live" => None,
                Some(call) => call.ended_at,
                None => op["call_ended_at"].as_i64(),
            };
            if !clarp_kernel::rings::binding::expired(op["retired"]["at"].as_i64(),ended,now) {continue}
            if hot { rings.forget_opening(&ring.ring_id); hot_changed=true; }
            deletes.push(("opening_history",ring.ring_id.clone()));
            deletes.push(("opening_by_call",ring.call_id.clone()));
            if let Some(token)=op["token"].as_str(){deletes.push(("opening_by_token",token.into()));}
            for binding in op["clip_bindings"].as_array().into_iter().flatten(){
                if let Some(key)=binding["turn_id"].as_u64().and_then(|turn|clarp_kernel::rings::binding::key(&ring.agent_id,turn)) {deletes.push(("opening_by_turn",key));}
            }
        }
        let mut writes=Vec::new();
        if hot_changed {writes.push((super::RINGS,super::RINGS_KEPT.into(),serde_json::to_vec(rings.rings()).map_err(|e|Unavailable(e.to_string()))?));}
        app.fleet.change_together(writes,deletes).await?;
        books.rings=rings;
        Ok(())
    }

    pub async fn retire_openings(
        &self,
        app: &App,
        principal: Option<&str>,
    ) -> Result<(), Unavailable> {
        let _guard = self.opening_turn.lock().await;
        let retired = {
            let mut held = self.books.lock().await;
            let books = Self::read(&mut held, app).await?;
            let archived = Self::archived_openings(app).await?;
            books
                .rings
                .rings()
                .iter()
                .chain(archived.iter())
                .filter(|r| !r.opening.is_null() && r.opening["retired"].is_null())
                .filter_map(|r| {
                    let reason = if let Some(owner) = principal {
                        (r.opening["principal"] == owner).then(|| "auth_revoked".to_string())
                    } else {
                        let call = books.calls.by_id(&r.call_id, app.clock.now_ms())?;
                        (call["state"] != "live")
                            .then(|| format!("call_{}", call["reason"].as_str().unwrap_or("ended")))
                    }?;
                    Some((r.opening.clone(), reason))
                })
                .collect::<Vec<_>>()
        };
        for (op, reason) in retired {
            let row = self.queue_row(app, &op).await?;
            let id = op["ring_id"].as_str().unwrap_or("");
            let withdraw = self
                .with_opening(app, id, |rings, _, with, (out, _)| {
                    let generation = proven(with);
                    let world = rules::World {
                        now: with.at.now_ms,
                        generation: &generation,
                        host: with.at.host_id,
                        queue: &row,
                        fence: None,
                        nonce: "",
                        trace: "",
                    };
                    let changed = rings.change_opening(id, with.at.now_ms, out, |r, fx| {
                        fx.announce = rules::retire_current(&mut r.opening, &reason, &world, fx);
                    });
                    changed.is_some_and(|(_, fx)| fx.takes_waiter(&row, &op["intent"]["trace_id"]))
                })
                .await?;
            if withdraw
                && let Some(turn) = row["turn"].as_u64()
                && let Some(m) = op["session"].as_str().and_then(|s| app.member(s))
            {
                m.agent
                    .withdraw(clarp_kernel::turn::TurnId(turn), reason)
                    .await?;
            }
        }
        Ok(())
    }
}
