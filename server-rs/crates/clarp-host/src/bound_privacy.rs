//! Who may read the words of an agent bound to a model provider of its own (issue 751).
//!
//! Such an agent's turns run on that provider alone, and its words go to no model or service
//! of the Host's. Other agents run on their own providers: what they read of it they may pass
//! to theirs. So its words are read only by:
//!
//! - its owner: the apps (a paired device), and the administrator's token presented without
//!   an agent's turn;
//! - itself, and every agent bound as it is (the same Codex home and the same provider: its
//!   forks, and the helpers made with it as their parent, are bound so);
//! - the agents the owner lets read it, one by one (`readers` on its record). An agent asks
//!   with `clarp-admin agent provider --reader`, and the owner answers on a card.
//!
//! Any other reader (another agent, a tool call that cannot say whose turn it is, another
//! Host, a page of another origin) is told the agent is there, its name and its state, and
//! not what it says. One check decides it ([`may_read`]); one guard ([`guard`]) applies it to
//! every route by the class the route is given in [`CLASSES`], and a test walks the Host's
//! routes so that a route that answers an agent's words cannot be added without a class.
//! The agents' own tools ask the same check ([`reader_of_call`]), as do the messages an agent
//! sends ([`withheld_message`]) and the updates it gives its teams ([`withheld_update`]).
//!
//! What it does not stop: anything run with the administrator's token and no turn reads as
//! the owner (an agent's tool run outside a turn falls back to that token); an agent on the
//! same machine that reads the Host's configuration (its administrator token) or the agent's
//! own files. The Host keeps its words out of what it hands to other agents; the machine's
//! own files are the machine's.

use std::collections::HashSet;
use std::sync::Arc;

use axum::body::{Body, Bytes};
use axum::extract::{MatchedPath, Request, State};
use axum::http::{HeaderMap, StatusCode, header};
use axum::middleware::Next;
use axum::response::{IntoResponse, Response};
use futures_util::StreamExt;
use serde_json::{Map, Value, json};

use crate::edge::{App, Principal};
use crate::fleet::{AgentRecord, Member};

/// The code a read of a withheld agent's words is refused with.
pub const REFUSED: &str = "bound_conversation";

/// Who reads.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Reader {
    /// The owner: the apps, and the administrator without an agent's turn.
    Owner,
    /// An agent, by its turn: its id, and the Codex home and provider it is bound to (empty
    /// where it is not).
    Agent {
        agent_id: String,
        home: String,
        provider: String,
    },
    /// An agent's tool or script that cannot say whose turn it is.
    Unknown,
    /// Another Host.
    Elsewhere,
}

impl Reader {
    /// The reader an agent is.
    pub fn agent(record: &AgentRecord) -> Self {
        Reader::Agent {
            agent_id: record.agent_id.clone(),
            home: record.codex_home.clone(),
            provider: record.codex_provider.clone(),
        }
    }

    /// The agent's id, where the reader is one.
    pub fn agent_id(&self) -> Option<&str> {
        match self {
            Reader::Agent { agent_id, .. } => Some(agent_id),
            _ => None,
        }
    }
}

/// The one check: whether this reader may read what this agent says.
pub fn may_read(reader: &Reader, record: &AgentRecord) -> bool {
    if !record.bound() {
        return true;
    }
    match reader {
        Reader::Owner => true,
        Reader::Agent {
            agent_id,
            home,
            provider,
        } => {
            *agent_id == record.agent_id
                || (*home == record.codex_home && *provider == record.codex_provider)
                || record.readers.iter().any(|reader| reader == agent_id)
        }
        Reader::Unknown | Reader::Elsewhere => false,
    }
}

/// Whether every reader who may read `maker` may read `helper` too. Only then may what the
/// helper says stand in its maker's line (`edge::Families`), which its maker's readers are
/// given in the snapshot, in the one-row read and in an event's `parent` (issue 819). A helper
/// that is not bound is read by everyone; one that is must be bound as its maker is, and read by
/// each agent its maker lets in.
pub fn read_with(helper: &AgentRecord, maker: &AgentRecord) -> bool {
    if !helper.bound() {
        return true;
    }
    maker.bound()
        && helper.codex_home == maker.codex_home
        && helper.codex_provider == maker.codex_provider
        && maker
            .readers
            .iter()
            .all(|reader| *reader == helper.agent_id || helper.readers.contains(reader))
}

/// The agents whose words a reader may not read, and every agent's names (which stay shown).
#[derive(Debug, Default)]
pub struct Withheld {
    /// By id and by session.
    agents: HashSet<String>,
    /// Every agent's persona and session: a name is no word.
    names: HashSet<String>,
}

impl Withheld {
    /// What this reader may not read on this Host now; `None` where it may read everything.
    pub fn of(app: &App, reader: &Reader) -> Option<Withheld> {
        if *reader == Reader::Owner {
            return None;
        }
        let directory = app.fleet.directory();
        let everyone = directory.members.iter().chain(directory.janitors.iter());
        let records: Vec<&AgentRecord> = everyone.map(|member| &member.record).collect();
        Self::among(records, reader)
    }

    /// What this reader may not read among these agents; `None` where it may read them all.
    pub fn among<'a>(
        records: impl IntoIterator<Item = &'a AgentRecord>,
        reader: &Reader,
    ) -> Option<Withheld> {
        let mut withheld = Withheld::default();
        for record in records {
            withheld.names.insert(record.persona.clone());
            withheld.names.insert(record.session.clone());
            if !may_read(reader, record) {
                withheld.agents.insert(record.agent_id.clone());
                withheld.agents.insert(record.session.clone());
            }
        }
        withheld.agents.remove("");
        (!withheld.agents.is_empty()).then_some(withheld)
    }

    /// Whether this agent (by id or session) is withheld.
    pub fn has(&self, agent: &str) -> bool {
        !agent.is_empty() && self.agents.contains(agent)
    }

    /// Blanks the words of every object in `value` that is of a withheld agent. Answers
    /// whether anything was blanked.
    pub fn walk(&self, value: &mut Value) -> bool {
        match value {
            Value::Object(fields) => {
                if self.names_one(fields) {
                    *value = self.blanked(fields);
                    return true;
                }
                let mut changed = false;
                for child in fields.values_mut() {
                    changed |= self.walk(child);
                }
                changed
            }
            Value::Array(items) => {
                let mut changed = false;
                for item in items {
                    changed |= self.walk(item);
                }
                changed
            }
            _ => false,
        }
    }

    /// Whether an object is of a withheld agent: one of the keys that say whose it is names
    /// one.
    fn names_one(&self, fields: &Map<String, Value>) -> bool {
        WHOSE
            .iter()
            .filter_map(|key| fields.get(*key).and_then(Value::as_str))
            .any(|agent| self.has(agent))
    }

    /// An object of a withheld agent with its words blanked: what says whose it is, what it
    /// is and when stays, and every other text is emptied, all the way down. The rows a
    /// `transcript-updated` carries (feature `events_rows`) are left out whole: their count,
    /// kinds, times and ids are what `/log` refuses such a reader, not words to blank.
    fn blanked(&self, fields: &Map<String, Value>) -> Value {
        let mut kept = Map::new();
        for (key, value) in fields {
            if CARRIED.contains(&key.as_str()) {
                continue;
            }
            kept.insert(key.clone(), self.blank(key, value));
        }
        kept.insert("words_withheld".to_string(), Value::Bool(true));
        Value::Object(kept)
    }

    fn blank(&self, key: &str, value: &Value) -> Value {
        match value {
            Value::String(text) if self.keeps(key, text) => value.clone(),
            Value::String(_) => Value::String(String::new()),
            Value::Array(items)
                if items.iter().all(Value::is_string)
                    && !kept_key(key)
                    && !key.ends_with("_ids") =>
            {
                Value::Array(Vec::new())
            }
            Value::Array(items) => {
                Value::Array(items.iter().map(|item| self.blank(key, item)).collect())
            }
            Value::Object(fields) => {
                let mut kept = Map::new();
                for (inner, value) in fields {
                    kept.insert(inner.clone(), self.blank(inner, value));
                }
                Value::Object(kept)
            }
            other => other.clone(),
        }
    }

    /// Whether a text under this key is no word of the agent's: an id, a moment, an address,
    /// a name the Host gave, or a value of a short list (a state, a kind).
    fn keeps(&self, key: &str, text: &str) -> bool {
        if NAME_KEYS.contains(&key) {
            return self.names.contains(text);
        }
        // An id is no word, but a reference a card's asker wrote is its words.
        if key == "reference_id" {
            return false;
        }
        if key.ends_with("_id") || key.ends_with("_ids") || key.ends_with("_url") {
            return true;
        }
        if key.ends_with("_at") || key.ends_with("_ms") || key == "timestamp" || key == "ts" {
            return true;
        }
        let named = key.ends_with("session") || key.ends_with("state") || key.ends_with("status");
        (kept_key(key) || named) && is_token(text)
    }

    /// The same SSE frame with every event of a withheld agent blanked.
    fn frame(&self, frame: &[u8]) -> Vec<u8> {
        let Ok(text) = std::str::from_utf8(frame) else {
            return frame.to_vec();
        };
        let mut out = String::with_capacity(text.len());
        for line in text.split_inclusive('\n') {
            let (body, end) = match line.strip_suffix('\n') {
                Some(body) => (body, "\n"),
                None => (line, ""),
            };
            let Some(data) = body.strip_prefix("data:") else {
                out.push_str(line);
                continue;
            };
            let data = data.strip_prefix(' ').unwrap_or(data);
            let mut event = serde_json::from_str::<Value>(data).unwrap_or(Value::Null);
            if self.walk(&mut event) {
                out.push_str("data: ");
                out.push_str(&event.to_string());
                out.push_str(end);
            } else {
                out.push_str(line);
            }
        }
        out.into_bytes()
    }
}

