//! Small JSON reads for the providers' voice catalogues.
use std::fmt::Write as _;
use std::time::Duration;

use serde_json::Value;

#[derive(Debug)]
pub enum LookupError {
    Http(u16),
    Request(String),
}

impl std::fmt::Display for LookupError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Http(status) => write!(f, "HTTP {status}"),
            Self::Request(why) => f.write_str(why),
        }
    }
}

pub(crate) fn segment(value: &str) -> String {
    let mut encoded = String::new();
    for byte in value.bytes() {
        if byte.is_ascii_alphanumeric() || b"-_~".contains(&byte) {
            encoded.push(char::from(byte));
        } else {
            let _ = write!(encoded, "%{byte:02X}");
        }
    }
    encoded
}

pub(crate) fn get(
    url: &str,
    credentials: &[(&str, &str)],
    extra: Option<(&str, &str)>,
) -> Result<Value, LookupError> {
    let config = ureq::Agent::config_builder()
        .max_redirects(0)
        .timeout_global(Some(Duration::from_secs(15)))
        .http_status_as_error(false)
        .build();
    let agent: ureq::Agent = config.into();
    for (place, (header, value)) in credentials.iter().enumerate() {
        let mut request = agent.get(url).header(*header, *value);
        if let Some((header, value)) = extra {
            request = request.header(header, value);
        }
        let mut response = request
            .call()
            .map_err(|error| LookupError::Request(format!("request failed: {error}")))?;
        let status = response.status().as_u16();
        if status == 401 && place + 1 < credentials.len() {
            continue;
        }
        if !(200..300).contains(&status) {
            return Err(LookupError::Http(status));
        }
        let said = response
            .body_mut()
            .read_to_string()
            .map_err(|error| LookupError::Request(format!("request failed: {error}")))?;
        return serde_json::from_str(&said)
            .map_err(|error| LookupError::Request(format!("request failed: {error}")));
    }
    Err(LookupError::Request(
        "voice catalogue credentials are unavailable".into(),
    ))
}
