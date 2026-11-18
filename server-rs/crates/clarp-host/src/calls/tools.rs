//! Effects for the frozen tool team of one joined voice call. The side router
//! never blocks the voice socket. Only its session may append the returned note.
use crate::edge::App;
use clarp_kernel::{
    live_call::{
        self, callback, receipts,
        routing::Route,
        team::{self, Action, Prompt, Team},
    },
    rings::Joined,
};
use serde_json::{Value, json};
use std::sync::{
    Arc,
    atomic::{AtomicBool, Ordering},
};
use std::time::Duration;

pub struct Worker {
    pub app: App,
    pub call: Joined,
    pub active: Arc<AtomicBool>,
    pub team: Team,
    pub began: tokio::time::Instant,
    pub replay:
        tokio::sync::mpsc::UnboundedSender<(Value, tokio::sync::oneshot::Sender<Option<bool>>)>,
    pub taken: Vec<Value>,
    pub asked: std::collections::BTreeSet<String>,
    pub answers: clarp_kernel::live_call::answers::Answers,
    pub auto_decided: bool,
    pub reconciled: tokio::sync::mpsc::UnboundedSender<String>,
}

pub fn time(epoch_ms: i64) -> String {
    let zone = clarp_kernel::oracle::threads::zone_named(&crate::oracle::zone_of_this_machine());
    clarp_kernel::oracle::threads::iso_time(epoch_ms, &zone)
        .and_then(|iso| iso.get(11..16).map(str::to_owned))
        .unwrap_or_else(|| "?".into())
}

impl super::Desk {
    fn tool_slot(&self, slot: &str) -> Arc<tokio::sync::Mutex<()>> {
        let mut slots = self
            .tool_slots
            .lock()
            .unwrap_or_else(|err| err.into_inner());
        slots.retain(|_, slot| slot.strong_count() > 0);
        let slot = slots.entry(slot.into()).or_default();
        slot.upgrade().unwrap_or_else(|| {
            let active = Arc::new(tokio::sync::Mutex::new(()));
            *slot = Arc::downgrade(&active);
            active
        })
    }
}

impl Worker {
    pub async fn current(&self) -> bool {
        self.active.load(Ordering::Acquire)
            && self.app.fleet.directory().can_receive(&self.call.principal)
            && self
                .app
                .calls
                .step(
                    &self.app,
                    (&self.call.call_id, self.call.call_epoch),
                    |book| book.holds(),
                )
                .await
                .unwrap_or(false)
    }