/// What a `transcript-updated` carries of a chat's rows (feature `events_rows`): left out of a
/// withheld agent's event, never blanked.
const CARRIED: [&str; 4] = ["rows", "rows_latest", "rows_coordination", "hidden_after"];

/// The keys that say whose an object is.
/// An object is the agent's that says it is from it, for it, about it or done by it: its own
/// rows, what was sent or routed to it, what it delegated or was delegated, what it asked and
/// answered. Its parent, leader, janitor or focus name another agent's place, not its words.
const WHOSE: &[&str] = &[
    "session",
    "agent_id",
    "agent_session",
    "source_agent_id",
    "source_session",
    "sender_agent_id",
    "sender_session",
    "requester_agent_id",
    "requested_session",
    "target_agent_id",
    "target_session",
    "delegate_agent_id",
    "delegate_session",
    "principal_agent_id",
    "principal_session",
    "owner_agent_id",
    "owner_session",
    "answer_session",
    "reply_to_agent_id",
    "reply_to_session",
    "actor_agent_id",
    "actor_session",
    "helper_session",
];

/// The keys a name is under: kept where it is an agent's name, as every name is shown.
const NAME_KEYS: &[&str] = &[
    "name",
    "persona",
    "agent_name",
    "display_name",
    "sender_name",
];

/// The keys whose short values are no words.
fn kept_key(key: &str) -> bool {
    matches!(
        key,
        "session"
            | "agent_session"
            | "parent"
            | "parent_session"
            | "type"
            | "kind"
            | "state"
            | "status"
            | "op"
            | "event"
            | "role"
            | "backend"
            | "model"
            | "effort"
            | "origin"
            | "view"
            | "audience"
            | "codex_provider"
            | "voice_id"
            | "coordination_visibility"
            | "helper_state"
            | "chat_view"
    )
}

/// Whether a text is one word of a short list: no space, and short.
fn is_token(text: &str) -> bool {
    text.len() <= 64 && !text.chars().any(char::is_whitespace)
}

// ---- who asks ----------------------------------------------------------------------------------

/// The reader a request is: an agent by the turn it presents (`X-Clarp-Turn`), an agent's tool
/// by its token, another Host by `X-Clarp-Peer`, and otherwise the owner (a paired device,
/// from any page it reads, and the administrator's token without a turn).
/// A key that cannot be read is answered so (503), as the Host answers any turn it cannot place.
pub async fn reader_of(
    app: &App,
    principal: Option<&Principal>,
    headers: &HeaderMap,
) -> Result<Reader, crate::edge::ApiError> {
    let turn = headers
        .get(crate::turn_identity::HEADER)
        .map(|token| token.to_str().unwrap_or_default().trim())
        .filter(|token| !token.is_empty());
    if let Some(token) = turn {
        return by_turn(app, token)
            .await
            .map_err(|why| crate::edge::ApiError::retryable(StatusCode::SERVICE_UNAVAILABLE, why));
    }
    if matches!(principal, Some(Principal::Tools)) {
        return Ok(Reader::Unknown);
    }
    // A paired device is the owner's, whatever page it reads from; another Host says so.
    if crate::coordination::from_a_peer(headers) {
        return Ok(Reader::Elsewhere);
    }
    Ok(Reader::Owner)
}

/// The reader an agent's own tool is: the agent whose turn it carries, the owner where it
/// carries none and was called with the owner's token, and otherwise an agent that cannot
/// say whose turn it is.
pub async fn reader_of_call(
    app: &App,
    call: &crate::tools::ToolCall,
    owner_token: bool,
) -> Result<Reader, String> {
    let turn = call
        .env
        .get(crate::turn_identity::TURN_ENV)
        .map(|token| token.trim())
        .filter(|token| !token.is_empty());
    match turn {
        Some(token) => by_turn(app, token).await,
        None if owner_token => Ok(Reader::Owner),
        None => Ok(Reader::Unknown),
    }
}

async fn by_turn(app: &App, token: &str) -> Result<Reader, String> {
    match crate::turn_identity::caller(app, token).await {
        Ok(Some(member)) => Ok(Reader::agent(&member.record)),
        Ok(None) => Ok(Reader::Unknown),
        Err(why) => Err(why.0),
    }
}

/// The words a refusal says, of the agent it is about.
pub fn refusal_words(persona: &str) -> String {
    format!(
        "{persona} is bound to a model provider of its own, and its words are not read by \
         other agents. Its owner can let an agent read it: clarp-admin agent provider \
         --session <its session> --reader <the agent>."
    )
}

/// The refusal of a read of a withheld agent's words.
pub fn refused(persona: &str) -> Response {
    words_refused(persona).into_response()
}

pub(crate) fn words_refused(persona: &str) -> crate::edge::ApiError {
    let message = refusal_words(persona);
    let body = json!({ "error": REFUSED, "message": message });
    crate::edge::ApiError::custom(StatusCode::FORBIDDEN, message, body)
}

// ---- the routes ----------------------------------------------------------------------------------

/// What a route answers, for the guard.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Class {
    /// No agent's words.
    NoWords,
    /// Agents' words. Refused where the request names a withheld agent (its `session` or
    /// `agent_id`, or what [`Names`] adds); otherwise every object of a withheld agent in its
    /// JSON answer is blanked.
    Words(Names),
    /// A stream of events: every event of a withheld agent is blanked.
    Stream,
    /// Surfaces whose answer cannot be blanked (files, terminals, recordings, sound, sockets):
    /// given where the read names only agents the reader may read (by what [`Names`] says), and
    /// otherwise refused while one agent's words are withheld from the reader.
    OwnerOnly(Names),
}

/// What else names the agent a route of words is about.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Names {
    /// Its query alone.
    Query,
    /// The path's segment at this place (from 0, after the first `/`): a session or an id.
    Segment(usize),
    /// The path's segment at this place is an artifact's id: the agent that made it.
    Artifact(usize),
    /// The query's `plan_id`: the agent whose goal it is.
    Plan,
    /// The path's segment at this place is a clip's number: the agent whose speech it is.
    Clip(usize),
    /// The path's segment at this place is a gallery asset's id: the chat it was shown in.
    Media(usize),
    /// Nothing: the route cannot say whose words it serves.
    Nobody,
}

use Class::{NoWords, OwnerOnly, Stream, Words};
use Names::{Artifact, Clip, Media, Nobody, Plan, Query, Segment};

