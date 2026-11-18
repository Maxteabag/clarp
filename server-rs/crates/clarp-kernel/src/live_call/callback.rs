//! A callback belongs to the calling phone and the agent. The director owns its
//! timer. Uncertainty permits one identical POST with the same operation id.
use super::team::operation;
use crate::python;
use serde_json::{Value, json};

pub struct Binding<'a> {
    pub host: &'a str,
    pub principal: &'a str,
    pub agent: &'a str,
    pub call: &'a str,
}

impl Binding<'_> {
    pub fn slot(&self) -> String {
        operation("cb-", &[self.host, self.principal, self.agent])
    }
    fn body(&self, action: &str, id: &str) -> Value {
        json!({"action":action,"operation_id":id,"recipient_principal":self.principal,"agent_session":self.agent,"call_id":self.call,"host_id":self.host})
    }
}

pub enum Failure {
    Ended,
    Ledger,
}

pub trait Director {
    /// None is uncertain; a stopped call fences every attempted POST.
    fn post(&mut self, body: &Value) -> Result<Option<(u16, Value)>, Failure>;
    fn save(&mut self, row: &Value) -> Result<(), Failure>;
    fn time(&self, epoch: &Value) -> String;
}

fn send(d: &mut impl Director, body: &Value) -> Result<Option<(u16, Value)>, Failure> {
    for _ in 0..2 {
        if let Some((code, reply)) = d.post(body)?
            && code < 500
        {
            return Ok(Some((code, reply)));
        }
    }
    Ok(None)
}

fn string(value: &Value) -> String {
    value
        .as_str()
        .map_or_else(|| python::repr(value), str::to_owned)
}

fn note(result: &str, status: &Value, at: &str, failure: &Value) -> String {
    match status.as_str() {
        Some("fyrt") => format!("that call back has already gone out (at {at})"),
        Some("fyrer") => "the call back is being placed now (not confirmed)".into(),
        Some("fyrt_usikker") => {
            "that call back may already have gone out; regi is not sure, and it is never rung again"
                .into()
        }
        Some("avlyst") => "that call back was cancelled earlier; it will not ring".into(),
        Some("ikke_plassert") => {
            "that call back was not placed (another call was ringing); it is not tried again".into()
        }
        Some("feil") => format!(
            "that call back failed ({}); it is not tried again",
            if python::truthy(failure) {
                python::first(&string(failure), 120).to_owned()
            } else {
                "no reason given".into()
            }
        ),
        Some("venter") if result == "scheduled" => {
            format!("call back scheduled at {at} (never 'rang')")
        }
        Some("venter") => format!("an earlier call back is already pending at {at}"),
        _ => format!(
            "the call back's status is unknown ({}); it is neither confirmed pending nor rung",
            string(status)
        ),
    }
}

fn cancel(b: &Binding<'_>, row: &mut Value, d: &mut impl Director) -> Result<Value, Failure> {
    let pending = row["operation_id"].as_str().unwrap_or("");
    let id = operation("cbx-", &[b.host, b.principal, b.agent, b.call, pending]);
    let mut receipt = json!({"tool":"cancel_callback"});
    let Some((code, reply)) = send(d, &b.body("cancel", &id))? else {
        return Ok(
            json!({"tool":"cancel_callback","status":"unconfirmed","note":"not known whether it was cancelled; the call back may still come"}),
        );
    };
    let result = reply["result"].as_str().unwrap_or("");
    if code == 200 && reply["ok"] == true && matches!(result, "nothing_pending" | "cancelled") {
        if result == "cancelled" && !row.is_null() {
            row["status"] = json!("cancelled");
            row["regi_status"] = reply["status"].clone();
            d.save(row)?;
        }
        receipt["status"] = json!(result);
        if python::truthy(&reply["operation_id"]) {
            receipt["operation_id"] = reply["operation_id"].clone();
        }
    } else {
        receipt["status"] = json!("refused");
        receipt["error"] = reply
            .get("error")
            .filter(|v| python::truthy(v))
            .cloned()
            .unwrap_or(json!(format!("HTTP {code}")));
        receipt["note"] = json!("regi did not cancel it; the call back may still come");
    }
    Ok(receipt)
}

pub struct Request<'a> {
    pub tool: &'a str,
    pub args: &'a Value,
    pub config: &'a Value,
    pub explicit: bool,
    pub eligible: bool,
}

