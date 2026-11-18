//! An answering phone chooses whether its captured greeting or an initiative
//! from the agent opens the call. All effects are returned for the Host's
//! transaction. Only an answer or claim by the owner gets the token and nonce.
use crate::python;
use serde_json::{Value, json};

pub const MODE: &str = "listen_first";

pub fn uuid(bytes: &[u8]) -> Option<&str> {
    let raw = std::str::from_utf8(bytes).ok()?;
    let text = raw.strip_suffix('\n').unwrap_or(raw);
    (text.len() == 36
        && text.bytes().enumerate().all(|(i, c)| {
            if matches!(i, 8 | 13 | 18 | 23) {
                c == b'-'
            } else {
                c.is_ascii_digit() || (b'a'..=b'f').contains(&c)
            }
        }))
    .then_some(text)
}

pub fn breach(stored: &Value, generation: &Value) -> Option<&'static str> {
    if generation.is_null()
        || ["boot", "instance"]
            .iter()
            .any(|k| stored[*k].as_str().is_none_or(str::is_empty))
    {
        Some("host_identity_unprovable")
    } else if stored["boot"] != generation["boot"] {
        Some("host_rebooted")
    } else if stored["instance"] != generation["instance"] {
        Some("db_restored")
    } else {
        None
    }
}

fn open(state: &Value) -> bool {
    matches!(
        state.as_str(),
        Some("pending" | "listening" | "speech_latched")
    )
}

pub fn public(op: &Value, secret: bool) -> Value {
    let mut shown = json!({"mode":MODE});
    for k in [
        "ring_id",
        "call_id",
        "epoch",
        "state",
        "silence_ms",
        "session",
        "persona",
        "server_id",
    ] {
        shown[k] = op[k].clone();
    }
    if secret && python::truthy(&op["token"]) {
        for k in ["token", "owner_nonce"] {
            shown[k] = op[k].clone();
        }
    }
    shown["intent"] = if python::truthy(&op["intent"]) {
        let mut intent = json!({});
        for k in ["kind", "admission_id", "gen", "state"] {
            intent[k] = op["intent"][k].clone();
        }
        intent
    } else {
        Value::Null
    };
    if python::truthy(&op["late_greeting"]) {
        shown["greeting"] = json!("ordinary");
    }
    if python::truthy(&op["retired"]) {
        shown["retired"] = op["retired"]["reason"].clone();
    }
    shown
}

pub struct World<'a> {
    pub generation: &'a Value,
    pub now: i64,
    pub host: &'a str,
    pub fence: Option<&'a str>,
    pub queue: &'a Value,
    pub nonce: &'a str,
    pub trace: &'a str,
}

#[derive(Default)]
pub struct Effects {
    pub dirty: bool,
    pub admit: bool,
    pub withdraw: bool,
    pub unknown: bool,
    pub announce: bool,
}

impl Effects {
    /// Dropping a queued initiative cannot affect an ordinary send which
    /// happened to use the same id. A claimed turn is fenced by its launcher.
    pub fn takes_waiter(&self, row: &Value, trace: &Value) -> bool {
        (self.withdraw || self.unknown)
            && row["foreign"] != true
            && row["trace_id"] == *trace
            && matches!(row["status"].as_str(), Some("queued" | "parked"))
    }
}

fn int(value: &Value) -> Value {
    if value.is_i64() || value.is_u64() {
        value.clone()
    } else {
        Value::Null
    }
}
fn error(status: u16, why: &str) -> (u16, Value) {
    (status, json!({"ok":false,"error":why}))
}
fn refused(op: &Value, status: u16, why: &str, extra: Value) -> (u16, Value) {
    let mut body = json!({"ok":false,"error":why,"opening":public(op,false)});
    if let Some(fields) = extra.as_object() {
        for (k, v) in fields {
            body[k] = v.clone();
        }
    }
    (status, body)
}

