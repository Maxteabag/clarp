//! One explicit Install owns a durable job generation. Only the Host publishes its files.
use super::*;
use crate::fleet::FleetHandle;
use clarp_kernel::heard::inventory::{self as models, CatalogModel};
use clarp_kernel::jobs::{Job, JobCommand, JobStatus, Worker as JobWorker};
use clarp_ports::model_download::{DownloadRequest, DownloadRun, ModelDownloader};
use serde::{Deserialize, Serialize};
use staging::{Folders, staged_bytes};
use std::fs::File;
use std::os::unix::fs::MetadataExt;

const KIND: &str = "transcription-model-install";
const INTENTS: &str = "model-publication";

fn job_id(model: &str) -> String {
    format!(
        "{KIND}-{}",
        &format!("{:x}", Sha256::digest(model.as_bytes()))[..16]
    )
}
fn error(why: impl std::fmt::Display) -> ApiError {
    ApiError::new(StatusCode::INTERNAL_SERVER_ERROR, why.to_string())
}
pub(super) fn job_refused(why: crate::fleet::ComputerJobRefusal) -> ApiError {
    let status = match why {
        crate::fleet::ComputerJobRefusal::Kernel(_) => StatusCode::BAD_REQUEST,
        crate::fleet::ComputerJobRefusal::AgentOwned { .. } => StatusCode::CONFLICT,
    };
    ApiError::new(status, why.to_string())
}
fn installing(model: &str, job: &Job) -> Value {
    json!({"model_id":model,"status":"installing","error":"","job_generation":job.generation,"job_handle":job.handle()})
}

struct Active {
    generation: u64,
    maximum: u64,
    folders: Folders,
    stage: File,
    run: Box<dyn DownloadRun>,
}

#[derive(Serialize, Deserialize)]
struct Intent {
    generation: u64,
    device: u64,
    inode: u64,
}

enum RollbackError {
    Refused(String),
    Store(crate::agent::Unavailable),
    Entry(std::io::Error),
}

impl From<crate::agent::Unavailable> for RollbackError {
    fn from(e: crate::agent::Unavailable) -> Self {
        Self::Store(e)
    }
}

impl From<std::io::Error> for RollbackError {
    fn from(e: std::io::Error) -> Self {
        Self::Entry(e)
    }
}

impl From<RollbackError> for ApiError {
    fn from(e: RollbackError) -> Self {
        match e {
            RollbackError::Refused(why) => ApiError::new(StatusCode::BAD_REQUEST, why),
            RollbackError::Store(e) => e.into(),
            RollbackError::Entry(e) => error(e),
        }
    }
}

pub(crate) struct Desk {
    root: PathBuf,
    downloader: Box<dyn ModelDownloader>,
    running: tokio::sync::Mutex<HashMap<String, Active>>,
}

impl Desk {
    pub(super) fn new(root: PathBuf, downloader: Box<dyn ModelDownloader>) -> Self {
        Self {
            root,
            downloader,
            running: Default::default(),
        }
    }

    /// An interrupted transfer cannot launch itself. Move a renamed but uncommitted
    /// inode back into staging before ever allowing a new generation to adopt it.
    pub(super) async fn recover(
        &self,
        fleet: &FleetHandle,
        inventory: &super::inventory::Inventory,
    ) -> Result<(), ApiError> {
        for model in models::CATALOG.iter().filter(|m| m.source.is_some()) {
            let id = model.id();
            let job = fleet.computer_job(job_id(&id)).await?.map(|r| r.job);
            let rollback = match self.rollback(fleet, &id, job.as_ref()).await {
                Ok(()) => true,
                Err(RollbackError::Entry(_) | RollbackError::Refused(_)) => false,
                Err(RollbackError::Store(e)) => return Err(e.into()),
            };
            if let Some(job) = job.as_ref().filter(|j| j.status.is_active()) {
                let reason = if job.status == JobStatus::Queued {
                    "worker_never_started"
                } else {
                    "worker_vanished"
                };
                finish_failed(fleet, &id, job.generation, reason).await?;
            }
            if !rollback {
                eprintln!("clarp-host: a pending model publication could not be reconciled");
                continue;
            }
            if let Some(cancelled) = job.as_ref().filter(|j| j.status == JobStatus::Cancelled) {
                if self
                    .cancelled_files(fleet, inventory, &id, cancelled)
                    .await
                    .is_err()
                {
                    eprintln!("clarp-host: pending model cancellation could not finish");
                }
                continue;
            }
            if let Err(e) = self.prune_stage(&id, job.as_ref().map(|j| j.status)) {
                eprintln!("clarp-host: could not safely prune model staging: {e}");
            }
        }
        Ok(())
    }

