//! Fleet operations on a generation of portrait edits. A cancel and the acceptance
//! of two finished images are ordered on the same writer as the computer job.
use super::*;
use clarp_kernel::jobs::{Job, JobCommand, JobStatus, Worker};
use clarp_kernel::kept_portraits as kept;
use clarp_kernel::media::Asset;
use clarp_kernel::portrait_generation as rules;
use serde::{Deserialize, Serialize};

pub(crate) const PENDING: &str = "portrait-publications";
pub(crate) const BYTES: &str = "portrait-publication-bytes";

#[derive(Clone, Serialize, Deserialize)]
pub(crate) struct Publication {
    pub agent_id: String,
    pub assets: [Asset; 2],
}

pub(crate) enum Admission {
    Existing(Job),
    Begun(Job),
    Refused(&'static str),
}

impl Fleet {
    pub(super) fn begin_portraits(&mut self, agent_id: &str) -> Result<Admission, Unavailable> {
        let Some(at) = self.place_of(agent_id) else {
            return Ok(Admission::Refused("unknown Agent session"));
        };
        let member = &self.members[at].record;
        let id = rules::job_id(&member.agent_id);
        if let Some(known) = self
            .computer_jobs
            .get(&id)
            .filter(|j| j.job.status.is_active())
        {
            return Ok(Admission::Existing(known.job));
        }
        let count = self.portrait_count();
        if clarp_kernel::contacts::picture::admitted(None, count).is_none() {
            return Ok(Admission::Refused(rules::DAILY_REASON));
        }
        let command = JobCommand::Register {
            id,
            queued: true,
            kind: rules::KIND.into(),
            title: format!("Generate portraits for {}", member.persona),
            detail: "Generating two character-consistent portrait alternatives".into(),
            worker: Worker::none(),
            restart_cancelled: true,
            started_trace_id: String::new(),
            heartbeat_timeout_ms: Some(300_000),
            metadata: serde_json::json!({"session":member.session,"provider":"openai","model":clarp_kernel::contacts::picture::MODEL,"expire_queued":true}),
        };
        let change = self
            .change_computer_job(command, vec![])?
            .map_err(|why| Unavailable(why.to_string()))?;
        Ok(Admission::Begun(change.job.ok_or_else(|| {
            Unavailable("missing portrait generation".into())
        })?))
    }

    pub(super) fn portrait_count(&self) -> u32 {
        self.computer_jobs.begun_since(
            rules::KIND,
            self.now_ms() - clarp_kernel::contacts::picture::DAILY_WINDOW_MS,
        )
    }

    /// Nothing has been staged on disk before this point. Save the gallery, job outcome,
    /// and the two complete assets in one decision. The short-lived delivery record lets a
    /// crash between that decision and the agent's media journal finish without another edit.
    pub(super) fn accept_portraits(
        &mut self,
        id: &str,
        generation: u64,
        publication: &Publication,
        bytes: [Vec<u8>; 2],
    ) -> Result<bool, Unavailable> {
        if !self.computer_jobs.get(id).is_some_and(|row| {
            row.job.status == JobStatus::Running && row.job.generation == generation
        }) {
            return Ok(false);
        }
        let Some(at) = self.place_of(&publication.agent_id) else {
            return Err(Unavailable("unknown Agent session".into()));
        };
        let mut record = self.members[at].record.clone();
        let digest = record
            .avatar
            .as_deref()
            .ok_or_else(|| Unavailable("primary portrait unavailable".into()))?;
        let primary_name = picture_name(digest);
        let head = self
            .works
            .blobs
            .read(&primary_name, 0, 12)
            .map_err(|e| Unavailable(e.to_string()))?;
        let kind = kept::picture_kind(&head)
            .ok_or_else(|| Unavailable("primary portrait content unavailable".into()))?;
        record
            .portraits
            .retain(|portrait| portrait.sha256 == digest);
        kept::before_a_change(
            &mut record.portraits,
            (&record.agent_id, record.created_at_ms),
            Some((digest, kind)),
            |_| true,
        )
        .map_err(|e| Unavailable(e.why.into()))?;
        for asset in &publication.assets {
            if asset.sha256 == digest {
                return Err(Unavailable(
                    "generated portrait duplicates the primary".into(),
                ));
            }
            record.portraits.push(Portrait {
                portrait_id: format!("portrait_{}", crate::ids::secret(18).replace('-', "_")),
                media_asset_id: Some(asset.asset_id.clone()),
                sha256: asset.sha256.clone(),
                mime_type: asset.mime_type.clone(),
                created_by: asset.created_by.clone(),
                created_at: asset.created_at,
            });
        }
        let encode_err = |e: serde_json::Error| Unavailable(e.to_string());
        let mut writes = vec![
            (
                AGENTS.into(),
                record.agent_id.clone(),
                Some(serde_json::to_vec(&record).map_err(encode_err)?),
            ),
            (
                shelf(PENDING),
                publication.assets[0].asset_id.clone(),
                Some(serde_json::to_vec(publication).map_err(encode_err)?),
            ),
        ];
        for (asset, raw) in publication.assets.iter().zip(&bytes) {
            writes.push((shelf(BYTES), asset.asset_id.clone(), Some(raw.clone())));
        }
        let finish = JobCommand::Finish {
            id: id.into(),
            generation,
            worker: Worker::none(),
            failed: None,
        };
        let change = self
            .change_computer_job(finish, writes)?
            .map_err(|e| Unavailable(e.to_string()))?;
        if change.record.is_none() {
            return Ok(false);
        }
        // Only a durably accepted pair writes content-addressed blobs. Make both readable
        // before publishing the new directory: readers racing the write see the old gallery.
        // The durable publication carries the original bytes if a disk write is interrupted.
        for (asset, raw) in publication.assets.iter().zip(bytes) {
            if let Err(error) = self
                .works
                .blobs
                .put(&asset.blob(), &raw)
                .and_then(|()| self.works.blobs.put(&picture_name(&asset.sha256), &raw))
            {
                self.works.log.line(&format!(
                    "clarp-host: committed portrait awaits local publication: {error}"
                ));
                break;
            }
        }
        let (sent, _) = oneshot::channel();
        self.replaced(at, record, sent);
        Ok(true)
    }
}
