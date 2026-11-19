//! Admission of one tool action against the call's frozen team. No effect
//! starts here; the Host commits the request identity before dispatch.
use crate::python;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

pub fn operation(prefix: &str, parts: &[&str]) -> String {
    let digest = Sha256::digest(parts.join("\u{1f}").as_bytes());
    format!("{prefix}{digest:x}")[..prefix.len() + 24].into()
}

#[derive(Debug, PartialEq)]
pub enum Action {
    Answer(Value),
    Status,
    Transcript(usize),
    Delegate { request: String, normalized: String },
}

pub struct Proposal<'a> {
    pub session: &'a str,
    pub name: &'a str,
    pub action: Action,
}

#[derive(Default)]
pub struct Team {
    delegations: usize,
}

fn string(value: &Value) -> String {
    value
        .as_str()
        .map_or_else(|| python::repr(value), str::to_owned)
}

impl Team {
    pub fn propose<'a>(&mut self, members: &'a [Value], tool: &str, args: &Value) -> Proposal<'a> {
        let target = &args["agent"];
        let Some(member) = members.iter().find(|member| &member["session"] == target) else {
            return Proposal {
                session: "",
                name: "",
                action: Action::Answer(
                    json!({"tool":tool,"agent":python::first(&string(target),64),"status":"refused","error":"not on this team"}),
                ),
            };
        };
        let name = member["name"].as_str().unwrap_or("");
        let session = member["session"].as_str().unwrap_or("");
        let action = match tool {
            "get_agent_status" => Action::Status,
            "read_agent_transcript" => Action::Transcript(
                args["limit"]
                    .as_u64()
                    .filter(|n| (1..=5).contains(n))
                    .unwrap_or(3) as usize,
            ),
            "delegate_to_agent" => {
                let request = if python::truthy(&args["request"]) {
                    string(&args["request"])
                } else {
                    String::new()
                };
                let normalized = request
                    .split(python::blank)
                    .filter(|s| !s.is_empty())
                    .collect::<Vec<_>>()
                    .join(" ");
                let size = normalized.chars().count();
                if normalized.is_empty() {
                    Action::Answer(
                        json!({"tool":tool,"agent":name,"status":"refused","reason":"empty_request","note":"nothing was sent"}),
                    )
                } else if size > 4000 {
                    Action::Answer(
                        json!({"tool":tool,"agent":name,"status":"refused","reason":"request_too_large","max":4000,"len":size,"note":"nothing was sent; say it shorter"}),
                    )
                } else if self.delegations >= 12 {
                    Action::Answer(
                        json!({"tool":tool,"agent":name,"status":"refused","reason":"delegation limit for this call"}),
                    )
                } else {
                    self.delegations += 1;
                    Action::Delegate {
                        request,
                        normalized,
                    }
                }
            }
            _ => Action::Answer(json!({"tool":tool,"status":"refused","error":"unknown tool"})),
        };
        Proposal {
            session,
            name,
            action,
        }
    }
}

pub fn read(tool: &str, member: &str, data: Value) -> Value {
    json!({"tool":tool,"agent":member,"status":"ok","untrusted_data":data})
}

pub fn ended(tool: &str) -> Value {
    json!({"tool":tool,"status":"call_ended"})
}

pub enum Prompt<'a> {
    Sent,
    Refused(&'a str),
    Uncertain(&'a str),
}

pub fn prompted(member: &str, id: &str, outcome: Prompt<'_>) -> Value {
    let mut receipt = json!({"tool":"delegate_to_agent","agent":member,"request_id":id});
    match outcome {
        Prompt::Sent => {
            receipt["status"] = json!("forwarded");
            receipt["sent"] = json!(true);
        }
        Prompt::Refused(reason) | Prompt::Uncertain(reason) => {
            let (status, note) = if matches!(outcome, Prompt::Refused(_)) {
                ("refused", "nothing was sent")
            } else {
                (
                    "unknown_delivery",
                    "not known whether it arrived; not retried",
                )
            };
            receipt["status"] = json!(status);
            receipt["reason"] = json!(python::first(reason, 120));
            receipt["note"] = json!(note);
        }
    }
    receipt
}

pub fn prompt(name: &str, request: &str) -> String {
    format!("Fra {name} (teamleder), rett fra telefonsamtalen med Peter: {request}")
}

pub struct Question {
    pub id: String,
    pub request: String,
}

impl Question {
    pub fn new(bound: [&str; 4], name: &str, question: &Value) -> Result<Self, Value> {
        let normalized = if python::truthy(question) {
            string(question)
        } else {
            String::new()
        };
        let question = normalized
            .split(python::blank)
            .filter(|word| !word.is_empty())
            .collect::<Vec<_>>()
            .join(" ");
        if question.is_empty() || question.chars().count() > 4000 {
            return Err(
                json!({"tool":"ask_my_agent","status":"refused","reason":if question.is_empty(){"empty question"}else{"question too long"}}),
            );
        }
        let [host, phone, call, session] = bound;
        let id = operation(
            "sandra-",
            &[host, phone, call, session, &question.to_lowercase()],
        );
        let request = format!(
            "Spørsmål fra {name}s Live-samtale med Peter (svar kort og muntlig, på norsk; slå opp det som trengs med verktøyene dine): {question}"
        );
        Ok(Self { id, request })
    }

    pub fn already_asked(&self) -> Value {
        json!({"tool":"ask_my_agent","status":"already_asked","operation_id":self.id,"note":"already asked in this call; the answer comes back by itself (read_result replays it)"})
    }

    pub fn asked(&self, row: &Value) -> Value {
        json!({"tool":"ask_my_agent","status":"asked","operation_id":self.id,"regi_status":Value::Null,"reason":if row["status"]=="failed"{row["error"].clone()}else{Value::Null},"note":"say briefly that you are checking (\"jeg sjekker\"); the answer comes back in parts"})
    }

    pub fn missing(&self, unknown: bool) -> Value {
        if unknown {
            json!({"tool":"ask_my_agent","status":"unknown","operation_id":self.id,"reason":"RuntimeError","note":"not known whether the question went out; not resent; say so"})
        } else {
            json!({"tool":"ask_my_agent","status":"failed","reason":"RuntimeError","note":"the question could not be sent; say so"})
        }
    }
}