fn supersede(op: &mut Value, row: &Value, effects: &mut Effects) -> &'static str {
    if python::truthy(&op["identity_breach"]) {
        return "unknown";
    }
    let intent = &mut op["intent"];
    let verdict = match intent["state"].as_str() {
        Some("started") => return "started",
        Some("unknown") => return "unknown",
        Some("cancelled") => return "cancelled",
        Some("intended") => "cancelled",
        _ if row.is_null() => "cancelled",
        _ if row["trace_id"] != intent["trace_id"] => "unknown",
        _ => match row["status"].as_str() {
            Some("queued" | "parked") if row["claimed_at"].is_null() => {
                effects.withdraw = true;
                "cancelled"
            }
            Some("withdrawn" | "cancelled" | "claimed") => "cancelled",
            _ => "unknown",
        },
    };
    intent["state"] = json!(verdict);
    if verdict == "unknown" {
        effects.withdraw = matches!(row["status"].as_str(), Some("queued" | "parked"))
            && row["claimed_at"].is_null();
        op["state"] = json!("unknown");
    }
    verdict
}

pub fn retire(op: &mut Value, reason: &str, w: &World<'_>, effects: &mut Effects) -> bool {
    if python::truthy(&op["retired"])
        || op["state"] == "fallback"
        || python::truthy(&op["identity_breach"])
    {
        return false;
    }
    if open(&op["state"]) || op["state"] == "unknown" {
        op["state"] = json!("retired");
    } else if op["state"] == "initiative_queued" {
        let verdict = supersede(op, w.queue, effects);
        op["state"] = json!(if verdict == "started" {
            "initiative_started"
        } else {
            "retired"
        });
    }
    op["retired"] = json!({"reason":reason,"at":w.now});
    effects.dirty = true;
    true
}