/// Every route a `GET` reaches, by the path it is declared with, and what it answers.
pub const CLASSES: &[(&str, Class)] = &[
    ("/", NoWords),
    ("/agent-file", OwnerOnly(Query)),
    ("/agent-files", OwnerOnly(Query)),
    ("/agent-goal", Words(Query)),
    ("/agent-heartbeat/status", Words(Query)),
    ("/agent-helper-state", NoWords),
    ("/agent-model-options", NoWords),
    ("/agent-portrait-generation", NoWords),
    ("/agent-portraits", NoWords),
    ("/agent-portraits/{id}/content", NoWords),
    ("/agent-schedules", Words(Query)),
    ("/agent-transit", Words(Query)),
    ("/agents/archived", Words(Query)),
    ("/agents/snapshot", Words(Query)),
    // One agent's row: blanked as the snapshot blanks it, never refused (issue 809).
    ("/agents/{session}", Words(Query)),
    ("/artifact-catalogs/clarp.form/1", NoWords),
    ("/artifact-catalogs/clarp.form/2", NoWords),
    ("/artifacts", Words(Query)),
    ("/artifacts/{*rest}", Words(Artifact(1))),
    ("/attention", Words(Query)),
    ("/attention/inbox", Words(Query)),
    ("/audio/", OwnerOnly(Nobody)),
    ("/audio/{*name}", OwnerOnly(Nobody)),
    ("/automation-settings", NoWords),
    ("/avatar-settings", NoWords),
    ("/avatars/{agent}", NoWords),
    ("/backend-auth", NoWords),
    ("/backend-usage", NoWords),
    ("/background-jobs", Words(Query)),
    ("/background-jobs/{job_id}", Words(Query)),
    ("/calls", Words(Query)),
    ("/calls/ring", Words(Query)),
    ("/calls/ring/device", Words(Query)),
    ("/calls/ring/live", OwnerOnly(Nobody)),
    ("/calls/ring/settings", NoWords),
    ("/cartesia-voice-preview", NoWords),
    ("/cartesia-voices", NoWords),
    ("/client-log", NoWords),
    ("/clips/message", Words(Query)),
    ("/clips/recoverable", Words(Query)),
    ("/clips/{clip}/complete.mp3", OwnerOnly(Clip(1))),
    ("/clips/{clip}/stream", OwnerOnly(Clip(1))),
    ("/controller-actions", Words(Query)),
    ("/controller-actions/audit", Words(Query)),
    ("/controller-narration", NoWords),
    ("/coordination-settings", NoWords),
    ("/devices/push-tokens", NoWords),
    ("/diagnostics/health", NoWords),
    ("/diagnostics/settings", NoWords),
    ("/dirs", OwnerOnly(Nobody)),
    ("/events", Stream),
    ("/favorite-paths", NoWords),
    ("/goal-ledger", Words(Plan)),
    ("/goal-ledger/delegations", Words(Query)),
    ("/harness-providers", NoWords),
    ("/harness-providers/models", NoWords),
    ("/heartbeat/settings", NoWords),
    ("/herald/settings", NoWords),
    ("/host/start", NoWords),
    ("/hotseat/desk", NoWords),
    ("/hotseat/status", NoWords),
    ("/icon.png", NoWords),
    ("/janitor-policy", NoWords),
    ("/janitor-runs/{*rest}", Words(Query)),
    ("/janitor-triggers", Words(Query)),
    ("/janitor-triggers/preview", Words(Query)),
    ("/janitors", Words(Query)),
    ("/janitors/{session}", Words(Segment(1))),
    ("/janitors/{session}/{action}", Words(Segment(1))),
    ("/judgments/settings", NoWords),
    ("/lane-settings", NoWords),
    ("/launch-directories", NoWords),
    ("/live", Words(Query)),
    ("/location", NoWords),
    ("/log", Words(Query)),
    // A withheld agent is never searched for this reader (`search::find`); one named by its
    // session is refused as its `/log` is.
    ("/search", Words(Query)),
    ("/send-settings", NoWords),
    ("/managed-skills", NoWords),
    ("/manifest.json", NoWords),
    ("/media", Words(Query)),
    ("/media/", OwnerOnly(Nobody)),
    ("/media/deleted", Words(Query)),
    ("/media/{*asset_id}", OwnerOnly(Media(1))),
    ("/message-tool-details", Words(Query)),
    ("/network/relay", NoWords),
    ("/notification-avatars/{agent}", NoWords),
    ("/oracle/calls/status", Words(Query)),
    ("/oracle/contact", Words(Query)),
    ("/oracle/delegations", Words(Query)),
    ("/oracle/handoff", Words(Query)),
    ("/oracle/status", Words(Query)),
    ("/oracle/v2", OwnerOnly(Nobody)),
    ("/oracle/voice-provider", NoWords),
    ("/orchestrator/decisions", Words(Query)),
    ("/orchestrator/settings", NoWords),
    ("/paired-devices", NoWords),
    ("/pairing/identity", NoWords),
    ("/past-sessions", OwnerOnly(Nobody)),
    ("/peers", NoWords),
    ("/peers/snapshot", Words(Query)),
    ("/persona-avatars/{id}", NoWords),
    ("/personalities/settings", NoWords),
    ("/text-turn-ack/settings", NoWords),
    ("/pinned-artifacts", Words(Query)),
    ("/podcast-history", Words(Query)),
    ("/podcast-history/sources", Words(Query)),
    ("/remote-action", NoWords),
    ("/server-info", NoWords),
    ("/server-terminal", OwnerOnly(Nobody)),
    ("/server-update", NoWords),
    ("/static/", NoWords),
    ("/static/{*path}", NoWords),
    ("/status", Words(Query)),
    ("/stt/stream", OwnerOnly(Query)),
    ("/styles.css", NoWords),
    ("/sw.js", NoWords),
    ("/task-plan", Words(Query)),
    ("/task-plan/document", Words(Plan)),
    ("/task-plans", Words(Query)),
    ("/teams", Words(Query)),
    ("/teams/{id}/messages", Words(Query)),
    ("/terminal/{*named}", OwnerOnly(Segment(1))),
    ("/tool-explanations/settings", NoWords),
    ("/tool-explanations/stats", NoWords),
    ("/transcription-audio", OwnerOnly(Nobody)),
    ("/transcription-capabilities", NoWords),
    ("/transcription-guidance", NoWords),
    ("/transcription-providers", NoWords),
    ("/tts/providers", NoWords),
    ("/turn-queue", Words(Query)),
    ("/viz", NoWords),
    ("/vocab/assignments", OwnerOnly(Nobody)),
    ("/vocab/budgets", NoWords),
    ("/vocab/packs", OwnerOnly(Nobody)),
    ("/vocab/preview", OwnerOnly(Nobody)),
    ("/vocab/profile", OwnerOnly(Nobody)),
    ("/vocab/profiles", OwnerOnly(Nobody)),
    ("/vocab/run", OwnerOnly(Nobody)),
    ("/vocab/runs", OwnerOnly(Nobody)),
    ("/vocab/terms", OwnerOnly(Nobody)),
    ("/voice-catalog", NoWords),
    ("/voice-events", Words(Query)),
    ("/voice-events/utterances", Words(Query)),
    ("/voice-humanness", NoWords),
    ("/voice-preview", NoWords),
    ("/voices", NoWords),
];

/// The class of the route a request reached; `None` for one not in [`CLASSES`].
pub fn class_of(route: &str) -> Option<Class> {
    CLASSES
        .iter()
        .find(|(path, _)| *path == route)
        .map(|(_, class)| *class)
}

/// The guard: every request that reaches a route passes it. The owner passes untouched, as
/// does whatever reaches a route that answers no words, and every other method than a read
/// (what an agent writes is no other agent's words; the tools ask [`reader_of_call`]). A read
/// by anyone else is refused where it names an agent whose words are withheld from it, and is
/// answered with every object of such an agent blanked. A route without a class is one only
/// the owner reads while anything is withheld from the reader.
pub async fn guard(State(app): State<App>, request: Request, next: Next) -> Response {
    if request.method() != axum::http::Method::GET && request.method() != axum::http::Method::HEAD {
        return next.run(request).await;
    }
    let route = request
        .extensions()
        .get::<MatchedPath>()
        .map(|path| path.as_str().to_string())
        .unwrap_or_default();
    // A route without a class is refused to every reader but the owner while an agent is
    // withheld from it: what it answers is not known here.
    let class = class_of(&route).unwrap_or(OwnerOnly(Nobody));
    if class == NoWords {
        return next.run(request).await;
    }
    let principal = request.extensions().get::<Principal>().cloned();
    let reader = match reader_of(&app, principal.as_ref(), request.headers()).await {
        Ok(reader) => reader,
        Err(refused) => return refused.into_response(),
    };
    let mut response = judged(&app, request, next, class, &reader).await;
    // Who the answer was made for: a list's tag is its caller's (`tagged.rs`).
    let limited = matches!(
        principal,
        Some(Principal::Device {
            scope: crate::pairing::Scope::Limited,
            ..
        })
    );
    response
        .extensions_mut()
        .insert(crate::tagged::Caller { reader, limited });
    response
}

