//! The Android push service. Runs on sender threads, with no ambient credentials.
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use clarp_kernel::{direct_push, fcm as rules, python};
use clarp_ports::clock::Clock;
use clarp_ports::fcm::{FcmHttp, Response};
use serde_json::{Value, json};

use crate::owner::{OwnerFile, text_at};
use crate::relay::Outcome;

const ISSUER: &str = "https://oauth2.googleapis.com/token";
const SCOPE: &str = "https://www.googleapis.com/auth/firebase.messaging";

pub struct Http {
    client: ureq::Agent,
    local: Option<String>,
}

impl Http {
    pub fn new(endpoint: Option<String>) -> Self {
        let config = ureq::Agent::config_builder()
            .timeout_global(Some(Duration::from_secs(10)))
            .max_redirects(0)
            .http_status_as_error(false)
            .build();
        Self {
            client: config.into(),
            local: endpoint,
        }
    }

    fn read(mut response: ureq::http::Response<ureq::Body>) -> Response {
        let retry_after = response
            .headers()
            .get("retry-after")
            .and_then(|s| s.to_str().ok())
            .unwrap_or("")
            .into();
        Response {
            status: response.status().as_u16(),
            retry_after,
            body: response
                .body_mut()
                .with_config()
                .limit(65536)
                .read_to_vec()
                .unwrap_or_default(),
        }
    }
}

impl FcmHttp for Http {
    fn exchange(&self, issuer: &str, assertion: &str) -> Option<Response> {
        let url = self
            .local
            .as_ref()
            .map_or_else(|| issuer.to_string(), |local| format!("{local}/token"));
        // The assertion consists entirely of base64url segments and dots.
        let form = format!(
            "grant_type=urn%3Aietf%3Aparams%3Aoauth%3Agrant-type%3Ajwt-bearer&assertion={assertion}"
        );
        self.client
            .post(&url)
            .header("Content-Type", "application/x-www-form-urlencoded")
            .send(form.as_bytes())
            .ok()
            .map(Self::read)
    }

    fn deliver(&self, project: &str, bearer: &str, message: &[u8]) -> Option<Response> {
        let base = self
            .local
            .as_deref()
            .unwrap_or("https://fcm.googleapis.com");
        let url = format!("{base}/v1/projects/{project}/messages:send");
        self.client
            .post(&url)
            .header("Authorization", &format!("Bearer {bearer}"))
            .header("Content-Type", "application/json")
            .send(message)
            .ok()
            .map(Self::read)
    }
}

#[derive(PartialEq)]
struct Identity {
    project: String,
    email: String,
    kid: String,
    issuer: String,
    path: PathBuf,
}

struct Account {
    id: Identity,
    pem: String,
}

impl Account {
    fn assertion(&self, now: i64) -> Option<String> {
        use ring::signature::{RSA_PKCS1_SHA256, RsaKeyPair};
        let der = self
            .pem
            .trim()
            .strip_prefix("-----BEGIN PRIVATE KEY-----")?
            .strip_suffix("-----END PRIVATE KEY-----")?;
        let der = python::b64decode(&der.split_whitespace().collect::<String>())?;
        let pair = RsaKeyPair::from_pkcs8(&der).ok()?;
        let mut header = json!({"alg":"RS256","typ":"JWT"});
        if !self.id.kid.is_empty() {
            header["kid"] = json!(self.id.kid);
        }
        let signed = direct_push::to_sign(
            &header,
            &json!({"iss":self.id.email,"scope":SCOPE,"aud":self.id.issuer,"iat":now,"exp":now+3600}),
        );
        let mut signature = vec![0; pair.public().modulus_len()];
        pair.sign(
            &RSA_PKCS1_SHA256,
            &ring::rand::SystemRandom::new(),
            signed.as_bytes(),
            &mut signature,
        )
        .ok()?;
        Some(format!("{signed}.{}", direct_push::base64url(&signature)))
    }
}

pub struct Sender {
    owner: OwnerFile,
    http: Arc<dyn FcmHttp>,
    clock: Arc<dyn Clock + Send + Sync>,
    cached: Mutex<Option<(Identity, String, f64)>>,
}

