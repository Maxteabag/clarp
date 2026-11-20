//! Render specs as the Host serves them: published, revised and checked, read, and answered
//! once (`server/lib/routes/artifacts.py`, `render_spec_store.py`, Host contract 102).
//!
//! The rules are the kernel's ([`clarp_kernel::artifacts::render_spec`]); this reads the
//! bodies as the Python Host reads them, tells who may see a spec and answer it, and says a
//! refusal as that Host says it: `{"error": {code, path, message}}`, with the status of its
//! code and a line in the log that names the code and the pointer and nothing the caller
//! wrote.
//!
//! Who may see a spec is said by each request: `X-Clarp-Client: <platform>/<build>
//! contract=<n>` at or above the threshold the owner set in his file (`[artifacts]
//! render_spec_min_client`, read at every request). Without a threshold, as the reference
//! ships, no client is shown a spec and none can answer one.

use axum::http::{HeaderMap, StatusCode, header};
use axum::response::{IntoResponse, Response};
use clarp_kernel::artifacts::render_spec::{
    self as spec, CATALOG_UNAVAILABLE, Catalog, J, KIND, Obj, Publish, Refusal, Unavailable,
};
use clarp_kernel::artifacts::{Artifact, Ctx, Maker, Refused};
use clarp_kernel::python;
use clarp_speech::markup::strip_hidden_blocks;
use serde_json::{Value, json};

use crate::edge::{ApiError, App};
use crate::fleet::Member;
use crate::ids;
use crate::library::{self, Want};
use crate::owner::OwnerFile;

/// The header a client says who it is in.
const CLIENT_HEADER: &str = "x-clarp-client";

/// The lowest client contract that is shown a spec and may answer one, as the owner set it:
/// a whole number in his file, and nothing else.
fn threshold(app: &App) -> Option<i64> {
    let file = OwnerFile::new(app.config.speech_keys.clone()).read()?;
    let artifacts = file.get("artifacts")?.as_table()?;
    match artifacts.get("render_spec_min_client")? {
        toml::Value::Integer(minimum) => Some(*minimum),
        _ => None,
    }
}

/// What the client of a request says it is (`X-Clarp-Client`), as it said it: read by one
/// reader (`clarp_kernel::client`) wherever a rule asks it.
pub(crate) fn client(headers: &HeaderMap) -> Option<&str> {
    headers
        .get(CLIENT_HEADER)
        .and_then(|value| value.to_str().ok())
}

/// Whether the client of this request can see a spec and answer it.
pub(crate) fn capable(app: &App, headers: &HeaderMap) -> bool {
    let header = client(headers);
    header.is_some() && spec::capable(header, threshold(app))
}

/// What a refusal is answered with, as the Python Host answers it, and the line it logs.
pub(crate) fn answer(refusal: &Refusal, catalog: Result<&Catalog, Unavailable>) -> ApiError {
    let triple = refusal.triple(catalog);
    let said = |key: &str| triple[key].as_str().unwrap_or_default().to_string();
    let (code, path) = (said("code"), said("path"));
    eprintln!("clarp-host: renderSpecRefused code={code} path={path}");
    let status = StatusCode::from_u16(Refusal::status(&code)).unwrap_or(StatusCode::BAD_REQUEST);
    ApiError::detailed(status, code, triple)
}

/// A refusal of the rules of artifacts, where it may be one of a render spec.
fn refused(why: Refused, catalog: Result<&Catalog, Unavailable>) -> ApiError {
    // What else is refused here is a name that is taken, as the reference's database says.
    match Refusal::of(&why) {
        Some(refusal) => answer(&refusal, catalog),
        None => ApiError::new(StatusCode::CONFLICT, why.message),
    }
}

/// A refusal of the rules of artifacts, said as the Python Host says it.
pub(crate) fn said(why: Refused) -> ApiError {
    let catalog = Catalog::embedded();
    refused(why, catalog.as_ref().map_err(|e| *e))
}

fn no(code: &'static str, path: &str, catalog: Result<&Catalog, Unavailable>) -> ApiError {
    answer(&Refusal::new(code, path), catalog)
}

