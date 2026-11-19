//! Global receipt numbers, dense per-form ordinals and immutable event rows.
//! A batch publishes its receipts, rows and counters in one fleet transaction.
use super::*;
use clarp_kernel::artifacts::form_events as rules;
use serde::{Serialize, de::DeserializeOwned};
use std::collections::HashMap;

const COUNTER: &str = "form-event-counter";
const TAIL: &str = "form-event-tails";
const ROW: &str = "form-event-rows";
const ID: &str = "form-event-ids";
const CONFIG: &str = "form-event-config";
type Write = (&'static str, String, Vec<u8>);
fn broken() -> ApiError {
    refused(
        StatusCode::INTERNAL_SERVER_ERROR,
        "invalid form event record",
    )
}
fn key(id: &str, suffix: &str) -> String {
    format!("{}:{id}{suffix}", id.len())
}
async fn read<T: DeserializeOwned>(
    app: &App,
    kind: &'static str,
    key: String,
) -> Result<Option<T>, ApiError> {
    app.fleet
        .kept(kind, key)
        .await?
        .map(|bytes| serde_json::from_slice(&bytes).map_err(|_| broken()))
        .transpose()
}
fn put(kind: &'static str, key: String, value: &impl Serialize) -> Result<Write, ApiError> {
    Ok((kind, key, serde_json::to_vec(value).map_err(|_| broken())?))
}
fn version(app: &App, id: &str, status: StatusCode) -> Result<Value, ApiError> {
    Fleet::of(app)
        .0
        .iter()
        .find_map(|(_, view)| {
            let form = view.books.artifacts.get(id)?;
            (form.kind == "html_form" && form.payload.get("read_only") != Some(&Value::Bool(true)))
                .then(|| form.payload.get("version").cloned())
                .flatten()
        })
        .ok_or_else(|| refused(status, "interactive form not found"))
}
pub(super) fn full(p: &Principal) -> Result<(), ApiError> {
    if matches!(
        p,
        Principal::Device {
            scope: Scope::Limited,
            ..
        }
    ) {
        return Err(refused(
            StatusCode::FORBIDDEN,
            "full device access required",
        ));
    }
    Ok(())
}
pub(crate) async fn configure(
    app: &App,
    id: &str,
    data: Option<&Value>,
) -> Result<Value, ApiError> {
    let bad = |why| refused(StatusCode::BAD_REQUEST, why);
    let key = data.map(rules::draft_key).transpose().map_err(bad)?;
    let v = version(app, id, StatusCode::BAD_REQUEST)?;
    let draft = if let Some(key) = key {
        app.fleet
            .change_together(vec![put(CONFIG, id.into(), key)?], vec![])
            .await?;
        key.clone()
    } else {
        read(app, CONFIG, id.into()).await?.unwrap_or(Value::Null)
    };
    Ok(json!({"artifact_id":id,"version":v,"draft_key":draft}))
}
pub(super) async fn append(app: &App, id: &str, data: &Value) -> Result<Value, ApiError> {
    let conflict = |why| refused(StatusCode::CONFLICT, why);
    let batch = rules::batch(data).map_err(conflict)?;
    let _writer = app.form_event_writer.lock().await;
    let v = version(app, id, StatusCode::CONFLICT)?;
    if data["version"] != v {
        return Err(conflict("form version mismatch"));
    }
    let mut pending = HashMap::new();
    let mut position: Option<(i64, i64)> = None;
    let mut writes = Vec::new();
    let mut accepted = Vec::new();
    for event in batch {
        let id_key = key(id, event.id);
        let existing = match pending.get(event.id) {
            Some(receipt) => Some(Clone::clone(receipt)),
            None => read::<(i64, String)>(app, ID, id_key.clone()).await?,
        };
        let seq = if let Some((seq, digest)) = existing {
            if digest != event.digest {
                return Err(conflict("event ID already used for different data"));
            }
            seq
        } else {
            let (ordinal, seq) = match position {
                Some(pair) => pair,
                None => {
                    let (ordinal, _): (i64, i64) =
                        read(app, TAIL, id.into()).await?.unwrap_or_default();
                    (
                        ordinal,
                        read(app, COUNTER, "last".into()).await?.unwrap_or_default(),
                    )
                }
            };
            let ordinal = ordinal.checked_add(1).ok_or_else(broken)?;
            let seq = seq.checked_add(1).ok_or_else(broken)?;
            position = Some((ordinal, seq));
            let row = json!({"seq":seq,"artifact_id":id,"version":v,"event_id":event.id,"client_seq":event.client_seq,"client_at":event.client_at,"received_at":app.clock.now_ms(),"event":event.value});
            let receipt = (seq, event.digest);
            writes.push(put(ID, id_key, &receipt)?);
            writes.push(put(ROW, key(id, &ordinal.to_string()), &row)?);
            pending.insert(event.id, receipt);
            seq
        };
        accepted.push(json!({"event_id":event.id,"seq":seq}));
    }
    if let Some((ordinal, seq)) = position {
        writes.push(put(TAIL, id.into(), &(ordinal, seq))?);
        writes.push(put(COUNTER, "last".into(), &seq)?);
        app.fleet.change_together(writes, vec![]).await?;
    }
    Ok(json!({"artifact_id":id,"version":v,"accepted":true,"events":accepted}))
}
async fn row(app: &App, id: &str, n: i64) -> Result<Value, ApiError> {
    read(app, ROW, key(id, &n.to_string()))
        .await?
        .ok_or_else(broken)
}
pub(crate) async fn page(
    app: &App,
    id: &str,
    after: i128,
    limit: i128,
    through: Option<i128>,
) -> Result<Value, ApiError> {
    version(app, id, StatusCode::BAD_REQUEST)?;
    let (count, last): (i64, i64) = read(app, TAIL, id.into()).await?.unwrap_or_default();
    let (after, limit, through) = rules::cursors(after, limit, through.unwrap_or(last.into()))
        .map_err(|s| refused(StatusCode::BAD_REQUEST, s))?;
    let mut lo = 0;
    let mut hi = count;
    if after > 0 {
        while lo < hi {
            let mid = lo + (hi - lo) / 2;
            if row(app, id, mid + 1).await?["seq"]
                .as_i64()
                .ok_or_else(broken)?
                <= after
            {
                lo = mid + 1;
            } else {
                hi = mid;
            }
        }
    }
    let mut events = Vec::new();
    let mut next = after;
    let mut more = false;
    while lo < count {
        lo += 1;
        let entry = row(app, id, lo).await?;
        let seq = entry["seq"].as_i64().ok_or_else(broken)?;
        if seq > through {
            break;
        }
        if events.len() == limit {
            more = true;
            break;
        }
        next = seq;
        events.push(entry);
    }
    Ok(
        json!({"artifact_id":id,"events":events,"next_seq":next,"snapshot_seq":through,"has_more":more}),
    )
}
pub(crate) fn jsonl(page: &Value) -> String {
    page["events"]
        .as_array()
        .into_iter()
        .flatten()
        .map(|row| python::dumps(row) + "\n")
        .collect()
}
