//! What an agent sounds like and how it is heard, as an app is shown it (feature
//! `voice_profile`, the Python Host's PR 485).
//!
//! The profile is made from the same resolver the speaker calls (`voice::chosen`, through
//! `clarp_kernel::voice_profile`) and the same plan of engines a recording is heard with, so
//! that what an app shows is what the Host does. A snapshot makes one view of the Host and
//! resolves every agent from it, counting who shares a voice in one pass; nothing is fetched
//! for it. The owner's file is read when a view is made: a snapshot asked after the file
//! changed shows the new profile and a new `voice_profile_version` at once.

use std::sync::{Arc, OnceLock, Weak};

use clarp_kernel::languages;
use clarp_kernel::voice_profile::{self, HostView, Row};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

use crate::fleet::{Directory, Member};
use crate::heard::Ears;
use crate::voice::{Mouth, speaker};

/// The roster's events about one agent that carry its profile.
pub const CARRIED_BY: [&str; 6] = [
    "created",
    "relaunched",
    "forked",
    "contact-assigned",
    "agent-renamed",
    "voice-changed",
];

/// The view of the Host every agent's profile is resolved from, as it is now: the speaker's
/// view, the names of voices in memory, and how the Host hears.
pub fn view(mouth: &Mouth, ears: &Ears, directory: &Directory, owners: &toml::Table) -> HostView {
    let mut view = mouth.speech_view(owners);
    let mut labels = voice_profile::shipped_labels();
    let (saved, signature) = mouth.cached_voice_names();
    clarp_kernel::voice_names::merge(&mut labels, saved);
    labels
        .entry(clarp_kernel::said::CARTESIA.into())
        .or_default()
        .extend(mouth.cartesia_names());
    labels.insert(
        clarp_kernel::said::GEMINI.into(),
        clarp_kernel::voices::gemini_labels(&view.voices),
    );
    view.labels = labels;
    view.names_version = signature;
    let (cloud, own, language) = ears.as_profiled(directory);
    view.cloud_engine = cloud;
    view.own_engine = own;
    view.language = language;
    view.agent_engines = languages::stored(directory.setting(languages::AGENT_ENGINES_KEY));
    view.agent_languages = languages::languages(directory.setting(languages::AGENT_LANGUAGES_KEY));
    view
}

/// A version of everything the Host-wide part of a profile is made from: the owner's
/// settings of speech, the keys, the names of voices, how the Host hears. Another version:
/// re-read the agents. The same inputs, the same version.
pub fn version(view: &HostView) -> String {
    let digest = Sha256::digest(format!("{view:?}").as_bytes());
    digest
        .iter()
        .take(8)
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

/// A member of the fleet as the sharing of voices counts it: archived or a Janitor, it shares
/// nobody's voice.
fn row(member: &Member) -> Row<'_> {
    Row {
        agent: speaker(&member.record),
        inactive: member.record.archived_at_ms.is_some() || member.record.is_janitor,
    }
}

/// Every member's profile, by its session: the agents and the Janitors, in one pass.
pub fn all(view: &HostView, directory: &Directory) -> Vec<(String, Value)> {
    let members: Vec<&Arc<Member>> = directory
        .members
        .iter()
        .chain(&directory.janitors)
        .collect();
    let rows: Vec<Row<'_>> = members.iter().map(|member| row(member)).collect();
    let made = voice_profile::profiles(view, &rows);
    members
        .iter()
        .map(|member| member.record.session.clone())
        .zip(made)
        .collect()
}

/// What the head of a snapshot says of voices: what a new agent gets, and the version.
pub fn head(view: &HostView) -> (Value, String) {
    (voice_profile::defaults(view), version(view))
}

/// One agent's profile outside a snapshot, by its session, its sharing counted over the
/// Host's agents. `None`: no such agent.
pub fn of_session(
    mouth: &Mouth,
    ears: &Ears,
    directory: &Directory,
    session: &str,
) -> Option<Value> {
    let members: Vec<&Arc<Member>> = directory
        .members
        .iter()
        .chain(&directory.janitors)
        .collect();
    let member = members.iter().find(|member| {
        member.record.session == session && member.record.archived_at_ms.is_none()
    })?;
    let rows: Vec<Row<'_>> = members.iter().map(|member| row(member)).collect();
    let view = view(mouth, ears, directory, &mouth.owners_from(directory));
    Some(voice_profile::profile_among(
        &view,
        row(member).agent,
        &rows,
    ))
}

/// What adds an agent's profile to the roster's events about it, wherever they are told from.
pub struct Profiles {
    // The mouth already owns the hub. Neither component is kept alive by the subscription.
    mouth: Weak<Mouth>,
    ears: Weak<Ears>,
}

impl Profiles {
    pub fn new(mouth: &Arc<Mouth>, ears: &Arc<Ears>) -> Self {
        Self {
            mouth: Arc::downgrade(mouth),
            ears: Arc::downgrade(ears),
        }
    }

    /// The event with the agent's profile, when it is an event of the roster about one agent
    /// that carries it and the agent is known. An event is never lost to it.
    pub fn add_to(&self, event: &mut Value) {
        let kind = event["kind"].as_str().unwrap_or("");
        if event["type"] != "agent-roster"
            || !CARRIED_BY.contains(&kind)
            || event.get("voice_profile").is_some()
        {
            return;
        }
        let (Some(session), Some(mouth), Some(ears)) = (
            event["session"].as_str(),
            self.mouth.upgrade(),
            self.ears.upgrade(),
        ) else {
            return;
        };
        if let Some(profile) = of_session(&mouth, &ears, &mouth.directory(), session) {
            event["voice_profile"] = profile;
        }
    }
}

/// Where the Host's [`Profiles`] are found once it has them.
pub type Slot = OnceLock<Profiles>;

/// An event of the roster that tells open apps every agent's voice or hearing may have
/// changed: re-read the agents.
pub fn changed_for_all() -> Value {
    json!({ "type": "agent-roster", "kind": "voice-profile" })
}
