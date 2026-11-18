//! The Oracle's delegations: the record of what it handed to agents.
//!
//! What the Oracle is asked to look into it hands to an agent as a turn of that agent's. The
//! Host keeps a record of each such delegation from the moment it is taken until its result
//! was delivered: taken once however often it is asked, and refused where the same id comes
//! with other work; waiting in the agent's queue or under way; ended by the turn's answer,
//! by its failure or by a stop; and shown to the phone that asked until that phone says it
//! has it. A delegation that is over never changes again, but for one correction: a stop
//! calls off one that had failed while it was being stopped.
//!
//! These are the Python Host's rules (`server/lib/oracle_delegations.py`) without a store:
//! the book is the rows, and says which of them changed. What that Host asks of the rest of
//! itself is given here: the agent a session names, whether a message of the delegation's
//! own id is in the conversation, what the queue has, what became of a turn. Held to that
//! Host's own module by `tests/fixtures/oracle-delegation-vectors.json`.

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use crate::python;

/// Whose a delegation is where nobody else asked: the Host's own administrator.
pub const ADMINISTRATOR: &str = "administrator";
/// How long an id may be, a reason that is kept, and what an agent is told of a request.
const ID_MOST: usize = 80;
const REASON_MOST: usize = 500;
const REQUEST_MOST: usize = 8000;
/// How many are shown at most, and where no number is asked for.
const SHOWN_MOST: i64 = 200;
pub const SHOWN_USUALLY: i64 = 50;
/// How many an agent is told it still owes.
const OWED_MOST: usize = 20;
const NO_REASON: &str = "Agent turn failed";
const INTERRUPTED: &str = "Agent turn was interrupted before a durable result; delegate it again";
/// What follows a request that is for the agent's eyes and not part of what it was asked.
const REFERENCE_DATA: &str = "\n\n<oracle-reference-data>";
/// What an agent's live call asks its user's Oracle is told only into that call: such a
/// delegation is known by its id and by how its request begins, and is shown to no phone.
const LIVE_ASK: &str = "sandra-";
const LIVE_ASK_SIGNS: usize = 24;
const LIVE_ASK_BEGINS: &str = "Sp\u{f8}rsm\u{e5}l fra ";
const LIVE_ASK_HAS: &str = "Live-samtale";

/// Where a delegation is.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Status {
    /// Taken; its turn is under way, or about to be.
    Accepted,
    /// Its turn waits in the agent's queue.
    Queued,
    Completed,
    Failed,
    Cancelled,
}

impl Status {
    /// Not over yet.
    pub fn active(self) -> bool {
        matches!(self, Self::Accepted | Self::Queued)
    }

    pub fn word(self) -> &'static str {
        match self {
            Self::Accepted => "accepted",
            Self::Queued => "queued",
            Self::Completed => "completed",
            Self::Failed => "failed",
            Self::Cancelled => "cancelled",
        }
    }
}

/// One delegation, as it is kept.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Delegation {
    pub delegation_id: String,
    /// Who asked: the administrator, or a phone by its id.
    pub owner_principal: String,
    /// The id its turn is followed by, and the id of the message that begins the turn.
    pub trace_id: String,
    pub client_msg_id: String,
    pub agent_id: String,
    pub session: String,
    /// The session of the agent's tool, as it was when the turn was admitted.
    pub backend_session_id: String,
    pub request_text: String,
    pub status: Status,
    pub result_message_id: Option<String>,
    pub result_text: String,
    pub error: String,
    pub created_at: i64,
    pub updated_at: i64,
    /// When the phone that asked said it has the result.
    pub delivered_at: Option<i64>,
}

impl Delegation {
    /// The delegation as a client is told it.
    pub fn told(&self) -> Value {
        json!({
            "completion_trace_id": "", "delegation_id": self.delegation_id,
            "owner_principal": self.owner_principal, "trace_id": self.trace_id,
            "client_msg_id": self.client_msg_id, "agent_id": self.agent_id,
            "session": self.session, "backend_session_id": self.backend_session_id,
            "request_text": self.request_text, "status": self.status.word(),
            "result_message_id": self.result_message_id, "result_text": self.result_text,
            "error": self.error, "created_at": self.created_at, "updated_at": self.updated_at,
            "delivered_at": self.delivered_at, "delivered": self.delivered_at.is_some(),
        })
    }