fn identity(op: &mut Value, w: &World<'_>, effects: &mut Effects) -> Option<&'static str> {
    let cause = breach(&op["fence"]["host_gen"], w.generation)?;
    let intent = &op["intent"];
    let history = op["state"] == "fallback"
        || if python::truthy(intent) {
            matches!(intent["state"].as_str(), Some("cancelled" | "unknown"))
                || matches!(
                    w.queue["status"].as_str(),
                    Some("done" | "failed" | "interrupted" | "ended")
                )
        } else {
            !open(&op["state"])
        };
    if !history {
        if !python::truthy(&op["retired"]) {
            op["retired"] = json!({"reason":cause,"at":w.now});
        }
        op["state"] = json!("unknown");
        if matches!(
            op["intent"]["state"].as_str(),
            Some("intended" | "admitted" | "started")
        ) {
            op["intent"]["state"] = json!("unknown");
            effects.unknown = matches!(
                w.queue["status"].as_str(),
                Some("queued" | "parked" | "claimed")
            );
        }
        if !python::truthy(&op["identity_breach"]) {
            op["identity_breach"] = json!({"cause":cause,"at":w.now});
        }
        effects.dirty = true;
    }
    Some(cause)
}

fn intent(op: &mut Value, cause: &str, trace: &str) {
    let generation = op["intent"]["gen"].as_i64().unwrap_or(0) + 1;
    op["state"] = json!("initiative_queued");
    op["intent"] = json!({"kind":"initiative","admission_id":format!("call-open:{}:{generation}",op["ring_id"].as_str().unwrap_or("")),"gen":generation,"state":"intended","cause":cause,"trace_id":trace});
}

fn fingerprint(body: &Value) -> String {
    let mut fields = json!({});
    for k in [
        "claim",
        "epoch",
        "expected_epoch",
        "owner_nonce",
        "reason",
        "token",
    ] {
        fields[k] = body[k].clone();
    }
    python::dumps_ascii(&fields)
}

pub fn claim(
    op: &mut Value,
    owner: &str,
    body: &Value,
    w: &World<'_>,
    fx: &mut Effects,
) -> (u16, Value) {
    let kind = body["claim"].as_str().unwrap_or("");
    let id = body["request_id"].as_str().unwrap_or("");
    if !matches!(
        kind,
        "renew"
            | "listening"
            | "speech_started"
            | "speech_abandoned"
            | "silence"
            | "no_mic"
            | "not_ready"
    ) || python::strip(id).is_empty()
        || id.chars().count() > 128
        || if kind == "renew" {
            int(&body["expected_epoch"]).is_null() || !body["owner_nonce"].is_string()
        } else {
            int(&body["epoch"]).is_null()
        }
    {
        return error(400, "bad claim");
    }
    if op.is_null()
        || op["token"] != body["token"]
        || !body["token"]
            .as_str()
            .is_some_and(|s| s.starts_with("opn_"))
    {
        return error(404, "unknown opening");
    }
    if op["principal"] != owner {
        return refused(op, 403, "not the answering device", Value::Null);
    }
    if let Some(cause) = identity(op, w, fx) {
        return refused(op, 409, "opening retired", json!({"reason":cause}));
    }
    let fp = fingerprint(body);
    if let Some(prior) = op["requests"].get(id) {
        if prior["fingerprint"] != fp {
            return refused(op, 400, "bad claim", Value::Null);
        }
        let shown = if kind == "renew" {
            prior["opening"].clone()
        } else {
            public(op, true)
        };
        return (200, json!({"ok":true,"opening":shown,"changed":false}));
    }
    if !python::truthy(&op["retired"]) && op["server_id"] != w.host {
        retire(op, "host_changed", w, fx);
    }
    if python::truthy(&op["retired"]) {
        return refused(
            op,
            409,
            "opening retired",
            json!({"reason":op["retired"]["reason"]}),
        );
    }
    let mut changed = true;
    let latch = || json!({"epoch":body["epoch"],"at_ms":int(&body["at_ms"]),"capture_gen":int(&body["capture_gen"])});
    if kind == "renew" {
        if !open(&op["state"]) {
            return refused(op, 409, "already resolved", json!({"outcome":op["state"]}));
        }
        if op["epoch"] != body["expected_epoch"] {
            return refused(op, 409, "stale epoch", Value::Null);
        }
        if op["owner_nonce"] != body["owner_nonce"] {
            return refused(op, 409, "owner changed", Value::Null);
        }
        op["epoch"] = json!(op["epoch"].as_i64().unwrap_or(1) + 1);
        op["owner_nonce"] = json!(w.nonce);
    } else if op["epoch"] != body["epoch"] {
        return refused(op, 409, "stale epoch", Value::Null);
    } else if kind == "speech_started" && op["state"] == "initiative_queued" {
        let verdict = supersede(op, w.queue, fx);
        if verdict != "cancelled" {
            if verdict == "started" {
                op["state"] = json!("initiative_started");
            }
            fx.dirty = true;
            return refused(op, 409, "already resolved", json!({"outcome":op["state"]}));
        }
        op["state"] = json!("speech_latched");
        op["latch"] = latch();
    } else if !open(&op["state"]) {
        return refused(op, 409, "already resolved", json!({"outcome":op["state"]}));
    } else {
        match kind {
            "listening" => {
                op["capture"] =
                    json!({"capture_gen":int(&body["capture_gen"]),"at_ms":int(&body["at_ms"])});
                if op["state"] == "pending" {
                    op["state"] = json!("listening");
                }
            }
            "speech_started" => {
                if op["state"] == "speech_latched" {
                    changed = false;
                } else {
                    op["state"] = json!("speech_latched");
                    op["latch"] = latch();
                }
            }
            "speech_abandoned"
                if op["state"] != "speech_latched"
                    || !matches!(
                        body["reason"].as_str(),
                        Some("empty_capture" | "capture_aborted")
                    ) =>
            {
                return refused(op, 400, "bad claim", Value::Null);
            }
            "speech_abandoned" => {
                intent(op, kind, w.trace);
                fx.admit = true;
            }
            _ if op["state"] == "speech_latched" => {
                return refused(op, 409, "speech latched", Value::Null);
            }
            "silence"
                if ["at_ms", "watermark_ms"].iter().any(|k| {
                    int(&body[*k]).as_i64().unwrap_or(-1)
                        < op["silence_ms"].as_i64().unwrap_or(1250)
                }) =>
            {
                return refused(op, 400, "bad claim", Value::Null);
            }
            _ => {
                intent(op, kind, w.trace);
                fx.admit = true;
            }
        }
    }
    fx.dirty = true;
    if fx.admit
        && let Some(reason) = w.fence
    {
        fx.admit = false;
        retire(op, reason, w, fx);
        return refused(op, 409, "opening retired", json!({"reason":reason}));
    }
    let result = public(op, true);
    let mut seen = json!({"fingerprint":fp,"at":w.now});
    if kind == "renew" {
        seen["opening"] = result.clone();
    }
    op["requests"][id] = seen;
    if let Some(requests) = op["requests"].as_object_mut()
        && requests.len() > 64
        && let Some(old) = requests
            .iter()
            .min_by_key(|(_, v)| v["at"].as_i64())
            .map(|(k, _)| k.clone())
    {
        requests.shift_remove(&old);
    }
    fx.announce = changed;
    (200, json!({"ok":true,"opening":result,"changed":changed}))
}

pub struct Greeting<'a> {
    pub principal: &'a str,
    pub token: &'a Value,
    pub epoch: &'a Value,
    pub message: &'a str,
    pub trace: &'a str,
    pub session: &'a str,
    pub call: &'a str,
}

