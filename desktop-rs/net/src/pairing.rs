//! Exchanging a one-time pairing code for a device token, apart from the
//! engine: `clarp-slint --pair URL CODE` pairs over SSH, with no window.

use clarp_core::endpoint::{error_message, normalize_base, resolve};
use clarp_core::json::Object;
use serde_json::{Value, json};
use url::Url;

use crate::USER_AGENT;
use clarp_core::tr;

// i18n-screen: pairing

/// The device token in a `/pairing/exchange` reply.
pub fn device_token(reply: &Object) -> Result<String, String> {
    let token = reply.get("device").and_then(Value::as_object).and_then(|d| d.get("token")).and_then(Value::as_str).unwrap_or_default();
    // i18n: pairing dialog, error: the Host's answer lacked this computer's sign-in key
    if token.is_empty() { Err(tr!("Pairing response did not contain a device credential")) } else { Ok(token.to_owned()) }
}

/// Posts `code` to `base`'s `/pairing/exchange` and returns the device token,
/// or the Host's reason it would not pair.
pub async fn exchange(base: &Url, code: &str, device_name: &str) -> Result<String, String> {
    let endpoint = resolve(&normalize_base(base.clone()), "pairing/exchange").ok_or_else(|| {
        // i18n: pairing dialog, error: the address cannot pair devices; {} is the address
        tr!("{} has no pairing endpoint", base)
    })?;
    // A redirect is reported, not followed: the code goes to the URL given.
    let client = reqwest::Client::builder()
        .user_agent(USER_AGENT)
        .redirect(reqwest::redirect::Policy::none())
        .timeout(std::time::Duration::from_secs(30))
        .build()
        .map_err(|error| error.to_string())?;
    let response = client
        .post(endpoint.clone())
        .header(reqwest::header::CONTENT_TYPE, "application/json")
        .body(json!({"code": code.trim(), "device_name": device_name}).to_string())
        .send()
        .await
        // i18n: pairing dialog, error: {0} is the address tried, {1} the technical reason
        .map_err(|error| tr!("Could not reach {0}: {1}", endpoint, error))?;
    let status = response.status();
    // i18n: pairing dialog, error: the Host's answer could not be read; {} is the technical reason
    let body = response.bytes().await.map_err(|error| tr!("Could not read the pairing reply: {}", error))?;
    if !status.is_success() {
        // i18n: pairing dialog, error when the Host gives no reason; {} is the HTTP status number
        return Err(error_message(&body, &tr!("Pairing failed with HTTP {}", status.as_u16())));
    }
    match serde_json::from_slice::<Value>(&body) {
        Ok(Value::Object(reply)) => device_token(&reply),
        // i18n: pairing dialog, error: the Host's answer was not in the expected format (JSON stays as it is)
        _ => Err(tr!("The pairing reply was not JSON")),
    }
}