/// `str(value)`, as Python makes a text of a value that is not one.
fn python_str(value: &J) -> String {
    match value {
        J::Text(text) => text.lossy(),
        J::Int(number) => number.to_string(),
        J::Float(number) => python::float(*number),
        J::Bool(true) => "True".to_string(),
        J::Bool(false) => "False".to_string(),
        J::Null => "None".to_string(),
        other => python::repr(&other.to_value()),
    }
}

/// `str(data.get(key) or otherwise)`.
fn field(data: &Obj, key: &str, otherwise: &str) -> String {
    match data.get(key) {
        Some(value) if value.truthy() => python_str(value),
        _ => otherwise.to_string(),
    }
}

/// An id of an artifact (`^[A-Za-z0-9._:-]{1,180}$`).
fn valid_id(id: &str) -> bool {
    (1..=180).contains(&id.len())
        && id
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"._:-".contains(&byte))
}

/// Whether a request asks to check only (`?validate_only=1`, said once, as `parse_qs`
/// reads it).
pub(crate) fn validate_only(query: Option<&str>) -> bool {
    // A `+` would be a blank: neither the name nor the one value counted has either.
    let decoded = |part: &str| {
        let bytes = part.as_bytes();
        let mut out = Vec::with_capacity(bytes.len());
        let mut at = 0;
        while at < bytes.len() {
            let hex = bytes
                .get(at + 1..at + 3)
                .and_then(|two| std::str::from_utf8(two).ok())
                .and_then(|two| u8::from_str_radix(two, 16).ok());
            match (bytes[at], hex) {
                (b'%', Some(byte)) => {
                    out.push(byte);
                    at += 3;
                }
                (byte, _) => {
                    out.push(byte);
                    at += 1;
                }
            }
        }
        String::from_utf8_lossy(&out).into_owned()
    };
    let said: Vec<String> = query
        .unwrap_or_default()
        .split('&')
        .filter_map(|pair| pair.split_once('='))
        .filter(|(_, value)| !value.is_empty())
        .filter(|(name, _)| decoded(name) == "validate_only")
        .map(|(_, value)| decoded(value))
        .collect();
    said == ["1"]
}

/// A body read as the Python Host reads one at `/artifacts`: an empty body is an empty
/// object. `None`: it is no JSON to that Host.
pub(crate) fn body(bytes: &[u8]) -> Option<J> {
    if bytes.is_empty() {
        return Some(J::Object(Obj::new()));
    }
    spec::json::read(bytes)
}

/// Whether a body asks for a render spec to be made (`type`, without its blanks and in
/// small letters).
pub(crate) fn is_spec(data: &Obj) -> bool {
    let kind = data.get("type").filter(|kind| kind.truthy());
    kind.is_some_and(|kind| python::strip(&python_str(kind)).to_lowercase() == KIND)
}

/// What a publication says besides its payload.
struct Envelope {
    session: String,
    title: String,
    summary: String,
    status: String,
    reference_id: String,
    artifact_id: String,
    payload: J,
    expected: Option<J>,
}

/// `POST /artifacts` of a render spec, or of its check alone (`?validate_only=1`).
/// Answers the status and the body.
pub(crate) async fn publish(
    app: &App,
    data: &Obj,
    validate_only: bool,
    capable: bool,
    turn: Option<&str>,
) -> Result<(StatusCode, Value), ApiError> {
    let catalog = Catalog::embedded();
    if data.duplicate {
        return Err(no("invalid_object", "", catalog.as_ref().map_err(|e| *e)));
    }
    // The route makes a text of every other value (`str()`): only a text can fail to be one.
    let texts: Vec<(&str, &J)> = spec::ENVELOPE_TEXTS
        .iter()
        .filter_map(|key| Some((*key, data.get(key)?)))
        .filter(|(_, value)| matches!(value, J::Text(_)))
        .collect();
    if let Err(refusal) = spec::envelope(&texts) {
        return Err(answer(&refusal, catalog.as_ref().map_err(|e| *e)));
    }
    let envelope = Envelope {
        session: field(data, "session", ""),
        title: field(data, "title", ""),
        summary: field(data, "summary", ""),
        status: field(data, "status", "ready"),
        reference_id: field(data, "reference_id", ""),
        artifact_id: field(data, "artifact_id", ""),
        payload: data.get("payload").cloned().unwrap_or(J::Null),
        expected: data.get("expected_version").cloned(),
    };
    let row = published(app, envelope, validate_only, capable, turn).await?;
    Ok(match row {
        None => (StatusCode::OK, json!({ "valid": true })),
        Some(row) => (StatusCode::CREATED, json!({ "artifact": row })),
    })
}

