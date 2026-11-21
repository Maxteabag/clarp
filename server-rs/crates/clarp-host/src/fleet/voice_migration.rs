use std::sync::Arc;

use clarp_kernel::{languages, python, voice_migration, voice_profile::HostView};
use serde_json::{Value, json};
use tokio::sync::oneshot;

use super::{AGENTS, Fleet, Member, Owner, PERSONAS, SETTINGS, Unavailable};

impl Fleet {
    pub(super) fn migrate_voices(
        &mut self,
        mut view: HostView,
        reviewed: Option<&str>,
    ) -> Result<Result<Value, String>, Unavailable> {
        let [roster, cartesia, gemini] = self.works.telling.owner_tables();
        let owner = Owner {
            roster: &roster,
            cartesia: &cartesia,
            gemini: &gemini,
        };
        view.agent_engines = languages::stored(
            self.settings
                .get(languages::AGENT_ENGINES_KEY)
                .map(String::as_str),
        );
        view.agent_languages = languages::languages(
            self.settings
                .get(languages::AGENT_LANGUAGES_KEY)
                .map(String::as_str),
        );
        for (key, value) in [
            ("speech.provider", &mut view.voices.provider),
            ("speech.fallback", &mut view.voices.fallback),
        ] {
            if let Some(kept) = self.settings.get(key) {
                value.clone_from(kept);
            }
        }
        let row = |id: &str, persona: &str, session: &str, voice: &str| {
            json!({
                "agent_id":id, "name":persona, "session":session, "voice_id":voice,
            })
        };
        let agents: Vec<_> = self
            .members
            .iter()
            .map(|member| {
                let agent = &member.record;
                row(
                    &agent.agent_id,
                    &agent.persona,
                    &agent.session,
                    &agent.voice_id,
                )
            })
            .chain(self.held.iter().map(|agent| {
                row(
                    &agent.agent_id,
                    &agent.persona,
                    &agent.session,
                    &agent.voice_id,
                )
            }))
            .collect();
        let contacts = self.book.listed(&owner);
        let plan = voice_migration::plan(&view, &agents, &contacts);
        let Some(reviewed) = reviewed else {
            return Ok(Ok(plan));
        };
        if plan["digest"] != reviewed {
            return Ok(Err(format!(
                "nothing written: the plan is {}, the dry run read was {}; run the dry run again and read it",
                plan["digest"].as_str().unwrap_or(""),
                reviewed,
            )));
        }

        let failed = |err: serde_json::Error| Unavailable::new(err.to_string());
        let mut written = Vec::new();
        let mut refused = Vec::new();
        let mut writes = Vec::new();
        let mut changed = Vec::new();
        let mut book = self.book.as_ref().clone();
        for row in plan["voices"].as_array().into_iter().flatten() {
            let label = format!(
                "{} {}",
                row["kind"].as_str().unwrap_or(""),
                row["name"].as_str().unwrap_or("")
            );
            if row["audible"] == true {
                refused.push(label);
                continue;
            }
            let voice = row["new"].as_str().unwrap_or("");
            if row["kind"] == "agent" {
                let id = row["agent_id"].as_str().unwrap_or("");
                let mut record = self
                    .stored_agent(id)?
                    .ok_or_else(|| Unavailable::new("agent missing during migration".into()))?;
                record.voice_id = voice.into();
                writes.push((
                    AGENTS.into(),
                    id.to_owned(),
                    Some(serde_json::to_vec(&record).map_err(failed)?),
                ));
                changed.push(record);
            } else if let Some(contact) =
                book.set_stored_voice(row["persona_id"].as_str().unwrap_or(""), voice)
            {
                writes.push((
                    PERSONAS.into(),
                    contact.persona_id.clone(),
                    Some(serde_json::to_vec(contact).map_err(failed)?),
                ));
            } else {
                continue;
            }
            written.push(label);
        }
        let mut settings = Vec::new();
        for (slot, key) in [
            ("engines", languages::AGENT_ENGINES_KEY),
            ("languages", languages::AGENT_LANGUAGES_KEY),
        ] {
            let Some(additions) = plan["transcription"][slot]
                .as_object()
                .filter(|rows| !rows.is_empty())
            else {
                continue;
            };
            let current = languages::stored(self.settings.get(key).map(String::as_str));
            let mut current: serde_json::Map<_, _> = current
                .into_iter()
                .map(|(key, value)| (key, json!(value)))
                .collect();
            for (id, value) in additions {
                current
                    .entry(id.to_lowercase())
                    .or_insert_with(|| value.clone());
                written.push(format!("{slot} {id}"));
            }
            let current = python::sorted_ascii(&Value::Object(current));
            writes.push((
                SETTINGS.into(),
                key.into(),
                Some(current.as_bytes().to_vec()),
            ));
            settings.push((key.into(), current));
        }
        if !writes.is_empty() {
            self.store
                .put_together(&writes)
                .map_err(|err| Unavailable::new(err.to_string()))?;
            self.settings.extend(settings);
            self.book = Arc::new(book);
            for record in changed {
                if let Some(at) = self.place_of_any(&record.agent_id) {
                    let (noticed, _) = oneshot::channel();
                    let agent = self.members[at].agent.clone();
                    agent.amended(record.clone(), noticed);
                    self.members[at] = Arc::new(Member { agent, record });
                } else if let Some(held) = self
                    .held
                    .iter_mut()
                    .find(|held| held.agent_id == record.agent_id)
                {
                    held.voice_id = record.voice_id;
                }
            }
            self.publish();
            self.works.hub.publish(
                json!({"type":"agent-roster", "kind":"voice-profile"}),
                self.now_ms(),
            );
        }
        Ok(Ok(json!({"written":written,"refused_audible":refused})))
    }
}