/// The answer to a read of this class, as this reader may have it.
async fn judged(
    app: &App,
    request: Request,
    next: Next,
    class: Class,
    reader: &Reader,
) -> Response {
    if *reader == Reader::Owner {
        return next.run(request).await;
    }
    // A stream is judged frame by frame, against the fleet as it is when each event is sent:
    // an agent bound after it opened is withheld from then on, and a reader the owner stops
    // reads no more.
    if class == Stream {
        let response = next.run(request).await;
        let (parts, body) = response.into_parts();
        return Response::from_parts(parts, sse_filtered(body, app.clone(), reader.clone()));
    }
    let Some(withheld) = Withheld::of(app, reader) else {
        return next.run(request).await;
    };
    // Owned: the body a request holds may not be shared across what the guard waits for.
    let uri = request.uri().clone();
    match class {
        NoWords => next.run(request).await,
        OwnerOnly(names) => {
            if named(app, &uri, names, &withheld).await == About::Readable {
                return next.run(request).await;
            }
            owner_only()
        }
        Stream => next.run(request).await,
        Words(names) => {
            let about = named(app, &uri, names, &withheld).await;
            if let About::Withheld(persona) = &about {
                return refused(persona);
            }
            let response = next.run(request).await;
            blanked(response, &withheld, about == About::Readable).await
        }
    }
}

/// Which agent a read names.
#[derive(Debug, PartialEq, Eq)]
enum About {
    /// None.
    Nobody,
    /// Only agents whose words the reader may read.
    Readable,
    /// One whose words are withheld from it, by its name.
    Withheld(String),
}

/// The refusal of a read only the owner makes while an agent's words are withheld from the
/// reader: files, terminals, recordings, sound, sockets.
fn owner_only() -> Response {
    let message = "Only the owner reads this while the Host has an agent bound to a model \
        provider of its own whose words are not yours to read.";
    let body = json!({ "error": REFUSED, "message": message });
    (StatusCode::FORBIDDEN, axum::Json(body)).into_response()
}

/// Which agents a request names: one whose words are withheld from the reader, by its name,
/// before any other.
async fn named(app: &App, uri: &axum::http::Uri, names: Names, withheld: &Withheld) -> About {
    let query = uri.query().unwrap_or("");
    let mut asked: Vec<String> = url::form_urlencoded::parse(query.as_bytes())
        .filter(|(key, _)| names != Nobody && (key == "session" || key == "agent_id"))
        .map(|(_, value)| value.trim().to_string())
        .collect();
    let segments: Vec<String> = uri
        .path()
        .trim_start_matches('/')
        .split('/')
        .map(decoded)
        .collect();
    match names {
        Query | Nobody => {}
        Clip(at) => {
            let clip = segments.get(at).and_then(|clip| clip.parse::<u64>().ok());
            let speaker =
                clip.and_then(|clip| crate::voice::speaker_of(&app.fleet.directory(), clip));
            asked.push(speaker.unwrap_or_else(|| UNKNOWN.to_string()));
        }
        Media(at) => {
            let asset = segments.get(at).cloned().unwrap_or_default();
            let shown_in = app
                .gallery
                .ask(move |store| store.content(&asset).map(|(image, _)| image.asset.session))
                .await
                .ok();
            asked.push(shown_in.unwrap_or_else(|| UNKNOWN.to_string()));
        }
        Segment(at) => asked.extend(segments.get(at).cloned()),
        Artifact(at) => {
            let maker = segments
                .get(at)
                .and_then(|id| crate::library::maker_of(app, id));
            asked.extend(maker);
        }
        Plan => {
            let plan = url::form_urlencoded::parse(query.as_bytes())
                .find(|(key, _)| key == "plan_id")
                .map(|(_, value)| value.into_owned());
            asked.extend(plan.map(|plan| crate::goaltool::owner_of(&plan).to_string()));
        }
    }
    asked.retain(|agent| !agent.is_empty());
    if asked.iter().any(|agent| agent == UNKNOWN) {
        return About::Nobody;
    }
    let directory = app.fleet.directory();
    let withheld = asked
        .iter()
        .filter(|agent| withheld.has(agent))
        .find_map(|agent| {
            let everyone = directory.members.iter().chain(directory.janitors.iter());
            everyone
                .map(|member: &Arc<Member>| &member.record)
                .find(|record| record.session == *agent || record.agent_id == *agent)
                .map(|record| record.persona.clone())
        });
    match withheld {
        Some(persona) => About::Withheld(persona),
        None if asked.is_empty() => About::Nobody,
        None => About::Readable,
    }
}

/// What a clip or an asset no agent can be found for names: nobody the reader may read.
const UNKNOWN: &str = "\0unknown";

/// A segment of a path as it reads, its escapes undone.
fn decoded(segment: &str) -> String {
    let pair = format!("x={}", segment.replace('+', "%2B"));
    url::form_urlencoded::parse(pair.as_bytes())
        .next()
        .map(|(_, value)| value.into_owned())
        .unwrap_or_default()
}

/// The answer with every object of a withheld agent blanked, where it is JSON. Any other
/// answer of a route of words is given only where the read named agents the reader may read
/// (an artifact's page, of an agent it reads), and refused otherwise, as nothing in it can
/// be told apart.
async fn blanked(response: Response, withheld: &Withheld, readable: bool) -> Response {
    let json = response
        .headers()
        .get(header::CONTENT_TYPE)
        .and_then(|kind| kind.to_str().ok())
        .is_some_and(|kind| kind.starts_with("application/json"));
    if !json {
        if response.status().is_success() && !readable {
            return owner_only();
        }
        return response;
    }
    let (mut parts, body) = response.into_parts();
    let Ok(bytes) = axum::body::to_bytes(body, usize::MAX).await else {
        return (
            StatusCode::INTERNAL_SERVER_ERROR,
            "answer could not be read",
        )
            .into_response();
    };
    let Ok(mut value) = serde_json::from_slice::<Value>(&bytes) else {
        return Response::from_parts(parts, Body::from(bytes));
    };
    if !withheld.walk(&mut value) {
        return Response::from_parts(parts, Body::from(bytes));
    }
    parts.headers.remove(header::CONTENT_LENGTH);
    Response::from_parts(parts, Body::from(value.to_string()))
}

/// A stream of events with every event of an agent withheld from `reader` blanked, frame by
/// frame, each judged against the fleet as it is then.
fn sse_filtered(body: Body, app: App, reader: Reader) -> Body {
    let inner = body.into_data_stream();
    let judged = move |frame: &[u8]| match Withheld::of(&app, &reader) {
        Some(withheld) => withheld.frame(frame),
        None => frame.to_vec(),
    };
    let frames = futures_util::stream::unfold(
        (inner, Vec::<u8>::new(), judged, false),
        |(mut inner, mut held, judged, done)| async move {
            if done {
                return None;
            }
            loop {
                if let Some(end) = held.windows(2).position(|pair| pair == b"\n\n") {
                    let frame: Vec<u8> = held.drain(..end + 2).collect();
                    let out = Bytes::from(judged(&frame));
                    return Some((Ok(out), (inner, held, judged, false)));
                }
                match inner.next().await {
                    Some(Ok(chunk)) => held.extend_from_slice(&chunk),
                    Some(Err(why)) => return Some((Err(why), (inner, held, judged, true))),
                    None if held.is_empty() => return None,
                    None => {
                        let rest = std::mem::take(&mut held);
                        let out = Bytes::from(judged(&rest));
                        return Some((Ok(out), (inner, held, judged, true)));
                    }
                }
            }
        },
    );
    Body::from_stream(frames)
}