impl Sender {
    pub fn new(
        owner: OwnerFile,
        http: Arc<dyn FcmHttp>,
        clock: Arc<dyn Clock + Send + Sync>,
    ) -> Self {
        Self {
            owner,
            http,
            clock,
            cached: Mutex::new(None),
        }
    }

    fn account(&self) -> Option<Account> {
        let path = text_at(&self.owner.read()?, &["fcm", "service_account"])?;
        let path = path
            .strip_prefix("~/")
            .map_or_else(
                || PathBuf::from(&path),
                |rest| crate::config::home().join(rest),
            )
            .canonicalize()
            .ok()?;
        let value: Value = serde_json::from_slice(&std::fs::read(&path).ok()?).ok()?;
        if !value["type"].is_null() && value["type"] != "service_account" {
            return None;
        }
        let text = |k: &str| value[k].as_str().map(str::trim).filter(|s| !s.is_empty());
        let issuer = text("token_uri").unwrap_or(ISSUER);
        if !issuer.starts_with("https://") {
            return None;
        }
        Some(Account {
            id: Identity {
                project: text("project_id")?.into(),
                email: text("client_email")?.into(),
                kid: text("private_key_id").unwrap_or("").into(),
                issuer: issuer.into(),
                path,
            },
            pem: text("private_key")?.into(),
        })
    }

    pub fn configured(&self) -> bool {
        self.account().is_some()
    }

    fn bearer(&self, account: Account, refresh: bool) -> Option<String> {
        let now = self.clock.now_ms() as f64 / 1000.0;
        let mut cached = self.cached.lock().ok()?;
        if !refresh
            && let Some((id, token, expires)) = cached.as_ref()
            && id == &account.id
            && now < expires - 300.0
        {
            return Some(token.clone());
        }
        let response = self
            .http
            .exchange(&account.id.issuer, &account.assertion(now as i64)?)?;
        if response.status != 200 {
            return None;
        }
        let answer: Value = serde_json::from_slice(&response.body).ok()?;
        let token = answer["access_token"].as_str()?.to_string();
        let ttl = answer["expires_in"]
            .as_f64()
            .or_else(|| answer["expires_in"].as_str()?.parse().ok())
            .filter(|v| *v != 0.0)
            .unwrap_or(3600.0);
        *cached = Some((account.id, token.clone(), now + ttl));
        Some(token)
    }

    pub fn push(
        &self,
        token: &str,
        payload: &Value,
        context: &Value,
        priority: &str,
        collapse: &str,
        activity: bool,
    ) -> Outcome {
        let Some(data) = rules::data(payload, context, collapse, self.clock.now_ms()) else {
            return Outcome::Refused {
                status: 0,
                reason: "PayloadTooLarge".into(),
            };
        };
        let body =
            rules::message(token, data, priority, if activity { collapse } else { "" }).to_string();
        let mut sending = rules::Sending::default();
        let mut refresh = false;
        loop {
            let Some(account) = self.account() else {
                return Outcome::NoPath;
            };
            let project = account.id.project.clone();
            let Some(bearer) = self.bearer(account, refresh) else {
                return Outcome::Down("FCM authentication unavailable".into());
            };
            refresh = false;
            let next = match self.http.deliver(&project, &bearer, body.as_bytes()) {
                Some(answer) => sending.answered(
                    answer.status,
                    &serde_json::from_slice(&answer.body).unwrap_or(Value::Null),
                    &answer.retry_after,
                ),
                None => sending.answered(0, &Value::Null, ""),
            };
            match next {
                rules::Next::Refresh => refresh = true,
                rules::Next::Retry(secs) => std::thread::sleep(Duration::from_secs(secs)),
                rules::Next::Finished(200, _, _) => return Outcome::Sent,
                rules::Next::Finished(410, reason, _) => return Outcome::Dead(reason),
                rules::Next::Finished(status, reason, _) => {
                    return Outcome::Refused { status, reason };
                }
            }
        }
    }
}