pub fn greet(op: &mut Value, g: Greeting<'_>, w: &World<'_>, fx: &mut Effects) -> (u16, Value) {
    if int(g.epoch).is_null() {
        return error(400, "bad call_opening");
    }
    if op.is_null()
        || op["token"] != *g.token
        || !g.token.as_str().is_some_and(|t| t.starts_with("opn_"))
    {
        return error(404, "unknown opening");
    }
    if op["principal"] != g.principal {
        return refused(op, 403, "not the answering device", Value::Null);
    }
    if let Some(cause) = identity(op, w, fx) {
        return refused(op, 409, "opening retired", json!({"reason":cause}));
    }
    if op["state"] == "greeting"
        && op["greeting"]["client_msg_id"] == g.message
        && !python::truthy(&op["retired"])
    {
        return (
            200,
            json!({"route":"greeting","session":op["session"],"opening":public(op,false),"changed":false}),
        );
    }
    if !python::truthy(&op["retired"]) && op["server_id"] != w.host {
        retire(op, "host_changed", w, fx);
    }
    if python::truthy(&op["retired"]) {
        return refused(
            op,
            409,
            "opening retired",
            json!({"reason":op["retired"]["reason"]}),
        );
    }
    if op["session"] != g.session || op["call_id"] != g.call {
        return refused(op, 409, "wrong call", Value::Null);
    }
    if op["epoch"] != *g.epoch {
        return refused(op, 409, "stale epoch", Value::Null);
    }
    if matches!(op["state"].as_str(), Some("greeting" | "fallback")) {
        return refused(op, 409, "already resolved", json!({"outcome":op["state"]}));
    }
    let verdict = match op["state"].as_str() {
        Some("initiative_queued") => supersede(op, w.queue, fx),
        Some("initiative_started") => "started",
        Some("unknown") => "unknown",
        _ => "greeting",
    };
    let route = match verdict {
        "started" => {
            op["state"] = json!("initiative_started");
            if !python::truthy(&op["late_greeting"]) {
                op["late_greeting"] =
                    json!({"client_msg_id":g.message,"trace_id":g.trace,"at":w.now});
            }
            "ordinary"
        }
        "unknown" => "ordinary",
        _ => {
            op["state"] = json!("greeting");
            op["greeting"] = json!({"client_msg_id":g.message,"trace_id":g.trace,"admission":"pending","at":w.now});
            "greeting"
        }
    };
    fx.dirty = true;
    fx.announce = true;
    (
        200,
        json!({"route":route,"session":op["session"],"opening":public(op,false),"changed":true}),
    )
}

pub fn greeting_admitted(op: &mut Value, id: &str, ok: bool, w: &World<'_>, fx: &mut Effects) {
    if identity(op, w, fx).is_some() {
        return;
    }
    if op["greeting"]["client_msg_id"] == id && op["greeting"]["admission"] != "confirmed" {
        op["greeting"]["admission"] = json!(if ok { "confirmed" } else { "unknown" });
        fx.dirty = true;
    }
}

/// Identity and stop evidence captured by the Host at the answer. A generation
/// read is side-effect free, including when the instance token is absent.
pub trait Proof {
    fn generation(&self) -> Value;
    fn stop(&self, agent: &str) -> (u64, bool);
    fn auth(&self, principal: &str) -> Value;
}

