//! The Host's installed models, registered through its own storage port.
use super::*;
use crate::fleet::FleetHandle;
use clarp_kernel::heard::inventory as rules;
use std::path::Path;
use std::sync::RwLock;

/// What this desk keeps, declared once (`kept.rs`: the registry walks it at every start).
pub(crate) const KEPT: &[crate::kept::Kind] = &[crate::kept::Kind::book(
    "transcription-models",
    crate::kept::Key::Plain,
    crate::kept::fits::<Vec<Value>>,
)];

pub(super) struct Inventory(RwLock<Option<Arc<Vec<Value>>>>);

impl Inventory {
    pub(super) async fn open(fleet: &crate::fleet::FleetHandle) -> Result<Self, String> {
        // Through the loader: a list that cannot be read is moved aside, counted and said
        // (`kept.rs`), and a store that refuses that is the answer.
        let found = fleet
            .judged("transcription-models", "records".into())
            .await
            .map_err(|e| e.0)?;
        let records = match found {
            crate::kept::Found::Read(bytes) => serde_json::from_slice::<Vec<Value>>(&bytes)
                .ok()
                .map(Arc::new),
            // Moved aside: unreadable, as before; its next change writes the list anew.
            crate::kept::Found::Unfit => None,
            crate::kept::Found::Absent => Some(Arc::default()),
        };
        Ok(Self(RwLock::new(records)))
    }

    pub(super) async fn recover(
        &self,
        fleet: &FleetHandle,
        root: &Path,
        active: Option<&str>,
    ) -> Result<(), String> {
        for record in self.records().iter() {
            if record["_remove_pending"] != true {
                continue;
            }
            let Some(id) = record["id"].as_str() else {
                continue;
            };
            if Some(id) == active {
                let mut rows = self.records().as_ref().clone();
                for row in &mut rows {
                    if row["id"] == id
                        && let Some(fields) = row.as_object_mut()
                    {
                        fields.remove("_remove_pending");
                    }
                }
                self.publish(fleet, rows)
                    .await
                    .map_err(|e| format!("clearing pending default model removal: {e:?}"))?;
            } else if self.remove(fleet, root, id).await.is_err() {
                eprintln!("clarp-host: a pending transcription model removal could not finish");
            }
        }
        Ok(())
    }

    pub(super) fn records(&self) -> Arc<Vec<Value>> {
        self.0
            .read()
            .unwrap_or_else(PoisonError::into_inner)
            .clone()
            .unwrap_or_default()
    }

    pub(super) async fn publish(
        &self,
        fleet: &FleetHandle,
        records: Vec<Value>,
    ) -> Result<(), ApiError> {
        fleet
            .keep(
                "transcription-models",
                "records".into(),
                Some(serde_json::to_vec(&records).map_err(|e| {
                    ApiError::new(StatusCode::INTERNAL_SERVER_ERROR, e.to_string())
                })?),
            )
            .await?;
        *self.0.write().unwrap_or_else(PoisonError::into_inner) = Some(Arc::new(records));
        Ok(())
    }

    pub(super) async fn register_managed(
        &self,
        fleet: &FleetHandle,
        id: &str,
        path: &Path,
        generation: u64,
        job_id: String,
    ) -> Result<bool, ApiError> {
        let model =
            rules::install_model(id).map_err(|e| ApiError::new(StatusCode::BAD_REQUEST, e))?;
        let mut row = model.installed_record();
        row["_local_path"] = json!(path);
        row["_managed_root"] = json!(path);
        row["_install_generation"] = json!(generation);
        if installed_path(&row).is_none() {
            return Err(ApiError::new(
                StatusCode::INTERNAL_SERVER_ERROR,
                "published transcription model is incomplete",
            ));
        }
        let mut rows = self.records().as_ref().clone();
        rows.retain(|r| r["id"] != id);
        rows.push(row);
        let bytes = serde_json::to_vec(&rows)
            .map_err(|e| ApiError::new(StatusCode::INTERNAL_SERVER_ERROR, e.to_string()))?;
        let change = fleet
            .finish_computer_job_with(
                job_id,
                generation,
                vec![
                    ("transcription-models", "records".into(), Some(bytes)),
                    ("model-publication", id.into(), None),
                ],
            )
            .await?
            .map_err(super::install::job_refused)?;
        if change.record.is_none() {
            return Ok(false);
        }
        *self.0.write().unwrap_or_else(PoisonError::into_inner) = Some(Arc::new(rows));
        Ok(true)
    }

    pub(super) async fn remove(
        &self,
        fleet: &FleetHandle,
        root: &Path,
        id: &str,
    ) -> Result<(), ApiError> {
        let mut records = self.records().as_ref().clone();
        let Some(index) = rules::removal_index(&records, id) else {
            let unreadable = self
                .0
                .read()
                .unwrap_or_else(PoisonError::into_inner)
                .is_none();
            if unreadable {
                self.publish(fleet, records).await?;
            }
            return Ok(());
        };
        let record = &records[index];
        let pending = record["_remove_pending"] == true;
        if let Some(stored) = record.get("_managed_root") {
            let folder = super::managed::ManagedFolder::checked(root, id)
                .map_err(|why| ApiError::new(StatusCode::BAD_REQUEST, why))?;
            super::managed::ManagedFolder::note_stored(root, id, stored);
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
    tokio::spawn(async move {
        app.ears.installs.remove_model(&app, &id).await?;
        Ok(Json(json!({"ok": true, "model_id": id})))
    })
    .await
    .map_err(|e| ApiError::new(StatusCode::INTERNAL_SERVER_ERROR, e.to_string()))?
}