/// `POST /artifacts/<id>` of a render spec: a whole new revision that names the version it
/// follows (`artifacts.update`). `current` is the spec as it is.
pub(crate) async fn update(
    app: &App,
    current: &Artifact,
    data: &Obj,
    capable: bool,
) -> Result<Value, ApiError> {
    let catalog = Catalog::embedded();
    let catalog = catalog.as_ref().map_err(|e| *e);
    if data.duplicate {
        return Err(no("invalid_object", "", catalog));
    }
    if data.contains("payload_patch") {
        return Err(no("unknown_key", "/payload_patch", catalog));
    }
    let Some(payload) = data.get("payload") else {
        return Err(no("missing_key", "/payload", catalog));
    };
    // Each as the body has it, before anything strips or slices it; one that is not given
    // stays as it is (R72).
    let given: Vec<(&str, &J)> = ["title", "summary", "status"]
        .iter()
        .filter_map(|key| Some((*key, data.get(key)?)))
        .collect();
    if let Err(refusal) = spec::envelope(&given) {
        return Err(answer(&refusal, catalog));
    }
    let or = |key: &str, otherwise: &str| data.get(key).map_or(otherwise.to_string(), python_str);
    let envelope = Envelope {
        session: current.session.clone(),
        title: or("title", &current.title),
        summary: or("summary", &current.summary),
        status: or("status", &current.status),
        reference_id: current.reference_id.clone(),
        artifact_id: current.artifact_id.clone(),
        payload: payload.clone(),
        expected: data.get("expected_version").cloned(),
    };
    // A revision: the trace stays the one the spec was first published under.
    let row = published(app, envelope, false, capable, None).await?;
    Ok(json!({ "artifact": row.unwrap_or(Value::Null) }))
}

/// A render spec published or checked: the row as this client is shown it, or `None` when
/// it was only checked (`artifacts._create` and `render_spec_store.publish`).
async fn published(
    app: &App,
    envelope: Envelope,
    validate_only: bool,
    capable: bool,
    turn: Option<&str>,
) -> Result<Option<Value>, ApiError> {
    let catalog = Catalog::embedded();
    let known = catalog.as_ref().map_err(|e| *e);
    let title = python::strip(&strip_hidden_blocks(&envelope.title)).to_string();
    let status = python::strip(&envelope.status).to_lowercase();
    let Some(member) = app.member(python::strip(&envelope.session)) else {
        return Err(no("not_open", "/session", known));
    };
    if title.is_empty() {
        return Err(no("missing_key", "/title", known));
    }
    let artifact_id = python::strip(&envelope.artifact_id).to_string();
    if !artifact_id.is_empty() && !valid_id(&artifact_id) {
        return Err(no("element_id", "/artifact_id", known));
    }
    let Ok(catalog) = known else {
        return Err(no(CATALOG_UNAVAILABLE, "", known));
    };
    let fail = |refusal: Refusal| answer(&refusal, Ok(catalog));
    spec::validate(catalog, &envelope.payload).map_err(fail)?;
    let stored = spec::stored(catalog, &envelope.payload).map_err(fail)?;
    let elsewhere = !artifact_id.is_empty()
        && app.fleet.directory().members.iter().any(|other| {
            other.record.agent_id != member.record.agent_id
                && other
                    .agent
                    .view()
                    .books
                    .artifacts
                    .any(&artifact_id)
                    .is_some()
        });
    let asked = Publish {
        artifact_id,
        title,
        summary: envelope.summary,
        status,
        reference_id: envelope.reference_id,
        stored,
        expected: envelope.expected.filter(|expected| *expected != J::Null),
        validate_only,
        elsewhere,
        // Kept by a new spec only: a revision keeps the one it has.
        source_trace_id: crate::turn_identity::publishing_trace(app, turn, &member.record.agent_id)
            .await?,
    };
    if validate_only {
        checked(app, &member, &asked).map_err(|why| refused(why, Ok(catalog)))?;
        return Ok(None);
    }
    let done = member
        .agent
        .artifact(Want::SpecPublish(Box::new(asked)))
        .await?;
    let done = done.map_err(|why| refused(why, Ok(catalog)))?;
    Ok(Some(library::shown(app, done.artifact, "", capable).await))
}

