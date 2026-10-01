//! Port of the C++ client's `ApiClient`.

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, RwLock};
use std::time::Duration;

use clarp_core::endpoint::{error_message, normalize_base, redirect_is_no_less_safe, resolve, same_origin};
use clarp_core::json::Object;
use reqwest::header::{ACCEPT, AUTHORIZATION, CONTENT_TYPE, HeaderMap, HeaderValue};
use reqwest::{Method, redirect};
use serde_json::Value;
use tokio::runtime::Handle;
use url::Url;

use crate::USER_AGENT;

#[derive(Debug, Clone, PartialEq)]
pub enum ApiReply {
    Json { tag: String, object: Object },
    Bytes { tag: String, bytes: Vec<u8>, content_type: String },
    Failed { tag: String, message: String, status: u16 },
}

#[derive(Default)]
struct Endpoint {
    base: Option<Url>,
    token: String,
}

type Sink = Arc<dyn Fn(ApiReply) + Send + Sync>;

/// Every reply is tagged by the caller. Replies to requests issued before the
/// latest `set_endpoint` are dropped: a slow answer from the previous Host
/// must never land in the new Host's state.
#[derive(Clone)]
pub struct ApiClient {
    endpoint: Arc<RwLock<Endpoint>>,
    generation: Arc<AtomicU64>,
    api: reqwest::Client,
    media: reqwest::Client,
    runtime: Handle,
    sink: Sink,
}

enum Body {
    None,
    Json(Value),
    Bytes { body: Vec<u8>, content_type: String, headers: Vec<(String, String)> },
}

impl ApiClient {
    pub fn new(runtime: Handle, sink: impl Fn(ApiReply) + Send + Sync + 'static) -> Self {
        let api = reqwest::Client::builder()
            .user_agent(USER_AGENT)
            .redirect(redirect::Policy::custom(|attempt| {
                let safe = attempt
                    .previous()
                    .last()
                    .is_none_or(|from| redirect_is_no_less_safe(from, attempt.url()));
                if !safe {
                    attempt.error("Refusing a redirect to a less secure URL")
                } else if attempt.previous().len() >= 10 {
                    attempt.error("Too many redirects")
                } else {
                    attempt.follow()
                }
            }))
            .build()
            .expect("HTTP client builds");
        // Media requests carry the credential into a renderer-owned path, so a
        // redirect may never leave the origin, even an "upgrade" to https.
        let media = reqwest::Client::builder()
            .user_agent(USER_AGENT)
            .redirect(redirect::Policy::custom(|attempt| {
                let first = attempt.previous().first().cloned();
                match first {
                    Some(origin) if same_origin(attempt.url(), &origin) && attempt.previous().len() < 10 => {
                        attempt.follow()
                    }
                    _ => attempt.error("Refusing cross-origin authenticated media redirect"),
                }
            }))
            .build()
            .expect("HTTP client builds");
        Self {
            endpoint: Arc::default(),
            generation: Arc::default(),
            api,
            media,
            runtime,
            sink: Arc::new(sink),
        }
    }

    pub fn set_endpoint(&self, base: Url, token: &str) {
        self.generation.fetch_add(1, Ordering::SeqCst);
        let mut endpoint = self.endpoint.write().expect("endpoint lock");
        endpoint.base = Some(normalize_base(base));
        endpoint.token = token.to_owned();
    }

    pub fn resolve(&self, path: &str) -> Option<Url> {
        let endpoint = self.endpoint.read().expect("endpoint lock");
        endpoint.base.as_ref().and_then(|base| resolve(base, path))
    }

    pub fn get(&self, tag: &str, path: &str, query: &[(&str, &str)]) {
        let url = self.resolve(path).map(|mut url| {
            if !query.is_empty() {
                url.query_pairs_mut().clear().extend_pairs(query);
            }
            url
        });
        self.send_json(tag, Method::GET, url, Body::None, None);
    }

    pub fn post_json(&self, tag: &str, path: &str, body: Value, timeout: Option<Duration>) {
        self.send_json(tag, Method::POST, self.resolve(path), Body::Json(body), timeout);
    }

    pub fn put_json(&self, tag: &str, path: &str, body: Value) {
        self.send_json(tag, Method::PUT, self.resolve(path), Body::Json(body), None);
    }

    pub fn delete(&self, tag: &str, path: &str) {
        self.send_json(tag, Method::DELETE, self.resolve(path), Body::None, None);
    }

    pub fn post_bytes(&self, tag: &str, path: &str, body: Vec<u8>, content_type: &str, headers: &[(&str, &str)]) {
        let headers = headers.iter().map(|(k, v)| ((*k).to_owned(), (*v).to_owned())).collect();
        let body = Body::Bytes { body, content_type: content_type.to_owned(), headers };
        self.send_json(tag, Method::POST, self.resolve(path), body, None);
    }