    /// One an agent's live call asked: shown to no phone.
    fn of_a_live_call(&self) -> bool {
        let signs = self.delegation_id.strip_prefix(LIVE_ASK);
        let by_id = signs.is_some_and(|signs| {
            signs.len() == LIVE_ASK_SIGNS
                && signs
                    .bytes()
                    .all(|sign| matches!(sign, b'0'..=b'9' | b'a'..=b'f'))
        });
        by_id && begins_as_a_live_ask(&self.request_text)
    }
}

/// Whether two signs are the same to a database that folds the case of the Latin letters
/// of ASCII and of no others (SQLite's `LIKE`).
fn alike(one: char, other: char) -> bool {
    one == other || (one.is_ascii() && one.eq_ignore_ascii_case(&other))
}

/// Whether a request begins as a live call's question does and names the call after that.
fn begins_as_a_live_ask(request: &str) -> bool {
    let mut signs = request.chars();
    let begins = LIVE_ASK_BEGINS
        .chars()
        .all(|wanted| signs.next().is_some_and(|sign| alike(sign, wanted)));
    if !begins {
        return false;
    }
    let rest: Vec<char> = signs.collect();
    let wanted: Vec<char> = LIVE_ASK_HAS.chars().collect();
    rest.windows(wanted.len()).any(|here| {
        here.iter()
            .zip(&wanted)
            .all(|(sign, wanted)| alike(*sign, *wanted))
    })
}

/// Why something asked of the book is refused: the status a route answers, and the words.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Refused {
    pub status: u16,
    pub why: &'static str,
}

fn refused(status: u16, why: &'static str) -> Refused {
    Refused { status, why }
}

/// The id of a delegation as it is kept: without the blanks around it, of letters, digits,
/// dots, underscores and hyphens, eighty at most. `Err`: it is none.
pub fn normalize_id(said: &str) -> Result<&str, Refused> {
    let id = python::strip(said);
    let sign = |sign: u8| sign.is_ascii_alphanumeric() || matches!(sign, b'.' | b'_' | b'-');
    if id.is_empty() || id.len() > ID_MOST || !id.bytes().all(sign) {
        return Err(refused(400, "invalid delegation_id"));
    }
    Ok(id)
}

/// How many are shown for a number that was asked for.
fn shown(limit: i64) -> usize {
    usize::try_from(limit.clamp(1, SHOWN_MOST)).unwrap_or(1)
}

/// What a route is asked to hand to an agent.
#[derive(Debug, Clone, Copy)]
pub struct Asked<'a> {
    pub delegation_id: &'a str,
    pub session: &'a str,
    pub request: &'a str,
    pub owner: &'a str,
}

/// The agent a session names, as far as a delegation minds.
#[derive(Debug, Clone, Copy)]
pub struct Agent<'a> {
    pub agent_id: &'a str,
    pub janitor: bool,
}

/// A turn that is to be admitted for a delegation: what the agent is sent, and under which
/// ids. It is sent as the Oracle's, without speech, and waits where the agent is busy.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Admit {
    pub delegation_id: String,
    pub session: String,
    pub text: String,
    pub trace_id: String,
    pub client_msg_id: String,
}

/// What follows a delegation that was asked for.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Taken {
    /// Nothing: it is over, or its turn has begun. The delegation as it is.
    AsItIs(Delegation),
    /// Its turn is to be admitted; what became of that is said after ([`Book::dispatched`],
    /// [`Book::fail`]).
    ToAdmit(Admit),
}

/// A delegation a stop of its agent is to call off.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ToCallOff {
    pub delegation_id: String,
    pub trace_id: String,
    pub client_msg_id: String,
    /// Its turn waited in the queue: it is taken out of it.
    pub waited: bool,
}

/// What became of the turn of a delegation that is not over, when its Host looks again.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Fate {
    /// No message of it is in the conversation: its turn is still the queue's to start.
    NotAdmitted,
    /// Its turn runs.
    Live,
    /// Its turn is over and left an answer, at this moment.
    Said {
        message_id: String,
        text: String,
        at_ms: i64,
    },
    /// Its turn is gone and left none.
    Lost,
}

/// The delegations of a Host, in the order they were taken.
#[derive(Debug, Clone, Default)]
pub struct Book {
    rows: Vec<Delegation>,
    /// The places of the rows that changed since the Host last asked.
    changed: Vec<usize>,
}

impl Book {
    /// The book as it was kept.
    pub fn kept(mut rows: Vec<Delegation>) -> Self {
        rows.sort_by(|one, other| {
            let by_time = one.created_at.cmp(&other.created_at);
            by_time.then_with(|| one.delegation_id.cmp(&other.delegation_id))
        });
        Self {
            rows,
            changed: Vec::new(),
        }
    }

