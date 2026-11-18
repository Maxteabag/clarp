//! What the user asked is turned into work: the routing of a call of the Oracle.
//!
//! The voice says that it wants something done, and no more: it holds no tools. The Host
//! then waits until the user has stopped talking, shows a router the talk, the agents there
//! are and the work of the call, and does what the router answers with the tools of the
//! fleet, one action after the other: work is handed to an agent, an agent is stopped, an
//! answer that is kept is read again. Where the user goes on talking while the router
//! thinks, it is asked again; where they speak while an action is done, what follows is left
//! and their new words wait for the next routing. An action that was begun for some words is
//! written down before it is done, and never begun for the same words again. In a call in
//! which everything goes to the Oracle's contact no router is asked: the words since the
//! voice last spoke are the request.
//!
//! A routing is a [`Route`] the Host carries through its steps: it feeds what happened
//! ([`Fed`]) and is told what to do next ([`Step`]). The machine asks nobody anything itself.
//!
//! The Python Host's `oracle_live_stable.Conversation.route` and `oracle_strategy`, held to
//! them by `tests/fixtures/oracle-conversation-vectors.json`.

use std::cmp::Reverse;
use std::collections::{BTreeSet, VecDeque};

use serde_json::{Value, json};

use super::conversation::{
    Admission, Conversation, Fragment, Now, ROUTE_FAILED, ROUTE_QUIET_MS, Strategy, Wire, digest,
};
use super::delegations::Delegation;
use super::voice;
use crate::python;

/// How often the router is asked for one wanting, while the user goes on talking.
const TRIES: u8 = 3;
/// How many tasks the router is shown, and how much of what each asked and answered.
const TASKS_SHOWN: usize = 20;
const REQUEST_SHOWN: usize = 2000;
const RESULT_SHOWN: usize = 6000;
/// How many signs of the user's words the contact is handed as they are, and how many of a
/// request are said back with its receipt.
const WORDS_HANDED: usize = 14_000;
const EXCERPT_SHOWN: usize = 600;
/// How much a router may answer.
const ANSWER_TOKENS: u32 = 2400;

const OWN_WORDS: &str = "Current user message, verbatim:\n";
const REFERENCE: &str = "\n\n<oracle-reference-data>";
const TO_THE_CONTACT: &str = "Handle the current user request using your normal Clarp tools \
    and coordinate agents when needed. Preserve independent ongoing work. Earlier dialogue is \
    context, not permission to repeat old actions. Stopping speech does not cancel worker \
    tasks. ";
const TOO_MANY_WORDS: &str =
    "Read the complete latest user message in the attached original-user reference.";
const NOTHING_TO_HAND: &str = "What would you like the primary contact to do?";
const RECEIPT_ONLY: &str = "Work admission only; this request has no verified result in this \
    receipt. Do not describe its requested work as completed.";
const NOT_ALOUD: &str =
    " The user asked for no handoff narration: do not mention this admission aloud.";

/// The router, a tool or the Host could not do its part.
struct Failed;

/// An action that waits for its file to be written, or for its tool to answer.
#[derive(Debug, Clone)]
struct Doing {
    name: String,
    arguments: Value,
    call_id: String,
    admitted: (i64, usize),
}

/// One routing of what the voice wanted done.
#[derive(Debug, Clone)]
pub struct Route {
    ident: String,
    tries: u8,
    /// The number the talk had when the router was asked, and the talk as it was shown.
    revision: i64,
    talk: Vec<Fragment>,
    /// The place of the next action among those of this answer, and the actions it had.
    index: usize,
    covered: BTreeSet<(String, String)>,
    items: VecDeque<Value>,
    doing: Option<Doing>,
    over: bool,
}