    /// Fetch authenticated media (avatars, attachments). Only the Host's own
    /// origin may receive the bearer token.
    pub fn get_bytes(&self, tag: &str, path: &str) {
        let tag = tag.to_owned();
        let (base, token) = self.snapshot();
        let url = self.resolve(path).filter(|url| base.as_ref().is_some_and(|b| same_origin(url, b)));
        let Some(url) = url else {
            (self.sink)(ApiReply::Failed { tag, message: "Refusing cross-origin authenticated media".into(), status: 0 });
            return;
        };
        let generation = self.generation.load(Ordering::SeqCst);
        let request = self
            .media
            .get(url)
            .headers(auth_headers(&token, "image/png,image/jpeg,image/webp,image/gif,image/*"));
        let this = self.clone();
        self.runtime.spawn(async move {
            let reply = match request.send().await {
                Err(error) => ApiReply::Failed { tag, message: error.to_string(), status: 0 },
                Ok(response) => {
                    let status = response.status();
                    let content_type = response
                        .headers()
                        .get(CONTENT_TYPE)
                        .and_then(|v| v.to_str().ok())
                        .unwrap_or_default()
                        .to_owned();
                    match response.bytes().await {
                        Ok(bytes) if status.is_success() => {
                            ApiReply::Bytes { tag, bytes: bytes.to_vec(), content_type }
                        }
                        Ok(_) => ApiReply::Failed { tag, message: http_status_text(status), status: status.as_u16() },
                        Err(error) => ApiReply::Failed { tag, message: error.to_string(), status: status.as_u16() },
                    }
                }
            };
            this.deliver(generation, reply);
        });
    }

    fn snapshot(&self) -> (Option<Url>, String) {
        let endpoint = self.endpoint.read().expect("endpoint lock");
        (endpoint.base.clone(), endpoint.token.clone())
    }

    fn deliver(&self, generation: u64, reply: ApiReply) {
        if generation == self.generation.load(Ordering::SeqCst) {
            (self.sink)(reply);
        }
    }

    fn send_json(&self, tag: &str, method: Method, url: Option<Url>, body: Body, timeout: Option<Duration>) {
        let tag = tag.to_owned();
        let Some(url) = url else {
            (self.sink)(ApiReply::Failed { tag, message: "No Host endpoint is configured".into(), status: 0 });
            return;
        };
        let generation = self.generation.load(Ordering::SeqCst);
        let (_, token) = self.snapshot();
        let mut request = self.api.request(method, url).headers(auth_headers(&token, "application/json"));
        request = match body {
            Body::None => request,
            Body::Json(value) => request
                .header(CONTENT_TYPE, "application/json")
                .body(serde_json::to_vec(&value).unwrap_or_default()),
            Body::Bytes { body, content_type, headers } => {
                let mut request = request.header(CONTENT_TYPE, content_type).body(body);
                for (name, value) in headers {
                    request = request.header(name, value);
                }
                request
            }
        };
        if let Some(timeout) = timeout.filter(|t| !t.is_zero()) {
            request = request.timeout(timeout);
        }
        let this = self.clone();
        self.runtime.spawn(async move {
            let reply = match request.send().await {
                Err(error) => ApiReply::Failed { tag, message: error.to_string(), status: 0 },
                Ok(response) => {
                    let status = response.status();
                    match response.bytes().await {
                        Err(error) => ApiReply::Failed { tag, message: error.to_string(), status: status.as_u16() },
                        Ok(body) if !status.is_success() => ApiReply::Failed {
                            tag,
                            message: error_message(&body, &http_status_text(status)),
                            status: status.as_u16(),
                        },
                        Ok(body) => match serde_json::from_slice::<Value>(&body) {
                            Ok(Value::Object(object)) => ApiReply::Json { tag, object },
                            Ok(_) => ApiReply::Failed {
                                tag,
                                message: "Invalid JSON response: not an object".into(),
                                status: status.as_u16(),
                            },
                            Err(error) => ApiReply::Failed {
                                tag,
                                message: format!("Invalid JSON response: {error}"),
                                status: status.as_u16(),
                            },
                        },
                    }
                }
            };
            this.deliver(generation, reply);
        });
    }
}

fn http_status_text(status: reqwest::StatusCode) -> String {
    match status.canonical_reason() {
        Some(reason) => format!("HTTP {} {reason}", status.as_u16()),
        None => format!("HTTP {}", status.as_u16()),
    }
}

pub(crate) fn auth_headers(token: &str, accept: &'static str) -> HeaderMap {
    let mut headers = HeaderMap::new();
    headers.insert(ACCEPT, HeaderValue::from_static(accept));
    if !token.is_empty()
        && let Ok(value) = HeaderValue::from_str(&format!("Bearer {token}")) {
            headers.insert(AUTHORIZATION, value);
        }
    headers
}