    fn binding(&self) -> callback::Binding<'_> {
        callback::Binding {
            host: &self.app.server_id,
            principal: &self.call.principal,
            agent: self.call.snapshot["agent"]["session"]
                .as_str()
                .unwrap_or(""),
            call: &self.call.call_id,
        }
    }

    async fn save(&self, key: &str, row: &Value) -> bool {
        self.app
            .fleet
            .keep("live_tools", key.into(), Some(row.to_string().into_bytes()))
            .await
            .is_ok()
    }

    async fn callback(&self, name: &str, args: &Value, explicit: bool) -> Value {
        let b = self.binding();
        let slot = b.slot();
        let mutex = self.app.calls.tool_slot(&slot);
        let _owner = mutex.lock().await;
        let known = self.app.fleet.kept("live_tools", slot.clone()).await;
        let mut row = match known {
            Ok(Some(bytes)) => match serde_json::from_slice::<Value>(&bytes) {
                Ok(value) => value,
                Err(_) => return failed(name),
            },
            Ok(None) => Value::Null,
            Err(_) => return failed(name),
        };
        let now = self.app.clock.now_ms() as f64 / 1000.0;
        if row["at"].as_f64().is_some_and(|at| now - at >= 86400.0) {
            row = Value::Null;
        }
        let principal = self.call.principal.clone();
        let eligible = self
            .app
            .calls
            .rings(&self.app, |rings, _, with, _| {
                rings.callback_recipient(&principal, &with.world)
            })
            .await
            .unwrap_or(false);
        let r = callback::Request {
            config: &self.call.snapshot["live_tools"],
            tool: name,
            args,
            explicit,
            eligible,
        };
        let result = tokio::task::block_in_place(|| {
            let mut effects = Callback {
                worker: self,
                slot: &slot,
                rt: tokio::runtime::Handle::current(),
            };
            callback::execute(&b, r, &mut row, &mut effects)
        });
        match result {
            Ok(receipt) => receipt,
            Err(callback::Failure::Ended) => team::ended(name),
            Err(callback::Failure::Ledger) => failed(name),
        }
    }

    async fn ask(&mut self, args: &Value) -> Value {
        let question = args["question"]
            .as_str()
            .unwrap_or("")
            .split(clarp_kernel::python::blank)
            .filter(|s| !s.is_empty())
            .collect::<Vec<_>>()
            .join(" ");
        let mut receipt = json!({"tool":"ask_my_agent"});
        if question.is_empty() || question.chars().count() > 4000 {
            receipt["status"] = json!("refused");
            receipt["reason"] = json!(if question.is_empty() {
                "empty question"
            } else {
                "question too long"
            });
            return receipt;
        }
        let contact = &self.call.snapshot["live_tools"]["contact"];
        let session = contact["session"].as_str().unwrap_or("");
        let id = team::operation(
            "sandra-",
            &[
                &self.app.server_id,
                &self.call.principal,
                &self.call.call_id,
                session,
                &question.to_lowercase(),
            ],
        );
        if self.asked.contains(&id) || self.answers.contains(&id) {
            return json!({"tool":"ask_my_agent","status":"already_asked","operation_id":id,"note":"already asked in this call; the answer comes back by itself (read_result replays it)"});
        }
        let name = self.call.snapshot["profile"]["display_name"]
            .as_str()
            .filter(|s| !s.is_empty())
            .unwrap_or("the agent");
        let request = format!(
            "Spørsmål fra {name}s Live-samtale med Peter (svar kort og muntlig, på norsk; slå opp det som trengs med verktøyene dine): {question}"
        );
        let row = match crate::oracle::live_ask(
            &self.app,
            &self.call.principal,
            (&id, session, &request),
        )
        .await
        {
            Ok(row) => row,
            Err(_) => {
                match crate::oracle::live_rows(&self.app, &[id.clone()].into_iter().collect()).await
                {
                    Ok(rows) => match rows.first() {
                        Some(row) => row.told(),
                        None => {
                            return json!({"tool":"ask_my_agent","status":"failed","reason":"RuntimeError","note":"the question could not be sent; say so"});
                        }
                    },
                    Err(_) => {
                        return json!({"tool":"ask_my_agent","status":"unknown","operation_id":id,"reason":"RuntimeError","note":"not known whether the question went out; not resent; say so"});
                    }
                }
            }
        };
        self.asked.insert(id.clone());
        json!({"tool":"ask_my_agent","status":"asked","operation_id":id,"regi_status":Value::Null,"reason":if row["status"]=="failed"{row["error"].clone()}else{Value::Null},"note":"say briefly that you are checking (\"jeg sjekker\"); the answer comes back in parts"})
    }

    pub fn adopt(&mut self, rows: Vec<clarp_kernel::oracle::delegations::Delegation>) {
        for row in rows {
            if row.status.active()
                || row.owner_principal != self.call.principal
                || !self.asked.contains(&row.delegation_id)
            {
                continue;
            }
            let name = self.call.snapshot["live_tools"]["contact"]["name"].as_str();
            if let Ok(base) = clarp_kernel::oracle::relay::result_relay(&row, name)
                && self
                    .answers
                    .queue(
                        &base.key,
                        &base.label,
                        &base.source,
                        &base.text,
                        &base.request,
                    )
                    .is_ok()
            {
                self.asked.remove(&row.delegation_id);
            }
        }
    }

    pub async fn execute(&mut self, tool: &str, args: &Value, explicit: bool) -> Value {
        if !self.current().await {
            return team::ended(tool);
        }
        if tool == "ask_my_agent" {
            return self.ask(args).await;
        }
        if tool == "read_result" {
            let receipt = self.answers.read(args);
            if receipt["status"] != "nothing_to_read"
                && let Some(part) = self
                    .answers
                    .due(self.began.elapsed().as_secs_f64(), 0.0, 0.0)
            {
                let (done, delivered) = tokio::sync::oneshot::channel();
                let msg = json!({"type":format!("session.{}.append",part.channel),"content":part.text,"delegation_id":Value::Null});
                let sent = if self.replay.send((msg, done)).is_ok() {
                    delivered.await.unwrap_or(Some(false))
                } else {
                    Some(false)
                };
                if self
                    .answers
                    .delivered(&part, sent, self.began.elapsed().as_secs_f64())
                    && self.current().await
                    && crate::oracle::acknowledge_live(&self.app, &part.id, &self.call.principal)
                        .await
                        .unwrap_or(false)
                {
                    self.answers.acknowledged(&part.id);
                }
            }
            return receipt;
        }
        if matches!(tool, "schedule_callback" | "cancel_callback") {
            return self.callback(tool, args, explicit).await;
        }
        let cfg = &self.call.snapshot["live_tools"];
        let members = cfg["agents"].as_array().map_or(&[][..], Vec::as_slice);
        let proposed = self.team.propose(members, tool, args);
        match proposed.action {
            Action::Answer(answer) => answer,
            Action::Status | Action::Transcript(_) => {
                let limit = if let Action::Transcript(n) = proposed.action {
                    n
                } else {
                    3
                };
                let data = crate::oracle::live_read(
                    &self.app,
                    &self.call.principal,
                    tool,
                    proposed.session,
                    limit,
                )
                .await;
                team::read(tool, proposed.name, data)
            }
            Action::Delegate {
                request,
                normalized,
            } => {
                let b = self.binding();
                let nonce = crate::ids::hex(16);
                let id = team::operation("rq-", &[b.host, b.call, proposed.session, &nonce]);
                let mut row = json!({"kind":"delegate_request","status":"claimed","status_text":"requesting","at":self.app.clock.now_ms() as f64/1000.0,"to":proposed.session,"host_id":b.host,"principal":b.principal,"agent_session":b.agent,"call_id":b.call});
                if !self.save(&id, &row).await {
                    return failed(tool);
                }
                if !self.current().await {
                    return team::ended(tool);
                }
                let receipt = if cfg["delegate"]["kind"] == "prompt" {
                    let name = self.call.snapshot["profile"]["display_name"]
                        .as_str()
                        .filter(|s| !s.is_empty())
                        .unwrap_or("the agent");
                    let args = [
                        "--to".into(),
                        proposed.session.into(),
                        "--from".into(),
                        b.agent.into(),
                        "--text".into(),
                        team::prompt(name, &normalized),
                    ];
                    let result = crate::admin_tool::prompt(&self.app, &args).await;
                    let verdict = match result.exit {
                        0 => Prompt::Sent,
                        4 => Prompt::Refused(&result.stderr),
                        _ => Prompt::Uncertain(&result.stderr),
                    };
                    team::prompted(proposed.name, &id, verdict)
                } else {
                    let body = json!({"call_id":b.call,"principal":b.principal,"from":b.agent,"agent":proposed.session,"request":request});
                    let answer =
                        tokio::task::block_in_place(|| post(&cfg["delegate"]["url"], &body, 30));
                    let outcome = match answer {
                        Some((code, answer)) => receipts::director(&answer, Some(i64::from(code))),
                        None => {
                            json!({"state":"unconfirmed","status":"unknown_delivery","note":"regi's answer was unclear; not retried"})
                        }
                    };
                    let mut receipt = json!({"tool":tool,"agent":proposed.name,"request_id":id});
                    if let Some(fields) = outcome.as_object() {
                        for (k, v) in fields {
                            if k != "state" && !v.is_null() {
                                receipt[k] = v.clone();
                            }
                        }
                    }
                    receipt
                };
                row["status_text"] = receipt["status"].clone();
                row["status"] = json!(match receipt["status"].as_str() {
                    Some("refused" | "not_forwarded") => "not_done",
                    Some("unknown_delivery") => "unconfirmed",
                    _ => "done",
                });
                row["receipt"] = receipt.clone();
                let _ = self.save(&id, &row).await;
                if let Some(intent) = receipt["intent"].as_str().filter(|s| !s.is_empty())
                    && !receipt["sesjon"].is_null()
                {
                    let key = team::operation(
                        "dl-",
                        &[b.host, b.principal, b.agent, proposed.session, intent],
                    );
                    let mutex = self.app.calls.tool_slot(&key);
                    let _guard = mutex.lock().await;
                    let mut known = row.clone();
                    known["kind"] = json!("delegate_intent");
                    known["request_id"] = json!(id);
                    if self.save(&key, &known).await
                        && receipt["status"] == "queued_at_regi"
                        && let Some(url) =
                            live_call::on_this_machine(cfg["delegate"].get("reconcile_url"))
                    {
                        self.reconcile(
                            key,
                            url.into(),
                            receipt.clone(),
                            id,
                            proposed.name.to_owned(),
                        );
                    }
                }
                receipt
            }
        }
    }

    fn reconcile(&self, key: String, url: String, receipt: Value, attempt: String, agent: String) {
        let (app, active, call) = (self.app.clone(), self.active.clone(), self.call.clone());
        let notes = self.reconciled.clone();
        tokio::spawn(async move {
            tokio::time::sleep(Duration::from_secs(15)).await;
            if !active.load(Ordering::Acquire)
                || !app
                    .calls
                    .step(&app, (&call.call_id, call.call_epoch), |b| b.holds())
                    .await
                    .unwrap_or(false)
            {
                return;
            }
            let session = receipt["sesjon"]
                .as_i64()
                .or_else(|| receipt["sesjon"].as_str()?.parse().ok());
            let Some(session) = session else {
                return;
            };
            let encode = |s: &str| clarp_kernel::portraits::quoted(s).replace("%20", "+");
            let url = format!(
                "{url}?sesjon={session}&principal={}&intent={}",
                encode(&call.principal),
                encode(receipt["intent"].as_str().unwrap_or(""))
            );
            let answer = tokio::task::spawn_blocking(move || {
                let agent: ureq::Agent = ureq::Agent::config_builder()
                    .proxy(None)
                    .max_redirects(0)
                    .timeout_global(Some(Duration::from_secs(30)))
                    .build()
                    .into();
                let mut response = agent.get(&url).call().ok()?;
                response
                    .body_mut()
                    .with_config()
                    .limit(65536)
                    .read_json::<Value>()
                    .ok()
            })
            .await
            .ok()
            .flatten();
            let Some(answer) = answer else {
                return;
            };
            let mutex = app.calls.tool_slot(&key);
            let _guard = mutex.lock().await;
            let Some(mut row) = app
                .fleet
                .kept("live_tools", key.clone())
                .await
                .ok()
                .flatten()
                .and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok())
            else {
                return;
            };
            if row["request_id"] != attempt {
                return;
            }
            let status = match answer["intent_status"].as_str() {
                Some("sendt") if answer["sent"] == true => "forwarded",
                Some("sender" | "venter") => "queued_at_regi",
                Some("feil") => "not_forwarded",
                _ => "unknown_delivery",
            };
            row["status_text"] = json!(status);
            row["reconciled"] = json!(true);
            row["intent_status"] = answer["intent_status"].clone();
            if app
                .fleet
                .keep("live_tools", key, Some(row.to_string().into_bytes()))
                .await
                .is_ok()
                && status == "forwarded"
                && active.load(Ordering::Acquire)
            {
                let note = format!(
                    "Host note (reconciled, untrusted data): {} Mention it only if it matters.",
                    clarp_kernel::python::dumps_ascii(&json!({"agent":agent,"status":"forwarded"}))
                );
                let _ = notes.send(note);
            }
        });
    }

    pub async fn route(&mut self, key: &str, route: &Route) -> Option<Vec<Value>> {
        if !self.current().await {
            return None;
        }
        let response = crate::oracle::route_voice(&self.app, key, route.body.clone()).await?;
        let mut receipts = Vec::new();
        for item in response["output"]
            .as_array()
            .into_iter()
            .flatten()
            .filter(|it| it["type"] == "function_call")
            .take(8)
        {
            let args = item["arguments"]
                .as_str()
                .and_then(|s| serde_json::from_str::<Value>(s).ok())
                .filter(Value::is_object)
                .unwrap_or_else(|| json!({}));
            let receipt = self
                .execute(item["name"].as_str().unwrap_or(""), &args, true)
                .await;
            if receipt["status"] == "call_ended" {
                receipts.push(receipt);
                break;
            }
            if matches!(
                receipt["status"].as_str(),
                Some(
                    "forwarded"
                        | "queued_at_regi"
                        | "already_present"
                        | "unknown_delivery"
                        | "scheduled"
                        | "already_scheduled"
                        | "cancelled"
                        | "nothing_pending"
                )
            ) {
                self.taken.push(receipt.clone());
            }
            receipts.push(receipt);
        }
        if receipts.iter().any(|r| {
            matches!(
                r["tool"].as_str(),
                Some("schedule_callback" | "cancel_callback")
            )
        }) {
            self.auto_decided = true;
        }
        let auto = &self.call.snapshot["live_tools"]["callback_on_delegation"];
        if !auto.is_null()
            && !self.auto_decided
            && receipts.iter().any(|r| {
                r["tool"] == "delegate_to_agent"
                    && matches!(
                        r["status"].as_str(),
                        Some("forwarded" | "queued_at_regi" | "already_present")
                    )
            })
        {
            self.auto_decided = true;
            let added = self
                .execute(
                    "schedule_callback",
                    &json!({"minutes":auto["minutes"],"reason":"status etter utdeling"}),
                    false,
                )
                .await;
            if matches!(
                added["status"].as_str(),
                Some("scheduled" | "already_scheduled")
            ) {
                self.taken.push(added.clone());
            }
            receipts.push(added);
        }
        Some(receipts)
    }
}

