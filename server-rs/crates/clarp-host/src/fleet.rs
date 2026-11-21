//! The fleet: which agents this Host has.
//!
//! One thread owns the list. It is the only writer of the fleet's tables, and it publishes the
//! list as a whole whenever it changes, so that everyone else reads a list that is complete and
//! never waits for the fleet to find an agent.

pub(crate) mod delegations;

use clarp_kernel::kept_portraits::Portrait;
use clarp_kernel::launch_directories::{self as launches, Ledger, Owners};
const LAUNCH_USAGE: &str = "launch-path-usage";
use std::collections::{BTreeMap, HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::mpsc::{self, RecvTimeoutError};
use std::time::{Duration, Instant};

use clarp_actor::Profile;
use clarp_kernel::devices::{self, PushBook, PushRow};
use clarp_kernel::personas::{self, Book, Kept, Look, Made, Owner, PERSONALITY_MOST, Wanted};
use clarp_kernel::voice_names::Snapshot as VoiceCatalogue;
use clarp_kernel::{helpers, languages, python, roster};
use clarp_ports::blobs::Blobs;
use clarp_ports::clock::Clock;
use clarp_ports::codec::CodecSet;
use clarp_ports::kv::KeyValue;
use clarp_ports::log::LogEngine;
use clarp_protocol::requests::CreateAgentRequest;
use clarp_speech::listen::strip;
use serde::{Deserialize, Serialize};
use tokio::sync::{oneshot, watch};

use crate::agent::{self, AgentHandle, Answer, Setup, Unavailable};
use crate::config::Config;
use crate::hub::Hub;
use crate::ids;
use crate::launcher::Launcher;
use crate::pairing::{self, Device, Issued, Paired, PendingCode, Scope};
use crate::pusher::Pusher;
use crate::revisions::Revisions;
use crate::wire;
pub(crate) mod computer_jobs;
pub(crate) mod portrait_jobs;
mod voice_migration;
use clarp_kernel::jobs::{Job, JobChange, JobCommand};
pub use computer_jobs::{ComputerJob, ComputerJobRefusal, JobOwner};

/// What this desk keeps, declared once (`kept.rs`: the registry walks it at every start).
pub(crate) const KEPT: &[crate::kept::Kind] = &[
    crate::kept::Kind::table(
        AGENTS,
        crate::kept::Policy::Rows,
        crate::kept::Key::Plain,
        crate::kept::fits::<AgentRecord>,
    ),
    crate::kept::Kind::table(
        DEVICES,
        crate::kept::Policy::Rows,
        crate::kept::Key::Plain,
        crate::kept::fits::<Device>,
    ),
    crate::kept::Kind::table(
        PUSH,
        crate::kept::Policy::Rows,
        crate::kept::Key::Secret,
        crate::kept::fits::<PushRow>,
    ),
    crate::kept::Kind::table(
        PERSONAS,
        crate::kept::Policy::Rows,
        crate::kept::Key::Plain,
        crate::kept::fits::<Made>,
    ),
    crate::kept::Kind::table(
        LOOKS,
        crate::kept::Policy::Rows,
        crate::kept::Key::Plain,
        crate::kept::fits::<Look>,
    ),
    crate::kept::Kind::table(
        PEERS,
        crate::kept::Policy::Rows,
        crate::kept::Key::Plain,
        crate::kept::fits::<Peer>,
    ),
    crate::kept::Kind::table(
        SETTINGS,
        crate::kept::Policy::Book,
        crate::kept::Key::Plain,
        crate::kept::text,
    ),
    crate::kept::Kind::table(
        JOBS,
        crate::kept::Policy::Book,
        crate::kept::Key::Plain,
        crate::kept::text,
    ),
    crate::kept::Kind::table(
        CODES,
        crate::kept::Policy::Book,
        crate::kept::Key::Secret,
        crate::kept::fits::<PendingCode>,
    ),
    crate::kept::Kind::book(
        LAUNCH_USAGE,
        crate::kept::Key::Secret,
        crate::kept::fits::<launches::Usage>,
    ),
    crate::kept::Kind::book(
        "opening_by_turn",
        crate::kept::Key::Plain,
        crate::kept::json,
    ),
];

/// The voice timeline's events and their sequence, written here and declared by its desk
/// (`voices::KEPT`).
pub(crate) const VOICE_EVENTS: &str = "voice-events";
pub(crate) const VOICE_SEQUENCE: &str = "voice-event-sequence";

const AGENTS: &str = "agent";
const SETTINGS: &str = "setting";
const FOCUS: &str = "focus";
/// Pairing codes that wait to be handed in, by the digest of the code.
const CODES: &str = "pairing-code";
/// Paired devices, by device id.
const DEVICES: &str = "device";
const JOBS: &str = "job";
/// The register of push devices: one row for each token, under the token.
const PUSH: &str = "push";
/// The other Hosts this one may write to, by the name the owner gave each.
const PEERS: &str = "peer";
/// The personas the owner made, by their ids.
const PERSONAS: &str = "persona";
/// How the owner made built-in personas look, by their ids.
const LOOKS: &str = "persona-look";

/// How long a tool gets to end by itself when its turn is stopped.
const STOP_GRACE_MS: u64 = 5_000;

/// What the fleet knows about an agent: who it is, not what it is doing.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct AgentRecord {
    pub agent_id: String,
    /// The agent's name in every request. Unique among the agents of a Host.
    pub session: String,
    /// The agent's name as a person reads it.
    pub persona: String,
    pub backend: String,
    pub cwd: String,
    #[serde(default)]
    pub model: String,
    #[serde(default)]
    pub effort: String,
    /// Changes when the agent starts a new conversation: clients then drop what they cached.
    pub conversation_id: String,
    pub created_at_ms: i64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub deleted_at_ms: Option<i64>,
    #[serde(default, skip_serializing_if = "is_false")]
    pub ledger_retired: bool,
    /// The user muted it: what it says is marked unread and pages nobody.
    #[serde(default, skip_serializing_if = "is_false")]
    pub muted: bool,
    /// It is woken from time to time to look at what it has standing (the heartbeat).
    #[serde(default, skip_serializing_if = "is_false")]
    pub heartbeat_enabled: bool,
    /// When the agent was put away, when it is. It keeps everything and can still be
    /// written to; the apps leave it out of their lists.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub archived_at_ms: Option<i64>,
    /// The agent that made this one, by its id, when one did. It stays when that agent
    /// is gone.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub parent_agent_id: Option<String>,
    /// `helper`: made by another agent to do one piece of work. Nothing: an agent like
    /// any other.
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub role: String,
    /// It has a job of maintenance and is no agent to chat with ([`clarp_kernel::janitors`]).
    /// It is a member of the fleet and not of the roster: the apps list it apart.
    #[serde(default, skip_serializing_if = "is_false")]
    pub is_janitor: bool,
    /// How a helper's work stands ([`clarp_kernel::helpers`]). Nothing for an agent that
    /// is no helper.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub helper_state: Option<String>,
    /// When a helper's work was over, while it is.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub helper_completed_at_ms: Option<i64>,
    /// The voice it was made with, as it was named: kept for the day this Host speaks.
    /// `{}` is the Python Host's word for none of its own.
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub voice_id: String,
    /// How it speaks: the clause its tool is told with its name. It is the agent's from
    /// the day it is made, and stays when the agent is given another name. Empty: as the
    /// persona of its name speaks.
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub personality: String,
    /// How much it says while it works, when its answer is read aloud: from 0, a line when
    /// it begins and one when it is done, to 3, all along.
    #[serde(default, skip_serializing_if = "is_zero")]
    pub narration: i64,
    /// The symbol it is shown with where it has no picture of its own. Empty: none.
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub avatar_symbol: String,
    /// The digest of the picture it has of its own: the one it was given when it was made,
    /// else the one its persona had then. It is kept under that digest, and the beginning
    /// of it is what the picture's address carries.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub avatar: Option<String>,
    /// The portraits it keeps: the picture it wears, once a change has recorded it, and
    /// the ones chosen beside it from what it published (`clarp_kernel::kept_portraits`).
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub portraits: Vec<Portrait>,
}

impl AgentRecord {
    /// The agent's role, as the contract names it.
    pub fn role(&self) -> &str {
        if self.role.is_empty() {
            "agent"
        } else {
            &self.role
        }
    }

    pub fn is_helper(&self) -> bool {
        self.role == "helper"
    }

    /// Whether the agent is quiet: it never claims its owner's attention by itself (the
    /// kernel's one definition). Asked wherever a record of either kind, an agent of the roster
    /// or a janitor, can appear: its row in the snapshot, and what its turn tells.
    pub fn is_quiet(&self) -> bool {
        clarp_kernel::roster::quiet(self.is_janitor)
    }
}

#[allow(clippy::trivially_copy_pass_by_ref)]
fn is_false(value: &bool) -> bool {
    !*value
}

#[allow(clippy::trivially_copy_pass_by_ref)]
fn is_zero(value: &i64) -> bool {
    *value == 0
}

/// What the user changes about an agent after it is made.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Amend {
    /// Whether what it says pages the user.
    Mute(bool),
    /// Whether it is woken by the heartbeat.
    Heartbeat(bool),
    /// Its name as a person reads it, as [`roster::display_name`] keeps one. Its session
    /// name and its id stay: everything that names the agent goes on naming it.
    Rename(String),
    /// Put away, or taken out again.
    Archive(bool),
    /// Which model its next turn runs with, and how much effort. `None` leaves one as it
    /// is; an empty text is the tool's own choice.
    Tune {
        model: Option<String>,
        effort: Option<String>,
    },
    /// The tool it runs on: for a member that has no conversation with the tool it had.
    /// Whoever asks has seen to that.
    Tool(String),
    /// The voices it was given, as they are stored: one for each engine it has one for.
    Voice { provider: String, voice: String },
    /// How much it says while it works, from 0 to 3.
    Narration(i64),
    /// Whether it is a janitor: an agent is made one, and a janitor is let go.
    Janitor(bool),
    /// The portraits it keeps, and the picture it wears among them when that changes.
    Portraits {
        kept: Vec<Portrait>,
        worn: Option<String>,
    },
    /// The contact it is given (`POST /agent-assign`): the persona of that name, whose
    /// voices, way of speaking, symbol and picture it takes on in place of its own, none
    /// where the persona has none (`contact_assignment.assign_contact`). Its conversation
    /// stays. Not told as a new name: whoever assigns it tells the apps.
    Contact(String, VoiceCatalogue),
    /// The contact `POST /agent-assign` gives it with `auto`, chosen in this turn: its own
    /// where it is in its pool, else the pool's first, else one minted for it (`fetched`: the
    /// voices of Cartesia's this Host has listed, what a minted contact's is picked from first).
    ContactAuto(VoiceCatalogue),
}

/// Why an agent was not changed.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum NotAmended {
    /// The Host has no such agent.
    Unknown,
    /// Another agent has the name.
    Occupied { owner: String, session: String },
    /// No contact could be minted for it.
    NoContact,
}

/// An agent as a change left it. `taken` is done when the agent's own thread has it.
struct Amended {
    record: AgentRecord,
    taken: oneshot::Receiver<()>,
}

/// Why an agent was not made.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum NotMade {
    /// The request cannot be met, and this says why.
    Refused(String),
    /// Another agent has the name.
    Occupied { owner: String, session: String },
    /// Refused in the Python Host's words: the status, its word for the reason, and why.
    Told(Told),
}

/// A refusal in the Python Host's words: a status, its word for the reason, and why.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Told {
    pub status: u16,
    pub code: &'static str,
    pub message: String,
}

impl Told {
    pub(crate) fn new(status: u16, code: &'static str, message: impl Into<String>) -> Self {
        Self {
            status,
            code,
            message: message.into(),
        }
    }
}

impl From<Told> for NotMade {
    fn from(told: Told) -> Self {
        Self::Told(told)
    }
}

impl From<String> for NotMade {
    fn from(why: String) -> Self {
        Self::Refused(why)
    }
}

/// An agent of the fleet: who it is and the way to its thread.
pub struct Member {
    pub record: AgentRecord,
    pub agent: AgentHandle,
}

/// The fleet as a whole, as it was when it last changed.
#[derive(Default)]
pub struct Directory {
    pub launch_usage: Arc<Ledger>,
    pub cwd_owners: Arc<Owners>,
    /// The agents of the roster: everyone a person can speak to.
    pub members: Vec<Arc<Member>>,
    /// The janitors: members of the fleet with a job of maintenance, apart from the roster.
    /// Nothing that goes through the agents meets them; who means one asks for it.
    pub janitors: Vec<Arc<Member>>,
    /// The agent that is open, by id.
    pub focus: Option<String>,
    /// The paired devices that may speak, by the digest of their token.
    pub devices: HashMap<String, Arc<Device>>,
    /// The devices that are told of finished turns: who gets a push.
    pub push: Arc<PushBook>,
    /// The other Hosts this one may write to, by name.
    pub peers: Arc<BTreeMap<String, Peer>>,
    /// The settings the owner makes from an app, each a text under its key
    /// (`transcription.agent_engines`), as the Python Host keeps them.
    pub settings: Arc<BTreeMap<String, String>>,
    pub(crate) coordination: crate::hidden_coordination::Roster,
    /// The personas the owner made, and how he made built-in ones look.
    pub personas: Arc<Book>,
    /// The agents that were removed, by id: the name and the session each had last. A row an
    /// agent wrote, or that answers one, still names it by them, as the Python Host's roster
    /// keeps a removed agent's row.
    pub gone: Arc<BTreeMap<String, (String, String)>>,
}

/// The contact a launch asked for in place of a name of its own: always a minted one
/// (`mint_contact`), or the first free one of its tool's pool and a minted one where there is
/// none (`auto_contact`).
#[derive(Debug, Clone, Default)]
pub struct ContactAsked {
    pub mint: bool,
}

/// What an app says of a persona it makes or changes.
#[derive(Debug, Clone, Default)]
pub struct PersonaSaid {
    pub name: String,
    pub voice_id: String,
    pub base_voice_id: Option<String>,
    pub avatar_symbol: String,
    pub personality: String,
    /// The picture it is given, in base64. Empty: none.
    pub picture: String,
}

/// What a janitor's row says it is, where an agent's says `agent` and a helper's `helper`.
const JANITOR: &str = "janitor";

/// The members of the fleet as the roster and the janitors apart from it.
fn apart(members: &[Arc<Member>]) -> (Vec<Arc<Member>>, Vec<Arc<Member>>) {
    let all = members.iter().cloned();
    all.partition(|member| !member.record.is_janitor)
}

/// A janitor that is made as one: who it is, and where it works.
#[derive(Debug, Clone, Default)]
pub struct JanitorSeed {
    /// Its id, for one of the Host's own: the same on every Host. `None`: one is drawn.
    pub agent_id: Option<String>,
    pub session: String,
    pub persona: String,
    pub backend: String,
    pub cwd: String,
    pub model: String,
    pub effort: String,
}

/// What was left with the Host of one kind: each key with what is under it.
pub type Left = Vec<(String, Vec<u8>)>;

/// The table of a kind of thing that is left with the Host (`FleetHandle::keep`): one of its
/// own, apart from every table the fleet keeps for itself.
pub fn shelf(kind: &str) -> String {
    format!("kept.{kind}")
}

/// The name a persona's or an agent's own picture is kept under, by its digest. The Python
/// Host keeps each as a JPEG file, whatever is in it, and hands it out as one.
pub fn picture_name(digest: &str) -> String {
    format!("{digest}.jpg")
}

impl Directory {
    /// A setting's text, when it is set.
    pub fn setting(&self, key: &str) -> Option<&str> {
        self.settings.get(key).map(String::as_str)
    }

    pub fn by_session(&self, session: &str) -> Option<&Arc<Member>> {
        self.members
            .iter()
            .find(|member| member.record.session == session)
    }

    /// The agent a request names, by its session name or by its id.
    pub fn named(&self, key: &str) -> Option<&Arc<Member>> {
        self.by_session(key).or_else(|| {
            self.members
                .iter()
                .find(|member| member.record.agent_id == key)
        })
    }

    /// The janitor a request names, by its session name or by its id.
    pub fn janitor(&self, key: &str) -> Option<&Arc<Member>> {
        let by = |find: &dyn Fn(&AgentRecord) -> bool| {
            self.janitors.iter().find(|member| find(&member.record))
        };
        by(&|record| record.session == key).or_else(|| by(&|record| record.agent_id == key))
    }

    /// The agent or the janitor a request names: for what is read of either.
    pub fn anyone(&self, key: &str) -> Option<&Arc<Member>> {
        self.named(key).or_else(|| self.janitor(key))
    }

    /// The name and the session of the agent of this id as the roster has them now, also of
    /// one that was removed: what a row that names another agent shows of it when it is read.
    /// By its id alone, as the Python Host joins its roster.
    pub fn shown_as(&self, agent_id: &str) -> Option<(String, String)> {
        match self.by_id(agent_id) {
            Some(member) => Some((member.record.persona.clone(), member.record.session.clone())),
            None => self.gone.get(agent_id).cloned(),
        }
    }

    /// A current agent or janitor, by id without a session lookup or tombstone fallback.
    pub(crate) fn by_id(&self, agent_id: &str) -> Option<&Member> {
        self.members
            .iter()
            .chain(&self.janitors)
            .find(|member| member.record.agent_id == agent_id)
            .map(Arc::as_ref)
    }

