//! The relay that carries pushes to phones.
//!
//! A Host does not speak to Apple. It hands a push to the relay (Clarp Audio Central), which
//! holds the key to the push service. There are two ways to be allowed to:
//!
//! - **A grant.** The phone itself gave this Host leave to push to it: the grant names the
//!   phone, so no token is sent, and the Host needs no account of its own.
//! - **A credential.** The Host has an account at the relay. It binds a phone's token to
//!   that account once, and then sends to the token.
//!
//! A phone that gave a grant is reached by it; when the phone took the grant back, or gave
//! none, the credential is used when there is one. This is the Python Host's protocol
//! (`server/lib/apns.py`: `_GrantTransport`, `_RelayTransport`, `_Router`), request for
//! request.
//!
//! What is said here of a failure never holds a grant, a credential or what the push says.

use std::collections::HashSet;
use std::sync::Mutex;
use std::time::Duration;

use clarp_kernel::devices::PushRow;
use clarp_kernel::push::collapse_id_on_the_wire;
use serde_json::{Value, json};

/// What the push service calls a token that is gone for good.
const DEAD: [&str; 3] = ["BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic"];

/// What became of one push.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Outcome {
    /// The push service took it.
    Sent,
    /// The push service says the token is gone: nothing is sent to it again.
    Dead(String),
    /// The phone took its grant back, and this Host has no other way to it.
    GrantRevoked,
    /// This one push was refused. Nothing is known against the token.
    Refused { status: u16, reason: String },
    /// The relay could not be reached, or could not reach the push service.
    Down(String),
    /// This Host has no way to the phone: it gave no grant, and the Host has no credential.
    NoPath,
}

/// One push, as it was delivered or not.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Delivery {
    pub outcome: Outcome,
    /// The phone's grant was refused on the way: it is forgotten, whatever came after.
    pub grant_revoked: bool,
}

/// A push as it is handed to the relay.
#[derive(Debug, Clone, Copy)]
pub struct Push<'a> {
    pub payload: &'a Value,
    pub collapse_id: &'a str,
    /// `alert` for what a person is shown.
    pub push_type: &'a str,
    /// `10`: at once.
    pub priority: &'a str,
}

/// The way to the relay.
pub struct Relay {
    base: String,
    credential: String,
    agent: ureq::Agent,
    /// The tokens this Host has bound to its credential since it started, with the side of
    /// the push service each belongs to. Binding again is harmless.
    bound: Mutex<HashSet<(String, String)>>,
}

/// What the relay says went wrong, by its code.
fn error_code(answer: &Value) -> String {
    answer["error"]["code"]
        .as_str()
        .unwrap_or("")
        .chars()
        .take(64)
        .collect()
}

/// What the push service did with the one push of a request the relay took.
fn delivered(answer: &Value) -> Outcome {
    let Some(results) = answer.get("results") else {
        return Outcome::Refused {
            status: 502,
            reason: "RelayBadResponse".to_string(),
        };
    };
    let result = &results[0];
    let status = result["status"]
        .as_u64()
        .and_then(|status| u16::try_from(status).ok())
        .unwrap_or(0);
    let reason = result["reason"].as_str().unwrap_or("").to_string();
    if reason == "RelayDeliveryFailed" {
        // The relay is there and could not reach the push service: nothing about the token.
        return Outcome::Down("the relay could not reach the push service".to_string());
    }
    if status == 200 {
        Outcome::Sent
    } else if status == 410 || DEAD.contains(&reason.as_str()) {
        Outcome::Dead(if reason.is_empty() {
            status.to_string()
        } else {
            reason
        })
    } else {
        Outcome::Refused { status, reason }
    }
}

impl Relay {
    /// The relay at `base`, with this Host's credential there when it has one.
    pub fn new(base: &str, credential: &str) -> Self {
        // Longer than the relay's own wait for the push service, so that the relay answers
        // before this gives up.
        let config = ureq::Agent::config_builder()
            .timeout_global(Some(Duration::from_secs(20)))
            .http_status_as_error(false)
            .build();
        Self {
            base: base.trim_end_matches('/').to_string(),
            credential: credential.to_string(),
            agent: config.into(),
            bound: Mutex::new(HashSet::new()),
        }
    }

    /// One request to the relay: the status it answered with and what it said. The error is
    /// that it did not answer.
    fn post(&self, path: &str, bearer: &str, body: &Value) -> Result<(u16, Value), String> {
        let mut response = self
            .agent
            .post(format!("{}{path}", self.base))
            .header("Authorization", format!("Bearer {bearer}"))
            .send_json(body)
            // Only that it failed: never what the request held.
            .map_err(|_| format!("the relay did not answer {path}"))?;
        let status = response.status().as_u16();
        let answer = response
            .body_mut()
            .read_json::<Value>()
            .unwrap_or(Value::Null);
        match status {
            404 | 405 | 500.. => Err(format!("the relay answered {status} to {path}")),
            _ => Ok((status, answer)),
        }
    }

    fn by_grant(&self, grant: &str, notification: &Value) -> Outcome {
        let body = json!({ "notifications": [notification] });
        match self.post("/v1/push/send", grant, &body) {
            Err(why) => Outcome::Down(why),
            // The phone revoked this Host's grant.
            Ok((401, _)) => Outcome::GrantRevoked,
            Ok((200, answer)) => delivered(&answer),
            Ok((status, answer)) => Outcome::Refused {
                status,
                reason: match error_code(&answer) {
                    code if code.is_empty() => format!("Relay{status}"),
                    code => code,
                },
            },
        }
    }