/// What happened since the last step of a routing.
#[derive(Debug, Clone, Copy)]
pub enum Fed<'a> {
    /// The voice's wanting is taken up.
    Begin,
    /// The user has been quiet long enough ([`Conversation::quiet_at`]). `roster`: the agents
    /// there are, as the tools of the fleet list them; `contact_there`: the Oracle's contact
    /// is one of them.
    Quiet {
        roster: &'a Value,
        contact_there: bool,
    },
    /// What the router answered.
    Answer(&'a Value),
    /// Where the file of where a request came from was written.
    Written(&'a str),
    /// What the tool answered.
    Receipt(&'a Value),
    /// The router, a tool or the Host could not do its part.
    Failed,
}

/// What a routing needs next.
#[derive(Debug, Clone, PartialEq)]
pub enum Step {
    /// The user is to have been quiet for a while: then [`Fed::Quiet`].
    Wait,
    /// The router is to be asked this ([`request`]): then [`Fed::Answer`].
    Ask(Value),
    /// This is to be written where the agent can read it: then [`Fed::Written`].
    Write(Value),
    /// A tool of the fleet is to do this: then [`Fed::Receipt`]. `admitted`: the number of
    /// the talk and the place the action was written down as begun under
    /// ([`Conversation::begun`]); its receipt is to be written with it.
    Do {
        name: String,
        arguments: Value,
        call_id: String,
        admitted: (i64, usize),
    },
    /// The routing is over.
    Over,
}

/// What a router is asked with: the question as a text, and what it is asked with every time.
pub fn request(question: &Value) -> Value {
    let router = voice::router();
    json!({
        "model": router["model"],
        "instructions": router["instructions"],
        "input": python::dumps(question),
        "tools": router["tools"],
        "max_output_tokens": ANSWER_TOKENS,
        "reasoning": { "effort": "low" },
        "parallel_tool_calls": true,
    })
}

/// The words of the user that no routing took up yet. What was kept before fragments had
/// numbers is new only until something was routed.
fn current_requests(talk: &[Fragment], boundary: i64) -> Vec<usize> {
    let new = |row: &Fragment| match row.revision {
        None => boundary == 0,
        Some(revision) => revision > boundary,
    };
    let rows = talk.iter().enumerate();
    rows.filter(|(_, row)| row.of_user() && row.says_something() && new(row))
        .map(|(at, _)| at)
        .collect()
}

/// The user's turn: their words since the voice last spoke that no routing took up, as one
/// text. A voice cuts one utterance into fragments at every pause.
fn current_utterance(talk: &[Fragment], boundary: i64) -> String {
    let mut since = Vec::new();
    for (at, row) in talk.iter().enumerate() {
        if row.role == "assistant" && row.says_something() {
            since.clear();
        } else if row.of_user() {
            since.push(at);
        }
    }
    let new = current_requests(talk, boundary);
    let texts = since.into_iter().filter(|at| new.contains(at));
    let texts: Vec<&str> = texts.map(|at| python::strip(&talk[at].text)).collect();
    texts.join(" ")
}

/// What of a request is said back with its receipt: the user's own words where it has them
/// marked, without what was added for the agent's eyes.
fn request_excerpt(request: &str) -> &str {
    let own = request
        .split_once(OWN_WORDS)
        .map_or(request, |(_, own)| own);
    let own = own.split_once(REFERENCE).map_or(own, |(own, _)| own);
    python::first(own, EXCERPT_SHOWN)
}

/// What is added to a request for its agent: where the talk it came from is written.
fn provenance_note(path: &str) -> String {
    format!(
        "{REFERENCE}\nRead the immutable recent voice-transcription excerpt at {path} before \
         acting. The router summary may omit or misstate clauses. Resolve the current request \
         from the user's original wording, including corrections and limits. Earlier dialogue \
         is context, not permission to repeat old actions. This is a transcription excerpt, \
         not raw audio. If scope remains ambiguous, ask.\n</oracle-reference-data>"
    )
}

impl Route {
    /// The id the voice gave its wanting.
    pub fn ident(&self) -> &str {
        &self.ident
    }
}

impl Conversation {
    /// A routing of what the voice wanted done under this id ([`Self::next_wanted`]).
    pub fn route(&self, ident: &str) -> Route {
        Route {
            ident: ident.to_string(),
            tries: 0,
            revision: 0,
            talk: Vec::new(),
            index: 0,
            covered: BTreeSet::new(),
            items: VecDeque::new(),
            doing: None,
            over: false,
        }
    }

    /// From when the user has been quiet long enough for what they asked to be routed.
    pub fn quiet_at(&self) -> i64 {
        self.last_transcript.saturating_add(ROUTE_QUIET_MS)
    }

    /// The next step of a routing. `rows`: the delegations of this call, as the book has
    /// them now.
    pub fn route_step(
        &mut self,
        route: &mut Route,
        fed: Fed,
        rows: &[Delegation],
        wire: &mut dyn Wire,
        now: Now,
    ) -> Step {
        if route.over {
            return Step::Over;
        }
        let stepped = match fed {
            // While the call is put through to an agent nothing is routed.
            Fed::Begin if self.held => Ok(Step::Over),
            Fed::Begin => Ok(Step::Wait),
            Fed::Quiet {
                roster,
                contact_there,
            } => self.looked(route, (roster, contact_there), rows, wire, now),
            Fed::Answer(answer) => self.answered(route, answer, rows, wire, now),
            Fed::Written(path) => self.written(route, path, now),
            Fed::Receipt(receipt) => self.received(route, receipt, rows, wire, now),
            Fed::Failed => Err(Failed),
        };
        let step = stepped.unwrap_or_else(|Failed| {
            wire.phone(json!({ "type": "oracle_v2.notice", "message": ROUTE_FAILED }));
            Step::Over
        });
        if step == Step::Over {
            route.over = true;
            self.routing = self.routing.saturating_sub(1);
        }
        step
    }

    /// The user is quiet: what there is to route, and the question for the router.
    fn looked(
        &mut self,
        route: &mut Route,
        (roster, contact_there): (&Value, bool),
        rows: &[Delegation],
        wire: &mut dyn Wire,
        now: Now,
    ) -> Result<Step, Failed> {
        route.tries += 1;
        route.revision = self.revision;
        route.talk.clone_from(&self.fragments);
        let current = current_requests(&route.talk, self.routed_revision);
        if route.revision <= self.routed_revision && current.is_empty() {
            return Ok(Step::Over);
        }
        // What was said before this connection, and what an action was begun for, is not
        // routed again.
        let begun =
            route.revision <= self.resume_revision || self.admitted.contains(&route.revision);
        if begun {
            return Ok(Step::Over);
        }
        if self.strategy == Strategy::DirectContact {
            let proposal = self.direct_proposal(route, contact_there)?;
            return self.answered(route, &proposal, rows, wire, now);
        }
        let mut tasks: Vec<&Delegation> = rows.iter().collect();
        tasks.sort_by_key(|row| Reverse(row.created_at));
        let tasks = tasks.into_iter().take(TASKS_SHOWN);
        let tasks: Vec<Value> = tasks.map(|row| self.task_record(row)).collect();
        let current: Vec<&Fragment> = current.into_iter().map(|at| &route.talk[at]).collect();
        Ok(Step::Ask(json!({
            "conversation": route.talk,
            "roster": roster,
            "authoritative_tasks": tasks,
            "current_user_requests": current,
            "routing_boundary": self.routed_revision,
        })))
    }

    /// A task of the call as the router is shown it, with how far its answer was read.
    fn task_record(&self, row: &Delegation) -> Value {
        let answer = if row.result_text.is_empty() {
            &row.error
        } else {
            &row.result_text
        };
        let mut record = json!({
            "operation_id": row.delegation_id,
            "agent": row.session,
            "status": row.status.word(),
            "request": python::first(&row.request_text, REQUEST_SHOWN),
            "result": python::first(answer, RESULT_SHOWN),
            "result_truncated": answer.chars().count() > RESULT_SHOWN,
        });
        if self.background.contains(&row.delegation_id) {
            record["from_earlier_call"] = json!(true);
        }
        if let Some(relay) = self.relays.get(&row.delegation_id) {
            record["relay"] = json!({
                "parts_total": relay.total(),
                "parts_sent": relay.sent,
                "next_part": relay.next + 1,
            });
        }
        record
    }

    /// The one action of a turn in which everything goes to the contact: the user's words
    /// are handed to it, unread by the Host.
    fn direct_proposal(&self, route: &Route, contact_there: bool) -> Result<Value, Failed> {
        // A contact can be gone after the call began.
        if self.contact.is_empty() || !contact_there {
            return Err(Failed);
        }
        let turn = current_utterance(&route.talk, self.routed_revision);
        let mut said = route.talk.iter().rev();
        let newest = said.find(|row| row.of_user() && row.says_something());
        let latest = if turn.is_empty() {
            newest.map_or("", |row| row.text.as_str())
        } else {
            turn.as_str()
        };
        if latest.is_empty() {
            let content = json!([{ "type": "output_text", "text": NOTHING_TO_HAND }]);
            return Ok(json!({ "output": [{ "type": "message", "content": content }] }));
        }
        let words = if latest.chars().count() <= WORDS_HANDED {
            latest
        } else {
            TOO_MANY_WORDS
        };
        let request = format!("{TO_THE_CONTACT}{OWN_WORDS}{words}");
        Ok(json!({ "output": [{
            "type": "function_call",
            "name": "investigate_with_oracle",
            "call_id": format!("direct-{}-{}", self.direct_prefix, route.revision),
            "arguments": python::dumps_ascii(&json!({ "request": request })),
        }] }))
    }

    /// The router answered. Where the user went on talking meanwhile it is asked again, a
    /// few times; else what it answered is done.
    fn answered(
        &mut self,
        route: &mut Route,
        answer: &Value,
        rows: &[Delegation],
        wire: &mut dyn Wire,
        now: Now,
    ) -> Result<Step, Failed> {
        if route.revision != self.revision {
            let again = route.tries < TRIES;
            return Ok(if again { Step::Wait } else { Step::Over });
        }
        let answer = answer.as_object().ok_or(Failed)?;
        route.items = match answer.get("output") {
            None => VecDeque::new(),
            Some(output) => output.as_array().ok_or(Failed)?.iter().cloned().collect(),
        };
        self.next_action(route, rows, wire, now)
    }

    /// Goes through what the router answered until an action needs the Host, or nothing is
    /// left. The same action twice is done once.
    fn next_action(
        &mut self,
        route: &mut Route,
        rows: &[Delegation],
        wire: &mut dyn Wire,
        now: Now,
    ) -> Result<Step, Failed> {
        // The user spoke: what follows is left.
        while route.revision == self.revision
            && let Some(item) = route.items.pop_front()
        {
            let item = item.as_object().ok_or(Failed)?;
            match item.get("type").and_then(Value::as_str) {
                Some("function_call") => {
                    let written = item.get("arguments").and_then(Value::as_str);
                    let arguments: Value = written
                        .and_then(|written| serde_json::from_str(written).ok())
                        .ok_or(Failed)?;
                    let name = item.get("name").and_then(Value::as_str).ok_or(Failed)?;
                    let action = (name.to_string(), python::canonical(&arguments));
                    if !route.covered.insert(action) {
                        route.index += 1;
                        continue;
                    }
                    if name == "read_result" {
                        // From the text that is kept: nothing is begun, no agent is asked.
                        let asked = arguments.as_object().ok_or(Failed)?;
                        let of = |name: &str| asked.get(name).unwrap_or(&Value::Null);
                        let which = (of("operation_id"), of("from_part"), of("verbatim"));
                        self.read_result(which, rows, wire, now);
                        route.index += 1;
                        continue;
                    }
                    // It is written down as begun before anything is done: under a name
                    // that is the same whenever this place of this talk is asked for.
                    let place = (route.revision, route.index);
                    let named = format!("{}:{}:{}", self.thread_id, place.0, place.1);
                    let call_id = format!("oracle-{}", python::first(&digest(&named), 40));
                    self.admitted.insert(route.revision);
                    self.admissions.push(Admission {
                        revision: route.revision,
                        index: route.index,
                        call_id: call_id.clone(),
                        tool: name.to_string(),
                        arguments: python::canonical(&arguments),
                    });
                    let asked = arguments.as_object().ok_or(Failed)?;
                    let hands = matches!(
                        name,
                        "delegate_to_agent" | "investigate_with_oracle" | "cancel_agent"
                    );
                    let request = asked
                        .get("request")
                        .filter(|request| python::truthy(request));
                    let provenance = match request.filter(|_| hands) {
                        None => None,
                        Some(request) => {
                            let router_call = item.get("call_id").ok_or(Failed)?;
                            let agent = asked.get("agent").filter(|agent| python::truthy(agent));
                            let target = agent.cloned().unwrap_or_else(|| json!(self.contact));
                            let binding = json!({
                                "delegation_id": route.ident,
                                "revision": route.revision,
                                "call_id": router_call,
                                "tool": name,
                                "target_session": target,
                                "owner_principal": self.principal,
                                "voice_session_id": null,
                                "recent_fragment_limit": 30,
                            });
                            Some(self.provenance(route, binding, request))
                        }
                    };
                    route.doing = Some(Doing {
                        name: name.to_string(),
                        admitted: place,
                        arguments,
                        call_id,
                    });
                    return match provenance {
                        Some(provenance) => Ok(Step::Write(provenance)),
                        None => handed(route),
                    };
                }
                Some("message") => {
                    let content = match item.get("content") {
                        None => &[][..],
                        Some(content) => content.as_array().ok_or(Failed)?.as_slice(),
                    };
                    let mut text = String::new();
                    for part in content {
                        let part = part.as_object().ok_or(Failed)?;
                        if part.get("type").is_some_and(|kind| kind == "output_text") {
                            let words = part.get("text").map_or(Some(""), Value::as_str);
                            text += words.ok_or(Failed)?;
                        }
                    }
                    if !text.is_empty() {
                        self.append("commentary", &text, wire);
                    }
                }
                _ => {}
            }
        }
        // What was begun stands; what the user said since waits for the next routing.
        self.routed_revision = route.revision;
        self.checkpoint(now);
        Ok(Step::Over)
    }

    /// Where a request came from, for the agent that is handed it: a router's summary may
    /// leave out or misstate what the user said, so their words go with it.
    fn provenance(&self, route: &Route, binding: Value, request: &Value) -> Value {
        let of_user = route.talk.iter().filter(|row| row.of_user());
        let words: Vec<&str> = of_user.map(|row| row.text.as_str()).collect();
        json!({
            "schema": 1,
            "history_is_excerpt": true,
            "source": "live_voice_transcription_not_raw_audio",
            "turn_boundaries_verified": false,
            "scope": "Recent retained fragments only; not the complete conversation",
            "handoff_binding": binding,
            "router_summary": request,
            "conversation": route.talk,
            "original_user_messages": words,
        })
    }

    /// The file was written: its place is added to the request, and the request handed on.
    fn written(&mut self, route: &mut Route, path: &str, now: Now) -> Result<Step, Failed> {
        let doing = route.doing.as_mut().ok_or(Failed)?;
        let request = doing.arguments["request"].as_str().ok_or(Failed)?;
        doing.arguments["request"] = json!(format!("{request}{}", provenance_note(path)));
        if route.revision != self.revision {
            // The user corrected themselves meanwhile: the older summary is not handed on.
            route.doing = None;
            self.routed_revision = route.revision;
            self.checkpoint(now);
            return Ok(Step::Over);
        }
        handed(route)
    }

    /// The tool answered: the voice is told what came of it, and the next action is taken up.
    fn received(
        &mut self,
        route: &mut Route,
        receipt: &Value,
        rows: &[Delegation],
        wire: &mut dyn Wire,
        now: Now,
    ) -> Result<Step, Failed> {
        let doing = route.doing.take().ok_or(Failed)?;
        // What the tools handed on is this call's own, whatever came of it.
        if let Some(operation) = receipt["operation_id"].as_str().filter(|id| !id.is_empty()) {
            self.owns(operation);
        }
        route.index += 1;
        self.checkpoint(now);
        // The action was done, whatever its tool answered: only then is an answer that is
        // no record a failure.
        receipt.as_object().ok_or(Failed)?;
        let status = receipt["status"].as_str().unwrap_or("");
        let handed_on = status == "accepted" || status == "queued";
        if handed_on {
            self.cue("handed_off", "", wire, now);
        }
        if handed_on && self.strategy == Strategy::DirectContact {
            let request = doing.arguments["request"].as_str().unwrap_or("");
            let quiet = if self.narration == "off" {
                NOT_ALOUD
            } else {
                ""
            };
            let facts = json!({
                "agent": self.contact,
                "operation_id": receipt["operation_id"],
                "status": status,
                "original_request_excerpt": request_excerpt(request),
            });
            let text = format!("{RECEIPT_ONLY}{quiet} {}", python::dumps(&facts));
            self.append("thinking", &text, wire);
        }
        if !handed_on && !python::truthy(&receipt["cancelled"]) {
            let delivered = self.deliver_tool_output(&doing.name, receipt, wire, now);
            delivered.map_err(|_| Failed)?;
        }
        self.next_action(route, rows, wire, now)
    }
}

/// The action that waits is handed to its tool.
fn handed(route: &Route) -> Result<Step, Failed> {
    let doing = route.doing.as_ref().ok_or(Failed)?;
    Ok(Step::Do {
        name: doing.name.clone(),
        arguments: doing.arguments.clone(),
        call_id: doing.call_id.clone(),
        admitted: doing.admitted,
    })
}