fn failed(tool: &str) -> Value {
    json!({"tool":tool,"status":"failed","error":"ledger unavailable"})
}

fn post(url: &Value, body: &Value, seconds: u64) -> Option<(u16, Value)> {
    let url = live_call::on_this_machine(Some(url))?;
    let agent: ureq::Agent = ureq::Agent::config_builder()
        .proxy(None)
        .max_redirects(0)
        .timeout_global(Some(Duration::from_secs(seconds)))
        .http_status_as_error(false)
        .build()
        .into();
    let mut result = agent.post(url).send_json(body).ok()?;
    let code = result.status().as_u16();
    if matches!(code, 300..=399 | 404 | 405) {
        return None;
    }
    let data = result
        .body_mut()
        .with_config()
        .limit(65536)
        .read_to_vec()
        .ok()?;
    let reply = if data.is_empty() {
        json!({})
    } else {
        serde_json::from_slice(&data).ok()?
    };
    if !reply.is_object() {
        return None;
    }
    Some((
        if (200..300).contains(&code) {
            200
        } else {
            code
        },
        reply,
    ))
}

struct Callback<'a> {
    worker: &'a Worker,
    slot: &'a str,
    rt: tokio::runtime::Handle,
}
impl callback::Director for Callback<'_> {
    fn post(&mut self, body: &Value) -> Result<Option<(u16, Value)>, callback::Failure> {
        if !self.rt.block_on(self.worker.current()) {
            return Err(callback::Failure::Ended);
        }
        Ok(post(
            &self.worker.call.snapshot["live_tools"]["callback"]["url"],
            body,
            10,
        ))
    }
    fn save(&mut self, row: &Value) -> Result<(), callback::Failure> {
        let mut row = row.clone();
        let now = self.worker.app.clock.now_ms() as f64 / 1000.0;
        if row.get("at").is_none() {
            row["at"] = json!(now);
        }
        row["updated_at"] = json!(now);
        self.rt
            .block_on(self.worker.save(self.slot, &row))
            .then_some(())
            .ok_or(callback::Failure::Ledger)
    }
    fn time(&self, epoch: &Value) -> String {
        epoch
            .as_f64()
            .map_or_else(|| "?".into(), |s| time((s * 1000.0) as i64))
    }
}
