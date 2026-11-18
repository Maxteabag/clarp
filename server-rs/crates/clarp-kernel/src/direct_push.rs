//! A push handed to the push service itself, with a key of the Host's own.
//!
//! A Host hands its pushes to the relay ([`crate::push`] says what they are). One kind the
//! relay does not carry: the push a phone must show as an incoming call the moment it
//! arrives, which is how a ring rings a phone that is locked or asleep. A Host whose owner
//! gave it a key at the push service (a file, the key's id, the team) signs a short-lived
//! token with that key and hands such a push to the service itself.
//!
//! Here is what is pure in that, as the Python Host does it (`server/lib/apns.py`:
//! `_host`, `_send_one`, `_auth_jwt`, `_DirectTransport`): where the service is, the request
//! for one push, what an answer of the service means, what a token says and for how long
//! one is used, and when a push is handed over a second time. The signing and the
//! connection are the Host's.
//!
//! Held to that Host's own module by `tests/fixtures/direct-push-vectors.json`.

use serde_json::{Value, json};

use crate::python;

/// Where the push service is, for apps as they are given out and for apps under development.
pub const PRODUCTION: &str = "https://api.push.apple.com";
pub const SANDBOX: &str = "https://api.sandbox.push.apple.com";
/// For how long one token is used. The service takes a token for an hour and wants a Host
/// to keep one for at least twenty minutes.
pub const TOKEN_USED_FOR_S: i64 = 50 * 60;
/// What the service calls a token of a phone that is gone for good.
pub const GONE_FOR_GOOD: [&str; 3] = ["BadDeviceToken", "DeviceTokenNotForTopic", "Unregistered"];
/// What it calls a token of the Host's that it does not take: a new one is signed.
pub const TOKEN_REFUSED: [&str; 2] = ["ExpiredProviderToken", "InvalidProviderToken"];

/// Where the push service is for a phone's side of it.
pub fn host(environment: &str) -> &'static str {
    if environment.to_lowercase() == "sandbox" {
        SANDBOX
    } else {
        PRODUCTION
    }
}

/// One push, as it is handed to the service.
#[derive(Debug, Clone, Copy)]
pub struct Push<'a> {
    pub payload: &'a Value,
    /// `alert` for what a person is shown, `voip` for an incoming call, `background` for
    /// what wakes the app quietly, `liveactivity` for the chip on the lock screen.
    pub push_type: &'a str,
    /// `10`: at once. `5`: when the phone finds the time.
    pub priority: &'a str,
    /// The name later pushes of the same thing replace this one by. Empty: the payload's own.
    pub collapse_id: &'a str,
    /// The second until which the service keeps the push for a phone it cannot reach.
    pub expiration: Option<i64>,
}

/// The request for one push: where on the service it goes, what its head says, and its body.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Request {
    pub path: String,
    pub headers: Vec<(&'static str, String)>,
    pub body: String,
}

/// The request for one push to one phone. `provider_token`: the token this Host signed;
/// `bundle_id`: the app the phone's token belongs to.
pub fn request(provider_token: &str, bundle_id: &str, device_token: &str, push: &Push) -> Request {
    // Each kind of push goes to the app under a name of its own.
    let topic = match push.push_type {
        "liveactivity" => format!("{bundle_id}.push-type.liveactivity"),
        "voip" => format!("{bundle_id}.voip"),
        _ => bundle_id.to_string(),
    };
    let collapse_id = match push.collapse_id {
        "" => crate::push::collapse_id(push.payload),
        given => given.to_string(),
    };
    let mut headers = vec![
        ("authorization", format!("bearer {provider_token}")),
        ("apns-topic", topic),
        ("apns-push-type", push.push_type.to_string()),
        ("apns-priority", push.priority.to_string()),
        ("apns-collapse-id", collapse_id),
    ];
    if let Some(until) = push.expiration {
        headers.push(("apns-expiration", until.to_string()));
    }
    Request {
        path: format!("/3/device/{device_token}"),
        headers,
        body: python::dumps_ascii(push.payload),
    }
}