    fn prune_stage(&self, id: &str, status: Option<JobStatus>) -> std::io::Result<()> {
        if status.is_some_and(|s| s != JobStatus::Cancelled) {
            return Ok(());
        }
        let folders = match Folders::open(&self.root, id, false) {
            Ok(f) => f,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(()),
            Err(e) => return Err(e),
        };
        let stage = match folders.staging(false) {
            Ok(f) => f,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(()),
            Err(e) => return Err(e),
        };
        let age = SystemTime::now()
            .duration_since(stage.metadata()?.modified()?)
            .unwrap_or_default();
        if models::discard_staging(status, age) {
            folders.remove_stage()?;
        }
        Ok(())
    }

    /// Idle staging with no durable job has no owner. Active, failed and interrupted
    /// job staging remains available for a later explicit Install.
    pub(super) async fn sweep_orphans(&self, fleet: &FleetHandle) -> Result<(), ApiError> {
        let _install = self.running.lock().await;
        for model in models::CATALOG.iter().filter(|m| m.source.is_some()) {
            let id = model.id();
            let status = fleet.computer_job(job_id(&id)).await?.map(|r| r.job.status);
            if status.is_none() {
                self.prune_stage(&id, status).map_err(error)?;
            }
        }
        Ok(())
    }

    async fn rollback(
        &self,
        fleet: &FleetHandle,
        id: &str,
        job: Option<&Job>,
    ) -> Result<(), RollbackError> {
        let Some(bytes) = fleet.kept(INTENTS, id.into()).await? else {
            return Ok(());
        };
        let intent: Intent = serde_json::from_slice(&bytes).map_err(std::io::Error::other)?;
        let Some(job) = job.filter(|j| j.kind == KIND) else {
            return Err(std::io::Error::other("model publication has no owning job").into());
        };
        if job.generation > intent.generation
            || (job.generation == intent.generation && job.status == JobStatus::Succeeded)
        {
            // The record is obsolete. Neither path may be moved on its authority.
            fleet.keep(INTENTS, id.into(), None).await?;
            return Ok(());
        }
        if job.generation != intent.generation {
            return Err(
                std::io::Error::other("model publication belongs to another generation").into(),
            );
        }
        super::managed::ManagedFolder::checked(&self.root, id).map_err(RollbackError::Refused)?;
        Folders::open(&self.root, id, false)?.undo_publish(intent.device, intent.inode)?;
        fleet.keep(INTENTS, id.into(), None).await?;
        Ok(())
    }