// ---- what a bound agent sends ------------------------------------------------------------------

/// What an agent is given of a message a bound agent sends it, where it may not read the
/// sender: that one came, and not what it says. `None` where it may read it.
pub fn withheld_message(sender: &AgentRecord, recipient: &AgentRecord) -> Option<String> {
    if may_read(&Reader::agent(recipient), sender) {
        return None;
    }
    Some(format!(
        "{} sent you a message. Its words are withheld: it is bound to a model provider of \
         its own, and you are not one of the agents its owner lets read it.",
        sender.persona
    ))
}

/// What a team member is given of an update a bound teammate posted, where it may not read
/// that teammate; `None` where it may.
pub fn withheld_update(sender: &AgentRecord, reader: &AgentRecord) -> Option<String> {
    if may_read(&Reader::agent(reader), sender) {
        return None;
    }
    Some("posted an update (words withheld: bound to a model provider of its own)".to_string())
}

/// What a team member is given of an update whose sender, or whose reader, the Host cannot
/// place: it is withheld, since it cannot be said who may read it.
pub const UNPLACED_UPDATE: &str =
    "posted an update (words withheld: its sender could not be placed)";

// ---- the agents' own tools ------------------------------------------------------------------

/// The exit code a tool's read of a withheld agent's words ends with (`EX_NOPERM`).
pub const TOOL_REFUSED: i32 = 77;

/// Whether a verb of an agent's tool reads what one agent wrote: its goals and their
/// documents, the artifacts it published and the cards it asked.
fn reads_about(tool: &str, verb: &str) -> bool {
    match tool {
        "clarp-goal" | "clarp-agent-tasks" => true,
        "clarp-agent-artifacts" => matches!(
            verb,
            "list" | "wait" | "form-events" | "report-history" | "attention"
        ),
        _ => false,
    }
}

/// The refusal of a tool's read about a withheld agent: one named by its session or id, by a
/// goal of its, or by an artifact it made. `None` where the call reads no such agent.
pub fn tool_refused(
    app: &App,
    call: &crate::tools::ToolCall,
    withheld: &Withheld,
) -> Option<crate::tools::ToolAnswer> {
    let verb = call.argv.first().map_or("", String::as_str);
    if !reads_about(&call.tool, verb) {
        return None;
    }
    let goals = call.tool != "clarp-agent-artifacts";
    let about = call.argv.iter().find_map(|said| {
        let said = said.trim();
        let maker = (!goals)
            .then(|| crate::library::maker_of(app, said))
            .flatten();
        let owner = goals.then(|| crate::goaltool::owner_of(said).to_string());
        [Some(said.to_string()), maker, owner]
            .into_iter()
            .flatten()
            .find(|agent| withheld.has(agent))
    })?;
    let directory = app.fleet.directory();
    let mut everyone = directory.members.iter().chain(directory.janitors.iter());
    let persona = everyone
        .find(|member| member.record.session == about || member.record.agent_id == about)
        .map_or_else(|| about.clone(), |member| member.record.persona.clone());
    Some(crate::tools::ToolAnswer::said(
        TOOL_REFUSED,
        format!("{}: {REFUSED}: {}", call.tool, refusal_words(&persona)),
    ))
}

/// A tool's answer with every object of a withheld agent in what it printed as JSON blanked.
pub fn tool_blanked(
    mut answer: crate::tools::ToolAnswer,
    withheld: &Withheld,
) -> crate::tools::ToolAnswer {
    let Ok(mut printed) = serde_json::from_str::<Value>(answer.stdout.trim()) else {
        return answer;
    };
    if withheld.walk(&mut printed) {
        let shown = serde_json::to_string_pretty(&printed).unwrap_or_default();
        answer.stdout = format!("{shown}\n");
    }
    answer
}

// ---- who the owner lets read, and the binding he keeps ------------------------------------

/// What a request to change who may read a bound agent became.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ReadersChange {
    /// The owner asked: done.
    Done(Vec<String>),
    /// An agent asked: a card asks the owner (by its artifact's id), one a reader.
    Asked(Vec<Value>),
}

/// What the Host keeps of a question it asked its owner on an agent's behalf: under the id of
/// the decision it made, and nowhere on the card. A card's payload and reference are the
/// asking agent's to write, so the Host acts on no card but one it keeps a question for.
pub(crate) const ASKED: &str = "bound-privacy-asked";

/// What this desk keeps, declared once (`kept.rs`): a question per card it made, read when a
/// card is answered or an agent asks again.
pub(crate) const KEPT: &[crate::kept::Kind] =
    &[
        crate::kept::Kind::book(ASKED, crate::kept::Key::Plain, crate::kept::fits::<Asked>)
            .when_read(),
    ];

/// A question the Host asked its owner: whether an agent may read a bound agent, or whether
/// a bound agent's binding may change.
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub struct Asked {
    /// The decision the Host made for it.
    pub decision_id: String,
    /// The agent in whose chat it asked, the one that asked.
    pub asker_id: String,
    /// The bound agent it is about.
    pub bound_id: String,
    /// What is asked.
    pub change: Change,
}

/// What the owner is asked to allow.
#[derive(Debug, Clone, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum Change {
    /// That this agent (by id) may read the bound agent.
    Reader { reader_id: String },
    /// That its binding becomes this one.
    Binding {
        home: String,
        provider: String,
        cloud_speech: Option<bool>,
    },
}

/// The card that asks the owner whether `reader` may read what `bound` says, in the chat of
/// the agent that asked (its answer is that agent's to read).
fn reader_card(asker: &AgentRecord, bound: &AgentRecord, reader: &AgentRecord) -> Value {
    json!({
        "session": asker.session,
        "title": format!("Let {} read {}'s conversation?", reader.persona, bound.persona),
        "question": format!(
            "{} asks that {} may read what {} says. {} is bound to a model provider of its own; \
             {} runs on {}.",
            asker.persona,
            reader.persona,
            bound.persona,
            bound.persona,
            reader.persona,
            if reader.codex_provider.is_empty() {
                "the machine's own provider".to_string()
            } else {
                format!("the provider {}", reader.codex_provider)
            }
        ),
        "context": "Yes adds it to the agents that may read this agent's conversation, its \
            messages and its team updates. It can be taken back with clarp-admin agent \
            provider --no-reader. Only your own answer counts: one given on your behalf by \
            an agent is not taken.",
    })
}

/// Lets these agents read `bound`, or stops them (`add` false), as `reader` asks: the owner's
/// word is done at once; an agent's adding is a card to the owner, one a reader not yet let
/// in (an agent asked again is answered the card that still waits); an agent cannot stop one.
pub async fn change_readers(
    app: &App,
    reader: &Reader,
    bound: &AgentRecord,
    agents: &[AgentRecord],
    add: bool,
) -> Result<ReadersChange, (StatusCode, Value)> {
    if *reader == Reader::Owner {
        let mut readers = bound.readers.clone();
        for agent in agents {
            readers.retain(|id| *id != agent.agent_id);
            if add {
                readers.push(agent.agent_id.clone());
            }
        }
        set_readers(app, bound, readers.clone()).await?;
        return Ok(ReadersChange::Done(readers));
    }
    if !add {
        let why = "Only the owner stops an agent reading a bound agent.";
        return Err((
            StatusCode::FORBIDDEN,
            json!({"error": "owner_only", "message": why}),
        ));
    }
    let asker = asker_of(app, reader, "An agent asks for a reader from its own turn.")?;
    let mut asked = Vec::new();
    for agent in agents {
        if bound.readers.contains(&agent.agent_id) {
            continue;
        }
        let change = Change::Reader {
            reader_id: agent.agent_id.clone(),
        };
        let card = reader_card(&asker.record, bound, agent);
        let made = ask_owner(app, &asker, bound, change, card).await?;
        asked.push(json!({
            "reader": agent.session,
            "artifact_id": made["artifact_id"],
            "decision_id": made["decision"]["decision_id"],
        }));
    }
    Ok(ReadersChange::Asked(asked))
}