/// What an answer of the service means: its status, the reason it gives where it did not
/// take the push, and the name it gave the push. The reason is what the answer says under
/// `reason`; where the answer is no such thing, the beginning of the answer as it is.
pub fn answered(status: u16, body: &str, named: &str) -> (u16, Value, String) {
    let reason = if status == 200 {
        json!("")
    } else {
        match serde_json::from_str::<Value>(body) {
            Ok(Value::Object(said)) => said.get("reason").cloned().unwrap_or_else(|| json!("")),
            // What Python takes for nothing says nothing.
            Ok(said) if !python::truthy(&said) => json!(""),
            _ => json!(python::first(body, 200)),
        }
    };
    (status, reason, named.to_string())
}

/// What a token of the Host's says, before it is signed: its head and its claims. `now_s`:
/// the second it is signed in.
pub fn token(team_id: &str, key_id: &str, now_s: i64) -> (Value, Value) {
    (
        json!({ "alg": "ES256", "kid": key_id, "typ": "JWT" }),
        json!({ "iss": team_id, "iat": now_s }),
    )
}

/// Bytes as a token carries them: base64 for an address, with nothing to fill it up.
pub fn base64url(bytes: &[u8]) -> String {
    let plain = python::b64encode(bytes);
    let fitted: String = plain
        .chars()
        .filter(|sign| *sign != '=')
        .map(|sign| match sign {
            '+' => '-',
            '/' => '_',
            other => other,
        })
        .collect();
    fitted
}

/// What is signed of a token: its head and its claims, each written without a blank and as
/// a token carries bytes, with a point between.
pub fn to_sign(head: &Value, claims: &Value) -> String {
    let part = |value: &Value| base64url(python::compact_ascii(value).as_bytes());
    format!("{}.{}", part(head), part(claims))
}

/// Whose token it is and with what it was signed: a token is used only for the team, the key
/// id and the file of the key it was signed for, as that file was then.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Signer {
    pub team_id: String,
    pub key_id: String,
    /// The file of the key, by its own path whatever links lead to it, and when it was last
    /// written, in nanoseconds; nought where that cannot be said.
    pub key_file: String,
    pub written_ns: i128,
}

/// A token that was signed, and when.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Signed {
    pub token: String,
    pub at_s: i64,
    pub by: Signer,
}

impl Signed {
    /// Whether the token is still the one to use now, for this signer.
    pub fn serves(&self, by: &Signer, now_s: i64) -> bool {
        self.by == *by && now_s - self.at_s < TOKEN_USED_FOR_S
    }
}

/// What handing one push to the service came to, as far as trying again goes.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Came {
    /// The service answered: a status and, as far as it is words, its reason.
    Answered(u16),
    /// The connection had been closed by somebody else before the push went out: nothing
    /// reached the service.
    Closed,
    /// It failed in another way: what reached the service is not known.
    Failed,
}

/// What follows one try.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Next {
    /// That is the outcome.
    Done,
    /// Once more, over a connection made anew, with the token as it is.
    AgainOverANewConnection,
    /// Once more, with a token signed anew.
    AgainWithANewToken,
}

/// One push being handed over: it is tried a second time in two cases, each at most once
/// and in either order, and only where nothing reached the phone: a connection somebody
/// else closed, and a token of the Host's that the service does not take.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct Tries {
    over_a_new_connection: bool,
    with_a_new_token: bool,
}

impl Tries {
    /// What follows a try that came to this. `reason`: the service's reason, where it
    /// answered.
    pub fn after(&mut self, came: Came, reason: &str) -> Next {
        match came {
            Came::Closed if !self.over_a_new_connection => {
                self.over_a_new_connection = true;
                Next::AgainOverANewConnection
            }
            Came::Answered(403) if !self.with_a_new_token && TOKEN_REFUSED.contains(&reason) => {
                self.with_a_new_token = true;
                Next::AgainWithANewToken
            }
            _ => Next::Done,
        }
    }
}