    pub fn rows(&self) -> &[Delegation] {
        &self.rows
    }

    /// The rows that changed since this was last asked, for the Host to write.
    pub fn written(&mut self) -> Vec<Delegation> {
        let mut places = std::mem::take(&mut self.changed);
        places.sort_unstable();
        places.dedup();
        places.iter().map(|at| self.rows[*at].clone()).collect()
    }

    pub fn get(&self, delegation_id: &str) -> Option<&Delegation> {
        let id = python::strip(delegation_id);
        self.rows.iter().find(|row| row.delegation_id == id)
    }

    /// Every row a rule holds for moves on: its state is set, the moment stamped, and what
    /// else is to change is changed. Answers how many moved.
    fn moved(
        &mut self,
        holds: impl Fn(&Delegation) -> bool,
        to: Status,
        now_ms: i64,
        change: impl Fn(&mut Delegation),
    ) -> usize {
        let mut count = 0;
        for (at, row) in self.rows.iter_mut().enumerate() {
            if holds(row) {
                row.status = to;
                row.updated_at = now_ms;
                change(row);
                self.changed.push(at);
                count += 1;
            }
        }
        count
    }

    /// A delegation is asked for (`POST /oracle/delegations`). `agent`: the agent the
    /// session names, without the blanks around it ([`python::strip`]), where there is
    /// one. `has_message`: a message with the delegation's own id is in the conversation,
    /// which is to say that its turn has begun. `Err`: it is refused, and nothing is kept.
    pub fn take(
        &mut self,
        asked: &Asked,
        agent: Option<Agent>,
        has_message: bool,
        now_ms: i64,
    ) -> Result<Taken, Refused> {
        let id = normalize_id(asked.delegation_id)?;
        let (session, request) = (python::strip(asked.session), python::strip(asked.request));
        if session.is_empty() || request.is_empty() {
            return Err(refused(400, "session and request are required"));
        }
        let agent = agent.ok_or_else(|| refused(404, "unknown agent"))?;
        if agent.janitor {
            return Err(refused(400, "Janitors cannot receive Oracle delegations"));
        }
        let owner = python::strip(asked.owner);
        if agent.agent_id.is_empty() || owner.is_empty() {
            return Err(refused(
                400,
                "complete Oracle delegation identity is required",
            ));
        }
        let trace_id = format!("oracle-{id}");
        let admit = Admit {
            delegation_id: id.to_string(),
            session: session.to_string(),
            text: request.to_string(),
            trace_id: trace_id.clone(),
            client_msg_id: trace_id.clone(),
        };
        if let Some(there) = self.rows.iter().find(|row| row.delegation_id == id) {
            let same = there.agent_id == agent.agent_id
                && there.session == session
                && there.request_text == request
                && there.owner_principal == owner;
            if !same {
                let why = "delegation_id was already used for different work";
                return Err(refused(409, why));
            }
            // One that is over is a record of what was delivered, and running it again
            // could do twice what cannot be undone; one whose turn has begun is under way.
            if !there.status.active() || has_message {
                return Ok(Taken::AsItIs(there.clone()));
            }
            return Ok(Taken::ToAdmit(admit));
        }
        self.changed.push(self.rows.len());
        self.rows.push(Delegation {
            delegation_id: id.to_string(),
            owner_principal: owner.to_string(),
            trace_id: trace_id.clone(),
            client_msg_id: trace_id,
            agent_id: agent.agent_id.to_string(),
            session: session.to_string(),
            backend_session_id: String::new(),
            request_text: request.to_string(),
            status: Status::Accepted,
            result_message_id: None,
            result_text: String::new(),
            error: String::new(),
            created_at: now_ms,
            updated_at: now_ms,
            delivered_at: None,
        });
        Ok(Taken::ToAdmit(admit))
    }

    /// The turn of a delegation was admitted. `waits`: it was put into the agent's queue
    /// and is still there, not begun. Answers the delegation as it is now.
    pub fn dispatched(
        &mut self,
        delegation_id: &str,
        backend_session_id: &str,
        waits: bool,
        now_ms: i64,
    ) -> Option<&Delegation> {
        let id = python::strip(delegation_id);
        let to = if waits {
            Status::Queued
        } else {
            Status::Accepted
        };
        let holds = |row: &Delegation| row.delegation_id == id && row.status.active();
        self.moved(holds, to, now_ms, |row| {
            row.backend_session_id = backend_session_id.to_string();
        });
        self.get(id)
    }