    async fn launch(&self, app: App, id: String, model: &CatalogModel) -> Result<Value, ApiError> {
        let mut running = self.running.lock().await;
        let fleet = &app.fleet;
        let key = job_id(&id);
        let prior = fleet.computer_job(key.clone()).await?.map(|r| r.job);
        if let Some(job) = &prior
            && job.status.is_active()
        {
            return Ok(installing(&id, job));
        }
        self.rollback(fleet, &id, prior.as_ref()).await?;
        if app.ears.inventory.path(&id).is_some() {
            return Ok(json!({"model_id":id,"status":"installed","error":""}));
        }
        let folders = Folders::open(&self.root, &id, true).map_err(error)?;
        let stage = folders.staging(true).map_err(error)?;
        let maximum = model.maximum_bytes();
        let used = staged_bytes(&stage).map_err(error)?;
        let remaining = maximum
            .checked_sub(used)
            .ok_or_else(|| error("model exceeds catalog byte limit"))?;
        let reserved = running
            .values()
            .try_fold(0u64, |total, r| {
                staged_bytes(&r.stage)
                    .map(|used| total.saturating_add(r.maximum.saturating_sub(used)))
            })
            .map_err(error)?;
        if !models::space_available(folders.free_bytes().map_err(error)?, reserved, remaining) {
            return Err(ApiError::new(
                StatusCode::INSUFFICIENT_STORAGE,
                "insufficient disk space for transcription model",
            ));
        }
        let source = model
            .source
            .as_ref()
            .ok_or_else(|| error("model source unavailable"))?;
        let request = DownloadRequest {
            repository: source.repository,
            revision: source.revision,
            staging: stage.try_clone().map_err(error)?,
            maximum_bytes: maximum,
        };
        let job = fleet
            .change_computer_job(JobCommand::Register {
                id: key.clone(),
                kind: KIND.into(),
                title: format!("Install transcription model: {id}"),
                queued: true,
                detail: "Preparing model download".into(),
                worker: JobWorker::none(),
                restart_cancelled: true,
                started_trace_id: String::new(),
                heartbeat_timeout_ms: Some(120000),
                metadata: json!({"model_id":id,"expire_queued":true}),
            })
            .await?
            .map_err(job_refused)?
            .job
            .ok_or_else(|| error("model installation was not registered"))?;
        let generation = job.generation;
        let started = fleet
            .change_computer_job(JobCommand::Start {
                id: key,
                generation: Some(generation),
                worker: JobWorker::none(),
            })
            .await?
            .map_err(job_refused)?;
        if !started
            .job
            .is_some_and(|j| j.status == JobStatus::Running && j.generation == generation)
        {
            return Err(error("model installation no longer active"));
        }
        let run = match self.downloader.start(request) {
            Ok(run) => run,
            Err(_) => {
                finish_failed(fleet, &id, generation, "install_launch_failed").await?;
                return Ok(
                    json!({"model_id":id,"status":"failed","error":"Could not start model installer","job_generation":generation}),
                );
            }
        };
        running.insert(
            id.clone(),
            Active {
                generation,
                maximum,
                folders,
                stage,
                run,
            },
        );
        let mut answer = installing(&id, &job);
        answer["provisional_id"] = json!(ids::hex(16));
        tokio::spawn(monitor(app, id, generation));
        Ok(answer)
    }

    async fn cancelled_files(
        &self,
        fleet: &FleetHandle,
        inventory: &super::inventory::Inventory,
        id: &str,
        job: &Job,
    ) -> Result<(), ApiError> {
        self.check_stage_for_removal(id)?;
        // Inventory and the job's success publish in one transaction. Without an
        // inventory record only the directory this id gives may be cleaned up.
        if let Some(record) = inventory.records().iter().find(|r| r["id"] == id) {
            if record["_install_generation"]
                .as_u64()
                .is_some_and(|gen_id| gen_id != job.generation)
            {
                return Ok(());
            }
            inventory.remove(fleet, &self.root, id, true).await?;
        } else if let Some(folder) = super::managed::ManagedFolder::checked(&self.root, id)
            .map_err(|why| ApiError::new(StatusCode::BAD_REQUEST, why))?
        {
            tokio::task::spawn_blocking(move || folder.remove())
                .await
                .map_err(error)?
                .map_err(error)?;
        }
        self.clean_stage(id)
    }

