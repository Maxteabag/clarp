//! Only talk to the configured URL. Neither redirects nor proxy settings may take a peer token.

use std::io::Read;
use std::time::Duration;

use serde_json::{Map, Value};
use url::Url;

const MAX_BYTES: usize = 4 * 1024 * 1024;

pub(crate) fn normalize_url(asked: &str) -> Option<&str> {
    let text = asked.trim().trim_end_matches('/');
    let (scheme, location) = text.split_once("://")?;
    let authority = location.split('/').next()?;
    if !matches!(scheme, "http" | "https")
        || authority.is_empty()
        || authority.contains('@')
        || text.contains(['\\', '?', '#'])
        || text.chars().any(|ch| ch.is_whitespace() || ch.is_control())
        || !Url::parse(text).is_ok_and(|url| url.has_host())
        || text.parse::<axum::http::Uri>().is_err()
    {
        return None;
    }
    Some(text)
}

pub(crate) fn valid_token(token: &str) -> bool {
    !token.is_empty() && token.bytes().all(|byte| byte.is_ascii_graphic())
}

pub(super) fn direct_agent(seconds: u64) -> ureq::Agent {
    ureq::Agent::config_builder()
        .proxy(None)
        .max_redirects(0)
        .timeout_global(Some(Duration::from_secs(seconds)))
        .http_status_as_error(false)
        .build()
        .into()
}

pub(super) fn fetch(
    base: &str,
    path: &str,
    token: Option<&str>,
    seconds: u64,
    body: Option<&Value>,
) -> Result<Map<String, Value>, String> {
    let agent = direct_agent(seconds);
    let url = format!("{base}{path}");
    let mut response = if let Some(body) = body {
        agent
            .post(&url)
            .header("Accept", "application/json")
            .send_json(body)
    } else {
        let request = agent.get(&url).header("Accept", "application/json");
        match token {
            Some(token) => request
                .header("Authorization", format!("Bearer {token}"))
                .call(),
            None => request.call(),
        }
    }
    .map_err(|_| "unreachable")?;
    let status = response.status().as_u16();
    match status {
        200..=299 => {}
        300..=399 => return Err("redirect_refused".to_string()),
        401 | 403 => return Err("refused".to_string()),
        _ => return Err(format!("http_{status}")),
    }
    let mut raw = Vec::new();
    response
        .body_mut()
        .as_reader()
        .take(MAX_BYTES as u64 + 1)
        .read_to_end(&mut raw)
        .map_err(|_| "unreachable")?;
    if raw.len() > MAX_BYTES {
        return Err("too_large".to_string());
    }
    let Value::Object(value) = serde_json::from_slice(&raw).map_err(|_| "not_json")? else {
        return Err("not_json".to_string());
    };
    if token.is_some_and(|token| object_contains(&value, token)) {
        return Err("refused".to_string());
    }
    Ok(value)
}

fn object_contains(object: &Map<String, Value>, token: &str) -> bool {
    object
        .iter()
        .any(|(key, value)| key.contains(token) || contains_token(value, token))
}

pub(super) fn contains_token(value: &Value, token: &str) -> bool {
    match value {
        Value::String(text) => text.contains(token),
        Value::Object(object) => object_contains(object, token),
        Value::Array(items) => items.iter().any(|item| contains_token(item, token)),
        _ => false,
    }
}
