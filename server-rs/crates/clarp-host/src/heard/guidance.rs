use super::*;
use clarp_kernel::vocabulary::guidance::{self, Agent, GLOSSARY_KEY, NAMES_KEY};

pub(crate) fn active_names(directory: &Directory) -> Vec<String> {
    guidance::active_names(directory.members.iter().map(|m| {
        let r = &m.record;
        Agent {
            persona: &r.persona,
            id: &r.agent_id,
            role: &r.role,
            parent: r.parent_agent_id.as_deref().unwrap_or(""),
            latest_start: m.agent.view().latest_turn_start,
            created: r.created_at_ms,
            archived: r.archived_at_ms.is_some(),
            deleted: r.deleted_at_ms.is_some(),
            janitor: r.is_janitor,
        }
    }))
}

fn payload(directory: &Directory) -> Value {
    guidance::payload(
        guidance::names_enabled(directory.setting(NAMES_KEY)),
        directory.setting(GLOSSARY_KEY).unwrap_or(""),
        &active_names(directory),
    )
}

pub(super) async fn get_guidance(State(app): State<App>) -> Json<Value> {
    Json(payload(&app.fleet.directory()))
}

pub(super) async fn set_guidance(
    State(app): State<App>,
    body: axum::body::Bytes,
) -> Result<Json<Value>, ApiError> {
    let bad = |why| ApiError::new(StatusCode::BAD_REQUEST, why);
    let data = Value::Object(crate::personas::object(&body)?);
    let _save = app.ears.settings_lock.lock().await;
    let directory = app.fleet.directory();
    let (enabled, glossary) = guidance::update(
        guidance::names_enabled(directory.setting(NAMES_KEY)),
        directory.setting(GLOSSARY_KEY).unwrap_or(""),
        &data,
    )
    .map_err(bad)?;
    app.fleet
        .set(vec![
            (NAMES_KEY.into(), enabled.to_string()),
            (GLOSSARY_KEY.into(), glossary),
        ])
        .await?;
    let mut out = payload(&app.fleet.directory());
    out["ok"] = json!(true);
    Ok(Json(out))
}