pub struct Record<'a> {
    pub ring: &'a super::Ring,
    pub principal: &'a str,
    pub host: &'a str,
    pub token: &'a str,
    pub nonce: &'a str,
    pub now: i64,
    pub generation: Value,
}

pub fn record(new: Record<'_>, proof: &dyn Proof) -> Value {
    let r = new.ring;
    let (stop, paused) = proof.stop(&r.agent_id);
    json!({"ring_id":r.ring_id,"call_id":r.call_id,"principal":new.principal,"server_id":new.host,
          "agent_id":r.agent_id,"session":r.session,"persona":r.persona,"reason":r.reason,
          "token":new.token,"owner_nonce":new.nonce,"epoch":1,"state":if new.token.is_empty(){"fallback"}else{"pending"},
          "silence_ms":1250,"capture":null,"latch":null,"intent":null,"greeting":null,"late_greeting":null,
          "retired":null,"requests":{},"created_at":new.now,"updated_at":new.now,"fence":{
            "answered_at":new.now,"stop_generation":stop,"paused_at_answer":paused,"auth":proof.auth(new.principal),"host_gen":new.generation}})
}

/// Validate the older admission and current session without leaking a nonce
/// in a denial. The newer Stop wins even if the mailbox was already paused.
pub fn fence<'a>(
    ring: &'a super::Ring,
    present: bool,
    calls: &crate::calls::Calls,
    host: &str,
    proof: &dyn Proof,
) -> Option<&'a str> {
    let op = &ring.opening;
    if let Some(reason) = op["retired"]["reason"].as_str() {
        return Some(reason);
    }
    if op["server_id"] != host {
        return Some("host_changed");
    }
    let owner = op["principal"].as_str().unwrap_or("");
    if !present
        || ring.state != "answered"
        || ring.answered_by != owner
        || op["call_id"] != ring.call_id
    {
        return Some("ring_changed");
    }
    if !calls
        .of(owner)
        .iter()
        .any(|c| c.call_id == ring.call_id && c.state == "live")
    {
        return Some("call_ended");
    }
    let saved = &op["fence"]["auth"];
    if !saved.is_null() {
        let current = proof.auth(owner);
        if current.is_null() {
            return Some("auth_revoked");
        }
        if &current != saved {
            return Some("auth_changed");
        }
    }
    if let Some(stop) = op["fence"]["stop_generation"].as_u64()
        && proof.stop(&ring.agent_id).0 != stop
    {
        return Some("stopped_after_answer");
    }
    None
}

impl super::Rings {
    pub fn opening_records(&self) -> impl Iterator<Item = &super::Ring> {
        self.rings
            .iter()
            .chain(self.archived.iter())
            .filter(|r| !r.opening.is_null())
    }

    pub fn attach_opening(&mut self, ring: super::Ring) {
        self.archived.push(ring);
    }

    pub fn detach_openings(&mut self) -> Vec<super::Ring> {
        std::mem::take(&mut self.archived)
    }

    pub fn by_opening_token(&self, token: &Value) -> Option<&super::Ring> {
        let token = token.as_str().filter(|s| s.starts_with("opn_"))?;
        self.opening_records().find(|r| r.opening["token"] == token)
    }

    /// A refusal may itself change the opening (e.g. an identity breach).
    /// Apply and persist those changes before returning the refusal.
    pub fn change_opening<T>(
        &mut self,
        id: &str,
        now: i64,
        out: &mut super::Effects,
        apply: impl FnOnce(&mut super::Ring, &mut Effects) -> T,
    ) -> Option<(T, Effects)> {
        let archived = self.archived.iter().any(|r| r.ring_id == id);
        let ring = self
            .rings
            .iter_mut()
            .chain(self.archived.iter_mut())
            .find(|r| r.ring_id == id && !r.opening.is_null())?;
        let mut changed = Effects::default();
        let answer = apply(ring, &mut changed);
        if changed.dirty {
            ring.opening["updated_at"] = json!(now);
            out.changed.rings = true;
        }
        if changed.announce && !archived {
            out.events.push(ring.event(now));
        }
        Some((answer, changed))
    }

