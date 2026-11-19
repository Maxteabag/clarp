//! Selection, loading and use of the one additional local speech worker.

use super::*;
use tokio::sync::{Mutex as AsyncMutex, OwnedMutexGuard};

type Cached = Option<(String, Arc<Whisper>)>;
pub(super) type Selection = OwnedMutexGuard<Cached>;

#[derive(Default)]
pub(super) struct Variants {
    loaded: Arc<AsyncMutex<Cached>>,
    inference: Arc<AsyncMutex<()>>,
}

impl Variants {
    pub(super) async fn exclusive(&self) -> Selection {
        self.loaded.clone().lock_owned().await
    }

    /// Default and alternative workers share the same inference turn.
    pub(super) async fn inference(&self) -> Result<OwnedMutexGuard<()>, Told> {
        tokio::time::timeout(WAITS, self.inference.clone().lock_owned())
            .await
            .map_err(|_| refuse(StatusCode::TOO_MANY_REQUESTS, heard::BUSY))
    }

    /// The selection guard stays with the clip through inference, including if the HTTP
    /// request goes away while a blocking worker operation is still in progress.
    pub(super) async fn select(
        &self,
        default: &Arc<Whisper>,
        id: &str,
        inventory: &inventory::Inventory,
        has_fallback: bool,
    ) -> Result<(Arc<Whisper>, Selection), Told> {
        let pending = self.loaded.clone().lock_owned();
        let mut slot = if has_fallback {
            tokio::time::timeout(WAITS, pending)
                .await
                .map_err(|_| refuse(StatusCode::TOO_MANY_REQUESTS, heard::BUSY))?
        } else {
            pending.await
        };
        let path = inventory
            .path(id)
            .ok_or_else(|| refuse(StatusCode::BAD_REQUEST, heard::not_installed(id)))?;
        if let Some((loaded, engine)) = slot.as_ref()
            && loaded == id
        {
            return Ok((engine.clone(), slot));
        }
        let default = default.clone();
        let selected = id.to_owned();
        tokio::task::spawn_blocking(move || {
            drop(slot.take());
            let model = selected.split_once(':').map_or("", |(_, model)| model);
            let language = languages::own_language(model).map(str::to_owned);
            let worker = default
                .installed_variant(&selected, &path, language)
                .map_err(|error| match error {
                    Deaf::Loading => refuse(
                        StatusCode::SERVICE_UNAVAILABLE,
                        format!("transcription model loading: {selected}"),
                    ),
                    Deaf::Failed(why) => refuse(StatusCode::INTERNAL_SERVER_ERROR, why),
                    Deaf::Busy => refuse(StatusCode::TOO_MANY_REQUESTS, heard::BUSY),
                })?;
            let worker = Arc::new(worker);
            *slot = Some((selected, worker.clone()));
            Ok((worker, slot))
        })
        .await
        .map_err(|_| {
            refuse(
                StatusCode::INTERNAL_SERVER_ERROR,
                "the transcription worker could not be loaded",
            )
        })?
    }
}