    /// Serialize a cancellation with publication. A stale receipt cannot stop another run.
    pub(crate) async fn cancel(
        &self,
        app: &App,
        command: JobCommand,
    ) -> Result<clarp_kernel::jobs::JobChange, ApiError> {
        let fleet = &app.fleet;
        let mut running = self.running.lock().await;
        let change = fleet
            .change_computer_job(command)
            .await?
            .map_err(job_refused)?;
        if !change.generation_mismatch
            && let Some(job) = &change.job
            && job.kind == KIND
            && job.status == JobStatus::Cancelled
            && let Some(id) = job.metadata["model_id"].as_str()
            && job_id(id) == job.id
        {
            if running
                .get(id)
                .is_some_and(|r| r.generation == job.generation)
                && let Some(mut r) = running.remove(id)
            {
                r.run.cancel().map_err(error)?;
            }
            self.rollback(fleet, id, Some(job)).await?;
            let _selected = app.ears.variants.exclusive().await;
            self.cancelled_files(fleet, &app.ears.inventory, id, job)
                .await?;
        }
        Ok(change)
    }

    pub(super) async fn remove_model(&self, app: &App, id: &str) -> Result<(), ApiError> {
        let _install = self.running.lock().await;
        if let Some(row) = app.fleet.computer_job(job_id(id)).await?
            && row.job.status.is_active()
        {
            return Err(ApiError::new(
                StatusCode::BAD_REQUEST,
                format!("model installation is still in progress: {id}"),
            ));
        }
        if app.ears.own_id().as_deref().unwrap_or("") == id {
            return Err(ApiError::new(
                StatusCode::BAD_REQUEST,
                "cannot remove the configured server default; choose another default or disable server transcription first",
            ));
        }
        self.check_stage_for_removal(id)?;
        let prior = app.fleet.computer_job(job_id(id)).await?;
        self.rollback(&app.fleet, id, prior.as_ref().map(|r| &r.job))
            .await?;
        let mut selected = app.ears.variants.exclusive().await;
        app.ears
            .inventory
            .remove(&app.fleet, &self.root, id, false)
            .await?;
        self.clean_stage(id)?;
        if selected.as_ref().is_some_and(|(loaded, _)| loaded == id) {
            selected.take();
        }
        Ok(())
    }

    fn check_stage_for_removal(&self, id: &str) -> Result<(), ApiError> {
        if models::install_model(id).is_err() {
            return Ok(());
        }
        match Folders::open(&self.root, id, false).and_then(|folders| folders.staging(false)) {
            Ok(_) => Ok(()),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(e)
                if e.raw_os_error()
                    .is_some_and(|n| n == nix::libc::ELOOP || n == nix::libc::ENOTDIR) =>
            {
                Err(ApiError::new(
                    StatusCode::BAD_REQUEST,
                    super::managed::refused(id, "link"),
                ))
            }
            Err(e) => Err(error(e)),
        }
    }

    pub(super) fn clean_stage(&self, id: &str) -> Result<(), ApiError> {
        if models::install_model(id).is_err() {
            return Ok(());
        }
        match Folders::open(&self.root, id, false) {
            Ok(f) => f.remove_stage().map_err(error),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(e) => Err(error(e)),
        }
    }
}

async fn finish_failed(
    fleet: &FleetHandle,
    id: &str,
    generation: u64,
    reason: &str,
) -> Result<(), ApiError> {
    fleet
        .change_computer_job(JobCommand::Finish {
            id: job_id(id),
            generation,
            worker: JobWorker::none(),
            failed: Some(reason.into()),
        })
        .await?
        .map_err(job_refused)?;
    Ok(())
}