    pub fn opening_audit(
        &mut self,
        id: &str,
        claim: &str,
        principal: &str,
        route: Option<&str>,
        now: i64,
        out: &mut super::Effects,
    ) {
        let mut event = json!({"outcome":"opening","ring_id":id,"claim":claim,"by":principal});
        if let Some(route) = route {
            event["route"] = json!(route);
        }
        self.write(event, now, out);
    }
}

pub fn header(op: &Value) -> String {
    let cause=match op["intent"]["cause"].as_str(){
        Some("silence")=>format!("They answered and have not spoken within about {} seconds of listening.",op["silence_ms"].as_i64().unwrap_or(1250) as f64/1000.0),
        Some("no_mic")=>"They answered, but their microphone is unavailable, so they cannot speak to you yet; they may reply by text.".into(),
        Some("not_ready")=>"They answered, but their microphone could not start.".into(),
        _=>"They answered and started to speak, but nothing usable was captured.".into()
    };
    format!(
        "<clarp-call-opening>\n[Group call] You called the user about: {}.\n{cause}\nOpen the conversation briefly in your own words; do not assume what they said.\n</clarp-call-opening>",
        op["reason"].as_str().unwrap_or("")
    )
}

/// Adopt only an admission written for the call with its own trace. A row
/// from an ordinary client remains that client's, even under a reserved id.
/// The Host may have committed the intent immediately before its actor append.
pub fn admit(
    op: &mut Value,
    admission: &Value,
    recover: bool,
    w: &World<'_>,
    fx: &mut Effects,
) -> &'static str {
    if op.is_null() || identity(op, w, fx).is_some() {
        return "fenced";
    }
    let intent = &op["intent"];
    if !(intent["state"] == "intended" || recover && intent["state"] == "admitted")
        || !op["retired"].is_null()
    {
        return "fenced";
    }
    if let Some(reason) = w.fence {
        retire(op, reason, w, fx);
        return "fenced";
    }
    let trace = &intent["trace_id"];
    fx.dirty = true;
    if !admission.is_null() && (admission["channel"] != "call" || &admission["trace_id"] != trace)
        || !w.queue.is_null() && &w.queue["trace_id"] != trace
    {
        op["intent"]["state"] = json!("unknown");
        op["state"] = json!("unknown");
        return "fenced";
    }
    op["intent"]["state"] = json!("admitted");
    if admission.is_null() {
        "admitted"
    } else {
        "adopted"
    }
}

/// A generation breach preserves terminal history, even during retirement.
pub fn retire_current(op: &mut Value, reason: &str, w: &World<'_>, effects: &mut Effects) -> bool {
    let previous = op["retired"].clone();
    if identity(op, w, effects).is_some() {
        if effects.dirty && previous.is_null() {
            op["retired"]["reason"] = json!(reason);
        }
        return effects.dirty;
    }
    retire(op, reason, w, effects)
}

pub fn clip(op: &Value, trace: &str) -> Option<(Value, bool)> {
    if trace.is_empty() {
        return None;
    }
    let initiative = op["intent"]["trace_id"] == trace;
    if !initiative && op["greeting"]["trace_id"] != trace {
        return None;
    }
    let kind = if initiative {
        "initiative"
    } else {
        "greeting_reply"
    };
    let admission = if initiative {
        &op["intent"]["admission_id"]
    } else {
        &op["greeting"]["client_msg_id"]
    };
    let skip = !op["retired"].is_null()
        || initiative
            && (matches!(
                op["intent"]["state"].as_str(),
                Some("unknown" | "cancelled")
            ) || python::truthy(&op["late_greeting"]));
    Some((
        json!({"call_id":op["call_id"],"opening":{"ring_id":op["ring_id"],"token":op["token"],"epoch":op["epoch"],"kind":kind,"admission_id":admission}}),
        skip,
    ))
}

#[derive(Default)]
pub struct Dispatch {
    pub opening: Effects,
    pub release: bool,
    pub withdraw_claimed: bool,
    pub claim: bool,
    pub launched_unknown: bool,
}

/// The dispatcher knows whether a backend was invoked. No operation may infer
/// that a launch was unsent merely from a timeout or a process disappearing.
#[derive(Clone, Copy, PartialEq, Eq)]
pub enum Gate {
    Claim,
    ReleaseUnsent,
    Parked,
    FailedAfterLaunch,
}