    /// The device that presents this token, when it is paired and not revoked.
    pub fn device(&self, token: &str) -> Option<&Arc<Device>> {
        if !pairing::looks_like_a_token(token) {
            return None;
        }
        self.devices.get(&pairing::digest(token))
    }

    /// Whether pushes may go to a row this owner has: a row nobody owns keeps its pushes,
    /// a paired device's only while the device is paired with full scope. A revoked phone
    /// is not shown what agents say.
    pub fn can_receive(&self, owner: &str) -> bool {
        can_receive(&self.devices, owner)
    }
}

fn can_receive(devices: &HashMap<String, Arc<Device>>, owner: &str) -> bool {
    !owner.starts_with("device_")
        || devices
            .values()
            .any(|device| device.device_id == owner && device.scope == Scope::Full)
}

/// Another Host this one may write to: where it is, and the token it takes. The owner names
/// it; an agent then writes to an agent there by that name (`clarp-admin prompt --server`).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Peer {
    pub url: String,
    pub token: String,
    #[serde(default = "enabled")]
    pub enabled: bool,
}

fn enabled() -> bool {
    true
}

/// A registration of a push token, as a device asked for it.
#[derive(Debug, Clone, Default)]
pub struct PushRegistration {
    pub token: String,
    pub session: String,
    pub environment: String,
    pub platform: String,
    pub base_url: String,
    pub push_grant: String,
    /// The paired device that asks, with full scope. Empty: someone else.
    pub owner: String,
    pub provider: String,
    /// The ways an Android phone says it can hold a ring's conversation.
    pub modes: Vec<String>,
}

/// What became of a push to a token.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Pushed {
    /// It went out.
    Sent,
    /// The push service said the token is dead.
    Dead,
    /// The phone took its leave to push back.
    GrantRevoked,
}

/// What every agent of a Host is given.
pub struct Works {
    pub runtime: tokio::runtime::Handle,
    pub opening_gate: crate::calls::opening_launch::Gate,
    pub config: Arc<Config>,
    pub engine: Arc<dyn LogEngine>,
    pub codecs: CodecSet,
    pub launcher: Arc<Launcher>,
    pub clock: Arc<dyn Clock + Send + Sync>,
    pub hub: Arc<Hub>,
    /// The numbers for changes the whole Host counts in one row.
    pub revisions: Arc<Revisions>,
    /// What every turn's tool is told: where the agent's own tools find this Host.
    pub tool_env: Vec<(String, String)>,
    /// The way to the phones that are not listening.
    pub pusher: Pusher,
    /// Where whole files are kept: what agents publish, and the pages of their reports.
    pub blobs: Arc<dyn Blobs>,
    pub portrait_hints: Option<crate::media_created::Announcer>,
    /// The way to the one that speaks.
    pub speaks: crate::voice::Speaks,
    /// What the tools of this Host's agents are told, by what holds when one is asked.
    pub telling: crate::telling::Telling,
    /// The way to the tally of what turns used.
    pub tally: crate::usage::Tally,
    /// What signs the token each turn is given, which tells whose turn calls.
    pub turns: Arc<crate::turn_identity::Signer>,
    /// The pools of the tools' accounts, where this Host runs the owner's selectors itself:
    /// one for each tool that has a selector.
    pub pools: Vec<Arc<crate::pool::Service>>,
    /// Where what the Host could not keep is said.
    pub log: crate::unkept::Log,
    /// What could not be read of what the Host keeps, by its kind and key (`kept.rs`).
    pub left_out: Arc<crate::kept::LeftOut>,
}

enum Ask {
    AppendVoice {
        rows: Vec<serde_json::Value>,
        answer: Option<Answer<usize>>,
    },

    DelegationMismatch(delegations::Mismatch),
    GoalDelegation {
        request: delegations::Request,
        answer: Answer<Result<serde_json::Value, crate::edge::ApiError>>,
    },
    /// The inner error is a refusal: the request cannot be met, and says why.
    Create {
        request: CreateAgentRequest,
        forked: Option<String>,
        /// The contact the request asked for in place of a name of its own, chosen in this
        /// turn ([`ContactAsked`]).
        contact: Option<ContactAsked>,
        catalogue: VoiceCatalogue,
        answer: Answer<Result<AgentRecord, NotMade>>,
    },
    /// An agent is started again in place. `dry`: only what would be is answered.
    StartAgain {
        replaces: String,
        request: CreateAgentRequest,
        dry: bool,
        answer: Answer<Result<AgentRecord, NotMade>>,
    },
    /// The user changes something about an agent. `of_any`: the change is the janitors'
    /// own, and may be of a janitor.
    Amend {
        key: String,
        change: Amend,
        of_any: bool,
        answer: Answer<Result<Amended, NotAmended>>,
    },
    /// A janitor is made as one: one of the Host's own, or one its owner asks for.
    MakeJanitor {
        seed: JanitorSeed,
        answer: Answer<Result<AgentRecord, NotMade>>,
    },
    /// A helper is marked done, failed or working again: by `by`, an agent, or by the
    /// user when nobody is named.
    Mark {
        key: String,
        state: String,
        by: String,
        answer: Answer<Result<String, Told>>,
    },
    /// An agent wrote to another: a helper that writes to the agent that made it has
    /// reported, and one that is written to by it has more work.
    Noted {
        sender: String,
        target: String,
        answer: Answer<()>,
    },
    /// An agent that comes from another Host, as it was known there.
    Adopt {
        record: AgentRecord,
        answer: Answer<Result<Arc<Member>, String>>,
    },
    StoredAgent {
        id: String,
        answer: Answer<Option<AgentRecord>>,
    },
    PastLedger {
        plan: String,
        after: i64,
        limit: usize,
        answer: Answer<(Vec<clarp_kernel::goals::ledger::Event>, bool)>,
    },
    Remove {
        session: String,
        answer: Answer<bool>,
    },
    Select {
        session: String,
        /// Tell the clients. A message to an agent opens it without a word.
        announce: bool,
        answer: Answer<Option<AgentRecord>>,
    },
    Issue {
        device_name: String,
        scope: Scope,
        ttl_seconds: i64,
        answer: Answer<Result<Issued, String>>,
    },
    Exchange {
        code: String,
        device_name: String,
        answer: Answer<Result<Paired, String>>,
    },
    Revoke {
        device_id: String,
        answer: Answer<bool>,
    },
    /// An agent registers a background job under an id. The inner error is the agent that
    /// or computer that has the id already.
    ComputerJobs {
        answer: Answer<Vec<Job>>,
    },
    ComputerJob {
        id: String,
        answer: Answer<Option<ComputerJob>>,
    },
    WatchComputerJob {
        id: String,
        generation: u64,
        answer: Answer<Option<watch::Receiver<bool>>>,
    },
    ChangeComputerJob {
        command: JobCommand,
        writes: Vec<clarp_ports::kv::Write>,
        answer: Answer<Result<JobChange, ComputerJobRefusal>>,
    },
    ClaimJob {
        job_id: String,
        agent_id: String,
        answer: Answer<Result<(), JobOwner>>,
    },
    JobOwner {
        job_id: String,
        answer: Answer<Option<String>>,
    },
    /// A device registers the token its push service gave it. The inner error is why not.
    PushRegister {
        registration: PushRegistration,
        answer: Answer<Result<(), devices::Refused>>,
    },
    /// Stops pushes to live rows: by token, by owner, or a token of one owner only.
    /// Answers how many rows it stopped.
    PushDisable {
        token: String,
        owner: String,
        only_owner: Option<String>,
        answer: Answer<Result<usize, devices::Refused>>,
    },
    /// What became of a push to a token. Nobody waits for it.
    Pushed {
        token: String,
        outcome: Pushed,
    },
    /// The owner names another Host. The inner error is what is missing.
    PeerAdd {
        name: String,
        url: String,
        token: String,
        answer: Answer<Result<(), String>>,
    },
    PeerRemove {
        name: String,
        answer: Answer<bool>,
    },
    /// The owner sets settings. The inner error is a store that failed, or a key that is
    /// no setting's.
    Set {
        settings: Vec<(String, String)>,
        answer: Answer<Result<(), Unavailable>>,
    },
    VoiceMigration {
        view: Box<clarp_kernel::voice_profile::HostView>,
        reviewed: Option<String>,
        answer: Answer<Result<Result<serde_json::Value, String>, Unavailable>>,
    },
    SetCoordination {
        chat: Option<(String, String)>,
        choice: Option<clarp_kernel::hidden_coordination::Visibility>,
        answer: Answer<clarp_kernel::hidden_coordination::Visibility>,
    },
    /// Something small is left with the Host under a key, in the table of its kind, or
    /// taken away (`None`).
    Keep {
        kind: &'static str,
        key: String,
        value: Option<Vec<u8>>,
        answer: Answer<Result<(), Unavailable>>,
    },
    KeepTogether {
        writes: Vec<clarp_ports::kv::Write>,
        answer: Answer<Result<(), Unavailable>>,
    },
    OpeningAudio {
        agent: String,
        turn: u64,
        answer: Answer<Result<Option<Vec<u8>>, Unavailable>>,
    },
    /// What was left of a kind: under one key, or all of it, ordered by key.
    Kept {
        kind: &'static str,
        key: Option<String>,
        answer: Answer<Result<Left, Unavailable>>,
    },
    /// One record of a kind, judged as it is read (`kept::judged`).
    Judged {
        kind: &'static str,
        key: String,
        answer: Answer<Result<crate::kept::Found, Unavailable>>,
    },
    /// A whole kind, each record judged as it is read (`kept::listed_as`), as the desk's type.
    Listed {
        kind: &'static str,
        as_type: Option<fn(&[u8]) -> bool>,
        answer: Answer<Result<crate::kept::Listed, Unavailable>>,
    },
    /// The owner makes a persona (`original` is `None`) or changes the one of that name.
    /// `fetched` are the voices of Cartesia's this Host has listed: what a voice is picked
    /// from first. The inner error is why not, in words for the app.
    PersonaWrite {
        original: Option<String>,
        said: PersonaSaid,
        fetched: VoiceCatalogue,
        /// Whether the apps are told the personas changed: not for a contact minted for an
        /// agent, of which the Python Host tells them nothing.
        announce: bool,
        answer: Answer<Result<serde_json::Value, String>>,
    },
    /// A picture made for a contact (`persona_avatar_generation.generate_for_persona`): kept
    /// on it where it still has neither picture nor symbol, and on the agents that go by it
    /// with none of their own. Answers its name where it was kept.
    PictureMade {
        persona_id: String,
        picture: Vec<u8>,
        answer: Answer<Option<String>>,
    },
    PortraitStatus {
        id: String,
        answer: Answer<(Option<ComputerJob>, u32)>,
    },
    BeginPortraits {
        agent: String,
        answer: Answer<portrait_jobs::Admission>,
    },
    AcceptPortraits {
        id: String,
        generation: u64,
        publication: Box<portrait_jobs::Publication>,
        bytes: [Vec<u8>; 2],
        answer: Answer<bool>,
    },
    /// The owner removes a persona of his own. Answers whether there was one to remove.
    PersonaRemove {
        name: String,
        answer: Answer<bool>,
    },
}

/// The way to the fleet's thread, and the fleet as anyone may read it.
#[derive(Clone)]
pub struct FleetHandle {
    pub(crate) delegation_view: delegations::Rejoin,
    tx: Arc<mpsc::Sender<Ask>>,
    directory: Arc<watch::Receiver<Arc<Directory>>>,
    log: crate::unkept::Log,
    left_out: Arc<crate::kept::LeftOut>,
}

impl FleetHandle {
    pub async fn append_voice(&self, rows: Vec<serde_json::Value>) -> Result<usize, Unavailable> {
        self.ask(|answer| Ask::AppendVoice {
            rows,
            answer: Some(answer),
        })
        .await
    }

    pub fn record_voice(&self, row: serde_json::Value) {
        if self
            .tx
            .send(Ask::AppendVoice {
                rows: vec![row],
                answer: None,
            })
            .is_err()
        {
            self.unkept("voice-events", "the fleet is closed");
        }
    }

    /// What could not be read of what the Host keeps.
    pub fn left_out(&self) -> &crate::kept::LeftOut {
        &self.left_out
    }

    /// The fleet as it is now.
    pub fn directory(&self) -> Arc<Directory> {
        self.directory.borrow().clone()
    }

    async fn ask<T>(&self, make: impl FnOnce(Answer<T>) -> Ask) -> Result<T, Unavailable> {
        let (answer, reply) = oneshot::channel();
        self.tx
            .send(make(answer))
            .map_err(|_| Unavailable("the fleet is closed".to_string()))?;
        reply
            .await
            .map_err(|_| Unavailable("the fleet did not answer".to_string()))?
    }

    /// Adds an agent. The inner error says why the request cannot be met.
    pub async fn create(
        &self,
        request: CreateAgentRequest,
    ) -> Result<Result<AgentRecord, NotMade>, Unavailable> {
        self.create_as(request, None).await
    }

    /// Makes an agent. `forked`: it begins with a copy of a session its tool kept, made
    /// for it under this name, and the apps are told of it as of a fork. Its conversation
    /// goes by the copy's name, as a new agent's goes by the name its tool's session will
    /// have: the copy is new, and nobody else ever read a conversation of that name.
    pub async fn create_as(
        &self,
        request: CreateAgentRequest,
        forked: Option<String>,
    ) -> Result<Result<AgentRecord, NotMade>, Unavailable> {
        self.create_contacted(request, forked, None, VoiceCatalogue::default())
            .await
    }

    /// Makes an agent as [`Self::create_as`] does, as the contact it asked for where it asked
    /// for one: chosen in the turn that registers it, so that launches at once never take the
    /// same contact (`agent_lifecycle.create` holds its lock across the pool, the mint and the
    /// registration). A contact minted for it is written before the agent: a launch that then
    /// fails leaves it, a persona nobody wears, as the Python Host leaves it.
    pub async fn create_contacted(
        &self,
        request: CreateAgentRequest,
        forked: Option<String>,
        contact: Option<ContactAsked>,
        catalogue: VoiceCatalogue,
    ) -> Result<Result<AgentRecord, NotMade>, Unavailable> {
        self.ask(|answer| Ask::Create {
            request,
            forked,
            contact,
            catalogue,
            answer,
        })
        .await
    }

    /// Starts an agent again in place: the same agent under the same session name, as the
    /// request says it is to be from now on (its name, its tool, where it works, its model
    /// and effort, its voice; what the request leaves out stays), with a conversation of a
    /// new name. `dry`: nothing is changed, and the answer is what would be. The inner error
    /// says why not. The agent's own log is told by whoever asks ([`AgentHandle::start_again`]).
    pub async fn start_again(
        &self,
        replaces: String,
        request: CreateAgentRequest,
        dry: bool,
    ) -> Result<Result<AgentRecord, NotMade>, Unavailable> {
        self.ask(|answer| Ask::StartAgain {
            replaces,
            request,
            dry,
            answer,
        })
        .await
    }

    /// Makes a janitor as one: a member of the fleet that is no agent of the roster. It has
    /// no voice and nobody is told that a chat is ready. The inner error says why not.
    pub async fn make_janitor(
        &self,
        seed: JanitorSeed,
    ) -> Result<Result<AgentRecord, NotMade>, Unavailable> {
        self.ask(|answer| Ask::MakeJanitor { seed, answer }).await
    }

    /// Changes something about the agent a request names, by its session name or its id.
    /// Answers the agent as it then is, when its own thread has the change too: what the
    /// agent says next, it says as what it now is.
    pub async fn amend(
        &self,
        key: String,
        change: Amend,
    ) -> Result<Result<AgentRecord, NotAmended>, Unavailable> {
        self.amended(key, change, false).await
    }

    /// Changes something about a member of the fleet, an agent of the roster or a janitor:
    /// what the janitors' own routes change. What a chat's controls change goes through
    /// [`Self::amend`], which finds no janitor.
    pub async fn amend_any(
        &self,
        key: String,
        change: Amend,
    ) -> Result<Result<AgentRecord, NotAmended>, Unavailable> {
        self.amended(key, change, true).await
    }

    async fn amended(
        &self,
        key: String,
        change: Amend,
        of_any: bool,
    ) -> Result<Result<AgentRecord, NotAmended>, Unavailable> {
        let amended = self
            .ask(|answer| Ask::Amend {
                key,
                change,
                of_any,
                answer,
            })
            .await?;
        Ok(match amended {
            Ok(Amended { record, taken }) => {
                // An agent whose thread has ended takes nothing, and says nothing either.
                let _ = taken.await;
                Ok(record)
            }
            Err(why) => Err(why),
        })
    }

    /// Marks a helper done, failed or working again. `by` is the agent that marks it, which
    /// has to be the one that made it; nobody named is the user. Answers the state it then
    /// has, or the Python Host's refusal.
    pub async fn mark(
        &self,
        key: String,
        state: String,
        by: String,
    ) -> Result<Result<String, Told>, Unavailable> {
        self.ask(|answer| Ask::Mark {
            key,
            state,
            by,
            answer,
        })
        .await
    }

    /// Says that one agent wrote to another, both by their ids: when the two are a helper
    /// and the agent that made it, the helper moves. Answers when it has.
    pub async fn noted(&self, sender: String, target: String) -> Result<(), Unavailable> {
        self.ask(|answer| Ask::Noted {
            sender,
            target,
            answer,
        })
        .await
    }

