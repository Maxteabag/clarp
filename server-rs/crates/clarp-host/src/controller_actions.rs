//! The registry, invocation receipts and bounded history for controller gestures.
use crate::{
    edge::{ApiError, App, Principal},
    ids,
};
use axum::{
    Extension, Json, Router,
    body::Bytes,
    extract::{RawQuery, State},
    http::{HeaderMap, StatusCode, Uri},
    response::{IntoResponse, Response},
    routing::get,
};
use clarp_kernel::{
    controller_actions::{self as rule, Error, Registry},
    destination::{self, Destination},
    python,
};
use serde::{Deserialize, Serialize, de::DeserializeOwned};
use serde_json::{Value, json};
use std::{
    collections::HashMap,
    io::Read,
    time::{Duration, Instant},
};
use tokio::sync::{Mutex, watch};

const KIND: &str = "controller-actions";
const RECEIPTS: &str = "controller-action-receipts";
type Answer = Result<Value, Error>;
type Row = (&'static str, String, Vec<u8>);
#[derive(Default)]
pub struct Desk(Mutex<Runtime>);
#[derive(Default)]
struct Runtime {
    pending: HashMap<String, watch::Sender<Option<Answer>>>,
    last: HashMap<String, (Instant, String, Option<Value>)>,
}
#[derive(Serialize, Deserialize)]
struct Receipt {
    at: i64,
    result: Value,
}

fn unavailable<T>(_: T) -> Error {
    (503, "controller action storage unavailable".into())
}
async fn read<T: DeserializeOwned + Default>(app: &App, key: &str) -> Result<T, Error> {
    match app
        .fleet
        .kept(KIND, key.into())
        .await
        .map_err(unavailable)?
    {
        Some(bytes) => serde_json::from_slice(&bytes).map_err(unavailable),
        None => Ok(T::default()),
    }
}
fn row(kind: &'static str, key: &str, value: &impl Serialize) -> Result<Row, Error> {
    Ok((
        kind,
        key.into(),
        serde_json::to_vec(value).map_err(unavailable)?,
    ))
}
async fn audits(app: &App, event: Value) -> Result<Row, Error> {
    let mut rows: Vec<Value> = read(app, "audit").await?;
    rows.push(event);
    if rows.len() > rule::AUDIT_KEEP {
        rows.drain(..rows.len() - rule::AUDIT_KEEP);
    }
    row(KIND, "audit", &rows)
}
/// The owner's `[controller_actions] allowed_urls`, each parsed once as a prefix
/// (`destination::prefix`): one that carries user information, a query or a fragment, or does
/// not parse, admits nothing, and one line says how many there were (R133,
/// `config._controller_action_urls`).
fn allow_list(app: &App) -> Vec<Destination> {
    let cfg = app.mouth.owners();
    let texts: Vec<String> = cfg
        .get("controller_actions")
        .and_then(|v| v.get("allowed_urls"))
        .and_then(|v| v.as_array())
        .into_iter()
        .flatten()
        // `str(url).strip()`: a text as it is; anything else is no empty text, and no URL: it
        // is refused (what it is called does not matter).
        .map(|v| v.as_str().map_or_else(|| format!("{v:?}"), str::to_string))
        .map(|text| python::strip(&text).to_string())
        .filter(|text| !text.is_empty())
        .collect();
    let kept: Vec<Destination> = texts
        .iter()
        .filter_map(|text| destination::prefix(text))
        .collect();
    // Said once for a list, as the Python Host says it once when it reads its file.
    static SAID: std::sync::Mutex<Vec<String>> = std::sync::Mutex::new(Vec::new());
    let mut said = SAID
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner);
    if kept.len() != texts.len() && *said != texts {
        said.clone_from(&texts);
        eprintln!(
            "config.controller_action_urls refused {} allowed URL(s) that do not parse as a plain http(s) destination",
            texts.len() - kept.len()
        );
    }
    kept
}
pub(crate) async fn change(app: &App, op: &str, data: &Value, actor: &str) -> Answer {
    let _serial = app.controller_actions.0.lock().await;
    let mut state: Registry = read(app, "registry").await?;
    let now = app.clock.now_ms();
    let (result, audit) = state.change(op, data, &allow_list(app), actor, now)?;
    app.fleet
        .keep_together(vec![
            row(KIND, "registry", &state)?,
            audits(app, audit).await?,
        ])
        .await
        .map_err(unavailable)?;
    app.hub.ephemeral(
        json!({"type":"controller-actions-updated","revision":state.revision}),
        now,
    );
    Ok(result)
}
pub(crate) async fn list(app: &App) -> Answer {
    Ok(read::<Registry>(app, "registry").await?.screen())
}
pub(crate) async fn audit(app: &App, limit: &str) -> Answer {
    let n = if limit.is_empty() {
        50
    } else {
        python::int(limit)
            .ok_or((400, "limit must be an integer".to_owned()))?
            .clamp(0, 500) as usize
    };
    let rows: Vec<Value> = read(app, "audit").await?;
    Ok(json!({"entries":rows.into_iter().rev().take(n).collect::<Vec<_>>() }))
}
async fn execute(app: &App, action: &Value) -> Value {
    if action["type"] == "http_post" {
        // Checked again at every run (an action stored before a rule changed is held to the
        // rule of now), and what is sent is the parsed destination, not the stored text.
        let Some(uri) =
            destination::allowed(action["url"].as_str().unwrap_or_default(), &allow_list(app))
        else {
            return json!({"ok":false,"error":"url is no longer allow-listed"});
        };
        let body = python::dumps_ascii(&action["body"]);
        let posted = tokio::task::spawn_blocking(move || -> Result<Value, String> {
            let client: ureq::Agent = ureq::Agent::config_builder()
                .proxy(None)
                .max_redirects(0)
                .http_status_as_error(false)
                .timeout_global(Some(Duration::from_secs(5)))
                .build()
                .into();
            let mut reply = client
                .post(uri)
                .header("Content-Type", "application/json")
                .header("User-Agent", "clarp-controller-action")
                .send(body.as_bytes())
                .map_err(|e| e.to_string())?;
            let status = reply.status().as_u16();
            let mut bytes = Vec::new();
            reply
                .body_mut()
                .as_reader()
                .take(2000)
                .read_to_end(&mut bytes)
                .map_err(|e| e.to_string())?;
            Ok(rule::http_result(status, &String::from_utf8_lossy(&bytes)))
        })
        .await
        .unwrap_or_else(|_| Err("HTTP action worker ended".into()));
        return posted.unwrap_or_else(|why| json!({"ok":false,"error":python::first(&why,300)}));
    }
    let send = crate::edge::send_message(
        app,
        clarp_protocol::requests::SendRequest {
            session: action["session"].as_str().unwrap_or_default().into(),
            text: action["text"].as_str().unwrap_or_default().into(),
            force_session: Some(true),
            synthesize_audio: Some(false),
            hands_free: Some(false),
            origin: Some("automation".into()),
            client_msg_id: Some(format!("controller-action-{}", ids::hex(8))),
            ..Default::default()
        },
    );
    match tokio::time::timeout(Duration::from_secs(15), send).await {
        Ok(Ok(_)) => json!({"ok":true}),
        Ok(Err(e)) => {
            let status = e.status().as_u16();
            let reply = axum::body::to_bytes(e.into_response().into_body(), usize::MAX)
                .await
                .unwrap_or_default();
            let detail = String::from_utf8_lossy(&reply[..reply.len().min(300)]);
            json!({"ok":false,"error":format!("HTTP {status}: {detail}")})
        }
        Err(_) => json!({"ok":false,"error":"timed out"}),
    }
}
async fn record_result(app: &App, request: &str, result: Value, event: Value) -> Answer {
    let receipt = Receipt {
        at: app.clock.now_ms(),
        result: result.clone(),
    };
    // Decide expiry while holding the completion lock. Never remove a newer
    // receipt written while this action was executing or delete the row we replace.
    let mut expired = Vec::new();
    for (key, bytes) in app.fleet.kept_all(RECEIPTS).await.map_err(unavailable)? {
        let prior: Receipt = serde_json::from_slice(&bytes).map_err(unavailable)?;
        if key != request && !rule::receipt_current(prior.at, receipt.at) {
            expired.push((RECEIPTS, key));
        }
    }
    app.fleet
        .change_together(
            vec![row(RECEIPTS, request, &receipt)?, audits(app, event).await?],
            expired,
        )
        .await
        .map_err(unavailable)?;
    Ok(result)
}
fn duplicate(mut result: Value) -> Value {
    result["duplicate"] = true.into();
    result
}
pub(crate) async fn invoke(app: &App, id: String, data: Value, actor: String) -> Answer {
    // A dropped HTTP connection never cancels a started effect or strands its waiters.
    let app = app.clone();
    tokio::spawn(async move { invoke_owned(&app, id, data, actor).await })
        .await
        .map_err(unavailable)?
}
async fn invoke_owned(app: &App, id: String, data: Value, actor: String) -> Answer {
    let request = rule::request_id(&data)?;
    let source = rule::source(&data);
    let mut rt = app.controller_actions.0.lock().await;
    let action = read::<Registry>(app, "registry").await?.enabled(&id)?;
    let now = app.clock.now_ms();
    if let Some(bytes) = app
        .fleet
        .kept(RECEIPTS, request.clone())
        .await
        .map_err(unavailable)?
    {
        let receipt: Receipt = serde_json::from_slice(&bytes).map_err(unavailable)?;
        if rule::receipt_current(receipt.at, now) {
            return Ok(duplicate(receipt.result));
        }
    }
    if let Some(wait) = rt.pending.get(&request) {
        let mut rx = wait.subscribe();
        drop(rt);
        rx.changed().await.map_err(unavailable)?;
        let answer = rx
            .borrow_and_update()
            .clone()
            .ok_or_else(|| unavailable("missing receipt"))?;
        return answer.map(duplicate);
    }
    rt.last
        .retain(|_, (start, _, _)| start.elapsed() < Duration::from_millis(rule::DEBOUNCE_MS));
    if let Some((start, _, last)) = rt.last.get(&id)
        && let Some(result) =
            rule::debounced(&action, start.elapsed().as_millis() as u64, last.as_ref())
    {
        let event =
            json!({"event":"debounced","at":now,"actor":actor,"action_id":id,"source":source});
        return record_result(app, &request, result, event).await;
    }
    let (done, _) = watch::channel(None);
    rt.pending.insert(request.clone(), done.clone());
    let started = Instant::now();
    rt.last.insert(id.clone(), (started, request.clone(), None));
    drop(rt);
    let mut result = execute(app, &action).await;
    result["action_id"] = json!(id);
    result["title"] = action["title"].clone();
    result["type"] = action["type"].clone();
    result["duration_ms"] = json!(started.elapsed().as_millis() as u64);
    let mut rt = app.controller_actions.0.lock().await;
    let event = json!({"at":app.clock.now_ms(),"event":"run","actor":actor,"action_id":id,"ok":result["ok"],"error":result["error"].as_str().unwrap_or(""),"status":result["status"],"duration_ms":result["duration_ms"],"source":source});
    if let Some((_, owner, last)) = rt.last.get_mut(&id)
        && *owner == request
    {
        *last = Some(result.clone());
    }
    let answer = record_result(app, &request, result, event).await;
    done.send_replace(Some(answer.clone()));
    rt.pending.remove(&request);
    answer
}