pub fn dispatch(
    action: Gate,
    op: &mut Value,
    id: &str,
    trace: &str,
    w: &World<'_>,
    fx: &mut Dispatch,
) -> Value {
    let intent = &op["intent"];
    let status = &w.queue["status"];
    let exact = intent["admission_id"] == id && intent["trace_id"] == trace;
    if w.queue["foreign"] == true && action != Gate::Claim {
        return if action == Gate::Parked {
            json!("unknown")
        } else {
            json!(false)
        };
    }
    match action {
        Gate::Claim => {
            if w.queue["foreign"] == true {
                return json!(true);
            }
            if w.queue.is_null() || !exact || w.queue["trace_id"] != trace {
                fx.opening.withdraw = matches!(status.as_str(), Some("queued" | "parked"));
                return json!(false);
            }
            if identity(op, w, &mut fx.opening).is_some() {
                return json!(false);
            }
            if op["intent"]["state"] == "started" {
                if !matches!(
                    status.as_str(),
                    Some("started" | "done" | "failed" | "interrupted" | "ended" | "unknown")
                ) {
                    if let Some(reason) = w.fence {
                        retire(op, reason, w, &mut fx.opening);
                    }
                    mark_unknown(op, &mut fx.opening);
                }
                return json!(false);
            }
            if op["intent"]["state"] != "admitted" || !op["retired"].is_null() {
                fx.opening.withdraw = matches!(status.as_str(), Some("queued" | "parked"));
                return json!(false);
            }
            if let Some(reason) = w.fence {
                retire(op, reason, w, &mut fx.opening);
                fx.opening.withdraw = matches!(status.as_str(), Some("queued" | "parked"));
                return json!(false);
            }
            if status != "queued" {
                return json!(false);
            }
            fx.claim = true;
            op["intent"]["state"] = json!("started");
            if op["state"] == "initiative_queued" {
                op["state"] = json!("initiative_started");
            }
            fx.opening.dirty = true;
            fx.opening.announce = true;
            json!(true)
        }
        Gate::ReleaseUnsent | Gate::Parked => {
            let refused = if action == Gate::Parked {
                json!("unknown")
            } else {
                json!(false)
            };
            if !exact || intent["state"] != "started" || status != "claimed" {
                return refused;
            }
            if identity(op, w, &mut fx.opening).is_some() {
                return refused;
            }
            if action == Gate::Parked {
                if let Some(reason) = w.fence {
                    retire(op, reason, w, &mut fx.opening);
                    fx.withdraw_claimed = true;
                    op["intent"]["state"] = json!("cancelled");
                    fx.opening.dirty = true;
                    fx.opening.announce = true;
                    return json!("cancelled");
                }
                return json!("launch");
            }
            if !op["retired"].is_null() {
                fx.withdraw_claimed = true;
                op["intent"]["state"] = json!("cancelled");
            } else {
                fx.release = true;
                op["intent"]["state"] = json!("admitted");
                if op["state"] == "initiative_started" {
                    op["state"] = json!("initiative_queued");
                }
            }
            fx.opening.dirty = true;
            fx.opening.announce = true;
            json!(true)
        }
        Gate::FailedAfterLaunch => {
            if !exact || intent["state"] != "started" {
                return json!(false);
            }
            if identity(op, w, &mut fx.opening).is_some() {
                return json!(fx.opening.dirty);
            }
            if let Some(reason) = w.fence {
                retire(op, reason, w, &mut fx.opening);
            }
            op["intent"]["state"] = json!("unknown");
            fx.launched_unknown = true;
            if op["retired"].is_null() {
                op["state"] = json!("unknown");
            }
            fx.opening.dirty = true;
            fx.opening.announce = true;
            json!(true)
        }
    }
}

fn mark_unknown(op: &mut Value, fx: &mut Effects) {
    op["intent"]["state"] = json!("unknown");
    fx.unknown = true;
    fx.dirty = true;
    fx.announce = true;
    if op["retired"].is_null() {
        op["state"] = json!("unknown");
    }
}