    /// Adds an agent that comes from another Host, under the identity it had there. The inner
    /// error says why it cannot be taken.
    pub async fn adopt(
        &self,
        record: AgentRecord,
    ) -> Result<Result<Arc<Member>, String>, Unavailable> {
        self.ask(|answer| Ask::Adopt { record, answer }).await
    }

    pub async fn stored_agent(&self, id: String) -> Result<Option<AgentRecord>, Unavailable> {
        self.ask(|answer| Ask::StoredAgent { id, answer }).await
    }
    pub async fn past_ledger(
        &self,
        plan: String,
        after: i64,
        limit: usize,
    ) -> Result<(Vec<clarp_kernel::goals::ledger::Event>, bool), Unavailable> {
        self.ask(|answer| Ask::PastLedger {
            plan,
            after,
            limit,
            answer,
        })
        .await
    }

    /// Removes an agent from the fleet. Its history stays on disk. Returns whether it was there.
    pub async fn remove(&self, session: String) -> Result<bool, Unavailable> {
        self.ask(|answer| Ask::Remove { session, answer }).await
    }

    /// Makes an agent the one that is open. `None` when there is no such agent.
    pub async fn select(&self, session: String) -> Result<Option<AgentRecord>, Unavailable> {
        self.ask(|answer| Ask::Select {
            session,
            announce: true,
            answer,
        })
        .await
    }

    /// Makes an agent the one that is open because it was written to. Nobody is told.
    pub async fn open(&self, session: String) -> Result<Option<AgentRecord>, Unavailable> {
        self.ask(|answer| Ask::Select {
            session,
            announce: false,
            answer,
        })
        .await
    }

    /// Issues a pairing code. The inner error says why the request cannot be met.
    pub async fn issue(
        &self,
        device_name: String,
        scope: Scope,
        ttl_seconds: i64,
    ) -> Result<Result<Issued, String>, Unavailable> {
        self.ask(|answer| Ask::Issue {
            device_name,
            scope,
            ttl_seconds,
            answer,
        })
        .await
    }

    /// Hands a pairing code in. The inner error says why it was not taken.
    pub async fn exchange(
        &self,
        code: String,
        device_name: String,
    ) -> Result<Result<Paired, String>, Unavailable> {
        self.ask(|answer| Ask::Exchange {
            code,
            device_name,
            answer,
        })
        .await
    }

    /// Takes a device's right to speak away. Returns whether it had one.
    pub async fn revoke(&self, device_id: String) -> Result<bool, Unavailable> {
        self.ask(|answer| Ask::Revoke { device_id, answer }).await
    }

    /// Registers a push token. The inner error is why it was refused.
    pub async fn push_register(
        &self,
        registration: PushRegistration,
    ) -> Result<Result<(), devices::Refused>, Unavailable> {
        self.ask(|answer| Ask::PushRegister {
            registration,
            answer,
        })
        .await
    }

    /// Stops pushes to live rows. Answers how many rows it stopped.
    pub async fn push_disable(
        &self,
        token: String,
        owner: String,
        only_owner: Option<String>,
    ) -> Result<Result<usize, devices::Refused>, Unavailable> {
        self.ask(|answer| Ask::PushDisable {
            token,
            owner,
            only_owner,
            answer,
        })
        .await
    }

    /// Names another Host this one may write to, or renames where one is. The inner error
    /// is what is missing.
    pub async fn peer_add(
        &self,
        name: String,
        url: String,
        token: String,
    ) -> Result<Result<(), String>, Unavailable> {
        self.ask(|answer| Ask::PeerAdd {
            name,
            url,
            token,
            answer,
        })
        .await
    }

    /// Forgets another Host. Returns whether it was known.
    pub async fn peer_remove(&self, name: String) -> Result<bool, Unavailable> {
        self.ask(|answer| Ask::PeerRemove { name, answer }).await
    }

    /// Makes a persona, or changes the one named `original`. The inner error is why not,
    /// in the Python Host's words. Answers the persona as an app is told it.
    pub async fn persona_write(
        &self,
        original: Option<String>,
        said: PersonaSaid,
        fetched: VoiceCatalogue,
    ) -> Result<Result<serde_json::Value, String>, Unavailable> {
        self.ask(|answer| Ask::PersonaWrite {
            original,
            said,
            fetched,
            announce: true,
            answer,
        })
        .await
    }

    /// Makes a persona minted as an agent's contact (`contact_minting.mint_contact`): as
    /// [`Self::persona_write`] makes one, without telling the apps the personas changed.
    pub async fn persona_mint(
        &self,
        said: PersonaSaid,
        fetched: VoiceCatalogue,
    ) -> Result<Result<serde_json::Value, String>, Unavailable> {
        self.ask(|answer| Ask::PersonaWrite {
            original: None,
            said,
            fetched,
            announce: false,
            answer,
        })
        .await
    }

    /// Keeps a picture made for a contact ([`Ask::PictureMade`]). Its name where it was kept;
    /// `None`: the contact is gone, built in, or has a picture or symbol of its own by now.
    pub async fn picture_made(
        &self,
        persona_id: String,
        picture: Vec<u8>,
    ) -> Result<Option<String>, Unavailable> {
        self.ask(|answer| Ask::PictureMade {
            persona_id,
            picture,
            answer,
        })
        .await
    }

    pub(crate) async fn portrait_status(
        &self,
        id: String,
    ) -> Result<(Option<ComputerJob>, u32), Unavailable> {
        self.ask(|answer| Ask::PortraitStatus { id, answer }).await
    }
    pub(crate) async fn begin_portraits(
        &self,
        agent: String,
    ) -> Result<portrait_jobs::Admission, Unavailable> {
        self.ask(|answer| Ask::BeginPortraits { agent, answer })
            .await
    }
    pub(crate) async fn accept_portraits(
        &self,
        id: String,
        generation: u64,
        publication: portrait_jobs::Publication,
        bytes: [Vec<u8>; 2],
    ) -> Result<bool, Unavailable> {
        self.ask(|answer| Ask::AcceptPortraits {
            id,
            generation,
            publication: Box::new(publication),
            bytes,
            answer,
        })
        .await
    }

    /// Removes a persona the owner made. Answers whether there was one of that name.
    pub async fn persona_remove(&self, name: String) -> Result<bool, Unavailable> {
        self.ask(|answer| Ask::PersonaRemove { name, answer }).await
    }

    /// Sets settings, each a text under its key. They are there for every reader when this
    /// returns, and after a restart.
    pub async fn set(&self, settings: Vec<(String, String)>) -> Result<(), Unavailable> {
        self.ask(|answer| Ask::Set { settings, answer }).await?
    }

    pub(crate) async fn migrate_voices(
        &self,
        view: clarp_kernel::voice_profile::HostView,
        reviewed: Option<String>,
    ) -> Result<Result<serde_json::Value, String>, Unavailable> {
        self.ask(|answer| Ask::VoiceMigration {
            view: Box::new(view),
            reviewed,
            answer,
        })
        .await?
    }

    pub(crate) async fn set_coordination(
        &self,
        chat: Option<(String, String)>,
        choice: Option<clarp_kernel::hidden_coordination::Visibility>,
    ) -> Result<clarp_kernel::hidden_coordination::Visibility, Unavailable> {
        self.ask(|answer| Ask::SetCoordination {
            chat,
            choice,
            answer,
        })
        .await
    }

    /// Leaves something small with the Host under a key, in the table of its kind (where a
    /// phone said it is), or takes it away (`None`). It is there after a restart. A kind's
    /// table is its own: nothing the fleet keeps for itself can be written this way.
    pub async fn keep(
        &self,
        kind: &'static str,
        key: String,
        value: Option<Vec<u8>>,
    ) -> Result<(), Unavailable> {
        self.ask(|answer| Ask::Keep {
            kind,
            key,
            value,
            answer,
        })
        .await?
    }

    /// A write nothing waits on failed: said once in the Host's own log, by the kind of
    /// record and the store's words, never by what was to be written.
    pub fn unkept(&self, kind: &str, why: &str) {
        self.log.unkept(kind, why);
    }

    pub fn unread(&self, kind: &str, why: &str) {
        self.log.unread(kind, why);
    }

    /// Keeps as [`Self::keep`] does, for a caller that waits on nothing: a failure is said.
    pub async fn keep_said(&self, kind: &'static str, key: String, value: Option<Vec<u8>>) {
        if let Err(why) = self.keep(kind, key, value).await {
            self.unkept(kind, &why.0);
        }
    }