    /// A turn that waited in the queue has begun.
    pub fn started(&mut self, trace_id: &str, now_ms: i64) -> bool {
        let trace = python::strip(trace_id);
        let holds = |row: &Delegation| row.trace_id == trace && row.status == Status::Queued;
        self.moved(holds, Status::Accepted, now_ms, |_| {}) > 0
    }

    /// A turn ended with an answer: it is the result of the delegation the turn was for.
    pub fn completed(&mut self, trace_id: &str, message_id: &str, text: &str, now_ms: i64) -> bool {
        let clean = python::strip(text);
        if trace_id.is_empty() || message_id.is_empty() || clean.is_empty() {
            return false;
        }
        let trace = python::strip(trace_id);
        let holds = |row: &Delegation| row.trace_id == trace && row.status.active();
        let moved = self.moved(holds, Status::Completed, now_ms, |row| {
            row.result_message_id = Some(message_id.to_string());
            row.result_text = clean.to_string();
            row.error = String::new();
        });
        moved > 0
    }

    /// The reason a delegation failed, as it is kept.
    fn reason(reason: &str) -> String {
        let said = if reason.is_empty() { NO_REASON } else { reason };
        python::first(said, REASON_MOST).to_string()
    }

    /// A delegation failed. Answers it as it is now: one that was over is as it was.
    pub fn fail(&mut self, delegation_id: &str, reason: &str, now_ms: i64) -> Option<&Delegation> {
        let id = python::strip(delegation_id);
        let error = Self::reason(reason);
        let holds = |row: &Delegation| row.delegation_id == id && row.status.active();
        self.moved(holds, Status::Failed, now_ms, |row| {
            row.error.clone_from(&error);
        });
        self.get(id)
    }

    /// A turn failed: so did the delegation it was for.
    pub fn failed_for_trace(&mut self, trace_id: &str, reason: &str, now_ms: i64) -> bool {
        let trace = python::strip(trace_id);
        let error = Self::reason(reason);
        let holds = |row: &Delegation| row.trace_id == trace && row.status.active();
        let moved = self.moved(holds, Status::Failed, now_ms, |row| {
            row.error.clone_from(&error);
        });
        moved > 0
    }

    /// The Host looks again at the delegations that are not over, each asked what became
    /// of its turn: one whose turn is over is ended with the answer it left, at the moment
    /// that was left; one whose turn is gone without one failed, now. Answers how many
    /// were ended.
    pub fn orphans(&mut self, mut fate: impl FnMut(&Delegation) -> Fate, now_ms: i64) -> usize {
        let open: Vec<usize> = (0..self.rows.len())
            .filter(|at| self.rows[*at].status.active())
            .collect();
        let mut ended = 0;
        for at in open {
            let row = self.rows[at].clone();
            let said = match fate(&row) {
                Fate::NotAdmitted | Fate::Live => continue,
                Fate::Said {
                    message_id,
                    text,
                    at_ms,
                } => self.completed(&row.trace_id, &message_id, &text, at_ms),
                Fate::Lost => false,
            };
            // An answer that says nothing is none: the delegation failed.
            let over = said || {
                let failed = self.fail(&row.delegation_id, INTERRUPTED, now_ms);
                failed.is_some_and(|row| row.status == Status::Failed)
            };
            ended += usize::from(over);
        }
        ended
    }

    /// A delegation is called off by its id (`POST /oracle/delegations/cancel`): only one
    /// that is over, which is told as it is. `Ok(None)`: the one who asks has none of the
    /// id. `Err`: its turn still runs or waits, and that is the agent's to stop.
    pub fn cancel(&self, delegation_id: &str, owner: &str) -> Result<Option<&Delegation>, Refused> {
        let found = self.get(delegation_id);
        let Some(row) = found.filter(|row| row.owner_principal == owner) else {
            return Ok(None);
        };
        if row.status.active() {
            let why = "nonterminal delegation requires agent cancellation";
            return Err(refused(409, why));
        }
        Ok(Some(row))
    }