async fn monitor(app: App, id: String, generation: u64) {
    let key = job_id(&id);
    let ended = app.fleet.wait_computer_job_end(key.clone(), generation);
    tokio::pin!(ended);
    let mut tick = tokio::time::interval(Duration::from_millis(500));
    let mut last_beat = tokio::time::Instant::now();
    loop {
        let stop = tokio::select! { _=&mut ended=>true, _=tick.tick()=>false };
        let desk = &app.ears.installs;
        let mut running = desk.running.lock().await;
        let Some(r) = running.get_mut(&id) else {
            return;
        };
        if r.generation != generation {
            return;
        }
        if stop {
            running.remove(&id);
            if let Ok(Some(row)) = app.fleet.computer_job(key.clone()).await
                && row.job.generation == generation
                && row.job.status == JobStatus::Cancelled
                && desk.clean_stage(&id).is_err()
            {
                eprintln!("clarp-host: could not remove cancelled model staging");
            }
            return;
        }
        let result = r.run.try_complete();
        if matches!(result, Ok(None)) {
            if last_beat.elapsed() >= Duration::from_secs(30) {
                let _ = app
                    .fleet
                    .change_computer_job(JobCommand::Heartbeat {
                        id: key.clone(),
                        generation: Some(generation),
                        worker: JobWorker::none(),
                        source: "worker".into(),
                    })
                    .await;
                last_beat = tokio::time::Instant::now();
            }
            continue;
        }
        if let Some(mut r) = running.remove(&id) {
            let failure = match result {
                Err(e) => Some(e.to_string()),
                Ok(_) => publish(&app, &id, &mut r)
                    .await
                    .err()
                    .map(|_| "model publication failed".to_owned()),
            };
            if let Some(reason) = failure {
                let current = app.fleet.computer_job(key.clone()).await.ok().flatten();
                let _ = desk
                    .rollback(&app.fleet, &id, current.as_ref().map(|r| &r.job))
                    .await;
                if finish_failed(&app.fleet, &id, generation, &reason)
                    .await
                    .is_err()
                {
                    eprintln!("clarp-host: could not record model installation failure");
                }
            }
        }
        return;
    }
}

async fn publish(app: &App, id: &str, active: &mut Active) -> Result<(), ApiError> {
    let _selection = app.ears.variants.exclusive().await;
    let stat = active.stage.metadata().map_err(error)?;
    let intent = Intent {
        generation: active.generation,
        device: stat.dev(),
        inode: stat.ino(),
    };
    let fleet = &app.fleet;
    fleet
        .keep(
            INTENTS,
            id.into(),
            Some(serde_json::to_vec(&intent).map_err(error)?),
        )
        .await?;
    active
        .folders
        .publish(&active.stage, active.maximum)
        .map_err(error)?;
    if !app
        .ears
        .inventory
        .register_managed(
            fleet,
            id,
            &active.folders.path(),
            active.generation,
            job_id(id),
        )
        .await?
    {
        return Err(error("model installation no longer active"));
    }
    Ok(())
}

pub(super) async fn install_model(
    State(app): State<App>,
    body: axum::body::Bytes,
) -> Result<(StatusCode, Json<Value>), ApiError> {
    let value = crate::personas::object(&body)?
        .remove("model_id")
        .unwrap_or(Value::Null);
    let id = if let Some(s) = value.as_str() {
        s.trim().to_owned()
    } else if clarp_kernel::python::truthy(&value) {
        clarp_kernel::python::repr(&value)
    } else {
        String::new()
    };
    let model =
        models::install_model(&id).map_err(|why| ApiError::new(StatusCode::BAD_REQUEST, why))?;
    let ears = app.ears.clone();
    let result = tokio::spawn(async move { ears.installs.launch(app, id, model).await })
        .await
        .map_err(error)??;
    Ok((StatusCode::ACCEPTED, Json(result)))
}

pub(super) async fn catalog(
    fleet: &FleetHandle,
    mut rows: Vec<Value>,
) -> Result<Vec<Value>, ApiError> {
    let jobs: HashMap<_, _> = fleet
        .computer_jobs()
        .await?
        .into_iter()
        .filter(|j| j.kind == KIND)
        .map(|job| (job.id.clone(), job))
        .collect();
    for row in &mut rows {
        let Some(job) = row["id"].as_str().and_then(|id| jobs.get(&job_id(id))) else {
            continue;
        };
        match job.status {
            JobStatus::Queued | JobStatus::Running => row["status"] = json!("installing"),
            JobStatus::Cancelled | JobStatus::Failed => {
                row["status"] = json!("failed");
                let why = if job.status == JobStatus::Cancelled {
                    "Model installation cancelled"
                } else if job.terminal_reason.is_empty() {
                    "Model installation failed"
                } else {
                    &job.terminal_reason
                };
                row["error"] = json!(why);
            }
            JobStatus::Succeeded => (),
        }
    }
    Ok(rows)
}