    pub async fn keep_together(
        &self,
        rows: Vec<(&'static str, String, Vec<u8>)>,
    ) -> Result<(), Unavailable> {
        self.change_together(rows, Vec::new()).await
    }

    pub async fn change_together(
        &self,
        rows: Vec<(&'static str, String, Vec<u8>)>,
        deletes: Vec<(&'static str, String)>,
    ) -> Result<(), Unavailable> {
        if rows.is_empty() && deletes.is_empty() {
            return Ok(());
        }
        let writes = rows
            .into_iter()
            .map(|(kind, key, value)| (shelf(kind), key, Some(value)))
            .chain(
                deletes
                    .into_iter()
                    .map(|(kind, key)| (shelf(kind), key, None)),
            )
            .collect();
        self.ask(|answer| Ask::KeepTogether { writes, answer })
            .await?
    }

    pub async fn opening_audio(
        &self,
        agent: &str,
        turn: u64,
    ) -> Result<Option<Vec<u8>>, Unavailable> {
        self.ask(|answer| Ask::OpeningAudio {
            agent: agent.to_owned(),
            turn,
            answer,
        })
        .await?
    }

    /// What was left of a kind under a key.
    pub async fn kept(
        &self,
        kind: &'static str,
        key: String,
    ) -> Result<Option<Vec<u8>>, Unavailable> {
        let key = Some(key);
        let found = self.ask(|answer| Ask::Kept { kind, key, answer }).await??;
        Ok(found.into_iter().next().map(|(_, value)| value))
    }

    /// Everything that was left of a kind, ordered by key.
    pub async fn kept_all(&self, kind: &'static str) -> Result<Left, Unavailable> {
        let kept: Left = self
            .ask(|answer| Ask::Kept {
                kind,
                key: None,
                answer,
            })
            .await??;
        // A copy set aside and a row left out are no desk's: the fleet's own read hides them
        // (`Fleet::kept`).
        Ok(kept)
    }

    /// Every record of a kind of rows, judged as its desk's type as it is read (`kept.rs`): those
    /// that can be read, and one that cannot left out, counted and said.
    pub async fn kept_rows<T: serde::de::DeserializeOwned>(
        &self,
        kind: &'static str,
    ) -> Result<crate::kept::Rows<T>, Unavailable> {
        let as_type = Some(crate::kept::fits::<T> as fn(&[u8]) -> bool);
        let listed = self
            .ask(|answer| Ask::Listed {
                kind,
                as_type,
                answer,
            })
            .await??;
        Ok(crate::kept::typed(listed))
    }

    /// Every record of a kind judged when read, each judged as it is read by the one judge
    /// (`kept.rs`): those that are its desk's, ordered by key. The only way such a kind is
    /// read whole.
    pub async fn kept_listed(&self, kind: &'static str) -> Result<Left, Unavailable> {
        let listed = self
            .ask(|answer| Ask::Listed {
                kind,
                as_type: None,
                answer,
            })
            .await??;
        Ok(listed.read)
    }

    /// One record of a kind, read when its desk asks for it (`kept.rs`): one that is not its
    /// desk's is judged (a book's set aside under `<key>.unreadable` first, a write the store
    /// refuses being the answer), counted and said, and the desk is answered nothing.
    pub async fn kept_book(
        &self,
        kind: &'static str,
        key: String,
    ) -> Result<Option<Vec<u8>>, Unavailable> {
        Ok(self.judged(kind, key).await?.read())
    }

    /// What `kept_book` reads, with whether a record was there and did not fit, for a desk that
    /// would otherwise write over what it could not see.
    pub async fn judged(
        &self,
        kind: &'static str,
        key: String,
    ) -> Result<crate::kept::Found, Unavailable> {
        self.ask(|answer| Ask::Judged { kind, key, answer }).await?
    }

    /// A book's document, read entry by entry through the loader: a document that is not its
    /// desk's is set aside (`kept_book`); an entry that is not is carried out of it, counted and
    /// said, to be put back with every save (`kept::saved`).
    pub async fn read_book<B, I>(
        &self,
        kind: &'static str,
        key: &str,
        fields: &[&str],
    ) -> Result<(B, crate::kept::Carried), Unavailable>
    where
        B: serde::de::DeserializeOwned + Default,
        I: serde::de::DeserializeOwned,
    {
        let kept = self.kept_book(kind, key.to_string()).await?;
        let Some(mut document) =
            kept.and_then(|kept| serde_json::from_slice::<serde_json::Value>(&kept).ok())
        else {
            return Ok((B::default(), crate::kept::Carried::default()));
        };
        let carried = crate::kept::carry::<B, I>(&mut document, fields);
        for at in carried.labels() {
            self.leave_out(kind, &format!("{key}#{at}"));
        }
        let book = serde_json::from_value(document).unwrap_or_default();
        Ok((book, carried))
    }

    /// After a book's save: an entry carried out of it whose key the desk wrote since is repaired
    /// by that write (`Carried::written_over`): said as written anew, counted and carried no more.
    pub fn written_over(
        &self,
        kind: &str,
        key: &str,
        carried: &std::sync::Mutex<crate::kept::Carried>,
        saved: &impl serde::Serialize,
    ) {
        let Ok(document) = serde_json::to_value(saved) else {
            return;
        };
        let Ok(mut carried) = carried.lock() else {
            return;
        };
        for (field, entry) in carried.written_over(&document) {
            carried.forget(&field, &entry);
            let at = format!("{key}#{field}.{entry}");
            self.left_out.written(&shelf(kind), &at, &self.log);
        }
    }

    /// A record of a kind left out by a desk that could not use it: counted and said by its
    /// kind and key, as the loader leaves one out.
    pub fn leave_out(&self, kind: &str, key: &str) {
        self.left_out.left_out(&shelf(kind), key, &self.log);
    }

    /// A record left out by a desk is now used after all: no longer counted, and said.
    pub fn recovered(&self, kind: &str, key: &str) {
        self.left_out.recovered(&shelf(kind), key, &self.log);
    }

    /// Says what became of a push to a token. Does not wait.
    pub fn pushed(&self, token: String, outcome: Pushed) {
        let _ = self.tx.send(Ask::Pushed { token, outcome });
    }

    pub async fn computer_jobs(&self) -> Result<Vec<Job>, Unavailable> {
        self.ask(|answer| Ask::ComputerJobs { answer }).await
    }
    pub async fn computer_job(&self, id: String) -> Result<Option<ComputerJob>, Unavailable> {
        self.ask(|answer| Ask::ComputerJob { id, answer }).await
    }
    /// Completes once this generation is no longer active. An absent, terminal or
    /// superseded run returns immediately. A closed fleet reports an error.
    pub async fn wait_computer_job_end(
        &self,
        id: String,
        generation: u64,
    ) -> Result<(), Unavailable> {
        if let Some(mut signal) = self
            .ask(|answer| Ask::WatchComputerJob {
                id,
                generation,
                answer,
            })
            .await?
        {
            signal
                .wait_for(|ended| *ended)
                .await
                .map_err(|_| Unavailable("the fleet is closed".into()))?;
        }
        Ok(())
    }

    /// An internal caller registers its kind, then uses the same generation-fenced
    /// commands as an agent worker. There is no public route to manufacture jobs.
    pub async fn change_computer_job(
        &self,
        command: JobCommand,
    ) -> Result<Result<JobChange, ComputerJobRefusal>, Unavailable> {
        self.ask(|answer| Ask::ChangeComputerJob {
            command,
            writes: Vec::new(),
            answer,
        })
        .await
    }

    /// Commit a successful generation together with its results. A cancelled, ended or
    /// superseded generation cannot publish any of these records.
    pub(crate) async fn finish_computer_job_with(
        &self,
        id: String,
        generation: u64,
        rows: Vec<(&'static str, String, Option<Vec<u8>>)>,
    ) -> Result<Result<JobChange, ComputerJobRefusal>, Unavailable> {
        let command = JobCommand::Finish {
            id,
            generation,
            worker: clarp_kernel::jobs::Worker::none(),
            failed: None,
        };
        let writes = rows
            .into_iter()
            .map(|(kind, key, value)| (shelf(kind), key, value))
            .collect();
        self.ask(|answer| Ask::ChangeComputerJob {
            command,
            writes,
            answer,
        })
        .await
    }

    /// Gives a background job's id to an agent. A job's id is one name on the whole Host:
    /// the inner error identifies its existing owner.
    pub async fn claim_job(
        &self,
        job_id: String,
        agent_id: String,
    ) -> Result<Result<(), JobOwner>, Unavailable> {
        self.ask(|answer| Ask::ClaimJob {
            job_id,
            agent_id,
            answer,
        })
        .await
    }

    /// The agent a background job's id belongs to.
    pub async fn job_owner(&self, job_id: String) -> Result<Option<String>, Unavailable> {
        self.ask(|answer| Ask::JobOwner { job_id, answer }).await
    }
}

struct Fleet {
    launch_usage: Arc<Ledger>,
    cwd_owners: Arc<Owners>,
    delegation_desk: delegations::Desk,
    works: Works,
    store: Box<dyn KeyValue>,
    members: Vec<Arc<Member>>,
    /// The agents that were removed, by id: their name and session as last kept.
    gone: BTreeMap<String, (String, String)>,
    /// The agents being removed whose thread has not answered its close yet, by id: when they
    /// were deleted. Their tombstone is kept; nothing else of their removal is done yet.
    closing: BTreeMap<String, i64>,
    focus: Option<String>,
    /// The devices that may speak, by the digest of their token.
    devices: HashMap<String, Arc<Device>>,
    /// Which agent each background job's id belongs to. The job itself is in that agent's
    /// log; this is only the way to it.
    jobs: HashMap<String, String>,
    computer_jobs: computer_jobs::ComputerJobs,
    /// The register of push devices.
    push: PushBook,
    /// The other Hosts this one may write to.
    peers: BTreeMap<String, Peer>,
    /// The owner's settings, by key.
    settings: BTreeMap<String, String>,
    /// The personas the owner made, and how he made built-in ones look.
    book: Arc<Book>,
    directory: watch::Sender<Arc<Directory>>,
    /// The names of the agents the Host keeps and does not run: those it could not read
    /// (where their bytes name them) and those it could not set up. Nobody else gets them.
    held: Vec<crate::kept::Name>,
}

pub(crate) fn non_empty(text: &str) -> Option<String> {
    Some(text.trim().to_string()).filter(|text| !text.is_empty())
}

fn setup(works: &Works, desk: &delegations::Desk, record: &AgentRecord) -> Result<Setup, String> {
    let backend = clarp_backend::by_id(&record.backend)
        .ok_or_else(|| format!("this Host has no backend named `{}`", record.backend))?;
    let profile = Profile {
        agent_id: record.agent_id.clone(),
        session: record.session.clone(),
        conversation_id: record.conversation_id.clone(),
        dir: works.config.data_dir.join("agents").join(&record.agent_id),
        run_dir: works.config.run_dir.clone(),
        cwd: PathBuf::from(&record.cwd),
        model: non_empty(&record.model),
        effort: non_empty(&record.effort),
        told: Some(works.telling.of(record)),
        tool_env: works
            .tool_env
            .iter()
            .cloned()
            .chain(std::iter::once((
                "CLARP_DELEGATION_DIR".into(),
                works
                    .config
                    .data_dir
                    .join("delegations")
                    .to_string_lossy()
                    .into_owned(),
            )))
            .chain(std::iter::once((
                "CLARP_SESSION".into(),
                record.session.clone(),
            )))
            .collect(),
        stop_grace_ms: STOP_GRACE_MS,
        keep_spools: works.config.keep_spools,
        accounts: works.config.accounts,
        epoch: works.hub.epoch().to_string(),
        hides_coordination: clarp_kernel::coordination::can_leave_note(
            &record.agent_id,
            Some(&record.backend),
            works.turns.has_key(),
        ),
    };
    Ok(Setup {
        delegation_rejoin: desk.for_worker(),
        runtime: works.runtime.clone(),
        opening_gate: works.opening_gate.clone(),
        record: record.clone(),
        profile,
        backend,
        engine: works.engine.clone(),
        codecs: works.codecs.clone(),
        launcher: works.launcher.clone(),
        clock: works.clock.clone(),
        hub: works.hub.clone(),
        revisions: works.revisions.clone(),
        jobs_checked_every: std::time::Duration::from_millis(works.config.jobs_checked_every_ms),
        pusher: works.pusher.clone(),
        blobs: works.blobs.clone(),
        speaks: works.speaks.clone(),
        telling: works.telling.clone(),
        tally: works.tally.clone(),
        turns: works.turns.clone(),
        pools: works.pools.clone(),
    })
}

fn retire_goals(works: &Works, record: &AgentRecord, at: i64) -> Result<(), String> {
    let mut log = works
        .engine
        .open(
            &works.config.data_dir.join("agents").join(&record.agent_id),
            works.codecs.clone(),
        )
        .map_err(|e| e.to_string())?;
    clarp_actor::retire_goal_ledger(log.as_mut(), at).map_err(|e| e.to_string())
}

/// What is counted of a deleted agent whose goals could not be retired yet: it is tried again at
/// the next start.
const RETIRE: &str = "agent-ledger";

/// Opens the fleet from its store, starts a thread for every agent in it, and returns the way
/// to the fleet's own thread.
pub fn start(works: Works, mut store: Box<dyn KeyValue>) -> Result<FleetHandle, String> {
    // Every kind the Host keeps, read once before anything is opened: the count at the door is
    // whole, and a book's record that cannot be read is set aside first (`kept.rs`).
    crate::kept::walk(store.as_mut(), &works.left_out, &works.log).map_err(|e| e.to_string())?;
    // The owner's settings have a dot in their key (`transcription.language`); what the
    // Host keeps for itself in the same table (its id, the focus) has none. They are read
    // first: what an agent's tool is told is worked out from them.
    let mut settings = BTreeMap::new();
    let mut computer_id = String::new();
    for (key, stored) in store.list(SETTINGS).map_err(|error| error.to_string())? {
        if key == "server_id" {
            computer_id = String::from_utf8(stored).map_err(|_| "invalid server id".to_owned())?;
        } else if key.contains('.') {
            settings.insert(key, String::from_utf8_lossy(&stored).into_owned());
        }
    }
    works.telling.settings_are(Arc::new(settings.clone()));
    let mut launch_usage = Ledger::new();
    // What cannot be read is set aside by the walk above, and counted there.
    for (key, bytes) in store
        .list(&shelf(LAUNCH_USAGE))
        .map_err(|e| e.to_string())?
    {
        // A copy set aside is known by the count, not by its name: a directory may be named so.
        if works.left_out.is_left_out(&shelf(LAUNCH_USAGE), &key) {
            continue;
        }
        if let Ok(usage) = serde_json::from_slice::<launches::Usage>(&bytes) {
            launch_usage.insert(key, usage);
        }
    }
    let launch_usage = Arc::new(launch_usage);
    let mut owners = Owners::new();
    let (tx, rx) = mpsc::channel();
    let tx = Arc::new(tx);
    let delegation_desk = delegations::Desk::open(&works, store.as_mut(), &tx)?;
    let mut members = Vec::new();
    let mut gone = BTreeMap::new();
    // Every record of every kind is read alone: one that cannot be read is left out, counted
    // and said, and never written over (`kept.rs`). Its names stay its own.
    let (left, log) = (works.left_out.clone(), works.log.clone());
    // One pass for each kind: each record read as its desk's type, alone (`kept.rs`).
    macro_rules! read {
        ($type:ty, $table:expr) => {
            crate::kept::rows::<$type>(store.as_mut(), $table, &left, &log)
                .map_err(|e| e.to_string())?
        };
    }
    let agents = read!(AgentRecord, AGENTS);
    let mut held: Vec<crate::kept::Name> = agents.names;
    let deleted: HashSet<String> = agents
        .read
        .iter()
        .filter(|(_, record)| record.deleted_at_ms.is_some())
        .map(|(agent_id, _)| agent_id.clone())
        .collect();
    for (agent_id, mut record) in agents.read {
        owners.insert(agent_id.clone(), (record.cwd.clone(), record.is_janitor));
        delegation_desk.known_record(&record);
        if let Some(at) = record.deleted_at_ms {
            gone.insert(
                record.agent_id.clone(),
                (record.persona.clone(), record.session.clone()),
            );
            if !record.ledger_retired {
                // A ledger that cannot be retired now is retired at the next start: counted
                // until then, and the agent stays deleted.
                if retire_goals(&works, &record, at).is_err() {
                    left.retire_later(RETIRE, &agent_id, &log);
                    continue;
                }
                record.ledger_retired = true;
                let value = serde_json::to_vec(&record).map_err(|e| e.to_string())?;
                store
                    .put(AGENTS, &agent_id, &value)
                    .map_err(|e| e.to_string())?;
            }
            continue;
        }
        // A helper whose parent was deleted, and whose removal did not come to it (its parent's
        // thread did not close in time, or the Host stopped first), loses it now, as removal
        // does.
        let parent_gone = record
            .parent_agent_id
            .as_ref()
            .is_some_and(|parent| deleted.contains(parent));
        let current = record.helper_state.as_deref().unwrap_or("");
        if let Some(moved) = helpers::next_state(current, "parent_gone").filter(|_| parent_gone) {
            record.helper_state = Some(moved.state.to_string());
            record.helper_completed_at_ms = moved.completed.then(|| works.clock.now_ms());
            let value = serde_json::to_vec(&record).map_err(|e| e.to_string())?;
            store
                .put(AGENTS, &agent_id, &value)
                .map_err(|e| e.to_string())?;
        }
        match setup(&works, &delegation_desk, &record) {
            Ok(setup) => members.push(Arc::new(Member {
                record,
                agent: agent::spawn(setup),
            })),
            // The agent stays in the store, and its names its own; a Host that has its
            // backend will run it.
            Err(why) => {
                eprintln!("clarp-host: agent {} is left out: {why}", record.session);
                left.not_run(&record.backend);
                held.push(crate::kept::Name {
                    agent_id: record.agent_id.clone(),
                    session: record.session.clone(),
                    persona: record.persona.clone(),
                    voice_id: record.voice_id.clone(),
                });
            }
        }
    }
    let focus = store
        .get(SETTINGS, FOCUS)
        .map_err(|error| error.to_string())?
        .and_then(|stored| String::from_utf8(stored).ok())
        .filter(|agent_id| {
            members
                .iter()
                .any(|member| member.record.agent_id == *agent_id)
        });
    let mut devices = HashMap::new();
    for (_, device) in read!(Device, DEVICES).read {
        if device.revoked_at.is_none() {
            devices.insert(device.token_hash.clone(), Arc::new(device));
        }
    }
    let computer_jobs = computer_jobs::ComputerJobs::open(
        store.as_ref(),
        computer_id,
        &works.revisions,
        &works.left_out,
    )?;
    let mut jobs = HashMap::new();
    for (job_id, stored) in store.list(JOBS).map_err(|error| error.to_string())? {
        jobs.insert(job_id, String::from_utf8_lossy(&stored).into_owned());
    }
    let mut push = PushBook::new();
    for (_, row) in read!(PushRow, PUSH).read {
        push.apply(&[row]);
    }
    // The personas the owner made, and the looks he gave built-in ones.
    let made: Vec<Made> = read!(Made, PERSONAS)
        .read
        .into_iter()
        .map(|(_, made)| made)
        .collect();
    let looks: Vec<Look> = read!(Look, LOOKS)
        .read
        .into_iter()
        .map(|(_, look)| look)
        .collect();
    let book = Arc::new(Book::new(made, looks));
    let mut peers = BTreeMap::new();
    for (name, peer) in read!(Peer, PEERS).read {
        peers.insert(name, peer);
    }
    let (roster, janitors) = apart(&members);
    let cwd_owners = Arc::new(owners);
    let (directory, read) = watch::channel(Arc::new(Directory {
        launch_usage: launch_usage.clone(),
        cwd_owners: cwd_owners.clone(),
        members: roster,
        janitors,
        focus: focus.clone(),
        devices: devices.clone(),
        push: Arc::new(push.clone()),
        peers: Arc::new(peers.clone()),
        settings: Arc::new(settings.clone()),
        coordination: crate::hidden_coordination::Roster::of(
            &settings,
            members.iter().map(|member| &member.record),
        ),
        personas: book.clone(),
        gone: Arc::new(gone.clone()),
    }));
    let read = Arc::new(read);
    works.hub.queue_roster(&read);
    let log = works.log.clone();
    let delegation_view = delegation_desk.for_worker();
    let left_out = works.left_out.clone();
    // A write to a key that was left out is the owner's repair: it is said, and no longer
    // counted.
    let store: Box<dyn KeyValue> = Box::new(crate::kept::Watched::new(
        store,
        left_out.clone(),
        log.clone(),
    ));
    let fleet = Fleet {
        launch_usage,
        cwd_owners,
        delegation_desk,
        works,
        store,
        members,
        gone,
        closing: BTreeMap::new(),
        focus,
        devices,
        jobs,
        computer_jobs,
        push,
        peers,
        settings,
        book,
        directory,
        held,
    };
    std::thread::Builder::new()
        .name("fleet".to_string())
        .spawn(move || fleet.run(&rx))
        .map_err(|error| format!("the fleet's thread could not be started: {error}"))?;
    Ok(FleetHandle {
        delegation_view,
        tx,
        directory: read,
        log,
        left_out,
    })
}

fn read_opening_audio(
    store: &dyn KeyValue,
    agent: &str,
    turn: u64,
) -> Result<Option<Vec<u8>>, clarp_ports::kv::StoreError> {
    match clarp_kernel::rings::binding::key(agent, turn) {
        Some(key) => store.get(&shelf("opening_by_turn"), &key),
        None => Ok(None),
    }
}

impl Fleet {
    fn run(mut self, rx: &mpsc::Receiver<Ask>) {
        let every = Duration::from_millis(self.works.config.helpers_checked_every_ms.max(1));
        let mut looked = Instant::now();
        loop {
            // Now and then the fleet looks after the helpers nobody speaks of any more.
            let ask = match rx.recv_timeout(every.saturating_sub(looked.elapsed())) {
                Ok(ask) => Some(ask),
                Err(RecvTimeoutError::Timeout) => None,
                Err(RecvTimeoutError::Disconnected) => return,
            };
            if looked.elapsed() >= every {
                looked = Instant::now();
                if let Err(why) = self.look_after_helpers() {
                    eprintln!(
                        "clarp-host: the helpers could not be looked after: {}",
                        why.0
                    );
                }
            }
            let Some(ask) = ask else { continue };
            // Each answer goes to whoever asked. One who has gone by then is told nothing:
            // nobody is left to tell, and what was done is done (and said, where it failed).
            match ask {
                Ask::DelegationMismatch(m) => {
                    let id = m.id().to_owned();
                    if let Err(why) = self.reconcile_mismatch(m) {
                        self.works.log.unkept("delegation", &format!("{id}: {why}"));
                    }
                }
                Ask::GoalDelegation { request, answer } => {
                    let _ = answer.send(Ok(self.delegation_request(request)));
                }
                Ask::Create {
                    request,
                    forked,
                    contact,
                    catalogue,
                    answer,
                } => {
                    let _ = answer.send(Ok(self.create(request, forked, contact, &catalogue)));
                }
                Ask::StartAgain {
                    replaces,
                    request,
                    dry,
                    answer,
                } => {
                    let _ = answer.send(Ok(self.start_again(&replaces, request, dry)));
                }
                Ask::Mark {
                    key,
                    state,
                    by,
                    answer,
                } => {
                    let _ = answer.send(self.mark(&key, &state, &by));
                }
                Ask::Noted {
                    sender,
                    target,
                    answer,
                } => {
                    let _ = answer.send(self.noted(&sender, &target));
                }
                Ask::Amend {
                    key,
                    change,
                    of_any,
                    answer,
                } => {
                    let _ = answer.send(self.amend(&key, change, of_any));
                }
                Ask::MakeJanitor { seed, answer } => {
                    let _ = answer.send(Ok(self.make_janitor(seed)));
                }
                Ask::Adopt { record, answer } => {
                    let _ = answer.send(Ok(self.adopt(record)));
                }
                Ask::StoredAgent { id, answer } => {
                    let _ = answer.send(self.stored_agent(&id));
                }
                Ask::PastLedger {
                    plan,
                    after,
                    limit,
                    answer,
                } => {
                    let _ = answer.send(self.past_ledger(&plan, after, limit));
                }
                Ask::Remove { session, answer } => {
                    let _ = answer.send(self.remove(&session));
                }
                Ask::Select {
                    session,
                    announce,
                    answer,
                } => {
                    let _ = answer.send(self.select(&session, announce));
                }
                Ask::Issue {
                    device_name,
                    scope,
                    ttl_seconds,
                    answer,
                } => {
                    let _ = answer.send(Ok(self.issue(&device_name, scope, ttl_seconds)));
                }
                Ask::Exchange {
                    code,
                    device_name,
                    answer,
                } => {
                    let _ = answer.send(Ok(self.exchange(&code, &device_name)));
                }
                Ask::Revoke { device_id, answer } => {
                    let _ = answer.send(self.revoke(&device_id));
                }
                Ask::ComputerJobs { answer } => {
                    let _ = answer.send(Ok(self.computer_jobs.list()));
                }
                Ask::ComputerJob { id, answer } => {
                    let _ = answer.send(Ok(self.computer_jobs.get(&id)));
                }
                Ask::WatchComputerJob {
                    id,
                    generation,
                    answer,
                } => {
                    let _ = answer.send(Ok(self.computer_jobs.subscribe(id, generation)));
                }
                Ask::ChangeComputerJob {
                    command,
                    writes,
                    answer,
                } => {
                    let _ = answer.send(self.change_computer_job(command, writes));
                }
                Ask::ClaimJob {
                    job_id,
                    agent_id,
                    answer,
                } => {
                    let _ = answer.send(self.claim_job(job_id, agent_id));
                }
                Ask::JobOwner { job_id, answer } => {
                    let mut owner = self.jobs.get(&job_id).cloned();
                    // The agent commits provider jobs autonomously. Recover
                    // their directory entry from the published committed book.
                    if owner.is_none() && job_id.starts_with("claude-task:") {
                        owner = self
                            .members
                            .iter()
                            .find(|m| m.agent.view().provider_mirrors.contains_key(&job_id))
                            .map(|m| m.record.agent_id.clone());
                        if let Some(id) = &owner {
                            self.jobs.insert(job_id, id.clone());
                        }
                    }
                    let _ = answer.send(Ok(owner));
                }
                Ask::PushRegister {
                    registration,
                    answer,
                } => {
                    let _ = answer.send(self.push_register(&registration));
                }
                Ask::PushDisable {
                    token,
                    owner,
                    only_owner,
                    answer,
                } => {
                    let _ = answer.send(self.push_disable(&token, &owner, only_owner.as_deref()));
                }
                Ask::Pushed { token, outcome } => self.pushed(&token, outcome),
                Ask::PeerAdd {
                    name,
                    url,
                    token,
                    answer,
                } => {
                    let _ = answer.send(self.peer_add(&name, &url, &token));
                }
                Ask::PeerRemove { name, answer } => {
                    let _ = answer.send(self.peer_remove(&name));
                }
                Ask::PersonaWrite {
                    original,
                    said,
                    fetched,
                    announce,
                    answer,
                } => {
                    let written =
                        self.persona_write(original.as_deref(), &said, &fetched, announce);
                    let _ = answer.send(written);
                }
                Ask::PictureMade {
                    persona_id,
                    picture,
                    answer,
                } => {
                    let _ = answer.send(self.picture_made(&persona_id, &picture));
                }
                Ask::PortraitStatus { id, answer } => {
                    let _ = answer.send(Ok((self.computer_jobs.get(&id), self.portrait_count())));
                }
                Ask::BeginPortraits { agent, answer } => {
                    let _ = answer.send(self.begin_portraits(&agent));
                }
                Ask::AcceptPortraits {
                    id,
                    generation,
                    publication,
                    bytes,
                    answer,
                } => {
                    let _ =
                        answer.send(self.accept_portraits(&id, generation, &publication, bytes));
                }
                Ask::PersonaRemove { name, answer } => {
                    let _ = answer.send(self.persona_remove(&name));
                }
                Ask::VoiceMigration {
                    view,
                    reviewed,
                    answer,
                } => {
                    let result = self.migrate_voices(*view, reviewed.as_deref());
                    let _ = answer.send(Ok(result));
                }
                Ask::Set { settings, answer } => {
                    let _ = answer.send(Ok(self.set(settings)));
                }
                Ask::SetCoordination {
                    chat,
                    choice,
                    answer,
                } => {
                    let _ = answer.send(self.set_coordination(chat, choice));
                }
                Ask::Keep {
                    kind,
                    key,
                    value,
                    answer,
                } => {
                    let _ = answer.send(Ok(self.keep(kind, &key, value)));
                }
                Ask::AppendVoice { rows, answer } => {
                    let result = self.append_voice(rows);
                    if let Some(answer) = answer {
                        let _ = answer.send(result);
                    } else if let Err(error) = result {
                        self.works.log.unkept("voice-events", &error.0);
                    }
                }
                Ask::KeepTogether { writes, answer } => {
                    let result = self
                        .store
                        .put_together(&writes)
                        .map_err(|e| Unavailable(e.to_string()));
                    let _ = answer.send(Ok(result));
                }
                Ask::OpeningAudio {
                    agent,
                    turn,
                    answer,
                } => {
                    let result = read_opening_audio(self.store.as_ref(), &agent, turn)
                        .map_err(|e| Unavailable(e.to_string()));
                    let _ = answer.send(Ok(result));
                }
                Ask::Kept { kind, key, answer } => {
                    let _ = answer.send(Ok(self.kept(kind, key.as_deref())));
                }
                Ask::Judged { kind, key, answer } => {
                    let (left, log) = (&self.works.left_out, &self.works.log);
                    let found =
                        crate::kept::judged(self.store.as_mut(), &shelf(kind), &key, left, log);
                    let _ = answer.send(Ok(found.map_err(|e| Unavailable(e.to_string()))));
                }
                Ask::Listed {
                    kind,
                    as_type,
                    answer,
                } => {
                    let (left, log) = (&self.works.left_out, &self.works.log);
                    let table = shelf(kind);
                    let store = self.store.as_mut();
                    let listed = match as_type {
                        Some(as_type) => crate::kept::listed_as(store, &table, as_type, left, log),
                        None => crate::kept::listed(store, &table, left, log),
                    };
                    let _ = answer.send(Ok(listed.map_err(|e| Unavailable(e.to_string()))));
                }
            }
        }
    }

    fn keep(&mut self, kind: &str, key: &str, value: Option<Vec<u8>>) -> Result<(), Unavailable> {
        let table = shelf(kind);
        let done = match value {
            Some(value) => self.store.put(&table, key, &value),
            None => self.store.delete(&table, key).map(|_| ()),
        };
        done.map_err(|error| Unavailable(error.to_string()))
    }

    fn kept(&self, kind: &str, key: Option<&str>) -> Result<Left, Unavailable> {
        let table = shelf(kind);
        let found = match key {
            Some(key) => self.store.get(&table, key).map(|value| {
                let value = value.map(|value| (key.to_string(), value));
                value.into_iter().collect::<Left>()
            }),
            None => self.store.list(&table),
        };
        let found = found.map_err(|error| Unavailable(error.to_string()))?;
        // What the judge holds is no desk's (`kept.rs`): a row left out, until the owner repairs
        // or deletes it, and a copy set aside, known by the count and not by its name.
        let left = &self.works.left_out;
        Ok(found
            .into_iter()
            .filter(|(key, _)| !left.is_left_out(&table, key))
            .collect())
    }

    fn claim_job(
        &mut self,
        job_id: String,
        agent_id: String,
    ) -> Result<Result<(), JobOwner>, Unavailable> {
        if self.computer_jobs.contains(&job_id) {
            return Ok(Err(JobOwner::Computer));
        }
        match self.jobs.get(&job_id) {
            Some(owner) if *owner != agent_id => Ok(Err(JobOwner::Agent(owner.clone()))),
            Some(_) => Ok(Ok(())),
            None => {
                self.store
                    .put(JOBS, &job_id, agent_id.as_bytes())
                    .map_err(|error| Unavailable(error.to_string()))?;
                self.jobs.insert(job_id, agent_id);
                Ok(Ok(()))
            }
        }
    }

    fn append_voice(&mut self, rows: Vec<serde_json::Value>) -> Result<usize, Unavailable> {
        if rows.is_empty() {
            return Ok(0);
        }
        // The high-water mark is committed with the batch. A failed write consumes no IDs,
        // and a restart never reads the owner's transcripts just to discover the next one.
        let cursor = shelf("voice-event-sequence");
        let table = shelf("voice-events");
        // Judged as it is read (`kept.rs`): one that is not eight bytes is moved aside, and the
        // sequence is not begun anew over the events it numbered.
        let (left, log) = (&self.works.left_out, &self.works.log);
        let found = crate::kept::judged(self.store.as_mut(), &cursor, "last", left, log)
            .map_err(|e| Unavailable(e.to_string()))?;
        let mut last = match found {
            crate::kept::Found::Absent => 0,
            crate::kept::Found::Read(bytes) => u64::from_be_bytes(
                bytes
                    .try_into()
                    .map_err(|_| Unavailable("invalid voice event sequence".into()))?,
            ),
            crate::kept::Found::Unfit => {
                return Err(Unavailable("invalid voice event sequence".into()));
            }
        };
        let count = rows.len();
        let mut writes = Vec::with_capacity(count + 1);
        for mut row in rows {
            last = last
                .checked_add(1)
                .ok_or_else(|| Unavailable("voice event sequence exhausted".into()))?;
            row["event_id"] = serde_json::json!(last);
            let bytes = serde_json::to_vec(&row)
                .map_err(|_| Unavailable("cannot encode voice event".into()))?;
            writes.push((table.clone(), format!("{last:020}"), Some(bytes)));
        }
        writes.push((cursor, "last".into(), Some(last.to_be_bytes().to_vec())));
        self.store
            .put_together(&writes)
            .map_err(|e| Unavailable(e.to_string()))?;
        Ok(count)
    }

    fn now_ms(&self) -> i64 {
        self.works.clock.now_ms()
    }

    fn publish(&self) {
        let settings = Arc::new(self.settings.clone());
        // The one that works out what agents' tools are told reads the same settings.
        self.works.telling.settings_are(settings.clone());
        let (roster, janitors) = apart(&self.members);
        self.directory.send_replace(Arc::new(Directory {
            launch_usage: self.launch_usage.clone(),
            cwd_owners: self.cwd_owners.clone(),
            members: roster,
            janitors,
            focus: self.focus.clone(),
            devices: self.devices.clone(),
            push: Arc::new(self.push.clone()),
            peers: Arc::new(self.peers.clone()),
            settings,
            coordination: crate::hidden_coordination::Roster::of(
                &self.settings,
                self.members.iter().map(|member| &member.record),
            ),
            personas: self.book.clone(),
            gone: Arc::new(self.gone.clone()),
        }));
    }

    fn voice_pool_settings(&self) -> toml::Table {
        let mut file = crate::owner::OwnerFile::new(self.works.config.speech_keys.clone())
            .read()
            .unwrap_or_default();
        crate::owner::apply_speech_settings(&mut file, |key| {
            self.settings.get(key).map(String::as_str)
        });
        file
    }

    /// The assignment and the write run in the same fleet turn.
    fn assigned_voice(
        &self,
        file: &toml::Table,
        owner: &Owner<'_>,
        name: &str,
        stored: &str,
        catalogue: &VoiceCatalogue,
    ) -> String {
        let host = clarp_kernel::voice_profile::HostView {
            voices: crate::owner::voices(file),
            agent_languages: languages::languages(
                self.settings
                    .get(languages::AGENT_LANGUAGES_KEY)
                    .map(String::as_str),
            ),
            language: languages::selected(
                self.settings
                    .get(languages::LANGUAGE_KEY)
                    .map(String::as_str),
                &self.works.config.speech_language,
            ),
            ..Default::default()
        };
        let configured = crate::owner::cartesia_pool(file);
        let pool = clarp_kernel::voice_pool::Pool {
            host: &host,
            configured_cartesia: &configured,
            catalogue,
            roster: owner.roster,
        };
        let contacts = self.book.listed(owner);
        let agents = self
            .members
            .iter()
            .map(|member| &member.record)
            .filter(|record| !record.is_janitor && record.archived_at_ms.is_none())
            .map(|record| clarp_kernel::voice_pool::User {
                name: &record.persona,
                stored: &record.voice_id,
                builtin: false,
            });
        let contacts = contacts
            .iter()
            .map(|contact| clarp_kernel::voice_pool::User {
                name: contact["name"].as_str().unwrap_or(""),
                stored: contact["voice_id"].as_str().unwrap_or(""),
                builtin: contact["builtin"] == true,
            });
        let users: Vec<_> = agents.chain(contacts).collect();
        let assigned = pool.assign(name, stored, &users);
        if let Some(note) = assigned.catalogue_miss {
            eprintln!(
                "voicePoolNoCatalogueVoice provider=cartesia language={} eligible=0 total={}",
                note.language, note.total
            );
        }
        for note in assigned.exhausted {
            eprintln!(
                "voicePoolExhausted provider={} pool={} shared_by={}",
                note.provider, note.size, note.shared_by
            );
        }
        assigned.stored
    }

    /// The owner makes a persona, or changes one. What is kept is written before anybody
    /// is told: the picture first, then what names it.
    fn persona_write(
        &mut self,
        original: Option<&str>,
        said: &PersonaSaid,
        fetched: &VoiceCatalogue,
        announce: bool,
    ) -> Result<Result<serde_json::Value, String>, Unavailable> {
        let file = self.voice_pool_settings();
        let [roster, cartesia, gemini] = crate::owner::personas(&file);
        let owner = Owner {
            roster: &roster,
            cartesia: &cartesia,
            gemini: &gemini,
        };
        let name = personas::named(&said.name).unwrap_or_else(|| said.name.clone());
        let voice_id = if original.is_none() {
            let stored = if said.voice_id.is_empty() {
                "{}"
            } else {
                &said.voice_id
            };
            clarp_kernel::said::canonical_voice(
                &self.assigned_voice(&file, &owner, &name, stored, fetched),
            )
        } else if said.voice_id.is_empty() {
            String::new()
        } else if let Some(original) = original {
            let current = self.book.named(&owner, original);
            let stored = current
                .as_ref()
                .and_then(|row| row["voice_id"].as_str())
                .unwrap_or("");
            clarp_kernel::said::merge_provider_maps(
                stored,
                &said.voice_id,
                said.base_voice_id.as_deref(),
            )
        } else {
            clarp_kernel::said::canonical_voice(&said.voice_id)
        };
        let wanted = Wanted {
            name: &said.name,
            voice_id: &voice_id,
            avatar_symbol: &said.avatar_symbol,
            personality: &said.personality,
            picture: &said.picture,
        };
        let voice = original
            .filter(|_| voice_id.is_empty())
            .and_then(|original| self.book.named(&owner, original))
            .filter(|current| current["voice_id"].as_str().is_none_or(str::is_empty))
            .map(|_| self.assigned_voice(&file, &owner, &name, "{}", fetched))
            .unwrap_or_default();
        let mut book = (*self.book).clone();
        let changed = match original {
            None => {
                let id = format!("persona-{}", ids::hex(8));
                book.create(&owner, &wanted, (&id, &voice), self.now_ms())
            }
            Some(original) => book.update(&owner, original, &wanted, &voice),
        };
        let change = match changed {
            Ok(change) => change,
            Err(why) => return Ok(Err(why)),
        };
        let failed = |error: clarp_ports::kv::StoreError| Unavailable(error.to_string());
        let (table, id, kept, digest) = match &change.kept {
            Kept::Made(made) => (
                PERSONAS,
                &made.persona_id,
                serde_json::to_vec(made),
                &made.avatar,
            ),
            Kept::Look(look) => (
                LOOKS,
                &look.persona_id,
                serde_json::to_vec(look),
                &look.avatar,
            ),
        };
        if let (Some(bytes), Some(digest)) = (&change.picture, digest) {
            let name = picture_name(digest);
            self.works.blobs.put(&name, bytes).map_err(failed)?;
        }
        let kept = kept.map_err(|error| Unavailable(error.to_string()))?;
        self.store.put(table, id, &kept).map_err(failed)?;
        let told = book.told(&owner, &change.kept);
        self.book = Arc::new(book);
        self.publish();
        let kind = if original.is_none() {
            "persona-created"
        } else {
            "persona-updated"
        };
        if announce {
            self.works
                .hub
                .publish(wire::roster_personas(kind), self.now_ms());
        }
        Ok(Ok(told))
    }

    /// A picture made for a contact, kept in this turn: on the contact where it is still the
    /// owner's own and has neither picture nor symbol (looked at again now: one may have come
    /// meanwhile, `generate_for_persona`), and on every agent that goes by its name with
    /// neither of its own (`agents.sync_contact_avatar`). The apps are told the contact
    /// changed, with its name.
    fn picture_made(
        &mut self,
        persona_id: &str,
        picture: &[u8],
    ) -> Result<Option<String>, Unavailable> {
        let listed = self.personas_listed();
        let Some(persona) = listed
            .iter()
            .find(|persona| persona["id"] == persona_id && persona["builtin"] == false)
        else {
            return Ok(None);
        };
        let text = |key: &str| persona[key].as_str().unwrap_or("").to_string();
        if !text("avatar_url").is_empty() || !text("avatar_symbol").is_empty() {
            return Ok(None);
        }
        let name = text("name");
        // The picture alone changes (`personas.update_avatar`): kept under its digest, and the
        // contact's record with that digest and nothing else of it written again.
        let Ok(Some((bytes, made_digest))) =
            personas::picture(&clarp_kernel::python::b64encode(picture))
        else {
            return Ok(None);
        };
        let [named, cartesia, gemini] = self.works.telling.owner_tables();
        let owner = Owner {
            roster: &named,
            cartesia: &cartesia,
            gemini: &gemini,
        };
        let mut book = (*self.book).clone();
        let Some(made) = book.set_picture(&owner, persona_id, &made_digest) else {
            return Ok(None);
        };
        let failed = |error: clarp_ports::kv::StoreError| Unavailable(error.to_string());
        self.works
            .blobs
            .put(&picture_name(&made_digest), &bytes)
            .map_err(failed)?;
        let kept = serde_json::to_vec(&made).map_err(|error| Unavailable(error.to_string()))?;
        self.store
            .put(PERSONAS, persona_id, &kept)
            .map_err(failed)?;
        self.book = Arc::new(book);
        self.publish();
        let digest = Some(made_digest);
        let bare: Vec<usize> = (0..self.members.len())
            .filter(|at| {
                let record = &self.members[*at].record;
                !record.is_janitor
                    && self.members[*at].agent.accepting_work().is_ok()
                    && record.avatar.is_none()
                    && record.avatar_symbol.is_empty()
                    && record.persona.eq_ignore_ascii_case(&name)
            })
            .collect();
        for at in bare {
            let mut record = self.members[at].record.clone();
            record.avatar.clone_from(&digest);
            let (took, _taken) = oneshot::channel();
            self.replace(at, record, took)?;
        }
        self.works
            .hub
            .publish(wire::roster_persona_updated(&name), self.now_ms());
        Ok(Some(name))
    }

    /// The owner removes a persona of his own. Its picture stays where it is kept: an agent
    /// that was made as that persona may still wear it.
    fn persona_remove(&mut self, name: &str) -> Result<bool, Unavailable> {
        let [roster, cartesia, gemini] = self.works.telling.owner_tables();
        let owner = Owner {
            roster: &roster,
            cartesia: &cartesia,
            gemini: &gemini,
        };
        let mut book = (*self.book).clone();
        let Some(made) = book.delete(&owner, name) else {
            return Ok(false);
        };
        self.store
            .delete(PERSONAS, &made.persona_id)
            .map_err(|error| Unavailable(error.to_string()))?;
        self.book = Arc::new(book);
        self.publish();
        self.works
            .hub
            .publish(wire::roster_personas("persona-deleted"), self.now_ms());
        Ok(true)
    }

    /// Writes settings and publishes them. Only what has a dot in its key is a setting.
    fn set(&mut self, settings: Vec<(String, String)>) -> Result<(), Unavailable> {
        let mut writes = Vec::with_capacity(settings.len());
        for (key, text) in &settings {
            if !key.contains('.') {
                return Err(Unavailable(format!("`{key}` is no setting")));
            }
            writes.push((
                SETTINGS.to_string(),
                key.clone(),
                Some(text.as_bytes().to_vec()),
            ));
        }
        self.store
            .put_together(&writes)
            .map_err(|error| Unavailable(error.to_string()))?;
        self.settings.extend(settings);
        self.publish();
        Ok(())
    }

    fn set_coordination(
        &mut self,
        chat: Option<(String, String)>,
        choice: Option<clarp_kernel::hidden_coordination::Visibility>,
    ) -> Result<clarp_kernel::hidden_coordination::Visibility, Unavailable> {
        use clarp_kernel::hidden_coordination::{self as rules, Visibility};
        let key = if let Some((id, _)) = &chat {
            if self.place_of(id).is_none() {
                return Err(Unavailable("no such agent".to_owned()));
            }
            rules::chat_key(id)
        } else {
            rules::GLOBAL_KEY.to_owned()
        };
        self.set(vec![(key, choice.map_or("", Visibility::word).to_owned())])?;
        for member in &self.members {
            if chat
                .as_ref()
                .is_none_or(|(id, _)| *id == member.record.agent_id)
            {
                member.agent.refresh_telling();
            }
        }
        let effective = choice.unwrap_or_else(|| {
            self.settings
                .get(rules::GLOBAL_KEY)
                .and_then(|value| Visibility::read(value))
                .unwrap_or_default()
        });
        let mut event = serde_json::json!({"type": "agent-roster", "kind": rules::ROSTER_KIND,
                               "coordination_visibility": effective});
        if let Some((_, session)) = chat {
            event["session"] = serde_json::json!(session);
        }
        self.works.hub.publish(event, self.now_ms());
        Ok(effective)
    }

    fn peer_add(
        &mut self,
        name: &str,
        url: &str,
        token: &str,
    ) -> Result<Result<(), String>, Unavailable> {
        let (name, url, token) = (name.trim(), url.trim().trim_end_matches('/'), token.trim());
        let addressed = url.starts_with("http://") || url.starts_with("https://");
        if name.is_empty() || !addressed || token.is_empty() {
            return Ok(Err("peer requires name, HTTP(S) URL, and token".to_string()));
        }
        let peer = Peer {
            url: url.to_string(),
            token: token.to_string(),
            enabled: true,
        };
        let stored = serde_json::to_vec(&peer).map_err(|error| Unavailable(error.to_string()))?;
        self.store
            .put(PEERS, name, &stored)
            .map_err(|error| Unavailable(error.to_string()))?;
        self.peers.insert(name.to_string(), peer);
        self.publish();
        Ok(Ok(()))
    }

    fn peer_remove(&mut self, name: &str) -> Result<bool, Unavailable> {
        let known = self
            .store
            .delete(PEERS, name)
            .map_err(|error| Unavailable(error.to_string()))?;
        self.peers.remove(name);
        self.publish();
        Ok(known)
    }

    /// Keeps rows of the push register as a change left them, and publishes the register.
    fn keep_push(&mut self, changed: &[PushRow]) -> Result<(), Unavailable> {
        for row in changed {
            let stored = serde_json::to_vec(row).map_err(|error| Unavailable(error.to_string()))?;
            self.store
                .put(PUSH, &row.token, &stored)
                .map_err(|error| Unavailable(error.to_string()))?;
        }
        if !changed.is_empty() {
            self.push.apply(changed);
            self.publish();
        }
        Ok(())
    }

    fn push_register(
        &mut self,
        asked: &PushRegistration,
    ) -> Result<Result<(), devices::Refused>, Unavailable> {
        let devices = &self.devices;
        let registered = self.push.register(
            &devices::Registration {
                token: &asked.token,
                session: &asked.session,
                environment: &asked.environment,
                platform: &asked.platform,
                base_url: &asked.base_url,
                push_grant: &asked.push_grant,
                owner: &asked.owner,
                provider: &asked.provider,
                modes: &asked.modes,
            },
            &|owner| can_receive(devices, owner),
            self.works.clock.now_ms(),
        );
        match registered {
            Ok(changed) => self.keep_push(&changed).map(Ok),
            Err(why) => Ok(Err(why)),
        }
    }

    fn push_disable(
        &mut self,
        token: &str,
        owner: &str,
        only_owner: Option<&str>,
    ) -> Result<Result<usize, devices::Refused>, Unavailable> {
        match self.push.disable(token, owner, only_owner, self.now_ms()) {
            Ok(changed) => self.keep_push(&changed).map(|()| Ok(changed.len())),
            Err(why) => Ok(Err(why)),
        }
    }

    fn pushed(&mut self, token: &str, outcome: Pushed) {
        let now = self.now_ms();
        let changed = match outcome {
            Pushed::Sent => self.push.pushed(token, now),
            Pushed::Dead => self.push.dead(token, now),
            Pushed::GrantRevoked => self.push.grant_revoked(token),
        };
        if let Some(row) = changed
            && let Err(why) = self.keep_push(&[row])
        {
            eprintln!(
                "clarp-host: what became of a push could not be kept: {}",
                why.0
            );
        }
    }

    fn tell_roster(&self, kind: &str, session: &str) {
        self.works
            .hub
            .publish(wire::roster(kind, session), self.now_ms());
    }

    fn taken(&self, session: &str) -> bool {
        let held = self.held.iter().any(|name| name.session == session);
        held || self
            .members
            .iter()
            .any(|member| member.record.session == session)
    }

    /// The agent that has this name, when it is not `except`.
    /// Every persona as the pool reads it, and every member of the fleet as a wearer: what
    /// a contact is chosen against, as they stand in this turn.
    fn personas_listed(&self) -> Vec<serde_json::Value> {
        let [named, cartesia, gemini] = self.works.telling.owner_tables();
        self.book.listed(&Owner {
            roster: &named,
            cartesia: &cartesia,
            gemini: &gemini,
        })
    }

    /// The contact of a launch that asked for one (`agent_spec`, `NeedsContact`): the first
    /// free contact of its tool's pool, or one minted for it, which is written now.
    fn contact_of_launch(
        &mut self,
        backend: &str,
        contact: &ContactAsked,
        catalogue: &VoiceCatalogue,
    ) -> Result<String, NotMade> {
        let pooled = {
            let personas = self.personas_listed();
            let contacts = crate::contacts::contacts_of(&personas);
            let wearers = self.wearers();
            let pool = clarp_kernel::contacts::pool(&contacts, &wearers, backend, "");
            pool.first().map(|name| (*name).to_string())
        };
        if !contact.mint
            && let Some(name) = pooled
        {
            return Ok(name);
        }
        match self.mint_contact(backend, catalogue) {
            Ok(Some(name)) => Ok(name),
            Ok(None) => Err(Told::new(409, "contact_pool_empty", "contact_pool_empty").into()),
            Err(why) => Err(NotMade::Refused(why.0)),
        }
    }

    /// The contact `POST /agent-assign` gives the member at `at` with `auto`
    /// (`contact_assignment.assign_contact`): `None` where none could be minted.
    fn contact_of_assignment(
        &mut self,
        at: usize,
        fetched: &VoiceCatalogue,
    ) -> Result<Option<String>, Unavailable> {
        let record = self.members[at].record.clone();
        let decided = {
            let personas = self.personas_listed();
            let contacts = crate::contacts::contacts_of(&personas);
            let wearers = self.wearers();
            let agent = clarp_kernel::contacts::Assignee {
                agent_id: &record.agent_id,
                persona: &record.persona,
                backend: &record.backend,
                janitor: record.is_janitor,
            };
            clarp_kernel::contacts::assign("auto", None, Some(agent), &contacts, &wearers)
        };
        match decided {
            Ok(clarp_kernel::contacts::Assigned::Take(name)) => Ok(Some(name)),
            Ok(clarp_kernel::contacts::Assigned::Mint) => {
                self.mint_contact(&record.backend, fetched)
            }
            _ => Ok(None),
        }
    }

    /// A contact minted for an agent of a tool (`contact_minting.mint_contact`): a persona of
    /// the owner's with its tier's first free name, a voice nobody speaks with and the tier's
    /// way of speaking for that name, written now, without telling the apps the personas
    /// changed. Its name; `None` where none was made.
    fn mint_contact(
        &mut self,
        backend: &str,
        fetched: &VoiceCatalogue,
    ) -> Result<Option<String>, Unavailable> {
        let personas = self.personas_listed();
        let existing: Vec<String> = personas
            .iter()
            .filter_map(|persona| persona["name"].as_str().map(str::to_string))
            .collect();
        // The names of agents the Host keeps and does not run are taken too.
        let occupied: Vec<String> = self
            .members
            .iter()
            .map(|member| member.record.persona.clone())
            .chain(self.held.iter().map(|kept| kept.persona.clone()))
            .collect();
        let Some((name, tier)) = clarp_kernel::contacts::given_name(backend, &occupied, &existing)
        else {
            return Ok(None);
        };
        let said = PersonaSaid {
            personality: clarp_kernel::contacts::personality(&name, tier).to_string(),
            name,
            ..PersonaSaid::default()
        };
        let made = self.persona_write(None, &said, fetched, false)?;
        Ok(made
            .ok()
            .and_then(|persona| persona["name"].as_str().map(str::to_string)))
    }

    /// Who wears which contact, as the pool reads it: every member, and every agent the Host
    /// keeps and does not run, whose contact is not given to another.
    fn wearers(&self) -> Vec<clarp_kernel::contacts::Wearer<'_>> {
        let mut wearers =
            crate::contacts::wearers_of(self.members.iter().map(|member| &member.record));
        wearers.extend(self.held.iter().map(|kept| clarp_kernel::contacts::Wearer {
            agent_id: &kept.agent_id,
            persona: &kept.persona,
            voice_id: &kept.voice_id,
        }));
        wearers
    }

    /// Who has this name, when it is not `except`: its name and its session. An agent the Host
    /// keeps and does not run has its name too.
    fn owner_of_name(&self, name: &str, except: &str) -> Option<crate::kept::Name> {
        let running = self.members.iter().map(|member| &member.record);
        let mut running = running
            .filter(|record| record.agent_id != except && roster::same_name(&record.persona, name));
        let named = |record: &AgentRecord| crate::kept::Name {
            agent_id: record.agent_id.clone(),
            session: record.session.clone(),
            persona: record.persona.clone(),
            voice_id: record.voice_id.clone(),
        };
        running.next().map(named).or_else(|| {
            let held = self.held.iter();
            let mut held = held.filter(|kept| roster::same_name(&kept.persona, name));
            held.next().cloned()
        })
    }

    /// An agent started again in place, by the Python Host's rules for it
    /// (`policies/agent_spec.py`, with `replace_sid`): it has to have a name, be an agent
    /// of this Host and no janitor; what the request does not say stays as the agent has
    /// it; its name must be no other agent's. A tool this Host has not is refused, as for a
    /// new agent. With another tool the model it had is the old tool's and is not kept,
    /// and neither is an effort the new tool has not.
    fn start_again(
        &mut self,
        replaces: &str,
        request: CreateAgentRequest,
        dry: bool,
    ) -> Result<AgentRecord, NotMade> {
        let persona = request.name.trim().to_string();
        if persona.is_empty() {
            return Err(Told::new(400, "name required", "name required").into());
        }
        let by_session = |member: &Arc<Member>| member.record.session == replaces;
        let Some(at) = self.members.iter().position(by_session) else {
            let why = "no such agent to replace";
            return Err(Told::new(404, why, why).into());
        };
        let before = self.members[at].record.clone();
        self.members[at]
            .agent
            .accepting_work()
            .map_err(|why| Told::new(503, agent::REMOVING_CODE, why.0))?;
        if before.is_janitor {
            let why = "Janitors are managed from their maintenance configuration";
            return Err(Told::new(409, "janitor_managed", why).into());
        }
        let said = |said: &Option<String>| said.as_deref().map_or("", strip).to_string();
        let cwd = match said(&request.cwd) {
            asked if asked.is_empty() => before.cwd.clone(),
            asked => {
                let default_cwd = &self.works.config.default_cwd;
                let cwd = crate::config::working_dir(&asked, &crate::config::home(), default_cwd);
                cwd.display().to_string()
            }
        };
        let backend = match said(&request.backend) {
            asked if asked.is_empty() => before.backend.clone(),
            asked => asked,
        };
        if clarp_backend::by_id(&backend).is_none() {
            return Err(format!("this Host has no backend named `{backend}`").into());
        }
        if let Some(owner) = self.owner_of_name(&persona, &before.agent_id) {
            return Err(NotMade::Occupied {
                owner: owner.persona.clone(),
                session: owner.session.clone(),
            });
        }
        let another_tool = backend != before.backend;
        let model = match &request.model {
            Some(model) => model.trim().to_string(),
            None if another_tool => String::new(),
            None => before.model.clone(),
        };
        let efforts = clarp_backend::efforts(&backend);
        let effort = match &request.effort {
            Some(asked) => match roster::effort(asked, efforts) {
                Some(effort) => effort,
                None => {
                    let why = "invalid effort for backend";
                    return Err(Told::new(400, why, why).into());
                }
            },
            None if efforts.contains(&before.effort.as_str()) => before.effort.clone(),
            None => String::new(),
        };
        let or_kept = |asked: String, kept: &str| {
            if asked.is_empty() {
                kept.to_string()
            } else {
                asked
            }
        };
        let record = AgentRecord {
            persona,
            backend,
            cwd,
            model,
            effort,
            voice_id: or_kept(said(&request.voice_id), &before.voice_id),
            personality: match &request.personality {
                Some(asked) => python::first(strip(asked), PERSONALITY_MOST).to_string(),
                None => before.personality.clone(),
            },
            avatar_symbol: match &request.avatar_symbol {
                Some(asked) => python::first(strip(asked), personas::SYMBOL_MOST).to_string(),
                None => before.avatar_symbol.clone(),
            },
            // A conversation of a new name: an app that kept the old one drops it.
            conversation_id: ids::uuid(),
            ..before.clone()
        };
        if dry {
            return Ok(record);
        }
        let (took, _taken) = oneshot::channel();
        self.put_launch(&record)?;
        self.replaced(at, record.clone(), took);
        self.works
            .hub
            .publish(wire::roster_started_again(&record), self.now_ms());
        Ok(record)
    }

    fn create(
        &mut self,
        mut request: CreateAgentRequest,
        forked: Option<String>,
        contact: Option<ContactAsked>,
        catalogue: &VoiceCatalogue,
    ) -> Result<AgentRecord, NotMade> {
        if let Some(parent) = request
            .parent
            .as_deref()
            .map(str::trim)
            .filter(|s| !s.is_empty())
        {
            if let Some(at) = self.place_of(parent) {
                self.members[at]
                    .agent
                    .accepting_work()
                    .map_err(|why| Told::new(503, agent::REMOVING_CODE, why.0))?;
            }
            match self.has_delegated_ancestor(parent) {
                Ok(true) => {
                    return Err(Told::new(
                        409,
                        "a bookkeeping delegate does not start helpers or forks",
                        "a bookkeeping delegate does not start helpers or forks",
                    )
                    .into());
                }
                Ok(false) => {}
                Err(why) => {
                    let code = "delegation_unavailable";
                    return Err(Told::new(why.status().as_u16(), code, why.message()).into());
                }
            }
        }
        // The contact asked for in place of a name of its own: its name, and no session of
        // its own (`agent_spec`: the session asked for is dropped).
        if let Some(contact) = contact {
            let backend = request
                .backend
                .as_deref()
                .map(str::trim)
                .filter(|backend| !backend.is_empty())
                .unwrap_or("claude")
                .to_string();
            request.name = self.contact_of_launch(&backend, &contact, catalogue)?;
            request.session = None;
        }
        let was_forked = forked.is_some();
        let persona = request.name.trim().to_string();
        if persona.is_empty() {
            return Err(Told::new(400, "name required", "name required").into());
        }
        // Where it works: what was asked for, or this Host's own directory for agents.
        let asked = request.cwd.as_deref().unwrap_or("");
        let default_cwd = &self.works.config.default_cwd;
        let cwd = crate::config::working_dir(asked, &crate::config::home(), default_cwd);
        let cwd = cwd.display().to_string();
        // One agent has a name at a time: who is written to by name must be one agent.
        if let Some(owner) = self.owner_of_name(&persona, "") {
            return Err(NotMade::Occupied {
                owner: owner.persona.clone(),
                session: owner.session.clone(),
            });
        }
        // The session name that was asked for is the agent's when no agent has it.
        let asked: String = request
            .session
            .as_deref()
            .unwrap_or("")
            .trim()
            .chars()
            .filter(|c| c.is_ascii_alphanumeric() || "._-".contains(*c))
            .collect();
        let session = if asked.is_empty() || self.taken(&asked) {
            let base = ids::slug(&persona);
            let mut session = base.clone();
            while self.taken(&session) {
                session = format!("{base}-{}", ids::hex(2));
            }
            session
        } else {
            asked
        };
        let backend = request
            .backend
            .as_deref()
            .and_then(non_empty)
            .unwrap_or_else(|| "claude".to_string());
        if clarp_backend::by_id(&backend).is_none() {
            return Err(format!("this Host has no backend named `{backend}`").into());
        }
        let said_effort = request.effort.as_deref().unwrap_or("");
        let Some(effort) = roster::effort(said_effort, clarp_backend::efforts(&backend)) else {
            let why = "invalid effort for backend";
            return Err(Told::new(400, why, why).into());
        };
        // Who makes it, and as what.
        let Some(role) = helpers::parse_role(request.role.as_deref().unwrap_or("")) else {
            let why = "role must be 'agent' or 'helper'";
            return Err(Told::new(400, "invalid_role", why).into());
        };
        let said_parent = request.parent.as_deref().unwrap_or("").trim();
        let parent = match (said_parent, self.place_of(said_parent)) {
            ("", _) => None,
            (_, Some(at)) => Some(self.members[at].record.agent_id.clone()),
            (_, None) => {
                let why = format!("No live agent matches parent '{said_parent}'.");
                return Err(Told::new(404, "parent_not_found", why).into());
            }
        };
        if role == "helper" && parent.is_none() {
            let why = "A helper needs a parent session.";
            return Err(Told::new(400, "helper_requires_parent", why).into());
        }
        // Who it is made as: the persona of its name, built in or the owner's own. What the
        // request does not say of the agent is the persona's, and is written down with the
        // agent, so that it keeps it under another name.
        let file = self.voice_pool_settings();
        let [named, cartesia, gemini] = crate::owner::personas(&file);
        let owner = Owner {
            roster: &named,
            cartesia: &cartesia,
            gemini: &gemini,
        };
        let definition = self.book.named(&owner, &persona);
        let of_persona = |key: &str| {
            let persona = definition.as_ref();
            persona
                .and_then(|persona| persona[key].as_str())
                .unwrap_or("")
        };
        let asked = |said: &Option<String>| said.as_deref().map_or("", strip).to_string();
        let or_persona = |asked: String, key: &str, most: usize| {
            let chosen = if asked.is_empty() {
                strip(of_persona(key))
            } else {
                &asked
            };
            python::first(chosen, most).to_string()
        };
        let personality = or_persona(asked(&request.personality), "personality", PERSONALITY_MOST);
        let avatar_symbol = or_persona(
            asked(&request.avatar_symbol),
            "avatar_symbol",
            personas::SYMBOL_MOST,
        );
        let voice_id = match asked(&request.voice_id) {
            voice if voice.is_empty() => of_persona("voice_id").to_string(),
            voice => voice,
        };
        let voice_id = if role == "agent" && !was_forked {
            self.assigned_voice(&file, &owner, &persona, &voice_id, catalogue)
        } else {
            voice_id
        };
        // Its picture: the one it is given, else the one its persona has of its own.
        let given = match personas::picture(&asked(&request.avatar_base64)) {
            Ok(given) => given,
            Err(why) => {
                let why = match why {
                    personas::AVATAR_TOO_LARGE => "avatar is too large",
                    other => other,
                };
                return Err(Told::new(400, "invalid avatar", why).into());
            }
        };
        let avatar = match &given {
            Some((_, digest)) => Some(digest.clone()),
            None => definition
                .as_ref()
                .and_then(|persona| persona["id"].as_str())
                .and_then(|id| self.book.picture_of(&owner, id)),
        };
        let record = AgentRecord {
            agent_id: ids::hex(8),
            session,
            persona,
            backend,
            cwd,
            model: request.model.unwrap_or_default().trim().to_string(),
            effort,
            conversation_id: forked.unwrap_or_else(ids::uuid),
            created_at_ms: self.now_ms(),
            parent_agent_id: parent,
            role: if role == "helper" {
                role.to_string()
            } else {
                String::new()
            },
            helper_state: helpers::initial_state(role).map(str::to_string),
            voice_id,
            personality,
            avatar_symbol,
            avatar,
            ..AgentRecord::default()
        };
        // Refused before anything is written: an agent whose backend this Host lacks.
        let setup = setup(&self.works, &self.delegation_desk, &record)?;
        // Its picture is kept before the agent that names it.
        if let Some((bytes, digest)) = &given {
            let kept = self.works.blobs.put(&picture_name(digest), bytes);
            kept.map_err(|error| error.to_string())?;
        }
        self.put_launch(&record)?;
        self.members.push(Arc::new(Member {
            record: record.clone(),
            agent: agent::spawn(setup),
        }));
        self.publish();
        // The apps are told when the fleet has the agent: that it is there, and then what
        // an agent that has never run is doing, in the Python Host's order. That its
        // conversation changed is not said: nothing is in it, and the Python Host says so
        // when the first thing is written to it, as the agent's own thread does here.
        let (hub, now, new) = (&self.works.hub, self.now_ms(), agent::never_ran());
        let made = if was_forked {
            wire::roster_forked(&record)
        } else {
            wire::roster_created(&record)
        };
        hub.publish(made, now);
        hub.publish(wire::state(&record, &new, &record.backend, None), now);
        hub.publish(wire::activity(&record, &new), now);
        Ok(record)
    }

    /// Makes a janitor as one. An id or a session name another member has is not given
    /// twice, and neither is a tool this Host has not.
    fn make_janitor(&mut self, seed: JanitorSeed) -> Result<AgentRecord, NotMade> {
        // One of the Host's own comes with its id, and is installed before anybody listens.
        // Any other is made because a request asked for it.
        let by_request = seed.agent_id.is_none();
        let agent_id = seed.agent_id.unwrap_or_else(|| ids::hex(8));
        let has_id = |member: &Arc<Member>| member.record.agent_id == agent_id;
        if seed.session.is_empty() || self.taken(&seed.session) || self.members.iter().any(has_id) {
            let why = "The janitor's identity is already owned";
            return Err(Told::new(409, "creation_conflict", why).into());
        }
        let default_cwd = &self.works.config.default_cwd;
        let cwd = crate::config::working_dir(&seed.cwd, &crate::config::home(), default_cwd);
        let record = AgentRecord {
            agent_id,
            session: seed.session,
            persona: seed.persona,
            backend: seed.backend,
            cwd: cwd.display().to_string(),
            model: seed.model,
            effort: seed.effort,
            conversation_id: ids::uuid(),
            created_at_ms: self.now_ms(),
            is_janitor: true,
            role: JANITOR.to_string(),
            // No voice of its own, in the Python Host's two words for it: one for a janitor
            // the Host installs, one for a janitor a request makes.
            voice_id: if by_request { "" } else { "{}" }.to_string(),
            ..AgentRecord::default()
        };
        let setup = setup(&self.works, &self.delegation_desk, &record)?;
        if by_request {
            self.put_launch(&record)?;
        } else {
            self.put(&record)?;
        }
        self.members.push(Arc::new(Member {
            record: record.clone(),
            agent: agent::spawn(setup),
        }));
        self.publish();
        if by_request {
            // The apps are told that it is there as what it is so far, as the Python Host
            // tells them. That a chat is ready nobody is told: a janitor is not spoken to,
            // and the janitors' own word follows when it has its job.
            let (hub, now, new) = (&self.works.hub, self.now_ms(), agent::never_ran());
            hub.publish(wire::state(&record, &new, &record.backend, None), now);
            hub.publish(wire::activity(&record, &new), now);
        }
        Ok(record)
    }

    /// `of_any`: the change is the janitors' own, and may be of a janitor.
    fn amend(
        &mut self,
        key: &str,
        change: Amend,
        of_any: bool,
    ) -> Result<Result<Amended, NotAmended>, Unavailable> {
        let found = if of_any {
            self.place_of_any(key)
        } else {
            self.place_of(key)
        };
        let Some(at) = found else {
            return Ok(Err(NotAmended::Unknown));
        };
        self.members[at].agent.accepting_work()?;
        // A contact chosen by the rule is chosen here, in the turn that gives it.
        let change = match change {
            Amend::ContactAuto(fetched) => match self.contact_of_assignment(at, &fetched)? {
                Some(name) => Amend::Contact(name, fetched),
                None => return Ok(Err(NotAmended::NoContact)),
            },
            other => other,
        };
        let before = &self.members[at].record;
        let mut record = before.clone();
        let assigned = matches!(change, Amend::Contact(..) | Amend::ContactAuto(_));
        match change {
            Amend::Mute(muted) => record.muted = muted,
            Amend::Heartbeat(on) => record.heartbeat_enabled = on,
            Amend::Rename(name) => {
                if let Some(owner) = self.owner_of_name(&name, &record.agent_id) {
                    return Ok(Err(NotAmended::Occupied {
                        owner: owner.persona.clone(),
                        session: owner.session.clone(),
                    }));
                }
                if record.persona != name {
                    let file = self.voice_pool_settings();
                    let [roster, _, _] = crate::owner::personas(&file);
                    let host = clarp_kernel::voice_profile::HostView {
                        voices: crate::owner::voices(&file),
                        ..Default::default()
                    };
                    let pool = clarp_kernel::voice_pool::Pool {
                        host: &host,
                        configured_cartesia: &[],
                        catalogue: &VoiceCatalogue::default(),
                        roster: &roster,
                    };
                    record.voice_id = pool.pinned_before_rename(&record.persona, &record.voice_id);
                }
                record.persona = name;
            }
            // Put away again, an agent is put away now.
            Amend::Archive(archived) => record.archived_at_ms = archived.then(|| self.now_ms()),
            Amend::Tune { model, effort } => {
                if let Some(model) = model {
                    record.model = model;
                }
                if let Some(effort) = effort {
                    record.effort = effort;
                }
            }
            Amend::Tool(backend) => record.backend = backend,
            Amend::Voice { provider, voice } => {
                let occupied = self
                    .members
                    .iter()
                    .filter(|other| {
                        other.record.agent_id != record.agent_id && !other.record.is_janitor
                    })
                    .find(|other| {
                        clarp_kernel::said::resolve_voice(Some(&other.record.voice_id), &provider)
                            .as_deref()
                            == Some(voice.as_str())
                    });
                if let Some(other) = occupied {
                    return Ok(Err(NotAmended::Occupied {
                        owner: other.record.persona.clone(),
                        session: other.record.session.clone(),
                    }));
                }
                record.voice_id =
                    clarp_kernel::said::merge_voice(Some(&record.voice_id), &provider, &voice);
            }

            Amend::Narration(level) => record.narration = level,
            // A janitor is that and nothing else: no helper of anybody, and not woken by
            // the heartbeat. Let go, it is an agent like any other again.
            Amend::Janitor(is) => {
                record.is_janitor = is;
                record.role = if is {
                    JANITOR.to_string()
                } else {
                    String::new()
                };
                record.heartbeat_enabled = record.heartbeat_enabled && !is;
            }
            Amend::Portraits { kept, worn } => {
                record.portraits = kept;
                if worn.is_some() {
                    record.avatar = worn;
                }
            }
            // Chosen above.
            Amend::ContactAuto(_) => {}
            Amend::Contact(name, catalogue) => {
                if let Some(owner) = self.owner_of_name(&name, &record.agent_id) {
                    return Ok(Err(NotAmended::Occupied {
                        owner: owner.persona.clone(),
                        session: owner.session.clone(),
                    }));
                }
                let file = self.voice_pool_settings();
                let [named, cartesia, gemini] = crate::owner::personas(&file);
                let owner = Owner {
                    roster: &named,
                    cartesia: &cartesia,
                    gemini: &gemini,
                };
                let Some(persona) = self.book.named(&owner, &name) else {
                    return Ok(Err(NotAmended::Unknown));
                };
                let of_persona = |key: &str| persona[key].as_str().unwrap_or("").to_string();
                record.persona = of_persona("name");
                let voice = of_persona("voice_id");
                let voice = if voice.is_empty() { "{}" } else { &voice };
                record.voice_id =
                    self.assigned_voice(&file, &owner, &record.persona, voice, &catalogue);
                record.personality = of_persona("personality");
                record.avatar_symbol = of_persona("avatar_symbol");
                record.avatar = persona["id"]
                    .as_str()
                    .and_then(|id| self.book.picture_of(&owner, id));
            }
        }
        let (took, taken) = oneshot::channel();
        if record == *before {
            let _ = took.send(());
            return Ok(Ok(Amended { record, taken }));
        }
        let renamed = record.persona != before.persona && !assigned;
        let put_away = record.archived_at_ms.is_some();
        self.replace(at, record.clone(), took)?;
        if renamed {
            self.works.hub.publish(
                wire::roster_renamed(&record.session, &record.persona),
                self.now_ms(),
            );
        }
        if put_away {
            self.abandon_helpers_of(&record.agent_id)?;
        }
        Ok(Ok(Amended { record, taken }))
    }

    /// Where the agent a request names is in the list: by its session name, or by its id.
    /// A janitor is no agent a request means: what is done to an agent of the roster is
    /// not done to one ([`Self::place_of_any`] is for who means either).
    fn place_of(&self, key: &str) -> Option<usize> {
        self.place_of_any(key)
            .filter(|at| !self.members[*at].record.is_janitor)
    }

    /// Where a member of the fleet is in the list, an agent of the roster or a janitor.
    fn place_of_any(&self, key: &str) -> Option<usize> {
        if key.is_empty() {
            return None;
        }
        let by = |find: &dyn Fn(&AgentRecord) -> bool| {
            self.members.iter().position(|member| find(&member.record))
        };
        by(&|record| record.session == key).or_else(|| by(&|record| record.agent_id == key))
    }

    /// Keeps an agent as a change left it, and tells its own thread and everyone who reads
    /// the fleet. `took` is done when the agent's thread has the change.
    fn replace(
        &mut self,
        at: usize,
        record: AgentRecord,
        took: oneshot::Sender<()>,
    ) -> Result<(), Unavailable> {
        if record.archived_at_ms.is_some() && self.members[at].record.archived_at_ms.is_none() {
            self.stop_for_roster(&record, "agent archived", None)?;
        } else {
            self.put(&record).map_err(Unavailable)?;
        }
        self.replaced(at, record, took);
        Ok(())
    }

    fn replaced(&mut self, at: usize, record: AgentRecord, took: oneshot::Sender<()>) {
        // The agent's thread is told in the order the fleet made the changes.
        let agent = self.members[at].agent.clone();
        agent.amended(record.clone(), took);
        self.members[at] = Arc::new(Member { record, agent });
        self.publish();
    }

    /// Moves a helper where an event takes it ([`helpers::next_state`]). Answers the state
    /// it then has, or nothing when the event changes nothing for it. `announce` tells
    /// the apps to read the list again.
    fn helper_event(
        &mut self,
        agent_id: &str,
        event: &str,
        announce: bool,
    ) -> Result<Option<String>, Unavailable> {
        let Some(at) = self
            .members
            .iter()
            .position(|member| member.record.agent_id == agent_id)
        else {
            return Ok(None);
        };
        // A helper awaiting removal keeps its own tombstone, including when its parent goes.
        if self.members[at].agent.accepting_work().is_err() {
            return Ok(None);
        }
        let before = &self.members[at].record;
        let current = before.helper_state.as_deref().unwrap_or("");
        let Some(moved) = helpers::next_state(current, event) else {
            return Ok(None);
        };
        let mut record = before.clone();
        record.helper_state = Some(moved.state.to_string());
        record.helper_completed_at_ms = moved.completed.then(|| self.now_ms());
        let session = record.session.clone();
        // Nobody waits for the helper's own thread to have it.
        let (took, _taken) = oneshot::channel();
        self.replace(at, record, took)?;
        if announce {
            self.tell_roster("helper-state", &session);
        }
        Ok(Some(moved.state.to_string()))
    }

    /// The helpers an agent made that are not finished lose it: it was removed or put
    /// away. Nothing else follows from that; a helper is never removed with its maker.
    fn abandon_helpers_of(&mut self, agent_id: &str) -> Result<(), Unavailable> {
        let children: Vec<String> = self
            .members
            .iter()
            .filter(|member| member.record.parent_agent_id.as_deref() == Some(agent_id))
            .map(|member| member.record.agent_id.clone())
            .collect();
        for child in children {
            self.helper_event(&child, "parent_gone", false)?;
        }
        Ok(())
    }

    fn noted(&mut self, sender: &str, target: &str) -> Result<(), Unavailable> {
        if sender.is_empty() || target.is_empty() || sender == target {
            return Ok(());
        }
        let parent_of = |agent_id: &str| {
            self.members
                .iter()
                .find(|member| member.record.agent_id == agent_id)
                .and_then(|member| member.record.parent_agent_id.clone())
        };
        let (senders, targets) = (parent_of(sender), parent_of(target));
        if senders.as_deref() == Some(target) {
            self.helper_event(sender, "reported", true)?;
        } else if targets.as_deref() == Some(sender) {
            self.helper_event(target, "parent_prompted", true)?;
        }
        Ok(())
    }

    fn mark(
        &mut self,
        key: &str,
        state: &str,
        by: &str,
    ) -> Result<Result<String, Told>, Unavailable> {
        let Some(event) = helpers::marked(state) else {
            let why = "state must be done, failed or running";
            return Ok(Err(Told::new(400, "invalid_state", why)));
        };
        let no_agent =
            |named: &str| Told::new(404, "agent_not_found", format!("No live agent: {named}"));
        let Some(at) = self.place_of(key) else {
            return Ok(Err(no_agent(key)));
        };
        self.members[at].agent.accepting_work()?;
        let helper = self.members[at].record.clone();
        if !helper.is_helper() {
            let why = format!("{} is not a helper", helper.session);
            return Ok(Err(Told::new(409, "not_a_helper", why)));
        }
        if !by.is_empty() {
            let Some(marker) = self.place_of(by) else {
                return Ok(Err(no_agent(by)));
            };
            if Some(&self.members[marker].record.agent_id) != helper.parent_agent_id.as_ref() {
                let why = "Only the helper's parent or the user may mark it";
                return Ok(Err(Told::new(403, "not_parent", why)));
            }
        }
        if helper.helper_state.as_deref() == Some(state) {
            return Ok(Ok(state.to_string()));
        }
        match self.helper_event(&helper.agent_id, event, true)? {
            Some(new) => Ok(Ok(new)),
            None => {
                let now = helper.helper_state.as_deref().unwrap_or("None");
                let why = format!("{} is {now}; cannot mark {state}", helper.session);
                Ok(Err(Told::new(409, "illegal_transition", why)))
            }
        }
    }

    /// What an agent is doing right now, as the list of agents counts it.
    fn doing(&self, member: &Member) -> helpers::Doing {
        doing(&self.members, member)
    }
}

/// What an agent is doing right now, among the agents of the roster: whether a turn runs or
/// waits, the background processes it left running, its helpers at work, and when anything
/// of it last changed.
pub fn doing(members: &[Arc<Member>], member: &Member) -> helpers::Doing {
    {
        let view = member.agent.view();
        let children: Vec<&AgentRecord> = members
            .iter()
            .map(|child| &child.record)
            .filter(|child| child.parent_agent_id.as_ref() == Some(&member.record.agent_id))
            .collect();
        let helper_sessions: Vec<&str> = children
            .iter()
            .filter(|child| child.is_helper())
            .map(|child| child.session.as_str())
            .collect();
        let processes = view
            .jobs
            .iter()
            .filter(|job| job.status.is_active())
            .filter(|job| {
                !view.mirrored_provider(&job.id)
                    && wire::mirrored_helper(job, &helper_sessions).is_none()
            });
        let last_job = view.jobs.iter().map(|job| job.updated_at_ms).max();
        let last_turn = view.status.last.as_ref().map(|last| last.ended_at_ms);
        helpers::Doing {
            busy: view.status.live.is_some() || !view.status.waiting.is_empty(),
            active_jobs: processes.count(),
            running_children: children
                .iter()
                .filter(|child| child.helper_state.as_deref() == Some("running"))
                .count(),
            idle_since_ms: [Some(member.record.created_at_ms), last_job, last_turn]
                .into_iter()
                .flatten()
                .max()
                .unwrap_or(0),
        }
    }
}

impl Fleet {
    /// Now and then: a helper that is working and has sat idle too long counts as having
    /// reported, and a helper that has been done for long enough is put away.
    fn look_after_helpers(&mut self) -> Result<(), Unavailable> {
        let now = self.now_ms();
        let idle_after = self.works.config.helper_idle_after_ms;
        let quiet: Vec<String> = self
            .members
            .iter()
            .filter(|member| {
                let state = member.record.helper_state.as_deref();
                state == Some("running")
                    && helpers::idle_due(state, &self.doing(member), now, idle_after)
            })
            .map(|member| member.record.agent_id.clone())
            .collect();
        for agent_id in quiet {
            self.helper_event(&agent_id, "went_idle", true)?;
        }
        let kept: Vec<helpers::Kept<'_>> = self
            .members
            .iter()
            .map(|member| helpers::Kept {
                agent_id: &member.record.agent_id,
                role: member.record.role(),
                helper_state: member.record.helper_state.as_deref().unwrap_or(""),
                archived: member.record.archived_at_ms.is_some(),
                helper_completed_at: member.record.helper_completed_at_ms,
            })
            .collect();
        let grace = self.works.config.helper_archive_grace_ms;
        let done: Vec<String> = helpers::archive_due(&kept, now, grace)
            .into_iter()
            .map(str::to_string)
            .collect();
        for agent_id in done {
            if let Ok(Ok(amended)) = self.amend(&agent_id, Amend::Archive(true), false) {
                self.tell_roster("helper-state", &amended.record.session);
            }
        }
        Ok(())
    }

    fn adopt(&mut self, mut record: AgentRecord) -> Result<Arc<Member>, String> {
        if record.agent_id.trim().is_empty() || record.session.trim().is_empty() {
            return Err("an agent that arrives needs its id and its session name".to_string());
        }
        if self
            .members
            .iter()
            .any(|member| member.record.agent_id == record.agent_id)
        {
            return Err(format!(
                "this Host already has the agent {}",
                record.agent_id
            ));
        }
        if self.taken(&record.session) {
            return Err(format!(
                "this Host already has an agent called {}",
                record.session
            ));
        }
        if !Path::new(&record.cwd).is_dir() {
            return Err(format!("{} is not a directory on this machine", record.cwd));
        }
        if record.conversation_id.trim().is_empty() {
            record.conversation_id = ids::uuid();
        }
        record.deleted_at_ms = None;
        let setup = setup(&self.works, &self.delegation_desk, &record)?;
        self.put(&record)?;
        self.gone.remove(&record.agent_id);
        let member = Arc::new(Member {
            record: record.clone(),
            agent: agent::spawn(setup),
        });
        self.members.push(member.clone());
        self.publish();
        self.works
            .hub
            .publish(wire::roster_created(&record), self.now_ms());
        Ok(member)
    }

    fn remember_owner(&mut self, record: &AgentRecord) {
        self.delegation_desk.known_record(record);
        if !self
            .cwd_owners
            .get(&record.agent_id)
            .is_some_and(|(cwd, janitor)| cwd == &record.cwd && *janitor == record.is_janitor)
        {
            Arc::make_mut(&mut self.cwd_owners).insert(
                record.agent_id.clone(),
                (record.cwd.clone(), record.is_janitor),
            );
        }
    }

    /// The fleet is the single writer. A failed registration cannot count a launch.
    fn put_launch(&mut self, record: &AgentRecord) -> Result<(), String> {
        let Some(usage) = launches::launched(&self.launch_usage, &record.cwd, self.now_ms()) else {
            return self.put(record);
        };
        let bytes = serde_json::to_vec(&usage).map_err(|e| e.to_string())?;
        let agent = serde_json::to_vec(record).map_err(|e| e.to_string())?;
        self.store
            .put_together(&[
                (AGENTS.into(), record.agent_id.clone(), Some(agent)),
                (shelf(LAUNCH_USAGE), usage.path.clone(), Some(bytes)),
            ])
            .map_err(|e| e.to_string())?;
        Arc::make_mut(&mut self.launch_usage).insert(usage.path.clone(), usage);
        self.remember_owner(record);
        Ok(())
    }

    fn put(&mut self, record: &AgentRecord) -> Result<(), String> {
        let stored = serde_json::to_vec(record).map_err(|error| error.to_string())?;
        self.store
            .put(AGENTS, &record.agent_id, &stored)
            .map_err(|error| error.to_string())?;
        self.remember_owner(record);
        Ok(())
    }

    fn stored_agent(&self, id: &str) -> Result<Option<AgentRecord>, Unavailable> {
        self.store
            .get(AGENTS, id)
            .map_err(|e| Unavailable(e.to_string()))?
            .map(|b| serde_json::from_slice(&b).map_err(|e| Unavailable(e.to_string())))
            .transpose()
    }
    fn past_ledger(
        &self,
        plan: &str,
        after: i64,
        limit: usize,
    ) -> Result<(Vec<clarp_kernel::goals::ledger::Event>, bool), Unavailable> {
        let Some(record) = self
            .stored_agent(crate::goaltool::owner_of(plan))?
            .filter(|r| r.deleted_at_ms.is_some())
        else {
            return Ok((vec![], false));
        };
        if !record.ledger_retired {
            return Err(Unavailable("goal ledger retirement is incomplete".into()));
        }
        let log = self
            .works
            .engine
            .open(
                &self
                    .works
                    .config
                    .data_dir
                    .join("agents")
                    .join(record.agent_id),
                self.works.codecs.clone(),
            )
            .map_err(|e| Unavailable(e.to_string()))?;
        clarp_actor::read_goal_events(log.as_ref(), plan, after, limit)
            .map_err(|e| Unavailable(e.to_string()))
    }

    fn remove(&mut self, session: &str) -> Result<bool, Unavailable> {
        let of_the_roster =
            |member: &Arc<Member>| member.record.session == session && !member.record.is_janitor;
        let Some(at) = self.members.iter().position(of_the_roster) else {
            return Ok(false);
        };
        let mut record = self.members[at].record.clone();
        // An agent asked to close before is not stopped twice: it was deleted then.
        let asked_before = self.closing.get(&record.agent_id).copied();
        record.deleted_at_ms = Some(asked_before.unwrap_or_else(|| self.now_ms()));
        let agent = self.members[at].agent.clone();
        if asked_before.is_none() {
            self.stop_for_roster(&record, "agent deleted", Some(&agent))?;
            self.closing.insert(
                record.agent_id.clone(),
                record.deleted_at_ms.unwrap_or_default(),
            );
        }
        // Its thread is the one writer of what is its own: until it has closed, nothing of
        // that is touched, and the agent stays as it is. Its tombstone is kept.
        if !agent.close(agent::CLOSE_WITHIN) {
            return Err(Unavailable(format!(
                "The agent's thread did not answer within {} seconds, so it is not removed yet: ask again to finish removing it.",
                agent::CLOSE_WITHIN.as_secs()
            )));
        }
        self.closing.remove(&record.agent_id);
        self.gone.insert(
            record.agent_id.clone(),
            (record.persona.clone(), record.session.clone()),
        );
        self.members.remove(at);
        let retired = retire_goals(
            &self.works,
            &record,
            record.deleted_at_ms.unwrap_or_default(),
        );
        self.abandon_helpers_of(&record.agent_id)?;
        if self.focus.as_deref() == Some(record.agent_id.as_str()) {
            self.focus = None;
            // A focus that names no agent is left out when the Host starts again.
            if let Err(why) = self.store.delete(SETTINGS, FOCUS) {
                self.works.log.unkept("focus", &why.to_string());
            }
        }
        self.publish();
        self.tell_roster("deleted", session);
        // And that it stopped, because it was removed.
        let now = self.now_ms();
        for told in wire::removed(&record, now) {
            self.works.hub.publish(told, now);
        }
        if let Err(why) = retired.and_then(|()| {
            record.ledger_retired = true;
            self.put(&record)
        }) {
            // Removal was already published. The durable tombstone keeps past
            // pages closed until startup finishes the retirement.
            self.works.log.unkept("goal ledger retirement", &why);
        }
        Ok(true)
    }

    fn select(
        &mut self,
        session: &str,
        announce: bool,
    ) -> Result<Option<AgentRecord>, Unavailable> {
        let Some(record) = self
            .members
            .iter()
            .find(|member| member.record.session == session && !member.record.is_janitor)
            .map(|member| member.record.clone())
        else {
            return Ok(None);
        };
        let already = self.focus.as_deref() == Some(record.agent_id.as_str());
        if !already {
            self.store
                .put(SETTINGS, FOCUS, record.agent_id.as_bytes())
                .map_err(|error| Unavailable(error.to_string()))?;
            self.focus = Some(record.agent_id.clone());
            self.publish();
        }
        if announce {
            self.works.hub.publish(wire::focus(&record), self.now_ms());
        }
        Ok(Some(record))
    }

    fn issue(
        &mut self,
        device_name: &str,
        scope: Scope,
        ttl_seconds: i64,
    ) -> Result<Issued, String> {
        let now = self.now_ms();
        let (issued, pending) = pairing::issue(device_name, scope, ttl_seconds, now)?;
        // Codes that ran out are swept when the next one is made.
        for (digest, stored) in self.store.list(CODES).map_err(|error| error.to_string())? {
            let expired = serde_json::from_slice::<PendingCode>(&stored)
                .map_or(true, |code| code.expires_at < now);
            if expired {
                // One that stays is swept again when the next code is made.
                if let Err(why) = self.store.delete(CODES, &digest) {
                    self.works.log.unkept("pairing code", &why.to_string());
                }
            }
        }
        let stored = serde_json::to_vec(&pending).map_err(|error| error.to_string())?;
        self.store
            .put(CODES, &pairing::digest(&issued.code), &stored)
            .map_err(|error| error.to_string())?;
        Ok(issued)
    }

    fn exchange(&mut self, code: &str, device_name: &str) -> Result<Paired, String> {
        let code = code.trim();
        if !pairing::looks_like_a_code(code) {
            return Err("invalid pairing code".to_string());
        }
        let digest = pairing::digest(code);
        let pending = self
            .store
            .get(CODES, &digest)
            .map_err(|error| error.to_string())?
            .and_then(|stored| serde_json::from_slice::<PendingCode>(&stored).ok())
            .ok_or("pairing code is invalid or already used")?;
        let (paired, device) = pairing::redeem(&pending, device_name, self.now_ms())?;
        let stored = serde_json::to_vec(&device).map_err(|error| error.to_string())?;
        // The device first, then the code is gone: a crash between the two leaves a device
        // nobody has the token of, never a code that works twice.
        self.store
            .put(DEVICES, &device.device_id, &stored)
            .map_err(|error| error.to_string())?;
        self.store
            .delete(CODES, &digest)
            .map_err(|error| error.to_string())?;
        self.devices
            .insert(device.token_hash.clone(), Arc::new(device));
        self.publish();
        Ok(paired)
    }

    fn revoke(&mut self, device_id: &str) -> Result<bool, Unavailable> {
        let Some(device) = self
            .devices
            .values()
            .find(|device| device.device_id == device_id)
            .cloned()
        else {
            return Ok(false);
        };
        let mut revoked = (*device).clone();
        revoked.revoked_at = Some(self.now_ms());
        let stored =
            serde_json::to_vec(&revoked).map_err(|error| Unavailable(error.to_string()))?;
        self.store
            .put(DEVICES, device_id, &stored)
            .map_err(|error| Unavailable(error.to_string()))?;
        self.devices.remove(&device.token_hash);
        self.publish();
        // Its push tokens stop at once: a revoked phone is not shown what agents say. The rows
        // keep their owner, and come back only when a paired device registers the token again.
        if let Ok(stopped) = self.push.disable("", device_id, None, self.now_ms()) {
            self.keep_push(&stopped)?;
        }
        Ok(true)
    }
}

#[cfg(test)]
mod opening_reads {
    use super::*;
    use clarp_ports::kv::{StoreError, Write};
    use std::cell::Cell;

    struct Observed {
        reads: Cell<usize>,
        scans: Cell<usize>,
    }
    impl KeyValue for Observed {
        fn get(&self, table: &str, key: &str) -> Result<Option<Vec<u8>>, StoreError> {
            self.reads.set(self.reads.get() + 1);
            assert_eq!(table, "kept.opening_by_turn");
            Ok((key == "agent:4").then(|| vec![4]))
        }
        fn list(&self, _: &str) -> Result<Vec<(String, Vec<u8>)>, StoreError> {
            self.scans.set(self.scans.get() + 1);
            Ok(vec![("previous".into(), vec![1])])
        }
        fn put(&mut self, _: &str, _: &str, _: &[u8]) -> Result<(), StoreError> {
            unreachable!()
        }
        fn put_together(&mut self, _: &[Write]) -> Result<(), StoreError> {
            unreachable!()
        }
        fn delete(&mut self, _: &str, _: &str) -> Result<bool, StoreError> {
            unreachable!()
        }
    }

    #[test]
    fn ordinary_audio_reads_at_most_one_key_and_walks_no_openings() {
        let port = Observed {
            reads: Cell::new(0),
            scans: Cell::new(0),
        };
        for (agent, turn, expected, reads) in [
            ("agent", 0, None, 0),
            ("agent", 3, None, 1),
            ("other", 4, None, 1),
            ("agent", 4, Some(vec![4]), 1),
        ] {
            port.reads.set(0);
            assert_eq!(read_opening_audio(&port, agent, turn).ok(), Some(expected));
            assert_eq!((port.reads.get(), port.scans.get()), (reads, 0));
        }
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod closing_tests;

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod retirement_tests;

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tolerance_tests;