pub fn execute(
    b: &Binding<'_>,
    r: Request<'_>,
    row: &mut Value,
    d: &mut impl Director,
) -> Result<Value, Failure> {
    if r.config["callback"]["kind"] != "http" {
        return Ok(json!({"tool":r.tool,"status":"refused","error":"call backs are off"}));
    }
    if r.tool == "cancel_callback" {
        return cancel(b, row, d);
    }
    if !r.eligible {
        return Ok(
            json!({"tool":r.tool,"status":"refused","error":"the calling phone cannot take a Live call back"}),
        );
    }
    let bounds = &r.config["callback_minutes"];
    let minutes = r.args["minutes"]
        .as_i64()
        .unwrap_or_else(|| bounds["default"].as_i64().unwrap_or(5))
        .clamp(
            bounds["min"].as_i64().unwrap_or(1),
            bounds["max"].as_i64().unwrap_or(30),
        );
    let raw = if python::truthy(&r.args["reason"]) {
        string(&r.args["reason"])
    } else {
        String::new()
    };
    let reason = raw
        .split(python::blank)
        .filter(|s| !s.is_empty())
        .collect::<Vec<_>>()
        .join(" ");
    let reason = if reason.is_empty() {
        "status"
    } else {
        python::first(&reason, 200)
    };
    let minutes_text = minutes.to_string();
    let parts = [b.host, b.principal, b.agent, b.call, &minutes_text, reason];
    let mut id = operation("cb-", &parts);
    let known = row.clone();
    let change = r.explicit
        && known["status"] == "pending"
        && known["call_id"] == b.call
        && !known["minutes"].is_null()
        && known["minutes"] != minutes;
    let mut body = b.body("schedule", &id);
    body["minutes"] = json!(minutes);
    body["reason"] = json!(reason);
    if python::truthy(&known["operation_id"])
        && known["status"] == "pending"
        && (known["call_id"] != b.call || change)
    {
        body["supersedes"] = known["operation_id"].clone();
    }
    if change {
        body["explicit"] = json!(true);
    }
    if row.is_null() {
        *row = json!({"kind":"callback","status":"claimed"});
    }
    row["requesting"] = json!(id);
    row["host_id"] = json!(b.host);
    row["principal"] = json!(b.principal);
    row["agent_session"] = json!(b.agent);
    row["call_id"] = json!(b.call);
    d.save(row)?;
    let mut answer = send(d, &body)?;
    if let Some((409, reply)) = &answer
        && reply["error"] == "supersedes_mismatch"
    {
        body["supersedes"] = reply["pending"].clone();
        answer = send(d, &body)?;
    }
    let mut replaced = None;
    if let Some((code, reply)) = &answer
        && change
    {
        if *code == 200 && reply["result"] == "already_scheduled" {
            let cancelled = cancel(b, row, d)?;
            if !matches!(
                cancelled["status"].as_str(),
                Some("cancelled" | "nothing_pending")
            ) {
                return Ok(
                    json!({"tool":r.tool,"status":if cancelled["status"]=="unconfirmed"{"unconfirmed"}else{"refused"},"note":format!("the earlier call back could not be replaced; it still stands at {}",d.time(&known["due"]))}),
                );
            }
            replaced =
                Some(json!({"operation_id":known["operation_id"],"minutes":known["minutes"]}));
            let mut parts = parts.to_vec();
            parts.push("after-cancel");
            id = operation("cb-", &parts);
            if let Some(fields) = body.as_object_mut() {
                fields.remove("supersedes");
                fields.remove("explicit");
            }
            body["operation_id"] = json!(id);
            answer = send(d, &body)?;
        } else if python::truthy(&reply["superseded"]) {
            replaced = Some(json!({"operation_id":reply["superseded"],"minutes":known["minutes"]}));
        }
    }
    let Some((code, reply)) = answer else {
        row["unconfirmed"] = json!(id);
        d.save(row)?;
        return Ok(
            json!({"tool":r.tool,"operation_id":id,"status":"unconfirmed","note":"regi's answer was unclear twice; not known whether the call back is scheduled"}),
        );
    };
    let result = reply["result"].as_str().unwrap_or("");
    if code != 200 || reply["ok"] != true || !matches!(result, "scheduled" | "already_scheduled") {
        let error = ["error", "reason"]
            .into_iter()
            .map(|k| &reply[k])
            .find(|v| python::truthy(v))
            .cloned()
            .unwrap_or(json!(format!("HTTP {code}")));
        return Ok(
            json!({"tool":r.tool,"operation_id":id,"status":"refused","error":error,"note":"regi did not schedule it"}),
        );
    }
    let current = reply
        .get("operation_id")
        .filter(|v| python::truthy(v))
        .cloned()
        .unwrap_or(json!(id));
    row["operation_id"] = current.clone();
    row["status"] = json!("pending");
    row["due"] = reply["due"].clone();
    row["regi_status"] = reply["status"].clone();
    if result == "scheduled" {
        row["minutes"] = json!(minutes);
    } else if python::truthy(&known["call_id"]) {
        row["call_id"] = known["call_id"].clone();
    }
    d.save(row)?;
    let at = d.time(&reply["due"]);
    let mut receipt = json!({"tool":r.tool,"status":result,"operation_id":current,"at":at,"regi_status":reply["status"],"note":note(result,&reply["status"],&at,&reply["feil"])});
    if result == "scheduled" {
        receipt["minutes"] = json!(minutes);
    } else if python::truthy(&known["minutes"]) {
        receipt["minutes"] = known["minutes"].clone();
    }
    if python::truthy(&reply["superseded"]) {
        receipt["superseded"] = reply["superseded"].clone();
    }
    if let Some(replaced) = replaced {
        receipt["replaced"] = replaced;
    }
    Ok(receipt)
}
