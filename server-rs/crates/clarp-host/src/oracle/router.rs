//! The router of the Oracle's calls, as this Host reaches it.
//!
//! The voice of a call holds no tools: what the user asked is shown to a second model, the
//! router, which answers with what is to be done ([`clarp_kernel::oracle::routing`]). It is a
//! provider's, asked with the owner's key over the web; in a test it is a stand-in on this
//! machine. A router that does not answer, or answers with anything but a record, did not
//! answer: nothing of what it said instead is kept or told.

use std::time::Duration;

use clarp_kernel::python;
use serde_json::Value;

use crate::edge::App;

/// Where the router is asked.
const ROUTER_URL: &str = "https://api.openai.com/v1/responses";
/// How long the router has to answer.
const ANSWERS_WITHIN: Duration = Duration::from_secs(35);

/// Asks the router with the owner's key. `None`: it did not answer.
pub(super) async fn asked(app: &App, key: &str, question: Value) -> Option<Value> {
    let address = match app.config.router_endpoint.as_str() {
        "" => ROUTER_URL.to_string(),
        stand_in => stand_in.to_string(),
    };
    let bearer = format!("Bearer {key}");
    let asked = tokio::task::spawn_blocking(move || {
        let config = ureq::Agent::config_builder()
            .timeout_global(Some(ANSWERS_WITHIN))
            .http_status_as_error(false)
            .build();
        let agent: ureq::Agent = config.into();
        let request = agent
            .post(&address)
            .header("Authorization", &bearer)
            .header("Content-Type", "application/json");
        let mut response = request.send(python::dumps_ascii(&question)).ok()?;
        if !(200..300).contains(&response.status().as_u16()) {
            return None;
        }
        response.body_mut().read_json::<Value>().ok()
    });
    asked.await.ok().flatten()
}