/// The agent whose turn asks, where a turn asks; refused (`owner_only`, in these words) for
/// any other caller that is not the owner.
fn asker_of(app: &App, reader: &Reader, why: &str) -> Result<Arc<Member>, (StatusCode, Value)> {
    let asker_id = reader.agent_id().unwrap_or_default();
    let directory = app.fleet.directory();
    let mut members = directory.members.iter();
    members
        .find(|member| !asker_id.is_empty() && member.record.agent_id == asker_id)
        .cloned()
        .ok_or_else(|| {
            (
                StatusCode::FORBIDDEN,
                json!({"error": "owner_only", "message": why}),
            )
        })
}

fn unkept(why: &crate::agent::Unavailable) -> (StatusCode, Value) {
    (
        StatusCode::SERVICE_UNAVAILABLE,
        json!({"error": "card_not_made", "message": why.0}),
    )
}

/// Asks the owner `change` about `bound` on a card in the asker's chat, and keeps the question
/// under the decision's id. A card the Host asked the same question on and that still waits
/// for him is answered again; nothing an agent wrote on a card of its own is taken for one.
async fn ask_owner(
    app: &App,
    asker: &Member,
    bound: &AgentRecord,
    change: Change,
    card: Value,
) -> Result<Value, (StatusCode, Value)> {
    let kept = app
        .fleet
        .kept_listed(ASKED)
        .await
        .map_err(|why| unkept(&why))?;
    let same: Vec<String> = kept
        .into_iter()
        .filter_map(|(id, bytes)| {
            let asked: Asked = serde_json::from_slice(&bytes).ok()?;
            let same = asked.asker_id == asker.record.agent_id
                && asked.bound_id == bound.agent_id
                && asked.change == change;
            same.then_some(id)
        })
        .collect();
    let waiting = crate::library::decisions_of(app, &asker.record.session)
        .into_iter()
        .find(|made| {
            let decision = &made["decision"];
            decision["status"] == "pending"
                && decision["decision_id"]
                    .as_str()
                    .is_some_and(|id| same.iter().any(|kept| kept == id))
        });
    if let Some(made) = waiting {
        return Ok(made);
    }
    let fields = card.as_object().cloned().unwrap_or_default();
    let made = crate::library::asked_of(app, asker, &fields)
        .await
        .map_err(|why| {
            let message = why.message().to_string();
            (
                why.status(),
                json!({"error": "card_not_made", "message": message}),
            )
        })?;
    let decision_id = made["decision"]["decision_id"]
        .as_str()
        .unwrap_or_default()
        .to_string();
    let asked = Asked {
        decision_id: decision_id.clone(),
        asker_id: asker.record.agent_id.clone(),
        bound_id: bound.agent_id.clone(),
        change,
    };
    let row = (
        ASKED,
        decision_id,
        serde_json::to_vec(&asked).unwrap_or_default(),
    );
    // Questions that no longer wait are let go with it: one a card a question.
    let gone = same.into_iter().map(|id| (ASKED, id)).collect();
    app.fleet
        .change_together(vec![row], gone)
        .await
        .map_err(|why| unkept(&why))?;
    Ok(made)
}

/// A binding a bound agent is asked to change to: its Codex home and provider (both empty to
/// let it go) and its choice of cloud speech.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Binding {
    pub home: String,
    pub provider: String,
    pub cloud_speech: Option<bool>,
}

impl Binding {
    /// The binding an agent has now.
    pub fn of(record: &AgentRecord) -> Self {
        Binding {
            home: record.codex_home.clone(),
            provider: record.codex_provider.clone(),
            cloud_speech: record.cloud_speech,
        }
    }
}

/// Whether this reader must ask the owner before `bound`'s binding becomes `to`: any change
/// of the binding of an agent bound to a provider of its own, asked by anyone but the owner.
/// Letting it go would carry its conversation to the machine's provider, another home or
/// provider to another, and cloud speech on to a speech provider in the cloud.
pub fn must_ask(reader: &Reader, bound: &AgentRecord, to: &Binding) -> bool {
    *reader != Reader::Owner && bound.bound() && Binding::of(bound) != *to
}

/// Asks the owner, on a card in the asking agent's chat, to change `bound`'s binding to `to`
/// ("Unbind B from its provider?" where it lets it go); his yes changes it
/// ([`answered`]). Answers the card as `asked` lists it.
pub async fn ask_binding(
    app: &App,
    reader: &Reader,
    bound: &AgentRecord,
    to: &Binding,
) -> Result<Value, (StatusCode, Value)> {
    let why = "Only the owner changes the binding of an agent bound to a provider of its own; \
        an agent asks him from its own turn.";
    let asker = asker_of(app, reader, why)?;
    let unbinds = to.home.is_empty() && to.provider.is_empty();
    let title = if unbinds {
        format!("Unbind {} from its provider?", bound.persona)
    } else {
        format!("Change {}'s provider?", bound.persona)
    };
    let speech = match to.cloud_speech {
        None => "as its kind has it",
        Some(true) => "on",
        Some(false) => "off",
    };
    let question = if unbinds {
        format!(
            "{} asks to let {} go from the model provider {}. Its next turn would run on the \
             machine's own provider, with its conversation. Cloud speech: {speech}.",
            asker.record.persona, bound.persona, bound.codex_provider
        )
    } else {
        format!(
            "{} asks to run {} on the provider {} in the Codex home {}. Cloud speech: {speech}.",
            asker.record.persona, bound.persona, to.provider, to.home
        )
    };
    let card = json!({
        "session": asker.record.session,
        "title": title,
        "question": question,
        "context": "Yes changes it as asked; its conversation goes where its binding points. \
            Only your own answer counts: one given on your behalf by an agent is not taken.",
    });
    let change = Change::Binding {
        home: to.home.clone(),
        provider: to.provider.clone(),
        cloud_speech: to.cloud_speech,
    };
    let made = ask_owner(app, &asker, bound, change, card).await?;
    Ok(json!({
        "binding": {"codex_home": to.home, "codex_provider": to.provider,
                    "cloud_speech": to.cloud_speech},
        "artifact_id": made["artifact_id"],
        "decision_id": made["decision"]["decision_id"],
    }))
}

async fn set_readers(
    app: &App,
    bound: &AgentRecord,
    readers: Vec<String>,
) -> Result<(), (StatusCode, Value)> {
    let change = crate::fleet::Amend::Readers(readers);
    match app.fleet.amend(bound.agent_id.clone(), change).await {
        Ok(Ok(_)) => Ok(()),
        Ok(Err(why)) => Err((
            StatusCode::CONFLICT,
            json!({"error": "not_changed", "message": format!("{why:?}")}),
        )),
        Err(why) => Err((
            StatusCode::SERVICE_UNAVAILABLE,
            json!({"error": "not_changed", "message": why.0}),
        )),
    }
}

/// What the Host does when a card is answered (`changed`: the answer changed it; a resolve
/// sent again changes nothing and does nothing here). Only a card the Host keeps a question
/// for counts ([`ASKED`]): what it asked is read from there, never from the card. Its owner's
/// own yes does what was asked; an answer given on his behalf by any agent is not taken. Once
/// answered, the question is let go.
pub async fn answered(
    app: &App,
    decision_id: &str,
    artifact: &Value,
    changed: bool,
    on_behalf: Option<&str>,
) {
    if !changed || artifact["decision"]["status"] == "pending" {
        return;
    }
    let kept = app.fleet.kept_book(ASKED, decision_id.to_string()).await;
    let Some(asked) = kept
        .ok()
        .flatten()
        .and_then(|bytes| serde_json::from_slice::<Asked>(&bytes).ok())
    else {
        return;
    };
    if artifact["decision"]["resolved_choice"] == "accepted" {
        match on_behalf.map(str::trim).filter(|acting| !acting.is_empty()) {
            Some(acting) => eprintln!(
                "clarp-host: a card of the owner's about a bound agent was answered by {acting} \
                 on his behalf; it is not taken"
            ),
            None => apply(app, &asked).await,
        }
    }
    let gone = vec![(ASKED, asked.decision_id)];
    if let Err(why) = app.fleet.change_together(Vec::new(), gone).await {
        app.fleet.unkept(ASKED, &why.0);
    }
}