fn response(code: StatusCode, result: Answer) -> Response {
    match result {
        Ok(v) => (code, Json(v)).into_response(),
        Err((n, msg)) => ApiError::new(
            StatusCode::from_u16(n).unwrap_or(StatusCode::BAD_REQUEST),
            msg,
        )
        .into_response(),
    }
}
async fn index(State(app): State<App>) -> Response {
    response(StatusCode::OK, list(&app).await)
}
async fn audit_route(State(app): State<App>, RawQuery(query): RawQuery) -> Response {
    let limit = url::form_urlencoded::parse(query.as_deref().unwrap_or_default().as_bytes())
        .find(|(k, _)| k == "limit")
        .map(|(_, v)| v.into_owned())
        .unwrap_or_default();
    response(StatusCode::OK, audit(&app, &limit).await)
}
async fn post(
    State(app): State<App>,
    Extension(principal): Extension<Principal>,
    headers: HeaderMap,
    uri: Uri,
    body: Bytes,
) -> Response {
    let data = if body.is_empty() {
        json!({})
    } else {
        serde_json::from_slice(&body).unwrap_or(Value::Null)
    };
    if !data.is_object() {
        return ApiError::new(StatusCode::BAD_REQUEST, "bad json").into_response();
    }
    let actor = match crate::turn_identity::caller(
        &app,
        headers
            .get(crate::turn_identity::HEADER)
            .and_then(|h| h.to_str().ok())
            .unwrap_or(""),
    )
    .await
    {
        Ok(Some(member)) => format!("agent:{}", member.record.session),
        Err(e) => return response(StatusCode::OK, Err(unavailable(e))),
        Ok(None) => match &principal {
            Principal::Device { device_id, .. } => format!("device:{device_id}"),
            _ => "administrator".into(),
        },
    };
    if let Some(id) = clarp_kernel::devices::invoked(uri.path()) {
        return response(StatusCode::OK, invoke(&app, id, data, actor).await);
    }
    let op = match uri.path() {
        "/controller-actions" => "create",
        "/controller-actions/update" => "update",
        "/controller-actions/delete" => "delete",
        "/controller-actions/mappings" => "mappings",
        _ => return ApiError::new(StatusCode::NOT_FOUND, "not found").into_response(),
    };
    if op != "mappings" && principal != Principal::Administrator {
        return ApiError::new(
            StatusCode::FORBIDDEN,
            "only a local administrator can define controller actions",
        )
        .into_response();
    }
    response(
        if op == "create" {
            StatusCode::CREATED
        } else {
            StatusCode::OK
        },
        change(&app, op, &data, &actor).await,
    )
}
pub(crate) fn routes() -> Router<App> {
    Router::new()
        .route("/controller-actions", get(index).post(post))
        .route("/controller-actions/audit", get(audit_route).post(post))
        .route("/controller-actions/{*rest}", axum::routing::post(post))
}