    /// The delegations a stop of an agent is to call off: what the one who asks handed to
    /// that session and is not over, the one that changed last first. The agent is stopped
    /// after this and before [`Book::called_off`].
    pub fn to_call_off(&self, session: &str, owner: &str) -> Result<Vec<ToCallOff>, Refused> {
        let (session, owner) = (python::strip(session), python::strip(owner));
        if session.is_empty() || owner.is_empty() {
            return Err(refused(400, "session and owner principal are required"));
        }
        let mut found: Vec<&Delegation> = self
            .rows
            .iter()
            .filter(|row| {
                row.session == session && row.owner_principal == owner && row.status.active()
            })
            .collect();
        found.sort_by_key(|row| std::cmp::Reverse(row.updated_at));
        let told = found.into_iter().map(|row| ToCallOff {
            delegation_id: row.delegation_id.clone(),
            trace_id: row.trace_id.clone(),
            client_msg_id: row.client_msg_id.clone(),
            waited: row.status == Status::Queued,
        });
        Ok(told.collect())
    }

    /// The agent was stopped: exactly the delegations that were to be called off are, also
    /// one that failed while its agent was being stopped. One that ended with an answer
    /// meanwhile stays ended. Answers those that are called off, in the order given.
    pub fn called_off(&mut self, ids: &[String], owner: &str, now_ms: i64) -> Vec<Delegation> {
        let owner = python::strip(owner);
        let holds = |row: &Delegation| {
            ids.contains(&row.delegation_id)
                && row.owner_principal == owner
                && (row.status.active() || row.status == Status::Failed)
        };
        self.moved(holds, Status::Cancelled, now_ms, |_| {});
        let called_off = ids.iter().filter_map(|id| self.get(id));
        called_off
            .filter(|row| row.status == Status::Cancelled)
            .cloned()
            .collect()
    }

    /// What a phone is still to be told (`GET /oracle/delegations`): the delegations of the
    /// one who asks that are over and not delivered, the one that ended first first.
    pub fn undelivered(&self, owner: &str, limit: i64) -> Vec<&Delegation> {
        let mut found: Vec<&Delegation> = self
            .rows
            .iter()
            .filter(|row| {
                row.delivered_at.is_none()
                    && row.owner_principal == owner
                    && !row.status.active()
                    && !row.of_a_live_call()
            })
            .collect();
        found.sort_by(|one, other| {
            let by_time = one.updated_at.cmp(&other.updated_at);
            by_time.then_with(|| one.delegation_id.cmp(&other.delegation_id))
        });
        found.truncate(shown(limit));
        found
    }

    /// The phone that asked says it has a result (`POST /oracle/delegations/ack`). `false`:
    /// the one who asks has no delegation of the id that is over.
    pub fn acknowledge(&mut self, delegation_id: &str, owner: &str, now_ms: i64) -> bool {
        let id = python::strip(delegation_id);
        let found = self.rows.iter().position(|row| {
            row.delegation_id == id && row.owner_principal == owner && !row.status.active()
        });
        let Some(at) = found else {
            return false;
        };
        let row = &mut self.rows[at];
        row.delivered_at = row.delivered_at.or(Some(now_ms));
        row.updated_at = now_ms;
        self.changed.push(at);
        true
    }

    /// Every delegation of the one who asks (`GET /oracle/delegations?all=1`), the one that
    /// changed last first.
    pub fn recent(&self, owner: &str, limit: i64) -> Vec<&Delegation> {
        let mut found: Vec<&Delegation> = self
            .rows
            .iter()
            .filter(|row| row.owner_principal == owner)
            .collect();
        found.sort_by(|one, other| {
            let by_time = other.updated_at.cmp(&one.updated_at);
            by_time.then_with(|| other.delegation_id.cmp(&one.delegation_id))
        });
        found.truncate(shown(limit));
        found
    }

    /// What an agent still owes the Oracle in the turn it runs: the requests of the
    /// delegations that turn is for, in the order they were taken, each without what was
    /// added for the agent's eyes only.
    pub fn owed(&self, agent_id: &str, trace_id: &str) -> Vec<Value> {
        if agent_id.is_empty() || trace_id.is_empty() {
            return Vec::new();
        }
        let mut found: Vec<&Delegation> = self
            .rows
            .iter()
            .filter(|row| {
                row.agent_id == agent_id && row.trace_id == trace_id && row.status.active()
            })
            .collect();
        found.sort_by(|one, other| {
            let by_time = one.created_at.cmp(&other.created_at);
            by_time.then_with(|| one.delegation_id.cmp(&other.delegation_id))
        });
        let owed = found.into_iter().take(OWED_MOST).map(|row| {
            let asked = row.request_text.split(REFERENCE_DATA).next().unwrap_or("");
            json!({
                "delegation_id": row.delegation_id,
                "request": python::first(asked, REQUEST_MOST),
            })
        });
        owed.collect()
    }
}