/// Lets go of the question the Host kept for a card that was withdrawn or dismissed
/// (`changed`: it was so now). A card it kept none for changes nothing.
pub async fn let_go(app: &App, decision_id: &str, changed: bool) {
    if !changed {
        return;
    }
    let kept = app.fleet.kept_book(ASKED, decision_id.to_string()).await;
    if !matches!(kept, Ok(Some(_))) {
        return;
    }
    let gone = vec![(ASKED, decision_id.to_string())];
    if let Err(why) = app.fleet.change_together(Vec::new(), gone).await {
        app.fleet.unkept(ASKED, &why.0);
    }
}

/// Does what the owner allowed: a reader let in, or a binding changed (where its home still
/// defines its provider).
async fn apply(app: &App, asked: &Asked) {
    let directory = app.fleet.directory();
    let find = |id: &str| {
        let mut members = directory.members.iter();
        members.find(|member| member.record.agent_id == id).cloned()
    };
    let Some(bound) = find(&asked.bound_id) else {
        return;
    };
    match &asked.change {
        Change::Reader { reader_id } => {
            if find(reader_id).is_none() || bound.record.readers.contains(reader_id) {
                return;
            }
            let mut readers = bound.record.readers.clone();
            readers.push(reader_id.clone());
            if let Err((_, why)) = set_readers(app, &bound.record, readers).await {
                eprintln!("clarp-host: a reader the owner let in was not kept: {why}");
            }
        }
        Change::Binding {
            home,
            provider,
            cloud_speech,
        } => {
            if !home.is_empty() || !provider.is_empty() {
                let backend = bound.record.backend.clone();
                let (home, provider) = (home.clone(), provider.clone());
                let checked = tokio::task::spawn_blocking(move || {
                    crate::fleet::check_binding(&backend, &home, &provider)
                })
                .await;
                if !matches!(checked, Ok(Ok(_))) {
                    eprintln!(
                        "clarp-host: a binding the owner approved can no longer be used; not \
                         changed"
                    );
                    return;
                }
            }
            let change = crate::fleet::Amend::Provider {
                home: home.clone(),
                provider: provider.clone(),
                cloud_speech: *cloud_speech,
            };
            if !matches!(
                app.fleet.amend(asked.bound_id.clone(), change).await,
                Ok(Ok(_))
            ) {
                eprintln!("clarp-host: a binding the owner approved was not kept");
            }
        }
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    fn record(session: &str, home: &str, provider: &str) -> AgentRecord {
        AgentRecord {
            agent_id: format!("id-{session}"),
            session: session.to_string(),
            persona: session.to_uppercase(),
            backend: "codex".to_string(),
            codex_home: home.to_string(),
            codex_provider: provider.to_string(),
            ..AgentRecord::default()
        }
    }

    #[test]
    fn bound_privacy_readers_are_the_owner_itself_its_binding_and_those_let_in() {
        let mut bound = record("ada", "/homes/ada", "theirs");
        let free = record("ben", "", "");
        let twin = record("cid", "/homes/ada", "theirs");
        let other_home = record("dot", "/homes/dot", "theirs");
        assert!(may_read(&Reader::Owner, &bound));
        assert!(may_read(&Reader::agent(&bound), &bound));
        assert!(may_read(&Reader::agent(&twin), &bound));
        assert!(!may_read(&Reader::agent(&free), &bound));
        assert!(!may_read(&Reader::agent(&other_home), &bound));
        assert!(!may_read(&Reader::Unknown, &bound));
        assert!(!may_read(&Reader::Elsewhere, &bound));
        // Every reader reads an agent that is not bound.
        assert!(may_read(&Reader::Unknown, &free));
        assert!(may_read(&Reader::agent(&bound), &free));
        bound.readers.push(free.agent_id.clone());
        assert!(may_read(&Reader::agent(&free), &bound));
    }

    /// A helper's words stand in its maker's line only where every reader of the maker reads
    /// the helper too (issue 819).
    #[test]
    fn bound_privacy_a_helper_is_read_with_its_maker_only_where_its_makers_readers_read_it() {
        let free_maker = record("ben", "", "");
        let mut bound_maker = record("ada", "/homes/ada", "theirs");
        let free_helper = record("hal", "", "");
        let mut twin_helper = record("tia", "/homes/ada", "theirs");
        let own_helper = record("ola", "/homes/ola", "theirs");
        // A helper nobody is kept from reads with any maker.
        assert!(read_with(&free_helper, &free_maker));
        assert!(read_with(&free_helper, &bound_maker));
        // A bound helper of a maker that everyone reads, or one bound otherwise, does not.
        assert!(!read_with(&twin_helper, &free_maker));
        assert!(!read_with(&own_helper, &bound_maker));
        // Bound as its maker is: it does, until the maker lets in a reader it does not.
        assert!(read_with(&twin_helper, &bound_maker));
        bound_maker.readers.push(free_maker.agent_id.clone());
        assert!(!read_with(&twin_helper, &bound_maker));
        twin_helper.readers.push(free_maker.agent_id.clone());
        assert!(read_with(&twin_helper, &bound_maker));
        // The helper itself among its maker's readers is no reader it lacks.
        bound_maker.readers.push(twin_helper.agent_id.clone());
        assert!(read_with(&twin_helper, &bound_maker));
    }

    #[test]
    fn bound_privacy_only_the_owner_changes_a_bound_agents_binding() {
        let bound = record("ada", "/homes/ada", "theirs");
        let free = record("ben", "", "");
        let twin = record("cid", "/homes/ada", "theirs");
        let unbound = Binding {
            home: String::new(),
            provider: String::new(),
            cloud_speech: None,
        };
        let speaking = Binding {
            cloud_speech: Some(true),
            ..Binding::of(&bound)
        };
        for to in [&unbound, &speaking] {
            assert!(!must_ask(&Reader::Owner, &bound, to));
            // Not even an agent bound as it is, nor one that cannot say whose turn it is.
            for reader in [Reader::agent(&free), Reader::agent(&twin), Reader::Unknown] {
                assert!(must_ask(&reader, &bound, to), "{reader:?}");
            }
        }
        // The binding as it is, asked again, and an agent that is not bound, ask nobody.
        assert!(!must_ask(
            &Reader::agent(&free),
            &bound,
            &Binding::of(&bound)
        ));
        assert!(!must_ask(&Reader::agent(&free), &free, &speaking));
    }

    #[test]
    fn bound_privacy_blanks_a_withheld_agents_objects_and_keeps_its_name_and_state() {
        let bound = record("ada", "/homes/ada", "theirs");
        let free = record("ben", "", "");
        let withheld = Withheld::among([&bound, &free], &Reader::agent(&free)).unwrap();
        let mut answer = json!({"agents": [
            {"session": "ada", "agent_id": "id-ada", "name": "ADA", "state": "working",
             "busy": true, "last_message": "the customer's figures",
             "status_text": "Reading the ledger", "updated_at": 5,
             "live_activity": {"headline": "Running tests", "kind": "tool"},
             "tags": ["secret"]},
            {"session": "ben", "agent_id": "id-ben", "last_message": "hello"}
        ]});
        assert!(withheld.walk(&mut answer));
        let ada = &answer["agents"][0];
        assert_eq!(ada["session"], "ada");
        assert_eq!(ada["name"], "ADA");
        assert_eq!(ada["state"], "working");
        assert_eq!(ada["busy"], true);
        assert_eq!(ada["updated_at"], 5);
        assert_eq!(ada["last_message"], "");
        assert_eq!(ada["status_text"], "");
        assert_eq!(ada["live_activity"]["headline"], "");
        assert_eq!(ada["live_activity"]["kind"], "tool");
        assert_eq!(ada["tags"], json!([]));
        assert_eq!(ada["words_withheld"], true);
        assert_eq!(answer["agents"][1]["last_message"], "hello");
        assert!(answer["agents"][1].get("words_withheld").is_none());
        // A reader that may read everything has nothing withheld.
        assert!(Withheld::among([&bound, &free], &Reader::agent(&bound)).is_none());
    }

    #[test]
    fn bound_privacy_blanks_a_withheld_agents_events_in_a_stream() {
        let bound = record("ada", "/homes/ada", "theirs");
        let withheld = Withheld::among([&bound], &Reader::Unknown).unwrap();
        let frame =
            b"id: 4\ndata: {\"type\":\"log-row\",\"session\":\"ada\",\"text\":\"secret\"}\n\n";
        let out = String::from_utf8(withheld.frame(frame)).unwrap();
        assert!(out.starts_with("id: 4\ndata: "), "{out}");
        assert!(!out.contains("secret"), "{out}");
        assert!(out.ends_with("\n\n"), "{out}");
        let other = b"data: {\"type\":\"log-row\",\"session\":\"ben\",\"text\":\"fine\"}\n\n";
        assert_eq!(withheld.frame(other), other.to_vec());
    }

    #[test]
    fn bound_privacy_blanks_a_withheld_agents_chat_preview_as_its_row() {
        // A chat's preview on the event stream (feature `events_chat_previews`) names its
        // agent as its row does: a reader who may not read it gets its ids, numbers and state,
        // and no words.
        let bound = record("ada", "/homes/ada", "theirs");
        let withheld = Withheld::among([&bound], &Reader::Unknown).unwrap();
        let preview = |session: &str| {
            let event = json!({
                "type": "agent-roster", "kind": "preview", "agent_id": format!("id-{session}"),
                "session": session,
                "fields": {
                    "conversation_id": "c1", "head_revision": 7, "last_message_id": "m7",
                    "last_message": "secret", "busy": true, "latest_state": "tool",
                    "latest_state_ts": 5, "status_text": "Reading secret.md",
                    "last_activity": 5,
                },
            });
            format!("data: {event}\n\n")
        };
        let out = String::from_utf8(withheld.frame(preview("ada").as_bytes())).unwrap();
        assert!(out.starts_with("data: ") && !out.contains("id: "), "{out}");
        let event: Value =
            serde_json::from_str(out.strip_prefix("data: ").unwrap().trim_end()).unwrap();
        assert!(!out.contains("secret"), "{out}");
        assert_eq!(event["kind"], "preview");
        assert_eq!(event["words_withheld"], true);
        let fields = &event["fields"];
        assert_eq!(fields["last_message"], "");
        assert_eq!(fields["status_text"], "");
        assert_eq!(
            (&fields["conversation_id"], &fields["last_message_id"]),
            (&json!("c1"), &json!("m7"))
        );
        assert_eq!(fields["head_revision"], 7);
        assert_eq!(fields["busy"], true);
        assert_eq!(fields["latest_state"], "tool");
        assert_eq!(fields["latest_state_ts"], 5);
        // A readable agent's preview goes as it is.
        let open = preview("ben");
        assert_eq!(withheld.frame(open.as_bytes()), open.as_bytes().to_vec());
    }

    #[test]
    fn bound_privacy_leaves_out_the_rows_a_withheld_agents_event_carries() {
        let bound = record("ada", "/homes/ada", "theirs");
        let withheld = Withheld::among([&bound], &Reader::Unknown).unwrap();
        let frame = concat!(
            "id: 9\ndata: {\"type\":\"transcript-updated\",\"session\":\"ada\",",
            "\"head_revision\":12,\"rows_from\":10,\"rows_latest\":12,",
            "\"rows_coordination\":{\"visibility\":\"hide\"},\"hidden_after\":{\"count\":2},",
            "\"rows\":[{\"id\":\"r1\",\"role\":\"assistant\",\"revision\":11,",
            "\"timestamp\":\"2026-11-26T05:00:00Z\",\"trace_id\":\"t1\",\"text\":\"secret\"}]}\n\n"
        );
        let out = String::from_utf8(withheld.frame(frame.as_bytes())).unwrap();
        let data = out.strip_prefix("id: 9\ndata: ").unwrap().trim_end();
        let event: Value = serde_json::from_str(data).unwrap();
        assert!(
            ["rows", "rows_latest", "rows_coordination", "hidden_after"]
                .iter()
                .all(|field| event.get(field).is_none()),
            "{event}"
        );
        assert_eq!(
            event["rows_from"], 10,
            "where the rows begin stays: the client reads /log"
        );
        assert_eq!(event["head_revision"], 12);
        assert_eq!(event["words_withheld"], true);
        // A readable agent's rows go as they are.
        let open = frame.replace("\"ada\"", "\"ben\"");
        assert_eq!(withheld.frame(open.as_bytes()), open.as_bytes().to_vec());
    }

    #[test]
    fn bound_privacy_messages_and_team_updates_of_a_bound_agent_carry_no_words() {
        let bound = record("ada", "/homes/ada", "theirs");
        let twin = record("cid", "/homes/ada", "theirs");
        let free = record("ben", "", "");
        let said = withheld_message(&bound, &free).unwrap();
        assert!(said.starts_with("ADA sent you a message."), "{said}");
        assert!(withheld_message(&bound, &twin).is_none());
        assert!(withheld_message(&free, &bound).is_none());
        assert!(withheld_update(&bound, &free).is_some());
        assert!(withheld_update(&bound, &twin).is_none());
    }

    /// Every `GET` route the Host declares, by the path it is declared with: read from the
    /// sources, so a route added anywhere is seen.
    fn declared_reads() -> Vec<(String, String)> {
        let src = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("src");
        let mut files = vec![src];
        let mut found = Vec::new();
        while let Some(path) = files.pop() {
            if path.is_dir() {
                for entry in std::fs::read_dir(&path).unwrap() {
                    files.push(entry.unwrap().path());
                }
                continue;
            }
            if path.extension().is_none_or(|ext| ext != "rs") {
                continue;
            }
            let text = std::fs::read_to_string(&path).unwrap();
            // The routes a test builds for itself are no routes of the Host.
            let text = text.split("#[cfg(test)]").next().unwrap_or_default();
            let mut rest = text;
            while let Some(at) = rest.find(".route(") {
                rest = &rest[at + ".route(".len()..];
                // The call's arguments, to its closing parenthesis.
                let mut depth = 1_i32;
                let mut end = 0;
                for (index, character) in rest.char_indices() {
                    match character {
                        '(' => depth += 1,
                        ')' => depth -= 1,
                        _ => {}
                    }
                    if depth == 0 {
                        end = index;
                        break;
                    }
                }
                let arguments = rest[..end].trim_start();
                let file = path.display().to_string();
                let Some((named, handler)) = arguments.split_once(',') else {
                    continue;
                };
                if !(handler.contains("get(") || handler.contains("any(")) {
                    continue;
                }
                let named = named.trim();
                if let Some(route) = named.strip_prefix('"').and_then(|r| r.strip_suffix('"')) {
                    if route.starts_with('/') {
                        found.push((route.to_string(), file));
                    }
                    continue;
                }
                // A path in a variable: the paths of the loop that gives it, else the
                // variable itself, which no class names, so the test says where it is.
                let name = named.trim_start_matches('&');
                let before = &text[..text.len() - rest.len()];
                let looped = before.rfind(&format!("for {name} in [")).map(|at| {
                    let list = &before[at..];
                    let list = &list[list.find('[').map_or(0, |open| open + 1)..];
                    let list = &list[..list.find(']').unwrap_or(list.len())];
                    let quoted = list.split('"').skip(1).step_by(2);
                    quoted.map(str::to_string).collect::<Vec<_>>()
                });
                match looped.filter(|paths| !paths.is_empty()) {
                    Some(paths) => found.extend(paths.into_iter().map(|p| (p, file.clone()))),
                    None => found.push((format!("<the path in {name}>"), file)),
                }
            }
        }
        found
    }

    #[test]
    fn bound_privacy_every_read_route_has_a_class() {
        let declared = declared_reads();
        assert!(declared.len() > 100, "found {} routes", declared.len());
        let unclassed: Vec<String> = declared
            .iter()
            .filter(|(route, _)| class_of(route).is_none())
            .map(|(route, file)| format!("{route} ({file})"))
            .collect();
        assert!(
            unclassed.is_empty(),
            "GET routes without a class in bound_privacy::CLASSES (say whether they answer \
             an agent's words): {unclassed:?}"
        );
        let stale: Vec<&str> = CLASSES
            .iter()
            .map(|(route, _)| *route)
            .filter(|route| !declared.iter().any(|(declared, _)| declared == route))
            .collect();
        assert!(
            stale.is_empty(),
            "classes of routes that are gone: {stale:?}"
        );
    }
}