/// The check of a publication that keeps nothing, against the agent's artifacts as they
/// are: nothing is written, so the agent's thread is not asked.
fn checked(app: &App, member: &Member, asked: &Publish) -> Result<(), Refused> {
    let view = member.agent.view();
    let book = &view.books.artifacts;
    let maker = Maker {
        agent_id: &member.record.agent_id,
        session: &member.record.session,
        persona: &member.record.persona,
    };
    let mut ctx = Ctx {
        now_ms: app.clock.now_ms(),
        token_hex: &mut ids::hex,
        taken: &|id| book.any(id).is_some(),
        published: &view.books.media,
        elsewhere: &|_| false,
        page: &|_| None,
    };
    book.spec_published(maker, asked, &mut ctx).map(|_| ())
}

/// `POST /artifacts/<id>/submit` to a render spec: the receipt (`render_spec_store.submit`).
/// `form` is the spec, also one that was thrown away, and `holder` the agent that has it.
pub(crate) async fn submit(
    app: &App,
    holder: &Member,
    form: &Artifact,
    data: &J,
    capable: bool,
) -> Result<Value, ApiError> {
    let catalog = Catalog::embedded();
    let known = catalog.as_ref().map_err(|e| *e);
    if !capable {
        return Err(no("client_contract", "", known));
    }
    let Ok(catalog) = known else {
        return Err(no(CATALOG_UNAVAILABLE, "", known));
    };
    // A submission id is used once on the whole Host: one under it in another agent's
    // book is the submission it comes again to.
    let sid = data.get("submission_id").and_then(J::str);
    // The agent's own submissions are looked for on its own thread, where two under one id are
    // taken one after the other.
    let elsewhere = sid.and_then(|sid| {
        let directory = app.fleet.directory();
        let others = directory.members.iter();
        let mut others = others.filter(|other| other.record.agent_id != holder.record.agent_id);
        others.find_map(|other| {
            let view = other.agent.view();
            let (artifact, submission) = view.books.artifacts.submission(sid)?;
            Some((artifact.artifact_id.clone(), submission.clone()))
        })
    });
    let want = Want::SpecSubmit {
        artifact_id: form.artifact_id.clone(),
        data: Box::new(data.clone()),
        elsewhere: elsewhere.map(Box::new),
        catalog: Box::new(catalog.clone()),
    };
    let done = holder.agent.artifact(want).await?;
    let done = done.map_err(|why| refused(why, Ok(catalog)))?;
    done.receipt
        .ok_or_else(|| ApiError::new(StatusCode::INTERNAL_SERVER_ERROR, "no receipt"))
}

/// The artifact itself, for a client that can see the spec: the spec, what it is, and once
/// it was answered the answers.
pub(crate) fn opened(mut row: Value, artifact: &Artifact) -> Result<Value, ApiError> {
    let catalog = Catalog::embedded();
    if catalog.is_err() {
        return Err(no(CATALOG_UNAVAILABLE, "", Err(Unavailable)));
    }
    spec::opened(&mut row, artifact);
    Ok(row)
}

/// `GET /artifact-catalogs/clarp.form/1`: the catalog's bytes, as both Hosts have them.
pub(crate) async fn catalog() -> Response {
    match Catalog::embedded() {
        Ok(catalog) => ([(header::CONTENT_TYPE, "application/json")], catalog.raw).into_response(),
        Err(unavailable) => no(CATALOG_UNAVAILABLE, "", Err(unavailable)).into_response(),
    }
}
