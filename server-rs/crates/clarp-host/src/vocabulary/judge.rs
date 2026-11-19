//! The optional junk-transcript judgment. Transport failures leave the phrase
//! filter in charge and never include the transcript or credentials in logs.
use crate::edge::{ApiError, App};
use axum::{Json, body::Bytes, extract::State, http::StatusCode};
use serde_json::{Value, json};
use std::sync::Mutex;
use std::time::{Duration, Instant};

const SITES: [&str; 7] = [
    "errors",
    "junk",
    "router",
    "naming",
    "watch",
    "explanations",
    "labels",
];
const URL: &str = "https://api.typesafe.ai/v1/systemone";

#[derive(Default)]
struct Breaker {
    failures: u32,
    until: Option<Instant>,
}
#[derive(Default)]
pub(crate) struct Judge {
    breaker: Mutex<Breaker>,
}
impl Judge {
    fn remaining(&self) -> f64 {
        self.breaker
            .lock()
            .unwrap_or_else(|p| p.into_inner())
            .until
            .map_or(0.0, |t| {
                t.saturating_duration_since(Instant::now()).as_secs_f64()
            })
    }

    fn record(&self, success: bool, billing: bool) {
        let mut b = self.breaker.lock().unwrap_or_else(|p| p.into_inner());
        if success {
            *b = Breaker::default();
            return;
        }
        b.failures = b.failures.saturating_add(1);
        if billing || b.failures >= 3 {
            let until = Instant::now() + Duration::from_secs(if billing { 3600 } else { 120 });
            b.until = Some(b.until.map_or(until, |held| held.max(until)));
        }
    }
}

fn budget(dir: &crate::fleet::Directory) -> u64 {
    dir.setting("judgments.timeout_ms")
        .and_then(|s| s.parse::<u64>().ok())
        .unwrap_or(900)
        .clamp(100, 30000)
}

pub async fn kept(app: &App, text: String) -> String {
    let directory = app.fleet.directory();
    let enabled = |k: &str| directory.setting(k) == Some("true");
    let judge = &app.ears.judge;
    let fallback = || clarp_kernel::heard::kept(&text);
    if !enabled("judgments.enabled") || !enabled("judgments.junk") || judge.remaining() > 0.0 {
        return fallback();
    }
    let Some(key) = app.ears.key("typesafe") else {
        return fallback();
    };
    let url = if app.config.judge_endpoint.is_empty() {
        URL
    } else {
        &app.config.judge_endpoint
    }
    .to_string();
    let timeout = budget(&directory);
    let state = json!({"model":"jev-latest","state":{"transcript":text},"questions":{"junk":*clarp_kernel::vocabulary::JUNK_QUESTION}});
    let verdict = tokio::task::spawn_blocking(move || {
        let client: ureq::Agent = ureq::Agent::config_builder()
            .timeout_global(Some(Duration::from_millis(timeout)))
            .max_redirects(0)
            .http_status_as_error(false)
            .build()
            .into();
        let mut response = client
            .post(url)
            .header("Authorization", format!("Bearer {key}"))
            .send_json(state)
            .map_err(|_| false)?;
        if !response.status().is_success() {
            return Err(response.status().as_u16() == 402);
        }
        let answer: Value = response.body_mut().read_json().map_err(|_| false)?;
        let junk = answer["answers"].get("junk").ok_or(false)?;
        let n = &junk["noul"];
        if n.is_null() {
            return Ok(0.0);
        }
        n.as_f64()
            .or_else(|| n.as_str()?.trim().parse().ok())
            .filter(|f| f.is_finite())
            .ok_or(false)
    })
    .await
    .unwrap_or(Err(false));
    match verdict {
        Ok(n) => {
            judge.record(true, false);
            clarp_kernel::vocabulary::judged_text(&text, Some(n))
        }
        Err(billing) => {
            judge.record(false, billing);
            fallback()
        }
    }
}

fn status(app: &App) -> Value {
    let dir = app.fleet.directory();
    let sites: serde_json::Map<_, _> = SITES
        .iter()
        .map(|s| {
            (
                s.to_string(),
                json!(dir.setting(&format!("judgments.{s}")) == Some("true")),
            )
        })
        .collect();
    json!({"credential":"TYPESAFE_API_KEY","credential_present":app.ears.key("typesafe").is_some(),"enabled":dir.setting("judgments.enabled")==Some("true"),
        "timeout_ms":budget(&dir),"breaker_open_seconds":(app.ears.judge.remaining()*10.0).round()/10.0,"sites":sites})
}

pub async fn get(State(app): State<App>) -> Json<Value> {
    Json(
        json!({"settings":status(&app),"sites":SITES,"defaults":{"enabled":false,"timeout_ms":900},"limits":{"timeout_ms":[100,30000]}}),
    )
}

pub async fn set(State(app): State<App>, body: Bytes) -> Result<Json<Value>, ApiError> {
    let bad = |s: &str| ApiError::new(StatusCode::BAD_REQUEST, s);
    let data: Value = serde_json::from_slice(&body)
        .ok()
        .filter(Value::is_object)
        .ok_or_else(|| bad("bad json"))?;
    let mut changes = Vec::new();
    if let Some(v) = data.get("enabled") {
        changes.push((
            "judgments.enabled".into(),
            v.as_bool()
                .ok_or_else(|| bad("enabled must be a boolean"))?
                .to_string(),
        ));
    }
    if let Some(v) = data.get("timeout_ms") {
        let n = v
            .as_i64()
            .ok_or_else(|| bad("timeout_ms must be an integer"))?;
        if !(100..=30000).contains(&n) {
            return Err(bad("timeout_ms must be between 100 and 30000"));
        }
        changes.push(("judgments.timeout_ms".into(), n.to_string()));
    }
    if let Some(sites) = data.get("sites").filter(|v| !v.is_null()) {
        for (k, v) in sites
            .as_object()
            .ok_or_else(|| bad("sites must be an object"))?
        {
            if !SITES.contains(&k.as_str()) {
                return Err(bad(&format!("unknown judgment site: {k}")));
            }
            let flag = v
                .as_bool()
                .ok_or_else(|| bad(&format!("sites.{k} must be a boolean")))?;
            if k != "junk" && flag {
                return Err(ApiError::new(
                    StatusCode::NOT_IMPLEMENTED,
                    format!("the {k} judgment site is not built on this Host yet"),
                ));
            }
            changes.push((format!("judgments.{k}"), flag.to_string()));
        }
    }
    app.fleet.set(changes).await?;
    app.ears.judge.record(true, false);
    Ok(Json(json!({"ok":true,"settings":status(&app)})))
}
