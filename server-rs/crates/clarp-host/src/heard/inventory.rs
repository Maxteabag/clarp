//! The Host's installed models, registered through its own storage port.
use super::*;
use crate::fleet::FleetHandle;
use clarp_kernel::heard::inventory as rules;
use std::path::Path;
use std::sync::RwLock;

pub(super) struct Inventory(RwLock<Arc<Vec<Value>>>);

impl Inventory {
    pub(super) async fn open(fleet: &crate::fleet::FleetHandle) -> Result<Self, String> {
        let kept = fleet
            .kept("transcription-models", "records".into())
            .await
            .map_err(|e| e.0)?;
        let records = match kept {
            Some(bytes) => serde_json::from_slice(&bytes)
                .map_err(|e| format!("invalid transcription inventory: {e}"))?,
            None => Vec::new(),
        };
        Ok(Self(RwLock::new(Arc::new(records))))
    }

    pub(super) async fn recover(&self, fleet: &FleetHandle, root: &Path, active: Option<&str>) {
        for record in self.records().iter() {
            if record["_remove_pending"] == true
                && let Some(id) = record["id"].as_str()
                && Some(id) != active
                && self.remove(fleet, root, id).await.is_err()
            {
                eprintln!("clarp-host: a pending transcription model removal could not finish");
            }
        }
    }

    pub(super) fn records(&self) -> Arc<Vec<Value>> {
        self.0
            .read()
            .unwrap_or_else(PoisonError::into_inner)
            .clone()
    }

    async fn publish(&self, fleet: &FleetHandle, records: Vec<Value>) -> Result<(), ApiError> {
        fleet
            .keep(
                "transcription-models",
                "records".into(),
                Some(serde_json::to_vec(&records).map_err(|e| {
                    ApiError::new(StatusCode::INTERNAL_SERVER_ERROR, e.to_string())
                })?),
            )
            .await?;
        *self.0.write().unwrap_or_else(PoisonError::into_inner) = Arc::new(records);
        Ok(())
    }

    async fn remove(&self, fleet: &FleetHandle, root: &Path, id: &str) -> Result<(), ApiError> {
        let mut records = self.records().as_ref().clone();
        let Some(index) = records.iter().position(|r| r["id"] == id) else {
            return Ok(());
        };
        let record = &records[index];
        let pending = record["_remove_pending"] == true;
        if !pending && installed_path(record).is_none() {
            return Ok(());
        }
        if !record["_managed_root"].is_null() {
            let bad = |why| ApiError::new(StatusCode::BAD_REQUEST, why);
            let target = record["_managed_root"]
                .as_str()
                .ok_or_else(|| bad("invalid managed folder".to_owned()))?;
            let folder =
                super::managed::ManagedFolder::checked(root, id, Path::new(target), pending)
                    .map_err(bad)?;
            if !pending {
                records[index]["_remove_pending"] = json!(true);
                self.publish(fleet, records.clone()).await?;
            }
            if let Some(folder) = folder {
                tokio::task::spawn_blocking(move || folder.remove())
                    .await
                    .map_err(|e| ApiError::new(StatusCode::INTERNAL_SERVER_ERROR, e.to_string()))?
                    .map_err(|e| ApiError::new(StatusCode::INTERNAL_SERVER_ERROR, e.to_string()))?;
            }
        }
        records.remove(index);
        self.publish(fleet, records).await
    }

    pub(super) fn path(&self, model: &str) -> Option<PathBuf> {
        self.records()
            .iter()
            .find(|r| r["id"] == model)
            .and_then(installed_path)
    }

    pub(super) fn models(&self) -> Vec<Value> {
        self.records()
            .iter()
            .filter(|r| installed_path(r).is_some())
            .filter_map(rules::public_record)
            .collect()
    }
}

pub(super) fn installed_path(record: &Value) -> Option<PathBuf> {
    if record["_remove_pending"] == true {
        return None;
    }
    let path = Path::new(record["_local_path"].as_str()?);
    (path.is_absolute() && rules::installed(record, |name| path.join(name).is_file()))
        .then(|| path.to_owned())
}

pub(super) async fn remove_model(
    State(app): State<App>,
    body: axum::body::Bytes,
) -> Result<Json<Value>, ApiError> {
    let data = crate::personas::object(&body)?;
    let value = data.get("model_id").unwrap_or(&Value::Null);
    let id = if !clarp_kernel::python::truthy(value) {
        String::new()
    } else if let Some(text) = value.as_str() {
        text.trim().to_owned()
    } else {
        clarp_kernel::python::repr(value)
    };
    if app.ears.own_id().as_deref().unwrap_or("") == id {
        return Err(ApiError::new(
            StatusCode::BAD_REQUEST,
            "cannot remove the configured server default; choose another default or disable server transcription first",
        ));
    }
    // Complete the registry write even if the client disconnects after submitting the request.
    tokio::spawn(async move {
        let mut selected = app.ears.variants.exclusive().await;
        app.ears
            .inventory
            .remove(&app.fleet, &app.config.data_dir.join("models"), &id)
            .await?;
        if selected.as_ref().is_some_and(|(loaded, _)| *loaded == id) {
            selected.take();
        }
        Ok(Json(json!({"ok": true, "model_id": id})))
    })
    .await
    .map_err(|e| ApiError::new(StatusCode::INTERNAL_SERVER_ERROR, e.to_string()))?
}
