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
        queue: &Value,
    ) -> Result<Applied, Unavailable> {
        self.rings(app, |rings, calls, with, (out, call_effects)| {
            let now = with.at.now_ms;
            let body = match &change {
                Change::Claim(b) | Change::Greeting { body: b, .. } => *b,
            };
            let token = &body["token"];
            let found = rings.by_opening_token(token);
            let ring = found.map(|ring| ring.ring_id.clone()).unwrap_or_default();
            let fence = found.and_then(|ring| {
                with.world
                    .opening
                    .and_then(|proof| rules::fence(ring, calls, with.at.host_id, proof))
            });
            let generation = proven(with);
            let nonce = format!("own_{}", crate::ids::hex(16));
            let trace = format!("callopen-{}", crate::ids::hex(6));
            let w = rules::World {
                generation: &generation,
                now,
                host: with.at.host_id,
                fence,
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
                withdraw: fx.withdraw,
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

    pub(crate) async fn greeting_admitted(
        &self,
        app: &App,
        id: &str,
        message: &str,
        ok: bool,
    ) -> Result<(), Unavailable> {
        self.rings(app, |rings, _, with, (out, _)| {
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
        Ok(books
            .rings
            .by_opening_token(token)
            .map(|r| r.opening.clone()))
    }

    async fn queue_row(&self, app: &App, op: &Value) -> Result<Value, Unavailable> {
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
        let mut applied = self.apply_opening(app, owner, change, &row).await?;
        if applied.status == 404
            && let Change::Claim(claim) = change
            && let Some(token) = body["token"].as_str().filter(|t| t.starts_with("opn_"))
            && let Some(kept) = app.fleet.kept("opening_history", token.into()).await?
        {
            let mut op: Value =
                serde_json::from_slice(&kept).map_err(|err| Unavailable(err.to_string()))?;
            let queue = self.queue_row(app, &op).await?;
            let g = super::generation::read(&app.config.data_dir);
            let generation = if g == super::generation::read(&app.config.data_dir) {
                g
            } else {
                Value::Null
            };
            let now = app.clock.now_ms();
            let nonce = format!("own_{}", crate::ids::hex(16));
            let trace = format!("callopen-{}", crate::ids::hex(6));
            let world = rules::World {
                generation: &generation,
                now,
                host: &app.server_id,
                fence: Some("ring_changed"),
                queue: &queue,
                nonce: &nonce,
                trace: &trace,
            };
            let mut effects = rules::Effects::default();
            let (status, answer) = rules::claim(&mut op, owner, claim, &world, &mut effects);
            if effects.dirty {
                op["updated_at"] = json!(now);
                app.fleet
                    .keep(
                        "opening_history",
                        token.into(),
                        Some(op.to_string().into_bytes()),
                    )
                    .await?;
            }
            return Ok(Applied {
                status,
                answer,
                admit: false,
                withdraw: false,
                state: op,
            });
        }
        if applied.withdraw
            && row["foreign"] != true
            && row["trace_id"] == original["intent"]["trace_id"]
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
        let authorized=self.rings(app,|rings,calls,with,(out,_)|{
            let generation=proven(with);
            rings.change_opening(ring_id,with.at.now_ms,out,|ring,fx|{
                let fence=with.world.opening.and_then(|p|rules::fence(ring,calls,with.at.host_id,p));
                let op=&mut ring.opening;
                if op["intent"]["admission_id"]!=id || op["intent"]["trace_id"]!=trace || !op["retired"].is_null() ||
                    !matches!(op["intent"]["state"].as_str(),Some("intended"|"admitted")) {return false;}
                let why=rules::breach(&op["fence"]["host_gen"],&generation).or(fence);
                if why.is_some() || (!row.is_null() && (row["foreign"]==true || row["trace_id"]!=trace)){
                    op["retired"]=json!({"at":with.at.now_ms,"reason":why.unwrap_or("admission_conflict")});
                    op["state"]=json!("unknown");op["intent"]["state"]=json!("unknown");fx.dirty=true;fx.announce=true;return false;
                }
                op["intent"]["state"]=json!("admitted");fx.dirty=true;row.is_null()
            }).is_some_and(|(send,_)|send)
        }).await?;
        if !authorized {
            return Ok(());
        }
        let (id, marked) = Message::filed(id);
        let msg = Message {
            id,
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
        let _ = member.agent.send(msg, Admission::Later).await?;
        Ok(())
    }
}

impl Desk {
    /// A process may stop between the intent commit and the agent's idempotent
    /// admission. Recover only the reservation that is still current.
    pub async fn recover_openings(&self, app: &App) -> Result<(), Unavailable> {
        let _guard = self.opening_turn.lock().await;
        let pending = {
            let mut held = self.books.lock().await;
            let books = Self::read(&mut held, app).await?;
            books
                .rings
                .rings()
                .iter()
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

    pub async fn retire_openings(
        &self,
        app: &App,
        principal: Option<&str>,
    ) -> Result<(), Unavailable> {
        let _guard = self.opening_turn.lock().await;
        let retired = {
            let mut held = self.books.lock().await;
            let books = Self::read(&mut held, app).await?;
            books
                .rings
                .rings()
                .iter()
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
                .rings(app, |rings, _, with, (out, _)| {
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
                    changed.is_some_and(|(_, fx)| fx.withdraw && row["foreign"] != true)
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