    fn forget(&self, token: &str) {
        if let Ok(mut bound) = self.bound.lock() {
            bound.retain(|(known, _)| known != token);
        }
    }

    /// Binds a token to this Host's credential, once. `Ok(false)`: the relay refused.
    fn bind(&self, token: &str, environment: &str) -> Result<bool, String> {
        let key = (token.to_string(), environment.to_string());
        if self.bound.lock().is_ok_and(|bound| bound.contains(&key)) {
            return Ok(true);
        }
        let body = json!({ "devices": [{ "deviceToken": token, "environment": environment }] });
        let (status, _) = self.post("/v1/push/devices", &self.credential, &body)?;
        if status != 200 {
            return Ok(false);
        }
        if let Ok(mut bound) = self.bound.lock() {
            bound.insert(key);
        }
        Ok(true)
    }

    fn by_credential(&self, row: &PushRow, notification: &Value) -> Outcome {
        let environment = if row.environment.eq_ignore_ascii_case("sandbox") {
            "sandbox"
        } else {
            "production"
        };
        let mut addressed = json!({ "deviceToken": row.token });
        if let (Some(addressed), Some(fields)) =
            (addressed.as_object_mut(), notification.as_object())
        {
            addressed.extend(fields.clone());
        }
        let body = json!({ "notifications": [addressed] });
        for attempt in 0..2 {
            match self.bind(&row.token, environment) {
                Err(why) => return Outcome::Down(why),
                Ok(false) => {
                    return Outcome::Refused {
                        status: 401,
                        reason: "RelayRegistrationRejected".to_string(),
                    };
                }
                Ok(true) => {}
            }
            let outcome = match self.post("/v1/push/send", &self.credential, &body) {
                Err(why) => return Outcome::Down(why),
                Ok((200, answer)) => delivered(&answer),
                Ok((status, answer)) => {
                    if status == 401 {
                        // A credential that was revoked: what it bound is gone with it.
                        self.forget(&row.token);
                    }
                    return Outcome::Refused {
                        status,
                        reason: match error_code(&answer) {
                            code if code.is_empty() => format!("Relay{status}"),
                            code => code,
                        },
                    };
                }
            };
            // The relay dropped the binding: bind again, and try once more.
            let dropped = matches!(&outcome, Outcome::Refused { reason, .. } if reason == "DeviceNotRegistered");
            if dropped && attempt == 0 {
                self.forget(&row.token);
                continue;
            }
            if matches!(outcome, Outcome::Dead(_)) {
                self.forget(&row.token);
            }
            return outcome;
        }
        Outcome::Refused {
            status: 403,
            reason: "DeviceNotRegistered".to_string(),
        }
    }

    /// Sends one push to one row of the register.
    pub fn send(&self, row: &PushRow, push: &Push<'_>) -> Delivery {
        let notification = json!({
            "pushType": push.push_type,
            "priority": push.priority,
            "collapseId": collapse_id_on_the_wire(push.collapse_id),
            "payload": push.payload,
        });
        let mut grant_revoked = false;
        if !row.push_grant.is_empty() {
            match self.by_grant(&row.push_grant, &notification) {
                // There is another way to the phone: go on with it.
                Outcome::GrantRevoked if !self.credential.is_empty() => grant_revoked = true,
                outcome => {
                    return Delivery {
                        grant_revoked: outcome == Outcome::GrantRevoked,
                        outcome,
                    };
                }
            }
        }
        if self.credential.is_empty() {
            return Delivery {
                outcome: Outcome::NoPath,
                grant_revoked,
            };
        }
        Delivery {
            outcome: self.by_credential(row, &notification),
            grant_revoked,
        }
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    #[test]
    fn what_the_push_service_did_is_read_off_the_relays_answer() {
        let answer = |status: u64, reason: &str| json!({ "results": [{ "status": status, "reason": reason, "apnsId": "x" }] });
        assert_eq!(delivered(&answer(200, "")), Outcome::Sent);
        assert_eq!(
            delivered(&answer(410, "Unregistered")),
            Outcome::Dead("Unregistered".to_string())
        );
        assert_eq!(
            delivered(&answer(400, "BadDeviceToken")),
            Outcome::Dead("BadDeviceToken".to_string())
        );
        assert_eq!(
            delivered(&answer(410, "")),
            Outcome::Dead("410".to_string())
        );
        assert_eq!(
            delivered(&answer(429, "TooManyRequests")),
            Outcome::Refused {
                status: 429,
                reason: "TooManyRequests".to_string()
            }
        );
        assert!(matches!(
            delivered(&answer(502, "RelayDeliveryFailed")),
            Outcome::Down(_)
        ));
        // The relay took the request and said nothing of the push: nothing is known.
        assert_eq!(
            delivered(&json!({ "results": [] })),
            Outcome::Refused {
                status: 0,
                reason: String::new()
            }
        );
        assert!(matches!(
            delivered(&json!("not what a relay says")),
            Outcome::Refused { status: 502, .. }
        ));
    }
}
